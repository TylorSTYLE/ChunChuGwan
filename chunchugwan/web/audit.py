"""사용자 액션 감사 로그 — 누가 무엇을 했는지 system_logs 에 남긴다.

logger.info 로 남기면 system_log 의 DB 핸들러(chunchugwan 네임스페이스
INFO 이상)가 적재해 시스템 로그 화면(/system/logs)에서 보인다. 기록은
보는 사람의 로케일과 무관해야 하므로 메시지는 한국어 원문으로 남긴다
(CLI·기존 시스템 로그와 동일).
"""

from __future__ import annotations

import logging

from fastapi import Request

logger = logging.getLogger(__name__)


def actor(request: Request) -> str:
    """요청 주체 표시 — 세션 사용자 이메일 → API 키 이름 → 익명(인증 꺼짐)."""
    user = getattr(request.state, "user", None)
    if user is not None:
        return user["email"]
    api_key = getattr(request.state, "api_key", None)
    if api_key is not None:
        return f"API 키 '{api_key['name']}'"
    return "익명(인증 꺼짐)"


def log(request: Request, message: str, *args: object) -> None:
    """사용자 액션을 시스템 로그에 기록 — 메시지 끝에 요청 주체를 붙인다."""
    if args:
        message = message % args
    logger.info("%s (요청자: %s)", message, actor(request))
