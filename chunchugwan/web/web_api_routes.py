"""SvelteKit SPA 용 세션 인증 JSON API (`/api/web`).

대시보드 SPA 의 데이터 소스. 기존 SSR 라우트(app/auth/system_routes)와 같은
코어 모듈(db·storage·permissions)을 호출하되 HTML 대신 JSON 을 반환한다
(아키텍처 원칙 1 — 쓰기는 여전히 코어 모듈 경유).

인증: 세션 쿠키 기반. `auth_gate` 미들웨어가 `/api/` 를 인증 게이트에서
통과시키므로(샌드박스 자원·확장 토큰 경로와 공유), 이 라우터는 의존성
`require_session` 으로 세션을 직접 강제한다. 확장용 API 키 인증 라우터
(`/api/v1`, api_routes.py)와는 별개다.

CSRF: `/api/v1` 과 달리 이 라우터의 POST 는 `auth_gate` 의 Origin 검사 대상이다
(SPA 가 FastAPI 와 same-origin 이라 통과). prefix 를 `/api/web` 으로 두는 이유.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from .. import (
    __version__, config, db, differ, documents, resources, searchindex, storage,
)
from . import i18n, permissions
from .templating import _auth_context

router = APIRouter(prefix="/api/web")


def _snapshot_viewer(request: Request) -> "tuple[int | None, bool] | None":
    """집계에서 인증 스냅샷을 가릴 기준 — (viewer_id, is_admin). app._snapshot_viewer 와 동일.

    인증 off 면 None(전체 허용). 세션 사용자는 (본인 id, view_authenticated_all 여부).
    세션 라우터라 API 키 경로는 없다.
    """
    if not config.AUTH_ENABLED:
        return None
    user = getattr(request.state, "user", None)
    if user is not None:
        return (user["id"], permissions.can_view_authenticated_all(user))
    return (None, False)


def _may_view_authenticated(request: Request, snap: sqlite3.Row) -> bool:
    """로그인 캡처 스냅샷 열람 가능 여부 — 소유자 또는 관리자만. app._may_view_authenticated 와 동일."""
    if not config.AUTH_ENABLED:
        return True
    owner_id = snap["authenticated_by"]
    user = getattr(request.state, "user", None)
    if permissions.can_view_authenticated_all(user):
        return True
    if owner_id is not None and user is not None and user["id"] == owner_id:
        return True
    return False


def require_session(request: Request) -> sqlite3.Row | None:
    """세션 인증 강제 — 미인증이면 401 JSON. 인증 off(loopback)면 None 허용.

    `auth_gate` 가 차단·승인대기 계정은 이미 걸러내므로(403/redirect 는
    HTML 전용이나, 미들웨어가 active 사용자만 request.state.user 에 싣는다)
    여기서는 active 세션 유무만 본다.
    """
    if not config.AUTH_ENABLED:
        return None  # loopback 단일 사용자 — 전부 허용
    from fastapi import HTTPException

    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(401, "인증이 필요합니다")
    return user


def _user_public(user: sqlite3.Row | None) -> dict | None:
    """클라이언트에 노출할 사용자 정보 — 인증 비밀(해시 등)은 절대 포함하지 않는다."""
    if user is None:
        return None
    return {
        "email": user["email"],
        "display_name": user["display_name"],
        "role": user["role"],
        "locale": user["locale"],
        "timezone": user["timezone"],
    }


@router.get("/me")
def me(request: Request, user: sqlite3.Row | None = Depends(require_session)) -> dict:
    """현재 세션·권한 컨텍스트 — SPA 의 메뉴/버튼 노출 게이팅 소스.

    `_auth_context`(권한 플래그·needs-human)를 재사용해 SSR 헤더와 동일한
    노출 규칙을 SPA 에 그대로 전달한다 (서버 권한 가드는 각 엔드포인트에서 이중 유지).
    """
    ctx = _auth_context(request)
    return {
        "auth_enabled": config.AUTH_ENABLED,
        "authenticated": user is not None or not config.AUTH_ENABLED,
        "user": _user_public(user),
        "flags": {k: v for k, v in ctx.items() if isinstance(v, bool)},
        "locale": getattr(request.state, "locale", i18n.DEFAULT_LOCALE),
        "timezone": (user["timezone"] if user is not None else None) or "UTC",
        "needs_human": ctx["needs_human_jobs"],
        "needs_human_count": ctx["needs_human_count"],
        "version": __version__,
    }


# 대시보드 현황 집계 기간 — app.dashboard 와 같은 정의 (기간별 용량 트렌드).
_TREND_PERIODS = (
    ("today", "오늘"),
    ("week", "이번 주"),
    ("month", "이번 달"),
    ("year", "올해"),
)


def _period_starts(now: datetime) -> dict[str, str]:
    """현황 집계 기간의 시작 시각 (ISO 8601 UTC) — app._period_starts 와 동일."""
    from datetime import timedelta

    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    starts = {
        "today": today,
        "week": today - timedelta(days=today.weekday()),
        "month": today.replace(day=1),
        "year": today.replace(month=1, day=1),
        "recent": now - timedelta(hours=24),
    }
    return {k: v.isoformat(timespec="seconds") for k, v in starts.items()}


@router.get("/dashboard")
def dashboard(
    request: Request, user: sqlite3.Row | None = Depends(require_session)
) -> dict:
    """시스템 현황(첫 화면) — app.dashboard 의 JSON 판. 동일 db·storage 호출."""
    starts = _period_starts(datetime.now(timezone.utc))
    with db.connect() as conn:
        total_pages = db.count_pages(conn)
        total_sites = db.count_sites(conn)
        snap_dirs = db.list_snapshot_dirs(conn)
        recent_snaps = db.list_recent_snapshots(conn, limit=10)
        recent_logs = db.list_archive_logs(conn, limit=10)

    total_bytes = sum(storage.archive_disk_usage().values())

    sizes: dict[int, int] = {}
    counts = {k: 0 for k in starts}
    period_bytes = {k: 0 for k in starts}
    for row in snap_dirs:
        size = row["bytes"]
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

    return {
        "total_pages": total_pages,
        "total_sites": total_sites,
        "total_snapshots": len(snap_dirs),
        "total_bytes": total_bytes,
        "week_count": counts["week"],
        "recent_count": counts["recent"],
        "trend": trend,
        "recent_snaps": [
            {**dict(s), "bytes": sizes.get(s["id"], 0)} for s in recent_snaps
        ],
        "recent_logs": [dict(row) for row in recent_logs],
        "version": __version__,
    }


_BADGES = {1: "changed", 0: "same"}


def _site_title(snap_rows) -> str | None:
    """사이트 스냅샷 중 최신부터 비정규화 title 을 찾는다 — app._site_title 과 동일."""
    for row in sorted(snap_rows, key=lambda r: r["taken_at"], reverse=True):
        if row["title"]:
            return row["title"]
    return None


def _snapshot_dir(snap: sqlite3.Row):
    return storage.page_dir(snap["domain"], snap["slug"]) / snap["dir_name"]


def _load_snapshot(request: Request, snapshot_id: int) -> sqlite3.Row:
    """스냅샷 로드 + 인증 스냅샷 가시성 검사 — app._load_snapshot 과 동일(은폐 404)."""
    from fastapi import HTTPException

    with db.connect() as conn:
        snap = db.get_snapshot(conn, snapshot_id)
    if snap is None:
        raise HTTPException(404, "스냅샷 없음")
    if snap["authenticated"] and not _may_view_authenticated(request, snap):
        raise HTTPException(404, "스냅샷 없음")
    return snap


@router.get("/sites")
def sites(
    request: Request, user: sqlite3.Row | None = Depends(require_session)
) -> dict:
    """아카이브 목록(사이트 단위) — app.index 의 JSON 판. 진행 중 사이트를 위로."""
    with db.connect() as conn:
        rows = db.list_sites_overview(conn, viewer=_snapshot_viewer(request))
        snap_dirs = db.list_snapshot_dirs(conn)
        tag_rows = db.list_site_network_tags(conn)

    site_tags: dict[int, list[dict]] = {}
    for row in tag_rows:
        site_tags.setdefault(row["site_id"], []).append(
            {"id": row["id"], "name": row["name"], "description": row["description"]}
        )
    site_bytes: dict[int, int] = {}
    site_snaps: dict[int, list] = {}
    for row in snap_dirs:
        site_bytes[row["site_id"]] = site_bytes.get(row["site_id"], 0) + row["bytes"]
        site_snaps.setdefault(row["site_id"], []).append(row)
    titles = {sid: _site_title(r) for sid, r in site_snaps.items()}

    items = [
        {
            "site_id": s["id"],
            "site_key": s["site_key"],
            "page_count": s["page_count"],
            "snapshot_count": s["snapshot_count"],
            "crawl_count": s["crawl_count"],
            "schedule_count": s["schedule_count"],
            "bytes": site_bytes.get(s["id"], 0),
            "title": titles.get(s["id"]),
            "network_tags": site_tags.get(s["id"], []),
            "activity_at": s["last_activity_at"] or None,
            "crawling": s["running_crawl_count"] > 0,
            "active": s["running_crawl_count"] > 0,
        }
        for s in rows
    ]
    items.sort(key=lambda i: i["site_key"])
    items.sort(key=lambda i: i["activity_at"] or "", reverse=True)
    items.sort(key=lambda i: not i["active"])
    return {"items": items}


@router.get("/pages/{page_id}")
def page_timeline(
    request: Request,
    page_id: int,
    user: sqlite3.Row | None = Depends(require_session),
) -> dict:
    """타임라인 — 한 페이지의 스냅샷 이력. app.timeline 의 JSON 판."""
    from fastapi import HTTPException

    with db.connect() as conn:
        page = db.get_page_by_id(conn, page_id)
        if page is None:
            raise HTTPException(404, "페이지 없음")
        snaps = db.list_snapshots(conn, page_id)
        checks = db.list_checks(conn, page_id)
        schedule = db.get_schedule(conn, page_id)
        network_tag = (
            db.get_network_tag(conn, page["network_tag_id"])
            if page["network_tag_id"]
            else None
        )
        site = db.get_site(conn, page["site_id"]) if page["site_id"] else None
        snap_logs = db.list_snapshot_archive_logs(conn, page_id)

    visible = [
        s for s in snaps
        if not s["authenticated"] or _may_view_authenticated(request, s)
    ]
    if len(visible) != len(snaps):
        checks = []
    snaps = visible
    log_by_snap = {row["snapshot_id"]: row for row in snap_logs}

    items = []
    for i, s in enumerate(snaps, 1):
        badge = "new" if i == 1 else _BADGES[s["changed"]]
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
            "idx": i,
            "snap": dict(s),
            "badge": badge,
            "files": files,
            "total_bytes": sum(f["bytes"] for f in files) if files else None,
            "steps": steps,
            "log": dict(log) if log is not None else None,
        })

    return {
        "page": dict(page),
        "site": dict(site) if site is not None else None,
        "network_tag": dict(network_tag) if network_tag is not None else None,
        "schedule": (
            {
                **dict(schedule),
                "label": i18n.schedule_label(
                    request, schedule["interval_seconds"], schedule["run_at_time"]
                ),
            }
            if schedule is not None
            else None
        ),
        "snapshots": items,
        "checks": [dict(c) for c in checks],
        "can_archive": permissions.can_archive(user),
        "can_delete": permissions.can_delete(user),
    }


@router.get("/snapshots/{snapshot_id}")
def snapshot(
    request: Request,
    snapshot_id: int,
    user: sqlite3.Row | None = Depends(require_session),
) -> dict:
    """스냅샷 뷰어 메타 — 파일/iframe 서빙은 FastAPI(/snapshot/{id}/file/*, 원칙 5)에 그대로.

    SPA 는 page_html_url 을 <iframe sandbox> src 로만 쓴다.
    """
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
    snap_dir = _snapshot_dir(snap)
    return {
        "snap": dict(snap),
        "network_tag": dict(network_tag) if network_tag is not None else None,
        "title": title,
        "documents": documents,
        "page_html_url": f"/snapshot/{snapshot_id}/file/page.html",
        "screenshot_url": f"/snapshot/{snapshot_id}/file/screenshot",
        "mobile_screenshot_url": f"/snapshot/{snapshot_id}/file/screenshot-mobile",
        "content_url": f"/snapshot/{snapshot_id}/file/content.md",
        "has_screenshot": storage.find_screenshot(snap_dir) is not None,
        "has_mobile_screenshot": storage.find_mobile_screenshot(snap_dir) is not None,
    }


def _failed_items(site_id: int, failed_logs, failed_crawl_pages) -> list[dict]:
    """실패한 작업(직접 아카이빙 실패 + 크롤 페이지 실패)을 시각 내림차순 합본 — app._failed_items 와 동일."""
    items: list[dict] = []
    for f in failed_logs:
        items.append({
            "kind": "log", "page_id": f["page_id"], "page_url": f["page_url"],
            "url": f["url"], "at": f["started_at"], "source": f["source"],
            "error": f["error"],
        })
    for f in failed_crawl_pages:
        items.append({
            "kind": "crawl", "url": f["url"], "at": f["failed_at"],
            "crawl_id": f["crawl_id"], "error": f["error"],
        })
    items.sort(key=lambda x: x["at"] or "", reverse=True)
    return items


@router.get("/sites/{site_id}")
def site_detail(
    request: Request,
    site_id: int,
    page: int = 1,
    per_page: int = 50,
    user: sqlite3.Row | None = Depends(require_session),
) -> dict:
    """사이트 상세 — 페이지 목록(페이징) + 크롤/스케줄/문서/실패/네트워크 태그. app.site_view 의 JSON 판."""
    from fastapi import HTTPException

    per_page = per_page if per_page in (25, 50, 75, 100, 200) else 50
    with db.connect() as conn:
        site = db.get_site(conn, site_id)
        if site is None:
            raise HTTPException(404, "사이트 없음")
        totals = db.site_page_totals(conn, site_id)
        total_pages = max(1, -(-totals["page_count"] // per_page))
        page = min(max(page, 1), total_pages)
        pages = db.list_site_pages(
            conn, site_id, limit=per_page, offset=(page - 1) * per_page
        )
        snap_dirs = db.list_site_snapshot_dirs(conn, site_id)
        crawls = db.list_site_crawls(conn, site_id)
        schedules = db.list_site_schedules(conn, site_id)
        crawl_schedules = db.list_site_crawl_schedules(conn, site_id)
        site_network_tags = db.list_site_network_tags(conn, site_id)
        failed_logs = db.list_site_failed_logs(conn, site_id)
        failed_log_urls = {f["page_url"] for f in failed_logs}
        failed_crawl_pages = [
            r for r in db.list_site_failed_crawl_pages(conn, site_id)
            if r["url"] not in failed_log_urls
        ]
        doc_total = db.count_site_document_groups(conn, site_id)
        site_documents = db.list_site_document_groups(
            conn, site_id, limit=200, offset=0
        )

    page_bytes: dict[int, int] = {}
    for row in snap_dirs:
        page_bytes[row["page_id"]] = page_bytes.get(row["page_id"], 0) + row["bytes"]
    schedule_labels = {
        s["page_id"]: i18n.interval_label(request, s["interval_seconds"])
        for s in schedules
    }
    return {
        "site": dict(site),
        "site_title": _site_title(snap_dirs),
        "pages": [{**dict(p), "bytes": page_bytes.get(p["id"], 0)} for p in pages],
        "page_count": totals["page_count"],
        "snapshot_total": totals["snapshot_count"],
        "site_bytes": sum(page_bytes.values()),
        "pager": {
            "page": page, "total_pages": total_pages,
            "per_page": per_page, "total": totals["page_count"],
        },
        "crawls": [dict(c) for c in crawls],
        "schedules": [
            {**dict(s), "label": schedule_labels[s["page_id"]]} for s in schedules
        ],
        "crawl_schedules": [
            {
                "start_url": s["start_url"],
                "label": i18n.interval_label(request, s["interval_seconds"]),
                "next_run_at": s["next_run_at"],
            }
            for s in crawl_schedules
        ],
        "network_tags": [dict(tg) for tg in site_network_tags],
        "documents": [dict(d) for d in site_documents],
        "doc_total": doc_total,
        "failed_items": _failed_items(site_id, failed_logs, failed_crawl_pages),
        "can_archive": permissions.can_archive(user),
        "can_delete": permissions.can_delete(user),
    }


def _collapse_equal(request: Request, rows, context: int = 3):
    """긴 equal 구간을 ('skip','N줄 동일','') 로 접는다 — app._collapse_equal 과 동일."""
    out = []
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
        head = context if i > 0 else 0
        tail = context if j < len(rows) else 0
        if len(run) > head + tail + 1:
            out.extend(run[:head])
            out.append(("skip", i18n.t(request, "{n}줄 동일", n=len(run) - head - tail), ""))
            if tail:
                out.extend(run[-tail:])
        else:
            out.extend(run)
        i = j
    return out


def _resolve_diff_pair(request: Request, page_id: int, from_idx, to_idx):
    """diff 대상 페이지/스냅샷 쌍 검증 — app._resolve_diff_pair 와 동일."""
    from fastapi import HTTPException

    with db.connect() as conn:
        page = db.get_page_by_id(conn, page_id)
        if page is None:
            raise HTTPException(404, "페이지 없음")
        snaps = db.list_snapshots(conn, page_id)
    if len(snaps) < 2:
        raise HTTPException(400, "비교하려면 스냅샷이 2개 이상 필요합니다")
    if to_idx is None:
        to_idx = len(snaps)
    if from_idx is None:
        from_idx = to_idx - 1
    if not (1 <= from_idx < to_idx <= len(snaps)):
        raise HTTPException(400, "잘못된 범위")
    old_snap, new_snap = snaps[from_idx - 1], snaps[to_idx - 1]
    for snap in (old_snap, new_snap):
        if snap["authenticated"] and not _may_view_authenticated(request, snap):
            raise HTTPException(404, "스냅샷 없음")
    return page, snaps, from_idx, to_idx, old_snap, new_snap


def _screenshot_paths(page, old_snap, new_snap):
    base = storage.page_dir(page["domain"], page["slug"])
    return (
        storage.find_screenshot(base / old_snap["dir_name"]),
        storage.find_screenshot(base / new_snap["dir_name"]),
    )


@router.get("/diff/{page_id}")
def diff(
    request: Request,
    page_id: int,
    from_idx: int | None = Query(None, alias="from"),
    to_idx: int | None = Query(None, alias="to"),
    user: sqlite3.Row | None = Depends(require_session),
) -> dict:
    """본문 diff(side-by-side) + 스크린샷 비교 메타. app.diff_view 의 JSON 판.

    쿼리 별칭: ?from=&to= (FastAPI alias). 픽셀 diff 이미지/스크린샷은 FastAPI
    (/diff/{id}/shotdiff, /snapshot/{id}/file/screenshot)에 그대로 둔다.
    """
    from fastapi import HTTPException

    page, snaps, from_idx, to_idx, old_snap, new_snap = _resolve_diff_pair(
        request, page_id, from_idx, to_idx
    )
    texts = []
    for snap in (old_snap, new_snap):
        path = (
            storage.page_dir(page["domain"], page["slug"])
            / snap["dir_name"] / "content.md"
        )
        if not path.is_file():
            raise HTTPException(404, "content.md 없음")
        texts.append(path.read_text(encoding="utf-8"))

    d = differ.diff_text(texts[0], texts[1])
    local_capture = old_snap["origin"] == "extension" or new_snap["origin"] == "extension"
    shot_ratio = None
    if not local_capture:
        old_p, new_p = _screenshot_paths(page, old_snap, new_snap)
        if old_p is not None and new_p is not None:
            shot_ratio, _ = differ.cached_screenshot_diff(
                old_p, new_p, f"shotdiff-{old_snap['id']}-{new_snap['id']}"
            )

    return {
        "page": dict(page),
        "added": d.added,
        "removed": d.removed,
        "rows": [list(r) for r in _collapse_equal(request, d.rows)],
        "from_idx": from_idx,
        "to_idx": to_idx,
        "total": len(snaps),
        "old_snap": dict(old_snap),
        "new_snap": dict(new_snap),
        "local_capture": local_capture,
        "old_shot": f"/snapshot/{old_snap['id']}/file/screenshot",
        "new_shot": f"/snapshot/{new_snap['id']}/file/screenshot",
        "shot_ratio": shot_ratio,
        "shotdiff_url": f"/diff/{page_id}/shotdiff?from={from_idx}&to={to_idx}",
    }


_SEARCH_PER_PAGE = 20


@router.get("/search")
def search(
    request: Request,
    q: str = "",
    domain: str = "",
    latest: int = 0,
    page: int = Query(1, ge=1),
    user: sqlite3.Row | None = Depends(require_session),
) -> dict:
    """아카이브 전문 검색(content.md + 문서 본문) — app.search_view 의 JSON 판. viewer 이상."""
    if not permissions.can_search(user):
        raise HTTPException(403, "검색 권한이 없습니다")
    query = (q or "").strip()
    domain_filter = (domain or "").strip() or None
    latest_only = bool(latest)
    available = searchindex.available()
    results = None
    total_pages = 1
    if available and query:
        r = searchindex.search(
            query, domain=domain_filter, latest_only=latest_only,
            limit=_SEARCH_PER_PAGE, offset=(page - 1) * _SEARCH_PER_PAGE,
        )
        total_pages = max(1, -(-r.total // _SEARCH_PER_PAGE))
        if page > total_pages and r.total:
            page = total_pages
            r = searchindex.search(
                query, domain=domain_filter, latest_only=latest_only,
                limit=_SEARCH_PER_PAGE, offset=(page - 1) * _SEARCH_PER_PAGE,
            )
        results = {
            "total": r.total,
            "mode": r.mode,
            "hits": [dataclasses.asdict(h) for h in r.hits],
        }
    return {
        "q": query,
        "domain": domain_filter or "",
        "latest": latest_only,
        "available": available,
        "results": results,
        "page": page,
        "total_pages": total_pages,
        "per_page": _SEARCH_PER_PAGE,
    }


_DOCUMENTS_PER_PAGE = 100


def _legacy_documents_pending() -> bool:
    """구형 files/ 문서가 남아 있는지 (배너용) — app._legacy_documents_pending 의 무캐시 판."""
    return any(documents.has_legacy_documents(d) for d in resources.snapshot_dirs())


@router.get("/documents")
def documents_list(
    request: Request,
    page: int = Query(1, ge=1),
    user: sqlite3.Row | None = Depends(require_session),
) -> dict:
    """문서 파일 통합 목록(sha256 그룹) — app.documents_view 의 JSON 판."""
    offset = (page - 1) * _DOCUMENTS_PER_PAGE
    with db.connect() as conn:
        totals = db.document_totals(conn)
        groups = db.list_document_groups(
            conn, limit=_DOCUMENTS_PER_PAGE + 1, offset=offset
        )
    has_next = len(groups) > _DOCUMENTS_PER_PAGE
    return {
        "groups": [dict(g) for g in groups[:_DOCUMENTS_PER_PAGE]],
        "totals": dict(totals),
        "page": page,
        "has_next": has_next,
        "legacy_pending": _legacy_documents_pending(),
    }


@router.get("/schedules")
def schedules(
    request: Request, user: sqlite3.Row | None = Depends(require_session)
) -> dict:
    """자동 재아카이빙 목록(페이지·사이트) — app.schedules_view 의 JSON 판."""
    with db.connect() as conn:
        rows = db.list_schedules(conn)
        crawl_rows = db.list_crawl_schedules(conn)

    def _label(s) -> str:
        return i18n.schedule_label(request, s["interval_seconds"], s["run_at_time"])

    return {
        "items": [{**dict(s), "label": _label(s)} for s in rows],
        "crawl_items": [{**dict(s), "label": _label(s)} for s in crawl_rows],
        "can_archive": permissions.can_archive(user),
    }


_LOG_STATUSES = ("new", "changed", "unchanged", "forced_same", "error")
_LOG_PAGE_SIZES = (10, 25, 50, 100, 200)
_LOG_PAGE_SIZE_DEFAULT = 25


def _clean_date(value: str | None) -> str | None:
    """날짜 입력을 YYYY-MM-DD 로 정규화 — app._clean_date 와 동일."""
    if not value:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


@router.get("/logs")
def logs(
    request: Request,
    domain: str | None = None,
    page_id: int | None = None,
    snapshot_id: int | None = None,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    page: int = 1,
    limit: int = _LOG_PAGE_SIZE_DEFAULT,
    user: sqlite3.Row | None = Depends(require_session),
) -> dict:
    """아카이빙 로그(필터·페이징) — app.logs_view 의 JSON 판. viewer 이상."""
    if not permissions.can_view_logs(user):
        raise HTTPException(403, "로그 열람 권한이 없습니다")
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
        total_pages = max(1, -(-total // limit))
        page = max(1, min(page, total_pages))
        rows = db.list_archive_logs(
            conn, **filters, limit=limit, offset=(page - 1) * limit
        )
        domains = db.list_log_domains(conn)

    items = []
    for row in rows:
        try:
            steps = json.loads(row["steps"]) if row["steps"] else []
        except ValueError:
            steps = []
        files: list[dict] = []
        if row["snap_dir_name"]:
            snap_dir = (
                storage.page_dir(row["snap_domain"], row["snap_slug"])
                / row["snap_dir_name"]
            )
            files = storage.snapshot_files(snap_dir)
        items.append({
            "log": dict(row),
            "steps": steps,
            "files": files,
            "total_bytes": sum(f["bytes"] for f in files) if files else None,
        })

    return {
        "items": items,
        "domains": list(domains),
        "domain": domain or "",
        "status": status or "",
        "date_from": date_from or "",
        "date_to": date_to or "",
        "snapshot_id": snapshot_id,
        "limit": limit,
        "limits": list(_LOG_PAGE_SIZES),
        "statuses": list(_LOG_STATUSES),
        "total": total,
        "total_pages": total_pages,
        "page_num": page,
    }
