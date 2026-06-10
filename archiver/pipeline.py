"""아카이빙 파이프라인 — capture → extract → 중복 검사 → 저장.

CLI `add`와 대시보드 재아카이빙이 공유하는 유일한 쓰기 진입점.
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from . import capture, config, db, extract, storage


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


def archive_url(url: str, force: bool = False) -> ArchiveOutcome:
    """URL 아카이빙 전체 흐름.

    잘못된 URL은 ValueError, 캡처 실패는 capture.CaptureError 를 던진다.
    해시가 직전 스냅샷과 같으면 checks 기록만 남긴다 (force 시 예외).
    """
    norm = storage.normalize_url(url)
    domain = urlsplit(norm).hostname or ""
    slug = storage.url_to_slug(norm)

    rules = config.load_domain_rules(domain)

    # 해시가 같으면 스냅샷 디렉토리를 만들지 않도록 임시 디렉토리에 먼저 캡처
    tmp_dir = Path(tempfile.mkdtemp(prefix="archiver-"))
    try:
        result = capture.capture(
            norm, tmp_dir,
            remove_selectors=tuple(rules.get("remove_selectors") or ()),
        )
        text = extract.extract_text(result.content_html, norm)
        normalized = extract.normalize(
            text, drop_line_patterns=tuple(rules.get("remove_line_patterns") or ())
        )
        content_hash = storage.content_sha256(normalized)

        with db.connect() as conn:
            page_id = db.get_or_create_page(conn, norm, domain, slug)
            prev = db.last_snapshot(conn, page_id)

            if prev and prev["content_hash"] == content_hash and not force:
                db.insert_check(conn, page_id, content_hash)
                return ArchiveOutcome(
                    status="unchanged", url=norm, content_hash=content_hash,
                    snapshot_dir=None, taken_at=None,
                    last_taken_at=prev["taken_at"],
                    http_status=result.http_status, title=result.title,
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
            db.insert_snapshot(
                conn, page_id,
                taken_at=meta.taken_at, dir_name=snap_dir.name,
                content_hash=content_hash, final_url=result.final_url,
                http_status=result.http_status, changed=changed,
            )
            status = "new" if prev is None else ("changed" if changed else "forced_same")
            return ArchiveOutcome(
                status=status, url=norm, content_hash=content_hash,
                snapshot_dir=snap_dir, taken_at=meta.taken_at,
                last_taken_at=prev["taken_at"] if prev else None,
                http_status=result.http_status, title=result.title,
            )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
