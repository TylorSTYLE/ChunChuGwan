"""인증 라우트 — 로그인 / 가입 / 로그아웃 / 2FA(TOTP·패스키)."""

from __future__ import annotations

import hmac
import logging
import secrets
import smtplib
import sqlite3
import zoneinfo
from urllib.parse import quote

import httpx
import jwt
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)

from .. import auth, config, db, mailer, oidc
from . import audit, i18n, permissions
from .i18n import t
from .templating import templates

# (지역명, [(IANA 이름, 표시 이름), ...]) — 계정 설정 선택기에 사용
TIMEZONE_GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    ("아시아", [
        ("Asia/Seoul", "한국 (UTC+9)"),
        ("Asia/Tokyo", "일본 (UTC+9)"),
        ("Asia/Shanghai", "중국 (UTC+8)"),
        ("Asia/Hong_Kong", "홍콩 (UTC+8)"),
        ("Asia/Singapore", "싱가포르 (UTC+8)"),
        ("Asia/Bangkok", "태국 (UTC+7)"),
        ("Asia/Jakarta", "인도네시아 서부 (UTC+7)"),
        ("Asia/Kolkata", "인도 (UTC+5:30)"),
        ("Asia/Karachi", "파키스탄 (UTC+5)"),
        ("Asia/Dubai", "UAE (UTC+4)"),
        ("Asia/Tehran", "이란 (UTC+3:30)"),
        ("Asia/Baghdad", "이라크 (UTC+3)"),
        ("Asia/Istanbul", "튀르키예 (UTC+3)"),
    ]),
    ("유럽", [
        ("Europe/London", "영국 (UTC+0/+1)"),
        ("Europe/Paris", "프랑스 (UTC+1/+2)"),
        ("Europe/Berlin", "독일 (UTC+1/+2)"),
        ("Europe/Rome", "이탈리아 (UTC+1/+2)"),
        ("Europe/Madrid", "스페인 (UTC+1/+2)"),
        ("Europe/Amsterdam", "네덜란드 (UTC+1/+2)"),
        ("Europe/Warsaw", "폴란드 (UTC+1/+2)"),
        ("Europe/Helsinki", "핀란드 (UTC+2/+3)"),
        ("Europe/Moscow", "러시아 모스크바 (UTC+3)"),
    ]),
    ("아메리카", [
        ("America/New_York", "미국 동부 (UTC-5/-4)"),
        ("America/Chicago", "미국 중부 (UTC-6/-5)"),
        ("America/Denver", "미국 산악 (UTC-7/-6)"),
        ("America/Los_Angeles", "미국 서부 (UTC-8/-7)"),
        ("America/Toronto", "캐나다 동부 (UTC-5/-4)"),
        ("America/Vancouver", "캐나다 서부 (UTC-8/-7)"),
        ("America/Sao_Paulo", "브라질 (UTC-3/-2)"),
        ("America/Mexico_City", "멕시코 (UTC-6/-5)"),
    ]),
    ("태평양·오세아니아", [
        ("Australia/Sydney", "호주 동부 (UTC+10/+11)"),
        ("Australia/Adelaide", "호주 중부 (UTC+9:30/+10:30)"),
        ("Australia/Perth", "호주 서부 (UTC+8)"),
        ("Pacific/Auckland", "뉴질랜드 (UTC+12/+13)"),
        ("Pacific/Honolulu", "하와이 (UTC-10)"),
        ("Pacific/Guam", "괌 (UTC+10)"),
    ]),
    ("아프리카·중동", [
        ("Africa/Cairo", "이집트 (UTC+2)"),
        ("Africa/Johannesburg", "남아프리카 (UTC+2)"),
        ("Africa/Lagos", "나이지리아 (UTC+1)"),
        ("Africa/Nairobi", "케냐 (UTC+3)"),
    ]),
    ("UTC", [
        ("UTC", "UTC (협정 세계시)"),
    ]),
]

_VALID_TIMEZONES: frozenset[str] = frozenset(
    tz for _, group in TIMEZONE_GROUPS for tz, _ in group
)

logger = logging.getLogger(__name__)

router = APIRouter()


def safe_next(next_url: str | None) -> str:
    """리다이렉트 대상 검증 — 사이트 내부 경로만 허용 (open redirect 방지)."""
    if next_url and next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    return "/"


def set_session_cookie(
    response: Response, token: str, max_age: int | None = None
) -> None:
    """세션 쿠키 설정 (HttpOnly + SameSite=Lax, https 환경이면 Secure)."""
    response.set_cookie(
        config.SESSION_COOKIE,
        token,
        max_age=max_age or config.SESSION_TTL_DAYS * 86400,
        httponly=True,
        samesite="lax",
        secure=config.COOKIE_SECURE,
    )


def _login_redirect(token: str, next_url: str | None) -> RedirectResponse:
    """로그인 성공 — 세션 쿠키를 심고 원래 가려던 곳으로."""
    res = RedirectResponse(url=safe_next(next_url), status_code=303)
    set_session_cookie(res, token)
    return res


# ---- 이메일 본인 인증 ----
# 켜져 있고 SMTP 가 설정된 경우, 패스워드 계정은 메일로 받은 코드로 이메일을
# 검증해야 한다 (SSO 계정은 IdP 가 검증하므로 제외). 인증 전에는 로그인이
# pending_email_verify 세션에 머무르고, 코드 확인 시 active 로 승격된다.


def _email_verification_required(conn: sqlite3.Connection, user) -> bool:
    """이 사용자가 로그인 마무리 전에 이메일 인증을 거쳐야 하는지."""
    return (
        user["password_hash"] is not None  # SSO 계정은 IdP 신뢰
        and not user["email_verified"]
        and db.email_verification_enabled(conn)
        and mailer.mail_enabled(conn)
    )


