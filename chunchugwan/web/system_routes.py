"""시스템 메뉴 — 백업/복원, 아카이브 내보내기/가져오기.

쓰기는 코어 모듈(backup.py)만 호출한다 (CLAUDE.md 원칙 1).
백업에는 인증 데이터(패스워드 해시·세션)가 포함되므로, 인증이 켜진 환경에서는
관리자만 접근할 수 있다 (인증 off 의 loopback 환경은 전체 허용).
"""

from __future__ import annotations

import logging
import secrets
import shutil
import smtplib
import tarfile
import tempfile
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from starlette.background import BackgroundTask

from .. import auth, backup as backup_mod
from .. import config, crawler, db, mailer, optimize, resources
from . import permissions
from .i18n import t
from .templating import filesize, templates

logger = logging.getLogger(__name__)


def _require_admin(request: Request) -> None:
    """관리자 게이트. 로그인 자체는 미들웨어가 보장한다."""
    if not permissions.system_allowed(request.state.user):
        raise HTTPException(403, t(request, "관리자만 접근할 수 있습니다"))


router = APIRouter(prefix="/system", dependencies=[Depends(_require_admin)])


def _dir_bytes(root: Path) -> int:
    """디렉토리 전체 용량 (없으면 0)."""
    if not root.is_dir():
        return 0
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file())


@router.get("", response_class=HTMLResponse)
def system_view(request: Request, notice: str = "", error: str = ""):
    with db.connect() as conn:
        counts = {
            t: conn.execute(f"SELECT COUNT(*) AS c FROM {t}").fetchone()["c"]
            for t in ("pages", "snapshots", "checks", "users")
        }
        signup_enabled = db.signup_enabled(conn)
        signup_default_role = db.signup_default_role(conn)
        crawl_defaults = crawler.crawl_defaults(conn)
        crawl_backoff = crawler.retry_backoff(conn)
        network_tags = db.list_network_tags(conn)
    return templates.TemplateResponse(
        request, "system.html",
        {
            "counts": counts,
            "signup_enabled": signup_enabled,
            "signup_default_role": signup_default_role,
            "signup_roles": db.SIGNUP_ROLES,
            "role_labels": db.ROLE_LABELS,
            "crawl_defaults": crawl_defaults,
            "crawl_retry_backoff": ", ".join(str(v) for v in crawl_backoff),
            "crawl_max_attempts": len(crawl_backoff) + 1,
            "network_tags": network_tags,
            "crawl_limits": {
                "max_pages": config.CRAWL_MAX_PAGES_LIMIT,
                "max_depth": config.CRAWL_MAX_DEPTH_LIMIT,
                "min_delay": config.CRAWL_MIN_DELAY_SECONDS,
                "max_delay": config.CRAWL_MAX_DELAY_SECONDS,
            },
            "archive_root": str(config.ARCHIVE_ROOT),
            "db_bytes": config.DB_PATH.stat().st_size if config.DB_PATH.is_file() else 0,
            "sites_bytes": _dir_bytes(config.SITES_DIR),
            "resources_bytes": _dir_bytes(config.RESOURCES_DIR),
            "documents_bytes": _dir_bytes(config.DOCUMENTS_DIR),
            "optimize_pending": sum(optimize.pending_counts()),
            "notice": notice, "error": error,
        },
    )


def _download(make: Callable[[Path], Path], prefix: str) -> FileResponse:
    """코어 함수로 tar.gz 를 만들어 다운로드로 응답 (전송 후 임시 파일 정리)."""
    tmpdir = Path(tempfile.mkdtemp(prefix=f"wccg-{prefix}-"))
    try:
        out = make(tmpdir)
    except Exception:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise
    return FileResponse(
        out, media_type="application/gzip", filename=out.name,
        background=BackgroundTask(shutil.rmtree, tmpdir, ignore_errors=True),
    )


@router.post("/backup")
def system_backup() -> FileResponse:
    """전체 백업 tar.gz 다운로드 (DB·인증 데이터·스냅샷 파일·rules.json)."""
    return _download(backup_mod.create_backup, "backup")


@router.post("/export")
def system_export() -> FileResponse:
    """아카이브 데이터만 내보내기 다운로드 (인증·로그 제외)."""
    return _download(backup_mod.export_archive, "export")


