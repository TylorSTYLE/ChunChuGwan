"""시스템 메뉴 — 백업/복원, 아카이브 내보내기/가져오기.

쓰기는 코어 모듈(backup.py)만 호출한다 (CLAUDE.md 원칙 1).
백업에는 인증 데이터(패스워드 해시·세션)가 포함되므로, 인증이 켜진 환경에서는
관리자만 접근할 수 있다 (인증 off 의 loopback 환경은 전체 허용).
"""

from __future__ import annotations

import json
import logging
import secrets
import shutil
import smtplib
import tarfile
import tempfile
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from starlette.background import BackgroundTask

from .. import __version__, auth, backup as backup_mod
from .. import config, crawler, crypto, db, documents, mailer, optimize, resources
from .. import searchindex, storage
from . import audit, permissions
from .i18n import t
from .templating import filesize, templates

logger = logging.getLogger(__name__)


def _require_admin(request: Request) -> None:
    """/system 영역 게이트 — 경로에 따라 사용자 관리(manage_users) 또는
    시스템 관리(manage_system) 권한을 요구한다. 로그인 자체는 미들웨어가 보장.

    /system/users·/system/api-keys 는 사용자 관리, 그 외 시스템 설정·백업·복원·
    네트워크·로그 등은 시스템 관리 권한이다. 두 권한은 세분 권한 오버라이드로
    분리 부여할 수 있어 한 관리자에게만 사용자 관리를 맡기는 식이 가능하다.
    """
    path = request.url.path
    if path.startswith("/system/users") or path.startswith("/system/api-keys"):
        if not permissions.can_manage_users(request.state.user):
            raise HTTPException(403, t(request, "사용자 관리 권한이 없습니다"))
    else:
        if not permissions.can_manage_system(request.state.user):
            raise HTTPException(403, t(request, "시스템 관리 권한이 없습니다"))


router = APIRouter(prefix="/system", dependencies=[Depends(_require_admin)])


@router.get("", response_class=HTMLResponse)
def system_view(request: Request, notice: str = "", error: str = ""):
    with db.connect() as conn:
        counts = {
            t: conn.execute(f"SELECT COUNT(*) AS c FROM {t}").fetchone()["c"]
            for t in ("pages", "snapshots", "checks", "users")
        }
        signup_enabled = db.signup_enabled(conn)
        signup_default_role = db.signup_default_role(conn)
        signup_role_choices = db.signup_roles(conn)
        role_labels = db.role_labels(conn)
        email_verification_enabled = db.email_verification_enabled(conn)
        email_verification_ttl_minutes = db.email_verification_ttl_minutes(conn)
        crawl_defaults = crawler.crawl_defaults(conn)
        crawl_backoff = crawler.retry_backoff(conn)
        network_tags = db.list_network_tags(conn)
        ext_credential_ttl_hours = db.ext_credential_ttl_hours(conn)
        mobile_screenshot_enabled = db.mobile_screenshot_enabled(conn)
        doc_limits = documents.limits(conn)
        smtp = mailer.resolve_config(conn)
        smtp_has_password = db.get_setting(conn, db.SMTP_PASSWORD_KEY) not in (None, "")
    usage = storage.archive_disk_usage()
    return templates.TemplateResponse(
        request, "system.html",
        {
            "version": __version__,
            "counts": counts,
            "signup_enabled": signup_enabled,
            "signup_default_role": signup_default_role,
            "signup_roles": signup_role_choices,
            "email_verification_enabled": email_verification_enabled,
            "email_verification_ttl_minutes": email_verification_ttl_minutes,
            "email_verification_ttl_limits": {
                "min": config.EMAIL_VERIFICATION_TTL_MINUTES_MIN,
                "max": config.EMAIL_VERIFICATION_TTL_MINUTES_MAX,
            },
            "role_labels": role_labels,
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
            "ext_credential_ttl_hours": ext_credential_ttl_hours,
            "ext_credential_ttl_limits": {
                "min": config.EXT_CREDENTIAL_TTL_HOURS_MIN,
                "max": config.EXT_CREDENTIAL_TTL_HOURS_MAX,
            },
            "mobile_screenshot_enabled": mobile_screenshot_enabled,
            "mobile_screenshot_size":
                f"{config.MOBILE_SCREENSHOT_WIDTH} × {config.MOBILE_SCREENSHOT_HEIGHT}",
            "document_limits": {
                "max_count": doc_limits.max_count,
                "max_mb": doc_limits.max_bytes // (1024 * 1024),
                "timeout_seconds": doc_limits.timeout_seconds,
            },
            "document_limit_ranges": {
                "count_min": config.DOCUMENT_MAX_COUNT_MIN,
                "count_max": config.DOCUMENT_MAX_COUNT_MAX,
                "mb_min": config.DOCUMENT_MAX_MB_MIN,
                "mb_max": config.DOCUMENT_MAX_MB_MAX,
                "timeout_min": config.DOCUMENT_FETCH_TIMEOUT_MIN,
                "timeout_max": config.DOCUMENT_FETCH_TIMEOUT_MAX,
            },
            "credential_key_set": crypto.is_configured(),
            "smtp_config": {
                "host": smtp.host,
                "port": smtp.port,
                "user": smtp.user,
                "sender": smtp.sender,
                "tls": smtp.tls,
                "enabled": smtp.enabled,
                "has_password": smtp_has_password,
            },
            "smtp_tls_modes": mailer.SMTP_TLS_MODES,
            "smtp_test_to": (
                request.state.user["email"] if request.state.user else ""
            ),
            "archive_root": str(config.ARCHIVE_ROOT),
            "db_bytes": usage["db"],
            "sites_bytes": usage["sites"],
            "resources_bytes": usage["resources"],
            "documents_bytes": usage["documents"],
            "optimize_pending": sum(optimize.pending_counts()),
            "search": searchindex.verify(),
            "search_reindex": reindex_status(),
            "notice": notice, "error": error,
        },
    )


