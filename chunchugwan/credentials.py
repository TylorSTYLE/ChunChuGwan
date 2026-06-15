"""1회성 인증 캡슐 암호화 — AES-256-GCM.

확장이 보낸 로그인 세션 캡슐(Playwright storage_state)을 DB 에 잠깐 보관할 때
암호화한다. 마스터 키(`config.CREDENTIAL_KEY`)는 index.db 밖(환경변수)에 있어
DB·백업 파일이 통째로 유출돼도 키 없이는 복호할 수 없다. 키가 비어 있으면
인증 캡처 기능 자체가 비활성이다(`is_enabled()` False).

남는 위험(설계 전제): 캡처가 도는 짧은 순간엔 평문 storage_state 가 메모리에
로드되고 마스터 키도 프로세스에 있으므로, 그 시점 서버가 침해되면 해당 쿠키가
노출될 수 있다. 디스크 암호화로는 이 창을 막지 못한다 — 짧은 TTL·캡처 직후
삭제·1회성으로 노출 창을 최소화하는 것이 완화책이다.
"""
from __future__ import annotations

import base64
import binascii
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import config

_NONCE_BYTES = 12


class CredentialKeyError(RuntimeError):
    """마스터 키가 없거나 형식이 잘못됨 — 인증 캡처를 진행할 수 없다."""


def is_enabled() -> bool:
    """인증 캡처 기능 활성 여부 — 마스터 키가 설정돼 있으면 True."""
    return bool(config.CREDENTIAL_KEY)


def _decode(raw: str) -> bytes | None:
    """hex(64자) 또는 base64(44자)로 인코딩된 32바이트 키를 디코드 (실패 시 None)."""
    for decoder in (bytes.fromhex, lambda s: base64.b64decode(s, validate=True)):
        try:
            decoded = decoder(raw)
        except (ValueError, binascii.Error):
            continue
        if len(decoded) == 32:
            return decoded
    return None


def _key() -> bytes:
    raw = config.CREDENTIAL_KEY
    if not raw:
        raise CredentialKeyError("WCCG_CREDENTIAL_KEY 가 설정되지 않았습니다")
    key = _decode(raw)
    if key is None:
        raise CredentialKeyError(
            "WCCG_CREDENTIAL_KEY 는 base64 또는 hex 로 인코딩한 32바이트(AES-256)여야 합니다"
        )
    return key


def encrypt(plaintext: bytes) -> bytes:
    """평문을 AES-256-GCM 으로 암호화 — `nonce(12) || ciphertext+tag` 를 반환."""
    nonce = os.urandom(_NONCE_BYTES)
    return nonce + AESGCM(_key()).encrypt(nonce, plaintext, None)


def decrypt(blob: bytes) -> bytes:
    """encrypt 산출물을 복호 — 변조·키 불일치 시 cryptography 가 예외를 던진다."""
    return AESGCM(_key()).decrypt(blob[:_NONCE_BYTES], blob[_NONCE_BYTES:], None)
