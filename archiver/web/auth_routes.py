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
        token = auth.issue_session(conn, user["id"])
    return _login_redirect(token, next)


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