def _save_upload(file: UploadFile) -> Path:
    """업로드 파일을 임시 파일로 저장 후 경로 반환."""
    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
        shutil.copyfileobj(file.file, tmp)
    return Path(tmp.name)


def _system_redirect(*, notice: str = "", error: str = "") -> RedirectResponse:
    query = f"error={quote(error, safe='')}" if error else f"notice={quote(notice, safe='')}"
    return RedirectResponse(f"/system?{query}", status_code=303)


@router.post("/compact")
def system_compact(request: Request):
    """저장공간 최적화 — 압축 변환 + 자원 참조 백필 + 고아 자원 정리.

    CLI ``wccg compact`` 와 동일한 단일 진입점(optimize.run). 내용 보존이고
    멱등이라 여러 번 실행해도 안전하다. 동기로 실행된다 — 스냅샷이 아주
    많으면 응답까지 시간이 걸릴 수 있다. 대상이 없으면 실행 없이 안내만
    한다 (화면의 버튼도 비활성화).
    """
    if sum(optimize.pending_counts()) == 0:
        return _system_redirect(
            notice=t(request, "최적화할 항목이 없습니다 — 스냅샷이 모두 압축·인덱스 형태입니다.")
        )
    try:
        result = optimize.run()
    except OSError as e:
        return _system_redirect(error=t(request, "최적화 실패: {e}", e=e))
    c = result.compact
    return _system_redirect(
        notice=t(
            request,
            "최적화 완료: 변환 {converted}/{total}개 · 공유 자원 {externalized}개 추출 · "
            "문서 {documents}개 이전 · 참조 백필 {indexed}개 · 고아 자원 {swept}개 정리 "
            "({saved} 절약)",
            converted=c.converted, total=c.total,
            externalized=c.externalized, documents=c.documents,
            indexed=result.indexed, swept=result.swept,
            saved=filesize(c.saved_bytes + result.swept_bytes),
        )
    )


@router.post("/settings")
def system_settings(
    request: Request,
    signup_enabled: bool = Form(False),
    signup_default_role: str = Form("pending"),
):
    """가입 설정 저장 — 회원 가입 허용 여부와 가입 계정의 초기 권한."""
    if signup_default_role not in db.SIGNUP_ROLES:
        raise HTTPException(
            400, t(request, "가입 초기 권한으로 쓸 수 없는 역할: {role}",
                   role=repr(signup_default_role))
        )
    with db.connect() as conn:
        db.set_setting(
            conn, db.SIGNUP_ENABLED_KEY, "on" if signup_enabled else "off"
        )
        db.set_setting(conn, db.SIGNUP_DEFAULT_ROLE_KEY, signup_default_role)
    return _system_redirect(notice=t(request, "가입 설정을 저장했습니다."))


@router.post("/crawl-settings")
def system_crawl_settings(
    request: Request,
    crawl_max_pages: int = Form(...),
    crawl_max_depth: int = Form(...),
    crawl_delay: int = Form(...),
    crawl_retry_backoff: str = Form(...),
):
    """사이트 아카이브 설정 저장 — 크롤 기본 옵션과 실패 재시도 대기.

    기본 옵션은 새 크롤 등록(웹 폼·CLI·크롤 스케줄)의 초깃값이고,
    재시도 대기는 진행 중인 크롤에도 즉시 적용된다.
    """
    try:
        crawler.validate_options(crawl_max_pages, crawl_max_depth, crawl_delay)
        backoff = crawler.parse_backoff(crawl_retry_backoff)
    except ValueError as e:
        return _system_redirect(error=t(request, str(e)))
    with db.connect() as conn:
        db.set_setting(conn, db.CRAWL_DEFAULT_MAX_PAGES_KEY, str(crawl_max_pages))
        db.set_setting(conn, db.CRAWL_DEFAULT_MAX_DEPTH_KEY, str(crawl_max_depth))
        db.set_setting(conn, db.CRAWL_DEFAULT_DELAY_KEY, str(crawl_delay))
        db.set_setting(
            conn, db.CRAWL_RETRY_BACKOFF_KEY, ",".join(str(v) for v in backoff)
        )
    return _system_redirect(notice=t(request, "사이트 아카이브 설정을 저장했습니다."))


