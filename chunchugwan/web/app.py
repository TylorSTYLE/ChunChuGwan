"""읽기 전용 대시보드 + 재아카이빙 트리거.

보안 원칙 (CLAUDE.md 5번):
- 바인딩은 127.0.0.1 고정
- 스냅샷 HTML 렌더링은 templates/snapshot.html 의
  <iframe sandbox="">  (allow-* 토큰 전부 없음 = 스크립트/폼/팝업 차단) 안에서만
- page.html 직접 응답에도 CSP `sandbox` 헤더를 붙여 직접 열어도
  대시보드 컨텍스트에서 스크립트가 실행되지 않게 한다
- 스냅샷 파일 서빙 시 경로는 DB에 기록된 dir_name 으로만 조립.
  사용자 입력 경로를 직접 파일시스템에 매핑하지 말 것.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
)

from .. import auth, config, db, differ, pipeline, storage
from . import auth_routes, system_routes
from .templating import templates

logger = logging.getLogger(__name__)

app = FastAPI(title="춘추관")
app.include_router(auth_routes.router)
app.include_router(system_routes.router)

# 인증 없이 접근 가능한 경로 (로그인 절차 자체 + 헬스체크)
# /login/passkey* 는 패스워드 통과 후 pending 세션 단계라 user 가 아직 없다 —
# 라우트 핸들러가 pending_totp 세션을 직접 요구한다.
_PUBLIC_PATHS = {
    "/healthz", "/login", "/login/totp", "/signup",
    "/login/passkey/options", "/login/passkey",
}


@app.middleware("http")
async def auth_gate(request: Request, call_next):
    """인증 게이트 + CSRF 방어.

    - POST 는 Origin(없으면 Referer) 호스트가 Host 와 일치해야 한다.
      (쿠키는 SameSite=Lax 라 이중 방어)
    - 최초 구동(사용자 0명)이면 환경변수 관리자를 등록하고, 환경변수가
      없으면 /setup 등록 페이지 외 모든 접근을 /setup 으로 보낸다.
    - AUTH_ENABLED 면 공개 경로 외에는 active 세션 필수.
      미인증 HTML 요청은 401 대신 /login?next= 으로 보낸다.
    - 모든 응답에 보안 헤더 부착. CSP 는 핸들러가 이미 설정한 경우
      (page.html 의 `sandbox`) 덮어쓰지 않는다. HSTS 는 리버스 프록시 책임.
    """
    request.state.user = None
    request.state.session = None

    if request.method == "POST":
        origin = request.headers.get("origin") or request.headers.get("referer")
        if origin and urlsplit(origin).netloc != request.headers.get("host", ""):
            return PlainTextResponse("CSRF 검증 실패", status_code=403)

    if config.AUTH_ENABLED:
        path = request.url.path
        first_run = False
        token = request.cookies.get(config.SESSION_COOKIE, "")
        with db.connect() as conn:
            if db.count_users(conn) == 0:
                first_run = not auth.bootstrap_admin_from_env(conn)
            if not first_run and token:
                sess = auth.resolve_session(conn, token)
                if sess is not None:
                    request.state.session = sess
                    if sess["state"] == "active":
                        request.state.user = db.get_user_by_id(conn, sess["user_id"])

        if first_run:
            if path not in ("/setup", "/healthz"):
                return RedirectResponse("/setup", status_code=302)
        else:
            public = path in _PUBLIC_PATHS or path.startswith("/auth/oidc/")
            if request.state.user is None and not public:
                target = path + (f"?{request.url.query}" if request.url.query else "")
                return RedirectResponse(
                    f"/login?next={quote(target, safe='')}", status_code=302
                )

    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    # DENY 금지 — snapshot.html 이 same-origin iframe 으로 page.html 을 임베드한다
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    if response.headers.get("content-type", "").startswith("text/html"):
        # 대시보드 템플릿이 인라인 <style>/<script> 를 쓰므로 unsafe-inline 허용.
        # 아카이빙된 page.html 은 위 setdefault 에서 기존 `sandbox` CSP 가 유지된다.
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; img-src 'self' data:",
        )
    return response

# 스냅샷 디렉토리에서 서빙을 허용하는 파일 화이트리스트
_ALLOWED_FILES: dict[str, str] = {
    "page.html": "text/html; charset=utf-8",
    "screenshot.png": "image/png",
    "content.md": "text/plain; charset=utf-8",
}

_BADGES = {1: "changed", 0: "same"}

# 진행 중 아카이빙 레지스트리 — 정규화 URL → 시작 시각(ISO 8601 UTC).
# 이 프로세스의 BackgroundTasks 로 실행되는 작업만 추적한다
# (CLI 등 별도 프로세스 실행은 보이지 않음. serve 는 단일 워커 전제).
_active_jobs: dict[str, str] = {}
_active_lock = threading.Lock()


def _register_job(url: str) -> bool:
    """진행 목록에 등록. 이미 진행 중인 URL 이면 False (중복 실행 방지)."""
    with _active_lock:
        if url in _active_jobs:
            return False
        _active_jobs[url] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return True


def _unregister_job(url: str) -> None:
    """진행 목록에서 제거 (완료/실패 공통)."""
    with _active_lock:
        _active_jobs.pop(url, None)


def _active_snapshot() -> dict[str, str]:
    """진행 중 작업의 사본 (렌더링/폴링용)."""
    with _active_lock:
        return dict(_active_jobs)


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def index(request: Request, queued: str = "", error: str = ""):
    active = _active_snapshot()
    with db.connect() as conn:
        pages = db.list_pages(conn)
    # 아직 pages 행이 없는 신규 URL 진행 건은 별도 행으로 보여준다
    known = {p["url"] for p in pages}
    pending_new = [
        {"url": u, "domain": urlsplit(u).hostname or "", "started_at": t}
        for u, t in sorted(active.items())
        if u not in known
    ]
    return templates.TemplateResponse(
        request, "index.html",
        {
            "pages": pages, "queued": queued, "error": error,
            "active_urls": set(active), "active_list": sorted(active),
            "pending_new": pending_new,
        },
    )


@app.get("/archive/active")
def archive_active() -> dict:
    """진행 중 아카이빙 URL 목록 (목록 화면 자동 갱신 폴링용)."""
    return {"active": sorted(_active_snapshot())}


def _period_starts(now: datetime) -> dict[str, str]:
    """현황 집계 기간의 시작 시각 (ISO 8601 UTC).

    today/week(월요일)/month/year 는 용량 트렌드, recent 는 최근 24시간 카드용.
    taken_at 컬럼과 같은 포맷이라 문자열 비교로 기간 포함을 판정한다.
    """
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    starts = {
        "today": today,
        "week": today - timedelta(days=today.weekday()),
        "month": today.replace(day=1),
        "year": today.replace(month=1, day=1),
        "recent": now - timedelta(hours=24),
    }
    return {k: v.isoformat(timespec="seconds") for k, v in starts.items()}


_TREND_PERIODS = (("today", "오늘"), ("week", "이번 주"), ("month", "이번 달"), ("year", "올해"))


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    """시스템 현황 — 아카이브 수, 기간별 용량 트렌드, 최근 스냅샷·로그."""
    starts = _period_starts(datetime.now(timezone.utc))
    with db.connect() as conn:
        total_pages = db.count_pages(conn)
        snap_dirs = db.list_snapshot_dirs(conn)
        recent_snaps = db.list_recent_snapshots(conn, limit=10)
        recent_logs = db.list_archive_logs(conn, limit=10)

    # 스냅샷은 불변이므로 디렉토리 용량을 그대로 합산한다
    sizes: dict[int, int] = {}
    counts = {k: 0 for k in starts}
    period_bytes = {k: 0 for k in starts}
    total_bytes = 0
    for row in snap_dirs:
        snap_dir = storage.page_dir(row["domain"], row["slug"]) / row["dir_name"]
        size = sum(f["bytes"] for f in storage.snapshot_files(snap_dir))
        sizes[row["id"]] = size
        total_bytes += size
        for key, start in starts.items():
            if row["taken_at"] >= start:
                counts[key] += 1
                period_bytes[key] += size

    trend = [
        {"label": label, "count": counts[key], "bytes": period_bytes[key]}
        for key, label in _TREND_PERIODS
    ]
    max_bytes = max(max(t["bytes"] for t in trend), 1)
    for t in trend:
        t["pct"] = t["bytes"] / max_bytes * 100

    return templates.TemplateResponse(
        request, "dashboard.html",
        {
            "total_pages": total_pages,
            "total_snapshots": len(snap_dirs),
            "total_bytes": total_bytes,
            "week_count": counts["week"],
            "recent_count": counts["recent"],
            "trend": trend,
            "recent_snaps": recent_snaps,
            "sizes": sizes,
            "recent_logs": recent_logs,
        },
    )


@app.get("/page/{page_id}", response_class=HTMLResponse)
def timeline(request: Request, page_id: int, queued: int = 0):
    with db.connect() as conn:
        page = db.get_page_by_id(conn, page_id)
        if page is None:
            raise HTTPException(404, "페이지 없음")
        snaps = db.list_snapshots(conn, page_id)
        checks = db.list_checks(conn, page_id)

    items = []
    for i, s in enumerate(snaps, 1):
        badge = "new" if i == 1 else _BADGES[s["changed"]]
        items.append({"idx": i, "snap": s, "badge": badge})
    items.reverse()  # 최신 먼저
    return templates.TemplateResponse(
        request, "timeline.html",
        {"page": page, "items": items, "checks": checks, "queued": queued},
    )


def _load_snapshot(snapshot_id: int):
    with db.connect() as conn:
        snap = db.get_snapshot(conn, snapshot_id)
    if snap is None:
        raise HTTPException(404, "스냅샷 없음")
    return snap


def _snapshot_dir(snap) -> Path:
    return storage.page_dir(snap["domain"], snap["slug"]) / snap["dir_name"]


@app.get("/snapshot/{snapshot_id}", response_class=HTMLResponse)
def snapshot_view(request: Request, snapshot_id: int):
    snap = _load_snapshot(snapshot_id)
    title = None
    try:
        title = storage.read_meta(_snapshot_dir(snap)).title
    except OSError:
        pass
    return templates.TemplateResponse(
        request, "snapshot.html",
        {
            "snap": snap,
            "title": title,
            "page_html_url": f"/snapshot/{snapshot_id}/file/page.html",
            "screenshot_url": f"/snapshot/{snapshot_id}/file/screenshot.png",
            "content_url": f"/snapshot/{snapshot_id}/file/content.md",
        },
    )


@app.get("/snapshot/{snapshot_id}/file/{name}")
def snapshot_file(snapshot_id: int, name: str):
    media_type = _ALLOWED_FILES.get(name)
    if media_type is None:
        raise HTTPException(404, "허용되지 않은 파일")
    snap = _load_snapshot(snapshot_id)
    path = _snapshot_dir(snap) / name
    if not path.is_file():
        raise HTTPException(404, "파일 없음")
    headers = {}
    if name == "page.html":
        # 직접 열어도 아카이빙된 JS가 실행되지 않도록 문서 자체를 샌드박스
        headers["Content-Security-Policy"] = "sandbox"
    return FileResponse(path, media_type=media_type, headers=headers)


def _collapse_equal(
    rows: list[tuple[str, str, str]], context: int = 3
) -> list[tuple[str, str, str]]:
    """긴 equal 구간을 ('skip', 'N줄 동일', '') 행으로 접는다."""
    out: list[tuple[str, str, str]] = []
    i = 0
    while i < len(rows):
        if rows[i][0] != "equal":
            out.append(rows[i])
            i += 1
            continue
        j = i
        while j < len(rows) and rows[j][0] == "equal":
            j += 1
        run = rows[i:j]
        head = context if i > 0 else 0          # 문서 시작이면 위 문맥 불필요
        tail = context if j < len(rows) else 0  # 문서 끝이면 아래 문맥 불필요
        if len(run) > head + tail + 1:
            out.extend(run[:head])
            out.append(("skip", f"{len(run) - head - tail}줄 동일", ""))
            if tail:
                out.extend(run[-tail:])
        else:
            out.extend(run)
        i = j
    return out


def _resolve_diff_pair(page_id: int, from_idx: int | None, to_idx: int | None):
    """diff 대상 페이지/스냅샷 쌍을 검증해 반환."""
    with db.connect() as conn:
        page = db.get_page_by_id(conn, page_id)
        if page is None:
            raise HTTPException(404, "페이지 없음")
        snaps = db.list_snapshots(conn, page_id)
    if len(snaps) < 2:
        raise HTTPException(400, f"비교하려면 스냅샷이 2개 이상 필요합니다 (현재 {len(snaps)}개)")

    if to_idx is None:
        to_idx = len(snaps)
    if from_idx is None:
        from_idx = to_idx - 1
    if not (1 <= from_idx < to_idx <= len(snaps)):
        raise HTTPException(400, f"잘못된 범위: from={from_idx} to={to_idx} (1 ~ {len(snaps)})")
    return page, snaps, from_idx, to_idx, snaps[from_idx - 1], snaps[to_idx - 1]


def _screenshot_paths(page, old_snap, new_snap) -> tuple[Path, Path]:
    base = storage.page_dir(page["domain"], page["slug"])
    return (
        base / old_snap["dir_name"] / "screenshot.png",
        base / new_snap["dir_name"] / "screenshot.png",
    )


@app.get("/diff/{page_id}", response_class=HTMLResponse)
def diff_view(
    request: Request,
    page_id: int,
    from_idx: int | None = Query(None, alias="from"),
    to_idx: int | None = Query(None, alias="to"),
):
    page, snaps, from_idx, to_idx, old_snap, new_snap = _resolve_diff_pair(
        page_id, from_idx, to_idx
    )
    texts = []
    for snap in (old_snap, new_snap):
        path = storage.page_dir(page["domain"], page["slug"]) / snap["dir_name"] / "content.md"
        if not path.is_file():
            raise HTTPException(404, f"content.md 없음: {snap['dir_name']}")
        texts.append(path.read_text(encoding="utf-8"))

    d = differ.diff_text(texts[0], texts[1])

    shot_ratio = None
    old_shot_path, new_shot_path = _screenshot_paths(page, old_snap, new_snap)
    if old_shot_path.is_file() and new_shot_path.is_file():
        shot_ratio, _ = differ.cached_screenshot_diff(
            old_shot_path, new_shot_path,
            f"shotdiff-{old_snap['id']}-{new_snap['id']}",
        )

    return templates.TemplateResponse(
        request, "diff.html",
        {
            "page": page,
            "d": d,
            "rows": _collapse_equal(d.rows),
            "from_idx": from_idx, "to_idx": to_idx, "total": len(snaps),
            "old_snap": old_snap, "new_snap": new_snap,
            "old_shot": f"/snapshot/{old_snap['id']}/file/screenshot.png",
            "new_shot": f"/snapshot/{new_snap['id']}/file/screenshot.png",
            "shot_ratio": shot_ratio,
            "shotdiff_url": f"/diff/{page_id}/shotdiff?from={from_idx}&to={to_idx}",
        },
    )


@app.get("/diff/{page_id}/shotdiff")
def shotdiff(
    page_id: int,
    from_idx: int | None = Query(None, alias="from"),
    to_idx: int | None = Query(None, alias="to"),
):
    """픽셀 diff 하이라이트 이미지 (캐시에서 서빙)."""
    page, _snaps, _f, _t, old_snap, new_snap = _resolve_diff_pair(page_id, from_idx, to_idx)
    old_shot_path, new_shot_path = _screenshot_paths(page, old_snap, new_snap)
    if not (old_shot_path.is_file() and new_shot_path.is_file()):
        raise HTTPException(404, "스크린샷 없음")
    _ratio, out_png = differ.cached_screenshot_diff(
        old_shot_path, new_shot_path, f"shotdiff-{old_snap['id']}-{new_snap['id']}"
    )
    return FileResponse(out_png, media_type="image/png")


_LOG_STATUSES = ("new", "changed", "unchanged", "forced_same", "error")


@app.get("/logs", response_class=HTMLResponse)
def logs_view(
    request: Request,
    domain: str | None = None,
    page_id: int | None = None,
    snapshot_id: int | None = None,
    status: str | None = None,
    limit: int = 100,
):
    """아카이브 실행 로그. 도메인/페이지/스냅샷/상태 필터 지원."""
    limit = max(1, min(limit, 500))
    if status not in _LOG_STATUSES:
        status = None
    with db.connect() as conn:
        logs = db.list_archive_logs(
            conn, domain=domain or None, page_id=page_id,
            snapshot_id=snapshot_id, status=status, limit=limit,
        )
        domains = db.list_log_domains(conn)
        page = db.get_page_by_id(conn, page_id) if page_id else None

    items = []
    for row in logs:
        try:
            steps = json.loads(row["steps"]) if row["steps"] else []
        except ValueError:
            steps = []
        # 스냅샷이 생긴 로그는 (불변) 스냅샷 디렉토리에서 파일 목록/용량 조회
        files: list[dict] = []
        if row["snap_dir_name"]:
            snap_dir = (
                storage.page_dir(row["snap_domain"], row["snap_slug"])
                / row["snap_dir_name"]
            )
            files = storage.snapshot_files(snap_dir)
        items.append({
            "log": row, "steps": steps, "files": files,
            "total_bytes": sum(f["bytes"] for f in files) if files else None,
        })
    return templates.TemplateResponse(
        request, "logs.html",
        {
            "items": items, "domains": domains, "page": page,
            "domain": domain or "", "status": status or "",
            "snapshot_id": snapshot_id, "limit": limit,
            "statuses": _LOG_STATUSES,
        },
    )


def _run_archive(url: str) -> None:
    """백그라운드 아카이빙. 결과는 archive_logs 에 기록된다."""
    try:
        outcome = pipeline.archive_url(url, source="web")
        logger.info("아카이빙 완료: %s [%s]", url, outcome.status)
    except Exception:
        logger.exception("아카이빙 실패: %s", url)
    finally:
        _unregister_job(url)


def _queue_archive(background: BackgroundTasks, url: str) -> None:
    """진행 목록 등록 후 백그라운드 작업 추가. 이미 진행 중이면 무시.

    등록은 응답 전(동기)에 해서 리다이렉트된 목록 화면이 바로 진행 상태를 본다.
    """
    if _register_job(url):
        background.add_task(_run_archive, url)


@app.post("/archive")
def archive_new(background: BackgroundTasks, url: str = Form(...)):
    """대시보드에서 새 URL 아카이빙. URL 검증은 동기로, 캡처는 백그라운드로."""
    try:
        norm = storage.normalize_url(url)
    except ValueError as exc:
        return RedirectResponse(
            f"/?error={quote(str(exc), safe='')}", status_code=303
        )
    _queue_archive(background, norm)
    return RedirectResponse(f"/?queued={quote(norm, safe='')}", status_code=303)


@app.post("/page/{page_id}/rearchive")
def rearchive(page_id: int, background: BackgroundTasks):
    with db.connect() as conn:
        page = db.get_page_by_id(conn, page_id)
    if page is None:
        raise HTTPException(404, "페이지 없음")
    _queue_archive(background, page["url"])
    return RedirectResponse(url=f"/page/{page_id}?queued=1", status_code=303)