def _issue_and_send_code(conn: sqlite3.Connection, user) -> bool:
    """인증 코드를 발급·저장하고 메일로 보낸다. 발송 실패해도 코드는 저장된다.

    반환값은 메일 발송 성공 여부 (화면 안내용 — 실패해도 사용자는 코드를
    재요청할 수 있다).
    """
    code = auth.generate_email_code()
    ttl = db.email_verification_ttl_minutes(conn) * 60
    db.create_email_verification(conn, user["id"], auth.hash_token(code), ttl)
    smtp = mailer.resolve_config(conn)
    if not smtp.enabled:
        return False
    try:
        mailer.send_verification_code(smtp, user["email"], code, ttl // 60)
        return True
    except (smtplib.SMTPException, OSError) as e:
        logger.warning("이메일 인증 코드 발송 실패 (%s): %s", user["email"], e)
        return False


def _post_password_login(
    conn: sqlite3.Connection, user, next_url: str | None
) -> RedirectResponse:
    """2FA 가 없는 패스워드 로그인 마무리 — 이메일 인증이 필요하면 그 화면으로."""
    if _email_verification_required(conn, user):
        _issue_and_send_code(conn, user)
        ttl = db.email_verification_ttl_minutes(conn) * 60
        token = auth.issue_session(
            conn, user["id"], state="pending_email_verify", ttl_seconds=ttl
        )
        res = RedirectResponse(
            url=f"/verify-email?next={quote(safe_next(next_url), safe='')}",
            status_code=303,
        )
        set_session_cookie(res, token, max_age=ttl)
        return res
    token = auth.issue_session(conn, user["id"])
    return _login_redirect(token, next_url)


def _two_factor_target(
    conn: sqlite3.Connection, token_hash: str, user, next_url: str | None
) -> str:
    """2FA 통과 후 세션을 마무리하고 이동할 경로 — 이메일 인증 필요 시 그 화면.

    세션 row 자체를 갱신한다 (active 승격 또는 pending_email_verify 전환). 쿠키는
    호출부가 그대로 다시 심는다 (서버사이드 만료가 실질 기준).
    """
    if _email_verification_required(conn, user):
        _issue_and_send_code(conn, user)
        ttl = db.email_verification_ttl_minutes(conn) * 60
        db.set_session_state(conn, token_hash, "pending_email_verify", ttl)
        return f"/verify-email?next={quote(safe_next(next_url), safe='')}"
    db.activate_session(conn, token_hash, config.SESSION_TTL_DAYS * 86400)
    return safe_next(next_url)


# ---- 최초 구동 관리자 등록 ----


@router.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request):
    """관리자 등록 페이지 — 사용자가 1명이라도 있으면 절대 표시하지 않는다."""
    with db.connect() as conn:
        if db.count_users(conn) > 0:
            return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse(
        request, "setup.html", {"error": None, "email": ""}
    )


@router.post("/setup", response_class=HTMLResponse)
def setup(request: Request, email: str = Form(...), password: str = Form(...)):
    """최초 구동 관리자 등록. users 가 비어 있지 않으면 INSERT 자체가 거부된다."""
    email = email.strip()
    error = auth.validate_credentials(email, password)
    if error is None:
        with db.connect() as conn:
            user_id = db.create_first_admin(conn, email, auth.hash_password(password))
            if user_id is None:
                # 관리자가 이미 등록됨 — 이 API 로는 더 이상 계정을 만들 수 없다
                raise HTTPException(403, t(request, "이미 관리자가 등록되어 있습니다"))
            token = auth.issue_session(conn, user_id)
        return _login_redirect(token, "/")
    return templates.TemplateResponse(
        request, "setup.html", {"error": t(request, error), "email": email},
        status_code=400,
    )


def _login_ctx(
    conn: sqlite3.Connection, next_url: str | None, email: str = "",
    error: str | None = None,
) -> dict:
    """로그인 화면 템플릿 컨텍스트 (가입 링크 노출 여부 포함)."""
    return {
        "next": safe_next(next_url), "error": error, "email": email,
        "oidc_enabled": config.oidc_enabled(),
        "signup_enabled": db.signup_enabled(conn),
    }


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str | None = None):
    if getattr(request.state, "user", None) is not None:
        return RedirectResponse(url="/", status_code=302)
    with db.connect() as conn:
        ctx = _login_ctx(conn, next)
    return templates.TemplateResponse(request, "login.html", ctx)


@router.post("/login", response_class=HTMLResponse)
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
):
    with db.connect() as conn:
        user = db.get_user_by_email(conn, email.strip())
        ok = (
            user is not None
            and user["password_hash"] is not None
            and auth.verify_password(user["password_hash"], password)
        )
        if not ok:
            return templates.TemplateResponse(
                request, "login.html",
                _login_ctx(conn, next, email,
                           t(request, "이메일 또는 패스워드가 올바르지 않습니다.")),
                status_code=401,
            )
        if user["role"] == "blocked":
            return templates.TemplateResponse(
                request, "login.html",
                _login_ctx(conn, next, email,
                           t(request, "차단된 계정입니다. 관리자에게 문의하세요.")),
                status_code=403,
            )
        if user["role"] == "withdrawn":
            return templates.TemplateResponse(
                request, "login.html",
                _login_ctx(conn, next, email, t(request, "탈퇴한 계정입니다.")),
                status_code=403,
            )
        if user["totp_secret"] is not None or db.count_passkeys(conn, user["id"]) > 0:
            # 2단계: TOTP/패스키 확인 전까지는 pending 세션 (짧은 수명)
            token = auth.issue_session(
                conn, user["id"], state="pending_totp",
                ttl_seconds=config.PENDING_TOTP_TTL_SECONDS,
            )
            res = RedirectResponse(
                url=f"/login/totp?next={safe_next(next)}", status_code=303
            )
            set_session_cookie(res, token, max_age=config.PENDING_TOTP_TTL_SECONDS)
            return res
        return _post_password_login(conn, user, next)


# ---- 2단계 로그인 (TOTP / 패스키) ----


def _pending_session(request: Request):
    """미들웨어가 적재한 pending_totp 세션 (없으면 None)."""
    sess = getattr(request.state, "session", None)
    if sess is not None and sess["state"] == "pending_totp":
        return sess
    return None


