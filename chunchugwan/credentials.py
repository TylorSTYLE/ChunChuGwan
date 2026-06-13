"""사이트 로그인 자격증명 — 코어 모듈.

아카이빙 대상 사이트에 춘추관이 로그인하기 위한 외부 자격증명을 관리한다.
비밀은 crypto 로 대칭 암호화해 db.site_credentials 에 암호문으로만 저장하고
(CLAUDE.md 원칙 6 예외), 읽을 때 복호화한다. "쓰기는 코어 모듈을 통해서만"
원칙대로 자격증명 생성·복호화는 이 모듈을 거친다.

종류(kind)는 확장형:
- http_basic : HTTP 기본/다이제스트 인증 (username, password)
- session    : 브라우저 세션 상태 storage_state (쿠키·localStorage JSON)
- jwt        : Bearer 토큰(JWT 등) — 캡처 시 Authorization: Bearer 헤더로 주입

캡처 연동(reveal_for_capture)에서 payload 를 꺼내 Playwright 컨텍스트에
주입한다 — http_basic→http_credentials(대상 origin 스코프),
session→storage_state, jwt→대상 origin 요청에만 Authorization 헤더
(context.route). 자격증명이 페이지의 서드파티 하위 자원으로 새지 않게 모두
대상 origin 으로 스코프한다 (capture._context_options 참조).
"""
from __future__ import annotations

import base64
import json
import sqlite3

from . import crypto, db

# 지원하는 자격증명 종류
KIND_HTTP_BASIC = "http_basic"
KIND_SESSION = "session"
KIND_JWT = "jwt"
KINDS = (KIND_HTTP_BASIC, KIND_SESSION, KIND_JWT)

# 종류별 사람이 읽는 라벨 (i18n 키 — 한국어 원문이 곧 메시지 키)
KIND_LABELS = {
    KIND_HTTP_BASIC: "HTTP 기본 인증",
    KIND_SESSION: "세션 쿠키",
    KIND_JWT: "JWT (Bearer 토큰)",
}

MAX_LABEL_LENGTH = 50
MAX_USERNAME_LENGTH = 200
MAX_PASSWORD_LENGTH = 1000
MAX_SESSION_BYTES = 256 * 1024   # storage_state JSON 상한 (256KB)
MAX_JWT_LENGTH = 8192            # Bearer 토큰(JWT) 길이 상한


class CredentialError(ValueError):
    """자격증명 입력 오류 — 사용자에게 보일 한국어 메시지를 담는다(i18n 키)."""


def kind_label(kind: str) -> str:
    """종류 코드의 표시 라벨(i18n 키). 모르는 종류면 코드를 그대로 반환."""
    return KIND_LABELS.get(kind, kind)


def validate_label(label: str) -> str | None:
    """라벨 검증. 문제 있으면 한국어 오류 메시지, 정상이면 None."""
    if not label:
        return "이름을 입력하세요."
    if len(label) > MAX_LABEL_LENGTH:
        return f"이름은 {MAX_LABEL_LENGTH}자 이하여야 합니다."
    if not label.isprintable():
        return "이름에 제어 문자를 쓸 수 없습니다."
    return None


