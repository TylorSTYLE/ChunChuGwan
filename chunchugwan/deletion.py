"""아카이브 데이터 삭제 — 페이지 전체 또는 단일 스냅샷.

CLI 와 대시보드가 공유하는 유일한 삭제 진입점. 쓰기 규칙(CLAUDE.md 1번)에
따라 DB 는 db.py, 파일은 storage.py 를 통해서만 조작한다.

순서는 DB 확정(커밋) → 파일 삭제: 파일 삭제가 중간에 실패해도 인덱스는
일관되고, 남는 고아 디렉토리는 무해하다 (반대 순서면 UI 가 깨진 참조를 본다).
단일 스냅샷 삭제 시 다음 스냅샷의 changed 재계산은 db.delete_snapshot 이 한다.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import db, differ, storage


@dataclass
class DeleteResult:
    url: str                # 삭제 대상 페이지의 정규화 URL
    snapshots_deleted: int  # 함께 삭제된 스냅샷 수


def delete_snapshot(snapshot_id: int) -> DeleteResult | None:
    """스냅샷 하나를 DB·파일·diff 캐시에서 삭제. 없는 id 면 None."""
    with db.connect() as conn:
        snap = db.get_snapshot(conn, snapshot_id)
        if snap is None:
            return None
        db.delete_snapshot(conn, snapshot_id)
    storage.delete_snapshot_dir(snap["domain"], snap["slug"], snap["dir_name"])
    differ.purge_shotdiff_cache([snapshot_id])
    return DeleteResult(url=snap["page_url"], snapshots_deleted=1)


def delete_page(page_id: int) -> DeleteResult | None:
    """페이지 전체(모든 스냅샷·확인 기록·스케줄)를 삭제. 없는 id 면 None."""
    with db.connect() as conn:
        page = db.get_page_by_id(conn, page_id)
        if page is None:
            return None
        snapshot_ids = [s["id"] for s in db.list_snapshots(conn, page_id)]
        db.delete_page(conn, page_id)
    storage.delete_page_dir(page["domain"], page["slug"])
    differ.purge_shotdiff_cache(snapshot_ids)
    return DeleteResult(url=page["url"], snapshots_deleted=len(snapshot_ids))