def _second_factor_ctx(conn: sqlite3.Connection, user_id: int, next_url: str | None) -> dict:
    """2단계 인증 페이지 템플릿 컨텍스트 (사용 가능한 수단 플래그 포함)."""
    user = db.get_user_by_id(conn, user_id)
    return {
        "next": safe_next(next_url),
        "error": None,
        "has_totp": user["totp_secret"] is not None,
        "has_passkey": db.count_passkeys(conn, user_id) > 0,
    }


@router.get("/login/totp", response_class=HTMLResponse)
def totp_page(request: Request, next: str | None = None):
    sess = _pending_session(request)
    if sess is None:
        return RedirectResponse(url="/login", status_code=302)
    with db.connect() as conn:
        ctx = _second_factor_ctx(conn, sess["user_id"], next)
    return templates.TemplateResponse(request, "totp.html", ctx)


@router.post("/login/totp", response_class=HTMLResponse)
def totp_login(request: Request, code: str = Form(...), next: str = Form("/")):
    sess = _pending_session(request)
    if sess is None:
        return RedirectResponse(url="/login", status_code=302)
    with db.connect() as conn:
        user = db.get_user_by_id(conn, sess["user_id"])
        window = user["totp_secret"] is not None and auth.verify_totp(
            user["totp_secret"], code, user["totp_last_used_at"]
        )
        if not window:
            ctx = _second_factor_ctx(conn, sess["user_id"], next)
            ctx["error"] = t(request, "코드가 올바르지 않습니다.")
            return templates.TemplateResponse(
                request, "totp.html", ctx, status_code=401
            )
        db.set_totp_last_used(conn, user["id"], window)
        target = _two_factor_target(conn, sess["token_hash"], user, next)
    res = RedirectResponse(url=target, status_code=303)
    set_session_cookie(res, request.cookies[config.SESSION_COOKIE])
    return res


@router.post("/login/passkey/options")
def passkey_login_options(request: Request):
    """패스키 2단계 인증 옵션 발급 (pending 세션 전용)."""
    sess = _pending_session(request)
    if sess is None:
        raise HTTPException(401, t(request, "패스워드 인증이 필요합니다"))
    with db.connect() as conn:
        creds = db.list_passkeys(conn, sess["user_id"])
        if not creds:
            raise HTTPException(400, t(request, "등록된 패스키가 없습니다"))
        options_json, challenge = auth.passkey_authentication_options(
            [c["credential_id"] for c in creds]
        )
        db.set_session_challenge(conn, sess["token_hash"], challenge)
    return Response(content=options_json, media_type="application/json")


@router.post("/login/passkey")
async def passkey_login(request: Request):
    """패스키 2단계 인증 응답 검증 → 세션 활성화."""
    sess = _pending_session(request)
    if sess is None:
        raise HTTPException(401, t(request, "패스워드 인증이 필요합니다"))
    body = await request.json()
    credential = body.get("credential")
    if not isinstance(credential, dict):
        raise HTTPException(400, t(request, "credential 누락"))
    with db.connect() as conn:
        challenge = db.consume_session_challenge(conn, sess["token_hash"])
        if challenge is None:
            raise HTTPException(400, t(request, "진행 중인 인증이 없습니다 — 다시 시도하세요"))
        cred = db.get_passkey(conn, sess["user_id"], str(credential.get("id", "")))
        if cred is None:
            raise HTTPException(401, t(request, "등록되지 않은 패스키입니다"))
        new_count = auth.verify_passkey_authentication(
            credential, challenge, cred["public_key"], cred["sign_count"]
        )
        if new_count is None:
            raise HTTPException(401, t(request, "패스키 인증에 실패했습니다"))
        db.touch_passkey(conn, cred["id"], new_count)
        user = db.get_user_by_id(conn, sess["user_id"])
        target = _two_factor_target(conn, sess["token_hash"], user, body.get("next"))
    res = JSONResponse({"ok": True, "next": target})
    set_session_cookie(res, request.cookies[config.SESSION_COOKIE])
    return res


# ---- TOTP 설정 (등록/해제) ----


@router.get("/settings/totp", response_class=HTMLResponse)
def totp_setup_page(request: Request):
    user = request.state.user
    ctx: dict = {"enabled": user["totp_secret"] is not None,
                 "has_password": user["password_hash"] is not None,
                 "error": None, "qr": None, "secret": None}
    if not ctx["enabled"] and ctx["has_password"]:
        secret = auth.new_totp_secret()
        with db.connect() as conn:
            db.set_totp_pending(conn, user["id"], secret)
        ctx["secret"] = secret
        ctx["qr"] = auth.qr_data_uri(
            auth.totp_provisioning_uri(secret, user["email"])
        )
    return templates.TemplateResponse(request, "totp_setup.html", ctx)


@router.post("/settings/totp", response_class=HTMLResponse)
def totp_confirm(request: Request, code: str = Form(...)):
    user = request.state.user
    with db.connect() as conn:
        fresh = db.get_user_by_id(conn, user["id"])
        pending = fresh["totp_pending_secret"]
        window = pending and auth.verify_totp(pending, code, None)
        if not window:
            ctx = {"enabled": False, "has_password": True,
                   "error": t(request, "코드가 올바르지 않습니다. QR을 다시 스캔 후 시도하세요."),
                   "secret": pending,
                   "qr": pending and auth.qr_data_uri(
                       auth.totp_provisioning_uri(pending, user["email"]))}
            return templates.TemplateResponse(
                request, "totp_setup.html", ctx, status_code=400
            )
        db.confirm_totp(conn, user["id"])
        db.set_totp_last_used(conn, user["id"], window)
    return RedirectResponse(url="/settings/totp", status_code=303)


@router.post("/settings/totp/disable", response_class=HTMLResponse)
def totp_disable(request: Request, password: str = Form(...)):
    user = request.state.user
    with db.connect() as conn:
        if user["password_hash"] is None or not auth.verify_password(
            user["password_hash"], password
        ):
            return templates.TemplateResponse(
                request, "totp_setup.html",
                {"enabled": True, "has_password": True,
                 "error": t(request, "패스워드가 올바르지 않습니다."),
                 "qr": None, "secret": None},
                status_code=401,
            )
        db.disable_totp(conn, user["id"])
    return RedirectResponse(url="/settings/totp", status_code=303)


