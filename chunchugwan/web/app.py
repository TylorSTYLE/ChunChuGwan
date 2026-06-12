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
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
)

from .. import (
    auth, config, db, deletion, differ, pipeline, resources, scheduler, storage,
)
from . import api_routes, auth_routes, i18n, permissions, system_routes
from .i18n import t
from .templating import templates

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """서버 구동 동안 스케줄러 폴링 스레드 운영 (WCCG_SCHEDULER=off 면 비활성).

    진행 중 작업 레지스트리(claim/release)를 같이 써서 수동 재아카이빙과
    같은 URL 이 동시에 돌지 않게 한다.
    """
    stop = threading.Event()
    thread: threading.Thread | None = None
    if config.SCHEDULER_ENABLED:
        thread = threading.Thread(
            target=scheduler.run_loop,
            args=(stop,),
            kwargs={
                "poll_seconds": config.SCHEDULER_POLL_SECONDS,
                "claim": _register_job,
                "release": _unregister_job,
            },
            name="wccg-scheduler",
            daemon=True,
        )
        thread.start()
    yield
    stop.set()
    if thread is not None:
        thread.join(timeout=5)


app = FastAPI(title="춘추관", lifespan=_lifespan)
app.include_router(auth_routes.router)
app.include_router(system_routes.router)
app.include_router(api_routes.router)

# 인증 없이 접근 가능한 경로 (로그인 절차 자체 + 헬스체크)
# /login/passkey* 는 패스워드 통과 후 pending 세션 단계라 user 가 아직 없다 —
# 라우트 핸들러가 pending_totp 세션을 직접 요구한다.
# /invite/{token} 은 초대받은 본인의 가입 페이지 — 토큰 자체가 자격 증명이다.
_PUBLIC_PATHS = {
    "/healthz", "/login", "/login/totp", "/signup", "/lang",
    "/login/passkey/options", "/login/passkey",
}

