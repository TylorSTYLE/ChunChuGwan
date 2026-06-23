"""첫 구동 상태 분류 + 복구모드 인덱스 재구축.

분류(classify)는 비파괴 — 사용자 수·아카이브 데이터 유무·blob 존재(로컬/S3)·
S3 DB백업 가용성을 보고 setup 이 제시할 옵션을 정한다(스캔·마이그레이션·초기화
같은 행동은 절대 하지 않는다).

복구모드(start_recovery)는 활성/설정된 백엔드의 sites/*/*/*/ 를 walk 해
pages·snapshots·snapshot_resources·snapshot_documents 를 meta.json·디렉토리
구조에서 재구성한다(백그라운드 스레드 + 진행상태). **보안: meta.json 에 없는
authenticated 플래그는 재구축이 불가능하므로, 복구되는 모든 스냅샷에
authenticated=1(관리자 전용)을 명시적으로 설정한다** — 컬럼 DEFAULT(0=공개)에
기대면 원래 비공개였던 스냅샷이 전체 공개로 노출되는 사고가 난다. 공개 전환은
관리자가 명시적으로 선택할 때만(복구-선택 API).
"""

from __future__ import annotations

import gzip
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import config, db, resources, storage

logger = logging.getLogger(__name__)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---- 백엔드 선택 (blob 이 있는 곳) ----


def _build_backend(name: str):
    if name == "s3":
        from .blobstore import S3BlobStore

        return S3BlobStore(**config.s3_settings())
    from .blobstore import LocalBlobStore

    return LocalBlobStore()


def _has_snapshots(backend) -> bool:
    """백엔드의 sites/ 에 meta.json 을 가진 스냅샷 디렉토리가 하나라도 있는지.

    존재 확인만 (S3 는 list/HEAD — 객체 다운로드 없음). 오류는 '없음' 으로 본다.
    """
    try:
        if not backend.is_dir(config.SITES_DIR):
            return False
        for p in backend.glob(config.SITES_DIR, "*/*/*"):
            if backend.is_file(p / "meta.json"):
                return True
        return False
    except Exception:  # noqa: BLE001 — 접근 실패는 보수적으로 '없음'
        return False


def _snapshot_dirs(backend) -> list[Path]:
    """백엔드의 확정 스냅샷 디렉토리 목록 (meta.json 보유), 경로 사전순."""
    if not backend.is_dir(config.SITES_DIR):
        return []
    return sorted(
        p for p in backend.glob(config.SITES_DIR, "*/*/*")
        if backend.is_file(p / "meta.json")
    )


def _s3_env_ok() -> bool:
    """S3 필수 env 가 완전한지 (비밀값 노출 없이)."""
    try:
        config.s3_settings()
        return True
    except Exception:  # noqa: BLE001
        return False


def _source_backend():
    """복구할 blob 이 있는 백엔드와 이름 — 로컬 우선, 없으면 S3. 없으면 (None, None)."""
    from .blobstore import LocalBlobStore

    if _has_snapshots(LocalBlobStore()):
        return LocalBlobStore(), "local"
    if config.s3_requested() and _s3_env_ok():
        try:
            backend = _build_backend("s3")
        except Exception:  # noqa: BLE001
            return None, None
        if _has_snapshots(backend):
            return backend, "s3"
    return None, None


# ---- 첫 구동 분류 (비파괴) ----


