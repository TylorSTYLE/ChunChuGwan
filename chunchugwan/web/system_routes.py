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
from .. import config, db, mailer, resources
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
    return templates.TemplateResponse(
        request, "system.html",
        {
            "counts": counts,
            "archive_root": str(config.ARCHIVE_ROOT),
            "db_bytes": config.DB_PATH.stat().st_size if config.DB_PATH.is_file() else 0,
            "sites_bytes": _dir_bytes(config.SITES_DIR),
            "resources_bytes": _dir_bytes(config.RESOURCES_DIR),
            "compactable": resources.compactable_count(),
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
    """저장 공간 압축 — 구형 스냅샷을 압축 저장 형태로 변환 (CLI compact 와 동일).

    내용 보존 변환이고 멱등이라 여러 번 실행해도 안전하다. 변환은 동기로
    실행된다 — 스냅샷이 아주 많으면 응답까지 시간이 걸릴 수 있다.
    압축 대상이 없으면 실행 없이 안내만 한다 (화면의 버튼도 비활성화).
    """
    dirs = resources.snapshot_dirs()
    if not dirs:
        return _system_redirect(notice=t(request, "압축할 스냅샷이 없습니다."))
    if not any(resources.needs_compaction(d) for d in dirs):
        return _system_redirect(
            notice=t(request, "스냅샷 {n}개 모두 이미 압축 형태입니다.", n=len(dirs))
        )
    try:
        result = resources.compact_all()
    except OSError as e:
        return _system_redirect(error=t(request, "압축 실패: {e}", e=e))
    return _system_redirect(
        notice=t(
            request,
            "압축 완료: 변환 {converted}/{total}개 · 공유 자원 {externalized}개 추출 · "
            "{before} → {after} ({saved} 절약)",
            converted=result.converted, total=result.total,
            externalized=result.externalized,
            before=filesize(result.before_bytes), after=filesize(result.after_bytes),
            saved=filesize(result.saved_bytes),
        )
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
            "roles": db.ROLES,
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
    """사용자 권한 변경. 최초 관리자는 변경 불가, 차단 시 세션 즉시 무효화."""
    if role not in db.ROLES:
        raise HTTPException(400, t(request, "알 수 없는 역할: {role}", role=repr(role)))
    with db.connect() as conn:
        target = db.get_user_by_id(conn, user_id)
        if target is None:
            raise HTTPException(404, t(request, "사용자 없음"))
        if target["is_founder"]:
            return _users_redirect(
                error=t(request, "최초 관리자의 권한은 변경할 수 없습니다.")
            )
        db.set_role(conn, user_id, role)
        if role == "blocked":
            db.delete_user_sessions(conn, user_id)
    return _users_redirect(
        notice=t(request, "{email} 권한을 '{label}'(으)로 변경했습니다.",
                 email=target["email"], label=t(request, db.ROLE_LABELS[role]))
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
