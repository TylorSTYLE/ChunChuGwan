"""Authentik OIDC 클라이언트 — Authorization Code Flow.

authlib 대신 httpx + PyJWT 로 직접 구현한다 (서명 쿠키 세션 강제를 피하고
프로젝트의 단순 함수형 스타일 유지). 전부 순수 함수라 테스트에서
monkeypatch 로 잘라내기 쉽다.
"""

from __future__ import annotations

from urllib.parse import urlencode

import httpx
import jwt

from . import config

HTTP_TIMEOUT = 10.0

_discovery: dict | None = None
_jwks_client: jwt.PyJWKClient | None = None


def discover() -> dict:
    """OIDC discovery 메타데이터 조회 (모듈 캐시)."""
    global _discovery
    if _discovery is None:
        url = f"{config.OIDC_ISSUER}/.well-known/openid-configuration"
        res = httpx.get(url, timeout=HTTP_TIMEOUT)
        res.raise_for_status()
        _discovery = res.json()
    return _discovery


def redirect_uri() -> str:
    """콜백 redirect_uri. PUBLIC_URL 미설정이면 로컬 대시보드 주소."""
    base = config.PUBLIC_URL or (
        f"http://{config.DASHBOARD_HOST}:{config.DASHBOARD_PORT}"
    )
    return f"{base}/auth/oidc/callback"


def build_authorize_url(state: str, nonce: str) -> str:
    """Authentik 인가 엔드포인트 URL 조립."""
    params = urlencode(
        {
            "response_type": "code",
            "client_id": config.OIDC_CLIENT_ID,
            "redirect_uri": redirect_uri(),
            "scope": "openid email profile",
            "state": state,
            "nonce": nonce,
        }
    )
    return f"{discover()['authorization_endpoint']}?{params}"


def exchange_code(code: str) -> dict:
    """인가 코드를 토큰으로 교환. 실패 시 httpx.HTTPStatusError."""
    res = httpx.post(
        discover()["token_endpoint"],
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri(),
            "client_id": config.OIDC_CLIENT_ID,
            "client_secret": config.OIDC_CLIENT_SECRET,
        },
        timeout=HTTP_TIMEOUT,
    )
    res.raise_for_status()
    return res.json()


def validate_id_token(id_token: str, nonce: str) -> dict:
    """ID 토큰 서명(JWKS) + iss/aud/exp/nonce 검증 후 클레임 반환.

    실패 시 jwt.PyJWTError 또는 ValueError.
    """
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = jwt.PyJWKClient(discover()["jwks_uri"])
    key = _jwks_client.get_signing_key_from_jwt(id_token)
    claims = jwt.decode(
        id_token,
        key.key,
        algorithms=["RS256", "ES256"],
        audience=config.OIDC_CLIENT_ID,
        issuer=discover()["issuer"],
        options={"require": ["exp", "iss", "aud", "sub"]},
    )
    if claims.get("nonce") != nonce:
        raise ValueError("nonce 불일치")
    return claims
