"""춘추관 간 네트워크 이전 — 소스(보내는 쪽) Pull 엔드포인트.

받는 쪽(목적지)이 발급 토큰으로 접근해 전체 데이터를 파일 단위로 내려받는다.
인증은 API 키가 아니라 **이전 토큰**(이전 모드일 때만 유효, SHA-256 비교)이며,
미들웨어가 `/api/` 를 세션 인증 대상에서 제외하므로 토큰만으로 접근된다.

설계·받는 쪽 워커는 `chunchugwan/migration.py` 참조.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse

from .. import db, migration

router = APIRouter(prefix="/api/migration")


def _require_token(request: Request) -> None:
    """이전 토큰 게이트 — 이전 모드 + 토큰 해시 일치가 아니면 401."""
    token = request.headers.get("x-migration-token", "").strip()
    with db.connect() as conn:
        if not migration.token_matches(conn, token):
            raise HTTPException(
                401, "유효한 이전 토큰이 아니거나 이전 모드가 아닙니다",
                headers={"WWW-Authenticate": "Migration"},
            )


@router.get("/info")
def migration_info(request: Request) -> JSONResponse:
    """버전·요약 정보 (받는 쪽이 다운로드 전 호환성 확인)."""
    _require_token(request)
    from .. import backup
    with db.connect() as conn:
        counts = backup._archive_counts(conn)
    return JSONResponse({
        "format_version": backup.FORMAT_VERSION,
        "created_at": backup._utcnow(),
        "counts": counts,
    })


@router.get("/manifest")
def migration_manifest(request: Request) -> JSONResponse:
    """전송 대상 매니페스트 (DB sha256·파일 목록)."""
    _require_token(request)
    return JSONResponse(migration.build_manifest())


@router.get("/db")
def migration_db(request: Request) -> FileResponse:
    """일관 DB 스냅샷 파일 스트리밍."""
    _require_token(request)
    path = migration.db_snapshot_path()
    if not path.is_file():
        # manifest 를 아직 안 받았으면 스냅샷이 없을 수 있다 — 즉석으로 만든다
        migration.ensure_db_snapshot()
    return FileResponse(path, media_type="application/octet-stream",
                        filename="index.db")


@router.get("/file")
def migration_file(request: Request, path: str = Query(...)) -> FileResponse:
    """단일 아카이브 파일 스트리밍 (경로 검증 필수 — traversal 방지)."""
    _require_token(request)
    try:
        target = migration.resolve_transfer_file(path)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return FileResponse(target, media_type="application/octet-stream")
