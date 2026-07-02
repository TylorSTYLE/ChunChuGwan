"""브라우저 확장 클라이언트 캡처의 서버측 적재(ingest).

확장이 브라우저 내부에서 캡처(CDP 풀페이지 + 프레임 순회 + 이미 로드된 자원·
문서 재요청)해 **인라인 완성한** 산출물(page.html·raw.html·스크린샷·문서 바이트)을
업로드하면, 서버는 **대상 URL 을 다시 가져오지 않고**(capture.py 미호출) 기존
코어(extract·resources·storage·db·searchindex)로 추출·정규화·해시·중복판정·
CAS 분리·검색 색인·저장만 수행한다. 즉 `pipeline._archive_url` 의 흐름을
네트워크 캡처만 제외하고 그대로 미러링한다.

원칙(EXTENSION_CLIENT_CAPTURE_PLAN.md):
- 서버 무요청(불변식 1): 이 경로는 어떤 네트워크 fetch 도 하지 않는다.
- 업로드 바이트는 신뢰 불가(원칙 5): /resource CAS 는 resources 의 MIME
  화이트리스트가, 문서 CAS 는 documents 의 확장자 화이트리스트가 재검증한다.
  page.html 은 항상 샌드박스 iframe(스크립트 금지)에서만 렌더되므로 악성
  HTML 도 실행되지 않는다.
- 자격증명 미수신/미저장(원칙 6): 확장이 사용자의 실제 세션으로 자원을
  재요청하므로 이 경로엔 자격증명이 흐르지 않는다.
- 확장 캡처 페이지는 서버 재요청 차단(불변식 2): 저장 시 pages.client_captured=1.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from . import config, db, documents, extract, netcheck, resources, searchindex, storage
# 문서 스냅샷의 안내 page.html·메타데이터 본문은 서버 캡처 경로와 동일한 형태를
# 써서 같은 문서면 해시가 일치하도록 pipeline 의 헬퍼를 재사용한다.
from .pipeline import _document_content_text, _write_document_page_html


@dataclass
class IngestDocument:
    """확장이 재요청해 올린 문서 1개 (페이지가 링크했거나 URL 자체가 문서)."""

    url: str
    filename: str
    content_type: str
    data: bytes


@dataclass
class IngestResult:
    """적재 결과 — REST 가 동기 응답으로 변환한다."""

    status: str                 # "new" | "changed" | "unchanged" | "forced_same"
    url: str
    content_hash: str
    page_id: int | None = None
    snapshot_id: int | None = None
    changed: bool = False
    incomplete: bool = False
    snapshot_dir: Path | None = None


class NetworkTagRequired(Exception):
    """사설 호스트인데 network_tag 가 없을 때 — 확장에 태그 선택/추가를 요구한다.

    REST 가 이를 받아 {needs_network_tag: true, host} 응답으로 변환한다.
    루프백은 이 예외가 아니라 ValueError(하드 거부)로 막는다.
    """

    def __init__(self, host: str) -> None:
        self.host = host
        super().__init__(f"로컬 네트워크(사설 IP) 주소는 로컬 네트워크 태그가 필요합니다: {host}")


def _resolve_gate(norm: str, host: str, requested: str | None) -> str | None:
    """네트워크 게이트 — 루프백 금지, 사설 대역은 태그 필수 (원칙 7).

    pipeline._resolve_network_tag 와 같은 정책이되, 사설-무태그를 ValueError
    대신 NetworkTagRequired 로 구분해 확장이 태그를 고를 수 있게 한다.
    공인 주소면 태그를 무시하고 None 을 반환한다.
    """
    kind = netcheck.classify_host(host)
    if kind == netcheck.LOOPBACK:
        raise ValueError(f"루프백 주소는 아카이빙할 수 없습니다: {host}")
    if kind != netcheck.PRIVATE:
        return None
    with db.connect() as conn:
        if requested is not None:
            if db.get_network_tag(conn, requested) is None:
                raise ValueError(f"등록되지 않은 로컬 네트워크 태그: {requested}")
            return requested
        page = db.get_page(conn, norm)
        if page is not None and page["network_tag_id"]:
            return page["network_tag_id"]
    raise NetworkTagRequired(host)


def _doc_entry(doc: IngestDocument, final_url: str) -> dict | None:
    """업로드 문서 1개를 manifest 항목으로 — 확장자 화이트리스트 밖이면 None.

    파일명은 documents 의 추론·검증을 거친다(supplied 파일명을
    Content-Disposition 으로 넘겨 우선 사용). 문서 화이트리스트(config.
    DOCUMENT_EXTENSIONS) 밖 확장자는 None 으로 떨궈 신뢰 불가 업로드를 막는다.
    """
    cd = f'attachment; filename="{doc.filename}"' if doc.filename else None
    fname = documents.direct_filename(doc.url, final_url or doc.url, cd, doc.content_type)
    if fname is None:
        return None
    return {
        "url": doc.url,
        "file": fname,
        "bytes": len(doc.data),
        "sha256": hashlib.sha256(doc.data).hexdigest(),
        "content_type": doc.content_type,
    }


def _store_documents(
    docs: list[IngestDocument], files_dir: Path, final_url: str
) -> list[dict]:
    """업로드 문서들을 files_dir 에 쓰고 문서 CAS 로 이동 → 최종 manifest 반환.

    네트워크 fetch 없음(바이트는 이미 업로드됨). ingest_into_cas 가 확장자
    검증 후 같은 sha256 은 한 번만 저장한다 — 검증 실패 항목은 자동 제외.
    """
    files_dir.mkdir(parents=True, exist_ok=True)
    manifest: list[dict] = []
    for doc in docs:
        entry = _doc_entry(doc, final_url)
        if entry is None:
            continue
        (files_dir / entry["file"]).write_bytes(doc.data)
        manifest.append(entry)
    documents.ingest_into_cas(files_dir, manifest)  # 검증 통과분만 manifest 에 남음
    return manifest


def _write_log(
    *, status: str, url: str, domain: str, page_id: int | None,
    snapshot_id: int | None, content_hash: str | None, http_status: int | None,
    requested_by: int | None, started_at: str, t0: float, steps: list[dict],
    error: str | None = None,
) -> None:
    """archive_logs 한 행 기록 (source='extension')."""
    with db.connect() as conn:
        db.insert_archive_log(
            conn, url=url, domain=domain, page_id=page_id, snapshot_id=snapshot_id,
            source="extension", requested_by=requested_by, status=status,
            started_at=started_at,
            duration_ms=int((time.monotonic() - t0) * 1000),
            http_status=http_status, content_hash=content_hash, error=error,
            steps=json.dumps(steps, ensure_ascii=False), job_id=None,
        )


def ingest_capture(
    *,
    url: str,
    page_html: str,
    raw_html: str,
    screenshot_png: bytes | None = None,
    final_url: str | None = None,
    title: str | None = None,
    http_status: int | None = None,
    documents_in: list[IngestDocument] | None = None,
    capture_env: dict | None = None,
    incomplete: bool = False,
    force: bool = False,
    network_tag: str | None = None,
    requested_by: int | None = None,
    protect: bool | None = None,
) -> IngestResult:
    """확장이 올린 페이지 캡처를 코어로 적재한다 (네트워크 없음).

    page_html 은 확장이 자원을 인라인 완성한 단일 파일 HTML, raw_html 은
    렌더 후 DOM(outerHTML — 추출·정규화·해시의 입력). screenshot_png 는 CDP
    풀페이지(없으면 스크린샷 생략). documents_in 은 확장이 재요청한 링크 문서.

    위반/실패: 루프백은 ValueError, 사설-무태그는 NetworkTagRequired.
    """
    t0 = time.monotonic()
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    norm = storage.normalize_url(url)
    domain = urlsplit(norm).hostname or ""
    slug = storage.url_to_slug(norm)
    final_url = final_url or norm

    network_tag_id = _resolve_gate(norm, domain, network_tag)

    rules = config.load_domain_rules(domain)
    text = extract.extract_text(raw_html, norm)
    normalized = extract.normalize(
        text, drop_line_patterns=tuple(rules.get("remove_line_patterns") or ())
    )
    content_hash = storage.content_sha256(normalized)
    steps = [{"step": "hash", "detail": f"sha256 {content_hash[:12]}"}]

    # --- 중복 판정 (짧은 트랜잭션) ---
    with db.connect() as conn:
        existing = db.get_page(conn, norm)
        prev = db.last_snapshot(conn, existing["id"]) if existing else None
        if prev and prev["content_hash"] == content_hash and not force:
            db.insert_check(conn, existing["id"], content_hash)
        prev_hash = prev["content_hash"] if prev else None
        prev_id = prev["id"] if prev else None
    if prev_hash == content_hash and not force:
        _write_log(
            status="unchanged", url=norm, domain=domain, page_id=existing["id"],
            snapshot_id=None, content_hash=content_hash, http_status=http_status,
            requested_by=requested_by, started_at=started_at, t0=t0, steps=steps,
        )
        return IngestResult(
            status="unchanged", url=norm, content_hash=content_hash,
            page_id=existing["id"], snapshot_id=prev_id, changed=False,
            incomplete=bool(incomplete),
        )

    tmp_dir = Path(tempfile.mkdtemp(prefix="wccg-ingest-"))
    try:
        # 업로드 산출물을 캡처 산출물과 같은 임시 레이아웃으로 배치
        (tmp_dir / "page.html").write_text(page_html, encoding="utf-8")
        (tmp_dir / "raw.html").write_text(raw_html, encoding="utf-8")
        if screenshot_png:
            (tmp_dir / "screenshot.png").write_bytes(screenshot_png)

        manifest: list[dict] = []
        if documents_in:
            manifest = _store_documents(documents_in, tmp_dir / "files", final_url)
            steps.append({"step": "documents", "detail": f"{len(manifest)}개 저장"})

        # 압축 변환 + 큰 자원/스타일 CAS 외부화 (MIME 화이트리스트 재검증 포함)
        stats = resources.compact_snapshot_dir(tmp_dir, final_url)
        steps.append({"step": "compress", "detail": f"자원 {stats.externalized}개 추출"})

        taken_at = datetime.now(timezone.utc)
        meta = storage.SnapshotMeta(
            url=norm, final_url=final_url,
            taken_at=taken_at.isoformat(timespec="seconds"),
            content_hash=content_hash, http_status=http_status, title=title,
            documents=manifest or None,
            origin="extension", incomplete=bool(incomplete), capture_env=capture_env,
        )
        snap_dir = storage.finalize_snapshot(tmp_dir, domain, slug, meta, normalized, taken_at)
        changed = 1 if prev_hash is None else int(prev_hash != content_hash)

        # --- 페이지·스냅샷·참조 기록 (원자적 트랜잭션) ---
        with db.connect() as conn:
            page_id = db.get_or_create_page(
                conn, norm, domain, slug, network_tag_id=network_tag_id
            )
            snapshot_id = db.insert_snapshot(
                conn, page_id,
                taken_at=meta.taken_at, dir_name=snap_dir.name,
                content_hash=content_hash, final_url=final_url,
                http_status=http_status, changed=changed,
                resources_indexed=1, css_externalized=1,
                bytes=storage.snapshot_dir_bytes(snap_dir),
                title=meta.title,
                origin="extension", incomplete=int(bool(incomplete)),
            )
            if manifest:
                db.insert_snapshot_documents(conn, snapshot_id, manifest)
            if stats.resource_names:
                # 확장 경로는 자원 원본 URL 매핑이 없어 url=None — GC·표시엔 충분
                db.insert_snapshot_resources(
                    conn, snapshot_id, [{"name": n, "url": None} for n in stats.resource_names]
                )
            db.set_page_client_captured(conn, page_id)  # 서버 재요청 차단 (불변식 2)
            # 클러스터 보호 선택 — 지정 시 page 보호값 + (새 사이트면) 사이트 기본값.
            if protect is not None:
                db.apply_archive_protect(
                    conn, page_id, protect=protect, site_protect_default=protect
                )

        # 검색 색인 (best-effort — 실패해도 search_indexed=0 로 두고 진행)
        with db.connect() as conn:
            try:
                if searchindex.index_snapshot(conn, snapshot_id):
                    steps.append({"step": "index", "detail": "검색 인덱스 반영"})
            except Exception:  # noqa: BLE001
                pass

        status = "new" if prev_hash is None else ("changed" if changed else "forced_same")
        _write_log(
            status=status, url=norm, domain=domain, page_id=page_id,
            snapshot_id=snapshot_id, content_hash=content_hash, http_status=http_status,
            requested_by=requested_by, started_at=started_at, t0=t0, steps=steps,
        )
        return IngestResult(
            status=status, url=norm, content_hash=content_hash, page_id=page_id,
            snapshot_id=snapshot_id, changed=bool(changed),
            incomplete=bool(incomplete), snapshot_dir=snap_dir,
        )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def ingest_document(
    *,
    url: str,
    document: IngestDocument,
    final_url: str | None = None,
    http_status: int | None = None,
    incomplete: bool = False,
    force: bool = False,
    network_tag: str | None = None,
    requested_by: int | None = None,
) -> IngestResult:
    """URL 자체가 문서(PDF 등)인 경우의 적재 — 문서 스냅샷.

    pipeline._archive_document_url 을 supplied bytes 버전으로 미러링: 문서 CAS
    저장 + 안내 page.html + 문서 메타데이터 content.md(파일 sha256 포함). 같은
    파일이면 직전 스냅샷과 해시가 같아 저장이 생략된다. raw.html·스크린샷 없음.
    """
    t0 = time.monotonic()
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    norm = storage.normalize_url(url)
    domain = urlsplit(norm).hostname or ""
    slug = storage.url_to_slug(norm)
    final_url = final_url or norm

    network_tag_id = _resolve_gate(norm, domain, network_tag)

    entry = _doc_entry(document, final_url)
    if entry is None:
        raise ValueError(f"문서 확장자가 화이트리스트 밖입니다: {document.filename!r}")
    text = _document_content_text(norm, entry)
    content_hash = storage.content_sha256(text)
    steps = [{"step": "hash", "detail": f"문서 sha256 {content_hash[:12]}"}]

    with db.connect() as conn:
        existing = db.get_page(conn, norm)
        prev = db.last_snapshot(conn, existing["id"]) if existing else None
        if prev and prev["content_hash"] == content_hash and not force:
            db.insert_check(conn, existing["id"], content_hash)
        prev_hash = prev["content_hash"] if prev else None
        prev_id = prev["id"] if prev else None
    if prev_hash == content_hash and not force:
        _write_log(
            status="unchanged", url=norm, domain=domain, page_id=existing["id"],
            snapshot_id=None, content_hash=content_hash, http_status=http_status,
            requested_by=requested_by, started_at=started_at, t0=t0, steps=steps,
        )
        return IngestResult(
            status="unchanged", url=norm, content_hash=content_hash,
            page_id=existing["id"], snapshot_id=prev_id, changed=False,
            incomplete=bool(incomplete),
        )

    tmp_dir = Path(tempfile.mkdtemp(prefix="wccg-ingest-"))
    try:
        (tmp_dir / "files").mkdir(parents=True, exist_ok=True)
        (tmp_dir / "files" / entry["file"]).write_bytes(document.data)
        manifest = [entry]
        documents.ingest_into_cas(tmp_dir / "files", manifest)
        if not manifest:
            raise ValueError(f"문서 CAS 저장 실패: {document.filename!r}")
        _write_document_page_html(tmp_dir, entry)
        resources.compact_snapshot_dir(tmp_dir, final_url)

        taken_at = datetime.now(timezone.utc)
        meta = storage.SnapshotMeta(
            url=norm, final_url=final_url,
            taken_at=taken_at.isoformat(timespec="seconds"),
            content_hash=content_hash, http_status=http_status,
            title=str(entry["file"]), documents=manifest,
            origin="extension", incomplete=bool(incomplete),
        )
        snap_dir = storage.finalize_snapshot(tmp_dir, domain, slug, meta, text, taken_at)
        changed = 1 if prev_hash is None else int(prev_hash != content_hash)

        with db.connect() as conn:
            page_id = db.get_or_create_page(
                conn, norm, domain, slug, network_tag_id=network_tag_id
            )
            snapshot_id = db.insert_snapshot(
                conn, page_id,
                taken_at=meta.taken_at, dir_name=snap_dir.name,
                content_hash=content_hash, final_url=final_url,
                http_status=http_status, changed=changed,
                resources_indexed=1, css_externalized=1,
                bytes=storage.snapshot_dir_bytes(snap_dir),
                title=meta.title,
                origin="extension", incomplete=int(bool(incomplete)),
            )
            db.insert_snapshot_documents(conn, snapshot_id, manifest)
            db.set_page_client_captured(conn, page_id)

        with db.connect() as conn:
            try:
                searchindex.index_snapshot(conn, snapshot_id)
            except Exception:  # noqa: BLE001
                pass

        status = "new" if prev_hash is None else ("changed" if changed else "forced_same")
        _write_log(
            status=status, url=norm, domain=domain, page_id=page_id,
            snapshot_id=snapshot_id, content_hash=content_hash, http_status=http_status,
            requested_by=requested_by, started_at=started_at, t0=t0, steps=steps,
        )
        return IngestResult(
            status=status, url=norm, content_hash=content_hash, page_id=page_id,
            snapshot_id=snapshot_id, changed=bool(changed),
            incomplete=bool(incomplete), snapshot_dir=snap_dir,
        )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
