"""인증 라우트 (C2 컷오버 잔여) — SSO(OIDC) 리다이렉트 흐름·로그아웃·패스키 2단계 옵션.

대부분의 인증 UI 는 SPA 의 `/api/web/auth/*`(web_auth_routes)로 옮겨갔고, 여기에는
HTML/리다이렉트가 본질인 OIDC 로그인·콜백, 폼 POST 로그아웃, 그리고 web_auth_routes
가 재사용하는 공용 헬퍼(이메일 인증·pending·2단계 세션 전이·세션 쿠키)만 남았다.
"""

from __future__ import annotations

import logging
import secrets
import smtplib
import sqlite3
from urllib.parse import quote

import httpx
import jwt
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from .. import auth, config, db, mailer, oidc
from .i18n import t

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
    """리다이렉트 대상 검증 — 사이트 내부 절대경로만 허용 (open redirect 방지).

    `//`(프로토콜 상대)뿐 아니라 백슬래시(`/\\evil.com` — 브라우저가 `/` 로 정규화해
    `//evil.com` 우회)와 제어·비출력 문자도 막고, 단일 `/` 로 시작하는 출력 가능한
    내부 경로만 통과시킨다. (보안 검토 F5)
    """
    if (
        next_url
        and next_url.startswith("/")
        and not next_url.startswith("//")
        and "\\" not in next_url
        and next_url.isprintable()
    ):
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


# ---- 최초 설정: 백업 복원 / 네트워크 이전 ----


def _require_first_run(request: Request) -> None:
    """설정 작업은 사용자가 아직 없을 때(최초 구동)만 허용한다."""
    with db.connect() as conn:
        if db.count_users(conn) > 0:
            raise HTTPException(403, t(request, "이미 설정이 완료되었습니다"))


# ---- 2단계 로그인 (TOTP / 패스키) ----


def _pending_session(request: Request):
    """미들웨어가 적재한 pending_totp 세션 (없으면 None)."""
    sess = getattr(request.state, "session", None)
    if sess is not None and sess["state"] == "pending_totp":
        return sess
    return None


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


# ---- TOTP 설정 (등록/해제) ----


# ---- 패스키 설정 (등록/삭제) ----
# 패스키는 TOTP 와 동일하게 패스워드 로그인의 2단계로만 쓴다 (SSO 는 IdP 2FA 신뢰).


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


# ---- 초대 수락 ----
# 초대 발급은 관리자 전용(system_routes), 수락은 링크를 받은 본인이 하므로 공개 경로.


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