# 브라우저가 주소만 보고 자동 요청하는 아이콘 경로 — 라우트가 없으므로 404 가
# 정답이다. /login·/setup 으로 리다이렉트하면 로그만 오염되므로 그대로 통과시킨다.
_BROWSER_ICON_PATHS = {
    "/favicon.ico", "/apple-touch-icon.png", "/apple-touch-icon-precomposed.png",
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
    request.state.locale = i18n.resolve_locale(request)

    if request.method == "POST":
        origin = request.headers.get("origin") or request.headers.get("referer")
        if origin and urlsplit(origin).netloc != request.headers.get("host", ""):
            return PlainTextResponse(t(request, "CSRF 검증 실패"), status_code=403)

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
            if path.startswith("/api/"):
                # API 클라이언트에게 /setup 리다이렉트는 의미가 없다
                return PlainTextResponse(
                    "최초 설정이 완료되지 않았습니다", status_code=401
                )
            if path not in ("/setup", "/healthz", "/lang") and path not in _BROWSER_ICON_PATHS:
                return RedirectResponse("/setup", status_code=302)
        else:
            # 차단된 계정 — 로그아웃 외 모든 접근 거부 (세션은 차단 시점에
            # 삭제되지만, 그 사이 발급된 세션이 있어도 여기서 막힌다)
            if (
                request.state.user is not None
                and request.state.user["role"] == "blocked"
                and path != "/logout"
            ):
                return PlainTextResponse(
                    t(request, "차단된 계정입니다. 관리자에게 문의하세요."), status_code=403
                )
            # /resource/ 는 인증 예외 — 샌드박스된 page.html(불투명 출처)의
            # 하위 자원 요청에는 SameSite 쿠키가 붙지 않아 세션 인증이
            # 불가능하다. 이름이 콘텐츠 sha256 그 자체라 추측 불가능하고,
            # 화이트리스트 미디어 타입만 CSP sandbox 로 서빙한다.
            # /api/ 는 세션 인증 대상이 아니다 — api_routes 의존성이
            # API 키를 검증한다 (키 없음/만료 시 401 JSON).
            public = (
                path in _PUBLIC_PATHS
                or path in _BROWSER_ICON_PATHS
                or path.startswith("/auth/oidc/")
                or path.startswith("/invite/")
                or path.startswith("/resource/")
                or path.startswith("/api/")
            )
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

# 스냅샷 파일 서빙 화이트리스트 — 논리 이름 → (실제 후보 파일, 미디어 타입).
# 앞선 후보부터 존재하는 파일을 서빙한다 (.gz 후보는 Content-Encoding: gzip).
# 'screenshot.png' 는 구형 링크 하위 호환 별칭.
_HTML_TYPE = "text/html; charset=utf-8"
_ALLOWED_FILES: dict[str, tuple[tuple[str, str], ...]] = {
    "page.html": (("page.html.gz", _HTML_TYPE), ("page.html", _HTML_TYPE)),
    "screenshot": (("screenshot.webp", "image/webp"), ("screenshot.png", "image/png")),
    "content.md": (("content.md", "text/plain; charset=utf-8"),),
}
_ALLOWED_FILES["screenshot.png"] = _ALLOWED_FILES["screenshot"]

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


@app.post("/lang")
def set_language(lang: str = Form(...), next_path: str = Form("/", alias="next")):
    """표시 언어 선택 — 쿠키에 저장하고 보던 화면으로 복귀."""
    if lang not in i18n.SUPPORTED_LOCALES:
        raise HTTPException(400, f"unsupported language: {lang!r}")
    res = RedirectResponse(auth_routes.safe_next(next_path), status_code=303)
    res.set_cookie(
        i18n.LANG_COOKIE, lang, max_age=i18n.LANG_COOKIE_MAX_AGE, samesite="lax",
    )
    return res


@app.get("/archives", response_class=HTMLResponse)
def index(request: Request, queued: str = "", error: str = "", notice: str = ""):
    active = _active_snapshot()
    with db.connect() as conn:
        pages = db.list_pages(conn)
        schedules = db.list_schedules(conn)
    schedule_labels = {
        s["page_id"]: i18n.interval_label(request, s["interval_seconds"])
        for s in schedules
    }
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
            "pages": pages, "queued": queued, "error": error, "notice": notice,
            "active_urls": set(active), "active_list": sorted(active),
            "pending_new": pending_new, "schedule_labels": schedule_labels,
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


@app.get("/", response_class=HTMLResponse)
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    """시스템 현황 (첫 화면) — 아카이브 수, 기간별 용량 트렌드, 최근 스냅샷·로그."""
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


# 대시보드 주기 선택지 (초 단위 — 1시간 ~ 1주일)
_SCHEDULE_OPTIONS = [
    (3600, "1시간"), (3 * 3600, "3시간"), (6 * 3600, "6시간"), (12 * 3600, "12시간"),
    (86400, "1일"), (3 * 86400, "3일"), (7 * 86400, "1주일"),
]


@app.get("/page/{page_id}", response_class=HTMLResponse)
def timeline(
    request: Request, page_id: int, queued: int = 0, notice: str = "", error: str = ""
):
    with db.connect() as conn:
        page = db.get_page_by_id(conn, page_id)
        if page is None:
            raise HTTPException(404, t(request, "페이지 없음"))
        snaps = db.list_snapshots(conn, page_id)
        checks = db.list_checks(conn, page_id)
        schedule = db.get_schedule(conn, page_id)

    items = []
    for i, s in enumerate(snaps, 1):
        badge = "new" if i == 1 else _BADGES[s["changed"]]
        items.append({"idx": i, "snap": s, "badge": badge})
    items.reverse()  # 최신 먼저
    return templates.TemplateResponse(
        request, "timeline.html",
        {
            "page": page, "items": items, "checks": checks, "queued": queued,
            "notice": notice, "error": error,
            "schedule": schedule,
            "schedule_label": (
                i18n.interval_label(request, schedule["interval_seconds"])
                if schedule else ""
            ),
            "interval_options": _SCHEDULE_OPTIONS,
        },
    )


def _schedule_redirect(page_id: int, next_path: str) -> str:
    """스케줄 변경 후 돌아갈 경로. 열린 리다이렉트 방지 — 알려진 경로만 허용."""
    return "/schedules" if next_path == "/schedules" else f"/page/{page_id}"


@app.post("/page/{page_id}/schedule")
def schedule_set(
    request: Request,
    page_id: int,
    interval: int = Form(...),
    next_path: str = Form("", alias="next"),
):
    """페이지 반복 주기 등록/변경. 주기는 1시간 ~ 1주일(초 단위)."""
    _require_archiver(request)
    with db.connect() as conn:
        page = db.get_page_by_id(conn, page_id)
    if page is None:
        raise HTTPException(404, t(request, "페이지 없음"))
    try:
        scheduler.set_schedule(page["url"], interval)
    except ValueError as e:
        raise HTTPException(400, t(request, str(e)))
    return RedirectResponse(_schedule_redirect(page_id, next_path), status_code=303)


@app.post("/page/{page_id}/schedule/delete")
def schedule_delete(
    request: Request, page_id: int, next_path: str = Form("", alias="next")
):
    """페이지 반복 주기 해제."""
    _require_archiver(request)
    with db.connect() as conn:
        page = db.get_page_by_id(conn, page_id)
    if page is None:
        raise HTTPException(404, t(request, "페이지 없음"))
    scheduler.remove_schedule(page["url"])
    return RedirectResponse(_schedule_redirect(page_id, next_path), status_code=303)


@app.get("/schedules", response_class=HTMLResponse)
def schedules_view(request: Request):
    """자동 재아카이빙 목록 — 등록된 스케줄 현황과 주기 변경·해제 관리."""
    with db.connect() as conn:
        rows = db.list_schedules(conn)
    items = [
        {"row": s, "label": i18n.interval_label(request, s["interval_seconds"])}
        for s in rows
    ]
    return templates.TemplateResponse(
        request, "schedules.html",
        {"items": items, "interval_options": _SCHEDULE_OPTIONS},
    )


def _load_snapshot(request: Request, snapshot_id: int):
    with db.connect() as conn:
        snap = db.get_snapshot(conn, snapshot_id)
    if snap is None:
        raise HTTPException(404, t(request, "스냅샷 없음", ctx="one"))
    return snap


def _snapshot_dir(snap) -> Path:
    return storage.page_dir(snap["domain"], snap["slug"]) / snap["dir_name"]


@app.get("/snapshot/{snapshot_id}", response_class=HTMLResponse)
def snapshot_view(request: Request, snapshot_id: int):
    snap = _load_snapshot(request, snapshot_id)
    title = None
    documents: list[dict] = []
    try:
        meta = storage.read_meta(_snapshot_dir(snap))
        title = meta.title
        documents = meta.documents or []
    except OSError:
        pass
    return templates.TemplateResponse(
        request, "snapshot.html",
        {
            "snap": snap,
            "title": title,
            "documents": documents,
            "page_html_url": f"/snapshot/{snapshot_id}/file/page.html",
            "screenshot_url": f"/snapshot/{snapshot_id}/file/screenshot",
            "content_url": f"/snapshot/{snapshot_id}/file/content.md",
        },
    )


@app.get("/snapshot/{snapshot_id}/file/{name}")
def snapshot_file(request: Request, snapshot_id: int, name: str):
    candidates = _ALLOWED_FILES.get(name)
    if candidates is None:
        raise HTTPException(404, t(request, "허용되지 않은 파일"))
    snap = _load_snapshot(request, snapshot_id)
    snap_dir = _snapshot_dir(snap)
    for filename, media_type in candidates:
        path = snap_dir / filename
        if path.is_file():
            break
    else:
        raise HTTPException(404, t(request, "파일 없음"))
    headers = {}
    if filename.endswith(".gz"):
        headers["Content-Encoding"] = "gzip"
    if name == "page.html":
        # 직접 열어도 아카이빙된 JS가 실행되지 않도록 문서 자체를 샌드박스
        headers["Content-Security-Policy"] = "sandbox"
    return FileResponse(path, media_type=media_type, headers=headers)


@app.get("/snapshot/{snapshot_id}/doc/{name}")
def snapshot_document(request: Request, snapshot_id: int, name: str):
    """함께 저장된 문서 파일 서빙 — meta.json 의 documents 목록에 있는
    이름만 허용한다 (목록의 이름은 documents.py 가 정제해 생성한 값).

    /resource/ 와 달리 인증 게이트를 그대로 거치며, 브라우저 안에서
    렌더링되지 않도록 항상 첨부파일 다운로드로 내려준다.
    """
    snap = _load_snapshot(request, snapshot_id)
    snap_dir = _snapshot_dir(snap)
    try:
        meta = storage.read_meta(snap_dir)
    except OSError:
        raise HTTPException(404, t(request, "메타데이터 없음"))
    if not any(d.get("file") == name for d in meta.documents or []):
        raise HTTPException(404, t(request, "허용되지 않은 파일"))
    path = snap_dir / "files" / name
    if not path.is_file():
        raise HTTPException(404, t(request, "파일 없음"))
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=name,  # Content-Disposition: attachment
        headers={"Content-Security-Policy": "sandbox"},
    )


