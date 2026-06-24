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
from html import escape as html_escape
import time
import zipfile
from contextlib import asynccontextmanager
import zoneinfo
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

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
    __version__, archive_worker, auth, backup, cluster_sync, config, crawler,
    credentials, crypto, db, deletion, differ, documents, live_challenge, netcheck,
    resources, scheduler, searchindex, storage, system_log,
)
from . import (
    api_routes, audit, auth_routes, cluster_routes, i18n, migration_routes,
    permissions, web_api_routes, web_auth_routes,
)
from pydantic import BaseModel
from .i18n import t

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
            # 클러스터 조정 루프 — 피어별 권한 갱신·델타 동기화(B 측에서만 동작).
            threading.Thread(
                target=cluster_sync.run_loop,
                args=(stop,),
                name="wccg-cluster",
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
app.include_router(api_routes.router)
app.include_router(migration_routes.router)
app.include_router(cluster_routes.router)
app.include_router(web_api_routes.router)
app.include_router(web_auth_routes.router)

@app.middleware("http")
async def auth_gate(request: Request, call_next):
    """인증 게이트 + CSRF 방어 (C2 컷오버 — SPA 가 인증 라우팅 단일 권위).

    - POST 는 Origin(없으면 Referer) 호스트가 Host 와 일치해야 한다 (쿠키 SameSite=Lax
      이중 방어). /api/v1·/api/migration 은 Bearer/X-API-Key·이전 토큰이 자격증명이라
      쿠키 CSRF 가 성립하지 않아 Origin 검사만 면제한다(/api/web 은 세션이라 검사).
    - active·비차단 세션만 request.state.user 에 싣는다. 미인증·차단·승인대기 계정의
      화면 라우팅은 SPA 루트 레이아웃이 /api/web/me 응답으로 단일 결정하므로(컷오버),
      미들웨어는 더 이상 경로별 리다이렉트를 하지 않는다. 보호 데이터는 /api 가
      require_session 으로, 아카이브 자원 라우트는 _require_viewer 로 직접 가드한다.
    - 최초 구동(first_run)에는 setup API 외 모든 /api 를 401 로 막는다 (SPA 셸은 그대로
      떠 setup 화면을 띄운다).
    - 모든 응답에 보안 헤더 + (text/html) CSP. page.html 의 sandbox CSP 는 핸들러가
      이미 설정해 setdefault 가 덮지 않는다. HSTS 는 리버스 프록시 책임.
    """
    request.state.user = None
    request.state.session = None
    request.state.locale = i18n.resolve_locale(request)

    path = request.url.path
    _csrf_exempt = path.startswith("/api/") and not path.startswith("/api/web/")
    if request.method == "POST" and not _csrf_exempt:
        origin = request.headers.get("origin") or request.headers.get("referer")
        if origin:
            if urlsplit(origin).netloc != request.headers.get("host", ""):
                return PlainTextResponse(t(request, "CSRF 검증 실패"), status_code=403)
        elif not request.headers.get("x-requested-with"):
            # Origin/Referer 둘 다 없으면(이중 방어 공백) SPA 가 항상 싣는 커스텀 헤더를
            # 요구한다 — 크로스오리진에서 커스텀 헤더는 프리플라이트 없이 못 붙는다(F4).
            return PlainTextResponse(t(request, "CSRF 검증 실패"), status_code=403)

    if config.AUTH_ENABLED:
        first_run = False
        token = request.cookies.get(config.SESSION_COOKIE, "")
        with db.connect() as conn:
            # 역할 프리셋 캐시 워밍 — conn 없는 web.permissions 가 이 요청에서 최신
            # 프리셋을 보도록 앞단에서 한 번 갱신한다 (settings 버전 비교, 멀티프로세스 안전).
            db.role_presets(conn)
            if db.first_run_needed(conn):
                first_run = not auth.bootstrap_admin_from_env(conn)
            if not first_run and token:
                sess = auth.resolve_session(conn, token)
                if sess is not None:
                    request.state.session = sess
                    if sess["state"] == "active":
                        user = db.get_user_by_id(conn, sess["user_id"])
                        # 차단·탈퇴 계정은 미인증으로 취급 — require_session 이 401 →
                        # SPA 가 로그인으로 안내하고, 로그인 시도 자체가 사유를 알린다.
                        if user is not None and user["role"] not in ("blocked", "withdrawn"):
                            request.state.user = user

        # 로그인 사용자는 DB에 저장된 언어 설정을 우선 적용
        if request.state.user is not None:
            stored = request.state.user["locale"]
            if stored in i18n.SUPPORTED_LOCALES:
                request.state.locale = stored

        # 승인 대기(pending) 계정 — 권한이 없어 데이터 API 를 막는다. 세션 컨텍스트(/me)·
        # 번역(i18n)·인증(auth, 로그아웃)만 통과시켜 SPA 가 pending 안내 화면을 띄우게 한다.
        # 비-API(SPA 셸·자원)는 그대로 흐른다(자원 라우트는 _require_viewer 가 따로 가드).
        user = request.state.user
        if (
            user is not None
            and user["role"] == "pending"
            and path.startswith("/api/")
            and path != "/api/web/me"
            and not path.startswith("/api/web/i18n/")
            and not path.startswith("/api/web/auth/")
        ):
            return PlainTextResponse(
                t(request, "승인 대기 중입니다 — 관리자 승인 후 이용할 수 있습니다."),
                status_code=403,
            )

        if first_run and path.startswith("/api/"):
            # 최초 설정 API(/api/web/auth/setup*)만 통과 — 나머지 /api 는 401.
            # 비-API(SPA 셸·자원)는 그대로 흘러 SPA 가 setup 화면을 띄운다.
            if not (
                path == "/api/web/auth/setup"
                or path.startswith("/api/web/auth/setup/")
            ):
                return PlainTextResponse("최초 설정이 완료되지 않았습니다", status_code=401)

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
    "screenshot-mobile": (
        ("screenshot-mobile.webp", "image/webp"),
        ("screenshot-mobile.png", "image/png"),
    ),
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


def _needs_human_urls(request: Request) -> dict[str, int]:
    """사람 확인 대기 작업의 url→job_id 매핑 (상태 배지·배너 공용).

    needs_human 은 진행 중 작업이 자동으로 못 푼 챌린지를 만나 사람을 기다리는
    상태다 — 진행 목록에는 그대로 '활성'으로 남아 있으므로, 목록 화면이 이
    매핑으로 '아카이빙 중' 대신 '사람 확인 대기'(라이브 화면 링크)를 보여준다.

    중요: 이 상태는 **워커가** WCCG_LIVE_CHALLENGE 로 판단해 DB 에 기록하는 사실
    이다. 대시보드(serve) 프로세스의 LIVE_CHALLENGE 설정에 묶지 않는다 — 워커와
    serve 의 env 가 다르거나 serve 에 그 플래그가 없어도 대기 작업이 누락 없이
    보이게, 대기 작업의 존재(DB)만으로 안내한다. 진행 중 챌린지 URL 은 관리자
    정보라 관리자일 때만 채운다 (그 외 사용자에겐 빈 dict → 기존처럼 '아카이빙 중')."""
    if not permissions.can_manage_system(getattr(request.state, "user", None)):
        return {}
    with db.connect() as conn:
        return {j["url"]: j["id"] for j in db.list_needs_human_jobs(conn)}


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


_FAVICON_PATH = Path(__file__).parent / "static" / "favicon.svg"


@app.get("/favicon.svg", include_in_schema=False)
def favicon() -> FileResponse:
    """SVG 파비콘 (OS 라이트/다크 자동) — 인증 없이 서빙."""
    return FileResponse(_FAVICON_PATH, media_type="image/svg+xml")


# SvelteKit 정적 SPA 산출물 — 패키지 동봉본(web/frontend_dist) 우선,
# 없으면 개발 빌드(frontend/build)를 가리킨다. C2 컷오버로 SPA 를 루트(/)로 서빙한다
# (catch-all 등록은 파일 맨 끝 — 등록 순서 우선 매칭이라 실 라우트가 먼저 잡힌다).
_FRONTEND_DIST = Path(__file__).parent / "frontend_dist"
if not _FRONTEND_DIST.exists():
    _dev_dist = Path(__file__).resolve().parents[2] / "frontend" / "build"
    if _dev_dist.exists():
        _FRONTEND_DIST = _dev_dist


def _serve_spa(full_path: str) -> FileResponse:
    """SvelteKit 정적 SPA 서빙 + 클라이언트 라우팅 fallback.

    실존 파일이면 그 파일을, 아니면 index.html 을 돌려준다 (딥링크·새로고침).
    경로는 dist 안으로만 매핑한다 (path traversal 방지).
    """
    if not _FRONTEND_DIST.exists():
        raise HTTPException(503, "SPA 빌드가 없습니다 — frontend 를 빌드하세요")
    root = _FRONTEND_DIST.resolve()
    if full_path:
        candidate = (root / full_path).resolve()
        if (candidate == root or root in candidate.parents) and candidate.is_file():
            return FileResponse(candidate)
    return FileResponse(root / "index.html")


# 크롬 확장(MV3) 소스 — 패키지에 함께 실린다 (Docker·휠 모두 chunchugwan 포함)
_EXTENSION_DIR = Path(__file__).resolve().parent.parent / "extension"


def _build_extension_zip() -> bytes:
    """chunchugwan/extension/ 를 요청 시 zip 으로 묶는다.

    manifest 의 version 은 확장 정본(서버 앱 버전과 독립 버저닝)이라 덮어쓰지 않고
    원본 그대로 담는다. 크롬은 자체호스팅 .crx 드래그 설치를 막으므로 unpacked ZIP
    으로 주고, 사용자는 압축 해제 후 '개발자 모드 → 압축해제된 확장 프로그램을
    로드' 로 설치한다.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(_EXTENSION_DIR.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(_EXTENSION_DIR).as_posix())
    return buf.getvalue()


@app.get("/extension/download")
def extension_download(request: Request) -> Response:
    """크롬 확장(unpacked) ZIP 다운로드 — 세션 인증. 압축 해제 후 개발자 모드 로드."""
    _require_viewer(request)
    if not _EXTENSION_DIR.is_dir():
        raise HTTPException(404, t(request, "확장 파일을 찾을 수 없습니다"))
    filename = f"wccg-chrome-extension-v{api_routes._extension_version()}.zip"
    return Response(
        content=_build_extension_zip(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---- 확장(크롬) 전용 진입 경로 (용도별 분리) ----
# 확장은 SPA 화면 구조(중첩 라우트 등)를 직접 알지 못하도록 /extension/* 진입점만
# 쓰고, 여기서 정식 SPA 라우트로 302 한다 — 화면 경로가 바뀌어도 확장은 무관하다.

@app.get("/extension/page/{page_id}")
def extension_page(request: Request, page_id: int):
    """확장: 아카이브된 페이지 타임라인 열기 → 사이트 계층 정식 경로로 이동."""
    _require_viewer(request)
    with db.connect() as conn:
        page = db.get_page_by_id(conn, page_id)
    if page is None:
        raise HTTPException(404, t(request, "페이지 없음"))
    return RedirectResponse(
        f"/archive/sites/{page['site_id'] or 0}/page/{page_id}", status_code=302
    )


@app.get("/extension/crawl/{crawl_id}")
def extension_crawl(request: Request, crawl_id: int):
    """확장: 사이트 아카이브(크롤) 회차 열기."""
    _require_viewer(request)
    return RedirectResponse(f"/crawls/{crawl_id}", status_code=302)


@app.get("/extension/needs-human")
def extension_needs_human(request: Request):
    """확장: 사람 확인 대기 목록 열기."""
    return RedirectResponse("/archive/needs-human", status_code=302)


@app.get("/extension/archives")
def extension_archives(request: Request):
    """확장: 내 아카이브(본인 요청 이력) 열기."""
    return RedirectResponse("/settings/archives", status_code=302)


@app.get("/extension/token")
def extension_token(request: Request):
    """확장: 개인 API Key(확장 토큰) 발급 폼 열기."""
    return RedirectResponse("/settings/api-keys#ext-token-form", status_code=302)


@app.get("/extension/go")
def extension_go(request: Request, url: str = ""):
    """확장: URL 로 아카이브 화면 열기 — 이미 있으면 타임라인, 없으면 새 아카이빙."""
    try:
        norm = storage.normalize_url(url) if url.strip() else ""
    except ValueError:
        norm = ""
    if norm:
        with db.connect() as conn:
            page = db.get_page(conn, norm)
        if page is not None:
            return RedirectResponse(
                f"/archive/sites/{page['site_id'] or 0}/page/{page['id']}",
                status_code=302,
            )
    return RedirectResponse(
        f"/archive/new?url={quote(url, safe='')}", status_code=302
    )


def _site_title(snap_rows) -> str | None:
    """사이트 스냅샷 중 최신 것부터 비정규화된 title 을 찾는다 (현재 타이틀).

    오류 페이지 캡처 등 title 없는 스냅샷이 끼면 직전 제목으로 폴백한다.
    snapshots.title 컬럼을 쓰므로 파일 IO 가 없다 — lookback 한도가 불필요하다.
    """
    for row in sorted(snap_rows, key=lambda r: r["taken_at"], reverse=True):
        if row["title"]:
            return row["title"]
    return None


# 사이트 상세의 표 페이징 단위 — 선택 가능한 표시 개수와 섹션별 기본값.
# 페이지·문서·실패한 작업 표가 각자 독립으로 페이징하되 같은 선택지를 쓴다.
_SITE_PAGES_PER_PAGE_CHOICES = (25, 50, 75, 100, 200)
_SITE_PAGES_PER_PAGE = 25
_SITE_DOCS_PER_PAGE = 25
_SITE_FAILED_PER_PAGE = 25
# 사이트 상세 쿼리 파라미터의 표준 순서 (URL·hidden 을 안정적으로 직렬화)
_SITE_PARAM_ORDER = ("page", "per_page", "dpage", "dper", "fpage", "fper")


def _failed_items(
    site_id: int, failed_logs: list, failed_crawl_pages: list,
) -> list[dict]:
    """실패한 작업을 직접 아카이빙 실패(로그)와 크롤 페이지 실패로 합쳐 시각
    내림차순으로 정렬한 dict 리스트 (사이트 상세 '실패한 작업' 페이징·렌더용).

    kind='log' 행은 페이지 링크·진행/대기 배지(page_url 기준)와 로그 재시도를,
    kind='crawl' 행은 크롤 회차 링크와 크롤 페이지 재시도를 쓴다. at 은 정렬·
    표시용 시각(로그=started_at, 크롤=failed_at, 없으면 None → 맨 뒤)."""
    items: list[dict] = []
    for f in failed_logs:
        items.append({
            "kind": "log", "page_id": f["page_id"], "page_url": f["page_url"],
            "url": f["url"], "at": f["started_at"], "source": f["source"],
            "error": f["error"],
            "retry_action": f"/sites/{site_id}/failed/{f['id']}/retry",
        })
    for f in failed_crawl_pages:
        items.append({
            "kind": "crawl", "url": f["url"], "at": f["failed_at"],
            "crawl_id": f["crawl_id"], "error": f["error"],
            "retry_action": f"/sites/{site_id}/crawl-failed/{f['id']}/retry",
        })
    items.sort(key=lambda x: x["at"] or "", reverse=True)
    return items


@app.get("/sites/{site_id}/certificates/{cert_id}.pem")
def site_certificate_pem(request: Request, site_id: int, cert_id: int):
    """보관된 인증서 PEM 다운로드 — 사이트 소속 행만, 항상 첨부파일로."""
    _require_viewer(request)
    with db.connect() as conn:
        cert = db.get_site_certificate(conn, site_id, cert_id)
    if cert is None:
        raise HTTPException(404, t(request, "인증서 없음"))
    filename = f"{cert['host'].replace(':', '_')}-{cert['fingerprint'][:12]}.pem"
    return PlainTextResponse(
        cert["pem"],
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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


# ---- 사이트 자격증명 SPA JSON API (/api/web/sites/{id}/credentials) ----
# SSR /sites/{id}/credentials 와 같은 코어(credentials·crypto·HAR 파서)를 재사용.


@app.get("/api/web/sites/{site_id}/credentials")
def api_credentials_list(request: Request, site_id: int) -> dict:
    """사이트 로그인 자격증명 목록 + 메타 (비밀 제외). 자격증명 관리 권한 전용."""
    _require_credentials(request)
    with db.connect() as conn:
        site = db.get_site(conn, site_id)
        if site is None:
            raise HTTPException(404, t(request, "사이트 없음"))
        rows = db.list_site_credentials(conn, site_id)
    return {
        "site": {"id": site["id"], "site_key": site["site_key"]},
        "credentials": [
            {"id": c["id"], "label": c["label"], "kind": c["kind"],
             "kind_label": credentials.kind_label(c["kind"]),
             "creator_email": c["creator_email"], "created_at": c["created_at"]}
            for c in rows
        ],
        "kinds": [
            {"value": k, "label": credentials.kind_label(k)} for k in credentials.KINDS
        ],
        "secret_key_configured": crypto.is_configured(),
    }


@app.post("/api/web/sites/{site_id}/credentials")
def api_credentials_create(
    request: Request,
    site_id: int,
    label: str = Form(""),
    kind: str = Form(""),
    username: str = Form(""),
    password: str = Form(""),
    storage_state: str = Form(""),
    token: str = Form(""),
    har_file: UploadFile | None = File(None),
) -> dict:
    """자격증명 등록 (JSON 응답). SSR site_credentials_create 와 같은 검증·암호화."""
    _require_credentials(request)
    if not crypto.is_configured():
        raise HTTPException(
            400, t(request, "WCCG_SECRET_KEY 가 설정되지 않아 자격증명을 저장할 수 없습니다."))
    label = label.strip()
    label_error = credentials.validate_label(label)
    if label_error is not None:
        raise HTTPException(400, t(request, label_error))
    if kind not in credentials.KINDS:
        raise HTTPException(400, t(request, "잘못된 자격증명 종류입니다."))
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
            {"username": username, "password": password,
             "storage_state": storage_state, "token": token},
        )
    except credentials.CredentialError as e:
        raise HTTPException(400, t(request, str(e)))
    with db.connect() as conn:
        if db.get_site_credential_by_label(conn, site_id, label) is not None:
            raise HTTPException(400, t(request, "이미 있는 이름입니다: {name}", name=label))
        credentials.add(
            conn, site_id, label, kind, payload,
            created_by=request.state.user["id"] if request.state.user else None,
        )
    audit.log(
        request, "사이트 자격증명 등록: %s '%s' (%s)", site["site_key"], label, kind)
    return {"ok": True}


@app.post("/api/web/sites/{site_id}/credentials/{cred_id}/delete")
def api_credentials_delete(request: Request, site_id: int, cred_id: int) -> dict:
    """자격증명 삭제 (JSON). 자격증명 관리 권한 전용."""
    _require_credentials(request)
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
    return {"ok": True}


@app.get("/api/web/archive/credentials")
def api_archive_credentials(request: Request, url: str = "") -> dict:
    """입력 URL 도메인(사이트)의 자격증명 목록 — 새 아카이빙 폼의 '연결' 선택용.

    비밀은 내려보내지 않는다(id·라벨·종류만). 자격증명 관리 권한 전용.
    """
    _require_credentials(request)
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
                    {"id": c["id"], "label": c["label"], "kind": c["kind"],
                     "kind_label": credentials.kind_label(c["kind"])}
                    for c in db.list_site_credentials(conn, site["id"])
                ]
    return {
        "site_key": site_key,
        "credentials": creds,
        "kinds": [
            {"value": k, "label": credentials.kind_label(k)} for k in credentials.KINDS
        ],
        "secret_key_configured": crypto.is_configured(),
    }


def resolve_archive_credential(
    request: Request, norm: str, *,
    existing_id: str, kind: str, label: str,
    username: str, password: str, storage_state: str, token: str,
) -> tuple[int | None, str | None]:
    """새 아카이빙 폼의 자격증명 선택을 해석 — (연결할 credential_id, 오류) 반환.

    자격증명 관리 권한이 없으면 무시(None, None). existing_id 가:
    - ""        : 연결 안 함
    - "__new__" : 새 자격증명을 만들어 그 id 를 연결 (HAR 업로드는 사이트 화면에서)
    - 숫자       : 그 기존 자격증명을 연결 (URL 도메인 소속인지 검증)
    """
    if config.AUTH_ENABLED and not permissions.can_manage_credentials(
        getattr(request.state, "user", None)
    ):
        return None, None
    existing_id = (existing_id or "").strip()
    if not existing_id:
        return None, None
    site_key = storage.site_key(norm)
    if existing_id == "__new__":
        if not crypto.is_configured():
            return None, t(request,
                           "WCCG_SECRET_KEY 가 설정되지 않아 자격증명을 저장할 수 없습니다.")
        label = (label or "").strip()
        label_error = credentials.validate_label(label)
        if label_error is not None:
            return None, t(request, label_error)
        if kind not in credentials.KINDS:
            return None, t(request, "잘못된 자격증명 종류입니다.")
        try:
            payload = credentials.build_payload(
                kind, {"username": username, "password": password,
                       "storage_state": storage_state, "token": token},
            )
        except credentials.CredentialError as e:
            return None, t(request, str(e))
        with db.connect() as conn:
            site_id = db.get_or_create_site(conn, site_key)
            if db.get_site_credential_by_label(conn, site_id, label) is not None:
                return None, t(request, "이미 있는 이름입니다: {name}", name=label)
            cred_id = credentials.add(
                conn, site_id, label, kind, payload,
                created_by=request.state.user["id"] if request.state.user else None,
            )
        audit.log(request, "새 아카이빙에 자격증명 생성·연결: %s '%s' (%s)",
                  site_key, label, kind)
        return cred_id, None
    if not existing_id.isdigit():
        return None, t(request, "잘못된 자격증명 선택입니다.")
    cred_id = int(existing_id)
    with db.connect() as conn:
        site = db.get_site_by_key(conn, site_key)
        cred = db.get_site_credential(conn, cred_id)
        if site is None or cred is None or cred["site_id"] != site["id"]:
            return None, t(request, "이 도메인에 등록된 자격증명이 아닙니다.")
    audit.log(request, "새 아카이빙에 기존 자격증명 연결: %s (cred #%d)", site_key, cred_id)
    return cred_id, None


@app.get("/api/web/active")
def api_archive_active(request: Request) -> dict:
    """진행 중 아카이빙 URL + 사람 확인 대기 (SPA 목록 폴링·전역 배너).

    관리자에게는 needs_human(사람 확인 대기) 작업도 내려보낸다 — serve 의
    LIVE_CHALLENGE 설정과 무관하게 DB 사실 기준. url 코드포인트 순 정렬로
    폴링이 재정렬 없이 비교하게 한다(비-BMP 문자 무한 새로고침 방지)."""
    _require_viewer(request)
    data: dict = {"active": sorted(_active_snapshot())}
    if permissions.can_manage_system(getattr(request.state, "user", None)):
        data["needs_human"] = [
            {"id": i, "url": u} for u, i in sorted(_needs_human_urls(request).items())
        ]
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
    if permissions.can_view_authenticated_all(user):
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
        return (user["id"], permissions.can_view_authenticated_all(user))
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


@app.get("/snapshot/{snapshot_id}/file/{name}")
def snapshot_file(request: Request, snapshot_id: int, name: str):
    _require_viewer(request)
    candidates = _ALLOWED_FILES.get(name)
    if candidates is None:
        raise HTTPException(404, t(request, "허용되지 않은 파일"))
    snap = _load_snapshot(request, snapshot_id)
    snap_dir = _snapshot_dir(snap)
    store = config.blob_store()
    for filename, media_type in candidates:
        path = snap_dir / filename
        if store.is_file(path):  # 존재 확인만 (S3 는 HEAD, 객체 미다운로드)
            break
    else:
        raise HTTPException(404, t(request, "파일 없음"))
    # 스냅샷은 불변(원칙 2) — snapshot_id+name 은 항상 같은 바이트를 가리키므로
    # 브라우저가 무기한 캐시하게 둔다 (인증 뒤라 private). 매번 재전송 방지.
    headers = {"Cache-Control": "private, max-age=31536000, immutable"}
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
    # 서빙 시점에만 로컬로 materialize (로컬 백엔드는 identity → 경로·헤더 무변경)
    return FileResponse(store.local_path(path), media_type=media_type, headers=headers)


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
    _require_viewer(request)
    snap = _load_snapshot(request, snapshot_id)
    snap_dir = _snapshot_dir(snap)
    store = config.blob_store()
    try:
        meta = storage.read_meta(snap_dir)
    except OSError:
        raise HTTPException(404, t(request, "메타데이터 없음"))
    entry = next(
        (d for d in meta.documents or [] if d.get("file") == name), None
    )
    if entry is None:
        raise HTTPException(404, t(request, "허용되지 않은 파일"))
    audit.log(request, "문서 다운로드: %s (스냅샷 #%d)", name, snapshot_id,
              action="download", target=name)
    legacy = snap_dir / "files" / name
    if store.is_file(legacy):
        return _document_response(store.local_path(legacy), name)
    with db.connect() as conn:
        row = db.get_snapshot_document(conn, snapshot_id, name)
    sha = row["sha256"] if row else str(entry.get("sha256") or "")
    cas_name = documents.cas_name(sha, name)
    if cas_name is not None:
        path = documents.cas_path(cas_name)
        if store.is_file(path):
            return _document_response(store.local_path(path), name)
    raise HTTPException(404, t(request, "파일 없음"))


_DOCUMENTS_PER_PAGE = 100


# 구형(files/) 문서 잔존 여부 — /documents 의 "compact 안내" 배너용 파생 힌트.
# 전체 스냅샷 디렉토리를 walk 하므로(legacy 가 없으면 short-circuit 도 안 됨)
# 루트별 짧은 TTL 로 캐시한다. compact 후 배너가 사라지기까지 최대 TTL 지연은 허용.
_LEGACY_DOCS_TTL_SECONDS = 60
_legacy_docs_cache: "tuple[float, str, bool] | None" = None


def _legacy_documents_pending() -> bool:
    """구형 files/ 문서가 남아 있는지 (배너용, 루트별 TTL 캐시)."""
    global _legacy_docs_cache
    root = str(config.ARCHIVE_ROOT)
    now = time.monotonic()
    cached = _legacy_docs_cache
    if cached is not None and cached[1] == root and now - cached[0] < _LEGACY_DOCS_TTL_SECONDS:
        return cached[2]
    pending = any(documents.has_legacy_documents(d) for d in resources.snapshot_dirs())
    _legacy_docs_cache = (now, root, pending)
    return pending


_SEARCH_PER_PAGE = 20


@app.get("/document/{sha256}/{name}")
def document_download(request: Request, sha256: str, name: str):
    """문서 CAS 파일 다운로드 — DB 에 기록된 (sha256, 파일명) 조합만 허용.

    문서 목록 화면의 다운로드 링크용. 인증 게이트를 그대로 거치며 항상
    첨부파일 다운로드로 내려준다 (snapshot_document 와 동일한 보안 성질).
    """
    _require_viewer(request)
    cas_name = documents.cas_name(sha256, name)
    if cas_name is None:
        raise HTTPException(404, t(request, "허용되지 않은 파일"))
    with db.connect() as conn:
        row = db.find_document(conn, sha256, name)
    if row is None:
        raise HTTPException(404, t(request, "허용되지 않은 파일"))
    store = config.blob_store()
    path = documents.cas_path(cas_name)
    if not store.is_file(path):
        raise HTTPException(404, t(request, "파일 없음"))
    audit.log(request, "문서 다운로드: %s", name, action="download", target=name)
    return _document_response(store.local_path(path), name)


@app.get("/resource/{name}")
def resource_file(request: Request, name: str):
    """page.html 이 참조하는 스냅샷 간 공유 자원(CAS) 서빙.

    인증 게이트 예외 경로 (auth_gate 참조). 콘텐츠 주소라 불변이므로
    영구 캐시를 허용하고, SVG 등에서 스크립트가 실행되지 않도록
    문서 컨텍스트를 샌드박스한다.
    """
    if not resources.is_valid_name(name):
        raise HTTPException(404, t(request, "잘못된 자원 이름"))
    store = config.blob_store()
    path = resources.resource_path(name)
    if not store.is_file(path):  # 존재 확인만 (S3 는 HEAD)
        raise HTTPException(404, t(request, "자원 없음"))
    # 서빙 시점에 로컬로 materialize (로컬 백엔드는 identity)
    served = store.local_path(path)
    headers = {
        "Content-Security-Policy": "sandbox",
        "Cache-Control": "public, max-age=31536000, immutable",
    }
    # CSS 는 gzip 으로 저장된다 (resources._store_css). 구형 아카이브의
    # 비압축 .css 와 공존하므로 매직 바이트로 판별한다 (materialize 된 로컬 파일).
    if name.endswith(".css") and resources.is_gzipped(served):
        headers["Content-Encoding"] = "gzip"
    return FileResponse(
        served,
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


@app.get("/diff/{page_id}/shotdiff")
def shotdiff(
    request: Request,
    page_id: int,
    from_idx: int | None = Query(None, alias="from"),
    to_idx: int | None = Query(None, alias="to"),
):
    """픽셀 diff 하이라이트 이미지 (캐시에서 서빙)."""
    _require_viewer(request)
    page, _snaps, _f, _t, old_snap, new_snap = _resolve_diff_pair(
        request, page_id, from_idx, to_idx
    )
    old_shot_path, new_shot_path = _screenshot_paths(page, old_snap, new_snap)
    if old_shot_path is None or new_shot_path is None:
        raise HTTPException(404, t(request, "스크린샷 없음"))
    # differ 는 PIL 로 경로를 직접 연다 — 서빙 시점에 로컬로 materialize 해 넘긴다
    # (로컬 백엔드는 identity). 결과 하이라이트는 로컬 CACHE_DIR 에 저장된다.
    store = config.blob_store()
    _ratio, out_png = differ.cached_screenshot_diff(
        store.local_path(old_shot_path), store.local_path(new_shot_path),
        f"shotdiff-{old_snap['id']}-{new_snap['id']}"
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
    protect: bool | None = None,
    site_protect_default: bool | None = None,
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
            protect=protect, site_protect_default=site_protect_default,
        )


def _require_viewer(request: Request) -> None:
    """아카이브 자원(스냅샷 파일·문서·인증서·diff·확장) 접근 가드 — 세션 로그인 필수.

    C2 컷오버 전에는 auth_gate 미들웨어가 모든 비공개 HTML 경로를 로그인으로
    보냈으나, SPA 가 루트로 서빙되면서 미들웨어는 더 이상 경로별 리다이렉트를
    하지 않는다. 따라서 아카이브 콘텐츠를 직접 서빙하는 자원 라우트는 여기서
    로그인(또는 확장 토큰)을 직접 강제한다 — `/resource/` CAS 만 예외(원칙 5,
    샌드박스 하위 요청엔 쿠키가 안 붙어 콘텐츠 주소 + 화이트리스트로만 서빙).
    인증 off(loopback)면 단일 사용자 로컬 도구라 전부 허용한다."""
    if not config.AUTH_ENABLED:
        return
    if request.state.user is not None:
        return
    if getattr(request.state, "api_key", None) is not None:
        return
    raise HTTPException(401, t(request, "로그인이 필요합니다"))


def _require_archiver(request: Request) -> None:
    """아카이빙 권한 가드 (admin/archiver). 보기 전용·차단 계정은 403."""
    if not permissions.can_archive(request.state.user):
        raise HTTPException(403, t(request, "아카이빙 권한이 없습니다"))


def _require_not_migrating(request: Request) -> None:
    """이전(마이그레이션) 모드 가드 — 데이터 이전 중에는 새 아카이빙·재아카이빙을 막는다.

    워커 게이트가 실제 처리를 이미 막지만(큐에 쌓여도 안 돌아감), 여기서 등록
    자체를 막아 사용자에게 즉시 안내한다. 이전 모드는 시스템 설정에서 끈다.
    """
    with db.connect() as conn:
        if db.migration_mode_enabled(conn):
            raise HTTPException(
                409,
                t(request, "이전(마이그레이션) 모드입니다 — 데이터 이전 중에는 "
                           "아카이빙할 수 없습니다. 시스템 설정에서 이전 모드를 끄세요."),
            )


def _require_credentials(request: Request) -> None:
    """자격증명 관리 가드 (manage_credentials). 사이트 로그인 자격증명 CRUD·연결용."""
    if not permissions.can_manage_credentials(request.state.user):
        raise HTTPException(403, t(request, "자격증명 관리 권한이 없습니다"))


def _require_admin(request: Request) -> None:
    """시스템 관리 가드 (manage_system) — 라이브 챌린지 등 시스템 운영 동작."""
    if not permissions.can_manage_system(request.state.user):
        raise HTTPException(403, t(request, "시스템 관리 권한이 없습니다"))


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


# ---- 라이브 챌린지 SPA JSON API (/api/web/live) ----
# SSR 라이브 라우트(위)와 같은 헬퍼·코어를 재사용하되 JSON 으로 응답한다.
# 컷오버(Phase C) 전까지 SSR /archive/jobs/{id}/live 와 공존한다.


class _LiveClick(BaseModel):
    x: int
    y: int
    kind: str = "click"
    delay_ms: int = 0


class _LiveKey(BaseModel):
    key: str
    kind: str = "text"
    delay_ms: int = 0


@app.get("/api/web/live")
def api_live_list(request: Request) -> dict:
    """사람 확인 대기 작업 목록 (관리자 전용) — SPA needs-human 화면."""
    _require_admin(request)
    uid = request.state.user["id"] if request.state.user else None
    with db.connect() as conn:
        jobs = db.list_needs_human_jobs(conn)
    return {
        "jobs": [
            {"id": j["id"], "url": j["url"],
             "needs_human_at": j["needs_human_at"],
             # 다른 admin 이 이미 처리 중인지 — 목록에서 '처리 중' 배지로 안내
             "held_by_other": (
                 j["live_owner_id"] is not None
                 and uid is not None
                 and j["live_owner_id"] != uid
             )}
            for j in jobs
        ]
    }


@app.get("/api/web/live/{job_id}")
def api_live_view(request: Request, job_id: int) -> dict:
    """라이브 세션 열기(클레임) — 화면 메타(URL·뷰포트·소유 여부)를 돌려준다."""
    _require_admin(request)
    job = _live_job_or_404(request, job_id)
    with db.connect() as conn:
        owned = db.claim_live_session(conn, job_id, request.state.user["id"])
        job = db.get_archive_job(conn, job_id)
    audit.log(request, "라이브 챌린지 처리 시작: %s", job["url"])
    return {
        "id": job_id, "url": job["url"], "owned": owned,
        "viewport_w": job["live_viewport_w"] or config.LIVE_VIEWPORT_W,
        "viewport_h": job["live_viewport_h"] or config.LIVE_VIEWPORT_H,
        "shot_interval_ms": config.LIVE_SHOT_INTERVAL_MS,
    }


@app.get("/api/web/live/{job_id}/state")
def api_live_state(request: Request, job_id: int) -> dict:
    """라이브 세션 상태 폴링 (관리자 전용). needs_human | done(통과·취소·종료)."""
    _require_admin(request)
    with db.connect() as conn:
        job = db.get_archive_job(conn, job_id)
    if job is None or not job["needs_human_at"]:
        return {"status": "done"}  # 통과·취소·종료 — 큐에서 빠졌거나 해제됨
    return {
        "status": "needs_human",
        "owned": job["live_owner_id"] == request.state.user["id"],
    }


@app.get("/api/web/live/{job_id}/shot")
def api_live_shot(request: Request, job_id: int):
    """라이브 스크린샷 (JPEG, 관리자 전용). live_token 으로만 경로를 조립한다."""
    _require_admin(request)
    job = _live_job_or_404(request, job_id)
    path = live_challenge.shot_path(job["live_token"])
    if not path.exists():
        raise HTTPException(404, t(request, "아직 화면이 준비되지 않았습니다"))
    return FileResponse(
        path, media_type="image/jpeg", headers={"Cache-Control": "no-store"},
    )


@app.post("/api/web/live/{job_id}/click")
def api_live_click(request: Request, job_id: int, body: _LiveClick) -> dict:
    """클릭/드래그 좌표를 명령 큐에 넣는다 (JSON). 소유 admin 만."""
    _require_admin(request)
    job = _live_job_or_404(request, job_id)
    _live_owner_or_403(request, job)
    if body.kind not in ("click", "move", "down", "up"):
        raise HTTPException(400, "kind")
    with db.connect() as conn:
        db.enqueue_live_command(
            conn, job["live_token"], kind=body.kind, x=body.x, y=body.y,
            delay_ms=max(0, body.delay_ms))
    return {"ok": True}


@app.post("/api/web/live/{job_id}/key")
def api_live_key(request: Request, job_id: int, body: _LiveKey) -> dict:
    """키 입력/문자열을 명령 큐에 넣는다 (JSON). 소유 admin 만."""
    _require_admin(request)
    job = _live_job_or_404(request, job_id)
    _live_owner_or_403(request, job)
    if body.kind not in ("key", "text"):
        raise HTTPException(400, "kind")
    with db.connect() as conn:
        db.enqueue_live_command(
            conn, job["live_token"], kind=body.kind, key=body.key[:200],
            delay_ms=max(0, body.delay_ms))
    return {"ok": True}


@app.post("/api/web/live/{job_id}/cancel")
def api_live_cancel(request: Request, job_id: int) -> dict:
    """라이브 세션 취소 (JSON) — worker 가 다음 폴링에 중단·실패 처리."""
    _require_admin(request)
    job = _live_job_or_404(request, job_id)
    _live_owner_or_403(request, job)
    with db.connect() as conn:
        db.set_live_cancel(conn, job_id)
    audit.log(request, "라이브 챌린지 취소: %s", job["url"])
    return {"ok": True}


@app.post("/api/web/live/{job_id}/solve")
def api_live_solve(request: Request, job_id: int) -> dict:
    """사람 확인 완료 — 강제 진행 (JSON). SSR live_force_solve 와 동일."""
    _require_admin(request)
    job = _live_job_or_404(request, job_id)
    _live_owner_or_403(request, job)
    with db.connect() as conn:
        db.set_live_force_solve(conn, job_id)
    audit.log(request, "사람 확인 완료(강제 진행): %s", job["url"])
    return {"ok": True}


_BUSY_MSG = "아카이빙이 진행 중인 페이지입니다 — 완료 후 다시 시도하세요"


# ---- 사이트 전체 아카이브 (크롤) ----


def _load_crawl(request: Request, crawl_id: int):
    with db.connect() as conn:
        crawl = db.get_crawl(conn, crawl_id)
    if crawl is None:
        raise HTTPException(404, t(request, "크롤 없음"))
    return crawl


_CRAWL_PAGE_STATUSES = ("pending", "in_progress", "done", "failed")


# ---- 크롤 회차 SPA JSON API (/api/web/crawls) ----
# SSR /crawls/{id} 와 같은 코어를 재사용하는 JSON 래퍼. 상세 GET 은 세션이면
# 누구나, 액션(취소·재시도·재실행)은 archiver 권한.


@app.get("/api/web/crawls/{crawl_id}")
def api_crawl_view(request: Request, crawl_id: int, status: str = "") -> dict:
    """크롤 회차 상세 — 상태별 집계·페이지 목록·재시도 정책 (SPA)."""
    _require_viewer(request)
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
    return {
        "crawl": dict(crawl),
        "counts": counts,
        "pages": [dict(p) for p in pages],
        "network_tag": dict(network_tag) if network_tag else None,
        "status_filter": status_filter,
        "retry_backoff_labels": [i18n.interval_label(request, s) for s in backoff],
        "max_attempts": len(backoff) + 1,
        "can_archive": permissions.can_archive(getattr(request.state, "user", None)),
    }


@app.post("/api/web/crawls/{crawl_id}/cancel")
def api_crawl_cancel(request: Request, crawl_id: int) -> dict:
    """크롤 취소 (JSON). archiver 전용."""
    _require_archiver(request)
    crawl = _load_crawl(request, crawl_id)
    with db.connect() as conn:
        db.cancel_crawl(conn, crawl_id)
    audit.log(request, "크롤 취소: #%d (%s)", crawl_id, crawl["start_url"])
    return {"ok": True}


@app.post("/api/web/crawls/{crawl_id}/retry")
def api_crawl_retry(request: Request, crawl_id: int) -> dict:
    """실패 페이지 일괄 재시도 (JSON). archiver 전용."""
    _require_archiver(request)
    crawl = _load_crawl(request, crawl_id)
    with db.connect() as conn:
        db.retry_failed_crawl_pages(conn, crawl_id)
    audit.log(
        request, "크롤 실패 페이지 일괄 재시도: #%d (%s)", crawl_id, crawl["start_url"])
    return {"ok": True}


@app.post("/api/web/crawls/{crawl_id}/pages/{crawl_page_id}/retry")
def api_crawl_page_retry(request: Request, crawl_id: int, crawl_page_id: int) -> dict:
    """실패한 크롤 페이지 하나 재시도 (JSON). archiver 전용."""
    _require_archiver(request)
    _load_crawl(request, crawl_id)
    with db.connect() as conn:
        row = db.get_failed_crawl_page(conn, crawl_id, crawl_page_id)
        if row is None:
            raise HTTPException(404, t(request, "실패 기록 없음"))
        db.retry_failed_crawl_page(conn, crawl_page_id)
    audit.log(request, "크롤 페이지 재시도: %s", row["url"])
    return {"ok": True}


@app.post("/api/web/sites/{site_id}/crawls/{crawl_id}/rerun")
def api_crawl_rerun(request: Request, site_id: int, crawl_id: int) -> dict:
    """크롤 회차를 같은 옵션으로 다시 실행 (JSON) → 새 크롤 id 반환. archiver 전용."""
    _require_archiver(request)
    _require_not_migrating(request)
    with db.connect() as conn:
        crawl = db.get_crawl(conn, crawl_id)
        if crawl is None or crawl["site_id"] != site_id:
            raise HTTPException(404, t(request, "크롤 없음"))
    try:
        new_crawl, merged = crawler.start_crawl(
            crawl["start_url"],
            max_pages=crawl["max_pages"], max_depth=crawl["max_depth"],
            delay_seconds=crawl["delay_seconds"], source="web",
            network_tag_id=crawl["network_tag_id"],
            credential_id=crawl["credential_id"],
        )
    except ValueError as exc:
        raise HTTPException(400, t(request, "아카이빙 실패: {e}", e=exc))
    audit.log(
        request, "사이트 아카이브 다시 실행: %s → 크롤 #%d%s",
        crawl["start_url"], new_crawl["id"],
        " (진행 중인 크롤로 병합)" if merged else "",
    )
    return {"crawl_id": new_crawl["id"], "merged": bool(merged)}


@app.get("/api/web/crawls/{crawl_id}/status")
def api_crawl_status(request: Request, crawl_id: int) -> dict:
    """크롤 진행 상태 JSON (SPA 진행 화면 폴링용)."""
    _require_viewer(request)
    crawl = _load_crawl(request, crawl_id)
    with db.connect() as conn:
        counts = db.crawl_page_counts(conn, crawl_id)
    return {"status": crawl["status"], "counts": counts}


# ---- 사이트 아카이브 스케줄 (주기적 재크롤) ----


@app.get("/crawl/{crawl_id}/goto")
def crawl_goto(request: Request, crawl_id: int, url: str):
    """아카이브 내 링크 리졸버 — 재작성된 page.html 앵커의 목적지.

    같은 크롤 세트에서 확인된 스냅샷 → 해당 URL 의 최신 스냅샷 순으로
    찾아 리다이렉트하고, 없으면 원본 링크를 안내하는 화면을 보여준다
    (라이브 사이트로 조용히 새지 않는다).
    """
    _require_viewer(request)
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
    # 아카이브에 없는 링크 — 라이브 사이트로 조용히 새지 않도록 안내 화면을 보여준다.
    # SPA 셸이 아니라 자체완결 HTML(스크립트 없음)로, 사용자가 직접 클릭한 top-navigation
    # 목적지라 대시보드 컨텍스트에서 안전하게 렌더된다.
    esc_url = html_escape(norm)
    esc_start = html_escape(crawl["start_url"])
    title = t(request, "아카이브에 없는 페이지")
    body = t(
        request,
        "이 링크의 페이지는 아카이브되지 않았습니다 — 크롤 범위 밖이거나 아직/끝내 저장되지 않았습니다.",
    )
    crawl_label = t(request, "크롤")
    open_label = t(request, "원본 페이지 열기 (라이브 사이트)")
    page_html = (
        "<!doctype html><html lang=\"ko\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{html_escape(title)} — 춘추관</title>"
        "<style>body{font:14px/1.6 system-ui,sans-serif;max-width:760px;margin:48px auto;"
        "padding:0 16px;color:#222}a{color:#c2410c}.mono{font-family:ui-monospace,monospace}"
        "table{border-collapse:collapse;margin:16px 0}th{text-align:left;padding:4px 12px 4px 0;"
        "vertical-align:top;color:#666}td{padding:4px 0}</style></head><body>"
        f"<h2>{html_escape(title)}</h2><p>{html_escape(body)}</p>"
        "<table><tbody>"
        f"<tr><th>URL</th><td class=\"mono\">{esc_url}</td></tr>"
        f"<tr><th>{html_escape(crawl_label)}</th><td>"
        f"<a href=\"/crawls/{crawl['id']}\" class=\"mono\">{esc_start}</a></td></tr>"
        "</tbody></table>"
        f"<p><a href=\"{esc_url}\" rel=\"noopener noreferrer\">{html_escape(open_label)}</a></p>"
        "</body></html>"
    )
    return HTMLResponse(page_html, status_code=404)


# ── SPA 루트 catch-all (반드시 마지막 등록) ──────────────────────────────────
# Starlette 는 등록 순서로 매칭한다 — 위의 모든 데이터/자원/JSON 라우트와
# include_router 로 먼저 등록된 /api/* 가 우선 잡히고, 그 외 경로만 여기로 떨어져
# SPA 셸(index.html)을 받는다. 딥링크·새로고침이 클라이언트 라우팅으로 복원된다.
@app.get("/{full_path:path}", include_in_schema=False)
def spa(full_path: str = "") -> Response:
    """미매칭 경로 → SvelteKit SPA 셸(또는 dist 실파일).

    미매칭 /api 경로는 SPA HTML 대신 404 JSON 으로 돌려준다 — 잘못된 API 호출이
    index.html 을 받지 않게 한다 (실존 /api/* 는 위에서 이미 매칭됨).
    """
    if full_path == "api" or full_path.startswith("api/"):
        raise HTTPException(404, "Not Found")
    return _serve_spa(full_path)
