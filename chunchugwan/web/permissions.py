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


def can_delete(user: sqlite3.Row | None) -> bool:
    """아카이브 데이터(페이지/스냅샷) 삭제 가능 여부 — 아카이빙 권한(admin/archiver)과 동일."""
    return can_archive(user)


def can_view_logs(user: sqlite3.Row | None) -> bool:
    """아카이빙 로그(/logs) 열람 가능 여부 — viewer 이상 (pending/blocked 제외)."""
    return not config.AUTH_ENABLED or bool(
        user and user["role"] in ("admin", "archiver", "viewer")
    )


def can_search(user: sqlite3.Row | None) -> bool:
    """아카이브 전문 검색(/search) 가능 여부 — viewer 이상.

    전문검색은 모든 아카이브 본문을 훑는 강한 열람 권한이라 로그 열람과
    같은 하한(viewer)을 둔다. pending/blocked 는 미들웨어가 이미 차단한다.
    """
    return can_view_logs(user)


# 확장 토큰(사용자 귀속 API 키)이 동작할 수 있는 역할 — 그 외(pending/blocked/
# withdrawn)는 토큰 자체가 무효 취급된다 (_api_auth 가 매 요청 재평가).
TOKEN_ROLES = ("admin", "archiver", "viewer")


def token_permissions_for_role(role: str) -> tuple[bool, bool]:
    """사용자 역할에서 확장 토큰 권한(can_view, can_archive)을 파생한다.

    보기=viewer 이상, 아카이브=archiver 이상. pending/blocked/withdrawn 은
    둘 다 False — 발급 거부 및 토큰 무효화의 근거가 된다. AUTH_ENABLED 여부는
    여기서 보지 않는다 (게이트 계층의 몫).
    """
    can_view = role in ("admin", "archiver", "viewer")
    can_archive = role in ("admin", "archiver")
    return can_view, can_archive
