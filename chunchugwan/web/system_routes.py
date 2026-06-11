"""시스템 메뉴 — 백업/복원, 아카이브 내보내기/가져오기.

쓰기는 코어 모듈(backup.py)만 호출한다 (CLAUDE.md 원칙 1).
백업에는 인증 데이터(패스워드 해시·세션)가 포함되므로, 인증이 켜진 환경에서는
관리자만 접근할 수 있다 (인증 off 의 loopback 환경은 전체 허용).
"""

from __future__ import annotations

import shutil
import tarfile
import tempfile
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from starlette.background import BackgroundTask

from .. import backup as backup_mod
from .. import config, db
from .templating import templates

def system_allowed(user) -> bool:
    """시스템 메뉴 접근 가능 여부 — 인증 off(loopback) 이거나 관리자."""
    return not config.AUTH_ENABLED or bool(user and user["is_admin"])


def _require_admin(request: Request) -> None:
    """관리자 게이트. 로그인 자체는 미들웨어가 보장한다."""
    if not system_allowed(request.state.user):
        raise HTTPException(403, "관리자만 접근할 수 있습니다")


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


@router.post("/restore")
def system_restore(file: UploadFile = File(...)):
    """전체 백업 업로드로 복원 — 현재 데이터(인증 포함)를 백업 시점으로 교체.

    복원되면 세션 테이블도 백업 시점으로 돌아가므로 현재 로그인은 무효가
    될 수 있다 (미들웨어가 /login 으로 보낸다).
    """
    tmp = _save_upload(file)
    try:
        manifest = backup_mod.restore_backup(tmp)
    except (ValueError, tarfile.TarError, OSError) as e:
        return _system_redirect(error=f"복원 실패: {e}")
    finally:
        tmp.unlink(missing_ok=True)
    c = manifest.get("counts", {})
    return _system_redirect(
        notice=f"복원 완료 (백업: {manifest.get('created_at', '?')}, "
               f"페이지 {c.get('pages', '?')}개, 스냅샷 {c.get('snapshots', '?')}개)"
    )


@router.post("/import")
def system_import(file: UploadFile = File(...), mode: str = Form("merge")):
    """내보낸 아카이브 데이터 업로드로 가져오기 (인증 데이터는 건드리지 않음)."""
    if mode not in ("merge", "overwrite"):
        raise HTTPException(400, f"알 수 없는 모드: {mode!r}")
    tmp = _save_upload(file)
    try:
        result = backup_mod.import_archive(tmp, mode=mode)
    except (ValueError, tarfile.TarError, OSError) as e:
        return _system_redirect(error=f"가져오기 실패: {e}")
    finally:
        tmp.unlink(missing_ok=True)
    return _system_redirect(
        notice=f"가져오기 완료 [{mode}]: 페이지 +{result.pages_added}, "
               f"스냅샷 +{result.snapshots_added} (스킵 {result.snapshots_skipped}), "
               f"확인 기록 +{result.checks_added}"
    )
