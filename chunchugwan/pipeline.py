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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from . import capture, config, db, extract, resources, storage

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


def archive_url(url: str, force: bool = False, source: str = "cli") -> ArchiveOutcome:
    """URL 아카이빙 전체 흐름.

    잘못된 URL은 ValueError, 캡처 실패는 capture.CaptureError 를 던진다.
    해시가 직전 스냅샷과 같으면 checks 기록만 남긴다 (force 시 예외).
    source 는 실행 주체('cli' | 'web' | 'schedule') — archive_logs 에 기록된다.
    """
    run = _RunLog(url, source)
    try:
        return _archive_url(url, force, run)
    except Exception as e:
        _log_failure(run, e)
        raise


def _archive_url(url: str, force: bool, run: _RunLog) -> ArchiveOutcome:
    norm = storage.normalize_url(url)
    domain = urlsplit(norm).hostname or ""
    slug = storage.url_to_slug(norm)
    run.url, run.domain = norm, domain

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
            )
        run.step(
            "capture",
            f"http {result.http_status or '-'} · 최종 URL {result.final_url} · "
            f"제목 {result.title or '-'}",
        )
        text = extract.extract_text(result.content_html, norm)
        normalized = extract.normalize(
            text, drop_line_patterns=tuple(rules.get("remove_line_patterns") or ())
        )
        run.step("extract", f"본문 {len(text)}자 → 정규화 {len(normalized)}자")
        content_hash = storage.content_sha256(normalized)
        run.step("hash", f"sha256 {content_hash[:12]}")

        with db.connect() as conn:
            page_id = db.get_or_create_page(conn, norm, domain, slug)
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
            )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
