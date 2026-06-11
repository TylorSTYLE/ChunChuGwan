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

import sqlite3

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel

from .. import auth, config, db, storage

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
    """
    request.state.api_key = None
    if not config.AUTH_ENABLED:
        return
    token = _extract_token(request)
    with db.connect() as conn:
        key = auth.resolve_api_key(conn, token) if token else None
        if key is not None:
            db.touch_api_key(conn, key["id"])
    if key is None:
        raise HTTPException(
            401, "유효한 API 키가 필요합니다",
            headers={"WWW-Authenticate": "Bearer"},
        )
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

    return webapp.snapshot_file(snapshot_id, name)


class ArchiveRequest(BaseModel):
    """POST /api/v1/archive 요청 본문."""

    url: str
    force: bool = False


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
    from . import app as webapp  # 순환 임포트 방지 — app 이 이 모듈을 임포트한다

    queued = webapp._queue_archive(background, norm, force=payload.force, source="api")
    return {"queued": queued, "url": norm}
