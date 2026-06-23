"""로컬↔S3 blob 저장 백엔드 마이그레이션 엔진.

활성 백엔드(원본)의 전 blob(sites·resources·documents)을 반대 백엔드(대상)로
파일 단위 copy 하고, 존재+크기(+CAS sha256)로 검증한다. 파일당 최대 3회 재시도
하며 **0 실패에서만 완료**로 보고 활성 백엔드를 전환한다(전환 전까지는 원본이
계속 활성이라 읽기 서빙이 끊기지 않는다). 원본은 자동 삭제하지 않고 관리자
수동 정리를 위해 "정리 대기" 로 남긴다.

migration.py(춘추관 간 이전)의 인메모리 상태 + 단일 워커 스레드 패턴을 미러하되,
HTTP Pull 이 아니라 두 blob 백엔드 인스턴스 간 copy 다. 진행 중에는 DB 플래그
(storage_migration_in_progress)로 캡처·스케줄·크롤이 멈추고(프로세스 간 공유),
serve 재시작으로 중단돼도 플래그가 남아 재실행으로 이어갈 수 있다.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config, db

logger = logging.getLogger(__name__)

# copy 대상 blob 최상위 디렉토리 (논리 경로 = ARCHIVE_ROOT 기준 상대).
_TOP_DIRS = ("sites", "resources", "documents")

# 파일 1개 copy 재시도 횟수·백오프 (테스트는 monkeypatch 로 단축 가능).
FILE_RETRIES = 3
_RETRY_BACKOFF_SECONDS = (0.2, 0.5, 1.0)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _cas_sha256(rel: Path) -> str | None:
    """CAS 파일명(`{sha256}{확장자}`)에서 sha256 추출 (resources·documents 한정)."""
    if rel.parts and rel.parts[0] in ("resources", "documents"):
        stem = rel.stem
        if len(stem) == 64 and all(c in "0123456789abcdef" for c in stem):
            return stem
    return None


def _make_backend(name: str):
    """이름('local'|'s3')으로 백엔드 인스턴스 생성. s3 는 env 완전해야 한다."""
    if name == "s3":
        from .blobstore import S3BlobStore

        return S3BlobStore(**config.s3_settings())
    from .blobstore import LocalBlobStore

    return LocalBlobStore()


def _source_location(name: str) -> str:
    """원본 위치 표기 (정리 안내용). 비밀값은 포함하지 않는다."""
    if name == "s3":
        prefix = config.S3_PREFIX
        base = f"s3://{config.S3_BUCKET}"
        return f"{base}/{prefix}" if prefix else base
    return str(config.ARCHIVE_ROOT)


def build_manifest(source) -> list[dict]:
    """원본 백엔드의 전 blob 매니페스트 — [{path(상대 POSIX), size, sha256}].

    sha256 은 CAS 파일명에서 무료로 얻을 수 있을 때만 채운다(sites·기타는 None —
    copy 시 원본 내용에서 계산해 종단 체크섬 업로드에 쓴다).
    """
    files: list[dict] = []
    for top in _TOP_DIRS:
        root = config.ARCHIVE_ROOT / top
        if not source.is_dir(root):
            continue
        for path_obj in source.rglob(root, "*"):
            if not source.is_file(path_obj):
                continue
            rel = path_obj.relative_to(config.ARCHIVE_ROOT)
            files.append({
                "path": rel.as_posix(),
                "size": source.size(path_obj),
                "sha256": _cas_sha256(rel),
            })
    return files


def _verified(target, path_obj: Path, entry: dict) -> bool:
    """대상에 파일이 존재하고 크기가 매니페스트와 일치하는지 (다운로드 없음)."""
    try:
        return target.is_file(path_obj) and target.size(path_obj) == entry["size"]
    except Exception:  # noqa: BLE001 — 검증 실패는 미존재로 간주(재시도 유도)
        return False


def _copy_file(source, target, path_obj: Path, entry: dict) -> str | None:
    """원본→대상 파일 1개 copy + 검증. 성공 None, 3회 실패 시 마지막 오류 문자열.

    멱등: 대상에 이미 존재+크기 일치면 다운로드 없이 스킵한다. CAS 파일은
    매니페스트 sha256(=파일명)과 원본 내용 해시가 일치해야 하고(손상 감지),
    업로드는 종단 체크섬(put_verified)으로 부분/손상 파일을 막는다.
    """
    last: str | None = None
    for attempt in range(FILE_RETRIES):
        try:
            if _verified(target, path_obj, entry):
                return None
            data = source.read_bytes(path_obj)
            actual = hashlib.sha256(data).hexdigest()
            # CAS 이름 해시는 '저장된 바이트'의 sha256 과 같다(이미지·문서 등) —
            # 단 .css 는 gzip 으로 저장하고 이름은 '압축 전' 원본의 sha256 이라
            # 저장 바이트와 다르다(resources._store_css). .css 는 이름 대조를
            # 건너뛰고, 전송 무결성은 put_verified 의 종단 체크섬(actual)으로 확보한다.
            if (entry["sha256"] and not entry["path"].endswith(".css")
                    and actual != entry["sha256"]):
                raise ValueError("원본 sha256 불일치 (CAS 손상)")
            target.put_verified(path_obj, data, actual)
            if not _verified(target, path_obj, entry):
                raise ValueError("대상 검증 실패 (존재/크기 불일치)")
            return None
        except Exception as e:  # noqa: BLE001 — 네트워크/IO/검증 모두 재시도
            last = f"{type(e).__name__}: {e}"
            if attempt < FILE_RETRIES - 1:
                time.sleep(_RETRY_BACKOFF_SECONDS[attempt])
    return last


# ---- 진행 상태 (인메모리 — serve 단일 프로세스, reindex/migration 패턴) ----
_lock = threading.Lock()
_state: dict = {"status": "idle"}
_thread: threading.Thread | None = None


def status() -> dict:
    """현재 마이그레이션 상태(폴링/CLI용) — 인메모리 진행률 + DB 요약·활성 백엔드."""
    with _lock:
        live = dict(_state)
    with db.connect() as conn:
        live["active_backend"] = db.storage_backend(conn)
        live["paused"] = db.writes_paused(conn)
        live["summary"] = db.storage_migration_summary(conn)
    return live


def _set_state(**kwargs) -> None:
    with _lock:
        _state.update(kwargs)


def _bump_done() -> None:
    with _lock:
        _state["done"] = _state.get("done", 0) + 1


def _bump_failed() -> None:
    with _lock:
        _state["failed_count"] = _state.get("failed_count", 0) + 1


def _running() -> bool:
    return _thread is not None and _thread.is_alive()


def _resolve_direction(conn) -> tuple[str, str]:
    """현재 활성 백엔드(원본) → 반대 백엔드(대상)."""
    source = db.storage_backend(conn)
    target = "s3" if source == "local" else "local"
    return source, target


def start_migration() -> str | None:
    """마이그레이션 백그라운드 작업 시작 (활성→반대). 오류 시 메시지 반환."""
    global _thread
    with _lock:
        if _running():
            return "이미 마이그레이션이 진행 중입니다."
        with db.connect() as conn:
            if db.migration_mode_enabled(conn):
                return "인스턴스 이전 모드 중에는 스토리지 마이그레이션을 시작할 수 없습니다."
            source, target = _resolve_direction(conn)
        if target == "s3":
            try:
                config.s3_settings()  # env 완전성 — 불완전이면 명확한 메시지로 막는다
            except RuntimeError as e:
                return str(e)
        # 쓰기 일시중지 플래그(DB) — 프로세스 간 공유, serve 재시작에도 유지
        with db.connect() as conn:
            db.set_storage_migration_active(conn, True)
        _state.clear()
        _state.update({
            "status": "manifest", "direction": f"{source}_to_{target}",
            "source_backend": source, "target_backend": target,
            "total": 0, "done": 0, "failed": [], "failed_count": 0, "error": None,
            "started_at": _utcnow(), "finished_at": None, "cleanup_pending": False,
        })
        _thread = threading.Thread(
            target=_migrate_worker, args=(source, target),
            name="wccg-storage-migration", daemon=True,
        )
        _thread.start()
    return None


def retry_failed() -> str | None:
    """실패 목록의 파일만 다시 copy 한다 (partial 상태에서만)."""
    global _thread
    with _lock:
        if _running():
            return "이미 마이그레이션이 진행 중입니다."
        if _state.get("status") != "partial":
            return "재시도할 수 있는 상태가 아닙니다."
        retry_paths = [f["path"] for f in _state.get("failed", [])]
        if not retry_paths:
            return "재시도할 파일이 없습니다."
        source = _state.get("source_backend")
        target = _state.get("target_backend")
        _state.update({"status": "copying", "failed": [], "failed_count": 0,
                       "done": 0, "total": len(retry_paths)})
        _thread = threading.Thread(
            target=_migrate_worker, args=(source, target),
            kwargs={"only_paths": retry_paths},
            name="wccg-storage-migration", daemon=True,
        )
        _thread.start()
    return None


def confirm_cleanup() -> str | None:
    """원본 정리 완료를 확인해 "정리 대기" 플래그를 해제한다 (데이터는 안 지운다)."""
    with db.connect() as conn:
        summary = db.storage_migration_summary(conn)
        if not summary or not summary.get("cleanup_pending"):
            return "정리 대기 중인 마이그레이션이 없습니다."
        summary["cleanup_pending"] = False
        summary["status"] = "cleaned"
        db.set_storage_migration_summary(conn, summary)
    with _lock:
        _state["cleanup_pending"] = False
    return None


def _migrate_worker(source_name: str, target_name: str, *,
                    only_paths: list[str] | None = None) -> None:
    """copy 워커 — 매니페스트 → 파일 copy → 0실패면 완료/전환, 실패 남으면 partial."""
    try:
        source = _make_backend(source_name)
        target = _make_backend(target_name)
        _set_state(status="manifest")
        manifest = build_manifest(source)
        if only_paths is not None:
            wanted = set(only_paths)
            manifest = [e for e in manifest if e["path"] in wanted]
        workers = max(1, min(config.S3_MIGRATION_WORKERS, 16))
        _set_state(status="copying", total=len(manifest), done=0, failed=[],
                   failed_count=0, workers=workers)
        logger.info("스토리지 마이그레이션 시작: %s→%s, 파일 %d개, 동시 전송 %d",
                    source_name, target_name, len(manifest), workers)

        # 파일 단위 copy 를 동시 전송한다 — 네트워크 I/O 바운드라 병렬도가 곧
        # 처리량이다. 진행/실패 카운트는 락으로 안전하게 누적해 라이브로 보인다.
        # (boto3 저수준 클라이언트는 스레드 간 공유가 안전하다.)
        failed: list[dict] = []

        def _do(entry: dict) -> tuple[dict, str | None]:
            path_obj = config.ARCHIVE_ROOT / entry["path"]
            return entry, _copy_file(source, target, path_obj, entry)

        def _handle(entry: dict, err: str | None) -> None:
            if err is not None:
                failed.append({"path": entry["path"], "error": err})
                _bump_failed()
                logger.warning("스토리지 마이그레이션 파일 실패: %s — %s",
                               entry["path"], err)
            _bump_done()

        if workers <= 1 or len(manifest) <= 1:
            for entry in manifest:
                _handle(*_do(entry))
        else:
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=workers) as ex:
                for entry, err in ex.map(_do, manifest):
                    _handle(entry, err)

        if failed:
            logger.warning(
                "스토리지 마이그레이션 미완료(partial): 전체 %d개 중 %d개 실패 — "
                "재시도로 0실패까지 해소해야 전환됩니다.", len(manifest), len(failed))
            # 실패가 남으면 미완료 — 전환·일시중지 해제 금지 (재시도로 0실패까지)
            _set_state(status="partial", failed=failed)
            _persist_summary()
            return
    except Exception as e:  # noqa: BLE001
        logger.exception("스토리지 마이그레이션 실패")
        _set_state(status="error", error=f"{type(e).__name__}: {e}")
        _persist_summary()
        return

    _complete(source_name, target_name)


def _complete(source_name: str, target_name: str) -> None:
    """0실패 완료 — 활성 백엔드 전환 + 일시중지 해제 + 정리 대기 기록."""
    source_location = _source_location(source_name)
    with _lock:
        total = _state.get("total", 0)
    with db.connect() as conn:
        db.set_storage_backend(conn, target_name)
        db.set_storage_migration_active(conn, False)
        db.set_storage_migration_summary(conn, {
            "status": "completed",
            "direction": f"{source_name}_to_{target_name}",
            "source_backend": source_name, "target_backend": target_name,
            "source_location": source_location,
            "cleanup_pending": True,
            "total": total, "finished_at": _utcnow(),
        })
    config.reset_blob_store()  # 새 활성 백엔드로 재생성
    logger.info("스토리지 마이그레이션 완료: %s→%s, %d개 전송, 활성 백엔드 → %s "
                "(원본 정리 대기: %s)", source_name, target_name, total,
                target_name, source_location)
    _set_state(status="done", finished_at=_utcnow(),
               cleanup_pending=True, source_location=source_location)


def _persist_summary() -> None:
    """partial/error 상태를 DB 요약에 기록 (세션 간/CLI 표시용)."""
    with _lock:
        snap = dict(_state)
    with db.connect() as conn:
        db.set_storage_migration_summary(conn, {
            "status": snap.get("status"),
            "direction": snap.get("direction"),
            "source_backend": snap.get("source_backend"),
            "target_backend": snap.get("target_backend"),
            "total": snap.get("total"), "done": snap.get("done"),
            "failed_count": len(snap.get("failed", [])),
            "error": snap.get("error"),
            "cleanup_pending": False,
            "finished_at": _utcnow(),
        })
