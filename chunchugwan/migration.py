"""춘추관 간 네트워크 데이터 이전(마이그레이션).

소스(보내는 쪽)는 시스템 설정에서 이전 모드를 켜고 인증 토큰을 발급한다.
받는 쪽(목적지)은 최초 설정 화면에서 소스 URL + 토큰을 입력해, 소스의
`/api/migration/*` 에서 전체 데이터를 **파일 단위로 Pull** 한다.

설계 요점:
- 단일 tar.gz 가 아니라 매니페스트 기반 파일 단위 전송 — 수 GB 전송의 내결함성.
  파일 1개 실패 시 최대 3회 재시도, 그래도 실패하면 실패 목록에 넣고 계속.
  전송 후 실패 목록을 노출하고 [전체 재시도] / [무시하고 종료] 를 선택하게 한다.
- DB(index.db)·rules.json 은 단일 파일이라 반드시 받아야 한다 (부분 허용 안 함).
  빠질 수 있는 건 sites/resources/documents 의 스냅샷 파일뿐 (graceful 404).
- 이전 모드 동안 소스의 스크래핑·스케줄·크롤은 코어에서 중단된다 (db.migration_mode_enabled).
- 토큰은 SHA-256 해시만 저장한다 (원칙 6 단방향, db.set_migration_mode).
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import shutil
import threading
import time
from pathlib import Path

import httpx

from . import auth, backup, config, db

logger = logging.getLogger(__name__)

# 전송 대상 최상위 디렉토리 (CAS·스냅샷). rules.json 은 단일 파일로 따로 다룬다.
_TOP_DIRS = ("sites", "resources", "documents")
_SINGLE_FILES = ("rules.json",)

# 파일 1개 전송 재시도 횟수·백오프
FILE_RETRIES = 3
_RETRY_BACKOFF_SECONDS = (1, 3, 5)
# HTTP 타임아웃 (연결/읽기) — 스트리밍 다운로드는 읽기 타임아웃이 청크 간 간격에 적용
_HTTP_TIMEOUT = httpx.Timeout(30.0, read=120.0)
_TOKEN_HEADER = "X-Migration-Token"


# ============================================================
# 소스(보내는 쪽) — 매니페스트·파일 서빙
# ============================================================


def source_staging_dir() -> Path:
    """소스가 일관 DB 스냅샷·매니페스트를 두는 임시 디렉토리 (캐시 안)."""
    return config.CACHE_DIR / "migration"


def _sha256_file(path: Path) -> str:
    """파일의 SHA-256 hex (스트리밍)."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _cas_sha256(rel: Path) -> str | None:
    """CAS 파일명(`{sha256}{확장자}`)에서 sha256 을 추출 (CAS 디렉토리 한정)."""
    if rel.parts and rel.parts[0] in ("resources", "documents"):
        stem = rel.stem  # 확장자 제거
        if len(stem) == 64 and all(c in "0123456789abcdef" for c in stem):
            return stem
    return None


def _iter_transfer_files() -> list[dict]:
    """전송 대상 파일 목록 — 상대경로·바이트·(가능하면) sha256.

    바이트는 os.stat 로 싸게 얻고, sha256 은 CAS 파일명에서 무료로 얻을 수 있을
    때만 채운다 (sites/·rules.json 은 None — 받는 쪽이 바이트 수로 검증).
    """
    files: list[dict] = []
    roots = [(d, config.ARCHIVE_ROOT / d) for d in _TOP_DIRS]
    for name, root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(config.ARCHIVE_ROOT)
            files.append({
                "path": rel.as_posix(),
                "bytes": path.stat().st_size,
                "sha256": _cas_sha256(rel),
            })
    for name in _SINGLE_FILES:
        path = config.ARCHIVE_ROOT / name
        if path.is_file():
            files.append({
                "path": name, "bytes": path.stat().st_size, "sha256": None,
            })
    return files


