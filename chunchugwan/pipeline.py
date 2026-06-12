"""아카이빙 파이프라인 — capture → extract → 중복 검사 → 저장.

CLI `add`와 대시보드 재아카이빙이 공유하는 유일한 쓰기 진입점.
모든 실행은 성공/실패를 불문하고 archive_logs 테이블에 단계별
소요시간과 함께 기록된다 (대시보드 /logs 에서 조회).
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from . import capture, config, db, documents, extract, netcheck, resources, storage

logger = logging.getLogger(__name__)


@dataclass
class ArchiveOutcome:
    status: str                # "new" | "changed" | "unchanged" | "forced_same"
    url: str                   # 정규화 URL
    content_hash: str
    snapshot_dir: Path | None  # unchanged 면 None
    taken_at: str | None
    last_taken_at: str | None  # 직전 스냅샷 시각 (없으면 None)
    http_status: int | None
    title: str | None
    documents: int = 0         # 함께 저장된 문서 파일 수
    # 이 실행으로 확인된 스냅샷 id — 새로 만든 스냅샷 또는 (unchanged 시)
    # 내용이 같았던 직전 스냅샷. 크롤러가 크롤 세트에 기록한다.
    snapshot_id: int | None = None
    # 페이지의 앵커 href (절대 URL) — 크롤러의 링크 추적용
    page_links: list[str] = field(default_factory=list)


class _RunLog:
    """단계별 소요시간/결과를 모아 archive_logs 한 행으로 기록하는 수집기."""

    def __init__(self, url: str, source: str) -> None:
        self.url = url          # normalize 성공 시 정규화 URL로 교체
        self.domain = ""
        self.source = source
        self.started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._t0 = time.monotonic()
        self._t_step = self._t0
        self.steps: list[dict[str, object]] = []

    def step(self, name: str, detail: str) -> None:
        """직전 step 이후의 경과 시간을 name 단계로 기록."""
        now = time.monotonic()
        ms = int((now - self._t_step) * 1000)
        self._t_step = now
        self.steps.append({"step": name, "ms": ms, "detail": detail})
        logger.info("[%s] %s %dms — %s", self.url, name, ms, detail)

    def write(
        self,
        conn: sqlite3.Connection,
        *,
        status: str,
        page_id: int | None = None,
        snapshot_id: int | None = None,
        content_hash: str | None = None,
        http_status: int | None = None,
        error: str | None = None,
    ) -> None:
        """수집된 단계들과 함께 로그 행을 삽입."""
        db.insert_archive_log(
            conn,
            url=self.url, domain=self.domain,
            page_id=page_id, snapshot_id=snapshot_id,
            source=self.source, status=status,
            started_at=self.started_at,
            duration_ms=int((time.monotonic() - self._t0) * 1000),
            http_status=http_status, content_hash=content_hash,
            error=error,
            steps=json.dumps(self.steps, ensure_ascii=False),
        )


def _log_failure(run: _RunLog, exc: Exception) -> None:
    """실패도 archive_logs 에 남긴다. 기록 실패가 원래 예외를 가리지 않게 한다."""
    logger.exception("아카이빙 실패: %s", run.url)
    try:
        with db.connect() as conn:
            page = db.get_page(conn, run.url)
            run.write(
                conn, status="error",
                page_id=page["id"] if page else None,
                error=f"{type(exc).__name__}: {exc}",
            )
    except Exception:
        logger.exception("archive_logs 기록 실패: %s", run.url)


def archive_url(
    url: str,
    force: bool = False,
    source: str = "cli",
    link_rewriter: capture.LinkRewriter | None = None,
    browser_session: capture.BrowserSession | None = None,
    network_tag_id: str | None = None,
) -> ArchiveOutcome:
    """URL 아카이빙 전체 흐름.

    잘못된 URL은 ValueError, 캡처 실패는 capture.CaptureError 를 던진다.
    해시가 직전 스냅샷과 같으면 checks 기록만 남긴다 (force 시 예외).
    source 는 실행 주체('cli' | 'web' | 'schedule' | 'api' | 'crawl') —
    archive_logs 에 기록된다. link_rewriter 는 사이트 전체 아카이브용
    page.html 앵커 재작성, browser_session 은 크롤러의 브라우저 재사용
    (둘 다 capture 참조).

    네트워크 게이트(netcheck): 루프백 주소는 항상 ValueError. 사설 대역은
    network_tag_id(시스템 설정의 로컬 네트워크 태그) 또는 기존 페이지의
    태그가 있어야 한다 — 없으면 ValueError. 공인 주소면 태그는 무시된다.
    """
    run = _RunLog(url, source)
    try:
        return _archive_url(url, force, run, link_rewriter, browser_session,
                            network_tag_id)
    except Exception as e:
        _log_failure(run, e)
        raise


def _resolve_network_tag(norm: str, host: str, requested: str | None) -> str | None:
    """네트워크 게이트 — 루프백 금지, 사설 대역은 로컬 네트워크 태그 필수.

    반환값은 페이지에 기록할 태그 id. 사설 대역이 아니면 None (공인 주소에
    태그를 넘겨도 무시). 태그 요청이 없으면 기존 페이지의 태그를 물려받아
    스케줄·재아카이빙이 태그 재지정 없이 동작한다. 위반은 ValueError.
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
    raise ValueError(
        "로컬 네트워크(사설 IP) 주소는 로컬 네트워크 태그를 지정해야 "
        "아카이빙할 수 있습니다 — 시스템 화면에서 태그를 만들고 "
        "새 아카이빙 화면에서 선택하세요"
    )