def build_payload(kind: str, form: dict) -> dict:
    """폼 입력에서 종류별 payload dict 를 만든다(검증 포함).

    오류면 CredentialError(한국어 메시지)를 던진다. 반환 dict 가 그대로
    암호화돼 저장된다.
    """
    if kind == KIND_HTTP_BASIC:
        username = (form.get("username") or "").strip()
        password = form.get("password") or ""
        if not username:
            raise CredentialError("사용자명을 입력하세요.")
        if not password:
            raise CredentialError("비밀번호를 입력하세요.")
        if len(username) > MAX_USERNAME_LENGTH:
            raise CredentialError(f"사용자명은 {MAX_USERNAME_LENGTH}자 이하여야 합니다.")
        if len(password) > MAX_PASSWORD_LENGTH:
            raise CredentialError(f"비밀번호는 {MAX_PASSWORD_LENGTH}자 이하여야 합니다.")
        return {"username": username, "password": password}

    if kind == KIND_SESSION:
        raw = form.get("storage_state") or ""
        if not raw.strip():
            raise CredentialError("세션 상태(storage_state) JSON 을 입력하세요.")
        if len(raw.encode("utf-8")) > MAX_SESSION_BYTES:
            raise CredentialError("세션 상태 JSON 이 너무 큽니다.")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            raise CredentialError("세션 상태가 올바른 JSON 이 아닙니다.") from None
        if not isinstance(data, dict) or "cookies" not in data:
            raise CredentialError(
                "세션 상태 JSON 형식이 아닙니다 (cookies 키가 필요합니다)."
            )
        return {"storage_state": data}

    if kind == KIND_JWT:
        token = (form.get("token") or "").strip()
        if not token:
            raise CredentialError("토큰을 입력하세요.")
        if len(token) > MAX_JWT_LENGTH:
            raise CredentialError(f"토큰은 {MAX_JWT_LENGTH}자 이하여야 합니다.")
        # 공백·줄바꿈 금지 — Authorization 헤더 주입(캡처 연동) 위험 + 붙여넣기 실수
        if any(ch.isspace() for ch in token):
            raise CredentialError("토큰에 공백·줄바꿈을 넣을 수 없습니다.")
        return {"token": token}

    raise CredentialError("잘못된 자격증명 종류입니다.")


def add(
    conn: sqlite3.Connection,
    site_id: int,
    label: str,
    kind: str,
    payload: dict,
    *,
    created_by: int | None,
) -> int:
    """payload 를 암호화해 자격증명을 저장하고 id 반환.

    crypto 키 미설정이면 crypto.SecretKeyMissing 이 전파된다(호출부가 처리).
    """
    secret = crypto.encrypt(json.dumps(payload, ensure_ascii=False))
    return db.create_site_credential(
        conn, site_id, label, kind, secret, created_by=created_by
    )


def reveal(conn: sqlite3.Connection, cred_id: int) -> dict | None:
    """자격증명을 복호화해 payload dict 반환 (없으면 None).

    복호화 실패는 crypto.SecretDecryptError, 키 미설정은
    crypto.SecretKeyMissing 이 전파된다.
    """
    row = db.get_site_credential(conn, cred_id)
    if row is None:
        return None
    return json.loads(crypto.decrypt(row["secret"]))


def reveal_for_capture(
    conn: sqlite3.Connection, cred_id: int
) -> tuple[str, dict] | None:
    """캡처 연동용 — (kind, 복호화 payload) 반환 (없으면 None).

    pipeline 이 페이지의 credential_id 로 호출해 capture 컨텍스트에 주입한다.
    복호화 실패는 crypto.SecretDecryptError, 키 미설정은
    crypto.SecretKeyMissing 이 전파된다 (호출부가 graceful 처리).
    """
    row = db.get_site_credential(conn, cred_id)
    if row is None:
        return None
    return row["kind"], json.loads(crypto.decrypt(row["secret"]))


def httpx_auth(kind: str, payload: dict) -> dict:
    """캡처 외 경로(httpx 문서 다운로드)용 인증 스펙.

    Basic·Bearer 는 Authorization 헤더로 보낸다 — httpx 가 교차 origin
    리다이렉트 시 Authorization 을 떼므로 누수에 안전하다. 세션은 쿠키 목록
    으로 돌려주고(documents 가 도메인 스코프 jar 로 변환), 대상 origin 매칭은
    호출부가 한다. 반환: {"headers": {...}} 또는 {"cookies": [cookie dict...]}.
    """
    if kind == KIND_HTTP_BASIC:
        raw = f"{payload.get('username', '')}:{payload.get('password', '')}"
        token = base64.b64encode(raw.encode("utf-8")).decode("ascii")
        return {"headers": {"Authorization": f"Basic {token}"}}
    if kind == KIND_JWT:
        return {"headers": {"Authorization": f"Bearer {payload.get('token', '')}"}}
    if kind == KIND_SESSION:
        cookies = payload.get("storage_state", {}).get("cookies", [])
        return {"cookies": cookies} if cookies else {}
    return {}
