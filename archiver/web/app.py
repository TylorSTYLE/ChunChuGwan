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

import logging
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
)

from .. import auth, config, db, differ, pipeline, storage
from . import auth_routes
from .templating import templates

logger = logging.getLogger(__name__)

app = FastAPI(title="Web Archiver")
app.include_router(auth_routes.router)

# 인증 없이 접근 가능한 경로 (로그인 절차 자체 + 헬스체크)
_PUBLIC_PATHS = {"/healthz", "/login", "/login/totp", "/signup"}


@app.middleware("http")
async def auth_gate(request: Request, call_next):
    """인증 게이트 + CSRF 방어.

    - POST 는 Origin(없으면 Referer) 호스트가 Host 와 일치해야 한다.
      (쿠키는 SameSite=Lax 라 이중 방어)
    - AUTH_ENABLED 면 공개 경로 외에는 active 세션 필수.
      미인증 HTML 요청은 401 대신 /login?next= 으로 보낸다.
    """
    request.state.user = None
    request.state.session = None

    if request.method == "POST":
        origin = request.headers.get("origin") or request.headers.get("referer")
        if origin and urlsplit(origin).netloc != request.headers.get("host", ""):
            return PlainTextResponse("CSRF 검증 실패", status_code=403)

    if config.AUTH_ENABLED:
        token = request.cookies.get(config.SESSION_COOKIE, "")
        if token:
            with db.connect() as conn:
                sess = auth.resolve_session(conn, token)
                if sess is not None:
                    request.state.session = sess
                    if sess["state"] == "active":
                        request.state.user = db.get_user_by_id(conn, sess["user_id"])

        path = request.url.path
        public = path in _PUBLIC_PATHS or path.startswith("/auth/oidc/")
        if request.state.user is None and not public:
            target = path + (f"?{request.url.query}" if request.url.query else "")
            return RedirectResponse(
                f"/login?next={quote(target, safe='')}", status_code=302
            )

    return await call_next(request)

# 스냅샷 디렉토리에서 서빙을 허용하는 파일 화이트리스트
_ALLOWED_FILES: dict[str, str] = {
    "page.html": "text/html; charset=utf-8",
    "screenshot.png": "image/png",
    "content.md": "text/plain; charset=utf-8",
}

_BADGES = {1: "changed", 0: "same"}


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    with db.connect() as conn:
        pages = db.list_pages(conn)
    return templates.TemplateResponse(request, "index.html", {"pages": pages})


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


def _rearchive(url: str) -> None:
    """백그라운드 재아카이빙. 실패는 로그만 남긴다."""
    try:
        outcome = pipeline.archive_url(url)
        logger.info("재아카이빙 완료: %s [%s]", url, outcome.status)
    except Exception:
        logger.exception("재아카이빙 실패: %s", url)


@app.post("/page/{page_id}/rearchive")
def rearchive(page_id: int, background: BackgroundTasks):
    with db.connect() as conn:
        page = db.get_page_by_id(conn, page_id)
    if page is None:
        raise HTTPException(404, "페이지 없음")
    background.add_task(_rearchive, page["url"])
    return RedirectResponse(url=f"/page/{page_id}?queued=1", status_code=303)