def _resource_fallback(url: str) -> tuple[str, bytes] | None:
    """자원 인라인 실패 폴백 — 같은 URL 로 저장된 과거 캡처본(자원 CAS) 조회.

    snapshot_resources 에서 URL 의 가장 최근 자원 이름을 찾아 CAS 콘텐츠와
    미디어 타입을 돌려준다. 폴백 실패가 캡처를 막지 않도록 예외는 삼킨다.
    """
    try:
        with db.connect() as conn:
            name = db.find_resource_by_url(conn, url)
        if name is None or not resources.is_valid_name(name):
            return None
        path = resources.resource_path(name)
        if not path.is_file():
            return None
        media = resources.EXT_MEDIA_TYPES.get(
            Path(name).suffix, "application/octet-stream"
        )
        return media.split(";")[0].strip(), path.read_bytes()
    except Exception:
        logger.warning("자원 폴백 조회 실패, 건너뜀: %s", url, exc_info=True)
        return None


def _archive_url(
    url: str,
    force: bool,
    run: _RunLog,
    link_rewriter: capture.LinkRewriter | None = None,
    browser_session: capture.BrowserSession | None = None,
    network_tag_id: str | None = None,
) -> ArchiveOutcome:
    norm = storage.normalize_url(url)
    domain = urlsplit(norm).hostname or ""
    slug = storage.url_to_slug(norm)
    run.url, run.domain = norm, domain

    network_tag_id = _resolve_network_tag(norm, domain, network_tag_id)
    if network_tag_id:
        run.step("netcheck", f"사설 대역 — 로컬 네트워크 태그 {network_tag_id}")

    rules = config.load_domain_rules(domain)
    run.step("normalize", f"{norm} → {domain}/{slug}"
             + (" (도메인 룰 적용)" if rules else ""))

    # 해시가 같으면 스냅샷 디렉토리를 만들지 않도록 임시 디렉토리에 먼저 캡처
    tmp_dir = Path(tempfile.mkdtemp(prefix="wccg-"))
    try:
        try:
            result = capture.capture(
                norm, tmp_dir,
                remove_selectors=tuple(rules.get("remove_selectors") or ()),
                link_rewriter=link_rewriter,
                session=browser_session,
                resource_fallback=_resource_fallback,
            )
        except capture.CaptureError as e:
            # HTTP 전용 사이트(443 닫힘 등)일 수 있으므로 http 로 한 번 더 시도한다:
            # 스킴 생략 입력에 https 를 추정 보완한 경우는 모든 캡처 실패에서,
            # 명시적 https 는 서버 연결 자체가 안 된 실패에 한해서만.
            retriable = storage.scheme_inferred(url) or isinstance(
                e, capture.CaptureConnectError
            )
            if not (retriable and norm.startswith("https://")):
                raise
            run.step("capture", f"https 캡처 실패 — http 로 재시도: {str(e).splitlines()[0]}")
            norm = "http://" + norm.removeprefix("https://")
            slug = storage.url_to_slug(norm)
            run.url = norm
            result = capture.capture(
                norm, tmp_dir,
                remove_selectors=tuple(rules.get("remove_selectors") or ()),
                link_rewriter=link_rewriter,
                session=browser_session,
                resource_fallback=_resource_fallback,
            )
        run.step(
            "capture",
            f"http {result.http_status or '-'} · 최종 URL {result.final_url} · "
            f"제목 {result.title or '-'}",
        )
        # 리다이렉트가 게이트를 우회하지 못하게 최종 URL 호스트도 판정한다.
        # 요청 자체는 이미 일어났지만 아카이브에는 아무것도 남기지 않는다.
        final_host = urlsplit(result.final_url).hostname or ""
        if final_host and final_host != domain:
            final_kind = netcheck.classify_host(final_host)
            if final_kind == netcheck.LOOPBACK:
                raise ValueError(
                    f"최종 URL 이 루프백 주소입니다 — 저장 중단: {result.final_url}"
                )
            if final_kind == netcheck.PRIVATE and network_tag_id is None:
                raise ValueError(
                    "최종 URL 이 로컬 네트워크(사설 IP) 주소입니다 — 로컬 네트워크 "
                    f"태그 없이 저장할 수 없습니다: {result.final_url}"
                )
        text = extract.extract_text(result.content_html, norm)
        normalized = extract.normalize(
            text, drop_line_patterns=tuple(rules.get("remove_line_patterns") or ())
        )
        run.step("extract", f"본문 {len(text)}자 → 정규화 {len(normalized)}자")
        content_hash = storage.content_sha256(normalized)
        run.step("hash", f"sha256 {content_hash[:12]}")

        with db.connect() as conn:
            page_id = db.get_or_create_page(
                conn, norm, domain, slug, network_tag_id=network_tag_id
            )
            prev = db.last_snapshot(conn, page_id)

            if prev and prev["content_hash"] == content_hash and not force:
                db.insert_check(conn, page_id, content_hash)
                run.step("decide", f"직전 스냅샷({prev['taken_at']})과 동일 — 저장 생략")
                run.write(
                    conn, status="unchanged", page_id=page_id,
                    content_hash=content_hash, http_status=result.http_status,
                )
                return ArchiveOutcome(
                    status="unchanged", url=norm, content_hash=content_hash,
                    snapshot_dir=None, taken_at=None,
                    last_taken_at=prev["taken_at"],
                    http_status=result.http_status, title=result.title,
                    snapshot_id=prev["id"], page_links=result.page_links,
                )

            # 저장이 확정된 뒤에만 문서 다운로드 — unchanged 면 받지 않는다
            # (주기적 재아카이빙에서 변경 없는 페이지의 문서를 매번 다시
            # 받는 낭비 방지). 다운로드 실패는 아카이빙을 막지 않는다.
            # 받은 문서는 문서 CAS 로 이동 — 같은 내용은 스냅샷·페이지가
            # 달라도 한 번만 저장된다 (참조는 snapshot_documents 행).
            doc_manifest: list[dict] = []
            if result.document_links:
                doc_manifest, doc_failed = documents.download_documents(
                    result.document_links, tmp_dir / "files",
                    referer=result.final_url,
                )
                documents.ingest_into_cas(tmp_dir / "files", doc_manifest)
                run.step(
                    "documents",
                    f"문서 링크 {len(result.document_links)}개 → "
                    f"{len(doc_manifest)}개 저장"
                    + (f" · 실패 {len(doc_failed)}개" if doc_failed else ""),
                )

            # 저장이 확정된 뒤에만 압축 변환 — unchanged 면 CAS 에 자원을
            # 남기지 않고 임시 디렉토리째 버려진다
            stats = resources.compact_snapshot_dir(tmp_dir)
            run.step(
                "compress",
                f"자원 {stats.externalized}개 추출 · "
                f"{stats.before_bytes // 1024}KB → {stats.after_bytes // 1024}KB",
            )

            taken_at = datetime.now(timezone.utc)
            meta = storage.SnapshotMeta(
                url=norm,
                final_url=result.final_url,
                taken_at=taken_at.isoformat(timespec="seconds"),
                content_hash=content_hash,
                http_status=result.http_status,
                title=result.title,
                documents=doc_manifest or None,
            )
            snap_dir = storage.finalize_snapshot(
                tmp_dir, domain, slug, meta, normalized, taken_at
            )
            changed = 1 if prev is None else int(prev["content_hash"] != content_hash)
            snapshot_id = db.insert_snapshot(
                conn, page_id,
                taken_at=meta.taken_at, dir_name=snap_dir.name,
                content_hash=content_hash, final_url=result.final_url,
                http_status=result.http_status, changed=changed,
                resources_indexed=1,  # 참조는 바로 아래에서 기록 — 백필 불필요
            )
            if doc_manifest:
                db.insert_snapshot_documents(conn, snapshot_id, doc_manifest)
            if stats.resource_names:
                # CAS 추출 자원의 참조 기록 — 삭제 GC 와 URL 폴백의 근거.
                # 원본 URL 은 캡처가 기록한 sha256 매핑에서 찾는다 (없으면 NULL)
                db.insert_snapshot_resources(
                    conn, snapshot_id,
                    [
                        {"name": n, "url": result.resource_urls.get(n[:64])}
                        for n in stats.resource_names
                    ],
                )
            status = "new" if prev is None else ("changed" if changed else "forced_same")
            run.step("store", f"스냅샷 저장 [{status}]: {snap_dir.name}")
            run.write(
                conn, status=status, page_id=page_id, snapshot_id=snapshot_id,
                content_hash=content_hash, http_status=result.http_status,
            )
            return ArchiveOutcome(
                status=status, url=norm, content_hash=content_hash,
                snapshot_dir=snap_dir, taken_at=meta.taken_at,
                last_taken_at=prev["taken_at"] if prev else None,
                http_status=result.http_status, title=result.title,
                documents=len(doc_manifest),
                snapshot_id=snapshot_id, page_links=result.page_links,
            )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
