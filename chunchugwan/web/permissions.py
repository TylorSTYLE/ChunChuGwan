"""권한 판정 헬퍼 — 라우트 가드와 템플릿 메뉴/버튼 노출 제어 공용.

역할(role)은 세분 권한(db.PERMISSIONS)의 묶음(프리셋)이고, 사용자별
permission_overrides 로 개별 가감한다. 모든 가드는 실효 권한(has_permission)
으로 판정하므로 오버라이드가 한 곳에서 전 경로에 반영된다.

인증이 꺼진 환경(loopback)은 단일 사용자 로컬 도구로 보고 전부 허용한다.
"""

from __future__ import annotations

import sqlite3

from fastapi import Request

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


def can_view_archive_logs(user: sqlite3.Row | None) -> bool:
    """아카이브 로그(/log/archive) 열람 가능 여부 — 기본 admin 만."""
    return has_permission(user, "view_archive_logs")


def can_view_system_logs(user: sqlite3.Row | None) -> bool:
    """시스템 로그(/log/system) 열람 가능 여부 — 기본 admin 만."""
    return has_permission(user, "view_system_logs")


def can_view_audit_logs(user: sqlite3.Row | None) -> bool:
    """감사 로그(/log/audit) 열람 가능 여부 — 기본 admin 만."""
    return has_permission(user, "view_audit_logs")


def can_search(user: sqlite3.Row | None) -> bool:
    """아카이브 전문 검색(/search) 가능 여부 — 아카이브 열람과 같은 하한(view)."""
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


def can_manage_trash(user: sqlite3.Row | None) -> bool:
    """휴지통(삭제 보류 아카이브) 열람·복원·영구삭제 가능 여부 — 기본 admin 만.

    삭제(=휴지통으로 보내기)는 종전 delete 권한이 게이트하고, 휴지통을 보고
    되돌리거나 영구삭제하는 것은 이 권한으로 분리한다.
    """
    return has_permission(user, "manage_trash")


def can_use_api_keys(user: sqlite3.Row | None) -> bool:
    """개인 API Key(확장 토큰) 발급·사용 가능 여부 — 크롬 확장 캡처도 이 권한.

    발급(`/settings/api-keys`)과 사용(소유자 귀속 토큰의 `/api/v1` 인증) 양쪽을
    같은 권한으로 게이트한다. 권한을 잃으면 _api_auth 가 기존 토큰도 거부한다.
    """
    return has_permission(user, "use_api_keys")


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


def menu_flags(user: sqlite3.Row | None) -> dict[str, bool]:
    """헤더 메뉴/버튼 노출 플래그 일괄 — 실효 권한을 1회만 계산한다.

    _auth_context 가 HTML 렌더마다 can_* 를 9회 호출하면 effective_permissions
    (오버라이드 JSON 파싱)도 9회 돈다. 한 번 계산한 집합에서 모두 파생한다.
    인증 off(loopback)면 전부 허용 — has_permission 과 같은 의미.
    """
    auth_off = not config.AUTH_ENABLED
    perms = effective_permissions(user)

    def has(permission: str) -> bool:
        return auth_off or permission in perms

    can_manage_system = has("manage_system")
    can_manage_users = has("manage_users")
    can_manage_credentials = has("manage_credentials")
    return {
        "system_allowed": (
            can_manage_system or can_manage_users or can_manage_credentials
        ),
        "can_manage_system": can_manage_system,
        "can_manage_users": can_manage_users,
        "can_manage_credentials": can_manage_credentials,
        "can_archive": has("archive"),
        "can_delete": has("delete"),
        "can_view_archive_logs": has("view_archive_logs"),
        "can_view_system_logs": has("view_system_logs"),
        "can_view_audit_logs": has("view_audit_logs"),
        "can_view_any_logs": (
            has("view_archive_logs") or has("view_system_logs")
            or has("view_audit_logs")
        ),
        "can_search": has("view"),
        "can_use_api_keys": has("use_api_keys"),
        "can_manage_trash": has("manage_trash"),
    }


def token_permissions_for_role(role: str) -> tuple[bool, bool]:
    """역할 프리셋에서 확장 토큰 권한(can_view, can_archive)을 파생 (오버라이드 미반영).

    보기=view, 아카이브=archive 권한 보유 여부. AUTH_ENABLED 여부는 여기서
    보지 않는다 (게이트 계층의 몫). 프리셋은 모듈 캐시에서 읽는다.
    """
    preset = db._presets_cache.get(role, frozenset())
    return "view" in preset, "archive" in preset


def token_permissions_for_user(user: sqlite3.Row | None) -> tuple[bool, bool]:
    """사용자 실효 권한에서 확장 토큰 권한(can_view, can_archive)을 파생 (오버라이드 반영)."""
    perms = effective_permissions(user)
    return "view" in perms, "archive" in perms


def auth_context(request: Request) -> dict:
    """로그인 사용자 + 메뉴/버튼 노출 플래그 + 사람 확인 대기 작업.

    SPA 세션 컨텍스트(/api/web/me)가 쓰는 권한·needs-human 묶음. 실효 권한을
    1회 계산해 메뉴 플래그를 모두 파생하고, manage_system 권한자에겐 워커가 DB 에
    기록한 needs_human(사람 확인 대기) 작업을 함께 내려보낸다 (serve 의
    LIVE_CHALLENGE 설정과 무관 — DB 사실 기준)."""
    user = getattr(request.state, "user", None)
    flags = menu_flags(user)
    needs_human_jobs: list = []
    if flags["can_manage_system"]:
        try:
            with db.connect() as conn:
                needs_human_jobs = [
                    {"id": j["id"], "url": j["url"]}
                    for j in db.list_needs_human_jobs(conn)
                ]
        except Exception:
            needs_human_jobs = []
    return {
        "user": user,
        **flags,
        "needs_human_jobs": needs_human_jobs,
        "needs_human_count": len(needs_human_jobs),
    }
