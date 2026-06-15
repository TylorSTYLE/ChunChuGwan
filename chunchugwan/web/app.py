"""읽기 전용 대시보드 + 재아카이빙 트리거.

보안 원칙 (CLAUDE.md 5번):
- 바인딩은 127.0.0.1 고정
- 스냅샷 HTML 렌더링은 templates/snapshot.html 의 샌드박스 iframe 안에서만.
  허용 토큰은 allow-top-navigation-by-user-activation 하나 — 사용자가
  링크를 직접 클릭했을 때만 뷰어 전체의 이동을 허용한다 (사이트 전체
  아카이브의 재작성된 링크가 다음 스냅샷으로 가는 통로). 스크립트/폼/팝업은
  여전히 차단 — allow-scripts 절대 추가 금지.
- page.html 직접 응답에도 같은 CSP `sandbox` 헤더를 붙여 직접 열어도
  대시보드 컨텍스트에서 스크립트가 실행되지 않게 한다
- 스냅샷 파일 서빙 시 경로는 DB에 기록된 dir_name 으로만 조립.
  사용자 입력 경로를 직접 파일시스템에 매핑하지 말 것.
"""

from __future__ import annotations

import io
import json
import logging
import threading
import zipfile
from contextlib import asynccontextmanager
import zoneinfo
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

from fastapi import (
    FastAPI, File, Form, HTTPException, Query, Request, UploadFile,
)
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)

from .. import (
    __version__, archive_worker, auth, backup, config, crawler, credentials, crypto,
    db, deletion, differ, documents, live_challenge, netcheck, resources, scheduler,
    searchindex, storage, system_log,
)
from . import api_routes, audit, auth_routes, i18n, permissions, system_routes
from .i18n import t
from .templating import templates

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """서버 구동 동안 스케줄러·크롤러·단발 아카이빙 폴링 스레드 운영
    (WCCG_SCHEDULER=off 면 비활성 — 그 경우 `wccg worker` 가 큐를 소비한다).

    진행 중 작업 레지스트리(claim/release)를 같이 써서 같은 URL 이 동시에
    돌지 않게 한다. 크롤러는 사이트 전체 아카이브 큐(crawl_pages)를, 아카이빙
    워커는 단발 아카이빙 큐(archive_jobs — 새/재아카이빙·API·CLI)를 소비한다.
    """
    # 시스템 로그 DB 적재 — `wccg serve` 가 이미 설치했으면 중복 설치 무시.
    # uvicorn 으로 직접 띄우는 경우를 위해 여기서도 보장한다.
    system_log.install("serve")
    stop = threading.Event()
    threads: list[threading.Thread] = []
    if config.SCHEDULER_ENABLED:
        threads = [
            threading.Thread(
                target=scheduler.run_loop,
                args=(stop,),
                kwargs={
                    "poll_seconds": config.SCHEDULER_POLL_SECONDS,
                    "claim": _register_job,
                    "release": _unregister_job,
                },
                name="wccg-scheduler",
                daemon=True,
            ),
            threading.Thread(
                target=crawler.run_loop,
                args=(stop,),
                kwargs={"claim": _register_job, "release": _unregister_job},
                name="wccg-crawler",
                daemon=True,
            ),
            threading.Thread(
                target=archive_worker.run_loop,
                args=(stop,),
                kwargs={"claim": _register_job, "release": _unregister_job},
                name="wccg-archive",
                daemon=True,
            ),
        ]
        for thread in threads:
            thread.start()
    yield
    stop.set()
    for thread in threads:
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
    "/healthz", "/login", "/login/totp", "/signup",
    "/login/passkey/options", "/login/passkey",
}

# 브라우저가 주소만 보고 자동 요청하는 아이콘 경로 — /login·/setup 으로
# 리다이렉트하면 로그만 오염되므로 그대로 통과시킨다. /favicon.svg 만 실제
# 라우트가 있고(아래 favicon()), 나머지는 라우트가 없어 404 가 정답이다.
_BROWSER_ICON_PATHS = {
    "/favicon.ico", "/favicon.svg",
    "/apple-touch-icon.png", "/apple-touch-icon-precomposed.png",
}

# 승인 대기(pending — 권한없음) 계정에게 허용하는 경로. 그 외는 전부
# /pending 안내 페이지로 보낸다 — 어떤 서비스 기능도 쓸 수 없어야 한다.
_PENDING_ALLOWED_PATHS = {
    "/pending", "/logout", "/healthz",
} | _BROWSER_ICON_PATHS


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

    # /api/ POST 는 Bearer/X-API-Key 토큰이 자격증명이라 쿠키 기반 CSRF 가
    # 성립하지 않는다 (확장 background fetch 는 Origin 이 없거나 chrome-extension://).
    # 키 검증·권한 가드는 그대로 강제되므로 Origin 검사만 면제한다.
    if request.method == "POST" and not request.url.path.startswith("/api/"):
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

        # 로그인 사용자는 DB에 저장된 언어 설정을 우선 적용
        if request.state.user is not None:
            stored = request.state.user["locale"]
            if stored in i18n.SUPPORTED_LOCALES:
                request.state.locale = stored

        if first_run:
            if path.startswith("/api/"):
                # API 클라이언트에게 /setup 리다이렉트는 의미가 없다
                return PlainTextResponse(
                    "최초 설정이 완료되지 않았습니다", status_code=401
                )
            if path not in ("/setup", "/healthz") and path not in _BROWSER_ICON_PATHS:
                return RedirectResponse("/setup", status_code=302)
        else:
            # 차단·탈퇴된 계정 — 로그아웃 외 모든 접근 거부 (세션은 차단/탈퇴
            # 시점에 삭제되지만, 그 사이 발급된 세션이 있어도 여기서 막힌다)
            if (
                request.state.user is not None
                and request.state.user["role"] in ("blocked", "withdrawn")
                and path != "/logout"
            ):
                message = (
                    "차단된 계정입니다. 관리자에게 문의하세요."
                    if request.state.user["role"] == "blocked"
                    else "탈퇴한 계정입니다."
                )
                return PlainTextResponse(t(request, message), status_code=403)
            # 승인 대기(권한없음) 계정 — 안내 페이지·로그아웃·언어 전환만 허용
            if (
                request.state.user is not None
                and request.state.user["role"] == "pending"
                and path not in _PENDING_ALLOWED_PATHS
            ):
                return RedirectResponse("/pending", status_code=302)
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

# 진행 중인 크롤·스케줄 작업 레지스트리 — 정규화 URL → 시작 시각(ISO 8601 UTC).
# 같은 프로세스의 크롤러·스케줄러 스레드가 같은 URL 을 동시에 아카이빙하지
# 않게 막는 claim 콜백용(단발 아카이빙 큐 소비자도 같은 레지스트리를 받는다).
# 단발 아카이빙의 진행 상태 자체는 이제 archive_jobs 큐(DB)가 보존한다.
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
    """진행 중 작업의 사본 (렌더링/폴링용) — 단발 아카이빙 큐(pending+in_progress)와
    인메모리 크롤·스케줄 진행을 합친 url→시각 매핑."""
    with db.connect() as conn:
        snap = {row["url"]: row["activity_at"] for row in db.list_active_archive_jobs(conn)}
    with _active_lock:
        snap.update(_active_jobs)
    return snap


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


_FAVICON_PATH = Path(__file__).parent / "static" / "favicon.svg"


@app.get("/favicon.svg", include_in_schema=False)
def favicon() -> FileResponse:
    """SVG 파비콘 (OS 라이트/다크 자동) — 인증 없이 서빙 (_BROWSER_ICON_PATHS)."""
    return FileResponse(_FAVICON_PATH, media_type="image/svg+xml")


# 크롬 확장(MV3) 소스 — 패키지에 함께 실린다 (Docker·휠 모두 chunchugwan 포함)
_EXTENSION_DIR = Path(__file__).resolve().parent.parent / "extension"