def db_snapshot_path() -> Path:
    """소스 일관 DB 스냅샷 경로."""
    return source_staging_dir() / "index.db"


def ensure_db_snapshot() -> Path:
    """소스의 일관 DB 복사본을 만들고(또는 갱신하고) 경로를 반환."""
    staging = source_staging_dir()
    staging.mkdir(parents=True, exist_ok=True)
    out = db_snapshot_path()
    backup._consistent_db_copy(out)
    return out


def build_manifest() -> dict:
    """전송 매니페스트 생성 — 일관 DB 스냅샷을 새로 뜨고 파일 목록을 모은다.

    이전 모드 동안 스크래핑이 멈춰 파일은 불변이므로 파일은 라이브 위치에서
    그대로 서빙하고, DB 만 일관 스냅샷을 떠 그 sha256 을 기록한다.
    """
    db_path = ensure_db_snapshot()
    with db.connect() as conn:
        counts = backup._archive_counts(conn)
    return {
        "format_version": backup.FORMAT_VERSION,
        "created_at": backup._utcnow(),
        "counts": counts,
        "db": {"bytes": db_path.stat().st_size, "sha256": _sha256_file(db_path)},
        "files": _iter_transfer_files(),
    }


def resolve_transfer_file(rel_path: str) -> Path:
    """받는 쪽이 요청한 상대경로를 검증해 실제 파일 경로로 해석한다.

    path traversal 방지: 최상위가 허용 디렉토리/파일이어야 하고, 정규화 결과가
    ARCHIVE_ROOT 안에 있어야 한다. 어긋나면 ValueError.
    """
    rel = rel_path.strip().lstrip("/")
    if not rel or ".." in Path(rel).parts:
        raise ValueError("잘못된 경로")
    top = Path(rel).parts[0]
    if top not in _TOP_DIRS and rel not in _SINGLE_FILES:
        raise ValueError("허용되지 않은 경로")
    root = config.ARCHIVE_ROOT.resolve()
    target = (config.ARCHIVE_ROOT / rel).resolve()
    if root != target and root not in target.parents:
        raise ValueError("경로가 아카이브 루트를 벗어납니다")
    if not target.is_file():
        raise ValueError("파일 없음")
    return target


def cleanup_source() -> None:
    """소스 측 이전 스테이징 정리 (이전 모드 해제 시 호출)."""
    shutil.rmtree(source_staging_dir(), ignore_errors=True)


def token_matches(conn, token: str) -> bool:
    """받는 쪽이 보낸 토큰이 발급된 이전 토큰과 일치하는지 (이전 모드 + 해시 비교)."""
    if not db.migration_mode_enabled(conn):
        return False
    stored = db.get_migration_token_hash(conn)
    if not stored or not token:
        return False
    return secrets.compare_digest(stored, auth.hash_token(token))


# ============================================================
# 받는 쪽(목적지) — 백그라운드 Pull
# ============================================================

# 재색인 패턴과 동일 — 모듈 레벨 상태 + 락 + 단일 워커 스레드.
_pull_lock = threading.Lock()
_pull_state: dict = {"status": "idle"}
_pull_thread: threading.Thread | None = None


def receiver_staging_dir() -> Path:
    """받는 쪽이 파일을 쌓는 스테이징 — CACHE_DIR 밖(루트 직속)이어야 한다
    (finalize 가 캐시를 비울 때 함께 지워지지 않도록)."""
    return config.ARCHIVE_ROOT / ".migration-staging"


def pull_status() -> dict:
    """현재 이전 진행 상태 스냅샷 (폴링용). 토큰은 절대 노출하지 않는다."""
    with _pull_lock:
        return {k: v for k, v in _pull_state.items() if k != "token"}


def _set_state(**kwargs) -> None:
    with _pull_lock:
        _pull_state.update(kwargs)


def _running() -> bool:
    return _pull_thread is not None and _pull_thread.is_alive()


