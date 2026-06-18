"""SvelteKit SPA 용 인증 JSON API (`/api/web/auth`).

미인증 흐름(로그인·2FA·가입·이메일 인증)을 JSON 으로 제공한다. auth_routes 의
SSR 핸들러와 같은 코어·상태머신(pending_totp·pending_email_verify 세션)을 재사용하되
HTML 대신 JSON 을 반환한다. 컷오버(#13 Phase C) 전까지 SSR `/login` 과 공존한다.

auth_gate 미들웨어가 `/api/` 를 미인증 통과시키므로(공개) 이 라우터는 require_session
을 쓰지 않는다 — 미인증 상태에서 호출된다. CSRF 는 `/api/web` 이라 Origin 검사 대상
(SPA 가 same-origin 이라 통과). 로그인 성공 응답은 세션 쿠키를 심고 다음 단계를
`status`(active|totp|email_verify)로 알려 SPA 가 화면 전이를 판단하게 한다.
"""
from __future__ import annotations

import hmac
import sqlite3

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .. import auth, config, db, mailer
from .auth_routes import (
    _email_verification_required,
    _is_pending_verify,
    _issue_and_send_code,
    _pending_session,
    _two_factor_target,
    _verify_target,
    passkey_login_options as _ssr_passkey_login_options,
    set_session_cookie,
)

router = APIRouter(prefix="/api/web/auth")


@router.get("/config")
def auth_config() -> dict:
    """로그인 화면 게이팅 — SSO·회원가입·메일 발송 가능 여부(비밀 아님, 공개)."""
    with db.connect() as conn:
        return {
            "oidc_enabled": config.oidc_enabled(),
            "signup_enabled": db.signup_enabled(conn),
            "mail_enabled": mailer.mail_enabled(conn),
        }


class LoginReq(BaseModel):
    email: str
    password: str


class CodeReq(BaseModel):
    code: str


def _active_or_verify(conn: sqlite3.Connection, user: sqlite3.Row) -> JSONResponse:
    """2FA 없는 로그인 마무리 — 이메일 인증 필요 시 pending_email_verify, 아니면 active.

    auth_routes._post_password_login 과 같은 분기를 JSON 으로 재현한다.
    """
    if _email_verification_required(conn, user):
        _issue_and_send_code(conn, user)
        ttl = db.email_verification_ttl_minutes(conn) * 60
        token = auth.issue_session(
            conn, user["id"], state="pending_email_verify", ttl_seconds=ttl
        )
        resp = JSONResponse({"status": "email_verify"})
        set_session_cookie(resp, token, max_age=ttl)
        return resp
    token = auth.issue_session(conn, user["id"])
    resp = JSONResponse({"status": "active"})
    set_session_cookie(resp, token)
    return resp


@router.post("/login")
def login(request: Request, body: LoginReq) -> JSONResponse:
    """이메일·패스워드 로그인 — 2FA/이메일인증 필요 시 pending 상태와 다음 단계를 반환."""
    email = body.email.strip()
    with db.connect() as conn:
        user = db.get_user_by_email(conn, email)
        ok = (
            user is not None
            and user["password_hash"] is not None
            and auth.verify_password(user["password_hash"], body.password)
        )
        if not ok:
            raise HTTPException(401, "이메일 또는 패스워드가 올바르지 않습니다.")
        if user["role"] == "blocked":
            raise HTTPException(403, "차단된 계정입니다. 관리자에게 문의하세요.")
        if user["role"] == "withdrawn":
            raise HTTPException(403, "탈퇴한 계정입니다.")
        if user["totp_secret"] is not None or db.count_passkeys(conn, user["id"]) > 0:
            # 2단계: TOTP/패스키 확인 전까지 pending 세션(짧은 수명)
            token = auth.issue_session(
                conn, user["id"], state="pending_totp",
                ttl_seconds=config.PENDING_TOTP_TTL_SECONDS,
            )
            resp = JSONResponse({
                "status": "totp",
                "has_totp": user["totp_secret"] is not None,
                "has_passkey": db.count_passkeys(conn, user["id"]) > 0,
            })
            set_session_cookie(resp, token, max_age=config.PENDING_TOTP_TTL_SECONDS)
            return resp
        return _active_or_verify(conn, user)


@router.get("/login/totp")
def login_totp_status(request: Request) -> dict:
    """2단계 인증 화면 상태 — pending_totp 세션의 사용 가능한 수단(TOTP·패스키)."""
    sess = _pending_session(request)
    if sess is None:
        raise HTTPException(401, "패스워드 인증이 필요합니다")
    with db.connect() as conn:
        user = db.get_user_by_id(conn, sess["user_id"])
        return {
            "has_totp": user["totp_secret"] is not None,
            "has_passkey": db.count_passkeys(conn, user["id"]) > 0,
        }


@router.post("/login/totp")
def login_totp(request: Request, body: CodeReq) -> JSONResponse:
    """2단계 인증(TOTP) 코드 검증 → 세션 활성(또는 이메일 인증 단계). pending_totp 세션 필수."""
    sess = _pending_session(request)
    if sess is None:
        raise HTTPException(401, "패스워드 인증이 필요합니다")
    with db.connect() as conn:
        user = db.get_user_by_id(conn, sess["user_id"])
        window = user["totp_secret"] is not None and auth.verify_totp(
            user["totp_secret"], body.code, user["totp_last_used_at"]
        )
        if not window:
            raise HTTPException(401, "코드가 올바르지 않습니다.")
        db.set_totp_last_used(conn, user["id"], window)
        status = "email_verify" if _email_verification_required(conn, user) else "active"
        # 세션을 active 로 승격(또는 pending_email_verify 전환) — 쿠키는 그대로 재설정
        _two_factor_target(conn, sess["token_hash"], user, None)
    resp = JSONResponse({"status": status})
    set_session_cookie(resp, request.cookies[config.SESSION_COOKIE])
    return resp