# ---- 로컬 네트워크 태그 ----
# 사설 IP 대역(로컬 네트워크) 아카이빙을 허용하는 태그. id 는 GUID 자동
# 발급, 표시 이름·설명은 문자열. 태그가 없으면 사설 대역은 아카이빙 불가
# (게이트는 pipeline·crawler — netcheck 참조). 루프백은 태그와 무관하게 금지.

MAX_NETWORK_TAG_NAME_LENGTH = 60
MAX_NETWORK_TAG_DESC_LENGTH = 200


@router.post("/network-tags")
def network_tags_create(
    request: Request, name: str = Form(...), description: str = Form("")
):
    """로컬 네트워크 태그 추가 — id(GUID)는 자동 발급된다."""
    name = name.strip()
    description = description.strip()
    if not name:
        return _system_redirect(error=t(request, "태그 이름을 입력하세요."))
    if len(name) > MAX_NETWORK_TAG_NAME_LENGTH:
        return _system_redirect(
            error=t(request, "태그 이름은 {n}자 이하여야 합니다.",
                    n=MAX_NETWORK_TAG_NAME_LENGTH)
        )
    if len(description) > MAX_NETWORK_TAG_DESC_LENGTH:
        return _system_redirect(
            error=t(request, "태그 설명은 {n}자 이하여야 합니다.",
                    n=MAX_NETWORK_TAG_DESC_LENGTH)
        )
    with db.connect() as conn:
        if db.get_network_tag_by_name(conn, name) is not None:
            return _system_redirect(
                error=t(request, "이미 있는 태그 이름입니다: {name}", name=name)
            )
        db.create_network_tag(conn, name, description)
    return _system_redirect(
        notice=t(request, "로컬 네트워크 태그 '{name}'을(를) 추가했습니다.", name=name)
    )


@router.post("/network-tags/{tag_id}/delete")
def network_tags_delete(request: Request, tag_id: str):
    """로컬 네트워크 태그 삭제 — 페이지·크롤·크롤 스케줄이 참조 중이면 거부."""
    with db.connect() as conn:
        tag = db.get_network_tag(conn, tag_id)
        if tag is None:
            raise HTTPException(404, t(request, "로컬 네트워크 태그 없음"))
        refs = db.count_network_tag_refs(conn, tag_id)
        if refs:
            return _system_redirect(
                error=t(request,
                        "'{name}' 태그는 사용 중이라 삭제할 수 없습니다 (참조 {n}개).",
                        name=tag["name"], n=refs)
            )
        db.delete_network_tag(conn, tag_id)
    return _system_redirect(
        notice=t(request, "로컬 네트워크 태그 '{name}'을(를) 삭제했습니다.",
                 name=tag["name"])
    )


@router.post("/restore")
def system_restore(request: Request, file: UploadFile = File(...)):
    """전체 백업 업로드로 복원 — 현재 데이터(인증 포함)를 백업 시점으로 교체.

    복원되면 세션 테이블도 백업 시점으로 돌아가므로 현재 로그인은 무효가
    될 수 있다 (미들웨어가 /login 으로 보낸다).
    """
    tmp = _save_upload(file)
    try:
        manifest = backup_mod.restore_backup(tmp)
    except (ValueError, tarfile.TarError, OSError) as e:
        return _system_redirect(error=t(request, "복원 실패: {e}", e=e))
    finally:
        tmp.unlink(missing_ok=True)
    c = manifest.get("counts", {})
    return _system_redirect(
        notice=t(
            request, "복원 완료 (백업: {created_at}, 페이지 {pages}개, 스냅샷 {snapshots}개)",
            created_at=manifest.get("created_at", "?"),
            pages=c.get("pages", "?"), snapshots=c.get("snapshots", "?"),
        )
    )


# ---- 사용자 관리 ----


@router.get("/users", response_class=HTMLResponse)
def users_view(request: Request, notice: str = "", error: str = ""):
    """사용자 목록 + 권한 조정 + 초대 (관리자 전용 — 라우터 의존성이 보장)."""
    me = request.state.user
    with db.connect() as conn:
        db.delete_expired_invites(conn)  # 기회적 정리
        users = db.list_users(conn)
        invites = db.list_invites(conn)
    return templates.TemplateResponse(
        request, "users.html",
        {
            "users": users,
            "invites": invites,
            "me_id": me["id"] if me else None,
            "roles": db.ASSIGNABLE_ROLES,
            "invitable_roles": db.INVITABLE_ROLES,
            "role_labels": db.ROLE_LABELS,
            "mail_enabled": config.mail_enabled(),
            "invite_ttl_days": config.INVITE_TTL_DAYS,
            "notice": notice, "error": error,
        },
    )