def start_pull(source_url: str, token: str) -> str | None:
    """이전 백그라운드 작업 시작. 이미 진행 중이거나 입력이 비면 오류 메시지 반환."""
    global _pull_thread
    source_url = source_url.strip().rstrip("/")
    token = token.strip()
    if not source_url or not token:
        return "소스 URL 과 인증 토큰을 모두 입력하세요."
    if not source_url.startswith(("http://", "https://")):
        return "소스 URL 은 http:// 또는 https:// 로 시작해야 합니다."
    with _pull_lock:
        if _running():
            return "이미 이전이 진행 중입니다."
        _pull_state.clear()
        _pull_state.update({
            "status": "connecting",
            "source_url": source_url,
            "token": token,  # 받는 쪽 메모리에만 — pull_status 에서 제외, DB·디스크 미저장
            "insecure": source_url.startswith("http://"),
            "total": 0, "done": 0, "failed": [], "summary": None, "error": None,
        })
        _pull_thread = threading.Thread(
            target=_pull_worker, args=(source_url, token),
            name="wccg-migration-pull", daemon=True,
        )
        _pull_thread.start()
    return None


def retry_failed() -> str | None:
    """실패 목록의 파일만 다시 받는다. partial 상태에서만 가능 (토큰은 상태에서 읽음)."""
    global _pull_thread
    with _pull_lock:
        if _running():
            return "이미 이전이 진행 중입니다."
        if _pull_state.get("status") != "partial":
            return "재시도할 수 있는 상태가 아닙니다."
        source_url = _pull_state.get("source_url")
        token = _pull_state.get("token")
        retry_paths = [f["path"] for f in _pull_state.get("failed", [])]
        if not retry_paths:
            return "재시도할 파일이 없습니다."
        if not token:
            return "토큰 정보가 없습니다 — 처음부터 다시 시작하세요."
        _pull_state.update({"status": "downloading", "failed": [],
                            "done": 0, "total": len(retry_paths)})
        _pull_thread = threading.Thread(
            target=_pull_worker, args=(source_url, token),
            kwargs={"only_paths": retry_paths},
            name="wccg-migration-pull", daemon=True,
        )
        _pull_thread.start()
    return None


def finish_pull() -> str | None:
    """실패를 무시하고 이전을 마무리(finalize) — 받은 데이터로 서비스를 시작한다."""
    global _pull_thread
    with _pull_lock:
        if _running():
            return "이미 이전이 진행 중입니다."
        if _pull_state.get("status") not in ("partial", "downloading"):
            return "마무리할 수 있는 상태가 아닙니다."
        _pull_state.update({"status": "restoring"})
        _pull_thread = threading.Thread(
            target=_finalize_worker, name="wccg-migration-finalize", daemon=True,
        )
        _pull_thread.start()
    return None


def _client(token: str) -> httpx.Client:
    return httpx.Client(timeout=_HTTP_TIMEOUT, headers={_TOKEN_HEADER: token},
                        follow_redirects=True)


def _download_file(client: httpx.Client, base: str, rel: str, dest: Path) -> str | None:
    """파일 1개를 스트리밍으로 받는다. 성공 None, 3회 실패 시 마지막 오류 문자열."""
    last: str | None = None
    for attempt in range(FILE_RETRIES):
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            with client.stream("GET", f"{base}/api/migration/file",
                               params={"path": rel}) as r:
                r.raise_for_status()
                with dest.open("wb") as f:
                    for chunk in r.iter_bytes():
                        f.write(chunk)
            return None
        except Exception as e:  # noqa: BLE001 — 네트워크/IO 모두 재시도
            last = f"{type(e).__name__}: {e}"
            dest.unlink(missing_ok=True)
            if attempt < FILE_RETRIES - 1:
                time.sleep(_RETRY_BACKOFF_SECONDS[attempt])
    return last


