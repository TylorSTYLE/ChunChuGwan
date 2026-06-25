"""디버그 진단 서버 — 별도 HTTP 포트로 내부 상태를 노출한다(읽기 + 안전한 트리거).

`WCCG_DEBUG=on` 일 때만 serve/worker 프로세스가 이 서버를 별도 포트
(`WCCG_DEBUG_PORT`, 기본 8799)에 데몬 스레드로 띄운다. 릴리스 compose 는 이
토글을 주지 않으므로(=off) 포트 자체가 열리지 않는다 — 개발/테스트(develop)
환경에서 문제를 빠르게 진단·재현하기 위한 도구다.

보안 (CLAUDE.md 아키텍처 원칙):
- **원칙 6 — 인증/시크릿 데이터 비노출.** `/debug/config` 는 안전한 운영 설정만
  화이트리스트로 내보내고, 시크릿(비밀번호·세션·키)은 값이 아니라 '설정됨
  여부(bool)' 만 알린다. 시스템 로그 본문은 대시보드 `/system/logs` 와 동일한
  공개 수준이다.
- **원칙 1 — 쓰기는 코어 경유.** 트리거(`/debug/capture`·`/debug/run/*`)는
  `pipeline`·`scheduler`·`archive_worker` 코어 함수를 그대로 호출한다. DB·파일을
  직접 조작하지 않는다.
- **원칙 5 — 루프백 기본.** 기본 바인딩 127.0.0.1. `WCCG_DEBUG_HOST=0.0.0.0` 으로
  LAN 노출하면 같은 네트워크의 누구나 내부 상태를 볼 수 있으므로 경고를 남기고,
  `WCCG_DEBUG_TOKEN` 설정 시 X-Debug-Token 헤더를 요구한다(선택적 하드닝).
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone

from .. import __version__, config, db

logger = logging.getLogger(__name__)

_server_thread: threading.Thread | None = None
_started_monotonic: float | None = None


# ---- 기동 ----

def maybe_start(source: str) -> None:
    """`WCCG_DEBUG=on` 이면 디버그 서버를 데몬 스레드로 띄운다 (멱등).

    source 는 이 서버를 띄운 프로세스('serve'|'worker') — health 응답에 싣는다.
    serve(uvicorn) 와 worker 는 별도 프로세스(도커는 별도 컨테이너)라 각자 같은
    포트를 자기 네임스페이스에서 바인딩한다. 같은 호스트에서 두 프로세스를 동시에
    디버그 켜면 두 번째는 포트 충돌로 못 띄우므로(경고만) 포트를 달리한다.
    """
    global _server_thread, _started_monotonic
    if not config.DEBUG_ENABLED:
        return
    if _server_thread is not None and _server_thread.is_alive():
        return
    _started_monotonic = time.monotonic()
    thread = threading.Thread(
        target=_run_server, args=(source,), name="wccg-debug-server", daemon=True,
    )
    thread.start()
    _server_thread = thread


def _run_server(source: str) -> None:
    import uvicorn

    host, port = config.DEBUG_HOST, config.DEBUG_PORT
    if not _is_loopback(host):
        logger.warning(
            "디버그 서버를 %s:%d 에 바인딩 — 루프백이 아니므로 같은 네트워크의 "
            "누구나 내부 상태를 볼 수 있습니다(WCCG_DEBUG_TOKEN 으로 보호 권장). "
            "develop/테스트 전용으로만 쓰세요.",
            host, port,
        )
    else:
        logger.info("디버그 서버 시작 — http://%s:%d (source=%s)", host, port, source)
    app = build_app(source)
    try:
        # uvicorn 은 비-main 스레드에서 시그널 핸들러 설치를 건너뛴다(스레드 안전).
        uvicorn.run(app, host=host, port=port, log_level="warning", access_log=False)
    except OSError as e:  # 포트 사용 중 등 — 진단 도구가 본 프로세스를 죽이지 않게.
        logger.warning("디버그 서버를 시작할 수 없습니다(포트 %d 사용 중?): %s", port, e)


# ---- 앱 ----

def build_app(source: str):
    """디버그 엔드포인트를 가진 독립 FastAPI 앱 (테스트는 이 앱을 직접 띄운다)."""
    from fastapi import Body, FastAPI, HTTPException, Request
    from fastapi.responses import JSONResponse

    app = FastAPI(title="춘추관 debug", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def _token_gate(request: Request, call_next):
        token = config.DEBUG_TOKEN
        if token and request.headers.get("x-debug-token") != token:
            return JSONResponse({"error": "디버그 토큰 불일치"}, status_code=401)
        return await call_next(request)

    @app.get("/")
    @app.get("/debug")
    def index() -> dict:
        """엔드포인트 목록 (자체 문서)."""
        return {
            "service": "춘추관 debug",
            "source": source,
            "version": __version__,
            "read": [
                "GET /debug/health", "GET /debug/queues", "GET /debug/db",
                "GET /debug/logs?tail=N&level=&src=", "GET /debug/search",
                "GET /debug/storage", "GET /debug/config",
            ],
            "trigger": [
                "POST /debug/capture  {url, force?}",
                "POST /debug/run/scheduler", "POST /debug/run/archive",
            ],
        }

    @app.get("/debug/health")
    def health() -> dict:
        """프로세스 생존·버전·백그라운드 스레드 상태."""
        threads = sorted(t.name for t in threading.enumerate() if t.name.startswith("wccg-"))
        start = _started_monotonic or time.monotonic()
        return {
            "ok": True,
            "source": source,
            "version": __version__,
            "pid": os.getpid(),
            "uptime_seconds": round(time.monotonic() - start, 1),
            "scheduler_enabled": config.SCHEDULER_ENABLED,
            "auth_enabled": config.AUTH_ENABLED,
            "worker_threads": threads,
            "debug_bind": f"{config.DEBUG_HOST}:{config.DEBUG_PORT}",
        }

    @app.get("/debug/queues")
    def queues() -> dict:
        """작업 큐 상태 — 단발 아카이빙·크롤·스케줄 + 쓰기 일시중지 여부."""
        now = _utcnow_iso()
        with db.connect() as conn:
            return {
                "archive_jobs": _group_by_status(conn, "archive_jobs"),
                "crawl_pages": _group_by_status(conn, "crawl_pages"),
                "crawls": _group_by_status(conn, "crawls"),
                "schedules": {
                    "total": _scalar(conn, "SELECT COUNT(*) FROM schedules"),
                    "due_now": _scalar_safe(
                        conn, "SELECT COUNT(*) FROM schedules WHERE next_run_at <= ?", (now,)),
                },
                "crawl_schedules": {
                    "total": _scalar(conn, "SELECT COUNT(*) FROM crawl_schedules"),
                    "due_now": _scalar_safe(
                        conn, "SELECT COUNT(*) FROM crawl_schedules WHERE next_run_at <= ?", (now,)),
                },
                "active_jobs": [dict(r) for r in db.list_active_archive_jobs(conn)],
                # 아무것도 처리되지 않는 이유를 바로 보여준다 — 이전/마이그레이션 중이면 쓰기 중단.
                "writes_paused": db.writes_paused(conn),
                "migration_mode": db.migration_mode_enabled(conn),
            }

    @app.get("/debug/db")
    def db_state() -> dict:
        """테이블별 행 수 + 무결성 빠른 점검 + 저널 모드 + 파일 크기."""
        with db.connect() as conn:
            tables = [
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            ]
            counts: dict[str, object] = {}
            for table in tables:
                try:
                    counts[table] = _scalar(conn, f'SELECT COUNT(*) FROM "{table}"')
                except sqlite3.OperationalError as e:
                    counts[table] = f"err: {e}"
            quick = conn.execute("PRAGMA quick_check").fetchone()[0]
            user_version = conn.execute("PRAGMA user_version").fetchone()[0]
            journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
        db_path = config.DB_PATH
        return {
            "path": str(db_path),
            "size_bytes": db_path.stat().st_size if db_path.is_file() else None,
            "wal_size_bytes": _wal_size(db_path),
            "user_version": user_version,
            "journal_mode": journal,
            "quick_check": quick,
            "row_counts": counts,
        }

    @app.get("/debug/logs")
    def logs(tail: int = 100, level: str | None = None, src: str | None = None) -> dict:
        """시스템 로그 tail (최신 순). level=ERROR·src=worker 등으로 필터."""
        tail = max(1, min(tail, 1000))
        with db.connect() as conn:
            rows = db.list_system_logs(conn, level=level, source=src, limit=tail)
        return {"count": len(rows), "logs": [dict(r) for r in rows]}

    @app.get("/debug/search")
    def search_state() -> dict:
        """전문 검색(FTS5) 인덱스 상태."""
        with db.connect() as conn:
            available = db.search_index_available(conn)
            return {
                "available": available,
                "fts_rows": db.count_fts_rows(conn) if available else 0,
                "indexed_snapshots": db.count_search_indexed(conn) if available else 0,
                "unindexed_pending": (
                    db.count_unindexed_search_snapshots(conn) if available else None),
            }

    @app.get("/debug/storage")
    def storage_state() -> dict:
        """저장 백엔드(local/s3)·마이그레이션·쓰기 중단 상태."""
        with db.connect() as conn:
            return {
                "backend": db.storage_backend(conn),
                "migration_active": db.storage_migration_active(conn),
                "writes_paused": db.writes_paused(conn),
                "s3_requested": config.s3_requested(),
            }

    @app.get("/debug/config")
    def config_state() -> dict:
        """유효 설정 — 시크릿 값은 절대 내보내지 않고 '설정됨 여부' 만 알린다(원칙 6)."""
        return {
            "toggles": {
                "auth_enabled": config.AUTH_ENABLED,
                "scheduler_enabled": config.SCHEDULER_ENABLED,
                "debug_enabled": config.DEBUG_ENABLED,
                "capture_engine": config.CAPTURE_ENGINE,
                "capture_headful": config.CAPTURE_HEADFUL,
                "capture_channel": config.CAPTURE_CHANNEL or None,
                "live_challenge": config.LIVE_CHALLENGE,
            },
            "paths": {
                "archive_root": str(config.ARCHIVE_ROOT),
                "db_path": str(config.DB_PATH),
                "log_file": config.LOG_FILE or None,
            },
            "bind": {
                "dashboard_host": config.DASHBOARD_HOST,
                "dashboard_port": config.DASHBOARD_PORT,
                "debug_host": config.DEBUG_HOST,
                "debug_port": config.DEBUG_PORT,
                "public_url": config.PUBLIC_URL or None,
            },
            "limits": {
                "crawl_workers": config.CRAWL_WORKERS,
                "session_ttl_days": config.SESSION_TTL_DAYS,
                "system_log_max_rows": config.SYSTEM_LOG_MAX_ROWS,
            },
            "secrets_configured": {  # 값이 아니라 설정 여부만 — 원칙 6
                "secret_key": bool(config.SECRET_KEY),
                "oidc": config.oidc_enabled(),
                "smtp_password": bool(config.SMTP_PASSWORD),
                "s3_secret": bool(config.S3_SECRET_ACCESS_KEY),
                "admin_password_env": bool(config.ADMIN_PASSWORD),
                "debug_token": bool(config.DEBUG_TOKEN),
                "setup_token": bool(config.SETUP_TOKEN),
            },
        }

    @app.post("/debug/capture")
    def capture(payload: dict | None = Body(default=None)) -> dict:
        """1회성 캡처를 코어(pipeline)로 동기 실행하고 결과 트레이스를 반환한다.

        실제 캡처(브라우저 렌더·추출·저장)가 끝날 때까지 블로킹한다(수십 초 가능).
        netcheck/잘못된 URL 은 400, 캡처 실패는 502 로 사유를 돌려준다.
        """
        url = (payload or {}).get("url")
        if not isinstance(url, str) or not url.strip():
            raise HTTPException(status_code=400, detail="url 이 필요합니다")
        force = bool((payload or {}).get("force", False))
        from .. import pipeline

        try:
            outcome = pipeline.archive_url(url.strip(), force=force, source="api")
        except ValueError as e:  # netcheck(루프백/사설 태그)·잘못된 URL
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:  # 캡처/네트워크 실패
            raise HTTPException(status_code=502, detail=f"{type(e).__name__}: {e}")
        return {
            "status": outcome.status,
            "url": outcome.url,
            "content_hash": outcome.content_hash,
            "snapshot_id": outcome.snapshot_id,
            "http_status": outcome.http_status,
            "title": outcome.title,
            "taken_at": outcome.taken_at,
            "documents": outcome.documents,
        }

    @app.post("/debug/run/scheduler")
    def run_scheduler() -> dict:
        """기한이 된 페이지 스케줄을 1회 실행 (코어 scheduler.run_due)."""
        from .. import scheduler

        results = scheduler.run_due(source="api")
        return {
            "processed": len(results),
            "results": [
                {"url": r.url, "status": r.status, "error": r.error} for r in results
            ],
        }

    @app.post("/debug/run/archive")
    def run_archive() -> dict:
        """단발 아카이빙 큐에서 작업 하나를 1회 처리 (코어 archive_worker.process_next)."""
        from .. import archive_worker

        step = archive_worker.process_next()
        if step is None:
            return {"processed": False}
        return {
            "processed": True,
            "url": step.url,
            "status": step.status,
            "error": step.error,
        }

    return app


# ---- 헬퍼 ----

def _is_loopback(host: str) -> bool:
    return host in ("127.0.0.1", "::1", "localhost") or host.startswith("127.")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    return conn.execute(sql, params).fetchone()[0]


def _scalar_safe(conn: sqlite3.Connection, sql: str, params: tuple = ()):
    """스키마에 컬럼이 없을 수 있는 조회 — 실패하면 None (디버그 엔드포인트가 안 죽게)."""
    try:
        return conn.execute(sql, params).fetchone()[0]
    except sqlite3.OperationalError as e:
        return f"err: {e}"


def _group_by_status(conn: sqlite3.Connection, table: str) -> dict:
    """status 컬럼 기준 행 수 묶음. 테이블/컬럼이 없으면 오류 표시만 남긴다."""
    try:
        rows = conn.execute(
            f'SELECT status, COUNT(*) AS n FROM "{table}" GROUP BY status'
        ).fetchall()
    except sqlite3.OperationalError as e:
        return {"_error": str(e)}
    return {row["status"]: row["n"] for row in rows}


def _wal_size(db_path) -> int:
    wal = db_path.with_name(db_path.name + "-wal")
    return wal.stat().st_size if wal.is_file() else 0
