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

import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

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
                "GET /debug/logs?tail=N&level=&src=&q=", "GET /debug/search",
                "GET /debug/storage", "GET /debug/config",
                "GET /debug/inspect?url=", "GET /debug/crawls",
                "GET /debug/crawl/{id}/failures", "GET /debug/challenges",
                "GET /debug/log/{id}",
            ],
            "trigger": [
                "POST /debug/capture  {url, force?}",
                "POST /debug/run/scheduler", "POST /debug/run/archive",
                "POST /debug/run/crawl  {crawl_id?}", "POST /debug/run/crawl-schedules",
                "POST /debug/run/recover-stale", "POST /debug/run/reindex  {full?}",
                "POST /debug/live/{job_id}/cancel", "POST /debug/live/{job_id}/solve",
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
                "storage_migration_active": db.storage_migration_active(conn),
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
    def logs(tail: int = 100, level: str | None = None,
             src: str | None = None, q: str | None = None) -> dict:
        """시스템 로그 tail (최신 순). level=ERROR·src=worker 로 필터, q= 는 본문 부분일치."""
        tail = max(1, min(tail, 1000))
        with db.connect() as conn:
            # system_logs 는 본문 LIKE 미지원 — q 가 있으면 더 넓게 떠서 파이썬에서 거른다.
            fetch = min(tail * 10, 5000) if q else tail
            rows = db.list_system_logs(conn, level=level, source=src, limit=fetch)
        out = [dict(r) for r in rows]
        if q:
            ql = q.lower()
            out = [r for r in out
                   if ql in " ".join(str(v) for v in r.values()).lower()][:tail]
        return {"count": len(out), "logs": out}

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
        """저장 백엔드(local/s3)·마이그레이션 진행률·사용량·쓰기 중단 상태."""
        from .. import storage_migration
        with db.connect() as conn:
            base = {
                "backend": db.storage_backend(conn),
                "migration_active": db.storage_migration_active(conn),
                "writes_paused": db.writes_paused(conn),
                "s3_requested": config.s3_requested(),
                "s3_usage": db.s3_usage(conn),
            }
        try:  # 인메모리 진행률(done/total/failed/direction) + DB 요약 — serve 프로세스 기준
            base["migration"] = storage_migration.status()
        except Exception as e:
            base["migration"] = {"_error": str(e)}
        return base

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

    # ── 큐 소비 트리거 — worker 가 돌리는 크롤·크롤스케줄 루프를 1회 스텝 (코어 경유) ──
    @app.post("/debug/run/crawl")
    def run_crawl(payload: dict | None = Body(default=None)) -> dict:
        """기한이 된 크롤 페이지 하나를 1회 처리 (crawler.process_next). crawl_id 로 한정 가능."""
        from .. import crawler

        crawl_id = (payload or {}).get("crawl_id")
        step = crawler.process_next(crawl_id=crawl_id)
        if step is None:
            return {"processed": False}
        return {"processed": True, "crawl_id": step.crawl_id, "url": step.url,
                "status": step.status, "error": step.error}

    @app.post("/debug/run/crawl-schedules")
    def run_crawl_schedules() -> dict:
        """기한이 된 크롤 스케줄을 1회 실행 (crawler.run_due_schedules)."""
        from .. import crawler

        results = crawler.run_due_schedules(source="api")
        return {"processed": len(results),
                "results": [{"start_url": r.start_url, "status": r.status,
                             "crawl_id": r.crawl_id, "error": r.error} for r in results]}

    @app.post("/debug/run/recover-stale")
    def run_recover_stale() -> dict:
        """중단으로 in_progress 에 박힌 단발 작업·크롤 페이지를 pending 으로 복구(멱등)."""
        cutoff = _stale_cutoff_iso()
        with db.connect() as conn:
            jobs = db.recover_stale_archive_jobs(conn, cutoff)
            pages = db.recover_stale_crawl_pages(conn, cutoff)
        return {"recovered_archive_jobs": jobs, "recovered_crawl_pages": pages}

    @app.post("/debug/run/reindex")
    def run_reindex(payload: dict | None = Body(default=None)) -> dict:
        """미색인 스냅샷 백필(full=true 면 전체 재색인). 동기 — 큰 DB 에선 오래 걸린다."""
        from .. import searchindex

        full = bool((payload or {}).get("full", False))
        n = searchindex.reindex_all() if full else searchindex.backfill_all()
        return {"reindexed": n, "full": full}

    # ── 라이브 챌린지로 멈춘 워커 풀어주기 (코어 플래그 — 워커가 다음 폴링에 반영) ──
    @app.post("/debug/live/{job_id}/cancel")
    def live_cancel(job_id: int) -> dict:
        """needs_human 으로 page 를 붙든 작업을 취소(워커가 다음 폴링에 중단)."""
        with db.connect() as conn:
            db.set_live_cancel(conn, job_id)
        return {"ok": True, "job_id": job_id, "action": "cancel"}

    @app.post("/debug/live/{job_id}/solve")
    def live_solve(job_id: int) -> dict:
        """needs_human 작업을 '사람 확인 완료'로 강제 채택(워커가 현재 page 를 채택)."""
        with db.connect() as conn:
            db.set_live_force_solve(conn, job_id)
        return {"ok": True, "job_id": job_id, "action": "force_solve"}

    # ── 진단 읽기 — 특정 URL·크롤 실패·챌린지·로그 상세 ──
    @app.get("/debug/inspect")
    def inspect(url: str) -> dict:
        """특정 URL 의 페이지·스냅샷·최근 아카이브 로그(단계/오류) — '왜 이렇게 캡처됐나'."""
        with db.connect() as conn:
            page = db.get_page_aggregate(conn, url)
            if page is None:
                raise HTTPException(status_code=404, detail="해당 URL 의 페이지가 없습니다")
            page = dict(page)
            snaps = [dict(s) for s in db.list_snapshots(conn, page["id"])][-10:]
            log_rows = db.list_archive_logs(conn, page_id=page["id"], limit=10)
        return {"page": page, "snapshots": snaps,
                "archive_logs": [_log_row(r) for r in log_rows]}

    @app.get("/debug/crawls")
    def crawls(limit: int = 50) -> dict:
        """크롤 회차 목록 + 상태별 페이지 수."""
        with db.connect() as conn:
            out = []
            for c in db.list_crawls(conn, limit=max(1, min(limit, 200))):
                row = dict(c)
                row["page_counts"] = db.crawl_page_counts(conn, row["id"])
                out.append(row)
        return {"count": len(out), "crawls": out}

    @app.get("/debug/crawl/{crawl_id}/failures")
    def crawl_failures(crawl_id: int, limit: int = 50) -> dict:
        """크롤의 실패 페이지 — url·오류·시도횟수 (실패 원인 진단)."""
        with db.connect() as conn:
            rows = db.list_crawl_pages(conn, crawl_id, status="failed")
        items = [{"url": r["url"], "error": r["error"], "attempts": r["attempts"],
                  "next_attempt_at": r["next_attempt_at"]}
                 for r in rows[:max(1, min(limit, 500))]]
        return {"crawl_id": crawl_id, "failed": len(rows), "pages": items}

    @app.get("/debug/challenges")
    def challenges() -> dict:
        """사람 확인 대기(needs_human) 작업 — 라이브 챌린지로 멈춘 워커."""
        with db.connect() as conn:
            rows = db.list_needs_human_jobs(conn)
        return {"count": len(rows), "jobs": [dict(r) for r in rows]}

    @app.get("/debug/log/{log_id}")
    def archive_log(log_id: int) -> dict:
        """아카이브 실행 로그 1건 (steps JSON 파싱 — 폴백/인증서/인증 경로)."""
        with db.connect() as conn:
            row = db.get_archive_log(conn, log_id)
        if row is None:
            raise HTTPException(status_code=404, detail="해당 로그가 없습니다")
        return _log_row(row)

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


def _stale_cutoff_iso() -> str:
    """스테일 클레임 회수 기준 시각 — 코어 process_next 진입부와 같은 cutoff."""
    return (datetime.now(timezone.utc)
            - timedelta(seconds=config.CRAWL_STALE_CLAIM_SECONDS)).isoformat()


def _log_row(row) -> dict:
    """archive_logs 행을 dict 로 — steps 가 JSON 문자열이면 파싱해 펼친다."""
    d = dict(row)
    steps = d.get("steps")
    if isinstance(steps, str):
        try:
            d["steps"] = json.loads(steps)
        except (ValueError, TypeError):
            pass
    return d
