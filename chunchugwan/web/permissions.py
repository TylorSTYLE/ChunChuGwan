"""권한 판정 헬퍼 — 라우트 가드와 템플릿 메뉴/버튼 노출 제어 공용.

역할(role)은 세분 권한(db.PERMISSIONS)의 묶음(프리셋)이고, 사용자별
permission_overrides 로 개별 가감한다. 모든 가드는 실효 권한(has_permission)
으로 판정하므로 오버라이드가 한 곳에서 전 경로에 반영된다.

인증이 꺼진 환경(loopback)은 단일 사용자 로컬 도구로 보고 전부 허용한다.
"""

from __future__ import annotations

import sqlite3

from .. import config, db


def effective_permissions(user: sqlite3.Row | None) -> frozenset[str]:
    """사용자의 실효 권한 집합 (역할 프리셋 ± 오버라이드). 미로그인은 빈 집합."""
    if user is None:
        return frozenset()
    try:
        overrides = user["permission_overrides"]
    except (IndexError, KeyError):
        overrides = None
    return db.effective_permissions(user["role"], overrides)


def has_permission(user: sqlite3.Row | None, permission: str) -> bool:
    """사용자가 해당 권한을 실효로 가졌는지 — 인증 off(loopback)면 전부 허용."""
    if not config.AUTH_ENABLED:
        return True
    return permission in effective_permissions(user)


def is_admin(user: sqlite3.Row | None) -> bool:
    """관리자 역할 여부 (역할 정체성 — 능력 판정에는 has_permission 을 쓴다)."""
    return bool(user and user["role"] == "admin")


def can_archive(user: sqlite3.Row | None) -> bool:
    """아카이빙 트리거(신규/재아카이빙) 가능 여부."""
    return has_permission(user, "archive")


def can_delete(user: sqlite3.Row | None) -> bool:
    """아카이브 데이터(페이지/스냅샷/사이트) 삭제 가능 여부."""
    return has_permission(user, "delete")


def can_view_logs(user: sqlite3.Row | None) -> bool:
    """아카이빙 로그(/logs)·열람 가능 여부 — viewer 이상."""
    return has_permission(user, "view")


def can_search(user: sqlite3.Row | None) -> bool:
    """아카이브 전문 검색(/search) 가능 여부 — 로그 열람과 같은 하한(view)."""
    return has_permission(user, "view")


def can_manage_credentials(user: sqlite3.Row | None) -> bool:
    """사이트 로그인 자격증명 관리·자격증명 연결 아카이빙 가능 여부."""
    return has_permission(user, "manage_credentials")


def can_manage_system(user: sqlite3.Row | None) -> bool:
    """시스템 설정·백업·복원·네트워크 태그·시스템 로그 관리 가능 여부."""
    return has_permission(user, "manage_system")


def can_manage_users(user: sqlite3.Row | None) -> bool:
    """사용자·초대·시스템 API 키 관리 가능 여부."""
    return has_permission(user, "manage_users")


def can_view_authenticated_all(user: sqlite3.Row | None) -> bool:
    """다른 사용자가 로그인 캡처한 인증 스냅샷까지 열람 가능 여부."""
    return has_permission(user, "view_authenticated_all")


def system_allowed(user: sqlite3.Row | None) -> bool:
    """관리자 영역(시스템·사용자·자격증명) 중 하나라도 접근 가능한지.

    헤더 '관리자' 메뉴 노출 기준 — 세부 화면은 각자 더 좁은 권한을 요구한다.
    인증 off(loopback)면 전부 허용.
    """
    if not config.AUTH_ENABLED:
        return True
    return (
        can_manage_system(user)
        or can_manage_users(user)
        or can_manage_credentials(user)
    )


# 확장 토큰(사용자 귀속 API 키)이 동작할 수 있는 역할 — 그 외(pending/blocked/
# withdrawn)는 토큰 자체가 무효 취급된다 (_api_auth 가 매 요청 재평가).
TOKEN_ROLES = ("admin", "archiver", "viewer")


def token_permissions_for_role(role: str) -> tuple[bool, bool]:
    """역할 프리셋에서 확장 토큰 권한(can_view, can_archive)을 파생 (오버라이드 미반영).

    보기=view, 아카이브=archive 권한 보유 여부. AUTH_ENABLED 여부는 여기서
    보지 않는다 (게이트 계층의 몫).
    """
    preset = db.ROLE_PRESETS.get(role, frozenset())
    return "view" in preset, "archive" in preset


def token_permissions_for_user(user: sqlite3.Row | None) -> tuple[bool, bool]:
    """사용자 실효 권한에서 확장 토큰 권한(can_view, can_archive)을 파생 (오버라이드 반영)."""
    perms = effective_permissions(user)
    return "view" in perms, "archive" in perms
