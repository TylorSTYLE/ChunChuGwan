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
import sqlite3
from urllib.parse import urlsplit

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel

from .. import auth, config, crawler, credentials, db, netcheck, storage
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
            can_view, can_archive = permissions.token_permissions_for_role(
                owner["role"]
            )
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
    with db.connect() as conn:
        pages = db.list_pages(conn)
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

    # 인증 스냅샷은 소유자/관리자에게만 노출 — 메타데이터도 가린다
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

    # 인증 스냅샷은 소유자/관리자만 (존재 은폐 404)
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
def api_archive(request: Request, payload: ArchiveRequest, background: BackgroundTasks):
    """아카이빙 트리거 — 검증은 동기, 캡처는 백그라운드 (웹 UI 와 동일 경로).

    같은 URL 이 이미 진행 중이면 중복 실행하지 않고 queued=false 로 응답한다.
    """
    _require_archive(request)
    try:
        norm = storage.normalize_url(payload.url)
    except ValueError as e:
        raise HTTPException(400, f"잘못된 URL: {e}")
    tag_id = _resolve_network_tag(norm, payload.network_tag)
    from . import app as webapp  # 순환 임포트 방지 — app 이 이 모듈을 임포트한다

    queued = webapp._queue_archive(
        background, norm, force=payload.force, source="api", network_tag_id=tag_id
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


@router.post("/crawl", status_code=202)
def api_crawl(request: Request, payload: CrawlRequest):
    """사이트 전체 아카이브(크롤) 트리거 — 큐 등록만 동기, 실행은 워커가 소비.

    같은 시작 URL 의 크롤이 진행 중이면 그 크롤로 병합(merged=true)한다.
    """
    _require_archive(request)
    try:
        norm = storage.normalize_url(payload.url)
    except ValueError as e:
        raise HTTPException(400, f"잘못된 URL: {e}")
    tag_id = _resolve_network_tag(norm, payload.network_tag)
    try:
        crawl, merged = crawler.start_crawl(
            payload.url,
            max_pages=payload.max_pages, max_depth=payload.max_depth,
            delay_seconds=payload.delay, source="api", network_tag_id=tag_id,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    with db.connect() as conn:
        counts = db.crawl_page_counts(conn, crawl["id"])
    verb = "병합" if merged else "등록"
    audit.log(request, "사이트 아카이브 %s(API): %s → 크롤 #%d", verb, norm, crawl["id"])
    return _crawl_json(crawl, counts, merged)


class AuthProfileRequest(BaseModel):
    """POST /api/v1/auth-profiles 요청 본문 — 1회성 인증 캡처용 세션 캡슐."""

    url: str
    storage_state: dict          # Playwright storage_state — 쿠키만(ID/PW 필드 없음)
    force: bool = False
    network_tag: str | None = None


_STORAGE_STATE_MAX_BYTES = 64 * 1024   # 거대 캡슐(DoS) 차단
_MAX_COOKIES = 50
_COOKIE_KEYS = (
    "name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite",
)


def _cookie_host_allowed(host: str, scope_host: str) -> bool:
    """쿠키 스코프 호스트가 대상 호스트이거나 그 상위 도메인인지 — 빈/TLD 거부."""
    host = host.lstrip(".").lower()
    if not host or "." not in host:  # 빈 스코프·점 없는 TLD(예: 'com') 거부
        return False
    return scope_host == host or scope_host.endswith("." + host)


def _validate_storage_state(storage_state: dict, scope_host: str) -> dict:
    """캡슐을 검증·정제해 cookies 만 남긴 storage_state 를 반환한다.

    - 대상 호스트(또는 상위 도메인) 스코프 쿠키만 허용. domain 또는 url 필드로
      스코프를 정하고, 빈/누락 스코프·TLD 쿠키는 거부 (임의 호스트 쿠키 주입 차단).
    - localStorage(origins)는 인증 캡처에 불필요하므로 드롭한다.
    - 크기·개수 상한으로 거대 캡슐을 차단한다.
    """
    if not isinstance(storage_state, dict):
        raise HTTPException(400, "storage_state 형식이 올바르지 않습니다")
    cookies = storage_state.get("cookies")
    if not isinstance(cookies, list) or not cookies:
        raise HTTPException(400, "storage_state.cookies 가 비어 있거나 형식이 잘못되었습니다")
    if len(cookies) > _MAX_COOKIES:
        raise HTTPException(413, f"쿠키가 너무 많습니다 (최대 {_MAX_COOKIES}개)")
    clean: list[dict] = []
    for cookie in cookies:
        if not (isinstance(cookie, dict) and cookie.get("name") and "value" in cookie):
            raise HTTPException(400, "쿠키 형식이 올바르지 않습니다")
        if cookie.get("domain"):
            host = str(cookie["domain"])
        elif cookie.get("url"):
            host = urlsplit(str(cookie["url"])).hostname or ""
        else:
            host = ""
        if not _cookie_host_allowed(host, scope_host):
            raise HTTPException(
                400,
                f"대상 호스트 밖이거나 스코프가 없는 쿠키입니다: {cookie.get('name')}",
            )
        # 화이트리스트 키만 남기고 도메인을 명시 (url 등 부가 필드 제거)
        c = {k: cookie[k] for k in _COOKIE_KEYS if k in cookie}
        c["domain"] = host.lstrip(".").lower()
        clean.append(c)
    cleaned = {"cookies": clean}
    raw = json.dumps(cleaned, separators=(",", ":")).encode("utf-8")
    if len(raw) > _STORAGE_STATE_MAX_BYTES:
        raise HTTPException(413, "자격증명 캡슐이 너무 큽니다")
    return cleaned


@router.post("/auth-profiles", status_code=202)
def api_auth_profile(
    request: Request, payload: AuthProfileRequest, background: BackgroundTasks
):
    """1회성 인증 캡처 — 로그인 세션 캡슐로 즉시 캡처하고 캡슐을 폐기한다.

    확장이 보낸 쿠키(storage_state)를 마스터 키로 암호화해 잠깐 보관했다가
    캡처에 쓰고, 성공·실패와 무관하게 캡처 직후 삭제한다(만료 GC 는 안전망).
    지속·예약 재아카이빙에는 쓰이지 않는다.
    """
    _require_archive(request)
    if not credentials.is_enabled():
        raise HTTPException(
            503, "인증 캡처가 비활성 상태입니다 — WCCG_CREDENTIAL_KEY 가 설정되지 않았습니다"
        )
    key = request.state.api_key
    owner_id = key["owner_user_id"] if key is not None else None
    if owner_id is None:
        # 사용자 귀속 확장 토큰만 — 시스템 키·인증 off 로는 소유자를 정할 수 없다
        raise HTTPException(403, "인증 캡처는 사용자 확장 토큰으로만 가능합니다")
    try:
        norm = storage.normalize_url(payload.url)
    except ValueError as e:
        raise HTTPException(400, f"잘못된 URL: {e}")
    # 인증 캡처는 https 대상만 — 주입 세션 쿠키의 평문 전송 차단(코어도 재강제)
    if not norm.startswith("https://"):
        raise HTTPException(400, "인증 캡처는 https 대상만 허용합니다")
    scope_host = urlsplit(norm).hostname or ""
    cleaned = _validate_storage_state(payload.storage_state, scope_host)
    tag_id = _resolve_network_tag(norm, payload.network_tag)
    with db.connect() as conn:
        ttl_hours = db.credential_ttl_hours(conn)
        ciphertext = credentials.encrypt(
            json.dumps(cleaned).encode("utf-8")
        )
        capsule_id = db.create_auth_capsule(
            conn, url=norm, scope_host=scope_host, owner_user_id=owner_id,
            ciphertext=ciphertext, network_tag_id=tag_id,
            ttl_seconds=ttl_hours * 3600,
        )
        db.delete_expired_auth_capsules(conn)  # 기회적 만료 정리(핫패스)
    from . import app as webapp  # 순환 임포트 방지 — app 이 이 모듈을 임포트한다

    queued = webapp._queue_archive(
        background, norm, force=payload.force, source="api",
        network_tag_id=tag_id, auth_capsule_id=capsule_id,
    )
    if not queued:
        # 같은 URL 이 이미 진행 중 — 쓰지 않은 캡슐을 즉시 폐기(평문 캡슐을 남기지 않음)
        with db.connect() as conn:
            db.delete_auth_capsule(conn, capsule_id)
    else:
        audit.log(request, "인증 캡처 등록(API): %s", norm)
    return {"queued": queued, "url": norm, "authenticated": True}