# ---- 패스키 설정 (등록/삭제) ----
# 패스키는 TOTP 와 동일하게 패스워드 로그인의 2단계로만 쓴다 (SSO 는 IdP 2FA 신뢰).


def _passkey_setup_ctx(conn: sqlite3.Connection, user) -> dict:
    return {
        "creds": db.list_passkeys(conn, user["id"]),
        "has_password": user["password_hash"] is not None,
        "error": None,
    }


@router.get("/settings/passkey", response_class=HTMLResponse)
def passkey_setup_page(request: Request):
    user = request.state.user
    with db.connect() as conn:
        ctx = _passkey_setup_ctx(conn, user)
    return templates.TemplateResponse(request, "passkey_setup.html", ctx)


@router.post("/settings/passkey/options")
def passkey_register_options(request: Request):
    """패스키 등록 옵션 발급. 이미 등록된 자격증명은 제외 목록으로 전달."""
    user = request.state.user
    if user["password_hash"] is None:
        raise HTTPException(400, t(request, "SSO 전용 계정은 패스키를 등록할 수 없습니다"))
    with db.connect() as conn:
        creds = db.list_passkeys(conn, user["id"])
        options_json, challenge = auth.passkey_registration_options(
            user["id"], user["email"], [c["credential_id"] for c in creds]
        )
        db.set_session_challenge(
            conn, request.state.session["token_hash"], challenge
        )
    return Response(content=options_json, media_type="application/json")


@router.post("/settings/passkey/register")
async def passkey_register(request: Request):
    """패스키 등록 응답 검증 → 저장."""
    user = request.state.user
    if user["password_hash"] is None:
        raise HTTPException(400, t(request, "SSO 전용 계정은 패스키를 등록할 수 없습니다"))
    body = await request.json()
    credential = body.get("credential")
    if not isinstance(credential, dict):
        raise HTTPException(400, t(request, "credential 누락"))
    name = (str(body.get("name") or "").strip() or t(request, "패스키"))[:64]
    with db.connect() as conn:
        challenge = db.consume_session_challenge(
            conn, request.state.session["token_hash"]
        )
        if challenge is None:
            raise HTTPException(400, t(request, "진행 중인 등록이 없습니다 — 다시 시도하세요"))
        verified = auth.verify_passkey_registration(credential, challenge)
        if verified is None:
            raise HTTPException(400, t(request, "패스키 등록 검증에 실패했습니다"))
        try:
            db.create_passkey(conn, user["id"], name=name, **verified)
        except sqlite3.IntegrityError:
            raise HTTPException(400, t(request, "이미 등록된 패스키입니다"))
    return {"ok": True}


@router.post("/settings/passkey/{passkey_id}/delete", response_class=HTMLResponse)
def passkey_delete(request: Request, passkey_id: int, password: str = Form(...)):
    """패스키 삭제 — 세션 탈취로 2FA 를 무력화하지 못하도록 패스워드 재확인."""
    user = request.state.user
    with db.connect() as conn:
        if user["password_hash"] is None or not auth.verify_password(
            user["password_hash"], password
        ):
            ctx = _passkey_setup_ctx(conn, user)
            ctx["error"] = t(request, "패스워드가 올바르지 않습니다.")
            return templates.TemplateResponse(
                request, "passkey_setup.html", ctx, status_code=401
            )
        if not db.delete_passkey(conn, user["id"], passkey_id):
            raise HTTPException(404, t(request, "패스키 없음"))
    return RedirectResponse(url="/settings/passkey", status_code=303)


# ---- 계정 설정 (이름/패스워드 변경) ----

# 확장 토큰 만료 옵션 — system_routes 의 API 키 만료 정책과 동일하게 유지한다
# (라우터 간 결합을 피하려 값만 복제, 변경 시 양쪽 같이). 값은 ttl 초, None=영구.
EXTENSION_TOKEN_EXPIRY_OPTIONS = [
    ("permanent", "영구"),
    ("1d", "1일"),
    ("1m", "1개월 (30일)"),
    ("1y", "1년 (365일)"),
    ("custom", "사용자 지정 (일)"),
]
_EXTENSION_TOKEN_TTL_SECONDS: dict[str, int | None] = {
    "permanent": None,
    "1d": 86400,
    "1m": 30 * 86400,
    "1y": 365 * 86400,
}
MAX_EXTENSION_TOKEN_CUSTOM_DAYS = 3650  # 10년 — 그 이상은 영구를 쓴다


def _extension_token_ttl(
    request: Request, expiry: str, custom_days: int
) -> int | None:
    """확장 토큰 만료 선택지를 ttl 초로 변환 (None=영구). 잘못된 입력은 ValueError."""
    if expiry in _EXTENSION_TOKEN_TTL_SECONDS:
        return _EXTENSION_TOKEN_TTL_SECONDS[expiry]
    if expiry == "custom":
        if not (1 <= custom_days <= MAX_EXTENSION_TOKEN_CUSTOM_DAYS):
            raise ValueError(t(
                request, "사용자 지정 만료는 1 ~ {n}일 사이여야 합니다.",
                n=MAX_EXTENSION_TOKEN_CUSTOM_DAYS,
            ))
        return custom_days * 86400
    raise ValueError(t(request, "알 수 없는 만료 선택: {expiry}", expiry=repr(expiry)))


def _account_ctx(
    user, *, error: str | None = None, notice: str | None = None,
) -> dict:
    with db.connect() as conn:
        passkey_count = db.count_passkeys(conn, user["id"])
        # 이메일 인증 — 기능이 켜져 있고 SMTP 가 설정됐을 때만 의미가 있다
        email_verification_on = (
            db.email_verification_enabled(conn) and mailer.mail_enabled(conn)
        )
        role_label = db.role_labels(conn).get(user["role"], user["role"])
    return {
        "display_name": user["display_name"] or "",
        "has_password": user["password_hash"] is not None,
        "email": user["email"],
        "role": user["role"],
        "role_label": role_label,
        "totp_enabled": user["totp_secret"] is not None,
        "passkey_count": passkey_count,
        "email_verified": bool(user["email_verified"]),
        "email_verification_on": email_verification_on,
        "timezone": user["timezone"] or "UTC",
        "timezone_groups": TIMEZONE_GROUPS,
        "locale": user["locale"] or i18n.DEFAULT_LOCALE,
        "locales": i18n.SUPPORTED_LOCALES,
        "locale_names": i18n.LOCALE_NAMES,
        "error": error,
        "notice": notice,
    }


