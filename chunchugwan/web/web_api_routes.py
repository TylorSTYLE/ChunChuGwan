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
import zoneinfo
from datetime import date, datetime, timedelta, timezone

from fastapi import (
    APIRouter, Depends, File, Form, HTTPException, Query, Request, Response,
    UploadFile,
)
from fastapi.responses import FileResponse, JSONResponse

from pydantic import BaseModel

from .. import (
    __version__, auth, config, crawler, crypto, db, deletion, differ, documents,
    mailer, optimize, resources, scheduler, searchindex, storage,
)
from . import audit, i18n, permissions

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

    `permissions.auth_context`(권한 플래그·needs-human)를 재사용해 SPA 메뉴 노출
    규칙을 전달한다 (서버 권한 가드는 각 엔드포인트에서 이중 유지).
    """
    ctx = permissions.auth_context(request)
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


@router.get("/i18n/{locale}")
def i18n_catalog(locale: str) -> dict:
    """SPA 번역 카탈로그 — i18n.py(_EN) 정본을 그대로 제공.

    SPA 의 `t(msg)` 는 ctx 를 쓰지 않으므로 "ctx|원문" 키는 제외(평문만).
    ko 는 빈 dict(원문 패스스루). 미인증 로그인 화면도 번역을 받도록 공개
    (auth_gate 가 /api/ 를 통과시키고, 번역 텍스트는 비밀이 아니다).
    """
    if locale not in i18n.SUPPORTED_LOCALES:
        raise HTTPException(404, "지원하지 않는 로케일입니다")
    return {k: v for k, v in i18n.CATALOGS.get(locale, {}).items() if "|" not in k}


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
        "trash_enabled": _trash_enabled_now(),
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
    audit.log(
        request, "아카이브 열람: %s (스냅샷 #%d)", snap["page_url"], snapshot_id,
        action="view", target=snap["page_url"],
    )
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
            "kind": "log", "id": f["id"], "page_id": f["page_id"],
            "page_url": f["page_url"], "url": f["url"], "at": f["started_at"],
            "source": f["source"], "error": f["error"],
        })
    for f in failed_crawl_pages:
        items.append({
            "kind": "crawl", "id": f["id"], "url": f["url"], "at": f["failed_at"],
            "crawl_id": f["crawl_id"], "error": f["error"],
        })
    items.sort(key=lambda x: x["at"] or "", reverse=True)
    return items


def _site_pages_block(
    conn: sqlite3.Connection, site_id: int, page: int, per_page: int
) -> tuple[dict, list, dict[int, int], sqlite3.Row]:
    """사이트 페이지 목록 슬라이스(바이트 포함) + 페이저를 만든다.

    site_detail 과 린 엔드포인트(/sites/{id}/pages)가 공유 — 한 쪽만 바뀌어 페이징
    규칙(per_page 허용집합·clamp·바이트 합산)이 어긋나지 않게 단일 출처로 둔다.
    반환: ({"pages": [...], "pager": {...}}, snap_dirs, page_bytes, totals)
    (snap_dirs·page_bytes·totals 는 site_detail 의 나머지 필드 계산에 재사용)
    """
    per_page = per_page if per_page in (25, 50, 75, 100, 200) else 50
    totals = db.site_page_totals(conn, site_id)
    total_pages = max(1, -(-totals["page_count"] // per_page))
    page = min(max(page, 1), total_pages)
    pages = db.list_site_pages(
        conn, site_id, limit=per_page, offset=(page - 1) * per_page
    )
    snap_dirs = db.list_site_snapshot_dirs(conn, site_id)
    page_bytes: dict[int, int] = {}
    for row in snap_dirs:
        page_bytes[row["page_id"]] = page_bytes.get(row["page_id"], 0) + row["bytes"]
    block = {
        "pages": [{**dict(p), "bytes": page_bytes.get(p["id"], 0)} for p in pages],
        "pager": {
            "page": page, "total_pages": total_pages,
            "per_page": per_page, "total": totals["page_count"],
        },
    }
    return block, snap_dirs, page_bytes, totals


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

    with db.connect() as conn:
        site = db.get_site(conn, site_id)
        if site is None:
            raise HTTPException(404, "사이트 없음")
        block, snap_dirs, page_bytes, totals = _site_pages_block(
            conn, site_id, page, per_page
        )
        crawls = db.list_site_crawls(conn, site_id)
        schedules = db.list_site_schedules(conn, site_id)
        crawl_schedules = db.list_site_crawl_schedules(conn, site_id)
        site_network_tags = db.list_site_network_tags(conn, site_id)
        certificates = db.list_site_certificates(conn, site_id)
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

    schedule_labels = {
        s["page_id"]: i18n.interval_label(request, s["interval_seconds"])
        for s in schedules
    }
    # 인증서 — 호스트별 최신 행이 "현재"(db 정렬), .pem 은 기존 바이너리 라우트로 다운로드
    current_cert_ids: dict[str, int] = {}
    for c in certificates:
        current_cert_ids.setdefault(c["host"], c["id"])
    cert_rows = [
        {
            "cert": dict(c),
            "san": json.loads(c["san"] or "[]"),
            "is_current": current_cert_ids[c["host"]] == c["id"],
            "pem_url": f"/sites/{site_id}/certificates/{c['id']}.pem",
        }
        for c in certificates
    ]
    return {
        "site": dict(site),
        "site_title": _site_title(snap_dirs),
        "pages": block["pages"],
        "page_count": totals["page_count"],
        "snapshot_total": totals["snapshot_count"],
        "site_bytes": sum(page_bytes.values()),
        "pager": block["pager"],
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
        "certificates": cert_rows,
        "documents": [dict(d) for d in site_documents],
        "doc_total": doc_total,
        "failed_items": _failed_items(site_id, failed_logs, failed_crawl_pages),
        "can_archive": permissions.can_archive(user),
        "can_delete": permissions.can_delete(user),
        "can_manage_credentials": permissions.can_manage_credentials(user),
        "trash_enabled": _trash_enabled_now(),
    }


@router.get("/sites/{site_id}/pages")
def site_pages(
    site_id: int,
    page: int = 1,
    per_page: int = 50,
    user: sqlite3.Row | None = Depends(require_session),
) -> dict:
    """사이트 페이지 목록만 (페이징) — 상세 화면 페이저 in-place 갱신용 린 응답.

    site_detail 의 통계·인증서·크롤·스케줄·문서 등을 생략하고 pages/pager 만 내려준다
    (이전/다음 시 SPA 가 목록만 교체). 가드는 site_detail 과 동일하게 세션만 요구한다.
    """
    from fastapi import HTTPException

    with db.connect() as conn:
        if db.get_site(conn, site_id) is None:
            raise HTTPException(404, "사이트 없음")
        block, *_ = _site_pages_block(conn, site_id, page, per_page)
    return block


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
    """아카이빙 로그(필터·페이징) — '아카이브 로그 보기'(기본 admin) 권한."""
    if not permissions.can_view_archive_logs(user):
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
        "can_archive": permissions.can_archive(user),
    }


# ── 쓰기 액션 ───────────────────────────────────────────────────────────────
# 변경 동작은 여전히 코어 모듈(scheduler·crawler·deletion·db)을 거친다(원칙 1).
# app.py 의 검증 헬퍼(_queue_archive·_network_gate·_interval_from_form·
# _requester_id)는 지연 import 로 재사용한다 — app 이 이 모듈을 모듈 레벨에서
# import 하므로 함수 안에서 역참조해 순환을 피한다.


def _require_archive(user: sqlite3.Row | None) -> None:
    if not permissions.can_archive(user):
        raise HTTPException(403, "아카이빙 권한이 없습니다")


def _require_delete(user: sqlite3.Row | None) -> None:
    if not permissions.can_delete(user):
        raise HTTPException(403, "삭제 권한이 없습니다")


def _require_manage_trash(user: sqlite3.Row | None) -> None:
    if not permissions.can_manage_trash(user):
        raise HTTPException(403, "휴지통 관리 권한이 없습니다")


def _actor_id(user: sqlite3.Row | None) -> int | None:
    """삭제·복원 기록용 사용자 id (인증 off/loopback 이면 None)."""
    return user["id"] if user is not None else None


def _trash_enabled_now() -> bool:
    """휴지통 기능 on/off — 상세 화면이 삭제 확인 문구를 맞추는 데 쓴다."""
    with db.connect() as conn:
        return db.trash_enabled(conn)


def _require_not_migrating() -> None:
    with db.connect() as conn:
        if db.migration_mode_enabled(conn):
            raise HTTPException(409, "이전(마이그레이션) 모드입니다 — 아카이빙할 수 없습니다")


@router.get("/network-tags")
def network_tags_list(
    request: Request, user: sqlite3.Row | None = Depends(require_session)
) -> dict:
    """새 아카이빙 폼 데이터 — 로컬 네트워크 태그 + 사이트 아카이브 기본값 (아카이빙 권한)."""
    if not permissions.can_archive(user):
        raise HTTPException(403, "아카이빙 권한이 없습니다")
    with db.connect() as conn:
        tags = db.list_network_tags(conn)
        crawl_defaults = crawler.crawl_defaults(conn)
    return {
        "network_tags": [
            {"id": t["id"], "name": t["name"], "description": t["description"]}
            for t in tags
        ],
        "crawl_defaults": {
            "max_pages": crawl_defaults["max_pages"],
            "max_depth": crawl_defaults["max_depth"],
            "delay": crawl_defaults["delay_seconds"],
        },
    }


class ArchiveReq(BaseModel):
    url: str
    force: bool = False
    site: bool = False
    interval: str = "0"
    custom_value: str = ""
    custom_unit: str = "h"
    run_at: str = ""
    network_tag: str = ""
    crawl_max_pages: str = ""
    crawl_max_depth: str = ""
    crawl_delay: str = ""
    # 로그인 자격증명 연결 (자격증명 관리 권한) — ""=연결 안 함, "__new__"=신규 생성, 숫자=기존
    cred_existing_id: str = ""
    cred_kind: str = ""
    cred_label: str = ""
    cred_username: str = ""
    cred_password: str = ""
    cred_storage_state: str = ""
    cred_token: str = ""


@router.post("/archive", status_code=202)
def archive_new(
    request: Request,
    body: ArchiveReq,
    user: sqlite3.Row | None = Depends(require_session),
) -> dict:
    """새 URL 아카이빙(또는 사이트 전체 크롤) 등록 — app.archive_new 의 JSON 판.

    자격증명 관리 권한이 있으면 입력 도메인의 기존 자격증명을 연결하거나 새로
    만들어 연결한다(`resolve_archive_credential`). 캡처는 워커 큐가 처리한다.
    """
    from .app import (
        _interval_from_form, _network_gate, _queue_archive, _requester_id,
        resolve_archive_credential,
    )

    _require_archive(user)
    _require_not_migrating()
    try:
        seconds = _interval_from_form(body.interval, body.custom_value, body.custom_unit)
        if seconds:
            scheduler.validate_interval(seconds)
            if body.run_at:
                scheduler.validate_run_at(body.run_at, seconds)
        norm = storage.normalize_url(body.url)
        tag_id = _network_gate(request, norm, body.network_tag.strip() or None)
    except ValueError as exc:
        raise HTTPException(400, f"아카이빙 실패: {exc}")

    cred_id, cred_error = resolve_archive_credential(
        request, norm, existing_id=body.cred_existing_id, kind=body.cred_kind.strip(),
        label=body.cred_label, username=body.cred_username, password=body.cred_password,
        storage_state=body.cred_storage_state, token=body.cred_token,
    )
    if cred_error is not None:
        raise HTTPException(400, cred_error)

    if body.site:
        options = {
            "max_pages": int(body.crawl_max_pages) if body.crawl_max_pages else None,
            "max_depth": int(body.crawl_max_depth) if body.crawl_max_depth else None,
            "delay_seconds": int(body.crawl_delay) if body.crawl_delay else None,
        }
        try:
            crawl, merged = crawler.start_crawl(
                body.url, **options, source="web",
                requested_by=_requester_id(request), network_tag_id=tag_id,
                credential_id=cred_id,
            )
            if seconds:
                crawler.set_crawl_schedule(
                    body.url, seconds, run_at=body.run_at or None, **options,
                    network_tag_id=tag_id, credential_id=cred_id,
                )
        except ValueError as exc:
            raise HTTPException(400, f"아카이빙 실패: {exc}")
        audit.log(request, "새 사이트 아카이브 등록: %s", body.url,
                  action="archive", target=norm)
        return {"site": True, "crawl_id": crawl["id"], "merged": merged}

    queued = _queue_archive(
        norm, force=body.force, requested_by=_requester_id(request),
        credential_id=cred_id,
        interval_seconds=seconds or None,
        run_at=(body.run_at or None) if seconds else None,
        network_tag_id=tag_id,
    )
    if queued:
        audit.log(request, "새 아카이빙 등록: %s", norm,
                  action="archive", target=norm)
    return {"site": False, "queued": norm, "enqueued": queued}


class RearchiveReq(BaseModel):
    force: bool = False


@router.post("/pages/{page_id}/rearchive", status_code=202)
def rearchive(
    request: Request,
    page_id: int,
    body: RearchiveReq,
    user: sqlite3.Row | None = Depends(require_session),
) -> dict:
    """재아카이빙 등록 — app.rearchive 의 JSON 판."""
    from .app import _queue_archive, _requester_id

    _require_archive(user)
    _require_not_migrating()
    with db.connect() as conn:
        page = db.get_page_by_id(conn, page_id)
    if page is None:
        raise HTTPException(404, "페이지 없음")
    enqueued = _queue_archive(
        page["url"], force=body.force, requested_by=_requester_id(request)
    )
    if enqueued:
        audit.log(request, "재아카이빙 등록: %s", page["url"],
                  action="archive", target=page["url"])
    return {"enqueued": enqueued}


class ScheduleReq(BaseModel):
    interval: str
    custom_value: str = ""
    custom_unit: str = "h"
    run_at: str = ""


@router.post("/pages/{page_id}/schedule")
def schedule_set(
    request: Request,
    page_id: int,
    body: ScheduleReq,
    user: sqlite3.Row | None = Depends(require_session),
) -> dict:
    """페이지 반복 주기 등록/변경 — app.schedule_set 의 JSON 판."""
    from .app import _interval_from_form

    _require_archive(user)
    with db.connect() as conn:
        page = db.get_page_by_id(conn, page_id)
    if page is None:
        raise HTTPException(404, "페이지 없음")
    try:
        seconds = _interval_from_form(body.interval, body.custom_value, body.custom_unit)
        scheduler.set_schedule(page["url"], seconds, run_at=body.run_at or None)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "interval_seconds": seconds}


@router.post("/pages/{page_id}/schedule/delete")
def schedule_delete(
    request: Request,
    page_id: int,
    user: sqlite3.Row | None = Depends(require_session),
) -> dict:
    """페이지 반복 주기 해제 — app.schedule_delete 의 JSON 판."""
    _require_archive(user)
    with db.connect() as conn:
        page = db.get_page_by_id(conn, page_id)
    if page is None:
        raise HTTPException(404, "페이지 없음")
    scheduler.remove_schedule(page["url"])
    return {"ok": True}


@router.post("/pages/{page_id}/delete")
def page_delete(
    request: Request,
    page_id: int,
    user: sqlite3.Row | None = Depends(require_session),
) -> dict:
    """페이지(전체 스냅샷) 삭제 — 기본은 휴지통으로 이동(시스템 설정 off 면 즉시 삭제)."""
    _require_delete(user)
    result = deletion.delete_page(page_id, deleted_by=_actor_id(user))
    if result is None:
        raise HTTPException(404, "페이지 없음")
    audit.log(
        request, "페이지 %s: %s",
        "휴지통 이동" if result.trashed else "영구 삭제", result.url,
    )
    return {
        "ok": True,
        "snapshots_deleted": result.snapshots_deleted,
        "trashed": result.trashed,
    }


@router.post("/sites/{site_id}/delete")
def site_delete(
    request: Request,
    site_id: int,
    user: sqlite3.Row | None = Depends(require_session),
) -> dict:
    """사이트 전체(페이지·크롤·스케줄) 삭제 — 기본은 휴지통으로 이동(설정 off 면 즉시 삭제)."""
    _require_delete(user)
    result = deletion.delete_site(site_id, deleted_by=_actor_id(user))
    if result is None:
        raise HTTPException(404, "사이트 없음")
    audit.log(
        request, "사이트 %s: %s",
        "휴지통 이동" if result.trashed else "영구 삭제", result.site_key,
    )
    return {
        "ok": True,
        "site_key": result.site_key,
        "pages_deleted": result.pages_deleted,
        "snapshots_deleted": result.snapshots_deleted,
        "trashed": result.trashed,
    }


# ---- 휴지통 (trash) ----


def _trash_entry_public(entry: sqlite3.Row, retention_days: int) -> dict:
    """휴지통 항목을 SPA 응답 형태로 — 보관 기한(expires_at)은 동적 계산."""
    expires_at: str | None = None
    if retention_days > 0:
        try:
            base = datetime.fromisoformat(entry["deleted_at"])
            expires_at = (base + timedelta(days=retention_days)).isoformat(
                timespec="seconds"
            )
        except (TypeError, ValueError):
            expires_at = None
    return {
        "id": entry["id"],
        "kind": entry["kind"],
        "label": entry["label"],
        "site_id": entry["site_id"],
        "page_id": entry["page_id"],
        "page_count": entry["page_count"],
        "snapshot_count": entry["snapshot_count"],
        "bytes": entry["bytes"],
        "deleted_at": entry["deleted_at"],
        "expires_at": expires_at,
        "deleted_by_email": entry["deleted_by_email"],
        "deleted_by_name": entry["deleted_by_name"],
    }


@router.get("/trash")
def trash_list(
    request: Request, user: sqlite3.Row | None = Depends(require_session)
) -> dict:
    """휴지통 항목 목록 + 현재 설정(보관 기간·기능 on/off) — 휴지통 관리 권한."""
    _require_manage_trash(user)
    with db.connect() as conn:
        retention_days = db.trash_retention_days(conn)
        entries = [
            _trash_entry_public(e, retention_days)
            for e in db.list_trash_entries(conn)
        ]
        trash_enabled = db.trash_enabled(conn)
    return {
        "entries": entries,
        "trash_enabled": trash_enabled,
        "retention_days": retention_days,
    }


@router.post("/trash/{trash_id}/restore")
def trash_restore(
    request: Request,
    trash_id: int,
    user: sqlite3.Row | None = Depends(require_session),
) -> dict:
    """휴지통 항목 복원 — 숨김 해제. 휴지통 관리 권한."""
    _require_manage_trash(user)
    entry = deletion.restore(trash_id)
    if entry is None:
        raise HTTPException(404, "휴지통 항목 없음")
    audit.log(request, "휴지통 복원: %s (%s)", entry["label"], entry["kind"])
    return {"ok": True, "label": entry["label"], "kind": entry["kind"]}


@router.post("/trash/{trash_id}/purge")
def trash_purge(
    request: Request,
    trash_id: int,
    user: sqlite3.Row | None = Depends(require_session),
) -> dict:
    """휴지통 항목 영구 삭제 — 되돌릴 수 없음. 휴지통 관리 권한."""
    _require_manage_trash(user)
    entry = deletion.purge(trash_id)
    if entry is None:
        raise HTTPException(404, "휴지통 항목 없음")
    audit.log(request, "휴지통 영구삭제: %s (%s)", entry["label"], entry["kind"])
    return {"ok": True, "label": entry["label"], "kind": entry["kind"]}


@router.post("/sites/{site_id}/failed/{log_id}/retry")
def site_failed_retry(
    request: Request,
    site_id: int,
    log_id: int,
    user: sqlite3.Row | None = Depends(require_session),
) -> dict:
    """실패한 직접 아카이빙 작업 재시도 — app.site_failed_retry 의 JSON 판."""
    from .app import _queue_archive, _requester_id

    _require_archive(user)
    with db.connect() as conn:
        log = db.get_site_failed_log(conn, site_id, log_id)
    if log is None:
        raise HTTPException(404, "실패 기록 없음")
    queued = _queue_archive(log["page_url"], requested_by=_requester_id(request))
    if queued:
        audit.log(request, "실패 작업 재시도: %s", log["page_url"])
    return {"queued": queued}


@router.post("/sites/{site_id}/failed/retry-all")
def site_failed_retry_all(
    request: Request,
    site_id: int,
    user: sqlite3.Row | None = Depends(require_session),
) -> dict:
    """사이트의 실패 작업(직접 아카이빙 + 크롤 페이지) 모두 재시도 — app.site_failed_retry_all 의 JSON 판."""
    from .app import _queue_archive, _requester_id

    _require_archive(user)
    with db.connect() as conn:
        site = db.get_site(conn, site_id)
        if site is None:
            raise HTTPException(404, "사이트 없음")
        failed_logs = db.list_site_failed_logs(conn, site_id)
        failed_log_urls = {f["page_url"] for f in failed_logs}
        failed_crawl_pages = [
            r for r in db.list_site_failed_crawl_pages(conn, site_id)
            if r["url"] not in failed_log_urls
        ]
        crawl_retried = db.retry_failed_crawl_pages_by_ids(
            conn, [r["id"] for r in failed_crawl_pages]
        )
    requester = _requester_id(request)
    queued = sum(
        _queue_archive(f["page_url"], requested_by=requester) for f in failed_logs
    )
    if queued or crawl_retried:
        audit.log(
            request, "사이트 실패 작업 모두 재시도: site #%d (로그 %d · 크롤 %d)",
            site_id, queued, crawl_retried,
        )
    return {"queued": queued, "crawl_retried": crawl_retried}


@router.post("/sites/{site_id}/export")
def site_export(
    request: Request,
    site_id: int,
    user: sqlite3.Row | None = Depends(require_session),
) -> FileResponse:
    """사이트 아카이브 내보내기(.ccg.export) 다운로드 — app.site_export 의 JSON 판."""
    from .maintenance import tar_download
    from .. import backup as backup_mod

    _require_archive(user)
    with db.connect() as conn:
        site = db.get_site(conn, site_id)
    if site is None:
        raise HTTPException(404, "사이트 없음")
    audit.log(request, "사이트 아카이브 내보내기: %s", site["site_key"])
    return tar_download(
        lambda dest: backup_mod.export_archive(dest, site_id=site_id), "export"
    )


@router.post("/logs/{log_id}/retry")
def log_retry(
    request: Request,
    log_id: int,
    user: sqlite3.Row | None = Depends(require_session),
) -> dict:
    """실패 로그의 URL 재시도 — app.log_retry 의 JSON 판 (필터·리다이렉트 없이 상태만 반환)."""
    from .app import _queue_archive, _requester_id

    _require_archive(user)
    _require_not_migrating()
    with db.connect() as conn:
        log = db.get_archive_log(conn, log_id)
    if log is None:
        raise HTTPException(404, "로그 없음")
    if log["status"] != "error":
        raise HTTPException(400, "실패한 로그만 재시도할 수 있습니다")
    queued = _queue_archive(log["url"], requested_by=_requester_id(request))
    if queued:
        audit.log(request, "실패 로그 재시도: %s", log["url"])
    return {"queued": queued}


# ── 관리 영역 (system_routes 의 JSON 판) ─────────────────────────────────────
# system_routes 는 _require_admin(manage_system) 라우터 가드를 쓰지만, 사용자·
# API 키 관리는 manage_users 권한이라 엔드포인트별로 가드를 명시한다.


def _require_manage_system(user: sqlite3.Row | None) -> None:
    if not permissions.can_manage_system(user):
        raise HTTPException(403, "시스템 관리 권한이 없습니다")


def _require_manage_users(user: sqlite3.Row | None) -> None:
    if not permissions.can_manage_users(user):
        raise HTTPException(403, "사용자 관리 권한이 없습니다")


@router.get("/system/users")
def system_users(
    request: Request, user: sqlite3.Row | None = Depends(require_session)
) -> dict:
    """사용자 목록 + 권한·초대 — system_routes.users_view 의 JSON 판."""
    _require_manage_users(user)
    with db.connect() as conn:
        db.delete_expired_invites(conn)
        users = db.list_users(conn)
        invites = db.list_invites(conn)
        mail_on = mailer.mail_enabled(conn)
        assignable = db.assignable_roles(conn)
        invitable = db.invitable_roles(conn)
        role_labels = db.role_labels(conn)
    # 권한은 역할(권한 묶음) 단위로만 부여한다 — 사용자별 세분 권한 편집은 없다.
    return {
        "users": [dict(u) for u in users],
        "invites": [dict(i) for i in invites],
        "me_id": user["id"] if user else None,
        "roles": list(assignable),
        "invitable_roles": list(invitable),
        "role_labels": dict(role_labels),
        "mail_enabled": mail_on,
        "invite_ttl_days": config.INVITE_TTL_DAYS,
    }


@router.get("/system/groups")
def system_groups(
    request: Request, user: sqlite3.Row | None = Depends(require_session)
) -> dict:
    """권한 그룹 목록 — system_routes.groups_view 의 JSON 판."""
    _require_manage_system(user)
    with db.connect() as conn:
        groups = [
            {
                "name": r["name"],
                "label": r["label"],
                "is_builtin": bool(r["is_builtin"]),
                "permissions": sorted(db._parse_permission_list(r["permissions"])),
                "member_count": db.count_users_with_role(conn, r["name"]),
            }
            for r in db.list_permission_groups(conn)
        ]
    return {
        "groups": groups,
        "permissions_catalog": list(db.PERMISSIONS),
        "permission_labels": dict(db.PERMISSION_LABELS),
    }


@router.get("/system/api-keys")
def system_api_keys(
    request: Request, user: sqlite3.Row | None = Depends(require_session)
) -> dict:
    """시스템 API 키 목록(owner=NULL) — system_routes.api_keys_view 의 JSON 판."""
    _require_manage_users(user)
    with db.connect() as conn:
        keys = db.list_system_api_keys(conn)
    return {"keys": [_public_api_key(k) for k in keys]}


_SYSLOG_PAGE_SIZES = (25, 50, 100, 200)
_SYSLOG_PAGE_SIZE_DEFAULT = 50


@router.get("/system/logs")
def system_logs(
    request: Request,
    level: str | None = None,
    source: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    page: int = 1,
    limit: int = _SYSLOG_PAGE_SIZE_DEFAULT,
    user: sqlite3.Row | None = Depends(require_session),
) -> dict:
    """시스템 로그(필터·페이징) — '시스템 로그 보기'(기본 admin) 권한."""
    if not permissions.can_view_system_logs(user):
        raise HTTPException(403, "로그 열람 권한이 없습니다")
    if limit not in _SYSLOG_PAGE_SIZES:
        limit = _SYSLOG_PAGE_SIZE_DEFAULT
    if level not in db.SYSTEM_LOG_LEVELS:
        level = None
    if source not in db.SYSTEM_LOG_SOURCES:
        source = None
    date_from = _clean_date(date_from)
    date_to = _clean_date(date_to)
    if date_from and date_to and date_from > date_to:
        date_from, date_to = date_to, date_from
    filters = {
        "level": level, "source": source,
        "date_from": date_from, "date_to": date_to,
    }
    with db.connect() as conn:
        total = db.count_system_logs(conn, **filters)
        total_pages = max(1, -(-total // limit))
        page = max(1, min(page, total_pages))
        rows = db.list_system_logs(
            conn, **filters, limit=limit, offset=(page - 1) * limit
        )
    return {
        "logs": [dict(r) for r in rows],
        "level": level or "",
        "source": source or "",
        "date_from": date_from or "",
        "date_to": date_to or "",
        "levels": list(db.SYSTEM_LOG_LEVELS),
        "sources": list(db.SYSTEM_LOG_SOURCES),
        "limit": limit,
        "limits": list(_SYSLOG_PAGE_SIZES),
        "total": total,
        "total_pages": total_pages,
        "page_num": page,
    }


@router.get("/audit")
def audit_logs(
    request: Request,
    action: str | None = None,
    actor: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    page: int = 1,
    limit: int = _SYSLOG_PAGE_SIZE_DEFAULT,
    user: sqlite3.Row | None = Depends(require_session),
) -> dict:
    """감사 로그(필터·페이징) — '감사 로그 보기'(기본 admin) 권한.

    누가 아카이빙·열람·문서 다운로드·관리 작업을 했는지 audit_logs 에서 읽는다.
    """
    if not permissions.can_view_audit_logs(user):
        raise HTTPException(403, "로그 열람 권한이 없습니다")
    if limit not in _SYSLOG_PAGE_SIZES:
        limit = _SYSLOG_PAGE_SIZE_DEFAULT
    if action not in db.AUDIT_ACTIONS:
        action = None
    date_from = _clean_date(date_from)
    date_to = _clean_date(date_to)
    if date_from and date_to and date_from > date_to:
        date_from, date_to = date_to, date_from
    with db.connect() as conn:
        actors = db.list_audit_actors(conn)
        if actor not in actors:
            actor = None
        filters = {
            "action": action, "actor": actor,
            "date_from": date_from, "date_to": date_to,
        }
        total = db.count_audit_logs(conn, **filters)
        total_pages = max(1, -(-total // limit))
        page = max(1, min(page, total_pages))
        rows = db.list_audit_logs(
            conn, **filters, limit=limit, offset=(page - 1) * limit
        )
    return {
        "logs": [dict(r) for r in rows],
        "action": action or "",
        "actor": actor or "",
        "date_from": date_from or "",
        "date_to": date_to or "",
        "actions": list(db.AUDIT_ACTIONS),
        "action_labels": dict(db.AUDIT_ACTION_LABELS),
        "actors": actors,
        "limit": limit,
        "limits": list(_SYSLOG_PAGE_SIZES),
        "total": total,
        "total_pages": total_pages,
        "page_num": page,
    }


# ── 관리 변경 액션 (system_routes 변경 POST 의 JSON 판) ──────────────────────
import secrets  # noqa: E402
import smtplib  # noqa: E402


class RoleReq(BaseModel):
    role: str


@router.post("/system/users/{user_id}/role")
def system_user_role(
    request: Request, user_id: int, body: RoleReq,
    user: sqlite3.Row | None = Depends(require_session),
) -> dict:
    """사용자 역할 변경 — system_routes.users_set_role 의 JSON 판(라스트-관리자 잠김 방지)."""
    _require_manage_users(user)
    with db.connect() as conn:
        if body.role not in db.assignable_roles(conn):
            raise HTTPException(400, "부여할 수 없는 역할")
        target = db.get_user_by_id(conn, user_id)
        if target is None:
            raise HTTPException(404, "사용자 없음")
        if target["is_founder"]:
            raise HTTPException(400, "최초 관리자의 권한은 변경할 수 없습니다")
        if target["role"] == "withdrawn":
            raise HTTPException(400, "탈퇴한 계정의 권한은 변경할 수 없습니다")
        presets = db.role_presets(conn)
        if "manage_users" not in presets.get(body.role, frozenset()):
            if db.count_active_users_with_permission(
                conn, "manage_users", exclude_user_id=user_id, presets=presets
            ) == 0:
                raise HTTPException(400, "사용자 관리 권한을 가진 마지막 계정입니다")
        db.set_role(conn, user_id, body.role)
        db.set_permission_overrides(conn, user_id, {})
        if body.role == "blocked":
            db.delete_user_sessions(conn, user_id)
    audit.log(request, "사용자 권한 변경: %s → %s", target["email"], body.role)
    return {"ok": True}


class DeleteUserReq(BaseModel):
    email: str = ""


@router.post("/system/users/{user_id}/delete")
def system_user_delete(
    request: Request, user_id: int, body: DeleteUserReq,
    user: sqlite3.Row | None = Depends(require_session),
) -> dict:
    """계정 정보 하드 삭제 — 확인 이메일 일치 요구. 최초 관리자·본인 불가."""
    _require_manage_users(user)
    with db.connect() as conn:
        target = db.get_user_by_id(conn, user_id)
        if target is None:
            raise HTTPException(404, "사용자 없음")
        if target["is_founder"]:
            raise HTTPException(400, "최초 관리자는 삭제할 수 없습니다")
        if user is not None and target["id"] == user["id"]:
            raise HTTPException(400, "본인 계정은 여기서 삭제할 수 없습니다")
        if body.email.strip().lower() != target["email"].lower():
            raise HTTPException(400, "확인 이메일이 일치하지 않습니다")
        db.delete_user(conn, target["id"])
    audit.log(request, "사용자 계정 정보 삭제: %s", target["email"])
    return {"ok": True}


class NameReq(BaseModel):
    display_name: str = ""


@router.post("/system/users/{user_id}/name")
def system_user_name(
    request: Request, user_id: int, body: NameReq,
    user: sqlite3.Row | None = Depends(require_session),
) -> dict:
    """사용자 표시 이름 변경 (빈 입력 = 제거)."""
    _require_manage_users(user)
    name = body.display_name.strip() or None
    if name is not None:
        err = auth.validate_display_name(name)
        if err is not None:
            raise HTTPException(400, err)
    with db.connect() as conn:
        target = db.get_user_by_id(conn, user_id)
        if target is None:
            raise HTTPException(404, "사용자 없음")
        db.set_display_name(conn, user_id, name)
    audit.log(request, "사용자 이름 변경: %s", target["email"])
    return {"ok": True}


@router.post("/system/users/{user_id}/logout")
def system_user_logout(
    request: Request, user_id: int,
    user: sqlite3.Row | None = Depends(require_session),
) -> dict:
    """사용자 전체 세션 강제 로그아웃."""
    _require_manage_users(user)
    with db.connect() as conn:
        target = db.get_user_by_id(conn, user_id)
        if target is None:
            raise HTTPException(404, "사용자 없음")
        db.delete_user_sessions(conn, user_id)
    audit.log(request, "사용자 강제 로그아웃: %s", target["email"])
    return {"ok": True}


def _invite_link(request: Request, token: str) -> str:
    base = config.PUBLIC_URL or str(request.base_url).rstrip("/")
    return f"{base}/invite/{token}"


class InviteReq(BaseModel):
    email: str
    role: str = "viewer"


@router.post("/system/users/invite")
def system_user_invite(
    request: Request, body: InviteReq,
    user: sqlite3.Row | None = Depends(require_session),
) -> dict:
    """이메일 초대 발급 — 메일 미설정이면 링크를 반환해 직접 전달."""
    _require_manage_users(user)
    email = body.email.strip()
    err = auth.validate_email(email)
    if err is not None:
        raise HTTPException(400, err)
    token = secrets.token_urlsafe(32)
    with db.connect() as conn:
        if body.role not in db.invitable_roles(conn):
            raise HTTPException(400, "초대할 수 없는 역할")
        role_label = db.role_labels(conn).get(body.role, body.role)
        if db.get_user_by_email(conn, email) is not None:
            raise HTTPException(400, "이미 가입된 이메일입니다")
        db.create_invite(
            conn, email, auth.hash_token(token), body.role,
            invited_by=user["id"] if user else None,
            ttl_seconds=config.INVITE_TTL_DAYS * 86400,
        )
    audit.log(request, "사용자 초대 발급: %s (권한 %s)", email, role_label)
    link = _invite_link(request, token)
    with db.connect() as conn:
        smtp = mailer.resolve_config(conn)
    mailed = False
    if smtp.enabled:
        inviter = user["email"] if user else "관리자"
        try:
            mailer.send_invite(smtp, email, link, inviter, role_label)
            mailed = True
        except (smtplib.SMTPException, OSError):
            mailed = False
    return {"ok": True, "email": email, "link": link, "mailed": mailed}


@router.post("/system/users/invite/{invite_id}/delete")
def system_invite_delete(
    request: Request, invite_id: int,
    user: sqlite3.Row | None = Depends(require_session),
) -> dict:
    """초대 취소."""
    _require_manage_users(user)
    with db.connect() as conn:
        if not db.delete_invite(conn, invite_id):
            raise HTTPException(404, "초대 없음")
    audit.log(request, "초대 취소: #%d", invite_id)
    return {"ok": True}


class GroupAddReq(BaseModel):
    name: str
    label: str = ""
    permissions: list[str] = []


@router.post("/system/groups")
def system_group_add(
    request: Request, body: GroupAddReq,
    user: sqlite3.Row | None = Depends(require_session),
) -> dict:
    """커스텀 권한 그룹 생성 — system_routes.groups_add 의 JSON 판."""
    _require_manage_system(user)
    perms = [p for p in db.PERMISSIONS if p in set(body.permissions)]
    with db.connect() as conn:
        try:
            created = db.create_permission_group(conn, body.name.strip(), body.label.strip(), perms)
        except ValueError as e:
            raise HTTPException(400, str(e))
    audit.log(request, "권한 그룹 생성: %s", created)
    return {"ok": True, "name": created}


class GroupEditReq(BaseModel):
    label: str = ""
    permissions: list[str] = []


@router.post("/system/groups/{name}")
def system_group_edit(
    request: Request, name: str, body: GroupEditReq,
    user: sqlite3.Row | None = Depends(require_session),
) -> dict:
    """그룹 세분 권한(+커스텀 라벨) 갱신 — 라스트-관리자 잠김 방지."""
    _require_manage_system(user)
    perms = [p for p in db.PERMISSIONS if p in set(body.permissions)]
    with db.connect() as conn:
        group = db.get_permission_group(conn, name)
        if group is None:
            raise HTTPException(404, "권한 그룹 없음")
        simulated = dict(db.role_presets(conn))
        simulated[name] = frozenset(perms)
        if db.count_active_users_with_permission(
            conn, "manage_users", presets=simulated
        ) == 0:
            raise HTTPException(400, "사용자 관리 권한을 가진 활성 계정이 모두 사라집니다")
        db.update_permission_group(
            conn, name,
            label=None if group["is_builtin"] else body.label.strip(),
            permissions=perms,
        )
    audit.log(request, "권한 그룹 편집: %s", name)
    return {"ok": True}


@router.post("/system/groups/{name}/delete")
def system_group_delete(
    request: Request, name: str,
    user: sqlite3.Row | None = Depends(require_session),
) -> dict:
    """권한 그룹 삭제 — 빌트인·소속 사용자 있는 그룹은 거부."""
    _require_manage_system(user)
    with db.connect() as conn:
        group = db.get_permission_group(conn, name)
        if group is None:
            raise HTTPException(404, "권한 그룹 없음")
        if group["is_builtin"]:
            raise HTTPException(400, "기본 권한 그룹은 삭제할 수 없습니다")
        members = db.count_users_with_role(conn, name)
        if members > 0:
            raise HTTPException(400, f"{members}명이 이 그룹에 속해 있습니다")
        db.delete_permission_group(conn, name)
    audit.log(request, "권한 그룹 삭제: %s", name)
    return {"ok": True}


_API_KEY_EXPIRY_TTL: dict[str, int | None] = {
    "permanent": None, "1d": 86400, "1m": 30 * 86400, "1y": 365 * 86400,
}
_MAX_API_KEY_CUSTOM_DAYS = 3650


class ApiKeyReq(BaseModel):
    name: str
    can_view: bool = False
    can_archive: bool = False
    expiry: str = "permanent"
    custom_days: int = 0


_TIMEZONES = sorted(zoneinfo.available_timezones())  # 프로세스 상수 — 요청마다 재열거 방지


def _resolve_api_key_ttl(body: ApiKeyReq) -> int | None:
    """API 키 만료 선택을 ttl 초로 변환(None=영구). 잘못된 입력은 400."""
    if body.expiry in _API_KEY_EXPIRY_TTL:
        return _API_KEY_EXPIRY_TTL[body.expiry]
    if body.expiry == "custom":
        if not (1 <= body.custom_days <= _MAX_API_KEY_CUSTOM_DAYS):
            raise HTTPException(400, f"사용자 지정 만료는 1 ~ {_MAX_API_KEY_CUSTOM_DAYS}일 사이여야 합니다")
        return body.custom_days * 86400
    raise HTTPException(400, "알 수 없는 만료 선택")


def _public_api_key(k: sqlite3.Row) -> dict:
    """API 키 행에서 화면 표시용 필드만 추린다 — token_hash 등 비밀·내부 컬럼 제외(원칙 6)."""
    fields = ("id", "name", "can_view", "can_archive", "expires_at", "created_at", "last_used_at")
    cols = k.keys()
    return {f: k[f] for f in fields if f in cols}


@router.post("/system/api-keys")
def system_api_key_create(
    request: Request, body: ApiKeyReq,
    user: sqlite3.Row | None = Depends(require_session),
) -> dict:
    """시스템 API 키 발급 — 토큰 원문은 이 응답에서만 1회 반환."""
    _require_manage_users(user)
    name = body.name.strip()
    err = auth.validate_api_key_name(name)
    if err is not None:
        raise HTTPException(400, err)
    if not (body.can_view or body.can_archive):
        raise HTTPException(400, "권한을 하나 이상 선택하세요")
    ttl = _resolve_api_key_ttl(body)
    with db.connect() as conn:
        token = auth.issue_api_key(
            conn, name, can_view=body.can_view, can_archive=body.can_archive,
            created_by=user["id"] if user else None,
            ttl_seconds=ttl, owner_user_id=None,
        )
    audit.log(request, "API 키 발급: '%s'", name)
    return {"ok": True, "token": token}


@router.post("/system/api-keys/{key_id}/delete")
def system_api_key_delete(
    request: Request, key_id: int,
    user: sqlite3.Row | None = Depends(require_session),
) -> dict:
    """시스템 API 키 폐기 (owner=NULL 만)."""
    _require_manage_users(user)
    with db.connect() as conn:
        key = db.get_api_key(conn, key_id)
        if key is None or key["owner_user_id"] is not None:
            raise HTTPException(404, "API 키 없음")
        db.delete_api_key(conn, key_id)
    audit.log(request, "API 키 폐기: #%d", key_id)
    return {"ok": True}


# ── 시스템 현황 + 설정 ───────────────────────────────────────────────────────


@router.get("/system")
def system_overview(
    request: Request, user: sqlite3.Row | None = Depends(require_session)
) -> dict:
    """시스템 현황 + 설정 — system_routes.system_view 의 JSON 판.

    운영 액션(백업/복원/이전/재색인)은 별도 POST 로 둔다. 여기서는 현황·설정값과
    화면 폼 검증에 쓰는 한도 범위를 함께 내려준다.
    """
    _require_manage_system(user)
    with db.connect() as conn:
        counts = {
            tbl: conn.execute(f"SELECT COUNT(*) AS c FROM {tbl}").fetchone()["c"]
            for tbl in ("pages", "snapshots", "checks", "users")
        }
        signup_enabled = db.signup_enabled(conn)
        signup_default_role = db.signup_default_role(conn)
        signup_role_choices = db.signup_roles(conn)
        role_labels = db.role_labels(conn)
        email_verification_enabled = db.email_verification_enabled(conn)
        email_verification_ttl_minutes = db.email_verification_ttl_minutes(conn)
        auth_throttle_enabled = db.auth_throttle_enabled(conn)
        auth_throttle = db.auth_throttle_settings(conn)
        crawl_defaults = crawler.crawl_defaults(conn)
        crawl_backoff = crawler.retry_backoff(conn)
        network_tags = db.list_network_tags(conn)
        ext_credential_ttl_hours = db.ext_credential_ttl_hours(conn)
        mobile_screenshot_enabled = db.mobile_screenshot_enabled(conn)
        trash_enabled = db.trash_enabled(conn)
        trash_retention_days = db.trash_retention_days(conn)
        doc_limits = documents.limits(conn)
        smtp = mailer.resolve_config(conn)
        smtp_has_password = db.get_setting(conn, db.SMTP_PASSWORD_KEY) not in (None, "")
        migration_mode = db.migration_mode_enabled(conn)
        migration_token_created_at = db.get_setting(
            conn, db.MIGRATION_TOKEN_CREATED_AT_KEY
        )
    usage = storage.archive_disk_usage()
    return {
        "version": __version__,
        "counts": counts,
        "signup_enabled": signup_enabled,
        "signup_default_role": signup_default_role,
        "signup_roles": list(signup_role_choices),
        "role_labels": dict(role_labels),
        "email_verification_enabled": email_verification_enabled,
        "email_verification_ttl_minutes": email_verification_ttl_minutes,
        "email_verification_ttl_limits": {
            "min": config.EMAIL_VERIFICATION_TTL_MINUTES_MIN,
            "max": config.EMAIL_VERIFICATION_TTL_MINUTES_MAX,
        },
        "auth_throttle_enabled": auth_throttle_enabled,
        "auth_throttle": auth_throttle,
        "auth_throttle_limits": {
            "limit_min": config.AUTH_THROTTLE_LIMIT_MIN,
            "limit_max": config.AUTH_THROTTLE_LIMIT_MAX,
            "window_min": config.AUTH_THROTTLE_WINDOW_MIN,
            "window_max": config.AUTH_THROTTLE_WINDOW_MAX,
        },
        "crawl_defaults": {
            "max_pages": crawl_defaults["max_pages"],
            "max_depth": crawl_defaults["max_depth"],
            "delay": crawl_defaults["delay_seconds"],
        },
        "crawl_retry_backoff": ", ".join(str(v) for v in crawl_backoff),
        "crawl_limits": {
            "max_pages": config.CRAWL_MAX_PAGES_LIMIT,
            "max_depth": config.CRAWL_MAX_DEPTH_LIMIT,
            "min_delay": config.CRAWL_MIN_DELAY_SECONDS,
            "max_delay": config.CRAWL_MAX_DELAY_SECONDS,
        },
        "ext_credential_ttl_hours": ext_credential_ttl_hours,
        "ext_credential_ttl_limits": {
            "min": config.EXT_CREDENTIAL_TTL_HOURS_MIN,
            "max": config.EXT_CREDENTIAL_TTL_HOURS_MAX,
        },
        "mobile_screenshot_enabled": mobile_screenshot_enabled,
        "trash_enabled": trash_enabled,
        "trash_retention_days": trash_retention_days,
        "trash_retention_limits": {
            "min": config.TRASH_RETENTION_DAYS_MIN,
            "max": config.TRASH_RETENTION_DAYS_MAX,
        },
        "document_limits": {
            "max_count": doc_limits.max_count,
            "max_mb": doc_limits.max_bytes // (1024 * 1024),
            "timeout_seconds": doc_limits.timeout_seconds,
        },
        "document_limit_ranges": {
            "count_min": config.DOCUMENT_MAX_COUNT_MIN,
            "count_max": config.DOCUMENT_MAX_COUNT_MAX,
            "mb_min": config.DOCUMENT_MAX_MB_MIN,
            "mb_max": config.DOCUMENT_MAX_MB_MAX,
            "timeout_min": config.DOCUMENT_FETCH_TIMEOUT_MIN,
            "timeout_max": config.DOCUMENT_FETCH_TIMEOUT_MAX,
        },
        "network_tags": [dict(tg) for tg in network_tags],
        "credential_key_set": crypto.is_configured(),
        "smtp_config": {
            "host": smtp.host, "port": smtp.port, "user": smtp.user,
            "sender": smtp.sender, "tls": smtp.tls, "enabled": smtp.enabled,
            "has_password": smtp_has_password,
        },
        "smtp_tls_modes": list(mailer.SMTP_TLS_MODES),
        "archive_root": str(config.ARCHIVE_ROOT),
        "usage": {
            "db": usage["db"], "sites": usage["sites"],
            "resources": usage["resources"], "documents": usage["documents"],
        },
        "optimize_pending": sum(optimize.pending_counts()),
        "search": searchindex.verify(),
        "migration_mode": migration_mode,
        "migration_token_created_at": migration_token_created_at,
        "public_url": config.PUBLIC_URL,
    }


class SignupSettingsReq(BaseModel):
    signup_enabled: bool = False
    signup_default_role: str = "pending"


@router.post("/system/settings")
def system_settings(
    request: Request, body: SignupSettingsReq,
    user: sqlite3.Row | None = Depends(require_session),
) -> dict:
    """가입 설정 — system_routes.system_settings 의 JSON 판."""
    _require_manage_system(user)
    with db.connect() as conn:
        if body.signup_default_role not in db.signup_roles(conn):
            raise HTTPException(400, "가입 초기 권한으로 쓸 수 없는 역할")
        db.set_setting(conn, db.SIGNUP_ENABLED_KEY, "on" if body.signup_enabled else "off")
        db.set_setting(conn, db.SIGNUP_DEFAULT_ROLE_KEY, body.signup_default_role)
    audit.log(request, "가입 설정 변경")
    return {"ok": True}


class EmailVerifyReq(BaseModel):
    email_verification_enabled: bool = False
    email_verification_ttl_minutes: int


@router.post("/system/email-verification-settings")
def system_email_verification(
    request: Request, body: EmailVerifyReq,
    user: sqlite3.Row | None = Depends(require_session),
) -> dict:
    """이메일 본인 인증 설정 — JSON 판."""
    _require_manage_system(user)
    lo = config.EMAIL_VERIFICATION_TTL_MINUTES_MIN
    hi = config.EMAIL_VERIFICATION_TTL_MINUTES_MAX
    if not (lo <= body.email_verification_ttl_minutes <= hi):
        raise HTTPException(400, f"인증 코드 만료 시간은 {lo} ~ {hi}분 사이여야 합니다")
    with db.connect() as conn:
        db.set_setting(
            conn, db.EMAIL_VERIFICATION_ENABLED_KEY,
            "on" if body.email_verification_enabled else "off",
        )
        db.set_setting(
            conn, db.EMAIL_VERIFICATION_TTL_MINUTES_KEY,
            str(body.email_verification_ttl_minutes),
        )
    audit.log(request, "이메일 본인 인증 설정 변경")
    return {"ok": True}


class TrashSettingsReq(BaseModel):
    trash_enabled: bool = True
    trash_retention_days: int


@router.post("/system/trash-settings")
def system_trash_settings(
    request: Request, body: TrashSettingsReq,
    user: sqlite3.Row | None = Depends(require_session),
) -> dict:
    """휴지통 설정 — 기능 on/off + 보관 기간(일, 0=자동삭제 끔). JSON 판."""
    _require_manage_system(user)
    lo = config.TRASH_RETENTION_DAYS_MIN
    hi = config.TRASH_RETENTION_DAYS_MAX
    if not (lo <= body.trash_retention_days <= hi):
        raise HTTPException(400, f"보관 기간은 {lo} ~ {hi}일 사이여야 합니다")
    with db.connect() as conn:
        db.set_setting(
            conn, db.TRASH_ENABLED_KEY, "on" if body.trash_enabled else "off"
        )
        db.set_setting(
            conn, db.TRASH_RETENTION_DAYS_KEY, str(body.trash_retention_days)
        )
    audit.log(request, "휴지통 설정 변경")
    return {"ok": True}


class AuthThrottleReq(BaseModel):
    auth_throttle_enabled: bool = True
    login_limit: int
    login_ip_limit: int
    login_window_minutes: int
    totp_limit: int
    email_verify_limit: int
    email_resend_limit: int


@router.post("/system/auth-throttle-settings")
def system_auth_throttle(
    request: Request, body: AuthThrottleReq,
    user: sqlite3.Row | None = Depends(require_session),
) -> dict:
    """인증 무차별 대입 방어(rate limit) 설정 — 한도·창·전체 토글."""
    _require_manage_system(user)
    lo, hi = config.AUTH_THROTTLE_LIMIT_MIN, config.AUTH_THROTTLE_LIMIT_MAX
    wlo, whi = config.AUTH_THROTTLE_WINDOW_MIN, config.AUTH_THROTTLE_WINDOW_MAX
    limits = {
        db.AUTH_LOGIN_LIMIT_KEY: body.login_limit,
        db.AUTH_LOGIN_IP_LIMIT_KEY: body.login_ip_limit,
        db.AUTH_TOTP_LIMIT_KEY: body.totp_limit,
        db.AUTH_EMAIL_VERIFY_LIMIT_KEY: body.email_verify_limit,
        db.AUTH_EMAIL_RESEND_LIMIT_KEY: body.email_resend_limit,
    }
    for value in limits.values():
        if not (lo <= value <= hi):
            raise HTTPException(400, f"시도 한도는 {lo} ~ {hi} 사이여야 합니다")
    if not (wlo <= body.login_window_minutes <= whi):
        raise HTTPException(400, f"로그인 카운트 창은 {wlo} ~ {whi}분 사이여야 합니다")
    with db.connect() as conn:
        db.set_setting(
            conn, db.AUTH_THROTTLE_ENABLED_KEY,
            "on" if body.auth_throttle_enabled else "off",
        )
        for key, value in limits.items():
            db.set_setting(conn, key, str(value))
        db.set_setting(
            conn, db.AUTH_LOGIN_WINDOW_MINUTES_KEY, str(body.login_window_minutes))
    audit.log(request, "인증 보호(rate limit) 설정 변경")
    return {"ok": True}


class CrawlSettingsReq(BaseModel):
    crawl_max_pages: int
    crawl_max_depth: int
    crawl_delay: int
    crawl_retry_backoff: str


@router.post("/system/crawl-settings")
def system_crawl_settings(
    request: Request, body: CrawlSettingsReq,
    user: sqlite3.Row | None = Depends(require_session),
) -> dict:
    """사이트 아카이브 기본 옵션·재시도 대기 — JSON 판."""
    _require_manage_system(user)
    try:
        crawler.validate_options(body.crawl_max_pages, body.crawl_max_depth, body.crawl_delay)
        backoff = crawler.parse_backoff(body.crawl_retry_backoff)
    except ValueError as e:
        raise HTTPException(400, str(e))
    with db.connect() as conn:
        db.set_setting(conn, db.CRAWL_DEFAULT_MAX_PAGES_KEY, str(body.crawl_max_pages))
        db.set_setting(conn, db.CRAWL_DEFAULT_MAX_DEPTH_KEY, str(body.crawl_max_depth))
        db.set_setting(conn, db.CRAWL_DEFAULT_DELAY_KEY, str(body.crawl_delay))
        db.set_setting(conn, db.CRAWL_RETRY_BACKOFF_KEY, ",".join(str(v) for v in backoff))
    audit.log(request, "사이트 아카이브 설정 변경")
    return {"ok": True}


class CredentialSettingsReq(BaseModel):
    ext_credential_ttl_hours: int


@router.post("/system/credential-settings")
def system_credential_settings(
    request: Request, body: CredentialSettingsReq,
    user: sqlite3.Row | None = Depends(require_session),
) -> dict:
    """확장 1회성 자격증명 TTL — JSON 판."""
    _require_manage_system(user)
    lo, hi = config.EXT_CREDENTIAL_TTL_HOURS_MIN, config.EXT_CREDENTIAL_TTL_HOURS_MAX
    if not (lo <= body.ext_credential_ttl_hours <= hi):
        raise HTTPException(400, f"자격증명 보관 시간은 {lo} ~ {hi}시간 사이여야 합니다")
    with db.connect() as conn:
        db.set_setting(conn, db.EXT_CREDENTIAL_TTL_HOURS_KEY, str(body.ext_credential_ttl_hours))
    audit.log(request, "확장 자격증명 설정 변경")
    return {"ok": True}


class CaptureSettingsReq(BaseModel):
    mobile_screenshot_enabled: bool = False


@router.post("/system/capture-settings")
def system_capture_settings(
    request: Request, body: CaptureSettingsReq,
    user: sqlite3.Row | None = Depends(require_session),
) -> dict:
    """캡처 설정(모바일 스크린샷) — JSON 판."""
    _require_manage_system(user)
    with db.connect() as conn:
        db.set_setting(
            conn, db.MOBILE_SCREENSHOT_ENABLED_KEY,
            "on" if body.mobile_screenshot_enabled else "off",
        )
    audit.log(request, "캡처 설정 변경")
    return {"ok": True}


class DocumentSettingsReq(BaseModel):
    document_max_count: int
    document_max_mb: int
    document_fetch_timeout: int


@router.post("/system/document-settings")
def system_document_settings(
    request: Request, body: DocumentSettingsReq,
    user: sqlite3.Row | None = Depends(require_session),
) -> dict:
    """문서 아카이브 한도 — JSON 판."""
    _require_manage_system(user)
    ranges = (
        (body.document_max_count, config.DOCUMENT_MAX_COUNT_MIN, config.DOCUMENT_MAX_COUNT_MAX, "문서 수 한도"),
        (body.document_max_mb, config.DOCUMENT_MAX_MB_MIN, config.DOCUMENT_MAX_MB_MAX, "문서 크기 한도(MB)"),
        (body.document_fetch_timeout, config.DOCUMENT_FETCH_TIMEOUT_MIN, config.DOCUMENT_FETCH_TIMEOUT_MAX, "문서 다운로드 타임아웃(초)"),
    )
    for value, lo, hi, label in ranges:
        if not (lo <= value <= hi):
            raise HTTPException(400, f"{label}는 {lo} ~ {hi} 사이여야 합니다")
    with db.connect() as conn:
        db.set_setting(conn, db.DOCUMENT_MAX_COUNT_KEY, str(body.document_max_count))
        db.set_setting(conn, db.DOCUMENT_MAX_MB_KEY, str(body.document_max_mb))
        db.set_setting(conn, db.DOCUMENT_FETCH_TIMEOUT_KEY, str(body.document_fetch_timeout))
    audit.log(request, "문서 아카이브 설정 변경")
    return {"ok": True}


class NetworkTagReq(BaseModel):
    name: str
    description: str = ""


@router.post("/system/network-tags")
def system_network_tag_create(
    request: Request, body: NetworkTagReq,
    user: sqlite3.Row | None = Depends(require_session),
) -> dict:
    """로컬 네트워크 태그 추가 — system_routes.network_tags_create 의 JSON 판."""
    from .maintenance import MAX_NETWORK_TAG_NAME_LENGTH, MAX_NETWORK_TAG_DESC_LENGTH

    _require_manage_system(user)
    name = body.name.strip()
    description = body.description.strip()
    if not name:
        raise HTTPException(400, "태그 이름을 입력하세요.")
    if len(name) > MAX_NETWORK_TAG_NAME_LENGTH:
        raise HTTPException(400, f"태그 이름은 {MAX_NETWORK_TAG_NAME_LENGTH}자 이하여야 합니다.")
    if len(description) > MAX_NETWORK_TAG_DESC_LENGTH:
        raise HTTPException(400, f"태그 설명은 {MAX_NETWORK_TAG_DESC_LENGTH}자 이하여야 합니다.")
    with db.connect() as conn:
        if db.get_network_tag_by_name(conn, name) is not None:
            raise HTTPException(400, f"이미 있는 태그 이름입니다: {name}")
        db.create_network_tag(conn, name, description)
    audit.log(request, "로컬 네트워크 태그 추가: '%s'", name)
    return {"ok": True}


@router.post("/system/network-tags/{tag_id}/delete")
def system_network_tag_delete(
    request: Request, tag_id: str,
    user: sqlite3.Row | None = Depends(require_session),
) -> dict:
    """로컬 네트워크 태그 삭제 — 참조 중이면 거부. system_routes.network_tags_delete 의 JSON 판."""
    _require_manage_system(user)
    with db.connect() as conn:
        tag = db.get_network_tag(conn, tag_id)
        if tag is None:
            raise HTTPException(404, "로컬 네트워크 태그 없음")
        refs = db.count_network_tag_refs(conn, tag_id)
        if refs:
            raise HTTPException(
                400, f"'{tag['name']}' 태그는 사용 중이라 삭제할 수 없습니다 (참조 {refs}개).")
        db.delete_network_tag(conn, tag_id)
    audit.log(request, "로컬 네트워크 태그 삭제: '%s'", tag["name"])
    return {"ok": True}


class NetworkTagMergeReq(BaseModel):
    source: str
    target: str


@router.post("/system/network-tags/merge")
def system_network_tag_merge(
    request: Request, body: NetworkTagMergeReq,
    user: sqlite3.Row | None = Depends(require_session),
) -> dict:
    """두 로컬 네트워크 태그 병합 — 같은 사설 네트워크를 가리킬 때만. JSON 판."""
    _require_manage_system(user)
    with db.connect() as conn:
        src = db.get_network_tag(conn, body.source)
        tgt = db.get_network_tag(conn, body.target)
        if src is None or tgt is None:
            raise HTTPException(404, "로컬 네트워크 태그 없음")
        if body.source == body.target:
            raise HTTPException(400, "같은 태그끼리는 병합할 수 없습니다.")
        src_sites = db.network_tag_site_ids(conn, body.source)
        tgt_sites = db.network_tag_site_ids(conn, body.target)
        if not src_sites or not tgt_sites:
            raise HTTPException(400, "참조가 없는 태그는 병합할 수 없습니다 — 삭제를 사용하세요.")
        if src_sites != tgt_sites:
            raise HTTPException(
                400,
                "두 태그가 같은 사설 네트워크(같은 IP·포트)를 가리킬 때만 병합할 수 있습니다.")
        moved = db.merge_network_tags(conn, body.source, body.target)
    audit.log(request, "로컬 네트워크 태그 병합: '%s' → '%s'", src["name"], tgt["name"])
    return {"ok": True, "moved": moved}


class SmtpSettingsReq(BaseModel):
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_tls: str = "starttls"
    smtp_clear_password: bool = False


@router.post("/system/smtp-settings")
def system_smtp_settings(
    request: Request, body: SmtpSettingsReq,
    user: sqlite3.Row | None = Depends(require_session),
) -> dict:
    """초대·인증 메일 SMTP 설정 — system_routes.system_smtp_settings 의 JSON 판.

    비밀번호는 대칭 암호화 암호문으로만 저장(원칙 6 예외). 빈 입력이면 기존
    저장값 유지, smtp_clear_password 면 삭제.
    """
    _require_manage_system(user)
    host = body.smtp_host.strip()
    if body.smtp_tls not in mailer.SMTP_TLS_MODES:
        raise HTTPException(400, "TLS 모드가 올바르지 않습니다.")
    if not (1 <= body.smtp_port <= 65535):
        raise HTTPException(400, "SMTP 포트는 1 ~ 65535 사이여야 합니다.")
    if body.smtp_password and not crypto.is_configured():
        raise HTTPException(
            400, "WCCG_SECRET_KEY 가 설정되지 않아 SMTP 비밀번호를 저장할 수 없습니다.")
    with db.connect() as conn:
        db.set_setting(conn, db.SMTP_HOST_KEY, host)
        db.set_setting(conn, db.SMTP_PORT_KEY, str(body.smtp_port))
        db.set_setting(conn, db.SMTP_USER_KEY, body.smtp_user.strip())
        db.set_setting(conn, db.SMTP_FROM_KEY, body.smtp_from.strip())
        db.set_setting(conn, db.SMTP_TLS_KEY, body.smtp_tls)
        if body.smtp_password:
            db.set_setting(conn, db.SMTP_PASSWORD_KEY, crypto.encrypt(body.smtp_password))
        elif body.smtp_clear_password:
            db.delete_setting(conn, db.SMTP_PASSWORD_KEY)
    audit.log(request, "메일(SMTP) 설정 변경: 호스트 %s, 포트 %d, TLS %s",
              host or "(없음)", body.smtp_port, body.smtp_tls)
    return {"ok": True}


@router.post("/system/smtp-test")
def system_smtp_test(
    request: Request, user: sqlite3.Row | None = Depends(require_session)
) -> dict:
    """저장된 SMTP 설정으로 요청 관리자 본인에게 테스트 메일 발송."""
    _require_manage_system(user)
    to_email = user["email"] if user else ""
    if not to_email:
        raise HTTPException(400, "테스트 메일을 받을 이메일 주소가 없습니다.")
    with db.connect() as conn:
        smtp = mailer.resolve_config(conn)
    if not smtp.enabled:
        raise HTTPException(400, "SMTP 호스트가 설정되지 않았습니다.")
    try:
        mailer.send_test(smtp, to_email)
    except (smtplib.SMTPException, OSError) as e:
        raise HTTPException(502, f"테스트 메일 발송에 실패했습니다: {e}")
    audit.log(request, "SMTP 테스트 메일 발송: %s", to_email)
    return {"ok": True, "email": to_email}


@router.post("/system/backup")
def system_backup(
    request: Request, user: sqlite3.Row | None = Depends(require_session)
) -> FileResponse:
    """전체 백업(.ccg.backup) 다운로드 — system_routes.system_backup 의 JSON 라우터 판."""
    from .maintenance import tar_download
    from .. import backup as backup_mod

    _require_manage_system(user)
    audit.log(request, "전체 백업 다운로드")
    return tar_download(backup_mod.create_backup, "backup")


@router.post("/system/export")
def system_export(
    request: Request, user: sqlite3.Row | None = Depends(require_session)
) -> FileResponse:
    """아카이브 데이터만 내보내기(.ccg.export) 다운로드 — 인증 데이터 제외."""
    from .maintenance import tar_download
    from .. import backup as backup_mod

    _require_manage_system(user)
    audit.log(request, "아카이브 내보내기 다운로드")
    return tar_download(backup_mod.export_archive, "export")


@router.post("/system/restore")
def system_restore(
    request: Request, file: UploadFile = File(...),
    user: sqlite3.Row | None = Depends(require_session),
) -> dict:
    """전체 백업 업로드로 복원 — 현재 데이터(인증 포함)를 백업 시점으로 교체."""
    import tarfile

    from .maintenance import _save_upload
    from .. import backup as backup_mod

    _require_manage_system(user)
    if not backup_mod.is_backup_filename(file.filename or ""):
        raise HTTPException(400, "복원은 .ccg.backup 확장자 파일만 받습니다.")
    tmp = _save_upload(file)
    try:
        manifest = backup_mod.restore_backup(tmp)
    except (ValueError, tarfile.TarError, OSError) as e:
        raise HTTPException(400, f"복원 실패: {e}")
    finally:
        tmp.unlink(missing_ok=True)
    audit.log(request, "백업 복원 실행 (백업: %s)", manifest.get("created_at", "?"))
    return {"ok": True, "manifest": manifest}


@router.post("/system/import")
def system_import(
    request: Request, file: UploadFile = File(...), mode: str = Form("merge"),
    user: sqlite3.Row | None = Depends(require_session),
) -> dict:
    """내보낸 아카이브 데이터 업로드로 가져오기 — 인증 데이터는 건드리지 않음."""
    import tarfile

    from .maintenance import _save_upload
    from .. import backup as backup_mod

    _require_manage_system(user)
    if mode not in ("merge", "overwrite"):
        raise HTTPException(400, f"알 수 없는 모드: {mode!r}")
    if not backup_mod.is_export_filename(file.filename or ""):
        raise HTTPException(400, "가져오기는 .ccg.export 확장자 파일만 받습니다.")
    tmp = _save_upload(file)
    try:
        result = backup_mod.import_archive(tmp, mode=mode)
    except (ValueError, tarfile.TarError, OSError) as e:
        raise HTTPException(400, f"가져오기 실패: {e}")
    finally:
        tmp.unlink(missing_ok=True)
    audit.log(request, "아카이브 가져오기 [%s]: 페이지 +%d, 스냅샷 +%d",
              mode, result.pages_added, result.snapshots_added)
    return {
        "ok": True,
        "added": {
            "pages": result.pages_added, "snapshots": result.snapshots_added,
            "skipped": result.snapshots_skipped, "checks": result.checks_added,
            "crawls": result.crawls_added, "certificates": result.certificates_added,
            "logs": result.logs_added,
        },
    }


@router.post("/system/compact")
def system_compact(
    request: Request, user: sqlite3.Row | None = Depends(require_session)
) -> dict:
    """저장공간 최적화(동기) — system_routes.system_compact 의 JSON 판. 멱등·내용 보존."""
    _require_manage_system(user)
    if sum(optimize.pending_counts()) == 0:
        return {"ok": True, "ran": False}
    try:
        result = optimize.run()
    except OSError as e:
        raise HTTPException(500, f"최적화 실패: {e}")
    audit.log(request, "저장공간 최적화 실행")
    c = result.compact
    return {
        "ok": True, "ran": True,
        "result": {
            "converted": c.converted, "total": c.total,
            "externalized": c.externalized, "documents": c.documents,
            "styles_extracted": result.styles_extracted,
            "indexed": result.indexed, "swept": result.swept,
            "saved_bytes": c.saved_bytes + result.styles_saved_bytes + result.swept_bytes,
        },
    }


@router.post("/system/search/reindex")
def system_search_reindex(
    request: Request, user: sqlite3.Row | None = Depends(require_session)
) -> dict:
    """검색 인덱스 전체 다시 색인을 백그라운드로 시작 — SSR 과 같은 인메모리 상태 공유."""
    import threading

    from .maintenance import _reindex_lock, _reindex_state, _reindex_worker

    _require_manage_system(user)
    if not searchindex.available():
        raise HTTPException(
            400, "검색 인덱스를 쓸 수 없습니다 — 이 SQLite 빌드에 FTS5 가 없습니다.")
    with _reindex_lock:
        if _reindex_state["running"]:
            return {"ok": True, "started": False, "already_running": True}
        _reindex_state.update(
            running=True, done=0, total=0, result=None, error=None, finished_at=None)
    audit.log(request, "검색 인덱스 전체 다시 색인 시작")
    threading.Thread(target=_reindex_worker, daemon=True).start()
    return {"ok": True, "started": True}


@router.get("/system/search/reindex/status")
def system_search_reindex_status(
    request: Request, user: sqlite3.Row | None = Depends(require_session)
) -> dict:
    """전체 다시 색인 진행 상태(JSON) — 시스템 화면 폴링용."""
    from .maintenance import reindex_status

    _require_manage_system(user)
    return reindex_status()


def _issue_migration_token(request: Request) -> dict:
    """이전 토큰 발급(또는 재발급)하고 모드를 켠다. 토큰 원문은 이 응답에서만 1회 노출.

    토큰은 SHA-256 해시만 저장(원칙 6 단방향, API 키와 동일).
    """
    token = secrets.token_urlsafe(32)
    with db.connect() as conn:
        db.set_migration_mode(conn, True, auth.hash_token(token))
    return {"ok": True, "token": token}


@router.post("/system/migration/enable")
def system_migration_enable(
    request: Request, user: sqlite3.Row | None = Depends(require_session)
) -> dict:
    """이전 모드 ON + 토큰 발급 — 받는 쪽이 이 토큰으로 데이터를 가져간다."""
    _require_manage_system(user)
    audit.log(request, "이전(마이그레이션) 모드 켬 — 토큰 발급")
    return _issue_migration_token(request)


@router.post("/system/migration/regenerate")
def system_migration_regenerate(
    request: Request, user: sqlite3.Row | None = Depends(require_session)
) -> dict:
    """이전 토큰 재발급 (이전 토큰 무효화). 모드는 켜진 채로 둔다."""
    _require_manage_system(user)
    audit.log(request, "이전(마이그레이션) 토큰 재발급")
    return _issue_migration_token(request)


@router.post("/system/migration/disable")
def system_migration_disable(
    request: Request, user: sqlite3.Row | None = Depends(require_session)
) -> dict:
    """이전 모드 OFF + 토큰 무효화 — 스크래핑·스케줄·크롤을 재개한다."""
    from .. import migration as migration_mod

    _require_manage_system(user)
    with db.connect() as conn:
        db.set_migration_mode(conn, False)
    migration_mod.cleanup_source()
    audit.log(request, "이전(마이그레이션) 모드 끔 — 스크래핑 재개")
    return {"ok": True}


# ── 개인 설정 (계정·개인 API Key·내 아카이브) ────────────────────────────────
# 헤더 개인설정 드롭다운의 세 화면. SSR auth_routes 의 /settings/* 와 같은 코어
# 동작을 JSON 으로 제공한다 (원칙 1·6 — 인증 데이터는 단방향, 쓰기는 코어 경유).


def _require_account(user: sqlite3.Row | None) -> sqlite3.Row:
    """개인 계정 컨텍스트 강제 — 인증 off(loopback)에는 '본인'이 없어 404."""
    if user is None:
        raise HTTPException(404, "인증이 비활성화되어 개인 계정이 없습니다")
    return user


@router.get("/settings/account")
def account(
    request: Request, user: sqlite3.Row | None = Depends(require_session)
) -> dict:
    """계정 설정 — 표시이름·언어·타임존·패스워드·2FA/패스키 상태. auth.account_page 의 JSON 판."""
    user = _require_account(user)
    with db.connect() as conn:
        passkeys = db.list_passkeys(conn, user["id"])
        email_verification_on = (
            db.email_verification_enabled(conn) and mailer.mail_enabled(conn)
        )
        role_label = db.role_labels(conn).get(user["role"], user["role"])
    return {
        "display_name": user["display_name"] or "",
        "email": user["email"],
        "role": user["role"],
        "role_label": role_label,
        "is_admin": user["role"] == "admin",
        "has_password": user["password_hash"] is not None,
        "totp_enabled": user["totp_secret"] is not None,
        "passkey_count": len(passkeys),
        "passkeys": [
            {
                "id": c["id"], "name": c["name"],
                "created_at": c["created_at"], "last_used_at": c["last_used_at"],
            }
            for c in passkeys
        ],
        "email_verified": bool(user["email_verified"]),
        "email_verification_on": email_verification_on,
        "timezone": user["timezone"] or "UTC",
        "timezones": _TIMEZONES,
        "locale": user["locale"] or i18n.DEFAULT_LOCALE,
        "locales": list(i18n.SUPPORTED_LOCALES),
        "locale_names": i18n.LOCALE_NAMES,
    }


class AccountNameReq(BaseModel):
    display_name: str = ""


@router.post("/settings/account/name")
def account_name(
    request: Request, body: AccountNameReq,
    user: sqlite3.Row | None = Depends(require_session),
) -> dict:
    """표시 이름 변경 — 빈 입력이면 제거(이메일 표시로 복귀)."""
    user = _require_account(user)
    name = body.display_name.strip() or None
    if name is not None:
        err = auth.validate_display_name(name)
        if err is not None:
            raise HTTPException(400, err)
    with db.connect() as conn:
        db.set_display_name(conn, user["id"], name)
    return {"ok": True, "display_name": name or ""}


class AccountLocaleReq(BaseModel):
    locale: str


@router.post("/settings/account/language")
def account_language(
    request: Request, body: AccountLocaleReq,
    user: sqlite3.Row | None = Depends(require_session),
) -> dict:
    """표시 언어 변경."""
    user = _require_account(user)
    lang = body.locale.strip()
    if lang not in i18n.SUPPORTED_LOCALES:
        raise HTTPException(400, "지원하지 않는 언어입니다")
    with db.connect() as conn:
        db.set_user_locale(conn, user["id"], lang)
    return {"ok": True}


class AccountTimezoneReq(BaseModel):
    timezone: str


@router.post("/settings/account/timezone")
def account_timezone(
    request: Request, body: AccountTimezoneReq,
    user: sqlite3.Row | None = Depends(require_session),
) -> dict:
    """표시 타임존 변경."""
    user = _require_account(user)
    tz = body.timezone.strip()
    if tz not in _TIMEZONES:
        raise HTTPException(400, "지원하지 않는 타임존입니다")
    with db.connect() as conn:
        db.set_user_timezone(conn, user["id"], tz)
    return {"ok": True}


class PasswordChangeReq(BaseModel):
    current_password: str
    new_password: str
    new_password2: str


@router.post("/settings/account/password")
def account_password(
    request: Request, body: PasswordChangeReq,
    user: sqlite3.Row | None = Depends(require_session),
) -> dict:
    """패스워드 변경 — 변경 후 현재 세션만 남기고 다른 기기 세션을 무효화한다."""
    user = _require_account(user)
    if user["password_hash"] is None:
        raise HTTPException(400, "SSO 전용 계정은 패스워드가 없습니다. IdP(Authentik)에서 관리하세요.")
    if not auth.verify_password(user["password_hash"], body.current_password):
        raise HTTPException(401, "현재 패스워드가 올바르지 않습니다.")
    if body.new_password != body.new_password2:
        raise HTTPException(400, "새 패스워드가 서로 일치하지 않습니다.")
    err = auth.validate_password(body.new_password)
    if err is not None:
        raise HTTPException(400, err)
    with db.connect() as conn:
        db.set_password_hash(conn, user["id"], auth.hash_password(body.new_password))
        db.delete_other_sessions(
            conn, user["id"], request.state.session["token_hash"]
        )
    return {"ok": True}


class WithdrawReq(BaseModel):
    password: str = ""
    confirm: str = ""


@router.post("/settings/account/withdraw")
def account_withdraw(
    request: Request, body: WithdrawReq,
    user: sqlite3.Row | None = Depends(require_session),
) -> JSONResponse:
    """본인 계정 탈퇴 (관리자 불가) — 권한을 탈퇴 상태로 바꾸고 전 세션을 무효화한다.

    탈취된 세션으로 임의 탈퇴를 막기 위해 패스워드 계정은 패스워드 재입력,
    SSO 전용 계정은 이메일 입력으로 본인을 재확인한다.
    """
    user = _require_account(user)
    if user["role"] == "admin":
        raise HTTPException(403, "관리자 계정은 탈퇴할 수 없습니다.")
    if user["password_hash"] is not None:
        if not auth.verify_password(user["password_hash"], body.password):
            raise HTTPException(401, "패스워드가 올바르지 않습니다.")
    elif body.confirm.strip().lower() != user["email"].lower():
        raise HTTPException(400, "확인 이메일이 일치하지 않습니다.")
    with db.connect() as conn:
        db.withdraw_user(conn, user["id"])
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(config.SESSION_COOKIE)
    return resp


@router.get("/settings/api-keys")
def personal_api_keys(
    request: Request, user: sqlite3.Row | None = Depends(require_session)
) -> dict:
    """개인 API Key(확장 토큰) 목록 — 본인 소유분만. use_api_keys 권한 전용."""
    user = _require_account(user)
    if not permissions.can_use_api_keys(user):
        raise HTTPException(403, "개인 API Key 사용 권한이 없습니다.")
    can_view, can_archive = permissions.token_permissions_for_user(user)
    with db.connect() as conn:
        tokens = db.list_api_keys_for_owner(conn, user["id"])
    return {
        "keys": [_public_api_key(k) for k in tokens],
        "can_view": can_view,
        "can_archive": can_archive,
    }


@router.post("/settings/api-keys")
def personal_api_key_create(
    request: Request, body: ApiKeyReq,
    user: sqlite3.Row | None = Depends(require_session),
) -> dict:
    """본인 귀속 개인 API Key 발급 — 권한은 역할 범위로 클램프, 토큰 원문은 1회만 반환."""
    user = _require_account(user)
    if not permissions.can_use_api_keys(user):
        raise HTTPException(403, "개인 API Key 사용 권한이 없습니다.")
    allowed_view, allowed_archive = permissions.token_permissions_for_user(user)
    if not (allowed_view or allowed_archive):
        raise HTTPException(403, "현재 권한으로는 API Key 를 발급할 수 없습니다.")
    # 선택 권한을 역할 허용 범위로 클램프 — 권한 상승 방지
    can_view = body.can_view and allowed_view
    can_archive = body.can_archive and allowed_archive
    if not (can_view or can_archive):
        raise HTTPException(400, "권한을 하나 이상 선택하세요.")
    name = body.name.strip()
    err = auth.validate_api_key_name(name)
    if err is not None:
        raise HTTPException(400, err)
    ttl = _resolve_api_key_ttl(body)
    with db.connect() as conn:
        token = auth.issue_api_key(
            conn, name, can_view=can_view, can_archive=can_archive,
            created_by=user["id"], owner_user_id=user["id"], ttl_seconds=ttl,
        )
    perms = ", ".join(
        label for flag, label in ((can_view, "보기"), (can_archive, "아카이브")) if flag
    )
    audit.log(request, "개인 API Key 발급: '%s' (권한: %s)", name, perms)
    return {"ok": True, "token": token}


@router.post("/settings/api-keys/{key_id}/delete")
def personal_api_key_delete(
    request: Request, key_id: int,
    user: sqlite3.Row | None = Depends(require_session),
) -> dict:
    """본인 귀속 개인 API Key 폐기 — 본인 소유분만(IDOR 방어), 즉시 무효화."""
    user = _require_account(user)
    with db.connect() as conn:
        key = db.get_api_key(conn, key_id)
        # 본인 소유 토큰만 — 타인 토큰·시스템 키(owner=NULL)는 404 로 은폐
        if key is None or key["owner_user_id"] != user["id"]:
            raise HTTPException(404, "API Key 없음")
        db.delete_api_key(conn, key_id)
    audit.log(request, "개인 API Key 폐기: '%s'", key["name"])
    return {"ok": True}


@router.get("/settings/archives")
def my_archives(
    request: Request,
    status: str | None = None,
    page: int = 1,
    limit: int = _LOG_PAGE_SIZE_DEFAULT,
    user: sqlite3.Row | None = Depends(require_session),
) -> dict:
    """내 아카이브 — 본인이 직접 요청한(web·확장 토큰) 단발 아카이빙 이력. app.my_archives 의 JSON 판."""
    from .app import _requester_id

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
            total = db.count_archive_logs(conn, requested_by=requester, status=status)
            total_pages = max(1, -(-total // limit))
            page = max(1, min(page, total_pages))
            rows = db.list_archive_logs(
                conn, requested_by=requester, status=status,
                limit=limit, offset=(page - 1) * limit,
            )
        items = [{"log": dict(row)} for row in rows]
    return {
        "items": items,
        "status": status or "",
        "limit": limit,
        "limits": list(_LOG_PAGE_SIZES),
        "statuses": list(_LOG_STATUSES),
        "total": total,
        "total_pages": total_pages,
        "page_num": page,
    }


# ── 개인 2단계 인증 (TOTP) ───────────────────────────────────────────────────
# 패스워드 로그인의 2단계로만 쓴다 (SSO 는 IdP 2FA 신뢰). auth.totp_setup_page
# /totp_confirm/totp_disable 의 JSON 판. 패스키(WebAuthn) 설정은 별도.


@router.post("/settings/totp/setup")
def totp_setup(
    request: Request, user: sqlite3.Row | None = Depends(require_session)
) -> dict:
    """2단계 인증 설정 시작 — 시크릿을 pending 으로 두고 QR(provisioning URI)을 반환."""
    user = _require_account(user)
    if user["password_hash"] is None:
        raise HTTPException(400, "SSO 전용 계정은 2단계 인증을 설정할 수 없습니다.")
    if user["totp_secret"] is not None:
        raise HTTPException(409, "이미 2단계 인증이 설정되어 있습니다.")
    secret = auth.new_totp_secret()
    with db.connect() as conn:
        db.set_totp_pending(conn, user["id"], secret)
    return {
        "secret": secret,
        "qr": auth.qr_data_uri(auth.totp_provisioning_uri(secret, user["email"])),
    }


class TotpConfirmReq(BaseModel):
    code: str


@router.post("/settings/totp/confirm")
def totp_confirm(
    request: Request, body: TotpConfirmReq,
    user: sqlite3.Row | None = Depends(require_session),
) -> dict:
    """pending 시크릿을 사용자가 입력한 코드로 확인 → 2단계 인증 활성화."""
    user = _require_account(user)
    with db.connect() as conn:
        fresh = db.get_user_by_id(conn, user["id"])
        pending = fresh["totp_pending_secret"]
        window = pending and auth.verify_totp(pending, body.code, None)
        if not window:
            raise HTTPException(400, "코드가 올바르지 않습니다. QR 을 다시 스캔 후 시도하세요.")
        db.confirm_totp(conn, user["id"])
        db.set_totp_last_used(conn, user["id"], window)
    return {"ok": True}


class TotpDisableReq(BaseModel):
    password: str


@router.post("/settings/totp/disable")
def totp_disable(
    request: Request, body: TotpDisableReq,
    user: sqlite3.Row | None = Depends(require_session),
) -> dict:
    """2단계 인증 해제 — 세션 탈취로 2FA 를 무력화하지 못하도록 패스워드 재확인."""
    user = _require_account(user)
    if user["password_hash"] is None or not auth.verify_password(
        user["password_hash"], body.password
    ):
        raise HTTPException(401, "패스워드가 올바르지 않습니다.")
    with db.connect() as conn:
        db.disable_totp(conn, user["id"])
    return {"ok": True}


# ── 개인 2단계 인증 (패스키 / WebAuthn) ───────────────────────────────────────
# TOTP 와 동일하게 패스워드 로그인의 2단계 수단. challenge 는 세션 행에 저장해
# options→register 사이를 잇는다. auth_routes 의 SSR /settings/passkey 의 JSON 판.


@router.post("/settings/passkey/options")
def passkey_register_options(
    request: Request, user: sqlite3.Row | None = Depends(require_session)
) -> Response:
    """패스키 등록 옵션 발급 — 이미 등록된 자격증명은 제외 목록으로 전달."""
    user = _require_account(user)
    if user["password_hash"] is None:
        raise HTTPException(400, "SSO 전용 계정은 패스키를 등록할 수 없습니다.")
    with db.connect() as conn:
        creds = db.list_passkeys(conn, user["id"])
        options_json, challenge = auth.passkey_registration_options(
            user["id"], user["email"], [c["credential_id"] for c in creds]
        )
        db.set_session_challenge(
            conn, request.state.session["token_hash"], challenge
        )
    return Response(content=options_json, media_type="application/json")


@router.post("/settings/passkey/register")
async def passkey_register(
    request: Request, user: sqlite3.Row | None = Depends(require_session)
) -> dict:
    """패스키 등록 응답 검증 → 저장. credential 구조가 중첩이라 raw JSON 으로 받는다."""
    user = _require_account(user)
    if user["password_hash"] is None:
        raise HTTPException(400, "SSO 전용 계정은 패스키를 등록할 수 없습니다.")
    body = await request.json()
    credential = body.get("credential")
    if not isinstance(credential, dict):
        raise HTTPException(400, "credential 누락")
    name = (str(body.get("name") or "").strip() or "패스키")[:64]
    with db.connect() as conn:
        challenge = db.consume_session_challenge(
            conn, request.state.session["token_hash"]
        )
        if challenge is None:
            raise HTTPException(400, "진행 중인 등록이 없습니다 — 다시 시도하세요")
        verified = auth.verify_passkey_registration(credential, challenge)
        if verified is None:
            raise HTTPException(400, "패스키 등록 검증에 실패했습니다")
        try:
            db.create_passkey(conn, user["id"], name=name, **verified)
        except sqlite3.IntegrityError:
            raise HTTPException(400, "이미 등록된 패스키입니다")
    audit.log(request, "패스키 등록: '%s'", name)
    return {"ok": True}


class PasskeyDeleteReq(BaseModel):
    password: str


@router.post("/settings/passkey/{passkey_id}/delete")
def passkey_delete(
    request: Request, passkey_id: int, body: PasskeyDeleteReq,
    user: sqlite3.Row | None = Depends(require_session),
) -> dict:
    """패스키 삭제 — 세션 탈취로 2FA 를 무력화하지 못하도록 패스워드 재확인."""
    user = _require_account(user)
    if user["password_hash"] is None or not auth.verify_password(
        user["password_hash"], body.password
    ):
        raise HTTPException(401, "패스워드가 올바르지 않습니다.")
    with db.connect() as conn:
        if not db.delete_passkey(conn, user["id"], passkey_id):
            raise HTTPException(404, "패스키 없음")
    audit.log(request, "패스키 삭제: #%d", passkey_id)
    return {"ok": True}