def classify() -> dict:
    """부팅/setup 시점 상황 분류 — 행동 없이 분류만. setup 상태 API 가 반환한다.

    6 케이스: operating(사용자>0) / data_preserved(사용자0+데이터) /
    restore_s3(빈DB+S3blob+db백업) / recover_local / recover_s3 / fresh(blob없음).
    """
    with db.connect() as conn:
        users = db.count_users(conn)
        snaps = db.count_snapshots_raw(conn)
    if users > 0:
        return {"case": "operating"}  # 1-a: 그대로 운영 — 스캔/행동 없음
    if snaps > 0:
        return {"case": "data_preserved", "has_archive_data": True}

    from .blobstore import LocalBlobStore

    local_blob = _has_snapshots(LocalBlobStore())
    s3_configured = config.s3_requested() and _s3_env_ok()
    s3_blob = False
    s3_backup = False
    if s3_configured:
        try:
            s3_blob = _has_snapshots(_build_backend("s3"))
        except Exception:  # noqa: BLE001
            s3_blob = False
        if s3_blob:
            from . import db_backup

            s3_backup = db_backup.has_restorable_backup()

    if s3_configured and s3_backup:
        case = "restore_s3"
    elif local_blob:
        case = "recover_local"
    elif s3_blob:
        case = "recover_s3"
    else:
        case = "fresh"
    return {
        "case": case,
        "local_blob": local_blob,
        "s3_configured": s3_configured,
        "s3_blob": s3_blob,
        "s3_db_backup": s3_backup,
    }


# ---- 복구모드 재구축 (백그라운드) ----

_lock = threading.Lock()
_state: dict = {"status": "idle"}
_thread: threading.Thread | None = None


def status() -> dict:
    """복구 진행 상태 (폴링용) — 인메모리 진행률 + DB 복구 메타(세션 간)."""
    with _lock:
        live = dict(_state)
    with db.connect() as conn:
        live["recovery_meta"] = db.recovery_meta(conn)
    return live


def _set_state(**kwargs) -> None:
    with _lock:
        _state.update(kwargs)


def _bump_done() -> None:
    with _lock:
        _state["done"] = _state.get("done", 0) + 1


def _running() -> bool:
    return _thread is not None and _thread.is_alive()


def start_recovery() -> str | None:
    """복구모드 백그라운드 작업 시작 (멱등 재실행 안전). 오류 시 메시지 반환."""
    global _thread
    with _lock:
        if _running():
            return "이미 복구가 진행 중입니다."
        with db.connect() as conn:
            if db.count_users(conn) > 0:
                return "이미 사용자가 있어 복구를 시작할 수 없습니다."
            meta = db.recovery_meta(conn)
            # 이어가기: 진행/오류 상태의 메타가 있으면 baseline 을 재사용한다
            if meta and meta.get("status") in ("running", "error"):
                baseline = meta.get("baseline_max_id", 0)
            else:
                baseline = db.max_snapshot_id(conn)
        source, name = _source_backend()
        if source is None:
            return "복구할 blob 을 찾을 수 없습니다 (로컬·S3 모두 비어 있음)."
        with db.connect() as conn:
            # 복구 중 캡처·스케줄·크롤 일시중지 — 기존 일반화된 게이트 재사용
            db.set_migration_mode(conn, True)
            db.set_recovery_meta(conn, {
                "baseline_max_id": baseline, "status": "running",
                "source_backend": name, "started_at": _utcnow(),
            })
        _state.clear()
        _state.update({
            "status": "scanning", "source_backend": name,
            "baseline_max_id": baseline, "done": 0, "total": 0,
            "error": None, "started_at": _utcnow(), "finished_at": None,
        })
        _thread = threading.Thread(
            target=_recover_worker, args=(name, baseline),
            name="wccg-recovery", daemon=True,
        )
        _thread.start()
    return None


def _snapshot_bytes(backend, snap_dir: Path) -> int:
    """스냅샷 디렉토리의 파일 용량 합 (백엔드 경유, 비정규화 bytes 재계산)."""
    total = 0
    for name in storage.SNAPSHOT_FILES:
        p = snap_dir / name
        if backend.is_file(p):
            total += backend.size(p)
    files_dir = snap_dir / "files"
    if backend.is_dir(files_dir):
        for f in backend.iterdir(files_dir):
            if backend.is_file(f):
                total += backend.size(f)
    return total


def _resource_refs(backend, snap_dir: Path) -> list[str]:
    """page.html(.gz) 의 /resource/ 참조 이름 목록 (snapshot_resources 재구축)."""
    for name, gz in (("page.html.gz", True), ("page.html", False)):
        p = snap_dir / name
        if backend.is_file(p):
            raw = backend.read_bytes(p)
            html = gzip.decompress(raw) if gz else raw
            return resources.referenced_names_in_html(html.decode("utf-8", "replace"))
    return []