def _pull_worker(source_url: str, token: str, *, only_paths: list[str] | None = None) -> None:
    """이전 다운로드 워커 — info → manifest → db → 파일들 → finalize/partial."""
    staging = receiver_staging_dir()
    try:
        with _client(token) as client:
            # 1) info — 버전 호환성 확인 (소스가 받는 쪽보다 신버전이면 거부)
            if only_paths is None:
                _set_state(status="connecting")
                info = client.get(f"{source_url}/api/migration/info")
                info.raise_for_status()
                meta = info.json()
                ver = meta.get("format_version")
                if not isinstance(ver, int) or ver > backup.FORMAT_VERSION:
                    _set_state(status="error",
                               error=f"지원하지 않는 소스 버전입니다 (소스 {ver} > "
                                     f"현재 {backup.FORMAT_VERSION}) — 받는 쪽을 먼저 업데이트하세요.")
                    return
                _set_state(summary=meta.get("counts"))

            # 2) manifest
            _set_state(status="manifest")
            mr = client.get(f"{source_url}/api/migration/manifest")
            mr.raise_for_status()
            manifest = mr.json()
            files = manifest.get("files", [])
            if only_paths is not None:
                wanted = set(only_paths)
                files = [f for f in files if f["path"] in wanted]
            else:
                staging.mkdir(parents=True, exist_ok=True)

            # 3) DB — 단일 파일, 실패 시 하드 오류 (부분 허용 안 함)
            if only_paths is None:
                _set_state(status="downloading", total=len(files) + 1, done=0)
                db_err = _download_file_db(client, source_url, staging / "index.db",
                                           manifest.get("db", {}))
                if db_err is not None:
                    _set_state(status="error",
                               error=f"DB 전송 실패 — 이전을 중단합니다: {db_err}")
                    return
                _bump_done()
            else:
                _set_state(status="downloading", total=len(files), done=0)

            # 4) 파일들 — 실패 시 3회 재시도, 그래도 실패면 실패 목록
            failed: list[dict] = []
            for entry in files:
                rel = entry["path"]
                err = _download_file(client, source_url, rel, staging / rel)
                if err is not None:
                    failed.append({"path": rel, "error": err})
                    logger.warning("이전 파일 전송 실패: %s — %s", rel, err)
                _bump_done()

            # 5) 실패 목록 처리
            if failed:
                _set_state(status="partial", failed=failed)
                return
    except Exception as e:  # noqa: BLE001
        logger.exception("이전 다운로드 실패")
        _set_state(status="error", error=f"{type(e).__name__}: {e}")
        return

    # 실패 없음 → finalize
    _finalize_worker()


def _download_file_db(client: httpx.Client, base: str, dest: Path, meta: dict) -> str | None:
    """DB 스냅샷 다운로드 (3회 재시도)."""
    last: str | None = None
    for attempt in range(FILE_RETRIES):
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            with client.stream("GET", f"{base}/api/migration/db") as r:
                r.raise_for_status()
                with dest.open("wb") as f:
                    for chunk in r.iter_bytes():
                        f.write(chunk)
            want = meta.get("sha256")
            if want and _sha256_file(dest) != want:
                raise ValueError("DB sha256 불일치")
            return None
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
            dest.unlink(missing_ok=True)
            if attempt < FILE_RETRIES - 1:
                time.sleep(_RETRY_BACKOFF_SECONDS[attempt])
    return last


def _bump_done() -> None:
    with _pull_lock:
        _pull_state["done"] = _pull_state.get("done", 0) + 1


def _finalize_worker() -> None:
    """스테이징을 아카이브 루트로 합쳐 이전을 마무리한다."""
    staging = receiver_staging_dir()
    try:
        _set_state(status="restoring")
        # finalize_migration 이 이전 모드를 끄는 것까지 책임진다 (받는 쪽은 정상 시작)
        backup.finalize_migration(staging)
    except Exception as e:  # noqa: BLE001
        logger.exception("이전 마무리 실패")
        _set_state(status="error", error=f"{type(e).__name__}: {e}")
        return
    _set_state(status="done")