def _api_keys_ctx(
    user, *, error: str | None = None, notice: str | None = None,
    new_token: str = "",
) -> dict:
    """개인 API Key(확장 토큰) 화면 컨텍스트 — 본인 토큰 목록, 1회 노출, 파생 권한."""
    can_view, can_archive = permissions.token_permissions_for_user(user)
    with db.connect() as conn:
        tokens = db.list_api_keys_for_owner(conn, user["id"])
    return {
        "ext_tokens": tokens,
        "new_token": new_token,
        "ext_can_view": can_view,
        "ext_can_archive": can_archive,
        "ext_expiry_options": EXTENSION_TOKEN_EXPIRY_OPTIONS,
        "ext_max_custom_days": MAX_EXTENSION_TOKEN_CUSTOM_DAYS,
        "error": error,
        "notice": notice,
    }


@router.get("/settings/account", response_class=HTMLResponse)
def account_page(request: Request, ok: str | None = None):
    user = request.state.user
    notice = {
        "name": "사용자 이름을 변경했습니다.",
        "password": "패스워드를 변경했습니다. 다른 기기의 세션은 로그아웃되었습니다.",
        "timezone": "시간대를 변경했습니다.",
        "language": "언어를 변경했습니다.",
        "email_verified": "이메일 본인 인증을 완료했습니다.",
    }.get(ok or "")
    if notice:
        notice = t(request, notice)
    return templates.TemplateResponse(
        request, "account.html", _account_ctx(user, notice=notice),
    )


@router.get("/settings/api-keys", response_class=HTMLResponse)
def api_keys_page(request: Request, ok: str | None = None, new_token: str = ""):
    """개인 API Key(확장 토큰) 관리 화면 — 발급/폐기. 본인 토큰만 노출."""
    user = request.state.user
    notice = {
        "token_issued": "개인 API Key 를 발급했습니다 — 아래 키를 지금 복사하세요. 다시 표시되지 않습니다.",
        "token_revoked": "개인 API Key 를 폐기했습니다.",
    }.get(ok or "")
    if notice:
        notice = t(request, notice)
    return templates.TemplateResponse(
        request, "personal_api_keys.html",
        _api_keys_ctx(user, notice=notice, new_token=new_token),
    )


@router.post("/settings/account/language", response_class=HTMLResponse)
def change_language(request: Request, language: str = Form(...)):
    user = request.state.user
    lang = language.strip()
    if lang not in i18n.SUPPORTED_LOCALES:
        ctx = _account_ctx(user, error=t(request, "지원하지 않는 언어입니다."))
        return templates.TemplateResponse(request, "account.html", ctx, status_code=400)
    with db.connect() as conn:
        db.set_user_locale(conn, user["id"], lang)
    return RedirectResponse(url="/settings/account?ok=language", status_code=303)


@router.post("/settings/account/timezone", response_class=HTMLResponse)
def change_timezone(request: Request, timezone: str = Form(...)):
    user = request.state.user
    tz = timezone.strip()
    if tz not in _VALID_TIMEZONES:
        ctx = _account_ctx(user, error=t(request, "지원하지 않는 타임존입니다."))
        return templates.TemplateResponse(request, "account.html", ctx, status_code=400)
    with db.connect() as conn:
        db.set_user_timezone(conn, user["id"], tz)
    return RedirectResponse(url="/settings/account?ok=timezone", status_code=303)


@router.post("/settings/account/name", response_class=HTMLResponse)
def change_display_name(request: Request, display_name: str = Form("")):
    user = request.state.user
    name = display_name.strip() or None  # 빈 입력 = 이름 제거 (이메일 표시로 복귀)
    if name is not None:
        error = auth.validate_display_name(name)
        if error is not None:
            ctx = _account_ctx(user, error=t(request, error))
            ctx["display_name"] = display_name
            return templates.TemplateResponse(
                request, "account.html", ctx, status_code=400
            )
    with db.connect() as conn:
        db.set_display_name(conn, user["id"], name)
    return RedirectResponse(url="/settings/account?ok=name", status_code=303)


@router.post("/settings/account/password", response_class=HTMLResponse)
def change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    new_password2: str = Form(...),
):
    user = request.state.user
    if user["password_hash"] is None:
        error = "SSO 전용 계정은 패스워드가 없습니다. IdP(Authentik)에서 관리하세요."
        status = 400
    elif not auth.verify_password(user["password_hash"], current_password):
        error = "현재 패스워드가 올바르지 않습니다."
        status = 401
    elif new_password != new_password2:
        error = "새 패스워드가 서로 일치하지 않습니다."
        status = 400
    else:
        error = auth.validate_password(new_password)
        status = 400
    if error is not None:
        return templates.TemplateResponse(
            request, "account.html", _account_ctx(user, error=t(request, error)),
            status_code=status,
        )
    with db.connect() as conn:
        db.set_password_hash(conn, user["id"], auth.hash_password(new_password))
        # 탈취된 세션을 끊을 수 있도록 현재 세션만 남기고 모두 무효화
        db.delete_other_sessions(
            conn, user["id"], request.state.session["token_hash"]
        )
    return RedirectResponse(url="/settings/account?ok=password", status_code=303)


