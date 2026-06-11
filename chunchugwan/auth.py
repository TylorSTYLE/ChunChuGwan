"""인증 코어 — 패스워드 해싱, 세션 토큰, TOTP, 패스키. 웹 프레임워크와 무관한 순수 로직."""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import logging
import re
import secrets
import sqlite3
import time
from typing import Any

import pyotp
import qrcode
import webauthn
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import PublicKeyCredentialDescriptor

from . import config, db

logger = logging.getLogger(__name__)

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


def validate_email(email: str) -> str | None:
    """이메일 형식 검증 (가입·초대 공용). 문제 있으면 한국어 오류 메시지."""
    if not _EMAIL_RE.match(email):
        return "올바른 이메일 형식이 아닙니다."
    return None


def validate_credentials(email: str, password: str) -> str | None:
    """가입 입력 검증. 문제 있으면 한국어 오류 메시지, 정상이면 None."""
    return validate_email(email) or validate_password(password)


def validate_password(password: str) -> str | None:
    """패스워드 정책 검증 (가입·변경 공용). 문제 있으면 한국어 오류 메시지."""
    if len(password) < config.MIN_PASSWORD_LENGTH:
        return f"패스워드는 {config.MIN_PASSWORD_LENGTH}자 이상이어야 합니다."
    return None


MAX_DISPLAY_NAME_LENGTH = 50


def validate_display_name(name: str) -> str | None:
    """표시 이름 검증. 문제 있으면 한국어 오류 메시지, 정상이면 None."""
    if len(name) > MAX_DISPLAY_NAME_LENGTH:
        return f"이름은 {MAX_DISPLAY_NAME_LENGTH}자 이하여야 합니다."
    if not name.isprintable():
        return "이름에 제어 문자를 쓸 수 없습니다."
    return None


# ---- 최초 구동 부트스트랩 ----


def bootstrap_admin_from_env(conn: sqlite3.Connection) -> bool:
    """환경변수의 관리자 계정을 등록하고 성공 여부 반환.

    users 가 비어 있을 때만 호출되는 전제. 환경변수가 없거나 형식이
    틀리면 False — 호출자는 /setup 등록 페이지로 유도한다.
    """
    if not (config.ADMIN_EMAIL and config.ADMIN_PASSWORD):
        return False
    error = validate_credentials(config.ADMIN_EMAIL, config.ADMIN_PASSWORD)
    if error is not None:
        logger.warning("WCCG_ADMIN_* 환경변수 무시 — %s", error)
        return False
    user_id = db.create_first_admin(
        conn, config.ADMIN_EMAIL, hash_password(config.ADMIN_PASSWORD)
    )
    if user_id is not None:
        logger.info("최초 구동 — 관리자 계정 등록: %s", config.ADMIN_EMAIL)
    return user_id is not None


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


# ---- 패스키 (WebAuthn) ----
# 옵션의 challenge 는 base64url 로 세션에 보관했다가 검증 시 1회용으로 소비한다.


def _descriptors(credential_ids: list[str]) -> list[PublicKeyCredentialDescriptor]:
    """base64url credential_id 목록을 WebAuthn 디스크립터로 변환."""
    return [
        PublicKeyCredentialDescriptor(id=base64url_to_bytes(cid))
        for cid in credential_ids
    ]


def passkey_registration_options(
    user_id: int, email: str, exclude_ids: list[str]
) -> tuple[str, str]:
    """패스키 등록 옵션 생성 → (클라이언트용 JSON, 보관용 챌린지 base64url)."""
    options = webauthn.generate_registration_options(
        rp_id=config.WEBAUTHN_RP_ID,
        rp_name=config.WEBAUTHN_RP_NAME,
        user_id=str(user_id).encode("ascii"),
        user_name=email,
        exclude_credentials=_descriptors(exclude_ids),
    )
    return webauthn.options_to_json(options), bytes_to_base64url(options.challenge)


def verify_passkey_registration(
    credential: dict[str, Any], challenge: str
) -> dict[str, Any] | None:
    """등록 응답 검증. 성공 시 DB 저장용 필드 dict, 실패 시 None."""
    try:
        verified = webauthn.verify_registration_response(
            credential=credential,
            expected_challenge=base64url_to_bytes(challenge),
            expected_rp_id=config.WEBAUTHN_RP_ID,
            expected_origin=config.WEBAUTHN_ORIGINS,
        )
    except (WebAuthnException, ValueError, KeyError, TypeError) as e:
        logger.warning("패스키 등록 검증 실패: %s", e)
        return None
    return {
        "credential_id": bytes_to_base64url(verified.credential_id),
        "public_key": bytes_to_base64url(verified.credential_public_key),
        "sign_count": verified.sign_count,
    }


def passkey_authentication_options(credential_ids: list[str]) -> tuple[str, str]:
    """패스키 인증 옵션 생성 → (클라이언트용 JSON, 보관용 챌린지 base64url)."""
    options = webauthn.generate_authentication_options(
        rp_id=config.WEBAUTHN_RP_ID,
        allow_credentials=_descriptors(credential_ids),
    )
    return webauthn.options_to_json(options), bytes_to_base64url(options.challenge)


def verify_passkey_authentication(
    credential: dict[str, Any], challenge: str, public_key: str, sign_count: int
) -> int | None:
    """인증 응답 검증. 성공 시 새 sign_count, 실패 시 None."""
    try:
        verified = webauthn.verify_authentication_response(
            credential=credential,
            expected_challenge=base64url_to_bytes(challenge),
            expected_rp_id=config.WEBAUTHN_RP_ID,
            expected_origin=config.WEBAUTHN_ORIGINS,
            credential_public_key=base64url_to_bytes(public_key),
            credential_current_sign_count=sign_count,
        )
    except (WebAuthnException, ValueError, KeyError, TypeError) as e:
        logger.warning("패스키 인증 검증 실패: %s", e)
        return None
    return verified.new_sign_count


def qr_data_uri(uri: str) -> str:
    """otpauth URI 를 QR PNG 의 base64 data URI 로 변환 (템플릿 인라인용)."""
    img = qrcode.make(uri, box_size=6, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
