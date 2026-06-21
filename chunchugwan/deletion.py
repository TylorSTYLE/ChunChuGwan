"""아카이브 데이터 삭제 — 페이지·사이트(휴지통 경유) 또는 단일 스냅샷.

CLI 와 대시보드가 공유하는 유일한 삭제 진입점. 쓰기 규칙(CLAUDE.md 1번)에
따라 DB 는 db.py, 파일은 storage.py 를 통해서만 조작한다.

휴지통(소프트 삭제): 시스템 설정 `trash_enabled`(기본 on)이 켜져 있으면 페이지·
사이트 삭제는 즉시 지우지 않고 trash_entries 항목으로 남기고 연결 행의 trash_id 를
세팅해 숨긴다(파일·CAS 는 그대로). 보관 기간(`trash_retention_days`)이 지나면
스케줄러가 purge_expired 로 영구 삭제하고, 사용자가 휴지통에서 복원(restore)하거나
즉시 영구삭제(purge)할 수 있다. `trash_enabled` off 또는 hard=True 면 종전처럼 즉시
영구 삭제한다. 단일 스냅샷 삭제(delete_snapshot)는 휴지통을 거치지 않는다(범위 밖).

영구 삭제 순서는 DB 확정(커밋) → 파일 삭제: 파일 삭제가 중간에 실패해도 인덱스는
일관되고, 남는 고아 디렉토리는 무해하다 (반대 순서면 UI 가 깨진 참조를 본다).
단일 스냅샷 삭제 시 다음 스냅샷의 changed 재계산은 db.delete_snapshot 이 한다.

문서·자원 CAS GC: 삭제되는 스냅샷이 참조하던 문서/공유 자원 중 더는
어떤 스냅샷도 참조하지 않는 것은 CAS 파일도 함께 삭제한다 (잔존 참조
판정은 같은 트랜잭션 안에서 끝나고, 파일 삭제는 커밋 후에 한다). 휴지통에
머무는 동안에는 참조 행(snapshot_documents/_resources)이 남아 CAS 가 보존되고,
영구 삭제 때 비로소 GC 된다.
자원 참조가 기록되지 않은 구형 스냅샷의 자원은 여기서 지워지지 않는다 —
저장공간 최적화(compact)의 백필 + 고아 정리가 맡는다.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

from . import db, differ, documents, resources, storage

logger = logging.getLogger(__name__)


@dataclass
class DeleteResult:
    url: str                # 삭제 대상 페이지의 정규화 URL
    snapshots_deleted: int  # 함께 삭제(또는 휴지통 이동)된 스냅샷 수
    trashed: bool = False   # True=휴지통으로 이동, False=즉시 영구 삭제


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


def _orphaned_resource_names(
    conn: sqlite3.Connection, doomed: list[str]
) -> list[str]:
    """참조 행 삭제 후, 더는 참조되지 않는 자원 CAS 이름 목록."""
    if not doomed:
        return []
    alive = set(db.list_resource_refs_by_names(conn, doomed))
    return sorted(set(doomed) - alive)


def delete_snapshot(snapshot_id: int) -> DeleteResult | None:
    """스냅샷 하나를 DB·파일·diff 캐시·고아 문서/자원 CAS 에서 삭제. 없는 id 면 None.

    단일 스냅샷 삭제는 휴지통을 거치지 않고 즉시 영구 삭제다(범위: 페이지·사이트만 휴지통).
    """
    with db.connect() as conn:
        snap = db.get_snapshot(conn, snapshot_id, include_trashed=True)
        if snap is None:
            return None
        doomed = _doomed_document_refs(conn, [snapshot_id])
        doomed_res = db.list_snapshot_resource_refs(conn, [snapshot_id])
        db.delete_snapshot(conn, snapshot_id)
        orphans = _orphaned_cas_names(conn, doomed)
        res_orphans = _orphaned_resource_names(conn, doomed_res)
    storage.delete_snapshot_dir(snap["domain"], snap["slug"], snap["dir_name"])
    documents.delete_cas(orphans)
    resources.delete_cas(res_orphans)
    differ.purge_shotdiff_cache([snapshot_id])
    return DeleteResult(url=snap["page_url"], snapshots_deleted=1)


@dataclass
class DeleteSiteResult:
    site_key: str           # 삭제된 사이트 키
    pages_deleted: int      # 함께 삭제(또는 휴지통 이동)된 페이지 수
    snapshots_deleted: int  # 함께 삭제(또는 휴지통 이동)된 스냅샷 수
    crawls_deleted: int     # 함께 삭제(또는 휴지통 이동)된 크롤 회차 수
    trashed: bool = False   # True=휴지통으로 이동, False=즉시 영구 삭제


def _hard_delete_page(
    conn: sqlite3.Connection, page: sqlite3.Row
) -> tuple[list[int], list[str], list[str]]:
    """페이지 1개를 DB 에서 하드 삭제하고 (삭제 스냅샷 id, 고아 문서 CAS, 고아 자원 CAS)
    를 돌려준다. 파일 삭제는 호출자가 커밋 후에 한다."""
    snapshot_ids = [s["id"] for s in db.list_snapshots(conn, page["id"])]
    doomed = _doomed_document_refs(conn, snapshot_ids)
    doomed_res = db.list_snapshot_resource_refs(conn, snapshot_ids)
    db.delete_page(conn, page["id"])
    orphans = _orphaned_cas_names(conn, doomed)
    res_orphans = _orphaned_resource_names(conn, doomed_res)
    return snapshot_ids, orphans, res_orphans


def delete_page(
    page_id: int, *, hard: bool = False, deleted_by: int | None = None
) -> DeleteResult | None:
    """페이지 전체(모든 스냅샷·확인 기록·스케줄)를 삭제. 없는 id 면 None.

    기본은 휴지통으로 이동(소프트). `trash_enabled` off 거나 hard=True 면 즉시 영구
    삭제한다. deleted_by 는 휴지통 항목의 삭제자(표시용, CLI/시스템은 None).
    """
    with db.connect() as conn:
        page = db.get_page_by_id(conn, page_id, include_trashed=True)
        if page is None:
            return None
        already_trashed = page["trash_id"] is not None
        if not hard:
            if already_trashed:
                # 이미 휴지통에 있음 — 멱등 no-op (중복 삭제 방지)
                snaps = db.list_snapshots(conn, page_id)
                return DeleteResult(
                    url=page["url"], snapshots_deleted=len(snaps), trashed=True
                )
            if db.trash_enabled(conn):
                snaps = db.list_snapshots(conn, page_id)
                trash_id = db.create_trash_entry(
                    conn, kind="page", label=page["url"],
                    site_id=page["site_id"], page_id=page_id,
                    page_count=1, snapshot_count=len(snaps),
                    bytes_total=sum(s["bytes"] for s in snaps),
                    deleted_by=deleted_by,
                )
                db.mark_page_trashed(conn, page_id, trash_id)
                return DeleteResult(
                    url=page["url"], snapshots_deleted=len(snaps), trashed=True
                )
        # 즉시 영구 삭제
        snapshot_ids, orphans, res_orphans = _hard_delete_page(conn, page)
    storage.delete_page_dir(page["domain"], page["slug"])
    documents.delete_cas(orphans)
    resources.delete_cas(res_orphans)
    differ.purge_shotdiff_cache(snapshot_ids)
    return DeleteResult(
        url=page["url"], snapshots_deleted=len(snapshot_ids), trashed=False
    )


def delete_site(
    site_id: int, *, hard: bool = False, deleted_by: int | None = None
) -> DeleteSiteResult | None:
    """사이트와 소속 데이터 전체를 삭제. 없는 id 면 None.

    기본은 휴지통으로 이동(소프트) — 소속 활성 페이지·크롤 회차·크롤 스케줄에
    trash_id 를 세팅해 숨긴다(파일은 그대로). `trash_enabled` off 거나 hard=True 면
    종전처럼 즉시 영구 삭제한다(소속 페이지·크롤·크롤 스케줄을 한 트랜잭션에서 지우고
    파일은 커밋 후 삭제, 사이트 행은 prune_site_if_empty 가 정리).
    """
    with db.connect() as conn:
        site = db.get_site(conn, site_id)
        if site is None:
            return None
        pages = db.list_site_pages(conn, site_id)  # 활성(비휴지통) 페이지만
        snapshot_ids: list[int] = []
        for page in pages:
            snapshot_ids += [s["id"] for s in db.list_snapshots(conn, page["id"])]
        if not hard and db.trash_enabled(conn):
            bytes_total = 0
            for page in pages:
                bytes_total += sum(
                    s["bytes"] for s in db.list_snapshots(conn, page["id"])
                )
            crawls = db.list_site_crawls(conn, site_id)  # 활성 크롤만
            trash_id = db.create_trash_entry(
                conn, kind="site", label=site["site_key"],
                site_id=site_id, page_id=None,
                page_count=len(pages), snapshot_count=len(snapshot_ids),
                bytes_total=bytes_total, deleted_by=deleted_by,
            )
            db.mark_site_trashed(conn, site_id, trash_id)
            return DeleteSiteResult(
                site_key=site["site_key"], pages_deleted=len(pages),
                snapshots_deleted=len(snapshot_ids), crawls_deleted=len(crawls),
                trashed=True,
            )
        # 즉시 영구 삭제
        doomed = _doomed_document_refs(conn, snapshot_ids)
        doomed_res = db.list_snapshot_resource_refs(conn, snapshot_ids)
        crawls_deleted = db.delete_site_crawls(conn, site_id)
        db.delete_site_crawl_schedules(conn, site_id)
        for page in pages:
            db.delete_page(conn, page["id"])
        db.prune_site_if_empty(conn, site_id)
        orphans = _orphaned_cas_names(conn, doomed)
        res_orphans = _orphaned_resource_names(conn, doomed_res)
    for page in pages:
        storage.delete_page_dir(page["domain"], page["slug"])
    documents.delete_cas(orphans)
    resources.delete_cas(res_orphans)
    differ.purge_shotdiff_cache(snapshot_ids)
    return DeleteSiteResult(
        site_key=site["site_key"],
        pages_deleted=len(pages),
        snapshots_deleted=len(snapshot_ids),
        crawls_deleted=crawls_deleted,
        trashed=False,
    )


# ---- 휴지통 복원·영구삭제 ----


def restore(trash_id: int) -> sqlite3.Row | None:
    """휴지통 항목을 복원 — 연결된 행의 trash_id 를 해제하고 항목을 제거. 없으면 None.

    파일·CAS 는 휴지통에 머무는 동안 손대지 않았으므로 복원은 DB 만 되돌리면 된다.
    반환은 복원된 항목 행(라우트·CLI 의 결과 표시용).
    """
    with db.connect() as conn:
        entry = db.get_trash_entry(conn, trash_id)
        if entry is None:
            return None
        db.clear_trash_entry(conn, trash_id)
    return entry


def purge(trash_id: int) -> sqlite3.Row | None:
    """휴지통 항목을 영구 삭제 — 기존 하드 삭제 기구로 행·파일·CAS·diff 캐시를 지운다.

    없는 id 면 None. site-kind 는 크롤 회차·크롤 스케줄을 먼저 지운 뒤 페이지를
    하드 삭제하므로 마지막 페이지 삭제 시 prune_site_if_empty 가 사이트 행을 정리한다.
    순서는 DB 확정 → 파일 삭제(모듈 불변식). 반환은 삭제된 항목 행.
    """
    with db.connect() as conn:
        entry = db.get_trash_entry(conn, trash_id)
        if entry is None:
            return None
        page_ids = db.list_trash_entry_page_ids(conn, trash_id)
        snapshot_ids: list[int] = []
        page_dirs: list[tuple[str, str]] = []
        for pid in page_ids:
            page = db.get_page_by_id(conn, pid, include_trashed=True)
            if page is None:
                continue
            page_dirs.append((page["domain"], page["slug"]))
            snapshot_ids += [s["id"] for s in db.list_snapshots(conn, pid)]
        doomed = _doomed_document_refs(conn, snapshot_ids)
        doomed_res = db.list_snapshot_resource_refs(conn, snapshot_ids)
        # 크롤 회차·스케줄(site-kind)을 먼저 지운다 — 그래야 마지막 페이지 삭제 시
        # prune_site_if_empty 가 사이트를 비었다고 보고 정리한다.
        db.delete_trash_entry_crawls(conn, trash_id)
        db.delete_trash_entry_crawl_schedules(conn, trash_id)
        for pid in page_ids:
            db.delete_page(conn, pid)
        # 연결 행이 모두 사라졌으니 항목 행 삭제 (FK 위반 없음)
        db.delete_trash_entry(conn, trash_id)
        orphans = _orphaned_cas_names(conn, doomed)
        res_orphans = _orphaned_resource_names(conn, doomed_res)
    for domain, slug in page_dirs:
        storage.delete_page_dir(domain, slug)
    documents.delete_cas(orphans)
    resources.delete_cas(res_orphans)
    differ.purge_shotdiff_cache(snapshot_ids)
    return entry


def purge_expired() -> int:
    """보관 기간이 지난 휴지통 항목을 영구 삭제. 삭제한 항목 수 반환.

    `trash_retention_days`==0 이면 자동 삭제 비활성(0 반환). 토글(trash_enabled)과
    무관하게 동작한다 — 기능을 꺼도 남은 항목은 기간 경과 시 정리된다.
    스케줄러(run_due)·`wccg schedule run` 이 주기적으로 호출한다.
    """
    with db.connect() as conn:
        days = db.trash_retention_days(conn)
        if days <= 0:
            return 0
        cutoff = db.trash_purge_cutoff(days)
        expired = db.list_expired_trash_entries(conn, cutoff)
    count = 0
    for entry in expired:
        if purge(entry["id"]) is not None:
            count += 1
    if count:
        logger.info("휴지통 자동 영구삭제: %d개 항목", count)
    return count