def _users_redirect(*, notice: str = "", error: str = "") -> RedirectResponse:
    query = f"error={quote(error, safe='')}" if error else f"notice={quote(notice, safe='')}"
    return RedirectResponse(f"/system/users?{query}", status_code=303)


@router.post("/users/{user_id}/role")
def users_set_role(request: Request, user_id: int, role: str = Form(...)):
    """사용자 권한 변경. 최초 관리자는 변경 불가, 차단 시 세션 즉시 무효화.

    탈퇴(withdrawn)는 본인 탈퇴로만 진입하므로 부여할 수 없고, 탈퇴한
    계정의 권한도 되돌릴 수 없다 — 계정 정보 삭제 후 재가입/초대가 경로다.
    """
    if role not in db.ASSIGNABLE_ROLES:
        raise HTTPException(400, t(request, "부여할 수 없는 역할: {role}", role=repr(role)))
    with db.connect() as conn:
        target = db.get_user_by_id(conn, user_id)
        if target is None:
            raise HTTPException(404, t(request, "사용자 없음"))
        if target["is_founder"]:
            return _users_redirect(
                error=t(request, "최초 관리자의 권한은 변경할 수 없습니다.")
            )
        if target["role"] == "withdrawn":
            return _users_redirect(
                error=t(request,
                        "탈퇴한 계정의 권한은 변경할 수 없습니다 — 계정 정보를 삭제하세요.")
            )
        db.set_role(conn, user_id, role)
        if role == "blocked":
            db.delete_user_sessions(conn, user_id)
    return _users_redirect(
        notice=t(request, "{email} 권한을 '{label}'(으)로 변경했습니다.",
                 email=target["email"], label=t(request, db.ROLE_LABELS[role]))
    )


@router.post("/users/{user_id}/delete")
def users_delete(request: Request, user_id: int, email: str = Form("")):
    """계정 정보 삭제 (하드 삭제) — 실수 방지로 대상 이메일 입력을 요구한다.

    세션·OIDC 연결·패스키까지 일괄 삭제되어 같은 이메일로 다시 가입하거나
    초대할 수 있게 된다. 최초 관리자와 본인 계정은 삭제할 수 없다.
    """
    me = request.state.user
    with db.connect() as conn:
        target = db.get_user_by_id(conn, user_id)
        if target is None:
            raise HTTPException(404, t(request, "사용자 없음"))
        if target["is_founder"]:
            return _users_redirect(
                error=t(request, "최초 관리자는 삭제할 수 없습니다.")
            )
        if me is not None and target["id"] == me["id"]:
            return _users_redirect(
                error=t(request, "본인 계정은 여기서 삭제할 수 없습니다.")
            )
        if email.strip().lower() != target["email"].lower():
            return _users_redirect(
                error=t(request, "확인 이메일이 일치하지 않습니다.")
            )
        db.delete_user(conn, target["id"])
    return _users_redirect(
        notice=t(request,
                 "{email} 계정 정보를 삭제했습니다. 같은 이메일로 다시 가입하거나 "
                 "초대할 수 있습니다.", email=target["email"])
    )


@router.post("/users/{user_id}/name")
def users_set_name(request: Request, user_id: int, display_name: str = Form("")):
    """사용자 표시 이름 변경 (빈 입력 = 제거, 이메일로 표시)."""
    name = display_name.strip() or None
    if name is not None:
        error = auth.validate_display_name(name)
        if error is not None:
            return _users_redirect(error=t(request, error))
    with db.connect() as conn:
        target = db.get_user_by_id(conn, user_id)
        if target is None:
            raise HTTPException(404, t(request, "사용자 없음"))
        db.set_display_name(conn, user_id, name)
    return _users_redirect(
        notice=(
            t(request, "{email} 이름을 '{name}'(으)로 변경했습니다.",
              email=target["email"], name=name)
            if name
            else t(request, "{email} 이름을 제거했습니다.", email=target["email"])
        )
    )


