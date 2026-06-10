"""인증 라우트 — 로그인 / 가입 / 로그아웃."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .. import auth, config, db
from .templating import templates

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


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str | None = None):
    if getattr(request.state, "user", None) is not None:
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse(
        request, "login.html",
        {"next": safe_next(next), "error": None, "email": "",
         "oidc_enabled": config.oidc_enabled()},
    )


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
                {"next": safe_next(next), "error": "이메일 또는 패스워드가 올바르지 않습니다.",
                 "email": email, "oidc_enabled": config.oidc_enabled()},
                status_code=401,
            )
        if user["totp_secret"] is not None:
            # 2단계: OTP 입력 전까지는 pending 세션 (짧은 수명)
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


# ---- TOTP 2단계 로그인 ----


def _pending_session(request: Request):
    """미들웨어가 적재한 pending_totp 세션 (없으면 None)."""
    sess = getattr(request.state, "session", None)
    if sess is not None and sess["state"] == "pending_totp":
        return sess
    return None


@router.get("/login/totp", response_class=HTMLResponse)
def totp_page(request: Request, next: str | None = None):
    if _pending_session(request) is None:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse(
        request, "totp.html", {"next": safe_next(next), "error": None}
    )


@router.post("/login/totp", response_class=HTMLResponse)
def totp_login(request: Request, code: str = Form(...), next: str = Form("/")):
    sess = _pending_session(request)
    if sess is None:
        return RedirectResponse(url="/login", status_code=302)
    with db.connect() as conn:
        user = db.get_user_by_id(conn, sess["user_id"])
        window = auth.verify_totp(
            user["totp_secret"], code, user["totp_last_used_at"]
        )
        if window is None:
            return templates.TemplateResponse(
                request, "totp.html",
                {"next": safe_next(next), "error": "코드가 올바르지 않습니다."},
                status_code=401,
            )
        db.set_totp_last_used(conn, user["id"], window)
        db.activate_session(
            conn, sess["token_hash"], ttl_seconds=config.SESSION_TTL_DAYS * 86400
        )
    res = RedirectResponse(url=safe_next(next), status_code=303)
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
                   "error": "코드가 올바르지 않습니다. QR을 다시 스캔 후 시도하세요.",
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
                 "error": "패스워드가 올바르지 않습니다.", "qr": None, "secret": None},
                status_code=401,
            )
        db.disable_totp(conn, user["id"])
    return RedirectResponse(url="/settings/totp", status_code=303)


@router.get("/signup", response_class=HTMLResponse)
def signup_page(request: Request):
    if getattr(request.state, "user", None) is not None:
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse(
        request, "signup.html", {"error": None, "email": ""}
    )


@router.post("/signup", response_class=HTMLResponse)
def signup(request: Request, email: str = Form(...), password: str = Form(...)):
    email = email.strip()
    error = auth.validate_credentials(email, password)
    if error is None:
        with db.connect() as conn:
            if db.get_user_by_email(conn, email) is not None:
                error = "이미 가입된 이메일입니다."
            else:
                user_id = db.create_user(conn, email, auth.hash_password(password))
                token = auth.issue_session(conn, user_id)
        if error is None:
            return _login_redirect(token, "/")
    return templates.TemplateResponse(
        request, "signup.html", {"error": error, "email": email}, status_code=400
    )


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
