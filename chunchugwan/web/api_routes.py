"""외부 소프트웨어용 REST API (/api/v1) — API 키 인증.

- 인증: `Authorization: Bearer <키>` 또는 `X-API-Key: <키>` 헤더.
  키는 관리자가 /system/api-keys 에서 발급하며, 키마다 보기(can_view)/
  아카이브(can_archive) 권한과 만료 시각을 가진다.
- 인증이 꺼진 환경(loopback)은 단일 사용자 로컬 도구로 보고 키 없이 전부
  허용한다 (웹 UI 와 동일 원칙 — permissions.py 참조).
- 쓰기는 코어 모듈(pipeline)만 호출한다 (CLAUDE.md 원칙 1).
- 미들웨어(auth_gate)는 /api/ 경로를 세션 인증 대상에서 제외한다 —
  키 검증은 이 라우터의 의존성이 전담한다.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from .. import auth, config, crawler, credentials, crypto, db, netcheck, storage
from . import audit, permissions

# 스냅샷 파일 응답에서 안내하는 논리 파일 이름 (서빙은 app.snapshot_file 공용)
_SNAPSHOT_FILE_NAMES = ("page.html", "screenshot", "content.md")


def _extract_token(request: Request) -> str:
    """Authorization: Bearer 또는 X-API-Key 헤더에서 키 원문 추출."""
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return request.headers.get("x-api-key", "").strip()


def _api_auth(request: Request) -> None:
    """API 키 게이트 — 유효 키를 request.state.api_key 에 적재.

    인증이 꺼진 환경은 키 없이 통과한다 (api_key=None = 전체 허용).
    사용자 귀속 확장 토큰(owner_user_id 보유)은 권한을 저장 컬럼이 아니라
    소유자의 *현재* 역할에서 매 요청 재평가한다 — 역할 강등·차단·탈퇴가
    즉시 반영되고, 권한 없는 역할(pending/blocked/withdrawn)이면 401 로 막는다.
    """
    request.state.api_key = None
    if not config.AUTH_ENABLED:
        return
    token = _extract_token(request)
    unauthorized = HTTPException(
        401, "유효한 API 키가 필요합니다",
        headers={"WWW-Authenticate": "Bearer"},
    )
    with db.connect() as conn:
        key = auth.resolve_api_key(conn, token) if token else None
        if key is None:
            raise unauthorized
        db.touch_api_key(conn, key["id"])
        if key["owner_user_id"] is not None:
            owner = db.get_user_by_id(conn, key["owner_user_id"])
            if owner is None or owner["role"] not in permissions.TOKEN_ROLES:
                raise HTTPException(
                    401, "토큰 소유자의 권한이 없습니다",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            can_view, can_archive = permissions.token_permissions_for_user(owner)
            key = dict(key)
            key["can_view"] = int(can_view)
            key["can_archive"] = int(can_archive)
    request.state.api_key = key


router = APIRouter(prefix="/api/v1", dependencies=[Depends(_api_auth)])


def _require_view(request: Request) -> None:
    """조회 권한 가드. 키가 없는 경우(인증 off)는 전체 허용."""
    key = request.state.api_key
    if key is not None and not key["can_view"]:
        raise HTTPException(403, "이 키에는 보기 권한이 없습니다")


def _require_archive(request: Request) -> None:
    """아카이빙 권한 가드. 키가 없는 경우(인증 off)는 전체 허용."""
    key = request.state.api_key
    if key is not None and not key["can_archive"]:
        raise HTTPException(403, "이 키에는 아카이브 권한이 없습니다")


def _page_json(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "url": row["url"],
        "domain": row["domain"],
        "created_at": row["created_at"],
        "snapshot_count": row["snapshot_count"],
        "last_taken_at": row["last_taken_at"],
    }


def _snapshot_json(snap: sqlite3.Row) -> dict:
    return {
        "id": snap["id"],
        "page_id": snap["page_id"],
        "taken_at": snap["taken_at"],
        "content_hash": snap["content_hash"],
        "final_url": snap["final_url"],
        "http_status": snap["http_status"],
        "changed": bool(snap["changed"]),
        "files": {
            name: f"/api/v1/snapshots/{snap['id']}/file/{name}"
            for name in _SNAPSHOT_FILE_NAMES
        },
    }


@router.get("/pages")
def api_pages(request: Request, url: str | None = None):
    """아카이브된 페이지 목록. url 쿼리로 단일 페이지 조회 (정규화 후 일치)."""
    _require_view(request)
    from . import app as webapp  # 순환 임포트 방지 — app 이 이 모듈을 임포트한다

    # 인증 스냅샷은 소유자/관리자만 카운트·시각에 반영 (집계 메타데이터 누출 차단)
    with db.connect() as conn:
        pages = db.list_pages(conn, viewer=webapp._snapshot_viewer(request))
    if url is not None:
        try:
            norm = storage.normalize_url(url)
        except ValueError as e:
            raise HTTPException(400, f"잘못된 URL: {e}")
        pages = [p for p in pages if p["url"] == norm]
    return {"pages": [_page_json(p) for p in pages]}


@router.get("/pages/{page_id}")
def api_page(request: Request, page_id: int):
    """페이지 상세 — 스냅샷 히스토리 포함 (오래된 순)."""
    _require_view(request)
    with db.connect() as conn:
        page = db.get_page_by_id(conn, page_id)
        if page is None:
            raise HTTPException(404, "페이지 없음")
        snaps = db.list_snapshots(conn, page_id)
    from . import app as webapp  # 순환 임포트 방지 — app 이 이 모듈을 임포트한다

    # 로그인 캡처 스냅샷은 소유자/관리자에게만 노출 — 메타데이터도 가린다
    snaps = [
        s for s in snaps
        if not s["authenticated"] or webapp._may_view_authenticated(request, s)
    ]
    return {
        "id": page["id"],
        "url": page["url"],
        "domain": page["domain"],
        "created_at": page["created_at"],
        "snapshots": [_snapshot_json(s) for s in snaps],
    }


@router.get("/snapshots/{snapshot_id}")
def api_snapshot(request: Request, snapshot_id: int):
    """스냅샷 메타데이터 + 파일 다운로드 경로."""
    _require_view(request)
    with db.connect() as conn:
        snap = db.get_snapshot(conn, snapshot_id)
    if snap is None:
        raise HTTPException(404, "스냅샷 없음")
    from . import app as webapp  # 순환 임포트 방지 — app 이 이 모듈을 임포트한다

    # 로그인 캡처 스냅샷은 소유자/관리자만 (존재 은폐 404)
    if snap["authenticated"] and not webapp._may_view_authenticated(request, snap):
        raise HTTPException(404, "스냅샷 없음")
    body = _snapshot_json(snap)
    body["page_url"] = snap["page_url"]
    return body


@router.get("/snapshots/{snapshot_id}/file/{name}")
def api_snapshot_file(request: Request, snapshot_id: int, name: str):
    """스냅샷 파일 다운로드 (page.html | screenshot | content.md).

    서빙 로직(화이트리스트·신/구 파일명 해석·CSP sandbox)은 대시보드의
    snapshot_file 과 공유한다.
    """
    _require_view(request)
    from . import app as webapp  # 순환 임포트 방지 — app 이 이 모듈을 임포트한다

    return webapp.snapshot_file(request, snapshot_id, name)


def _resolve_network_tag(norm: str, network_tag: str | None) -> str | None:
    """네트워크 게이트 — 루프백 거부, 사설 대역은 로컬 네트워크 태그 필수.

    공인 주소면 태그를 무시(None 반환). 사설 대역은 요청 본문의 태그(GUID,
    db.get_network_tag 로 검증)를 쓰되, 없으면 이미 등록된 페이지의 태그로
    폴백한다. 어느 쪽도 없으면 400. 코어(pipeline·crawler)가 한 번 더 강제한다.
    """
    kind = netcheck.classify_host(urlsplit(norm).hostname or "")
    if kind == netcheck.LOOPBACK:
        raise HTTPException(400, "루프백 주소는 아카이빙할 수 없습니다")
    if kind != netcheck.PRIVATE:
        return None
    with db.connect() as conn:
        if network_tag:
            if db.get_network_tag(conn, network_tag) is None:
                raise HTTPException(400, "등록되지 않은 로컬 네트워크 태그입니다")
            return network_tag
        page = db.get_page(conn, norm)
    if page is not None and page["network_tag_id"]:
        return page["network_tag_id"]
    raise HTTPException(
        400,
        "로컬 네트워크(사설 IP) 주소는 로컬 네트워크 태그가 필요합니다 — "
        "network_tag 필드에 시스템 화면의 태그 ID 를 넣거나, 대시보드에서 "
        "먼저 태그를 지정해 아카이빙하세요",
    )


def _crawl_json(crawl: sqlite3.Row, counts: dict, merged: bool) -> dict:
    return {
        "crawl_id": crawl["id"],
        "url": crawl["start_url"],
        "merged": merged,
        "status": crawl["status"],
        "scope_host": crawl["scope_host"],
        "scope_path": crawl["scope_path"],
        "max_pages": crawl["max_pages"],
        "max_depth": crawl["max_depth"],
        "delay_seconds": crawl["delay_seconds"],
        "created_at": crawl["created_at"],
        "counts": counts,
    }


class ArchiveRequest(BaseModel):
    """POST /api/v1/archive 요청 본문."""

    url: str
    force: bool = False
    network_tag: str | None = None  # 사설 대역 GUID (공인 주소면 무시)


@router.post("/archive", status_code=202)
def api_archive(request: Request, payload: ArchiveRequest):
    """아카이빙 트리거 — 검증은 동기, 캡처는 worker 가 큐를 소비해 실행 (웹 UI 와 동일 경로).

    같은 URL 이 이미 큐에 있으면 중복 등록하지 않고 queued=false 로 응답한다.
    """
    _require_archive(request)
    try:
        norm = storage.normalize_url(payload.url)
    except ValueError as e:
        raise HTTPException(400, f"잘못된 URL: {e}")
    tag_id = _resolve_network_tag(norm, payload.network_tag)
    from . import app as webapp  # 순환 임포트 방지 — app 이 이 모듈을 임포트한다

    # 사용자 귀속 확장 토큰이면 그 소유자를 요청자로 기록 → '내 아카이브'에 귀속.
    # 시스템 키(owner=NULL)·인증 off 면 None (주체 없음).
    key = request.state.api_key
    owner_id = key["owner_user_id"] if key is not None else None
    queued = webapp._queue_archive(
        norm, force=payload.force, source="api", requested_by=owner_id,
        network_tag_id=tag_id,
    )
    if queued:
        audit.log(request, "새 아카이빙 등록(API): %s", norm)
    return {"queued": queued, "url": norm}


class CrawlRequest(BaseModel):
    """POST /api/v1/crawl 요청 본문 — 사이트 전체 아카이브 트리거."""

    url: str
    max_pages: int | None = None
    max_depth: int | None = None
    delay: int | None = None
    network_tag: str | None = None  # 사설 대역 GUID (공인 주소면 무시)
    storage_state: dict | None = None  # 확장의 로그인 세션 — 있으면 인증 크롤(쿠키만)


@router.post("/crawl", status_code=202)
def api_crawl(request: Request, payload: CrawlRequest):
    """사이트 전체 아카이브(크롤) 트리거 — 큐 등록만 동기, 실행은 워커가 소비.

    같은 시작 URL 의 크롤이 진행 중이면 그 크롤로 병합(merged=true)한다.
    storage_state 가 실리면 확장의 로그인 세션으로 1회성 session 자격증명을
    만들어 crawls.credential_id 에 실어 크롤 전 페이지를 인증 상태로 캡처한다
    (auth-profiles 와 같은 공통 가드 — https·사용자 토큰·대상 호스트 스코프).
    """
    _require_archive(request)
    try:
        norm = storage.normalize_url(payload.url)
    except ValueError as e:
        raise HTTPException(400, f"잘못된 URL: {e}")
    tag_id = _resolve_network_tag(norm, payload.network_tag)
    cred_id = None
    if payload.storage_state is not None:
        cred_id, _owner = _ephemeral_session_credential(request, norm, payload.storage_state)
    try:
        crawl, merged = crawler.start_crawl(
            payload.url,
            max_pages=payload.max_pages, max_depth=payload.max_depth,
            delay_seconds=payload.delay, source="api", network_tag_id=tag_id,
            credential_id=cred_id,
        )
    except ValueError as e:
        if cred_id is not None:  # 쓰지 못한 1회성 자격증명 폐기
            with db.connect() as conn:
                db.delete_ephemeral_credential(conn, cred_id)
        raise HTTPException(400, str(e))
    if cred_id is not None and merged:
        # 진행 중 크롤로 병합 — 이번 옵션·자격증명은 버려지므로 즉시 폐기
        with db.connect() as conn:
            db.delete_ephemeral_credential(conn, cred_id)
        cred_id = None
    with db.connect() as conn:
        counts = db.crawl_page_counts(conn, crawl["id"])
    verb = "병합" if merged else "등록"
    suffix = " (인증)" if cred_id is not None else ""
    audit.log(
        request, "사이트 아카이브 %s(API)%s: %s → 크롤 #%d", verb, suffix, norm, crawl["id"]
    )
    return _crawl_json(crawl, counts, merged)


class AuthProfileRequest(BaseModel):
    """POST /api/v1/auth-profiles — 확장이 보낸 로그인 세션으로 1회성 인증 캡처."""

    url: str
    storage_state: dict          # Playwright storage_state — 쿠키만 (ID/PW 필드 없음)
    force: bool = False
    network_tag: str | None = None


def _scope_cookies(storage_state: dict, scope_host: str) -> dict:
    """대상 호스트(또는 상위 도메인) 스코프 쿠키만 남긴다 — 외부 도메인 쿠키 차단."""
    cookies = storage_state.get("cookies")
    if not isinstance(cookies, list):
        raise HTTPException(400, "storage_state.cookies 형식이 올바르지 않습니다")
    kept = []
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        host = str(cookie.get("domain", "")).lstrip(".").lower()
        if not host and cookie.get("url"):
            host = urlsplit(str(cookie["url"])).hostname or ""
        if host and "." in host and (
            scope_host == host or scope_host.endswith("." + host)
        ):
            kept.append(cookie)
    if not kept:
        raise HTTPException(400, "대상 호스트 도메인의 쿠키가 없습니다")
    return {"cookies": kept, "origins": []}


def _ephemeral_session_credential(
    request: Request, norm: str, storage_state: dict
) -> tuple[int, int | None]:
    """확장의 로그인 세션 쿠키로 1회성 session 자격증명을 만들어 (cred_id, owner_id) 반환.

    인증 캡처의 공통 가드 — WCCG_SECRET_KEY 필수(503), 사용자 확장 토큰만(403),
    https 대상만(400), 대상 호스트 스코프 쿠키만. auth-profiles(단일 페이지)와
    crawl(사이트 전체)이 공유한다. 자격증명은 1회성(expires_at 설정)이라 소비자가
    캡처 후 폐기하고, 누락분은 만료 GC(설정 가능, 기본 24h)가 정리한다.
    """
    if not crypto.is_configured():
        raise HTTPException(
            503, "세션 자격증명을 저장할 수 없습니다 — WCCG_SECRET_KEY 가 설정되지 않았습니다"
        )
    key = request.state.api_key
    owner_id = key["owner_user_id"] if key is not None else None
    if owner_id is None:
        # 사용자 귀속 확장 토큰만 — 시스템 키·인증 off 로는 소유자를 정할 수 없다
        raise HTTPException(403, "인증 캡처는 사용자 확장 토큰으로만 가능합니다")
    if not norm.startswith("https://"):
        raise HTTPException(400, "인증 캡처는 https 대상만 허용합니다")
    scope_host = urlsplit(norm).hostname or ""
    scoped = _scope_cookies(storage_state, scope_host)
    try:
        cred_payload = credentials.build_payload(
            credentials.KIND_SESSION,
            {"storage_state": json.dumps(scoped, ensure_ascii=False)},
        )
    except credentials.CredentialError as e:
        raise HTTPException(400, str(e))
    label = f"ext:{owner_id}:{secrets.token_hex(4)}"
    with db.connect() as conn:
        site_id = db.get_or_create_site(conn, storage.site_key(norm))
        ttl_seconds = db.ext_credential_ttl_hours(conn) * 3600
        cred_id = credentials.add(
            conn, site_id, label, credentials.KIND_SESSION, cred_payload,
            created_by=owner_id, ttl_seconds=ttl_seconds,
        )
        db.delete_expired_ext_credentials(conn)  # 기회적 만료 정리(핫패스)
    return cred_id, owner_id


@router.post("/auth-profiles", status_code=202)
def api_auth_profile(request: Request, payload: AuthProfileRequest):
    """확장의 '로그인 페이지' 1회성 인증 캡처.

    확장이 보낸 세션 쿠키로 site_credentials(kind=session) 1회성 자격증명을 만들고
    그 credential_id 로 아카이빙을 큐에 넣는다. 자격증명은 캡처 직후 폐기되고,
    누락분은 만료 GC(설정 가능, 기본 24h)가 정리한다. 예약·크롤 재사용에는
    연결하지 않는다(1회성).
    """
    _require_archive(request)
    try:
        norm = storage.normalize_url(payload.url)
    except ValueError as e:
        raise HTTPException(400, f"잘못된 URL: {e}")
    tag_id = _resolve_network_tag(norm, payload.network_tag)
    cred_id, owner_id = _ephemeral_session_credential(
        request, norm, payload.storage_state
    )
    from . import app as webapp  # 순환 임포트 방지 — app 이 이 모듈을 임포트한다

    queued = webapp._queue_archive(
        norm, force=payload.force, source="api", requested_by=owner_id,
        network_tag_id=tag_id, credential_id=cred_id,
    )
    if not queued:
        # 같은 URL 이 이미 큐에 있음 — 쓰지 않은 1회성 자격증명을 즉시 폐기
        with db.connect() as conn:
            db.delete_ephemeral_credential(conn, cred_id)
    else:
        audit.log(request, "확장 인증 캡처 등록(API): %s → 자격증명 #%d", norm, cred_id)
    return {"queued": queued, "url": norm, "authenticated": True}
