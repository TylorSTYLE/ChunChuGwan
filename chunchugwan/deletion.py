"""아카이브 데이터 삭제 — 페이지 전체 또는 단일 스냅샷.

CLI 와 대시보드가 공유하는 유일한 삭제 진입점. 쓰기 규칙(CLAUDE.md 1번)에
따라 DB 는 db.py, 파일은 storage.py 를 통해서만 조작한다.

순서는 DB 확정(커밋) → 파일 삭제: 파일 삭제가 중간에 실패해도 인덱스는
일관되고, 남는 고아 디렉토리는 무해하다 (반대 순서면 UI 가 깨진 참조를 본다).
단일 스냅샷 삭제 시 다음 스냅샷의 changed 재계산은 db.delete_snapshot 이 한다.

문서 CAS GC: 삭제되는 스냅샷이 참조하던 문서 중 더는 어떤 스냅샷도
참조하지 않는 것은 CAS 파일도 함께 삭제한다 (잔존 참조 판정은 같은
트랜잭션 안에서 끝나고, 파일 삭제는 커밋 후에 한다).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from . import db, differ, documents, storage


@dataclass
class DeleteResult:
    url: str                # 삭제 대상 페이지의 정규화 URL
    snapshots_deleted: int  # 함께 삭제된 스냅샷 수


def _doomed_document_refs(
    conn: sqlite3.Connection, snapshot_ids: list[int]
) -> list[sqlite3.Row]:
    """삭제될 스냅샷들의 문서 참조 — 행이 지워지기 전에 모아 둔다."""
    return db.list_snapshot_document_refs(conn, snapshot_ids)


def _orphaned_cas_names(
    conn: sqlite3.Connection, doomed: list[sqlite3.Row]
) -> list[str]:
    """참조 행 삭제 후, 더는 참조되지 않는 문서 CAS 이름 목록.

    CAS 이름은 (sha256, 확장자) 단위라 잔존 비교도 이름 기준으로 한다.
    """
    if not doomed:
        return []
    names = {n for r in doomed if (n := documents.cas_name(r["sha256"], r["file"]))}
    remaining = db.list_document_refs_by_shas(
        conn, list({r["sha256"] for r in doomed})
    )
    alive = {n for r in remaining if (n := documents.cas_name(r["sha256"], r["file"]))}
    return sorted(names - alive)


def delete_snapshot(snapshot_id: int) -> DeleteResult | None:
    """스냅샷 하나를 DB·파일·diff 캐시·고아 문서 CAS 에서 삭제. 없는 id 면 None."""
    with db.connect() as conn:
        snap = db.get_snapshot(conn, snapshot_id)
        if snap is None:
            return None
        doomed = _doomed_document_refs(conn, [snapshot_id])
        db.delete_snapshot(conn, snapshot_id)
        orphans = _orphaned_cas_names(conn, doomed)
    storage.delete_snapshot_dir(snap["domain"], snap["slug"], snap["dir_name"])
    documents.delete_cas(orphans)
    differ.purge_shotdiff_cache([snapshot_id])
    return DeleteResult(url=snap["page_url"], snapshots_deleted=1)


def delete_page(page_id: int) -> DeleteResult | None:
    """페이지 전체(모든 스냅샷·확인 기록·스케줄)를 삭제. 없는 id 면 None."""
    with db.connect() as conn:
        page = db.get_page_by_id(conn, page_id)
        if page is None:
            return None
        snapshot_ids = [s["id"] for s in db.list_snapshots(conn, page_id)]
        doomed = _doomed_document_refs(conn, snapshot_ids)
        db.delete_page(conn, page_id)
        orphans = _orphaned_cas_names(conn, doomed)
    storage.delete_page_dir(page["domain"], page["slug"])
    documents.delete_cas(orphans)
    differ.purge_shotdiff_cache(snapshot_ids)
    return DeleteResult(url=page["url"], snapshots_deleted=len(snapshot_ids))
