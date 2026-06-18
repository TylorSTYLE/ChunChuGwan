"""사용자 액션 감사 로그 — 누가 무엇을 했는지 audit_logs 테이블에 남긴다.

전용 audit_logs 테이블에 구조화(요청 주체·액션 종류·대상·원문)해 적재하고,
감사 로그 화면(/log/audit, '감사 로그 보기' 권한)이 보여준다. 시스템 로그와
분리돼 있다 (system_log 핸들러는 이 로거를 제외). 기록은 보는 사람의 로케일과
무관해야 하므로 메시지는 한국어 원문으로 남긴다 (CLI·기존 시스템 로그와 동일).

logger.info 로도 한 번 흘려 콘솔·테스트(caplog) 가시성을 유지하되, 적재는
audit_logs 직접 INSERT 로 한다 — 감사 적재 실패가 본 작업을 깨면 안 되므로
조용히 무시한다.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import Request

from .. import db

logger = logging.getLogger(__name__)


def actor(request: Request) -> tuple[str, int | None]:
    """요청 주체 — (표시 이름, 세션 사용자 id). 세션 사용자 → API 키 → 익명."""
    user = getattr(request.state, "user", None)
    if user is not None:
        return user["email"], user["id"]
    api_key = getattr(request.state, "api_key", None)
    if api_key is not None:
        return f"API 키 '{api_key['name']}'", None
    return "익명(인증 꺼짐)", None


def log(
    request: Request,
    message: str,
    *args: object,
    action: str = "admin",
    target: str | None = None,
) -> None:
    """사용자 액션을 감사 로그에 기록 — 메시지 끝에 요청 주체를 붙인다.

    action 은 화면 필터의 단위(db.AUDIT_ACTIONS) — 기본은 관리 작업(admin).
    아카이브 열람·문서 다운로드 등은 호출처에서 action·target 을 지정한다.
    """
    if args:
        message = message % args
    who, uid = actor(request)
    full = f"{message} (요청자: {who})"
    # 콘솔·테스트 가시성용 (system_log 핸들러는 이 로거를 제외해 시스템 로그엔 안 남음).
    logger.info(full)
    try:
        with db.connect() as conn:
            db.insert_audit_log(
                conn,
                created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                actor=who,
                actor_user_id=uid,
                action=action if action in db.AUDIT_ACTIONS else "admin",
                target=target,
                message=full,
            )
    except Exception:
        pass  # 감사 적재 실패(DB 락 등)는 본 작업을 깨지 않는다
