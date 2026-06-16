"""대칭 암호화 — 복원 가능해야 하는 비밀(외부 사이트 로그인 자격증명) 저장용.

CLAUDE.md 아키텍처 원칙 6 의 예외다. 사용자 인증 데이터(로그인 비밀번호·
세션·API 키·패스키)는 단방향 해시지만, 아카이빙 대상 사이트에 춘추관이
로그인하려면 자격증명을 그대로 replay 해야 하므로 대칭 암호화로 저장한다.
Fernet(AES-128-CBC + HMAC-SHA256)이라 복호화와 변조 감지를 모두 만족한다.

키는 환경변수 WCCG_SECRET_KEY 에서만 오고 DB·저장소에는 들어가지 않는다.
키가 없으면 암호화 기능 자체가 비활성화되며(SecretKeyMissing), 기존
아카이빙은 영향받지 않는다. 키를 바꾸면 기존 암호문은 복호화할 수 없다.
"""
from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from . import config


class SecretKeyMissing(RuntimeError):
    """WCCG_SECRET_KEY 가 설정되지 않아 암호화/복호화를 할 수 없다."""


class SecretDecryptError(RuntimeError):
    """복호화 실패 — 키가 바뀌었거나 암호문이 손상됐다."""


def is_configured() -> bool:
    """암호화 키(WCCG_SECRET_KEY)가 설정돼 있는지."""
    return bool(config.SECRET_KEY)


def _fernet() -> Fernet:
    """WCCG_SECRET_KEY 에서 Fernet 키를 파생. 미설정이면 SecretKeyMissing.

    임의의 문자열 패스프레이즈를 받아 SHA-256 → urlsafe base64 로 Fernet
    키(32바이트)를 만든다 — 사용자가 키 형식을 신경 쓰지 않아도 된다.
    """
    secret = config.SECRET_KEY
    if not secret:
        raise SecretKeyMissing(
            "WCCG_SECRET_KEY 가 설정되지 않아 자격증명을 암호화할 수 없습니다."
        )
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
    return Fernet(key)


def encrypt(plaintext: str) -> str:
    """평문을 암호화해 토큰 문자열로 반환. 키 미설정이면 SecretKeyMissing."""
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str:
    """암호문 토큰을 평문으로 복호화.

    키 미설정이면 SecretKeyMissing, 키 불일치·손상이면 SecretDecryptError.
    """
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as e:
        raise SecretDecryptError(
            "복호화에 실패했습니다 — WCCG_SECRET_KEY 가 바뀌었거나 암호문이 손상됐습니다."
        ) from e