@router.post("/settings/api-keys", response_class=HTMLResponse)
def create_extension_token(
    request: Request,
    name: str = Form(...),
    can_view: bool = Form(False),
    can_archive: bool = Form(False),
    expiry: str = Form("permanent"),
    custom_days: int = Form(0),
):
    """본인 귀속 개인 API Key(확장 토큰) 발급 — 권한은 역할 범위 안에서 선택, 원문은 1회만 노출.

    세션 인증 + 같은 출처 폼 POST 라 CSRF Origin 검사를 정상 통과한다.
    """
    user = request.state.user
    allowed_view, allowed_archive = permissions.token_permissions_for_user(user)
    if not (allowed_view or allowed_archive):
        ctx = _api_keys_ctx(
            user, error=t(request, "현재 권한으로는 API Key 를 발급할 수 없습니다.")
        )
        return templates.TemplateResponse(
            request, "personal_api_keys.html", ctx, status_code=403
        )
    # 선택한 권한을 역할이 허용하는 범위로 클램프 — 권한 상승 방지
    can_view = can_view and allowed_view
    can_archive = can_archive and allowed_archive
    if not (can_view or can_archive):
        ctx = _api_keys_ctx(
            user, error=t(request, "권한을 하나 이상 선택하세요.")
        )
        return templates.TemplateResponse(
            request, "personal_api_keys.html", ctx, status_code=400
        )
    name = name.strip()
    name_error = auth.validate_api_key_name(name)
    if name_error is not None:
        ctx = _api_keys_ctx(user, error=t(request, name_error))
        return templates.TemplateResponse(
            request, "personal_api_keys.html", ctx, status_code=400
        )
    try:
        ttl_seconds = _extension_token_ttl(request, expiry, custom_days)
    except ValueError as e:
        ctx = _api_keys_ctx(user, error=str(e))
        return templates.TemplateResponse(
            request, "personal_api_keys.html", ctx, status_code=400
        )
    with db.connect() as conn:
        token = auth.issue_api_key(
            conn, name,
            can_view=can_view, can_archive=can_archive,
            created_by=user["id"], owner_user_id=user["id"],
            ttl_seconds=ttl_seconds,
        )
    perms = ", ".join(
        label for flag, label in ((can_view, "보기"), (can_archive, "아카이브"))
        if flag
    )
    audit.log(request, "개인 API Key 발급: '%s' (권한: %s)", name, perms)
    return RedirectResponse(
        url=f"/settings/api-keys?ok=token_issued&new_token={quote(token, safe='')}",
        status_code=303,
    )


@router.post(
    "/settings/api-keys/{token_id}/delete",
    response_class=HTMLResponse,
)
def delete_extension_token(request: Request, token_id: int):
    """본인 귀속 개인 API Key(확장 토큰) 폐기 — 즉시 무효화. 본인 토큰만(IDOR 방어)."""
    user = request.state.user
    with db.connect() as conn:
        key = db.get_api_key(conn, token_id)
        # 본인 소유 토큰만 — 타인 토큰·시스템 키(owner=NULL)는 404 로 은폐
        if key is None or key["owner_user_id"] != user["id"]:
            raise HTTPException(404, t(request, "API Key 없음"))
        db.delete_api_key(conn, token_id)
    audit.log(request, "개인 API Key 폐기: '%s'", key["name"])
    return RedirectResponse(url="/settings/api-keys?ok=token_revoked", status_code=303)


@router.post("/settings/account/withdraw", response_class=HTMLResponse)
def withdraw_account(
    request: Request, password: str = Form(""), confirm: str = Form("")
):
    """본인 계정 탈퇴 (관리자 불가). 세션 탈취 방어를 위해 재확인을 요구한다.

    패스워드 계정은 패스워드 재입력, SSO 전용 계정은 이메일 입력으로 확인한다.
    탈퇴는 권한을 탈퇴 상태로 바꾸고 전 세션을 무효화한다 — 계정 정보는
    남아 재가입이 막히며, 삭제는 관리자가 사용자 관리에서 수행한다.
    """
    user = request.state.user
    if user["role"] == "admin":
        return templates.TemplateResponse(
            request, "account.html",
            _account_ctx(user, error=t(request, "관리자 계정은 탈퇴할 수 없습니다.")),
            status_code=403,
        )
    if user["password_hash"] is not None:
        if not auth.verify_password(user["password_hash"], password):
            return templates.TemplateResponse(
                request, "account.html",
                _account_ctx(user, error=t(request, "패스워드가 올바르지 않습니다.")),
                status_code=401,
            )
    elif confirm.strip().lower() != user["email"].lower():
        return templates.TemplateResponse(
            request, "account.html",
            _account_ctx(user, error=t(request, "확인 이메일이 일치하지 않습니다.")),
            status_code=400,
        )
    with db.connect() as conn:
        db.withdraw_user(conn, user["id"])
    res = RedirectResponse(url="/login", status_code=303)
    res.delete_cookie(config.SESSION_COOKIE)
    return res


# ---- OIDC (Authentik) SSO ----


def _require_oidc(request: Request) -> None:
    if not config.oidc_enabled():
        raise HTTPException(404, t(request, "OIDC 가 설정되지 않았습니다"))


@router.get("/auth/oidc/login")
def oidc_login(request: Request, next: str | None = None):
    _require_oidc(request)
    state = secrets.token_urlsafe(16)
    nonce = secrets.token_urlsafe(16)
    with db.connect() as conn:
        db.create_oidc_state(conn, state, nonce, safe_next(next))
    return RedirectResponse(url=oidc.build_authorize_url(state, nonce), status_code=302)


def _link_oidc_user(request: Request, conn: sqlite3.Connection, claims: dict) -> int:
    """OIDC 클레임을 로컬 계정에 연결하고 user_id 반환.

    ① (provider, sub) 기존 연결 → 그 계정.
    ② 검증된 이메일이 기존 계정과 일치 → identity 연결.
    ③ 둘 다 없으면 SSO 전용 계정 자동 프로비저닝 (password_hash NULL).
       초기 권한은 회원 가입과 같은 설정값(signup_default_role)을 따른다 —
       SSO 가 가입 승인 절차를 우회하는 경로가 되지 않게 한다.
    """
    sub = str(claims["sub"])
    ident = db.get_identity(conn, config.OIDC_PROVIDER, sub)
    if ident is not None:
        return ident["user_id"]

    email = (claims.get("email") or "").strip()
    if not email:
        raise HTTPException(400, t(request, "OIDC 응답에 이메일 클레임이 없습니다"))

    existing = db.get_user_by_email(conn, email)
    if existing is not None:
        if not claims.get("email_verified"):
            # 미검증 이메일로 기존 계정을 탈취하는 것을 차단
            raise HTTPException(
                403, t(request, "IdP 가 검증하지 않은 이메일이라 기존 계정에 연결할 수 없습니다")
            )
        db.create_identity(conn, existing["id"], config.OIDC_PROVIDER, sub)
        return existing["id"]

    user_id = db.create_user(conn, email, role=db.signup_default_role(conn))  # SSO 전용
    db.create_identity(conn, user_id, config.OIDC_PROVIDER, sub)
    return user_id


