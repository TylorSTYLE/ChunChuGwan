"""권한 판정 헬퍼 — 라우트 가드와 템플릿 메뉴/버튼 노출 제어 공용.

인증이 꺼진 환경(loopback)은 단일 사용자 로컬 도구로 보고 전부 허용한다.
"""

from __future__ import annotations

import sqlite3

from .. import config


def is_admin(user: sqlite3.Row | None) -> bool:
    """관리자 여부."""
    return bool(user and user["role"] == "admin")


def system_allowed(user: sqlite3.Row | None) -> bool:
    """시스템 메뉴·사용자 관리 접근 가능 여부 — 인증 off(loopback) 이거나 관리자."""
    return not config.AUTH_ENABLED or is_admin(user)


def can_archive(user: sqlite3.Row | None) -> bool:
    """아카이빙 트리거(신규/재아카이빙) 가능 여부."""
    return not config.AUTH_ENABLED or bool(
        user and user["role"] in ("admin", "archiver")
    )