_SYSLOG_PAGE_SIZES = (25, 50, 100, 200)
_SYSLOG_PAGE_SIZE_DEFAULT = 50


def _clean_date(value: str | None) -> str | None:
    """날짜 입력을 YYYY-MM-DD 로 정규화, 파싱 불가면 None (필터 무시)."""
    if not value:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


@router.get("/logs", response_class=HTMLResponse)
def system_logs_view(
    request: Request,
    level: str | None = None,
    source: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    page: int = 1,
    limit: int = _SYSLOG_PAGE_SIZE_DEFAULT,
):
    """시스템 로그 — 앱(serve·worker·CLI)의 logging 레코드. 관리자 전용 (라우터 가드)."""
    if limit not in _SYSLOG_PAGE_SIZES:
        limit = _SYSLOG_PAGE_SIZE_DEFAULT
    if level not in db.SYSTEM_LOG_LEVELS:
        level = None
    if source not in db.SYSTEM_LOG_SOURCES:
        source = None
    date_from = _clean_date(date_from)
    date_to = _clean_date(date_to)
    if date_from and date_to and date_from > date_to:
        date_from, date_to = date_to, date_from
    filters = {
        "level": level, "source": source,
        "date_from": date_from, "date_to": date_to,
    }
    with db.connect() as conn:
        total = db.count_system_logs(conn, **filters)
        total_pages = max(1, -(-total // limit))  # ceil
        page = max(1, min(page, total_pages))
        logs = db.list_system_logs(
            conn, **filters, limit=limit, offset=(page - 1) * limit,
        )

    # 페이징 링크 — 현재 필터를 유지한 채 page 만 바꾼다
    qs_base = [
        (k, v) for k, v in (
            ("level", level), ("source", source),
            ("date_from", date_from), ("date_to", date_to),
        ) if v is not None
    ]
    if limit != _SYSLOG_PAGE_SIZE_DEFAULT:
        qs_base.append(("limit", limit))

    def _page_url(n: int) -> str:
        params = qs_base + ([("page", n)] if n > 1 else [])
        return "/system/logs" + ("?" + urlencode(params) if params else "")

    return templates.TemplateResponse(
        request, "system_logs.html",
        {
            "logs": logs,
            "level": level or "", "source": source or "",
            "date_from": date_from or "", "date_to": date_to or "",
            "levels": db.SYSTEM_LOG_LEVELS, "sources": db.SYSTEM_LOG_SOURCES,
            "limit": limit, "limits": _SYSLOG_PAGE_SIZES,
            "total": total, "total_pages": total_pages, "page_num": page,
            "prev_url": _page_url(page - 1) if page > 1 else None,
            "next_url": _page_url(page + 1) if page < total_pages else None,
        },
    )


def tar_download(make: Callable[[Path], Path], prefix: str) -> FileResponse:
    """코어 함수로 tar.gz 를 만들어 다운로드로 응답 (전송 후 임시 파일 정리).

    사이트 단위 내보내기(app.site_export)도 같은 헬퍼를 쓴다.
    """
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
def system_backup(request: Request) -> FileResponse:
    """전체 백업 tar.gz 다운로드 (DB·인증 데이터·스냅샷 파일·rules.json)."""
    audit.log(request, "전체 백업 다운로드")
    return tar_download(backup_mod.create_backup, "backup")


@router.post("/export")
def system_export(request: Request) -> FileResponse:
    """아카이브 데이터만 내보내기 다운로드 (인증 데이터 제외)."""
    audit.log(request, "아카이브 내보내기 다운로드")
    return tar_download(backup_mod.export_archive, "export")


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
    audit.log(request, "저장공간 최적화 실행")
    c = result.compact
    return _system_redirect(
        notice=t(
            request,
            "최적화 완료: 변환 {converted}/{total}개 · 공유 자원 {externalized}개 추출 · "
            "문서 {documents}개 이전 · 공통 스타일 {styles}개 추출(스냅샷 {styled}개) · "
            "참조 백필 {indexed}개 · 고아 자원 {swept}개 정리 ({saved} 절약)",
            converted=c.converted, total=c.total,
            externalized=c.externalized, documents=c.documents,
            styles=result.styles_extracted, styled=result.styles_snapshots,
            indexed=result.indexed, swept=result.swept,
            saved=filesize(
                c.saved_bytes + result.styles_saved_bytes + result.swept_bytes
            ),
        )
    )


# 전체 다시 색인 진행 상태 — serve 단일 프로세스의 인메모리 (app._active_jobs 와 같은 패턴).
# 백그라운드 스레드가 갱신하고, 시스템 화면이 /system/search/reindex/status 를 폴링한다.
_reindex_lock = threading.Lock()
_reindex_state: dict = {
    "running": False, "done": 0, "total": 0,
    "result": None, "error": None, "finished_at": None,
}


def reindex_status() -> dict:
    """전체 다시 색인 진행 상태의 사본 (폴링/초기 렌더용)."""
    with _reindex_lock:
        return dict(_reindex_state)


def _reindex_worker() -> None:
    """백그라운드 재색인 — 진행률을 인메모리 상태에 갱신."""
    def _progress(done: int, total: int) -> None:
        with _reindex_lock:
            _reindex_state["done"] = done
            _reindex_state["total"] = total

    try:
        count = searchindex.reindex_all(progress=_progress)
        with _reindex_lock:
            _reindex_state["result"] = count
            _reindex_state["error"] = None
    except Exception as e:  # noqa: BLE001 — 실패해도 상태만 기록, 스레드는 정상 종료
        logger.exception("검색 인덱스 전체 다시 색인 실패")
        with _reindex_lock:
            _reindex_state["error"] = str(e)
    finally:
        with _reindex_lock:
            _reindex_state["running"] = False
            _reindex_state["finished_at"] = datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            )


@router.post("/search/reindex")
def system_search_reindex(request: Request):
    """검색 인덱스 전체 다시 색인을 백그라운드로 시작 (즉시 응답).

    CLI ``wccg search reindex --all`` 과 같은 작업(searchindex.reindex_all)을
    별도 스레드에서 돌린다 — 동기로 돌리면 첨부 문서 본문 추출까지 다시 하는
    동안 요청이 멈춰 대시보드가 응답하지 않는다. 진행 상황은 시스템 화면이
    /system/search/reindex/status 를 폴링해 보여준다. 과소 색인·orphan·stale
    (예: compact 이후 문서 본문 미반영)을 한 번에 바로잡는다. FTS5 미지원이면 비활성.
    """
    if not searchindex.available():
        return _system_redirect(
            error=t(request, "검색 인덱스를 쓸 수 없습니다 — 이 SQLite 빌드에 FTS5 가 없습니다.")
        )
    with _reindex_lock:
        if _reindex_state["running"]:
            return _system_redirect(
                notice=t(request, "이미 전체 다시 색인이 진행 중입니다.")
            )
        _reindex_state.update(
            running=True, done=0, total=0, result=None, error=None, finished_at=None
        )
    audit.log(request, "검색 인덱스 전체 다시 색인 시작")
    threading.Thread(target=_reindex_worker, daemon=True).start()
    return _system_redirect(
        notice=t(request, "검색 인덱스 전체 다시 색인을 시작했습니다 — 아래에 진행 상황이 표시됩니다.")
    )


@router.get("/search/reindex/status")
def system_search_reindex_status(request: Request) -> dict:
    """전체 다시 색인 진행 상태(JSON) — 시스템 화면 폴링용 (관리자 전용 라우터)."""
    return reindex_status()


@router.post("/settings")
def system_settings(
    request: Request,
    signup_enabled: bool = Form(False),
    signup_default_role: str = Form("pending"),
):
    """가입 설정 저장 — 회원 가입 허용 여부와 가입 계정의 초기 권한."""
    with db.connect() as conn:
        if signup_default_role not in db.signup_roles(conn):
            raise HTTPException(
                400, t(request, "가입 초기 권한으로 쓸 수 없는 역할: {role}",
                       role=repr(signup_default_role))
            )
        db.set_setting(
            conn, db.SIGNUP_ENABLED_KEY, "on" if signup_enabled else "off"
        )
        db.set_setting(conn, db.SIGNUP_DEFAULT_ROLE_KEY, signup_default_role)
    audit.log(
        request, "가입 설정 변경: 가입 %s, 초기 권한 %s",
        "허용" if signup_enabled else "차단", signup_default_role,
    )
    return _system_redirect(notice=t(request, "가입 설정을 저장했습니다."))


@router.post("/email-verification-settings")
def system_email_verification_settings(
    request: Request,
    email_verification_enabled: bool = Form(False),
    email_verification_ttl_minutes: int = Form(...),
):
    """이메일 본인 인증 설정 저장 — 사용 여부와 코드 만료 시간(분).

    SMTP 가 설정되지 않으면 켜더라도 동작하지 않는다 (로그인 게이트가 무시).
    """
    lo = config.EMAIL_VERIFICATION_TTL_MINUTES_MIN
    hi = config.EMAIL_VERIFICATION_TTL_MINUTES_MAX
    if not (lo <= email_verification_ttl_minutes <= hi):
        return _system_redirect(
            error=t(request, "인증 코드 만료 시간은 {lo} ~ {hi}분 사이여야 합니다.",
                    lo=lo, hi=hi)
        )
    with db.connect() as conn:
        db.set_setting(
            conn, db.EMAIL_VERIFICATION_ENABLED_KEY,
            "on" if email_verification_enabled else "off",
        )
        db.set_setting(
            conn, db.EMAIL_VERIFICATION_TTL_MINUTES_KEY,
            str(email_verification_ttl_minutes),
        )
    audit.log(
        request, "이메일 본인 인증 설정 변경: %s, 코드 만료 %d분",
        "사용" if email_verification_enabled else "사용 안 함",
        email_verification_ttl_minutes,
    )
    return _system_redirect(notice=t(request, "이메일 본인 인증 설정을 저장했습니다."))


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
    audit.log(
        request,
        "사이트 아카이브 설정 변경: 최대 %d페이지, 깊이 %d, 간격 %d초, 재시도 대기 [%s]",
        crawl_max_pages, crawl_max_depth, crawl_delay,
        ", ".join(str(v) for v in backoff),
    )
    return _system_redirect(notice=t(request, "사이트 아카이브 설정을 저장했습니다."))


@router.post("/credential-settings")
def system_credential_settings(
    request: Request, ext_credential_ttl_hours: int = Form(...)
):
    """확장 1회성 세션 자격증명의 만료 안전망 TTL(시간) 저장."""
    lo, hi = config.EXT_CREDENTIAL_TTL_HOURS_MIN, config.EXT_CREDENTIAL_TTL_HOURS_MAX
    if not (lo <= ext_credential_ttl_hours <= hi):
        return _system_redirect(
            error=t(request, "자격증명 보관 시간은 {lo} ~ {hi}시간 사이여야 합니다.",
                    lo=lo, hi=hi)
        )
    with db.connect() as conn:
        db.set_setting(
            conn, db.EXT_CREDENTIAL_TTL_HOURS_KEY, str(ext_credential_ttl_hours)
        )
    audit.log(request, "확장 자격증명 설정 변경: 보관 %d시간", ext_credential_ttl_hours)
    return _system_redirect(notice=t(request, "확장 자격증명 설정을 저장했습니다."))


@router.post("/capture-settings")
def system_capture_settings(
    request: Request, mobile_screenshot_enabled: bool = Form(False)
):
    """캡처 설정 저장 — 모바일 해상도 스크린샷도 함께 저장할지 (기본 off).

    이후 새로 만들어지는 스냅샷에만 적용된다 (기존 스냅샷은 그대로).
    """
    with db.connect() as conn:
        db.set_setting(
            conn, db.MOBILE_SCREENSHOT_ENABLED_KEY,
            "on" if mobile_screenshot_enabled else "off",
        )
    audit.log(
        request, "캡처 설정 변경: 모바일 스크린샷 %s",
        "켜짐" if mobile_screenshot_enabled else "꺼짐",
    )
    return _system_redirect(notice=t(request, "캡처 설정을 저장했습니다."))


@router.post("/document-settings")
def system_document_settings(
    request: Request,
    document_max_count: int = Form(...),
    document_max_mb: int = Form(...),
    document_fetch_timeout: int = Form(...),
):
    """문서 아카이브 한도 저장 — 스냅샷당 수·문서 1개 크기(MB)·다운로드 타임아웃(초).

    이후 새로 저장되는 스냅샷의 문서 다운로드에 적용된다.
    """
    ranges = (
        ("document_max_count", document_max_count,
         config.DOCUMENT_MAX_COUNT_MIN, config.DOCUMENT_MAX_COUNT_MAX,
         "문서 수 한도는 {lo} ~ {hi}개 사이여야 합니다."),
        ("document_max_mb", document_max_mb,
         config.DOCUMENT_MAX_MB_MIN, config.DOCUMENT_MAX_MB_MAX,
         "문서 크기 한도는 {lo} ~ {hi}MB 사이여야 합니다."),
        ("document_fetch_timeout", document_fetch_timeout,
         config.DOCUMENT_FETCH_TIMEOUT_MIN, config.DOCUMENT_FETCH_TIMEOUT_MAX,
         "문서 다운로드 타임아웃은 {lo} ~ {hi}초 사이여야 합니다."),
    )
    for _name, value, lo, hi, msg in ranges:
        if not (lo <= value <= hi):
            return _system_redirect(error=t(request, msg, lo=lo, hi=hi))
    with db.connect() as conn:
        db.set_setting(conn, db.DOCUMENT_MAX_COUNT_KEY, str(document_max_count))
        db.set_setting(conn, db.DOCUMENT_MAX_MB_KEY, str(document_max_mb))
        db.set_setting(
            conn, db.DOCUMENT_FETCH_TIMEOUT_KEY, str(document_fetch_timeout)
        )
    audit.log(
        request, "문서 아카이브 설정 변경: 최대 %d개, 개당 %dMB, 타임아웃 %d초",
        document_max_count, document_max_mb, document_fetch_timeout,
    )
    return _system_redirect(notice=t(request, "문서 아카이브 설정을 저장했습니다."))


@router.post("/smtp-settings")
def system_smtp_settings(
    request: Request,
    smtp_host: str = Form(""),
    smtp_port: int = Form(587),
    smtp_user: str = Form(""),
    smtp_password: str = Form(""),
    smtp_from: str = Form(""),
    smtp_tls: str = Form("starttls"),
    smtp_clear_password: bool = Form(False),
):
    """초대 메일 발송 SMTP 설정 저장 (DB — WCCG_SMTP_* 환경변수보다 우선).

    비밀번호는 대칭 암호화한 암호문으로만 저장한다(원칙 6 예외). 입력칸을
    비우면 기존 저장값을 유지하고, '저장된 비밀번호 삭제'를 켜면 지운다.
    """
    smtp_host = smtp_host.strip()
    smtp_user = smtp_user.strip()
    smtp_from = smtp_from.strip()
    if smtp_tls not in mailer.SMTP_TLS_MODES:
        return _system_redirect(error=t(request, "TLS 모드가 올바르지 않습니다."))
    if not (1 <= smtp_port <= 65535):
        return _system_redirect(
            error=t(request, "SMTP 포트는 1 ~ 65535 사이여야 합니다.")
        )
    if smtp_password and not crypto.is_configured():
        return _system_redirect(
            error=t(request,
                    "WCCG_SECRET_KEY 가 설정되지 않아 SMTP 비밀번호를 저장할 수 없습니다.")
        )
    with db.connect() as conn:
        db.set_setting(conn, db.SMTP_HOST_KEY, smtp_host)
        db.set_setting(conn, db.SMTP_PORT_KEY, str(smtp_port))
        db.set_setting(conn, db.SMTP_USER_KEY, smtp_user)
        db.set_setting(conn, db.SMTP_FROM_KEY, smtp_from)
        db.set_setting(conn, db.SMTP_TLS_KEY, smtp_tls)
        if smtp_password:
            db.set_setting(conn, db.SMTP_PASSWORD_KEY, crypto.encrypt(smtp_password))
        elif smtp_clear_password:
            db.delete_setting(conn, db.SMTP_PASSWORD_KEY)
    audit.log(
        request, "메일(SMTP) 설정 변경: 호스트 %s, 포트 %d, TLS %s",
        smtp_host or "(없음)", smtp_port, smtp_tls,
    )
    return _system_redirect(notice=t(request, "메일(SMTP) 설정을 저장했습니다."))


@router.post("/smtp-test")
def system_smtp_test(request: Request):
    """저장된 SMTP 설정으로 요청 관리자 본인에게 테스트 메일을 보낸다."""
    me = request.state.user
    to_email = me["email"] if me else ""
    if not to_email:
        return _system_redirect(
            error=t(request, "테스트 메일을 받을 이메일 주소가 없습니다.")
        )
    with db.connect() as conn:
        smtp = mailer.resolve_config(conn)
    if not smtp.enabled:
        return _system_redirect(
            error=t(request, "SMTP 호스트가 설정되지 않았습니다.")
        )
    try:
        mailer.send_test(smtp, to_email)
    except (smtplib.SMTPException, OSError) as e:
        logger.warning("SMTP 테스트 메일 발송 실패 (%s): %s", to_email, e)
        return _system_redirect(
            error=t(request, "테스트 메일 발송에 실패했습니다: {e}", e=e)
        )
    audit.log(request, "SMTP 테스트 메일 발송: %s", to_email)
    return _system_redirect(
        notice=t(request, "{email} 로 테스트 메일을 보냈습니다.", email=to_email)
    )


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
    audit.log(request, "로컬 네트워크 태그 추가: '%s'", name)
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
    audit.log(request, "로컬 네트워크 태그 삭제: '%s'", tag["name"])
    return _system_redirect(
        notice=t(request, "로컬 네트워크 태그 '{name}'을(를) 삭제했습니다.",
                 name=tag["name"])
    )


@router.post("/network-tags/merge")
def network_tags_merge(
    request: Request, source: str = Form(...), target: str = Form(...)
):
    """두 로컬 네트워크 태그 병합 — source 의 참조를 target 으로 옮기고 source 삭제.

    같은 사설 IP:포트(같은 사이트)를 가리킬 때만 허용한다 — 두 태그의 site_id
    집합이 완전히 같을 때다 (network_tag_site_ids). 참조가 없는 태그는 삭제를 쓴다.
    검증·병합을 한 트랜잭션 안에서 처리해 경합(TOCTOU)을 피한다.
    """
    with db.connect() as conn:
        src = db.get_network_tag(conn, source)
        tgt = db.get_network_tag(conn, target)
        if src is None or tgt is None:
            raise HTTPException(404, t(request, "로컬 네트워크 태그 없음"))
        if source == target:
            return _system_redirect(
                error=t(request, "같은 태그끼리는 병합할 수 없습니다.")
            )
        src_sites = db.network_tag_site_ids(conn, source)
        tgt_sites = db.network_tag_site_ids(conn, target)
        if not src_sites or not tgt_sites:
            return _system_redirect(
                error=t(request,
                        "참조가 없는 태그는 병합할 수 없습니다 — 삭제를 사용하세요.")
            )
        if src_sites != tgt_sites:
            return _system_redirect(
                error=t(request,
                        "두 태그가 같은 사설 네트워크(같은 IP·포트)를 가리킬 때만 "
                        "병합할 수 있습니다.")
            )
        moved = db.merge_network_tags(conn, source, target)
    audit.log(
        request, "로컬 네트워크 태그 병합: '%s' → '%s' (페이지 %d·크롤 %d·스케줄 %d)",
        src["name"], tgt["name"],
        moved["pages"], moved["crawls"], moved["crawl_schedules"],
    )
    return _system_redirect(
        notice=t(request,
                 "'{src}' 태그를 '{tgt}'(으)로 병합했습니다 "
                 "(페이지 {p}개·크롤 {c}개·스케줄 {s}개 이전).",
                 src=src["name"], tgt=tgt["name"],
                 p=moved["pages"], c=moved["crawls"], s=moved["crawl_schedules"])
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
    audit.log(
        request, "백업 복원 실행 (백업: %s)", manifest.get("created_at", "?")
    )
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
        mail_on = mailer.mail_enabled(conn)
        presets = db.role_presets(conn)
        assignable = db.assignable_roles(conn)
        invitable = db.invitable_roles(conn)
        role_labels = db.role_labels(conn)
        permission_role_names = db.permission_group_names(conn)
    # 세분 권한 편집기용 — 사용자별 실효 권한 + 프리셋과 다른(오버라이드된) 항목
    user_perms = {
        u["id"]: {
            "effective": sorted(
                db.effective_permissions(
                    u["role"], u["permission_overrides"], presets=presets
                )
            ),
            "overridden": sorted(
                db.parse_permission_overrides(u["permission_overrides"])
            ),
        }
        for u in users
    }
    return templates.TemplateResponse(
        request, "users.html",
        {
            "users": users,
            "invites": invites,
            "me_id": me["id"] if me else None,
            "roles": assignable,
            "invitable_roles": invitable,
            "role_labels": role_labels,
            "permission_roles": permission_role_names,
            "permissions_catalog": db.PERMISSIONS,
            "permission_labels": db.PERMISSION_LABELS,
            "user_perms": user_perms,
            "mail_enabled": mail_on,
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
    with db.connect() as conn:
        if role not in db.assignable_roles(conn):
            raise HTTPException(
                400, t(request, "부여할 수 없는 역할: {role}", role=repr(role))
            )
        labels = db.role_labels(conn)
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
        # 라스트-관리자 잠김 방지: 새 역할 프리셋에 manage_users 가 없고, 이 권한을
        # 가진 다른 활성 사용자가 없으면 거부 (역할 변경은 오버라이드를 초기화한다)
        presets = db.role_presets(conn)
        if "manage_users" not in presets.get(role, frozenset()):
            others = db.count_active_users_with_permission(
                conn, "manage_users", exclude_user_id=user_id, presets=presets
            )
            if others == 0:
                return _users_redirect(
                    error=t(request,
                            "사용자 관리 권한을 가진 마지막 계정입니다 — 역할을 바꿀 수 없습니다.")
                )
        db.set_role(conn, user_id, role)
        # 역할 변경 = 새 역할 프리셋으로 초기화 (이전 세분 권한 오버라이드는 비운다)
        db.set_permission_overrides(conn, user_id, {})
        if role == "blocked":
            db.delete_user_sessions(conn, user_id)
    label = labels.get(role, role)
    audit.log(request, "사용자 권한 변경: %s → %s", target["email"], label)
    return _users_redirect(
        notice=t(request, "{email} 권한을 '{label}'(으)로 변경했습니다.",
                 email=target["email"], label=t(request, label))
    )


@router.post("/users/{user_id}/permissions")
async def users_set_permissions(request: Request, user_id: int):
    """사용자 세분 권한 오버라이드 저장 — 역할 프리셋과 다른 항목만 저장한다.

    폼은 권한별 체크박스(perm_<키>)로, 체크 상태와 프리셋이 다른 권한만
    오버라이드로 남긴다. 최초 관리자·비활성(탈퇴/차단/대기) 계정은 조정 불가.
    """
    form = await request.form()
    with db.connect() as conn:
        target = db.get_user_by_id(conn, user_id)
        if target is None:
            raise HTTPException(404, t(request, "사용자 없음"))
        if target["is_founder"]:
            return _users_redirect(
                error=t(request, "최초 관리자의 권한은 변경할 수 없습니다.")
            )
        presets = db.role_presets(conn)
        if target["role"] not in db.permission_group_names(conn):
            return _users_redirect(
                error=t(request,
                        "이 계정 상태에서는 세분 권한을 조정할 수 없습니다 — 먼저 역할을 부여하세요.")
            )
        preset = presets.get(target["role"], frozenset())
        overrides = {}
        for perm in db.PERMISSIONS:
            granted = form.get(f"perm_{perm}") is not None
            if granted != (perm in preset):
                overrides[perm] = granted
        # 라스트-관리자 잠김 방지: manage_users 를 마지막 보유자에게서 떼면 거부
        new_eff = db.effective_permissions(
            target["role"], json.dumps(overrides), presets=presets
        )
        if "manage_users" not in new_eff:
            others = db.count_active_users_with_permission(
                conn, "manage_users", exclude_user_id=user_id, presets=presets
            )
            if others == 0:
                return _users_redirect(
                    error=t(request,
                            "사용자 관리 권한을 가진 마지막 계정입니다 — 이 권한은 뗄 수 없습니다.")
                )
        db.set_permission_overrides(conn, user_id, overrides)
    audit.log(request, "사용자 세분 권한 변경: %s", target["email"])
    return _users_redirect(
        notice=t(request, "{email} 의 세분 권한을 저장했습니다.", email=target["email"])
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
    audit.log(request, "사용자 계정 정보 삭제: %s", target["email"])
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
    audit.log(
        request, "사용자 이름 변경: %s → %s",
        target["email"], f"'{name}'" if name else "(제거)",
    )
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
    audit.log(request, "사용자 강제 로그아웃: %s", target["email"])
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
    token = secrets.token_urlsafe(32)
    with db.connect() as conn:
        if role not in db.invitable_roles(conn):
            raise HTTPException(
                400, t(request, "초대할 수 없는 역할: {role}", role=repr(role))
            )
        role_label = db.role_labels(conn).get(role, role)
        if db.get_user_by_email(conn, email) is not None:
            return _users_redirect(
                error=t(request, "{email} 은 이미 가입된 이메일입니다.", email=email)
            )
        db.create_invite(
            conn, email, auth.hash_token(token), role,
            invited_by=request.state.user["id"] if request.state.user else None,
            ttl_seconds=config.INVITE_TTL_DAYS * 86400,
        )
    audit.log(request, "사용자 초대 발급: %s (권한 %s)", email, role_label)
    link = _invite_link(request, token)
    with db.connect() as conn:
        smtp = mailer.resolve_config(conn)
    if smtp.enabled:
        inviter = (
            request.state.user["email"] if request.state.user
            else t(request, "관리자")
        )
        try:
            mailer.send_invite(smtp, email, link, inviter, role_label)
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
        invite = next(
            (i for i in db.list_invites(conn) if i["id"] == invite_id), None
        )
        if not db.delete_invite(conn, invite_id):
            raise HTTPException(404, t(request, "초대 없음"))
    audit.log(
        request, "초대 취소: %s", invite["email"] if invite else f"#{invite_id}"
    )
    return _users_redirect(notice=t(request, "초대를 취소했습니다."))


# ---- 권한 그룹 ----
# 역할(권한 묶음)을 코드 배포 없이 정의·편집한다. 빌트인(admin/archiver/viewer)은
# permissions 묶음만 편집 가능(삭제·개명 불가), 커스텀 그룹은 추가·삭제할 수 있다.
# pending/blocked/withdrawn 은 권한묶음이 아니라 접근 게이트 상태라 여기서 다루지
# 않는다. 라우터 의존성(_require_admin)이 manage_system 권한을 요구한다.


def _groups_redirect(*, notice: str = "", error: str = "") -> RedirectResponse:
    query = (
        f"error={quote(error, safe='')}" if error
        else f"notice={quote(notice, safe='')}"
    )
    return RedirectResponse(f"/system/groups?{query}", status_code=303)


@router.get("/groups", response_class=HTMLResponse)
def groups_view(request: Request, notice: str = "", error: str = ""):
    """권한 그룹 관리 화면 — 그룹별 세분 권한 편집 + 커스텀 그룹 추가/삭제."""
    with db.connect() as conn:
        groups = [
            {
                "name": r["name"],
                "label": r["label"],
                "is_builtin": bool(r["is_builtin"]),
                "permissions": set(db._parse_permission_list(r["permissions"])),
                "member_count": db.count_users_with_role(conn, r["name"]),
            }
            for r in db.list_permission_groups(conn)
        ]
    return templates.TemplateResponse(
        request, "groups.html",
        {
            "groups": groups,
            "permissions_catalog": db.PERMISSIONS,
            "permission_labels": db.PERMISSION_LABELS,
            "notice": notice, "error": error,
        },
    )


@router.post("/groups")
async def groups_add(request: Request):
    """커스텀 권한 그룹 생성 — name 정규화·중복은 코어(create_permission_group)가 검증."""
    form = await request.form()
    name = (form.get("name") or "").strip()
    label = (form.get("label") or "").strip()
    perms = [p for p in db.PERMISSIONS if form.get(f"perm_{p}") is not None]
    with db.connect() as conn:
        try:
            created = db.create_permission_group(conn, name, label, perms)
        except ValueError as e:
            return _groups_redirect(error=t(request, str(e)))
    audit.log(request, "권한 그룹 생성: %s", created)
    return _groups_redirect(
        notice=t(request, "권한 그룹 '{name}' 을(를) 만들었습니다.", name=created)
    )


@router.post("/groups/{name}/delete")
def groups_delete(request: Request, name: str):
    """권한 그룹 삭제 — 빌트인·소속 사용자 있는 그룹은 거부(참조 거부 정책)."""
    with db.connect() as conn:
        group = db.get_permission_group(conn, name)
        if group is None:
            raise HTTPException(404, t(request, "권한 그룹 없음"))
        if group["is_builtin"]:
            return _groups_redirect(
                error=t(request, "기본 권한 그룹은 삭제할 수 없습니다.")
            )
        members = db.count_users_with_role(conn, name)
        if members > 0:
            return _groups_redirect(
                error=t(request,
                        "{n}명이 이 그룹에 속해 있습니다 — 먼저 사용자 역할을 옮기세요.",
                        n=members)
            )
        db.delete_permission_group(conn, name)
    audit.log(request, "권한 그룹 삭제: %s", name)
    return _groups_redirect(
        notice=t(request, "권한 그룹 '{name}' 을(를) 삭제했습니다.", name=name)
    )


@router.post("/groups/{name}")
async def groups_edit(request: Request, name: str):
    """그룹 세분 권한(+커스텀은 라벨) 갱신. 라스트-manage_users 잠김을 시뮬레이션으로 방지."""
    form = await request.form()
    label = (form.get("label") or "").strip()
    perms = [p for p in db.PERMISSIONS if form.get(f"perm_{p}") is not None]
    with db.connect() as conn:
        group = db.get_permission_group(conn, name)
        if group is None:
            raise HTTPException(404, t(request, "권한 그룹 없음"))
        # 라스트-관리자 잠김 방지 — 이 그룹의 새 권한으로 프리셋을 시뮬레이션해
        # manage_users 보유 활성 사용자가 0 이 되면 거부 (오버라이드는 반영됨)
        simulated = dict(db.role_presets(conn))
        simulated[name] = frozenset(perms)
        if db.count_active_users_with_permission(
            conn, "manage_users", presets=simulated
        ) == 0:
            return _groups_redirect(
                error=t(request,
                        "이 변경은 사용자 관리 권한을 가진 활성 계정을 모두 없앱니다 — 거부했습니다.")
            )
        db.update_permission_group(
            conn, name,
            label=None if group["is_builtin"] else label,
            permissions=perms,
        )
    audit.log(request, "권한 그룹 편집: %s", name)
    return _groups_redirect(
        notice=t(request, "권한 그룹 '{name}' 을(를) 저장했습니다.", name=name)
    )


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
    """시스템 API 키 목록 + 발급 (관리자 전용 — 라우터 의존성이 보장).

    사용자 귀속 확장 토큰(owner 보유)은 여기 노출하지 않는다 — 본인이
    계정 설정에서 관리한다. 시스템 키(owner=NULL)만 보여준다.
    """
    with db.connect() as conn:
        keys = db.list_system_api_keys(conn)
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
            owner_user_id=None,  # 시스템 키 — 사용자 귀속 아님(공동관리)
        )
    perms = ", ".join(
        label for flag, label in ((can_view, "보기"), (can_archive, "아카이브"))
        if flag
    )
    audit.log(request, "API 키 발급: '%s' (권한: %s)", name, perms)
    return _api_keys_redirect(
        notice=t(request,
                 "'{name}' 키를 발급했습니다 — 아래 키를 지금 복사하세요. 다시 표시되지 않습니다.",
                 name=name),
        new_key=token,
    )


@router.post("/api-keys/{key_id}/delete")
def api_keys_delete(request: Request, key_id: int):
    """시스템 API 키 폐기 — 즉시 무효화된다.

    사용자 귀속 확장 토큰(owner 보유)은 이 화면에서 폐기할 수 없다 —
    본인이 계정 설정에서만 폐기한다(시스템 키만 대상).
    """
    with db.connect() as conn:
        key = db.get_api_key(conn, key_id)
        if key is None or key["owner_user_id"] is not None:
            raise HTTPException(404, t(request, "API 키 없음"))
        db.delete_api_key(conn, key_id)
    audit.log(
        request, "API 키 폐기: %s", f"'{key['name']}'" if key else f"#{key_id}"
    )
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
    audit.log(
        request, "아카이브 가져오기 [%s]: 페이지 +%d, 스냅샷 +%d",
        mode, result.pages_added, result.snapshots_added,
    )
    return _system_redirect(
        notice=t(
            request,
            "가져오기 완료 [{mode}]: 페이지 +{pages}, 스냅샷 +{snapshots} "
            "(스킵 {skipped}), 확인 기록 +{checks}, 크롤 +{crawls}, "
            "인증서 +{certs}, 로그 +{logs}",
            mode=mode, pages=result.pages_added, snapshots=result.snapshots_added,
            skipped=result.snapshots_skipped, checks=result.checks_added,
            crawls=result.crawls_added, certs=result.certificates_added,
            logs=result.logs_added,
        )
    )