@router.get("/auth/oidc/callback")
def oidc_callback(
    request: Request,
    code: str | None = None, state: str | None = None, error: str | None = None,
):
    _require_oidc(request)
    if error:
        raise HTTPException(400, t(request, "IdP 오류: {e}", e=error))
    if not code or not state:
        raise HTTPException(400, t(request, "code/state 누락"))

    with db.connect() as conn:
        st = db.consume_oidc_state(conn, state, config.OIDC_STATE_TTL_SECONDS)
    if st is None:
        raise HTTPException(400, t(request, "state 불일치 또는 만료 — 로그인을 다시 시도하세요"))

    try:
        tokens = oidc.exchange_code(code)
        claims = oidc.validate_id_token(tokens["id_token"], st["nonce"])
    except (httpx.HTTPError, jwt.PyJWTError, ValueError, KeyError) as e:
        logger.warning("OIDC 콜백 검증 실패: %s", e)
        raise HTTPException(400, t(request, "OIDC 토큰 검증 실패"))

    with db.connect() as conn:
        user_id = _link_oidc_user(request, conn, claims)
        role = db.get_user_by_id(conn, user_id)["role"]
        if role == "blocked":
            raise HTTPException(403, t(request, "차단된 계정입니다. 관리자에게 문의하세요."))
        if role == "withdrawn":
            raise HTTPException(403, t(request, "탈퇴한 계정입니다."))
        # SSO 는 IdP 의 2FA 를 신뢰 — 바로 active 세션
        token = auth.issue_session(conn, user_id)
    return _login_redirect(token, st["redirect_to"])


@router.get("/signup", response_class=HTMLResponse)
def signup_page(request: Request):
    if getattr(request.state, "user", None) is not None:
        return RedirectResponse(url="/", status_code=302)
    with db.connect() as conn:
        if not db.signup_enabled(conn):
            return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse(
        request, "signup.html", {"error": None, "email": ""}
    )


@router.post("/signup", response_class=HTMLResponse)
def signup(request: Request, email: str = Form(...), password: str = Form(...)):
    email = email.strip()
    error = auth.validate_credentials(email, password)
    if error is None:
        with db.connect() as conn:
            if not db.signup_enabled(conn):
                raise HTTPException(403, t(request, "회원 가입이 비활성화되어 있습니다."))
            if db.get_user_by_email(conn, email) is not None:
                error = "이미 가입된 이메일입니다."
            else:
                user_id = db.create_user(
                    conn, email, auth.hash_password(password),
                    role=db.signup_default_role(conn),
                )
                # 이메일 인증이 켜져 있으면 인증 화면으로, 아니면 바로 로그인
                user = db.get_user_by_id(conn, user_id)
                return _post_password_login(conn, user, "/")
    return templates.TemplateResponse(
        request, "signup.html", {"error": t(request, error), "email": email},
        status_code=400,
    )


@router.get("/pending", response_class=HTMLResponse)
def pending_page(request: Request):
    """가입 승인 대기(권한없음) 안내 — 미들웨어가 pending 계정을 여기로 보낸다."""
    user = getattr(request.state, "user", None)
    if user is None:
        return RedirectResponse(url="/login", status_code=302)
    if user["role"] != "pending":
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse(
        request, "pending.html", {"email": user["email"]}
    )


# ---- 이메일 본인 인증 화면 ----
# 두 경로가 같은 화면을 쓴다: ① 로그인 도중(pending_email_verify 세션 — 아직
# active 사용자가 아님) ② 이미 로그인한 기존 사용자가 개인 설정에서 직접 인증.
# 그래서 /verify-email* 은 공개 경로로 두되 핸들러가 두 상태를 직접 판별한다.


def _verify_target(request: Request, conn: sqlite3.Connection):
    """이 요청의 인증 대상 사용자와 세션을 돌려준다 (없으면 (None, None)).

    active 로그인 사용자(개인 설정 인증)가 우선이고, 없으면 인증 대기 세션을 본다.
    """
    user = getattr(request.state, "user", None)
    if user is not None:
        return user, getattr(request.state, "session", None)
    sess = getattr(request.state, "session", None)
    if sess is not None and sess["state"] == "pending_email_verify":
        return db.get_user_by_id(conn, sess["user_id"]), sess
    return None, None


def _is_pending_verify(sess) -> bool:
    """로그인 도중(인증 대기) 세션인지 — 인증 완료 시 active 로 승격할 대상."""
    return sess is not None and sess["state"] == "pending_email_verify"


def _verify_done_redirect(sess, next_url: str | None) -> str:
    """인증 완료 후 이동 경로 — 로그인 도중이면 원래 목적지, 아니면 계정 설정."""
    if _is_pending_verify(sess):
        return safe_next(next_url)
    return "/settings/account?ok=email_verified"


@router.get("/verify-email", response_class=HTMLResponse)
def verify_email_page(
    request: Request, next: str | None = None, sent: int = 0, error: str = ""
):
    """이메일 인증 화면 — 코드 입력 + 재발송. 로그인 도중·개인 설정 공용."""
    with db.connect() as conn:
        user, sess = _verify_target(request, conn)
        if user is None:
            return RedirectResponse(url="/login", status_code=302)
        if user["email_verified"]:
            return RedirectResponse(
                url=_verify_done_redirect(sess, next), status_code=302
            )
        mail_on = mailer.mail_enabled(conn)
        ttl_minutes = db.email_verification_ttl_minutes(conn)
    notice = t(request, "인증 코드를 메일로 보냈습니다.") if sent else None
    return templates.TemplateResponse(
        request, "verify_email.html",
        {
            "email": user["email"],
            "next": safe_next(next),
            "pending": _is_pending_verify(sess),
            "mail_enabled": mail_on,
            "ttl_minutes": ttl_minutes,
            "notice": notice,
            "error": error or None,
        },
    )


