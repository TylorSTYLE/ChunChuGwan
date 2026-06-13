"""사이트 로그인 자격증명 — 코어 모듈.

아카이빙 대상 사이트에 춘추관이 로그인하기 위한 외부 자격증명을 관리한다.
비밀은 crypto 로 대칭 암호화해 db.site_credentials 에 암호문으로만 저장하고
(CLAUDE.md 원칙 6 예외), 읽을 때 복호화한다. "쓰기는 코어 모듈을 통해서만"
원칙대로 자격증명 생성·복호화는 이 모듈을 거친다.

종류(kind)는 확장형:
- http_basic : HTTP 기본/다이제스트 인증 (username, password)
- session    : 브라우저 세션 상태 storage_state (쿠키·localStorage JSON)
- jwt        : Bearer 토큰(JWT 등) — 캡처 시 Authorization: Bearer 헤더로 주입

다음 단계(캡처 연동)에서 reveal() 로 payload 를 꺼내 Playwright 컨텍스트에
주입한다 — http_basic→http_credentials, session→storage_state,
jwt→extra_http_headers. 이 모듈은 아직 캡처를 호출하지 않는다(관리만).
"""
from __future__ import annotations

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

    캡처 연동(다음 단계)에서 쓴다. 복호화 실패는 crypto.SecretDecryptError,
    키 미설정은 crypto.SecretKeyMissing 이 전파된다.
    """
    row = db.get_site_credential(conn, cred_id)
    if row is None:
        return None
    return json.loads(crypto.decrypt(row["secret"]))
