"""인증 라우트 — 로그인 / 가입 / 로그아웃 / 2FA(TOTP·패스키)."""

from __future__ import annotations

import logging
import secrets
import sqlite3

import httpx
import jwt
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)

from .. import auth, config, db, oidc
from .i18n import t
from .templating import templates

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
        token = auth.issue_session(conn, user["id"])
    return _login_redirect(token, next)


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


def _activate_and_redirect(request: Request, next_url: str) -> RedirectResponse:
    """2단계 통과 — 쿠키 수명을 정식 세션으로 연장하고 목적지로."""
    res = RedirectResponse(url=safe_next(next_url), status_code=303)
    set_session_cookie(res, request.cookies[config.SESSION_COOKIE])
    return res


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
        db.activate_session(
            conn, sess["token_hash"], ttl_seconds=config.SESSION_TTL_DAYS * 86400
        )
    return _activate_and_redirect(request, next)


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
        db.activate_session(
            conn, sess["token_hash"], ttl_seconds=config.SESSION_TTL_DAYS * 86400
        )
    res = JSONResponse({"ok": True, "next": safe_next(body.get("next"))})
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


def _account_ctx(
    user, *, error: str | None = None, notice: str | None = None
) -> dict:
    with db.connect() as conn:
        passkey_count = db.count_passkeys(conn, user["id"])
    return {
        "display_name": user["display_name"] or "",
        "has_password": user["password_hash"] is not None,
        "email": user["email"],
        "role": user["role"],
        "role_label": db.ROLE_LABELS.get(user["role"], user["role"]),
        "totp_enabled": user["totp_secret"] is not None,
        "passkey_count": passkey_count,
        "error": error,
        "notice": notice,
    }


@router.get("/settings/account", response_class=HTMLResponse)
def account_page(request: Request, ok: str | None = None):
    user = request.state.user
    notice = {
        "name": "사용자 이름을 변경했습니다.",
        "password": "패스워드를 변경했습니다. 다른 기기의 세션은 로그아웃되었습니다.",
    }.get(ok or "")
    if notice:
        notice = t(request, notice)
    return templates.TemplateResponse(
        request, "account.html", _account_ctx(user, notice=notice)
    )


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
                token = auth.issue_session(conn, user_id)
        if error is None:
            return _login_redirect(token, "/")
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


# ---- 초대 수락 ----
# 초대 발급은 관리자 전용(system_routes), 수락은 링크를 받은 본인이 하므로 공개 경로.


def _invite_ctx(invite, token: str, *, email: str = "", error: str | None = None) -> dict:
    return {
        "invite": invite,
        "token": token,
        "email": email or (invite["email"] if invite is not None else ""),
        "role_label": db.ROLE_LABELS.get(invite["role"]) if invite is not None else None,
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
