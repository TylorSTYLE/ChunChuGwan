"""인증 코어 — 패스워드 해싱, 세션 토큰, TOTP. 웹 프레임워크와 무관한 순수 로직."""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import re
import secrets
import sqlite3
import time

import pyotp
import qrcode
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError

from . import config, db

_hasher = PasswordHasher()  # Argon2id, 라이브러리 권장 기본 파라미터

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

TOTP_PERIOD = 30  # 초 단위 시간창 (Google Authenticator 기본)


# ---- 패스워드 ----


def hash_password(password: str) -> str:
    """Argon2id 해시 생성."""
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """해시 대조. 불일치/손상 해시는 False."""
    try:
        return _hasher.verify(password_hash, password)
    except VerificationError:
        return False


def validate_credentials(email: str, password: str) -> str | None:
    """가입 입력 검증. 문제 있으면 한국어 오류 메시지, 정상이면 None."""
    if not _EMAIL_RE.match(email):
        return "올바른 이메일 형식이 아닙니다."
    if len(password) < config.MIN_PASSWORD_LENGTH:
        return f"패스워드는 {config.MIN_PASSWORD_LENGTH}자 이상이어야 합니다."
    return None


# ---- 세션 ----


def hash_token(token: str) -> str:
    """세션 토큰의 SHA-256 hex (DB에는 이 값만 저장)."""
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def issue_session(
    conn: sqlite3.Connection,
    user_id: int,
    state: str = "active",
    ttl_seconds: int | None = None,
) -> str:
    """세션을 생성하고 쿠키에 넣을 토큰 원문을 반환."""
    if ttl_seconds is None:
        ttl_seconds = config.SESSION_TTL_DAYS * 86400
    token = secrets.token_urlsafe(32)
    db.create_session(conn, hash_token(token), user_id, state, ttl_seconds)
    return token


def resolve_session(conn: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    """쿠키 토큰으로 유효한 세션 row 조회 (없거나 만료면 None)."""
    if not token:
        return None
    return db.get_session(conn, hash_token(token))


# ---- TOTP ----


def new_totp_secret() -> str:
    """base32 TOTP 시크릿 생성."""
    return pyotp.random_base32()


def totp_provisioning_uri(secret: str, email: str) -> str:
    """인증 앱 등록용 otpauth:// URI (Google Authenticator 호환)."""
    return pyotp.TOTP(secret).provisioning_uri(
        name=email, issuer_name=config.TOTP_ISSUER
    )


def verify_totp(secret: str, code: str, last_used: str | None) -> str | None:
    """TOTP 코드 검증. 성공 시 사용된 시간창 식별자, 실패/재사용이면 None.

    시계 오차를 고려해 현재 ±1 시간창을 허용하되, 같은 시간창의 코드는
    한 번만 통과시킨다 (last_used 와 비교).
    """
    code = code.strip().replace(" ", "")
    if not code.isdigit():
        return None
    totp = pyotp.TOTP(secret)
    now_counter = int(time.time() // TOTP_PERIOD)
    for offset in (0, -1, 1):
        counter = now_counter + offset
        expected = totp.at(counter * TOTP_PERIOD)
        if hmac.compare_digest(expected, code):
            if last_used is not None and counter <= int(last_used):
                return None  # 이미 사용된 시간창 — replay 거부
            return str(counter)
    return None


def qr_data_uri(uri: str) -> str:
    """otpauth URI 를 QR PNG 의 base64 data URI 로 변환 (템플릿 인라인용)."""
    img = qrcode.make(uri, box_size=6, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