@app.get("/resource/{name}")
def resource_file(request: Request, name: str):
    """page.html 이 참조하는 스냅샷 간 공유 자원(CAS) 서빙.

    인증 게이트 예외 경로 (auth_gate 참조). 콘텐츠 주소라 불변이므로
    영구 캐시를 허용하고, SVG 등에서 스크립트가 실행되지 않도록
    문서 컨텍스트를 샌드박스한다.
    """
    if not resources.is_valid_name(name):
        raise HTTPException(404, t(request, "잘못된 자원 이름"))
    path = resources.resource_path(name)
    if not path.is_file():
        raise HTTPException(404, t(request, "자원 없음"))
    return FileResponse(
        path,
        media_type=resources.EXT_MEDIA_TYPES[Path(name).suffix],
        headers={
            "Content-Security-Policy": "sandbox",
            "Cache-Control": "public, max-age=31536000, immutable",
        },
    )


def _collapse_equal(
    request: Request, rows: list[tuple[str, str, str]], context: int = 3
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
            out.append(("skip", t(request, "{n}줄 동일", n=len(run) - head - tail), ""))
            if tail:
                out.extend(run[-tail:])
        else:
            out.extend(run)
        i = j
    return out


def _resolve_diff_pair(
    request: Request, page_id: int, from_idx: int | None, to_idx: int | None
):
    """diff 대상 페이지/스냅샷 쌍을 검증해 반환."""
    with db.connect() as conn:
        page = db.get_page_by_id(conn, page_id)
        if page is None:
            raise HTTPException(404, t(request, "페이지 없음"))
        snaps = db.list_snapshots(conn, page_id)
    if len(snaps) < 2:
        raise HTTPException(
            400, t(request, "비교하려면 스냅샷이 2개 이상 필요합니다 (현재 {n}개)", n=len(snaps))
        )

    if to_idx is None:
        to_idx = len(snaps)
    if from_idx is None:
        from_idx = to_idx - 1
    if not (1 <= from_idx < to_idx <= len(snaps)):
        raise HTTPException(
            400,
            t(request, "잘못된 범위: from={f} to={t} (1 ~ {n})",
              f=from_idx, t=to_idx, n=len(snaps)),
        )
    return page, snaps, from_idx, to_idx, snaps[from_idx - 1], snaps[to_idx - 1]


def _screenshot_paths(page, old_snap, new_snap) -> tuple[Path | None, Path | None]:
    base = storage.page_dir(page["domain"], page["slug"])
    return (
        storage.find_screenshot(base / old_snap["dir_name"]),
        storage.find_screenshot(base / new_snap["dir_name"]),
    )


@app.get("/diff/{page_id}", response_class=HTMLResponse)
def diff_view(
    request: Request,
    page_id: int,
    from_idx: int | None = Query(None, alias="from"),
    to_idx: int | None = Query(None, alias="to"),
):
    page, snaps, from_idx, to_idx, old_snap, new_snap = _resolve_diff_pair(
        request, page_id, from_idx, to_idx
    )
    texts = []
    for snap in (old_snap, new_snap):
        path = storage.page_dir(page["domain"], page["slug"]) / snap["dir_name"] / "content.md"
        if not path.is_file():
            raise HTTPException(404, t(request, "content.md 없음: {d}", d=snap["dir_name"]))
        texts.append(path.read_text(encoding="utf-8"))

    d = differ.diff_text(texts[0], texts[1])

    shot_ratio = None
    old_shot_path, new_shot_path = _screenshot_paths(page, old_snap, new_snap)
    if old_shot_path is not None and new_shot_path is not None:
        shot_ratio, _ = differ.cached_screenshot_diff(
            old_shot_path, new_shot_path,
            f"shotdiff-{old_snap['id']}-{new_snap['id']}",
        )

    return templates.TemplateResponse(
        request, "diff.html",
        {
            "page": page,
            "d": d,
            "rows": _collapse_equal(request, d.rows),
            "from_idx": from_idx, "to_idx": to_idx, "total": len(snaps),
            "old_snap": old_snap, "new_snap": new_snap,
            "old_shot": f"/snapshot/{old_snap['id']}/file/screenshot",
            "new_shot": f"/snapshot/{new_snap['id']}/file/screenshot",
            "shot_ratio": shot_ratio,
            "shotdiff_url": f"/diff/{page_id}/shotdiff?from={from_idx}&to={to_idx}",
        },
    )


@app.get("/diff/{page_id}/shotdiff")
def shotdiff(
    request: Request,
    page_id: int,
    from_idx: int | None = Query(None, alias="from"),
    to_idx: int | None = Query(None, alias="to"),
):
    """픽셀 diff 하이라이트 이미지 (캐시에서 서빙)."""
    page, _snaps, _f, _t, old_snap, new_snap = _resolve_diff_pair(
        request, page_id, from_idx, to_idx
    )
    old_shot_path, new_shot_path = _screenshot_paths(page, old_snap, new_snap)
    if old_shot_path is None or new_shot_path is None:
        raise HTTPException(404, t(request, "스크린샷 없음"))
    _ratio, out_png = differ.cached_screenshot_diff(
        old_shot_path, new_shot_path, f"shotdiff-{old_snap['id']}-{new_snap['id']}"
    )
    return FileResponse(out_png, media_type="image/png")


_LOG_STATUSES = ("new", "changed", "unchanged", "forced_same", "error")


def _clean_date(value: str | None) -> str | None:
    """날짜 입력을 YYYY-MM-DD 로 정규화, 파싱 불가면 None (필터 무시)."""
    if not value:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


@app.get("/logs", response_class=HTMLResponse)
def logs_view(
    request: Request,
    domain: str | None = None,
    page_id: int | None = None,
    snapshot_id: int | None = None,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    page: int = 1,
    limit: int = 100,
):
    """아카이브 실행 로그. 도메인/페이지/스냅샷/상태/기간 필터 + 페이징."""
    limit = max(1, min(limit, 500))
    if status not in _LOG_STATUSES:
        status = None
    date_from = _clean_date(date_from)
    date_to = _clean_date(date_to)
    if date_from and date_to and date_from > date_to:
        date_from, date_to = date_to, date_from
    filters = {
        "domain": domain or None, "page_id": page_id,
        "snapshot_id": snapshot_id, "status": status,
        "date_from": date_from, "date_to": date_to,
    }
    with db.connect() as conn:
        total = db.count_archive_logs(conn, **filters)
        total_pages = max(1, -(-total // limit))  # ceil
        page = max(1, min(page, total_pages))
        logs = db.list_archive_logs(
            conn, **filters, limit=limit, offset=(page - 1) * limit,
        )
        domains = db.list_log_domains(conn)
        filter_page = db.get_page_by_id(conn, page_id) if page_id else None

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
    # 페이징 링크 — 현재 필터를 유지한 채 page 만 바꾼다
    qs_base = [
        (k, v) for k, v in (
            ("domain", domain or None), ("page_id", page_id),
            ("snapshot_id", snapshot_id), ("status", status),
            ("date_from", date_from), ("date_to", date_to),
        ) if v is not None
    ]
    if limit != 100:
        qs_base.append(("limit", limit))

    def _page_url(n: int) -> str:
        params = qs_base + ([("page", n)] if n > 1 else [])
        return "/logs" + ("?" + urlencode(params) if params else "")

    return templates.TemplateResponse(
        request, "logs.html",
        {
            "items": items, "domains": domains, "filter_page": filter_page,
            "domain": domain or "", "status": status or "",
            "date_from": date_from or "", "date_to": date_to or "",
            "snapshot_id": snapshot_id, "limit": limit,
            "statuses": _LOG_STATUSES,
            "total": total, "total_pages": total_pages, "page_num": page,
            "prev_url": _page_url(page - 1) if page > 1 else None,
            "next_url": _page_url(page + 1) if page < total_pages else None,
        },
    )


def _run_archive(
    url: str,
    force: bool = False,
    interval_seconds: int | None = None,
    source: str = "web",
) -> None:
    """백그라운드 아카이빙. 결과는 archive_logs 에 기록된다 (source 포함).

    interval_seconds 가 있으면 실행 후 자동 재아카이빙 주기를 등록한다 —
    신규 URL 은 아카이빙이 끝나야 pages 행이 생기므로 등록을 여기서 한다.
    """
    try:
        outcome = pipeline.archive_url(url, force=force, source=source)
        logger.info("아카이빙 완료: %s [%s]", url, outcome.status)
    except Exception:
        logger.exception("아카이빙 실패: %s", url)
    finally:
        if interval_seconds:
            try:
                scheduler.set_schedule(url, interval_seconds)
            except ValueError as e:
                # 아카이빙 실패로 pages 행이 안 생겼으면 주기 등록도 불가
                logger.warning("자동 재아카이빙 등록 실패: %s — %s", url, e)
        _unregister_job(url)


def _queue_archive(
    background: BackgroundTasks,
    url: str,
    force: bool = False,
    interval_seconds: int | None = None,
    source: str = "web",
) -> bool:
    """진행 목록 등록 후 백그라운드 작업 추가. 이미 진행 중이면 무시(False).

    등록은 응답 전(동기)에 해서 리다이렉트된 목록 화면이 바로 진행 상태를 본다.
    """
    if not _register_job(url):
        return False
    background.add_task(_run_archive, url, force, interval_seconds, source)
    return True


def _require_archiver(request: Request) -> None:
    """아카이빙 권한 가드 (admin/archiver). 보기 전용·차단 계정은 403."""
    if not permissions.can_archive(request.state.user):
        raise HTTPException(403, t(request, "아카이빙 권한이 없습니다"))


def _require_deleter(request: Request) -> None:
    """삭제 권한 가드 (admin/archiver). 보기 전용·차단 계정은 403."""
    if not permissions.can_delete(request.state.user):
        raise HTTPException(403, t(request, "삭제 권한이 없습니다"))


@app.get("/archive/new", response_class=HTMLResponse)
def archive_new_form(request: Request, error: str = "", url: str = ""):
    """새 아카이빙 등록 화면 — URL 입력 + 자동 재아카이빙 주기 선택."""
    _require_archiver(request)
    return templates.TemplateResponse(
        request, "archive_new.html",
        {"error": error, "url": url, "interval_options": _SCHEDULE_OPTIONS},
    )


@app.post("/archive")
def archive_new(
    request: Request,
    background: BackgroundTasks,
    url: str = Form(...),
    interval: int = Form(0),
):
    """새 URL 아카이빙. 검증은 동기로, 캡처·주기 등록(interval>0)은 백그라운드로."""
    _require_archiver(request)
    try:
        norm = storage.normalize_url(url)
        if interval:
            scheduler.validate_interval(interval)
    except ValueError as exc:
        params = urlencode(
            {"error": t(request, "아카이빙 실패: {e}", e=exc), "url": url}
        )
        return RedirectResponse(f"/archive/new?{params}", status_code=303)
    _queue_archive(background, norm, interval_seconds=interval or None)
    return RedirectResponse(f"/archives?queued={quote(norm, safe='')}", status_code=303)


@app.post("/page/{page_id}/rearchive")
def rearchive(
    request: Request,
    page_id: int,
    background: BackgroundTasks,
    force: bool = Form(False),
):
    _require_archiver(request)
    with db.connect() as conn:
        page = db.get_page_by_id(conn, page_id)
    if page is None:
        raise HTTPException(404, t(request, "페이지 없음"))
    _queue_archive(background, page["url"], force=force)
    return RedirectResponse(url=f"/page/{page_id}?queued=1", status_code=303)


_BUSY_MSG = "아카이빙이 진행 중인 페이지입니다 — 완료 후 다시 시도하세요"


@app.post("/page/{page_id}/delete")
def page_delete(request: Request, page_id: int):
    """페이지 전체 삭제 (모든 스냅샷·확인 기록·스케줄). admin/archiver 전용."""
    _require_deleter(request)
    with db.connect() as conn:
        page = db.get_page_by_id(conn, page_id)
    if page is None:
        raise HTTPException(404, t(request, "페이지 없음"))
    # 진행 중인 아카이빙과 경합하면 삭제 직후 스냅샷이 다시 생긴다 — 거부
    if page["url"] in _active_snapshot():
        return RedirectResponse(
            f"/archives?error={quote(t(request, _BUSY_MSG), safe='')}", status_code=303
        )
    result = deletion.delete_page(page_id)
    msg = t(request, "삭제됨: {url} (스냅샷 {n}개)",
            url=result.url, n=result.snapshots_deleted)
    return RedirectResponse(
        f"/archives?notice={quote(msg, safe='')}", status_code=303
    )


@app.post("/snapshot/{snapshot_id}/delete")
def snapshot_delete(request: Request, snapshot_id: int):
    """단일 스냅샷 삭제. admin/archiver 전용.

    다음 스냅샷의 changed 재계산(신/구 비교 보정)은 deletion → db 계층이 한다.
    """
    _require_deleter(request)
    snap = _load_snapshot(request, snapshot_id)
    # 진행 중이면 파이프라인이 '직전 스냅샷'을 읽는 중일 수 있다 — 거부
    if snap["page_url"] in _active_snapshot():
        return RedirectResponse(
            f"/page/{snap['page_id']}?error={quote(t(request, _BUSY_MSG), safe='')}",
            status_code=303,
        )
    deletion.delete_snapshot(snapshot_id)
    msg = t(request, "스냅샷 삭제됨: {t}", t=snap["taken_at"])
    return RedirectResponse(
        f"/page/{snap['page_id']}?notice={quote(msg, safe='')}", status_code=303
    )