@router.post("/verify-email", response_class=HTMLResponse)
def verify_email(request: Request, code: str = Form(...), next: str = Form("/")):
    """입력한 코드 검증 → 인증 완료. 로그인 도중이면 세션을 active 로 승격한다."""
    with db.connect() as conn:
        user, sess = _verify_target(request, conn)
        if user is None:
            return RedirectResponse(url="/login", status_code=303)
        if not user["email_verified"]:
            record = db.get_email_verification(conn, user["id"])
            ok = record is not None and hmac.compare_digest(
                record["code_hash"], auth.hash_token(code.strip())
            )
            if not ok:
                return templates.TemplateResponse(
                    request, "verify_email.html",
                    {
                        "email": user["email"],
                        "next": safe_next(next),
                        "pending": _is_pending_verify(sess),
                        "mail_enabled": mailer.mail_enabled(conn),
                        "ttl_minutes": db.email_verification_ttl_minutes(conn),
                        "notice": None,
                        "error": t(request, "코드가 올바르지 않거나 만료되었습니다."),
                    },
                    status_code=401,
                )
            db.set_email_verified(conn, user["id"])
            db.delete_email_verification(conn, user["id"])
        # 로그인 도중이면 인증 대기 세션을 정식 세션으로 승격
        if _is_pending_verify(sess):
            db.activate_session(
                conn, sess["token_hash"], ttl_seconds=config.SESSION_TTL_DAYS * 86400
            )
            res = RedirectResponse(url=safe_next(next), status_code=303)
            set_session_cookie(res, request.cookies[config.SESSION_COOKIE])
            return res
    return RedirectResponse(
        url="/settings/account?ok=email_verified", status_code=303
    )


@router.post("/verify-email/resend", response_class=HTMLResponse)
def verify_email_resend(request: Request, next: str = Form("/")):
    """인증 코드 재발송 (재요청). SMTP 미설정이면 안내만 한다."""
    next_q = quote(safe_next(next), safe="")
    with db.connect() as conn:
        user, sess = _verify_target(request, conn)
        if user is None:
            return RedirectResponse(url="/login", status_code=303)
        if user["email_verified"]:
            return RedirectResponse(
                url=_verify_done_redirect(sess, next), status_code=303
            )
        if not mailer.mail_enabled(conn):
            return RedirectResponse(
                url=f"/verify-email?next={next_q}&error="
                + quote(t(request, "메일 발송이 설정되지 않아 코드를 보낼 수 없습니다."), safe=""),
                status_code=303,
            )
        sent = _issue_and_send_code(conn, user)
    if sent:
        return RedirectResponse(
            url=f"/verify-email?next={next_q}&sent=1", status_code=303
        )
    return RedirectResponse(
        url=f"/verify-email?next={next_q}&error="
        + quote(t(request, "코드 발송에 실패했습니다. 잠시 후 다시 시도하세요."), safe=""),
        status_code=303,
    )


# ---- 초대 수락 ----
# 초대 발급은 관리자 전용(system_routes), 수락은 링크를 받은 본인이 하므로 공개 경로.


def _invite_ctx(invite, token: str, *, email: str = "", error: str | None = None) -> dict:
    role_label = None
    if invite is not None:
        with db.connect() as conn:
            role_label = db.role_labels(conn).get(invite["role"])
    return {
        "invite": invite,
        "token": token,
        "email": email or (invite["email"] if invite is not None else ""),
        "role_label": role_label,
        "error": error,
    }


@router.get("/invite/{token}", response_class=HTMLResponse)
def invite_page(request: Request, token: str):
    """초대 수락 페이지 — 토큰이 유효하면 패스워드 설정 폼."""
    if getattr(request.state, "user", None) is not None:
        return RedirectResponse(url="/", status_code=302)
    with db.connect() as conn:
        invite = db.get_invite_by_token(conn, auth.hash_token(token))
    return templates.TemplateResponse(
        request, "invite.html", _invite_ctx(invite, token),
        status_code=200 if invite is not None else 404,
    )


@router.post("/invite/{token}", response_class=HTMLResponse)
def invite_accept(request: Request, token: str, password: str = Form(...)):
    """초대 수락 — 패스워드 설정 후 초대된 권한으로 가입, 즉시 로그인."""
    with db.connect() as conn:
        invite = db.get_invite_by_token(conn, auth.hash_token(token))
        if invite is None:
            return templates.TemplateResponse(
                request, "invite.html", _invite_ctx(None, token), status_code=404
            )
        error = auth.validate_password(password)
        if error is None and db.get_user_by_email(conn, invite["email"]) is not None:
            # 초대 후 같은 이메일이 일반 가입한 경우 — 초대는 더 이상 유효하지 않다
            db.delete_invite(conn, invite["id"])
            error = "이미 가입된 이메일입니다. 로그인하세요."
        if error is not None:
            return templates.TemplateResponse(
                request, "invite.html",
                _invite_ctx(invite, token, error=t(request, error)), status_code=400,
            )
        user_id = db.create_user(
            conn, invite["email"], auth.hash_password(password), role=invite["role"]
        )
        db.delete_invite(conn, invite["id"])  # 1회용
        token_session = auth.issue_session(conn, user_id)
    return _login_redirect(token_session, "/")


@router.post("/logout")
def logout(request: Request):
    token = request.cookies.get(config.SESSION_COOKIE, "")
    if token:
        with db.connect() as conn:
            db.delete_session(conn, auth.hash_token(token))
            db.delete_expired_sessions(conn)  # 기회적 정리
    res = RedirectResponse(url="/login", status_code=303)
    res.delete_cookie(config.SESSION_COOKIE)
    return res