@router.post("/login/passkey/options")
def login_passkey_options(request: Request) -> Response:
    """패스키 2단계 인증 옵션 발급 — SSR 핸들러를 그대로 재사용(pending 세션 전용)."""
    return _ssr_passkey_login_options(request)


@router.post("/login/passkey")
async def login_passkey(request: Request) -> JSONResponse:
    """패스키 2단계 인증 응답 검증 → 세션 활성(또는 이메일 인증 단계).

    login_totp 와 같은 상태(active|email_verify) 계약을 따른다 — SSR 핸들러는
    next URL 을 반환하지만 SPA 는 status 로 화면 전이를 판단하므로 별도 구현.
    """
    sess = _pending_session(request)
    if sess is None:
        raise HTTPException(401, "패스워드 인증이 필요합니다")
    body = await request.json()
    credential = body.get("credential")
    if not isinstance(credential, dict):
        raise HTTPException(400, "credential 누락")
    with db.connect() as conn:
        challenge = db.consume_session_challenge(conn, sess["token_hash"])
        if challenge is None:
            raise HTTPException(400, "진행 중인 인증이 없습니다 — 다시 시도하세요")
        cred = db.get_passkey(conn, sess["user_id"], str(credential.get("id", "")))
        if cred is None:
            raise HTTPException(401, "등록되지 않은 패스키입니다")
        new_count = auth.verify_passkey_authentication(
            credential, challenge, cred["public_key"], cred["sign_count"]
        )
        if new_count is None:
            raise HTTPException(401, "패스키 인증에 실패했습니다")
        db.touch_passkey(conn, cred["id"], new_count)
        user = db.get_user_by_id(conn, sess["user_id"])
        status = "email_verify" if _email_verification_required(conn, user) else "active"
        _two_factor_target(conn, sess["token_hash"], user, None)
    resp = JSONResponse({"status": status})
    set_session_cookie(resp, request.cookies[config.SESSION_COOKIE])
    return resp


@router.post("/signup")
def signup(request: Request, body: LoginReq) -> JSONResponse:
    """회원 가입 — 가입 즉시 로그인(또는 이메일 인증 단계). 가입 비활성/중복은 거부."""
    email = body.email.strip()
    err = auth.validate_credentials(email, body.password)
    if err is not None:
        raise HTTPException(400, err)
    with db.connect() as conn:
        if not db.signup_enabled(conn):
            raise HTTPException(403, "회원 가입이 비활성화되어 있습니다.")
        if db.get_user_by_email(conn, email) is not None:
            raise HTTPException(400, "이미 가입된 이메일입니다.")
        uid = db.create_user(
            conn, email, auth.hash_password(body.password),
            role=db.signup_default_role(conn),
        )
        user = db.get_user_by_id(conn, uid)
        return _active_or_verify(conn, user)


@router.get("/verify-email/status")
def verify_email_status(request: Request) -> dict:
    """이메일 인증 대상 상태 — 로그인 도중(pending_email_verify) 또는 개인 설정 진입."""
    with db.connect() as conn:
        user, sess = _verify_target(request, conn)
        if user is None:
            raise HTTPException(401, "인증 대상이 없습니다")
        return {
            "email": user["email"],
            "verified": bool(user["email_verified"]),
            "pending": _is_pending_verify(sess),
            "mail_enabled": mailer.mail_enabled(conn),
            "ttl_minutes": db.email_verification_ttl_minutes(conn),
        }


@router.post("/verify-email")
def verify_email(request: Request, body: CodeReq) -> JSONResponse:
    """인증 코드 검증 → 완료. 로그인 도중이면 pending 세션을 active 로 승격한다."""
    with db.connect() as conn:
        user, sess = _verify_target(request, conn)
        if user is None:
            raise HTTPException(401, "인증 대상이 없습니다")
        if not user["email_verified"]:
            record = db.get_email_verification(conn, user["id"])
            ok = record is not None and hmac.compare_digest(
                record["code_hash"], auth.hash_token(body.code.strip())
            )
            if not ok:
                raise HTTPException(401, "코드가 올바르지 않거나 만료되었습니다.")
            db.set_email_verified(conn, user["id"])
            db.delete_email_verification(conn, user["id"])
        if _is_pending_verify(sess):
            db.activate_session(
                conn, sess["token_hash"], ttl_seconds=config.SESSION_TTL_DAYS * 86400
            )
            resp = JSONResponse({"status": "active"})
            set_session_cookie(resp, request.cookies[config.SESSION_COOKIE])
            return resp
    return JSONResponse({"status": "active"})


@router.post("/verify-email/resend")
def verify_email_resend(request: Request) -> dict:
    """인증 코드 재발송 — SMTP 미설정이면 400."""
    with db.connect() as conn:
        user, sess = _verify_target(request, conn)
        if user is None:
            raise HTTPException(401, "인증 대상이 없습니다")
        if user["email_verified"]:
            return {"sent": False, "verified": True}
        if not mailer.mail_enabled(conn):
            raise HTTPException(400, "메일 발송이 설정되지 않아 코드를 보낼 수 없습니다.")
        sent = _issue_and_send_code(conn, user)
    return {"sent": sent}