def _build_extension_zip() -> bytes:
    """chunchugwan/extension/ 를 요청 시 zip 으로 묶는다.

    manifest 의 version 은 서버 버전으로 맞춰 항상 최신을 서빙한다 (깃에
    빌드 산출물·바이너리를 두지 않는다). 크롬은 자체호스팅 .crx 드래그 설치를
    막으므로 unpacked ZIP 으로 주고, 사용자는 압축 해제 후 '개발자 모드 →
    압축해제된 확장 프로그램을 로드' 로 설치한다.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(_EXTENSION_DIR.rglob("*")):
            if not path.is_file():
                continue
            arc = path.relative_to(_EXTENSION_DIR).as_posix()
            if arc == "manifest.json":
                data = json.loads(path.read_text(encoding="utf-8"))
                data["version"] = __version__
                zf.writestr(arc, json.dumps(data, ensure_ascii=False, indent=2))
            else:
                zf.write(path, arc)
    return buf.getvalue()


@app.get("/extension/download")
def extension_download(request: Request) -> Response:
    """크롬 확장(unpacked) ZIP 다운로드 — 세션 인증. 압축 해제 후 개발자 모드 로드."""
    if not _EXTENSION_DIR.is_dir():
        raise HTTPException(404, t(request, "확장 파일을 찾을 수 없습니다"))
    filename = f"wccg-chrome-extension-v{__version__}.zip"
    return Response(
        content=_build_extension_zip(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _snapshot_dir_size(domain: str, slug: str, dir_name: str) -> int:
    """스냅샷 디렉토리의 파일 용량 합 (바이트). 디렉토리가 없으면 0."""
    snap_dir = storage.page_dir(domain, slug) / dir_name
    return sum(f["bytes"] for f in storage.snapshot_files(snap_dir))


def _snapshot_title(domain: str, slug: str, dir_name: str) -> str | None:
    """스냅샷 meta.json 의 title (없거나 읽기 실패 시 None)."""
    path = storage.page_dir(domain, slug) / dir_name / "meta.json"
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("title") or None
    except (OSError, ValueError):
        return None


# 사이트 타이틀 탐색 시 거슬러 올라가는 최신 스냅샷 수 한도
_TITLE_LOOKBACK = 5


def _site_title(snap_rows) -> str | None:
    """사이트 스냅샷 중 최신 것부터 meta.json title 을 찾는다 (현재 타이틀).

    오류 페이지 캡처 등 title 없는 스냅샷이 끼어도 직전 제목으로 폴백하되,
    파일 IO 를 한정하기 위해 _TITLE_LOOKBACK 개까지만 본다.
    """
    recent = sorted(snap_rows, key=lambda r: r["taken_at"], reverse=True)
    for row in recent[:_TITLE_LOOKBACK]:
        title = _snapshot_title(row["domain"], row["slug"], row["dir_name"])
        if title:
            return title
    return None


@app.get("/archives", response_class=HTMLResponse)
def index(request: Request, queued: str = "", error: str = "", notice: str = ""):
    """아카이브 목록 — 사이트(서브도메인) 단위 한 테이블.

    단일 페이지 아카이브든 사이트 전체 아카이브(크롤)든 같은 서브도메인이면
    한 행이다. 진행 중인 사이트(아카이빙 중 페이지·진행 중 크롤 보유)를 맨
    위에, 나머지는 마지막 활동 시각 내림차순. 행을 누르면 사이트 상세로 간다.
    """
    active = _active_snapshot()
    with db.connect() as conn:
        sites = db.list_sites_overview(conn, viewer=_snapshot_viewer(request))
        running = [c for c in db.list_crawls(conn) if c["status"] == "running"]
        snap_dirs = db.list_snapshot_dirs(conn)
        tag_rows = db.list_site_network_tags(conn)
    # 사이트별 로컬 네트워크 태그 — 같은 IP 대역의 다른 사설 네트워크 구분용
    site_tags: dict[int, list[dict]] = {}
    for row in tag_rows:
        site_tags.setdefault(row["site_id"], []).append(
            {"id": row["id"], "name": row["name"],
             "description": row["description"]}
        )
    # 사이트별 저장 용량 — 스냅샷은 불변이므로 디렉토리 용량을 그대로 합산
    site_bytes: dict[int, int] = {}
    site_snaps: dict[int, list] = {}
    for row in snap_dirs:
        site_bytes[row["site_id"]] = site_bytes.get(row["site_id"], 0) + (
            _snapshot_dir_size(row["domain"], row["slug"], row["dir_name"])
        )
        site_snaps.setdefault(row["site_id"], []).append(row)
    titles = {sid: _site_title(rows) for sid, rows in site_snaps.items()}
    active_keys = {storage.site_key(u) for u in active}
    items: list[dict] = [
        {
            "site_id": s["id"], "site_key": s["site_key"],
            "page_count": s["page_count"], "snapshot_count": s["snapshot_count"],
            "crawl_count": s["crawl_count"], "schedule_count": s["schedule_count"],
            "bytes": site_bytes.get(s["id"], 0),
            "title": titles.get(s["id"]),
            "network_tags": site_tags.get(s["id"], []),
            "activity_at": s["last_activity_at"] or None,
            "crawling": s["running_crawl_count"] > 0,
            "active": s["site_key"] in active_keys or s["running_crawl_count"] > 0,
        }
        for s in sites
    ]
    # 아직 pages 행이 없는 신규 URL 진행 건 — 사이트 행이 없으면 임시 행으로
    known_keys = {s["site_key"] for s in sites}
    items += [
        {
            "site_id": None, "site_key": key,
            "page_count": 0, "snapshot_count": 0, "crawl_count": 0,
            "schedule_count": 0, "bytes": 0, "title": None,
            "network_tags": [],
            "activity_at": t, "crawling": False,
            "active": True,
        }
        for u, t in sorted(active.items())
        if (key := storage.site_key(u)) not in known_keys
    ]
    items.sort(key=lambda i: i["site_key"])
    items.sort(key=lambda i: i["activity_at"] or "", reverse=True)
    items.sort(key=lambda i: not i["active"])
    # 진행 중 크롤 폴링용 — 카운트가 바뀌면 화면을 새로 그린다
    running_crawls = [
        {"id": c["id"],
         "counts": {"done": c["done_count"], "failed": c["failed_count"],
                    "waiting": c["pending_count"]}}
        for c in running
    ]
    return templates.TemplateResponse(
        request, "index.html",
        {
            "items": items, "queued": queued, "error": error, "notice": notice,
            "active_list": sorted(active), "running_crawls": running_crawls,
        },
    )


# 사이트 상세의 페이지 목록 페이징 단위 — 선택 가능한 표시 개수와 기본값
_SITE_PAGES_PER_PAGE_CHOICES = (25, 50, 75, 100, 200)
_SITE_PAGES_PER_PAGE = 25
# 사이트 상세에 함께 보여줄 문서 목록 상한 — 넘으면 전체 문서 화면으로 안내
_SITE_DOCUMENTS_CAP = 50


@app.get("/sites/{site_id}", response_class=HTMLResponse)
def site_view(
    request: Request,
    site_id: int,
    error: str = "",
    notice: str = "",
    page: int = Query(1, ge=1),
    per_page: int = Query(0),
):
    """사이트 상세 — 소속 페이지 목록(페이징) + 크롤 회차 목록 + 스케줄.

    사이트는 서브도메인 단위 그릇이고, 크롤 회차는 그 사이트를 특정 시점에
    돌았던 실행 기록이다 — 회차 상세(/crawls/{id})는 그대로 유지된다.
    """
    active = _active_snapshot()
    if per_page not in _SITE_PAGES_PER_PAGE_CHOICES:
        per_page = _SITE_PAGES_PER_PAGE
    with db.connect() as conn:
        site = db.get_site(conn, site_id)
        if site is None:
            raise HTTPException(404, t(request, "사이트 없음"))
        totals = db.site_page_totals(conn, site_id)
        total_pages = max(1, -(-totals["page_count"] // per_page))  # ceil
        page = min(page, total_pages)
        pages = db.list_site_pages(
            conn, site_id,
            limit=per_page, offset=(page - 1) * per_page,
        )
        snap_dirs = db.list_site_snapshot_dirs(conn, site_id)
        crawls = db.list_site_crawls(conn, site_id)
        schedules = db.list_site_schedules(conn, site_id)
        crawl_schedules = db.list_site_crawl_schedules(conn, site_id)
        certificates = db.list_site_certificates(conn, site_id)
        site_network_tags = db.list_site_network_tags(conn, site_id)
        failed_logs = db.list_site_failed_logs(conn, site_id)
        # 크롤 실패는 페이지 행이 없는 신규 URL 까지 포함 — 실패한 작업
        # 목록(archive_logs 기반)에 이미 있는 URL 은 겹치지 않게 뺀다
        failed_log_urls = {f["page_url"] for f in failed_logs}
        failed_crawl_pages = [
            r for r in db.list_site_failed_crawl_pages(conn, site_id)
            if r["url"] not in failed_log_urls
        ]
        # 이 사이트가 아카이빙한 문서 파일 (sha256 그룹). 상한 + 1 개를 받아
        # 초과 여부를 판단하고, 넘으면 전체 문서 화면으로 안내한다.
        site_documents = db.list_site_document_groups(
            conn, site_id, limit=_SITE_DOCUMENTS_CAP + 1
        )
    schedule_labels = {
        s["page_id"]: i18n.interval_label(request, s["interval_seconds"])
        for s in schedules
    }
    crawl_schedule_labels = [
        {
            "start_url": s["start_url"],
            "label": i18n.interval_label(request, s["interval_seconds"]),
            "next_run_at": s["next_run_at"],
        }
        for s in crawl_schedules
    ]
    # 인증서 — 호스트별 최신 행이 "현재", 나머지는 이전 버전 (db 가 정렬)
    current_cert_ids = {}
    for c in certificates:
        current_cert_ids.setdefault(c["host"], c["id"])
    cert_rows = [
        {
            "cert": c,
            "san": json.loads(c["san"] or "[]"),
            "is_current": current_cert_ids[c["host"]] == c["id"],
        }
        for c in certificates
    ]
    # 페이지별·사이트 전체 저장 용량 — 스냅샷 디렉토리 용량 합산
    page_bytes: dict[int, int] = {}
    for row in snap_dirs:
        page_bytes[row["page_id"]] = page_bytes.get(row["page_id"], 0) + (
            _snapshot_dir_size(row["domain"], row["slug"], row["dir_name"])
        )
    running_crawls = [
        {"id": c["id"],
         "counts": {"done": c["done_count"], "failed": c["failed_count"],
                    "waiting": c["pending_count"]}}
        for c in crawls if c["status"] == "running"
    ]

    def _page_url(n: int) -> str:
        params = []
        if n > 1:
            params.append(f"page={n}")
        if per_page != _SITE_PAGES_PER_PAGE:
            params.append(f"per_page={per_page}")
        return f"/sites/{site_id}" + ("?" + "&".join(params) if params else "")

    return templates.TemplateResponse(
        request, "site.html",
        {
            "site": site, "pages": pages, "crawls": crawls,
            "site_title": _site_title(snap_dirs),
            "network_tags": site_network_tags,
            "failed_logs": failed_logs,
            "failed_crawl_pages": failed_crawl_pages,
            "site_documents": site_documents[:_SITE_DOCUMENTS_CAP],
            "site_documents_more": len(site_documents) > _SITE_DOCUMENTS_CAP,
            "schedule_labels": schedule_labels,
            "crawl_schedules": crawl_schedule_labels,
            "page_count": totals["page_count"],
            "snapshot_total": totals["snapshot_count"],
            "page_bytes": page_bytes,
            "site_bytes": sum(page_bytes.values()),
            "page_num": page, "total_pages": total_pages,
            "per_page": per_page,
            "per_page_choices": _SITE_PAGES_PER_PAGE_CHOICES,
            "prev_url": _page_url(page - 1) if page > 1 else None,
            "next_url": _page_url(page + 1) if page < total_pages else None,
            "certificates": cert_rows,
            "active": active, "running_crawls": running_crawls,
            "error": error, "notice": notice,
        },
    )


@app.post("/sites/{site_id}/failed/{log_id}/retry")
def site_failed_retry(
    request: Request, site_id: int, log_id: int
):
    """실패한 작업 재시도 — 해당 페이지를 백그라운드로 재아카이빙. admin/archiver 전용.

    성공하면 그 URL 의 최신 로그가 성공으로 바뀌어 실패 목록에서 사라진다.
    """
    _require_archiver(request)
    with db.connect() as conn:
        log = db.get_site_failed_log(conn, site_id, log_id)
    if log is None:
        raise HTTPException(404, t(request, "실패 기록 없음"))
    if _queue_archive(log["page_url"], requested_by=_requester_id(request)):
        audit.log(request, "실패 작업 재시도: %s", log["page_url"])
        params = {"notice": t(request, "아카이빙이 백그라운드에서 시작되었습니다")}
    else:
        params = {"error": t(request, _BUSY_MSG)}
    return RedirectResponse(f"/sites/{site_id}?{urlencode(params)}", status_code=303)


@app.post("/sites/{site_id}/crawl-failed/{crawl_page_id}/retry")
def site_crawl_failed_retry(request: Request, site_id: int, crawl_page_id: int):
    """실패한 크롤 페이지 재시도 — 큐로 되돌려 크롤러가 다시 집어가게 한다.

    admin/archiver 전용. 끝난(done/cancelled) 크롤이면 다시 연다.
    """
    _require_archiver(request)
    with db.connect() as conn:
        row = db.get_site_failed_crawl_page(conn, site_id, crawl_page_id)
        if row is None:
            raise HTTPException(404, t(request, "실패 기록 없음"))
        db.retry_failed_crawl_page(conn, crawl_page_id)
    audit.log(request, "크롤 페이지 재시도: %s", row["url"])
    params = {"notice": t(request, "재시도가 등록되었습니다 — 크롤러가 곧 다시 시도합니다.")}
    return RedirectResponse(f"/sites/{site_id}?{urlencode(params)}", status_code=303)


@app.get("/sites/{site_id}/certificates/{cert_id}.pem")
def site_certificate_pem(request: Request, site_id: int, cert_id: int):
    """보관된 인증서 PEM 다운로드 — 사이트 소속 행만, 항상 첨부파일로."""
    with db.connect() as conn:
        cert = db.get_site_certificate(conn, site_id, cert_id)
    if cert is None:
        raise HTTPException(404, t(request, "인증서 없음"))
    filename = f"{cert['host'].replace(':', '_')}-{cert['fingerprint'][:12]}.pem"
    return PlainTextResponse(
        cert["pem"],
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/sites/{site_id}/export")
def site_export(request: Request, site_id: int):
    """사이트 아카이브 내보내기 — 소속 페이지·스냅샷과 참조 자원만 담은 tar.gz.

    파일 형식은 전체 내보내기(/system/export)와 같아 가져오기(웹 화면·
    wccg import)로 복원할 수 있다. admin/archiver 전용.
    """
    _require_archiver(request)
    with db.connect() as conn:
        site = db.get_site(conn, site_id)
    if site is None:
        raise HTTPException(404, t(request, "사이트 없음"))
    audit.log(request, "사이트 아카이브 내보내기: %s", site["site_key"])
    return system_routes.tar_download(
        lambda dest: backup.export_archive(dest, site_id=site_id), "export"
    )


@app.post("/sites/{site_id}/delete")
def site_delete(request: Request, site_id: int):
    """사이트 전체 삭제 — 소속 페이지·크롤 회차·크롤 스케줄 일괄. admin/archiver 전용.

    소속 페이지의 아카이빙이나 크롤이 진행 중이면 거부한다 — 삭제 직후
    스냅샷이 다시 생기는 경합을 막는다.
    """
    _require_deleter(request)
    with db.connect() as conn:
        site = db.get_site(conn, site_id)
        if site is None:
            raise HTTPException(404, t(request, "사이트 없음"))
        busy = any(
            c["status"] == "running" for c in db.list_site_crawls(conn, site_id)
        )
    active_keys = {storage.site_key(u) for u in _active_snapshot()}
    if busy or site["site_key"] in active_keys:
        return RedirectResponse(
            f"/sites/{site_id}?error="
            + quote(t(request, "아카이빙·크롤이 진행 중인 사이트입니다 — 완료 후 다시 시도하세요"), safe=""),
            status_code=303,
        )
    result = deletion.delete_site(site_id)
    if result is None:
        raise HTTPException(404, t(request, "사이트 없음"))
    audit.log(
        request, "사이트 삭제: %s (페이지 %d개, 스냅샷 %d개, 크롤 %d개)",
        result.site_key, result.pages_deleted, result.snapshots_deleted,
        result.crawls_deleted,
    )
    params = urlencode({
        "notice": t(
            request,
            "사이트 삭제됨: {key} (페이지 {p}개, 스냅샷 {s}개, 크롤 {c}개)",
            key=result.site_key, p=result.pages_deleted,
            s=result.snapshots_deleted, c=result.crawls_deleted,
        )
    })
    return RedirectResponse(f"/archives?{params}", status_code=303)


def _read_har_upload(har_file: UploadFile | None) -> bytes:
    """업로드된 HAR 파일을 바이트로 읽는다 (상한 초과 시 CredentialError). 없으면 b""."""
    if har_file is None or not (har_file.filename or "").strip():
        return b""
    raw = har_file.file.read(credentials.MAX_HAR_BYTES + 1)
    if len(raw) > credentials.MAX_HAR_BYTES:
        raise credentials.CredentialError("HAR 파일이 너무 큽니다.")
    return raw


def _session_storage_state(storage_state: str, har: bytes, *, site_key: str = "") -> str:
    """세션 자격증명용 storage_state 문자열을 정한다.

    HAR 업로드가 있으면 그것을 파싱해 쿠키를 추출한 storage_state 가 우선하고,
    없으면 입력한 JSON 을 그대로 쓴다. HAR 은 대상 사이트(site_key) 도메인의
    쿠키만 남겨 무관한 서드파티 쿠키가 섞이지 않게 한다. 파싱 실패는
    CredentialError(호출부가 처리).
    """
    if har:
        host = (urlsplit(f"//{site_key}").hostname or "") if site_key else ""
        return json.dumps(
            credentials.storage_state_from_har(har, site_host=host or None),
            ensure_ascii=False,
        )
    return storage_state


def _credentials_redirect(
    site_id: int, *, notice: str = "", error: str = ""
) -> RedirectResponse:
    """자격증명 페이지로 PRG 리다이렉트."""
    params = {"error": error} if error else ({"notice": notice} if notice else {})
    suffix = f"?{urlencode(params)}" if params else ""
    return RedirectResponse(
        f"/sites/{site_id}/credentials{suffix}", status_code=303
    )


@app.get("/sites/{site_id}/credentials", response_class=HTMLResponse)
def site_credentials_view(
    request: Request, site_id: int, notice: str = "", error: str = ""
):
    """사이트 로그인 자격증명 관리 — 목록 + 등록 폼. 관리자 전용.

    비밀은 표시하지 않는다 (라벨·종류·등록 정보만). 캡처 연동은 다음 단계.
    """
    _require_admin(request)
    with db.connect() as conn:
        site = db.get_site(conn, site_id)
        if site is None:
            raise HTTPException(404, t(request, "사이트 없음"))
        rows = db.list_site_credentials(conn, site_id)
    cred_rows = [
        {**dict(c), "kind_label": credentials.kind_label(c["kind"])} for c in rows
    ]
    kinds = [
        {"value": k, "label": credentials.kind_label(k)} for k in credentials.KINDS
    ]
    return templates.TemplateResponse(
        request, "site_credentials.html",
        {
            "site": site,
            "credentials": cred_rows,
            "kinds": kinds,
            "secret_key_configured": crypto.is_configured(),
            "notice": notice,
            "error": error,
        },
    )


@app.post("/sites/{site_id}/credentials")
def site_credentials_create(
    request: Request,
    site_id: int,
    label: str = Form(""),
    kind: str = Form(""),
    username: str = Form(""),
    password: str = Form(""),
    storage_state: str = Form(""),
    token: str = Form(""),
    har_file: UploadFile | None = File(None),
):
    """로그인 자격증명 등록 — 입력 검증 → 암호화 저장 → 감사 로그. 관리자 전용.

    빈 폼 값은 인코딩에서 누락되므로 모든 필드에 기본값을 둬, 누락도 422 가
    아니라 검증 메시지(리다이렉트)로 처리한다. 세션 종류는 storage_state JSON
    대신 로그인 흐름을 기록한 HAR 파일을 올려 쿠키를 자동 추출할 수도 있다.
    """
    _require_admin(request)
    if not crypto.is_configured():
        return _credentials_redirect(
            site_id,
            error=t(request, "WCCG_SECRET_KEY 가 설정되지 않아 자격증명을 저장할 수 없습니다."),
        )
    label = label.strip()
    label_error = credentials.validate_label(label)
    if label_error is not None:
        return _credentials_redirect(site_id, error=t(request, label_error))
    if kind not in credentials.KINDS:
        return _credentials_redirect(
            site_id, error=t(request, "잘못된 자격증명 종류입니다.")
        )
    with db.connect() as conn:
        site = db.get_site(conn, site_id)
    if site is None:
        raise HTTPException(404, t(request, "사이트 없음"))
    try:
        if kind == credentials.KIND_SESSION:
            storage_state = _session_storage_state(
                storage_state, _read_har_upload(har_file), site_key=site["site_key"]
            )
        payload = credentials.build_payload(
            kind,
            {
                "username": username,
                "password": password,
                "storage_state": storage_state,
                "token": token,
            },
        )
    except credentials.CredentialError as e:
        return _credentials_redirect(site_id, error=t(request, str(e)))
    with db.connect() as conn:
        if db.get_site_credential_by_label(conn, site_id, label) is not None:
            return _credentials_redirect(
                site_id, error=t(request, "이미 있는 이름입니다: {name}", name=label)
            )
        credentials.add(
            conn, site_id, label, kind, payload,
            created_by=request.state.user["id"] if request.state.user else None,
        )
    audit.log(
        request, "사이트 자격증명 등록: %s '%s' (%s)", site["site_key"], label, kind
    )
    return _credentials_redirect(site_id, notice=t(request, "자격증명을 등록했습니다."))


@app.post("/sites/{site_id}/credentials/{cred_id}/delete")
def site_credentials_delete(request: Request, site_id: int, cred_id: int):
    """로그인 자격증명 삭제. 관리자 전용."""
    _require_admin(request)
    with db.connect() as conn:
        cred = db.get_site_credential(conn, cred_id)
        if cred is None or cred["site_id"] != site_id:
            raise HTTPException(404, t(request, "자격증명 없음"))
        site = db.get_site(conn, site_id)
        db.delete_site_credential(conn, cred_id)
    audit.log(
        request, "사이트 자격증명 삭제: %s '%s'",
        site["site_key"] if site else f"#{site_id}", cred["label"],
    )
    return _credentials_redirect(site_id, notice=t(request, "자격증명을 삭제했습니다."))


@app.get("/archive/active")
def archive_active(request: Request) -> dict:
    """진행 중 아카이빙 URL 목록 (목록 화면 자동 갱신 폴링용).

    라이브 챌린지가 켜진 관리자에게는 사람 확인 대기 작업(needs_human)도 함께
    내려보낸다 — 사용자가 보고 있는 진행 화면에서 바로 '사람 확인 필요' 안내를
    띄우기 위함 (전역 배너 폴링이 이 키를 읽는다)."""
    data: dict = {"active": sorted(_active_snapshot())}
    if config.LIVE_CHALLENGE and permissions.system_allowed(
        getattr(request.state, "user", None)
    ):
        with db.connect() as conn:
            jobs = db.list_needs_human_jobs(conn)
        data["needs_human"] = [{"id": j["id"], "url": j["url"]} for j in jobs]
    return data


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
        total_sites = db.count_sites(conn)
        snap_dirs = db.list_snapshot_dirs(conn)
        recent_snaps = db.list_recent_snapshots(conn, limit=10)
        recent_logs = db.list_archive_logs(conn, limit=10)

    # 총 용량은 스냅샷 파일 합이 아니라 실제 저장공간 (DB·자원/문서 CAS 포함)
    total_bytes = sum(storage.archive_disk_usage().values())

    # 스냅샷은 불변이므로 디렉토리 용량을 그대로 합산한다 (트렌드·최근 목록용)
    sizes: dict[int, int] = {}
    counts = {k: 0 for k in starts}
    period_bytes = {k: 0 for k in starts}
    for row in snap_dirs:
        size = _snapshot_dir_size(row["domain"], row["slug"], row["dir_name"])
        sizes[row["id"]] = size
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
            "total_sites": total_sites,
            "total_snapshots": len(snap_dirs),
            "total_bytes": total_bytes,
            "week_count": counts["week"],
            "recent_count": counts["recent"],
            "trend": trend,
            "recent_snaps": recent_snaps,
            "sizes": sizes,
            "recent_logs": recent_logs,
            "extension_version": __version__,
        },
    )


@app.get("/go", response_class=HTMLResponse)
def go(request: Request, url: str):
    """URL → 타임라인 딥링크 (확장·북마크용). 세션 인증 라우트.

    입력 URL 을 정규화해 페이지를 찾으면 타임라인(/page/{id})으로 302 한다.
    http↔https 승격으로 스킴이 어긋난 경우를 대비해 반대 스킴도 한 번 더
    찾아본다. 없으면 안내 화면. 미인증이면 미들웨어가 /login 으로 보낸다.
    """
    try:
        norm = storage.normalize_url(url)
    except ValueError:
        raise HTTPException(400, t(request, "잘못된 URL"))
    scheme, rest = norm.split("://", 1)
    candidates = [norm]
    try:
        other = "http" if scheme == "https" else "https"
        candidates.append(storage.normalize_url(f"{other}://{rest}"))
    except ValueError:
        pass
    with db.connect() as conn:
        for cand in candidates:
            page = db.get_page(conn, cand)
            if page is not None:
                return RedirectResponse(f"/page/{page['id']}", status_code=302)
    return templates.TemplateResponse(
        request, "go_missing.html", {"url": norm}, status_code=404
    )


# 대시보드 주기 선택지 (초 단위 — 1시간 ~ 1개월)
_SCHEDULE_OPTIONS = [
    (3600, "1시간"), (3 * 3600, "3시간"), (6 * 3600, "6시간"), (12 * 3600, "12시간"),
    (86400, "1일"), (3 * 86400, "3일"), (7 * 86400, "1주일"), (30 * 86400, "1개월"),
]

# 직접 입력 단위 (분/시간/일 — 주는 7일로 입력)
_CUSTOM_UNIT_SECONDS = {"m": 60, "h": 3600, "d": 86400}


def _interval_from_form(interval: str, custom_value: str, custom_unit: str) -> int:
    """주기 폼 값을 초로 변환 — 프리셋(초 단위 숫자) 또는 직접 입력('custom').

    범위 검증은 scheduler.validate_interval 몫. 형식 위반 시 ValueError.
    """
    if interval != "custom":
        return int(interval)
    unit = _CUSTOM_UNIT_SECONDS.get(custom_unit)
    if unit is None:
        raise ValueError(f"잘못된 주기 단위: {custom_unit!r}")
    try:
        value = int(custom_value)
    except ValueError:
        raise ValueError("직접 입력 주기는 숫자여야 합니다")
    if value <= 0:
        raise ValueError("직접 입력 주기는 1 이상이어야 합니다")
    return value * unit


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
        network_tag = (
            db.get_network_tag(conn, page["network_tag_id"])
            if page["network_tag_id"] else None
        )
        site = db.get_site(conn, page["site_id"]) if page["site_id"] else None
        snap_logs = db.list_snapshot_archive_logs(conn, page_id)

    # 로그인 캡처 스냅샷은 소유자/관리자에게만 — 비소유자에겐 존재·해시·diff
    # 링크를 노출하지 않는다 (API 히스토리 목록과 동일 정책)
    visible = [
        s for s in snaps
        if not s["authenticated"] or _may_view_authenticated(request, s)
    ]
    if len(visible) != len(snaps):
        # 가려진 인증 스냅샷이 있으면, 같은 content_hash 를 가질 수 있는 변경없음
        # 확인 기록(checks)도 숨긴다 (해시 메타데이터 누출 차단)
        checks = []
    snaps = visible
    log_by_snap = {row["snapshot_id"]: row for row in snap_logs}
    items = []
    for i, s in enumerate(snaps, 1):
        badge = "new" if i == 1 else _BADGES[s["changed"]]
        # 상세 펼침용 — (불변) 스냅샷 디렉토리의 파일 목록 + 실행 로그의 단계
        files = storage.snapshot_files(
            storage.page_dir(page["domain"], page["slug"]) / s["dir_name"]
        )
        log = log_by_snap.get(s["id"])
        steps: list = []
        if log is not None and log["steps"]:
            try:
                steps = json.loads(log["steps"])
            except ValueError:
                steps = []
        items.append({
            "idx": i, "snap": s, "badge": badge,
            "files": files,
            "total_bytes": sum(f["bytes"] for f in files) if files else None,
            "steps": steps, "log": log,
        })
    items.reverse()  # 최신 먼저
    return templates.TemplateResponse(
        request, "timeline.html",
        {
            "page": page, "items": items, "checks": checks, "queued": queued,
            "notice": notice, "error": error,
            "network_tag": network_tag, "site": site,
            "schedule": schedule,
            "schedule_label": (
                i18n.schedule_label(
                    request, schedule["interval_seconds"], schedule["run_at_time"]
                )
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
    interval: str = Form(...),
    custom_value: str = Form(""),
    custom_unit: str = Form("h"),
    run_at: str = Form(""),
    next_path: str = Form("", alias="next"),
):
    """페이지 반복 주기 등록/변경. 주기는 1시간 ~ 1개월, 직접 입력·실행 시각 지원."""
    _require_archiver(request)
    with db.connect() as conn:
        page = db.get_page_by_id(conn, page_id)
    if page is None:
        raise HTTPException(404, t(request, "페이지 없음"))
    try:
        seconds = _interval_from_form(interval, custom_value, custom_unit)
        scheduler.set_schedule(page["url"], seconds, run_at=run_at or None)
    except ValueError as e:
        raise HTTPException(400, t(request, str(e)))
    audit.log(
        request, "스케줄 등록: %s (주기 %d초%s)", page["url"], seconds,
        f", 실행 시각 {run_at}" if run_at else "",
    )
    return RedirectResponse(_schedule_redirect(page_id, next_path), status_code=303)


@app.post("/page/{page_id}/schedule/next-run")
def schedule_next_run(
    request: Request,
    page_id: int,
    next_run: str = Form(...),
    next_path: str = Form("", alias="next"),
):
    """스케줄의 다음 실행 시각 변경.

    next_run 은 datetime-local 값(타임존 없는 naive 시각).
    사용자의 저장된 타임존으로 해석해 UTC 로 변환한다.
    """
    _require_archiver(request)
    with db.connect() as conn:
        page = db.get_page_by_id(conn, page_id)
    if page is None:
        raise HTTPException(404, t(request, "페이지 없음"))
    try:
        dt = datetime.fromisoformat(next_run)
    except ValueError:
        raise HTTPException(400, t(request, "잘못된 시각 형식: {v}", v=next_run))
    if dt.tzinfo is None:
        user = request.state.user
        user_tz = (user["timezone"] if user is not None else None) or "UTC"
        try:
            tz = zoneinfo.ZoneInfo(user_tz)
        except zoneinfo.ZoneInfoNotFoundError:
            tz = timezone.utc
        dt = dt.replace(tzinfo=tz).astimezone(timezone.utc)
    try:
        scheduler.set_next_run(page["url"], dt)
    except ValueError as e:
        raise HTTPException(400, t(request, str(e)))
    audit.log(
        request, "스케줄 다음 실행 변경: %s → %s",
        page["url"], dt.isoformat(timespec="seconds"),
    )
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
    audit.log(request, "스케줄 해제: %s", page["url"])
    return RedirectResponse(_schedule_redirect(page_id, next_path), status_code=303)


@app.get("/schedules", response_class=HTMLResponse)
def schedules_view(request: Request):
    """자동 재아카이빙 목록 — 페이지·사이트 아카이브 스케줄 현황과 변경·해제 관리."""
    with db.connect() as conn:
        rows = db.list_schedules(conn)
        crawl_rows = db.list_crawl_schedules(conn)

    def _label(s) -> str:
        return i18n.schedule_label(request, s["interval_seconds"], s["run_at_time"])

    items = [{"row": s, "label": _label(s)} for s in rows]
    crawl_items = [{"row": s, "label": _label(s)} for s in crawl_rows]
    return templates.TemplateResponse(
        request, "schedules.html",
        {
            "items": items, "crawl_items": crawl_items,
            "interval_options": _SCHEDULE_OPTIONS,
        },
    )


def _may_view_authenticated(request: Request, snap) -> bool:
    """로그인 자격증명으로 캡처된 스냅샷 열람 가능 여부 — 소유자 또는 관리자만.

    소유자 = 캡처에 쓰인 자격증명의 등록자(snapshots.authenticated_by). 세션
    사용자 본인/admin, 또는 그 사용자에게 귀속된 확장 토큰(api_key의 owner)으로
    인증된 경우 허용. 등록자 미상(NULL)이면 admin 전용. 인증 off 면 전부 허용.
    """
    if not config.AUTH_ENABLED:
        return True
    owner_id = snap["authenticated_by"]
    user = request.state.user
    if user is not None and user["role"] == "admin":
        return True
    if owner_id is not None:
        if user is not None and user["id"] == owner_id:
            return True
        key = getattr(request.state, "api_key", None)
        if key is not None and key["owner_user_id"] is not None:
            return key["owner_user_id"] == owner_id
    return False


def _snapshot_viewer(request: Request) -> "tuple[int | None, bool] | None":
    """집계(목록·카운트)에서 인증 스냅샷을 가릴 기준 — (viewer_id, is_admin).

    인증 off 면 None(전체 허용). 세션 사용자는 (본인 id, admin 여부), API 키는
    (소유자 id, False) — 시스템 키는 소유자 None 이라 인증 스냅샷이 제외된다.
    """
    if not config.AUTH_ENABLED:
        return None
    user = request.state.user
    if user is not None:
        return (user["id"], user["role"] == "admin")
    key = getattr(request.state, "api_key", None)
    if key is not None:
        return (key["owner_user_id"], False)
    return (None, False)


def _load_snapshot(request: Request, snapshot_id: int):
    with db.connect() as conn:
        snap = db.get_snapshot(conn, snapshot_id)
    if snap is None:
        raise HTTPException(404, t(request, "스냅샷 없음", ctx="one"))
    # 로그인 캡처 스냅샷은 소유자/관리자만 — 그 외에는 존재를 은폐(404)
    if snap["authenticated"] and not _may_view_authenticated(request, snap):
        raise HTTPException(404, t(request, "스냅샷 없음", ctx="one"))
    return snap


def _snapshot_dir(snap) -> Path:
    return storage.page_dir(snap["domain"], snap["slug"]) / snap["dir_name"]


@app.get("/snapshot/{snapshot_id}", response_class=HTMLResponse)
def snapshot_view(request: Request, snapshot_id: int):
    snap = _load_snapshot(request, snapshot_id)
    network_tag = None
    if snap["network_tag_id"]:
        with db.connect() as conn:
            network_tag = db.get_network_tag(conn, snap["network_tag_id"])
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
            "network_tag": network_tag,
            "title": title,
            "documents": documents,
            "page_html_url": f"/snapshot/{snapshot_id}/file/page.html",
            "screenshot_url": f"/snapshot/{snapshot_id}/file/screenshot",
            "content_url": f"/snapshot/{snapshot_id}/file/content.md",
            # 문서 스냅샷(URL 자체가 파일 다운로드)은 스크린샷이 없다 — 탭 숨김
            "has_screenshot": storage.find_screenshot(_snapshot_dir(snap)) is not None,
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
        # 직접 열어도 아카이빙된 JS가 실행되지 않도록 문서 자체를 샌드박스.
        # allow-top-navigation-by-user-activation 은 사용자가 링크를 직접
        # 클릭했을 때만 뷰어 전체의 이동을 허용한다 — 사이트 전체 아카이브의
        # 재작성된 링크(target="_top")가 다음 스냅샷 뷰어로 가는 통로이며,
        # 스크립트 실행은 여전히 차단된다 (allow-scripts 절대 추가 금지).
        headers["Content-Security-Policy"] = (
            "sandbox allow-top-navigation-by-user-activation"
        )
    return FileResponse(path, media_type=media_type, headers=headers)


def _document_response(path: Path, filename: str) -> FileResponse:
    """문서 파일 응답 — 항상 첨부파일 다운로드 + 문서 컨텍스트 샌드박스."""
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=filename,  # Content-Disposition: attachment
        headers={"Content-Security-Policy": "sandbox"},
    )


@app.get("/snapshot/{snapshot_id}/doc/{name}")
def snapshot_document(request: Request, snapshot_id: int, name: str):
    """함께 저장된 문서 파일 서빙 — meta.json 의 documents 목록에 있는
    이름만 허용한다 (목록의 이름은 documents.py 가 정제해 생성한 값).

    파일 본체는 문서 CAS(snapshot_documents 행의 sha256)에서 찾고, compact
    이전의 구형 스냅샷은 디렉토리의 files/ 에서 찾는다. /resource/ 와 달리
    인증 게이트를 그대로 거치며, 브라우저 안에서 렌더링되지 않도록 항상
    첨부파일 다운로드로 내려준다.
    """
    snap = _load_snapshot(request, snapshot_id)
    snap_dir = _snapshot_dir(snap)
    try:
        meta = storage.read_meta(snap_dir)
    except OSError:
        raise HTTPException(404, t(request, "메타데이터 없음"))
    entry = next(
        (d for d in meta.documents or [] if d.get("file") == name), None
    )
    if entry is None:
        raise HTTPException(404, t(request, "허용되지 않은 파일"))
    legacy = snap_dir / "files" / name
    if legacy.is_file():
        return _document_response(legacy, name)
    with db.connect() as conn:
        row = db.get_snapshot_document(conn, snapshot_id, name)
    sha = row["sha256"] if row else str(entry.get("sha256") or "")
    cas_name = documents.cas_name(sha, name)
    if cas_name is not None:
        path = documents.cas_path(cas_name)
        if path.is_file():
            return _document_response(path, name)
    raise HTTPException(404, t(request, "파일 없음"))


_DOCUMENTS_PER_PAGE = 100


@app.get("/documents", response_class=HTMLResponse)
def documents_view(request: Request, page: int = Query(1, ge=1)):
    """아카이브된 페이지들의 문서 파일 통합 목록.

    같은 내용(sha256)의 문서는 한 행으로 묶고 참조 스냅샷·페이지 수를
    보여준다. compact 이전 구형 스냅샷(files/)의 문서는 아직 참조 행이
    없으므로, 남아 있으면 압축 실행 안내를 띄운다.
    """
    offset = (page - 1) * _DOCUMENTS_PER_PAGE
    with db.connect() as conn:
        totals = db.document_totals(conn)
        groups = db.list_document_groups(
            conn, limit=_DOCUMENTS_PER_PAGE + 1, offset=offset
        )
    has_next = len(groups) > _DOCUMENTS_PER_PAGE
    legacy_pending = any(
        documents.has_legacy_documents(d) for d in resources.snapshot_dirs()
    )
    return templates.TemplateResponse(
        request, "documents.html",
        {
            "groups": groups[:_DOCUMENTS_PER_PAGE],
            "totals": totals,
            "page": page,
            "has_next": has_next,
            "legacy_pending": legacy_pending,
        },
    )


_SEARCH_PER_PAGE = 20


@app.get("/search", response_class=HTMLResponse)
def search_view(
    request: Request,
    q: str = "",
    domain: str = "",
    latest: int = 0,
    page: int = Query(1, ge=1),
):
    """아카이브 전문 검색 — content.md(정규화 텍스트) + 첨부 문서 본문.

    viewer 이상만 접근(검색은 모든 아카이브 본문을 훑는 강한 열람 권한).
    한국어는 trigram 부분문자열로 찾고, 1~2글자 쿼리는 부분일치로 폴백한다.
    """
    if not permissions.can_search(request.state.user):
        raise HTTPException(403, t(request, "검색 권한이 없습니다"))
    query = (q or "").strip()
    domain_filter = (domain or "").strip() or None
    latest_only = bool(latest)
    available = searchindex.available()
    results = None
    total_pages = 1
    if available and query:
        results = searchindex.search(
            query, domain=domain_filter, latest_only=latest_only,
            limit=_SEARCH_PER_PAGE, offset=(page - 1) * _SEARCH_PER_PAGE,
        )
        total_pages = max(1, -(-results.total // _SEARCH_PER_PAGE))  # ceil
        if page > total_pages and results.total:
            # 페이지가 범위를 넘었으면 마지막 페이지로 보정해 다시 조회
            page = total_pages
            results = searchindex.search(
                query, domain=domain_filter, latest_only=latest_only,
                limit=_SEARCH_PER_PAGE, offset=(page - 1) * _SEARCH_PER_PAGE,
            )
    return templates.TemplateResponse(
        request, "search.html",
        {
            "q": query, "domain": domain_filter or "", "latest": latest_only,
            "available": available, "results": results,
            "page": page, "total_pages": total_pages,
            "has_prev": page > 1, "has_next": page < total_pages,
            "per_page": _SEARCH_PER_PAGE,
        },
    )


@app.get("/document/{sha256}/{name}")
def document_download(request: Request, sha256: str, name: str):
    """문서 CAS 파일 다운로드 — DB 에 기록된 (sha256, 파일명) 조합만 허용.

    문서 목록 화면의 다운로드 링크용. 인증 게이트를 그대로 거치며 항상
    첨부파일 다운로드로 내려준다 (snapshot_document 와 동일한 보안 성질).
    """
    cas_name = documents.cas_name(sha256, name)
    if cas_name is None:
        raise HTTPException(404, t(request, "허용되지 않은 파일"))
    with db.connect() as conn:
        row = db.find_document(conn, sha256, name)
    if row is None:
        raise HTTPException(404, t(request, "허용되지 않은 파일"))
    path = documents.cas_path(cas_name)
    if not path.is_file():
        raise HTTPException(404, t(request, "파일 없음"))
    return _document_response(path, name)


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
    headers = {
        "Content-Security-Policy": "sandbox",
        "Cache-Control": "public, max-age=31536000, immutable",
    }
    # CSS 는 gzip 으로 저장된다 (resources._store_css). 구형 아카이브의
    # 비압축 .css 와 공존하므로 매직 바이트로 판별한다.
    if name.endswith(".css") and resources.is_gzipped(path):
        headers["Content-Encoding"] = "gzip"
    return FileResponse(
        path,
        media_type=resources.EXT_MEDIA_TYPES[Path(name).suffix],
        headers=headers,
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
    old_snap, new_snap = snaps[from_idx - 1], snaps[to_idx - 1]
    # 로그인 캡처 스냅샷은 소유자/관리자만 — diff(본문·스크린샷) 양쪽 진입을 한 곳에서 가린다
    for snap in (old_snap, new_snap):
        if snap["authenticated"] and not _may_view_authenticated(request, snap):
            raise HTTPException(404, t(request, "스냅샷 없음", ctx="one"))
    return page, snaps, from_idx, to_idx, old_snap, new_snap


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
_LOG_PAGE_SIZES = (10, 25, 50, 100, 200)
_LOG_PAGE_SIZE_DEFAULT = 25


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
    limit: int = _LOG_PAGE_SIZE_DEFAULT,
    retry: str | None = None,
):
    """아카이빙 로그. 도메인/페이지/스냅샷/상태/기간 필터 + 페이징.

    viewer 이상(admin/archiver/viewer)만 열람 — pending 은 미들웨어가
    이미 차단하지만, 권한 정책을 라우트에서도 명시적으로 강제한다.
    """
    if not permissions.can_view_logs(request.state.user):
        raise HTTPException(403, t(request, "로그 열람 권한이 없습니다"))
    if limit not in _LOG_PAGE_SIZES:
        limit = _LOG_PAGE_SIZE_DEFAULT
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
    if limit != _LOG_PAGE_SIZE_DEFAULT:
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
            "limits": _LOG_PAGE_SIZES,
            "statuses": _LOG_STATUSES,
            "total": total, "total_pages": total_pages, "page_num": page,
            "prev_url": _page_url(page - 1) if page > 1 else None,
            "next_url": _page_url(page + 1) if page < total_pages else None,
            # 재시도 폼의 복귀 경로(필터 유지)와 결과 알림 (queued|active)
            "current_url": _page_url(page),
            "retry": retry if retry in ("queued", "active") else None,
        },
    )


@app.get("/settings/archives", response_class=HTMLResponse)
def my_archives(
    request: Request,
    status: str | None = None,
    page: int = 1,
    limit: int = _LOG_PAGE_SIZE_DEFAULT,
):
    """내 아카이브 — 본인이 직접 요청한(web·확장 토큰) 단발 아카이빙 실행 이력.

    로그인 사용자라면 권한과 무관하게 본인 기록을 본다. 인증 off(loopback)에는
    '본인'이 없으므로 빈 목록을 보여준다. 예약·크롤·CLI 실행은 requested_by 가
    NULL 이라 포함되지 않는다.
    """
    requester = _requester_id(request)
    if status not in _LOG_STATUSES:
        status = None
    if limit not in _LOG_PAGE_SIZES:
        limit = _LOG_PAGE_SIZE_DEFAULT
    page = max(1, page)
    items: list[dict] = []
    total = 0
    total_pages = 1
    if requester is not None:
        with db.connect() as conn:
            total = db.count_archive_logs(
                conn, requested_by=requester, status=status
            )
            total_pages = max(1, -(-total // limit))  # ceil
            page = max(1, min(page, total_pages))
            logs = db.list_archive_logs(
                conn, requested_by=requester, status=status,
                limit=limit, offset=(page - 1) * limit,
            )
        items = [{"log": row} for row in logs]

    # 페이징 링크 — 현재 필터(상태·줄 수)를 유지한 채 page 만 바꾼다
    qs_base = [("status", status)] if status else []
    if limit != _LOG_PAGE_SIZE_DEFAULT:
        qs_base.append(("limit", limit))

    def _page_url(n: int) -> str:
        params = qs_base + ([("page", n)] if n > 1 else [])
        return "/settings/archives" + ("?" + urlencode(params) if params else "")

    return templates.TemplateResponse(
        request, "my_archives.html",
        {
            "items": items, "status": status or "", "limit": limit,
            "limits": _LOG_PAGE_SIZES, "statuses": _LOG_STATUSES,
            "total": total, "total_pages": total_pages, "page_num": page,
            "prev_url": _page_url(page - 1) if page > 1 else None,
            "next_url": _page_url(page + 1) if page < total_pages else None,
        },
    )


def _requester_id(request: Request) -> int | None:
    """요청한 사용자의 id (인증 off 등으로 사용자가 없으면 None).

    '내 아카이브' 귀속을 위해 archive_jobs/archive_logs.requested_by 에 싣는다.
    """
    user = getattr(request.state, "user", None)
    return user["id"] if user is not None else None


def _queue_archive(
    url: str,
    force: bool = False,
    interval_seconds: int | None = None,
    run_at: str | None = None,
    source: str = "web",
    requested_by: int | None = None,
    network_tag_id: str | None = None,
    credential_id: int | None = None,
) -> bool:
    """단발 아카이빙 작업을 archive_jobs 큐에 추가. 같은 URL 이 이미 큐에 있으면
    무시(False — 호출부가 기존처럼 '이미 진행 중' 안내를 띄운다).

    실제 캡처는 worker(또는 serve 단일 프로세스)의 archive_worker 가 큐를
    소비해 실행한다 — 대시보드 프로세스에서 직접 캡처하지 않는다. interval 이
    있으면 소비자가 아카이빙 후 자동 재아카이빙 주기를 등록한다. requested_by 는
    요청 사용자 — 큐를 거쳐 archive_logs 까지 이어져 '내 아카이브'에 귀속된다.
    URL 정규화·netcheck 게이트·자격증명 검증은 라우트 본문이 enqueue 전에 동기로
    끝내므로 잘못된 입력은 여전히 폼에서 즉시 에러로 보인다.
    """
    with db.connect() as conn:
        return db.enqueue_archive_job(
            conn, url, force=force, source=source, requested_by=requested_by,
            network_tag_id=network_tag_id, credential_id=credential_id,
            interval_seconds=interval_seconds, run_at=run_at,
        )


def _require_archiver(request: Request) -> None:
    """아카이빙 권한 가드 (admin/archiver). 보기 전용·차단 계정은 403."""
    if not permissions.can_archive(request.state.user):
        raise HTTPException(403, t(request, "아카이빙 권한이 없습니다"))


def _require_deleter(request: Request) -> None:
    """삭제 권한 가드 (admin/archiver). 보기 전용·차단 계정은 403."""
    if not permissions.can_delete(request.state.user):
        raise HTTPException(403, t(request, "삭제 권한이 없습니다"))


def _require_admin(request: Request) -> None:
    """관리자 가드 — 시스템 관리 동작(로그인 자격증명 등). 그 외 계정은 403."""
    if not permissions.system_allowed(request.state.user):
        raise HTTPException(403, t(request, "관리자만 접근할 수 있습니다"))


@app.get("/archive/new", response_class=HTMLResponse)
def archive_new_form(request: Request, error: str = "", url: str = ""):
    """새 아카이빙 등록 화면 — URL 입력 + 자동 재아카이빙 주기 선택.

    사이트 전체 아카이브 옵션(체크 시 크롤 옵션 노출)도 이 화면에서 받는다.
    크롤 옵션의 초깃값은 시스템 설정의 기본값이다.
    """
    _require_archiver(request)
    with db.connect() as conn:
        defaults = crawler.crawl_defaults(conn)
        network_tags = db.list_network_tags(conn)
    return templates.TemplateResponse(
        request, "archive_new.html",
        {
            "error": error, "url": url, "interval_options": _SCHEDULE_OPTIONS,
            "network_tags": network_tags,
            "secret_key_configured": crypto.is_configured(),
            "credential_kinds": [
                {"value": k, "label": credentials.kind_label(k)}
                for k in credentials.KINDS
            ],
            "crawl_defaults": {
                "max_pages": defaults["max_pages"],
                "max_depth": defaults["max_depth"],
                "delay": defaults["delay_seconds"],
            },
            "crawl_limits": {
                "max_pages": config.CRAWL_MAX_PAGES_LIMIT,
                "max_depth": config.CRAWL_MAX_DEPTH_LIMIT,
                "min_delay": config.CRAWL_MIN_DELAY_SECONDS,
                "max_delay": config.CRAWL_MAX_DELAY_SECONDS,
            },
        },
    )


def _network_gate(request: Request, norm: str, tag_id: str | None) -> str | None:
    """네트워크 게이트의 동기(폼) 검증 — 사용자에게 폼 오류로 바로 보여준다.

    공인 주소면 태그 무시(None 반환). 위반은 번역된 메시지의 ValueError.
    실제 강제는 pipeline/crawler 가 한 번 더 한다 (쓰기는 코어 모듈 원칙).
    """
    kind = netcheck.classify_host(urlsplit(norm).hostname or "")
    if kind == netcheck.LOOPBACK:
        raise ValueError(t(request, "루프백 주소는 아카이빙할 수 없습니다"))
    if kind != netcheck.PRIVATE:
        return None
    if not tag_id:
        raise ValueError(t(
            request,
            "로컬 네트워크(사설 IP) 주소는 로컬 네트워크 태그를 선택해야 "
            "아카이빙할 수 있습니다 — 태그는 시스템 화면에서 관리합니다",
        ))
    with db.connect() as conn:
        if db.get_network_tag(conn, tag_id) is None:
            raise ValueError(t(request, "알 수 없는 로컬 네트워크 태그입니다"))
    return tag_id


def _archive_site(
    request: Request, url: str, max_pages: str, max_depth: str, delay: str,
    interval_seconds: int | None = None, run_at: str | None = None,
    network_tag_id: str | None = None, credential_id: int | None = None,
) -> RedirectResponse:
    """사이트 전체 아카이브 등록 — 크롤을 만들고 진행 화면으로 보낸다.

    같은 시작 URL 의 크롤이 진행 중이면 그 크롤로 자동 병합되어 기존 진행
    화면으로 보낸다 (merged=1 — 화면이 병합 알림을 띄운다).
    실행은 크롤러 폴링 스레드가 큐를 소비하며 진행한다 (등록과 분리).
    주기가 있으면 같은 옵션으로 크롤 스케줄도 등록한다 — 다음 실행은
    지금 + 주기 (첫 실행은 방금 등록한 크롤).
    """
    try:
        options = {
            "max_pages": int(max_pages) if max_pages else None,
            "max_depth": int(max_depth) if max_depth else None,
            "delay_seconds": int(delay) if delay else None,
        }
        crawl, merged = crawler.start_crawl(
            url, **options, source="web",
            network_tag_id=network_tag_id, credential_id=credential_id,
        )
        if interval_seconds:
            crawler.set_crawl_schedule(
                url, interval_seconds, run_at=run_at, **options,
                network_tag_id=network_tag_id, credential_id=credential_id,
            )
    except ValueError as exc:
        params = urlencode(
            {"error": t(request, "아카이빙 실패: {e}", e=exc), "url": url}
        )
        return RedirectResponse(f"/archive/new?{params}", status_code=303)
    audit.log(
        request, "사이트 아카이브 등록: %s → 크롤 #%d%s", url, crawl["id"],
        " (진행 중인 크롤로 병합)" if merged else "",
    )
    if interval_seconds:
        audit.log(
            request, "크롤 스케줄 등록: %s (주기 %d초)", url, interval_seconds
        )
    suffix = "?merged=1" if merged else ""
    return RedirectResponse(f"/crawls/{crawl['id']}{suffix}", status_code=303)


def _default_credential_label(kind: str, username: str) -> str:
    """새 아카이빙 폼에서 라벨을 비웠을 때의 기본 라벨 (DB 에 그대로 저장)."""
    if kind == credentials.KIND_HTTP_BASIC and username.strip():
        return username.strip()
    if kind == credentials.KIND_SESSION:
        return "세션"
    if kind == credentials.KIND_JWT:
        return "JWT"
    return "로그인"


def _create_archive_credential(
    request: Request, site_key: str, *,
    kind: str, label: str, username: str, password: str,
    storage_state: str, token: str, har_file: UploadFile | None = None,
) -> tuple[int | None, str | None]:
    """새 자격증명을 만들어 (credential_id, None) 또는 (None, 오류메시지) 반환.

    관리자라고 가정한다(호출부가 보장). 관리 화면과 같은 credentials 코어로
    암호화 저장하고, 사이트가 없으면 확보(get_or_create_site)한다. 세션 종류는
    storage_state JSON 대신 HAR 파일(har_file)을 올려 쿠키를 자동 추출할 수 있다.
    """
    if not crypto.is_configured():
        return None, t(request, "WCCG_SECRET_KEY 가 설정되지 않아 자격증명을 저장할 수 없습니다.")
    if kind not in credentials.KINDS:
        return None, t(request, "잘못된 자격증명 종류입니다.")
    try:
        if kind == credentials.KIND_SESSION:
            storage_state = _session_storage_state(
                storage_state, _read_har_upload(har_file), site_key=site_key
            )
        payload = credentials.build_payload(
            kind,
            {
                "username": username, "password": password,
                "storage_state": storage_state, "token": token,
            },
        )
    except credentials.CredentialError as exc:
        return None, t(request, str(exc))
    label = label.strip() or _default_credential_label(kind, username)
    label_error = credentials.validate_label(label)
    if label_error is not None:
        return None, t(request, label_error)
    with db.connect() as conn:
        existing = db.get_site_by_key(conn, site_key)
        if existing is not None and db.get_site_credential_by_label(
            conn, existing["id"], label
        ) is not None:
            return None, t(
                request,
                "이 사이트에 이미 같은 이름의 자격증명이 있습니다: {name}", name=label,
            )
        site_id = db.get_or_create_site(conn, site_key)
        cred_id = credentials.add(
            conn, site_id, label, kind, payload,
            created_by=request.state.user["id"] if request.state.user else None,
        )
    audit.log(
        request, "새 아카이빙에서 로그인 자격증명 등록: %s '%s' (%s)",
        site_key, label, kind,
    )
    return cred_id, None


def _resolve_archive_credential(
    request: Request, norm: str, *,
    existing_id: str, kind: str, label: str,
    username: str, password: str, storage_state: str, token: str,
    har_file: UploadFile | None = None,
) -> tuple[int | None, str | None]:
    """새 아카이빙 폼의 자격증명 선택을 해석 — (연결할 credential_id, 오류) 반환.

    관리자 전용(비관리자면 무시). existing_id 가:
    - ""        : 연결 안 함 → (None, None)
    - "__new__" : 새 자격증명을 만들어 그 id 를 연결
    - 숫자       : 그 기존 자격증명을 연결 (URL 도메인 소속인지 검증)
    """
    if not permissions.system_allowed(request.state.user):
        return None, None
    existing_id = existing_id.strip()
    if not existing_id:
        return None, None
    site_key = storage.site_key(norm)
    if existing_id == "__new__":
        return _create_archive_credential(
            request, site_key, kind=kind, label=label, username=username,
            password=password, storage_state=storage_state, token=token,
            har_file=har_file,
        )
    if not existing_id.isdigit():
        return None, t(request, "잘못된 자격증명 선택입니다.")
    cred_id = int(existing_id)
    with db.connect() as conn:
        site = db.get_site_by_key(conn, site_key)
        cred = db.get_site_credential(conn, cred_id)
        if site is None or cred is None or cred["site_id"] != site["id"]:
            return None, t(request, "이 도메인에 등록된 자격증명이 아닙니다.")
    audit.log(
        request, "새 아카이빙에 기존 자격증명 연결: %s (cred #%d)", site_key, cred_id
    )
    return cred_id, None


@app.get("/archive/credentials")
def archive_credentials(request: Request, url: str = "") -> dict:
    """입력된 URL 의 도메인(사이트)에 등록된 로그인 자격증명 목록 (관리자 전용).

    새 아카이빙 폼이 URL 입력 시 호출해 '연결' 드롭다운을 채운다. 비밀은
    내려보내지 않는다 — id·라벨·종류만. URL 이 비었거나 잘못됐거나 사이트가
    없으면 빈 목록.
    """
    _require_admin(request)
    try:
        site_key = storage.site_key(storage.normalize_url(url)) if url.strip() else ""
    except ValueError:
        site_key = ""
    creds: list[dict] = []
    if site_key:
        with db.connect() as conn:
            site = db.get_site_by_key(conn, site_key)
            if site is not None:
                creds = [
                    {
                        "id": c["id"], "label": c["label"], "kind": c["kind"],
                        "kind_label": i18n.t(request, credentials.kind_label(c["kind"])),
                    }
                    for c in db.list_site_credentials(conn, site["id"])
                ]
    return {"site_key": site_key, "credentials": creds}


@app.post("/archive")
def archive_new(
    request: Request,
    url: str = Form(...),
    site: str = Form(""),
    crawl_max_pages: str = Form(""),
    crawl_max_depth: str = Form(""),
    crawl_delay: str = Form(""),
    interval: str = Form("0"),
    custom_value: str = Form(""),
    custom_unit: str = Form("h"),
    run_at: str = Form(""),
    network_tag: str = Form(""),
    cred_existing_id: str = Form(""),
    cred_kind: str = Form(""),
    cred_label: str = Form(""),
    cred_username: str = Form(""),
    cred_password: str = Form(""),
    cred_storage_state: str = Form(""),
    cred_token: str = Form(""),
    cred_har_file: UploadFile | None = File(None),
):
    """새 URL 아카이빙. 검증은 동기로, 캡처·주기 등록(interval>0)은 백그라운드로.

    site 체크 시 사이트 전체 아카이브(크롤) 등록으로 분기한다 — 주기를
    선택했으면 같은 옵션의 크롤 스케줄(주기적 재크롤)도 함께 등록된다.
    network_tag 는 로컬 네트워크(사설 IP) 대상일 때 필수 (루프백은 거부).
    """
    _require_archiver(request)
    try:
        seconds = _interval_from_form(interval, custom_value, custom_unit)
        if seconds:
            scheduler.validate_interval(seconds)
            if run_at:
                scheduler.validate_run_at(run_at, seconds)
        norm = storage.normalize_url(url)
        tag_id = _network_gate(request, norm, network_tag.strip() or None)
    except ValueError as exc:
        params = urlencode(
            {"error": t(request, "아카이빙 실패: {e}", e=exc), "url": url}
        )
        return RedirectResponse(f"/archive/new?{params}", status_code=303)
    link_credential_id, cred_error = _resolve_archive_credential(
        request, norm, existing_id=cred_existing_id, kind=cred_kind.strip(),
        label=cred_label, username=cred_username, password=cred_password,
        storage_state=cred_storage_state, token=cred_token,
        har_file=cred_har_file,
    )
    if cred_error is not None:
        # 비밀번호·토큰이 쿼리스트링·로그에 실리지 않게 url 만 보존한다
        params = urlencode({"error": cred_error, "url": url})
        return RedirectResponse(f"/archive/new?{params}", status_code=303)
    if site:
        # 크롤(사이트 전체)은 자격증명을 crawls.credential_id 에 실어 크롤러가
        # 모든 페이지에 적용한다 (network_tag_id 와 같은 경로). 주기 크롤은
        # crawl_schedules.credential_id 로 이어진다.
        return _archive_site(
            request, url, crawl_max_pages, crawl_max_depth, crawl_delay,
            interval_seconds=seconds or None,
            run_at=(run_at or None) if seconds else None,
            network_tag_id=tag_id, credential_id=link_credential_id,
        )
    if _queue_archive(
        norm, requested_by=_requester_id(request),
        interval_seconds=seconds or None, run_at=(run_at or None) if seconds else None,
        network_tag_id=tag_id, credential_id=link_credential_id,
    ):
        audit.log(
            request, "새 아카이빙 등록: %s%s", norm,
            f" (주기 {seconds}초)" if seconds else "",
        )
    return RedirectResponse(f"/archives?queued={quote(norm, safe='')}", status_code=303)


@app.post("/page/{page_id}/rearchive")
def rearchive(
    request: Request,
    page_id: int,
    force: bool = Form(False),
):
    _require_archiver(request)
    with db.connect() as conn:
        page = db.get_page_by_id(conn, page_id)
    if page is None:
        raise HTTPException(404, t(request, "페이지 없음"))
    if _queue_archive(page["url"], force=force, requested_by=_requester_id(request)):
        audit.log(
            request, "재아카이빙 등록: %s%s", page["url"],
            " (강제)" if force else "",
        )
    return RedirectResponse(url=f"/page/{page_id}?queued=1", status_code=303)


@app.post("/logs/{log_id}/retry")
def log_retry(
    request: Request,
    log_id: int,
    next_url: str = Form("/logs", alias="next"),
):
    """실패 로그의 URL 재시도 — 같은 URL 을 백그라운드로 다시 아카이빙.

    페이지가 안 만들어진 실패(page_id NULL)도 로그의 url 로 다시 시도할 수
    있다. 사설 대역 페이지의 네트워크 태그는 pipeline 이 기존 페이지에서
    물려받는다 (_resolve_network_tag).
    """
    _require_archiver(request)
    with db.connect() as conn:
        log = db.get_archive_log(conn, log_id)
    if log is None:
        raise HTTPException(404, t(request, "로그 없음"))
    if log["status"] != "error":
        raise HTTPException(400, t(request, "실패한 로그만 재시도할 수 있습니다"))
    queued = _queue_archive(log["url"], requested_by=_requester_id(request))
    if queued:
        audit.log(request, "실패 로그 재시도: %s", log["url"])
    # 필터를 유지한 채 로그 화면으로 복귀 — 내부 /logs 경로만 허용 (open redirect 방지)
    if not next_url.startswith("/logs"):
        next_url = "/logs"
    sep = "&" if "?" in next_url else "?"
    return RedirectResponse(
        f"{next_url}{sep}retry={'queued' if queued else 'active'}", status_code=303
    )


# ---- 사람 보조 챌린지 해결 (라이브 세션 뷰어 — 관리자 전용) ----
# worker 가 자동으로 못 푼 인터랙티브 챌린지를 사람이 직접 클릭/입력해 통과시킨다.
# 라이브 브라우저는 worker 에서 돌고, 화면(스크린샷 파일)·입력(live_commands DB)으로만
# 조율한다. 라이브 화면은 스크린샷 이미지 전용 — 아카이빙 DOM 임베드가 아니라 원칙 5
# 와 무관하고, 서버 위치에서 trusted 입력을 발생시키므로 admin 전용 + 소유자 바인딩.


def _live_job_or_404(request: Request, job_id: int):
    """needs_human 상태의 작업을 반환 (아니면 404). 라이브 라우트 공용."""
    with db.connect() as conn:
        job = db.get_archive_job(conn, job_id)
    if job is None or not job["needs_human_at"] or not job["live_token"]:
        raise HTTPException(404, t(request, "사람 확인이 필요한 작업이 아닙니다"))
    return job


def _live_owner_or_403(request: Request, job) -> None:
    """입력 권한 = 이 세션을 클레임한 admin 만 (다른 admin 은 보기만)."""
    if job["live_owner_id"] != request.state.user["id"]:
        raise HTTPException(403, t(request, "다른 관리자가 처리 중인 세션입니다"))


@app.get("/archive/needs-human")
def needs_human_list(request: Request):
    """사람 확인이 필요한 작업 목록 (관리자 전용)."""
    _require_admin(request)
    with db.connect() as conn:
        jobs = db.list_needs_human_jobs(conn)
    return templates.TemplateResponse(
        request, "needs_human.html", {"jobs": jobs},
    )


@app.get("/archive/jobs/{job_id}/live")
def live_view(request: Request, job_id: int):
    """라이브 챌린지 해결 화면 — 스크린샷을 보고 직접 클릭/입력해 통과시킨다.

    여는 관리자가 세션을 클레임(입력 권한자)한다. 이미 다른 관리자가
    클레임했으면 보기 전용으로 연다."""
    _require_admin(request)
    job = _live_job_or_404(request, job_id)
    with db.connect() as conn:
        owned = db.claim_live_session(conn, job_id, request.state.user["id"])
        job = db.get_archive_job(conn, job_id)
    audit.log(request, "라이브 챌린지 처리 시작: %s", job["url"])
    return templates.TemplateResponse(
        request, "live.html",
        {
            "job": job,
            "owned": owned,
            "viewport_w": job["live_viewport_w"] or config.LIVE_VIEWPORT_W,
            "viewport_h": job["live_viewport_h"] or config.LIVE_VIEWPORT_H,
        },
    )


@app.get("/archive/jobs/{job_id}/live/shot")
def live_shot(request: Request, job_id: int):
    """라이브 스크린샷 (관리자 전용). live_token 으로만 경로를 조립한다."""
    _require_admin(request)
    job = _live_job_or_404(request, job_id)
    path = live_challenge.shot_path(job["live_token"])
    if not path.exists():
        raise HTTPException(404, t(request, "아직 화면이 준비되지 않았습니다"))
    return FileResponse(
        path, media_type="image/jpeg",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/archive/jobs/{job_id}/live/state")
def live_state(request: Request, job_id: int):
    """라이브 세션 상태 폴링 (JSON). needs_human | done(통과·종료)."""
    _require_admin(request)
    with db.connect() as conn:
        job = db.get_archive_job(conn, job_id)
    if job is None or not job["needs_human_at"]:
        return {"status": "done"}  # 통과·취소·종료 — 큐에서 빠졌거나 해제됨
    return {
        "status": "needs_human",
        "owned": job["live_owner_id"] == request.state.user["id"],
    }


@app.post("/archive/jobs/{job_id}/live/click")
def live_click(
    request: Request, job_id: int,
    x: int = Form(...), y: int = Form(...),
    kind: str = Form("click"), delay_ms: int = Form(0),
):
    """클릭/드래그 좌표를 명령 큐에 넣는다 (worker 가 page.mouse 로 재생)."""
    _require_admin(request)
    job = _live_job_or_404(request, job_id)
    _live_owner_or_403(request, job)
    if kind not in ("click", "move", "down", "up"):
        raise HTTPException(400, "kind")
    with db.connect() as conn:
        db.enqueue_live_command(
            conn, job["live_token"], kind=kind, x=x, y=y, delay_ms=max(0, delay_ms))
    return {"ok": True}


@app.post("/archive/jobs/{job_id}/live/key")
def live_key(
    request: Request, job_id: int,
    key: str = Form(...), kind: str = Form("text"), delay_ms: int = Form(0),
):
    """키 입력/문자열을 명령 큐에 넣는다 (worker 가 page.keyboard 로 재생)."""
    _require_admin(request)
    job = _live_job_or_404(request, job_id)
    _live_owner_or_403(request, job)
    if kind not in ("key", "text"):
        raise HTTPException(400, "kind")
    with db.connect() as conn:
        db.enqueue_live_command(
            conn, job["live_token"], kind=kind, key=key[:200], delay_ms=max(0, delay_ms))
    return {"ok": True}


@app.post("/archive/jobs/{job_id}/live/cancel")
def live_cancel(request: Request, job_id: int):
    """라이브 세션 취소 — worker 가 다음 폴링에 중단하고 실패 처리한다."""
    _require_admin(request)
    job = _live_job_or_404(request, job_id)
    _live_owner_or_403(request, job)
    with db.connect() as conn:
        db.set_live_cancel(conn, job_id)
    audit.log(request, "라이브 챌린지 취소: %s", job["url"])
    return RedirectResponse("/archive/needs-human", status_code=303)


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
    audit.log(
        request, "페이지 삭제: %s (스냅샷 %d개)",
        result.url, result.snapshots_deleted,
    )
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
    audit.log(
        request, "스냅샷 삭제: %s (%s)", snap["page_url"], snap["taken_at"]
    )
    msg = t(request, "스냅샷 삭제됨: {t}", t=snap["taken_at"])
    return RedirectResponse(
        f"/page/{snap['page_id']}?notice={quote(msg, safe='')}", status_code=303
    )


# ---- 사이트 전체 아카이브 (크롤) ----


def _load_crawl(request: Request, crawl_id: int):
    with db.connect() as conn:
        crawl = db.get_crawl(conn, crawl_id)
    if crawl is None:
        raise HTTPException(404, t(request, "크롤 없음"))
    return crawl


@app.get("/crawls")
def crawls_view():
    """구 사이트 아카이브 목록 — 통합 아카이브 목록으로 이동했다."""
    return RedirectResponse("/archives", status_code=301)


_CRAWL_PAGE_STATUSES = ("pending", "in_progress", "done", "failed")


@app.get("/crawls/{crawl_id}", response_class=HTMLResponse)
def crawl_view(
    request: Request, crawl_id: int, merged: int = 0,
    status: str = "", notice: str = "",
):
    """크롤 진행 화면 — 상태별 집계와 페이지 목록, 취소·재시도.

    merged=1 이면 같은 사이트 아카이브가 이미 진행 중이라 이 크롤로
    병합되었다는 알림을 띄운다 (등록 직후 리다이렉트에서만 붙는다).
    status 로 페이지 목록을 상태별 필터링한다 (잘못된 값은 전체).
    실패 재시도 대기(시스템 설정, 진행 중 크롤에도 적용)도 함께 보여준다.
    """
    crawl = _load_crawl(request, crawl_id)
    status_filter = status if status in _CRAWL_PAGE_STATUSES else ""
    with db.connect() as conn:
        counts = db.crawl_page_counts(conn, crawl_id)
        pages = db.list_crawl_pages(conn, crawl_id, status=status_filter or None)
        backoff = crawler.retry_backoff(conn)
        network_tag = (
            db.get_network_tag(conn, crawl["network_tag_id"])
            if crawl["network_tag_id"] else None
        )
    return templates.TemplateResponse(
        request, "crawl.html",
        {
            "crawl": crawl, "counts": counts, "pages": pages, "merged": merged,
            "network_tag": network_tag,
            "status_filter": status_filter, "notice": notice,
            "retry_backoff_labels": [
                i18n.interval_label(request, s) for s in backoff
            ],
            "max_attempts": len(backoff) + 1,
        },
    )


@app.get("/crawls/{crawl_id}/status")
def crawl_status(request: Request, crawl_id: int) -> dict:
    """크롤 진행 상태 JSON (진행 화면 자동 갱신 폴링용)."""
    crawl = _load_crawl(request, crawl_id)
    with db.connect() as conn:
        counts = db.crawl_page_counts(conn, crawl_id)
    return {"status": crawl["status"], "counts": counts}


@app.post("/crawls/{crawl_id}/cancel")
def crawl_cancel(request: Request, crawl_id: int):
    """크롤 취소 — 처리 중인 페이지만 마치고 멈춘다. admin/archiver 전용."""
    _require_archiver(request)
    crawl = _load_crawl(request, crawl_id)
    with db.connect() as conn:
        db.cancel_crawl(conn, crawl_id)
    audit.log(request, "크롤 취소: #%d (%s)", crawl_id, crawl["start_url"])
    return RedirectResponse(f"/crawls/{crawl_id}", status_code=303)


@app.post("/crawls/{crawl_id}/retry")
def crawl_retry(request: Request, crawl_id: int):
    """실패한 페이지 일괄 재시도 (크롤이 닫혔으면 다시 연다). admin/archiver 전용."""
    _require_archiver(request)
    crawl = _load_crawl(request, crawl_id)
    with db.connect() as conn:
        db.retry_failed_crawl_pages(conn, crawl_id)
    audit.log(
        request, "크롤 실패 페이지 일괄 재시도: #%d (%s)",
        crawl_id, crawl["start_url"],
    )
    return RedirectResponse(f"/crawls/{crawl_id}", status_code=303)


@app.post("/crawls/{crawl_id}/pages/{crawl_page_id}/retry")
def crawl_page_retry(
    request: Request, crawl_id: int, crawl_page_id: int, status: str = ""
):
    """실패한 크롤 페이지 하나 재시도 (끝난 크롤이면 다시 연다). admin/archiver 전용.

    status 는 진행 화면의 페이지 필터 — 재시도 후 같은 필터로 돌아간다.
    """
    _require_archiver(request)
    _load_crawl(request, crawl_id)
    with db.connect() as conn:
        row = db.get_failed_crawl_page(conn, crawl_id, crawl_page_id)
        if row is None:
            raise HTTPException(404, t(request, "실패 기록 없음"))
        db.retry_failed_crawl_page(conn, crawl_page_id)
    audit.log(request, "크롤 페이지 재시도: %s", row["url"])
    params = {"notice": t(request, "재시도가 등록되었습니다 — 크롤러가 곧 다시 시도합니다.")}
    if status in _CRAWL_PAGE_STATUSES:
        params["status"] = status
    return RedirectResponse(f"/crawls/{crawl_id}?{urlencode(params)}", status_code=303)


# ---- 사이트 아카이브 스케줄 (주기적 재크롤) ----


def _load_crawl_schedule(request: Request, schedule_id: int):
    with db.connect() as conn:
        sched = db.get_crawl_schedule_by_id(conn, schedule_id)
    if sched is None:
        raise HTTPException(404, t(request, "스케줄 없음"))
    return sched


@app.post("/crawl-schedules/{schedule_id}")
def crawl_schedule_set(
    request: Request,
    schedule_id: int,
    interval: str = Form(...),
    custom_value: str = Form(""),
    custom_unit: str = Form("h"),
    run_at: str = Form(""),
):
    """크롤 스케줄 주기 변경 (크롤 옵션은 유지). admin/archiver 전용."""
    _require_archiver(request)
    sched = _load_crawl_schedule(request, schedule_id)
    try:
        seconds = _interval_from_form(interval, custom_value, custom_unit)
        crawler.set_crawl_schedule(
            sched["start_url"], seconds, run_at=run_at or None,
            max_pages=sched["max_pages"], max_depth=sched["max_depth"],
            delay_seconds=sched["delay_seconds"],
        )
    except ValueError as e:
        raise HTTPException(400, t(request, str(e)))
    audit.log(
        request, "크롤 스케줄 변경: %s (주기 %d초%s)", sched["start_url"], seconds,
        f", 실행 시각 {run_at}" if run_at else "",
    )
    return RedirectResponse("/schedules", status_code=303)


@app.post("/crawl-schedules/{schedule_id}/next-run")
def crawl_schedule_next_run(
    request: Request,
    schedule_id: int,
    next_run: str = Form(...),
):
    """크롤 스케줄의 다음 실행 시각 변경 (시각 해석은 페이지 스케줄과 동일)."""
    _require_archiver(request)
    sched = _load_crawl_schedule(request, schedule_id)
    try:
        dt = datetime.fromisoformat(next_run)
    except ValueError:
        raise HTTPException(400, t(request, "잘못된 시각 형식: {v}", v=next_run))
    if dt.tzinfo is None:
        user = request.state.user
        user_tz = (user["timezone"] if user is not None else None) or "UTC"
        try:
            tz = zoneinfo.ZoneInfo(user_tz)
        except zoneinfo.ZoneInfoNotFoundError:
            tz = timezone.utc
        dt = dt.replace(tzinfo=tz).astimezone(timezone.utc)
    try:
        crawler.set_crawl_schedule_next_run(schedule_id, dt)
    except ValueError as e:
        raise HTTPException(400, t(request, str(e)))
    audit.log(
        request, "크롤 스케줄 다음 실행 변경: %s → %s",
        sched["start_url"], dt.isoformat(timespec="seconds"),
    )
    return RedirectResponse("/schedules", status_code=303)


@app.post("/crawl-schedules/{schedule_id}/delete")
def crawl_schedule_delete(request: Request, schedule_id: int):
    """크롤 스케줄 해제 — 저장된 스냅샷·진행 중 크롤은 그대로 남는다."""
    _require_archiver(request)
    sched = _load_crawl_schedule(request, schedule_id)
    crawler.remove_crawl_schedule(sched["start_url"])
    audit.log(request, "크롤 스케줄 해제: %s", sched["start_url"])
    return RedirectResponse("/schedules", status_code=303)


@app.get("/crawl/{crawl_id}/goto")
def crawl_goto(request: Request, crawl_id: int, url: str):
    """아카이브 내 링크 리졸버 — 재작성된 page.html 앵커의 목적지.

    같은 크롤 세트에서 확인된 스냅샷 → 해당 URL 의 최신 스냅샷 순으로
    찾아 리다이렉트하고, 없으면 원본 링크를 안내하는 화면을 보여준다
    (라이브 사이트로 조용히 새지 않는다).
    """
    crawl = _load_crawl(request, crawl_id)
    try:
        norm = storage.normalize_url(url)
    except ValueError:
        raise HTTPException(400, t(request, "잘못된 URL"))
    with db.connect() as conn:
        snapshot_id = db.find_crawl_snapshot(conn, crawl_id, norm)
        if snapshot_id is None:
            page = db.get_page(conn, norm)
            if page is not None:
                last = db.last_snapshot(conn, page["id"])
                snapshot_id = last["id"] if last else None
    if snapshot_id is not None:
        return RedirectResponse(f"/snapshot/{snapshot_id}", status_code=302)
    return templates.TemplateResponse(
        request, "crawl_goto_missing.html",
        {"crawl": crawl, "url": norm},
        status_code=404,
    )