@router.post("/users/{user_id}/logout")
def users_force_logout(request: Request, user_id: int):
    """사용자의 모든 세션 강제 로그아웃 (본인 대상이면 현재 세션도 끊긴다)."""
    with db.connect() as conn:
        target = db.get_user_by_id(conn, user_id)
        if target is None:
            raise HTTPException(404, t(request, "사용자 없음"))
        db.delete_user_sessions(conn, user_id)
    return _users_redirect(
        notice=t(request, "{email} 의 모든 세션을 로그아웃했습니다.", email=target["email"])
    )


def _invite_link(request: Request, token: str) -> str:
    """초대 수락 링크 — 외부 노출 환경이면 WCCG_PUBLIC_URL 기준으로 조립."""
    base = config.PUBLIC_URL or str(request.base_url).rstrip("/")
    return f"{base}/invite/{token}"


@router.post("/users/invite")
def users_invite(request: Request, email: str = Form(...), role: str = Form("viewer")):
    """이메일 초대 발급. 메일 설정이 없으면 링크를 화면에 표시해 직접 전달한다.

    같은 이메일을 다시 초대하면 새 토큰으로 교체된다 (이전 링크 무효화).
    """
    email = email.strip()
    error = auth.validate_email(email)
    if error is not None:
        return _users_redirect(error=t(request, error))
    if role not in db.INVITABLE_ROLES:
        raise HTTPException(400, t(request, "초대할 수 없는 역할: {role}", role=repr(role)))
    token = secrets.token_urlsafe(32)
    with db.connect() as conn:
        if db.get_user_by_email(conn, email) is not None:
            return _users_redirect(
                error=t(request, "{email} 은 이미 가입된 이메일입니다.", email=email)
            )
        db.create_invite(
            conn, email, auth.hash_token(token), role,
            invited_by=request.state.user["id"] if request.state.user else None,
            ttl_seconds=config.INVITE_TTL_DAYS * 86400,
        )
    link = _invite_link(request, token)
    if config.mail_enabled():
        inviter = (
            request.state.user["email"] if request.state.user
            else t(request, "관리자")
        )
        try:
            mailer.send_invite(email, link, inviter, db.ROLE_LABELS[role])
        except (smtplib.SMTPException, OSError) as e:
            logger.warning("초대 메일 발송 실패 (%s): %s", email, e)
            return _users_redirect(
                error=t(request,
                        "{email} 초대를 만들었지만 메일 발송에 실패했습니다 — "
                        "링크를 직접 전달하세요: {link}", email=email, link=link)
            )
        return _users_redirect(
            notice=t(request, "{email} 에게 초대 메일을 보냈습니다.", email=email)
        )
    return _users_redirect(
        notice=t(request, "{email} 초대 링크 (메일 미설정 — 직접 전달하세요): {link}",
                 email=email, link=link)
    )


@router.post("/users/invite/{invite_id}/delete")
def users_invite_delete(request: Request, invite_id: int):
    """초대 취소 — 링크가 즉시 무효화된다."""
    with db.connect() as conn:
        if not db.delete_invite(conn, invite_id):
            raise HTTPException(404, t(request, "초대 없음"))
    return _users_redirect(notice=t(request, "초대를 취소했습니다."))


# ---- API 키 ----
# 외부 소프트웨어용 키 발급/폐기. 발급자는 기록용일 뿐 모든 관리자가
# 공동으로 보고 폐기할 수 있다. 키 원문은 발급 직후 한 번만 표시된다.

# 만료 선택지 — 값은 ttl 초, None 은 영구. 'custom' 은 일 단위 직접 입력.
API_KEY_EXPIRY_OPTIONS = [
    ("permanent", "영구"),
    ("1d", "1일"),
    ("1m", "1개월 (30일)"),
    ("1y", "1년 (365일)"),
    ("custom", "사용자 지정 (일)"),
]
_EXPIRY_TTL_SECONDS: dict[str, int | None] = {
    "permanent": None,
    "1d": 86400,
    "1m": 30 * 86400,
    "1y": 365 * 86400,
}
MAX_API_KEY_CUSTOM_DAYS = 3650  # 10년 — 그 이상은 영구를 쓰면 된다