def _rebuild_one(backend, snap_dir: Path) -> str:
    """스냅샷 1개를 인덱스에 재구성 (멱등). 'ok' | 'skip'.

    meta.json·디렉토리 구조에서 page·snapshot 을 만들고, 자원/문서 참조를 잇는다.
    복구분은 authenticated=1(관리자 전용)로 명시 INSERT 한다(보안 — DEFAULT 금지).
    """
    meta = json.loads(backend.read_text(snap_dir / "meta.json"))
    url = meta.get("url")
    if not url:
        return "skip"  # url 없으면 page 를 만들 수 없다 — 안전하게 건너뜀
    domain = snap_dir.parent.parent.name
    slug = snap_dir.parent.name
    dir_name = snap_dir.name
    final_url = meta.get("final_url") or url
    content_hash = meta.get("content_hash") or ""
    with db.connect() as conn:
        page_id = db.get_or_create_page(conn, url, domain, slug)
        if db.find_snapshot_by_dir(conn, page_id, dir_name) is not None:
            return "skip"  # 이미 재구축됨 (멱등)
        prev = db.last_snapshot(conn, page_id)
        # changed: 페이지의 직전 스냅샷과 content_hash 비교로 재유도
        changed = 1 if (prev is None or prev["content_hash"] != content_hash) else 0
        sid = db.insert_snapshot(
            conn, page_id,
            taken_at=meta.get("taken_at"), dir_name=dir_name,
            content_hash=content_hash, final_url=final_url,
            http_status=meta.get("http_status"), title=meta.get("title"),
            bytes=_snapshot_bytes(backend, snap_dir), changed=changed,
            authenticated=1, authenticated_by=None,  # 보안: 복구분 전수 제한
            origin=meta.get("origin") or "server",
            incomplete=1 if meta.get("incomplete") else 0,
            resources_indexed=0, css_externalized=0, search_indexed=0,
        )
        refs = _resource_refs(backend, snap_dir)
        if refs:
            db.insert_snapshot_resources(
                conn, sid, [{"name": n, "url": None} for n in refs])
        docs = meta.get("documents") or []
        if docs:
            db.insert_snapshot_documents(conn, sid, docs)
    return "ok"


def _recover_worker(name: str, baseline: int) -> None:
    """복구 워커 — 스냅샷 디렉토리를 walk 해 재구성하고 완료 시 활성 백엔드 설정."""
    try:
        backend = _build_backend(name)
        _set_state(status="scanning")
        dirs = _snapshot_dirs(backend)
        _set_state(status="rebuilding", total=len(dirs), done=0)
        for snap_dir in dirs:
            try:
                _rebuild_one(backend, snap_dir)
            except Exception as e:  # noqa: BLE001 — 한 스냅샷 실패가 전체를 막지 않음
                logger.warning("복구 실패(스냅샷 건너뜀): %s — %s",
                               snap_dir.name, type(e).__name__)
            _bump_done()
        with db.connect() as conn:
            db.set_storage_backend(conn, name)  # blob 위치에 맞게 활성 전환
            last_id = db.max_snapshot_id(conn)
            db.set_recovery_meta(conn, {
                "baseline_max_id": baseline, "last_id": last_id,
                "status": "done", "source_backend": name,
                "recovered": last_id - baseline, "finished_at": _utcnow(),
            })
            db.set_migration_mode(conn, False)  # 일시중지 해제
        config.reset_blob_store()
        _set_state(status="done", last_id=last_id, finished_at=_utcnow())
    except Exception as e:  # noqa: BLE001
        logger.exception("복구 실패")
        with db.connect() as conn:
            meta = db.recovery_meta(conn) or {}
            meta.update({"status": "error", "error": type(e).__name__,
                         "finished_at": _utcnow()})
            db.set_recovery_meta(conn, meta)
            db.set_migration_mode(conn, False)  # 스레드 종료 → 일시중지 해제
        _set_state(status="error", error=f"{type(e).__name__}: {e}")