@router.get("/api-keys", response_class=HTMLResponse)
def api_keys_view(request: Request, notice: str = "", error: str = "", new_key: str = ""):
    """API 키 목록 + 발급 (관리자 전용 — 라우터 의존성이 보장)."""
    with db.connect() as conn:
        keys = db.list_api_keys(conn)
    return templates.TemplateResponse(
        request, "api_keys.html",
        {
            "keys": keys,
            "expiry_options": API_KEY_EXPIRY_OPTIONS,
            "max_custom_days": MAX_API_KEY_CUSTOM_DAYS,
            "new_key": new_key,
            "notice": notice, "error": error,
        },
    )


def _api_keys_redirect(
    *, notice: str = "", error: str = "", new_key: str = ""
) -> RedirectResponse:
    query = f"error={quote(error, safe='')}" if error else f"notice={quote(notice, safe='')}"
    if new_key:
        query += f"&new_key={quote(new_key, safe='')}"
    return RedirectResponse(f"/system/api-keys?{query}", status_code=303)


def _api_key_ttl(request: Request, expiry: str, custom_days: int) -> int | None:
    """만료 선택지를 ttl 초로 변환 (None=영구). 잘못된 입력은 ValueError(번역됨)."""
    if expiry in _EXPIRY_TTL_SECONDS:
        return _EXPIRY_TTL_SECONDS[expiry]
    if expiry == "custom":
        if not (1 <= custom_days <= MAX_API_KEY_CUSTOM_DAYS):
            raise ValueError(t(
                request, "사용자 지정 만료는 1 ~ {n}일 사이여야 합니다.",
                n=MAX_API_KEY_CUSTOM_DAYS,
            ))
        return custom_days * 86400
    raise ValueError(t(request, "알 수 없는 만료 선택: {expiry}", expiry=repr(expiry)))


@router.post("/api-keys")
def api_keys_create(
    request: Request,
    name: str = Form(...),
    can_view: bool = Form(False),
    can_archive: bool = Form(False),
    expiry: str = Form("permanent"),
    custom_days: int = Form(0),
):
    """API 키 발급. 키 원문은 이 응답의 화면에서만 한 번 표시된다."""
    name = name.strip()
    name_error = auth.validate_api_key_name(name)
    if name_error is not None:
        return _api_keys_redirect(error=t(request, name_error))
    if not (can_view or can_archive):
        return _api_keys_redirect(error=t(request, "권한을 하나 이상 선택하세요."))
    try:
        ttl_seconds = _api_key_ttl(request, expiry, custom_days)
    except ValueError as e:
        return _api_keys_redirect(error=str(e))
    with db.connect() as conn:
        token = auth.issue_api_key(
            conn, name,
            can_view=can_view, can_archive=can_archive,
            created_by=request.state.user["id"] if request.state.user else None,
            ttl_seconds=ttl_seconds,
        )
    return _api_keys_redirect(
        notice=t(request,
                 "'{name}' 키를 발급했습니다 — 아래 키를 지금 복사하세요. 다시 표시되지 않습니다.",
                 name=name),
        new_key=token,
    )


@router.post("/api-keys/{key_id}/delete")
def api_keys_delete(request: Request, key_id: int):
    """API 키 폐기 — 즉시 무효화된다."""
    with db.connect() as conn:
        if not db.delete_api_key(conn, key_id):
            raise HTTPException(404, t(request, "API 키 없음"))
    return _api_keys_redirect(notice=t(request, "키를 폐기했습니다."))


@router.post("/import")
def system_import(
    request: Request, file: UploadFile = File(...), mode: str = Form("merge")
):
    """내보낸 아카이브 데이터 업로드로 가져오기 (인증 데이터는 건드리지 않음)."""
    if mode not in ("merge", "overwrite"):
        raise HTTPException(400, t(request, "알 수 없는 모드: {mode}", mode=repr(mode)))
    tmp = _save_upload(file)
    try:
        result = backup_mod.import_archive(tmp, mode=mode)
    except (ValueError, tarfile.TarError, OSError) as e:
        return _system_redirect(error=t(request, "가져오기 실패: {e}", e=e))
    finally:
        tmp.unlink(missing_ok=True)
    return _system_redirect(
        notice=t(
            request,
            "가져오기 완료 [{mode}]: 페이지 +{pages}, 스냅샷 +{snapshots} "
            "(스킵 {skipped}), 확인 기록 +{checks}",
            mode=mode, pages=result.pages_added, snapshots=result.snapshots_added,
            skipped=result.snapshots_skipped, checks=result.checks_added,
        )
    )
