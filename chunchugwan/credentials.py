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
import ipaddress
import json
import sqlite3
from datetime import datetime, timezone
from urllib.parse import urlsplit

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
MAX_HAR_BYTES = 64 * 1024 * 1024  # 업로드 HAR 파일 상한 (64MB — 쿠키만 추출)


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


def _normalize_cookie(cookie: object) -> dict | None:
    """storage_state 쿠키 한 개를 Playwright 가 받는 형태로 정규화. 못 살리면 None.

    Playwright new_context(storage_state=) 는 쿠키마다 url 또는 (domain, path)
    쌍을 요구한다(둘을 섞으면 거부). 브라우저/확장 내보내기가 흔히 빠뜨리는
    path 를 "/" 로 채우고 domain 이 있으면 domain/path 형식으로 통일한다.
    name 이 없거나 url·domain 둘 다 없으면 못 살린다.
    """
    if not isinstance(cookie, dict):
        return None
    name = cookie.get("name")
    if not isinstance(name, str) or not name:
        return None
    fixed = dict(cookie)
    if fixed.get("domain"):
        fixed.pop("url", None)            # domain/path 와 url 혼용 금지
        if not fixed.get("path"):
            fixed["path"] = "/"
        return fixed
    if fixed.get("url"):
        return fixed
    return None


def normalize_storage_state(data: dict, *, strict: bool) -> dict:
    """storage_state 의 cookies 를 Playwright 가 받는 형태로 정규화해 반환.

    각 쿠키는 url 또는 (domain, path) 쌍이 있어야 new_context 가 받는다
    (`_normalize_cookie`). strict=True(입력 검증)면 못 살리는 쿠키에서
    CredentialError 를 던지고, strict=False(저장된 자격증명 소비)면 그 쿠키만
    버리고 진행한다 — 한 쿠키 때문에 아카이빙 전체가 깨지지 않게.
    """
    cookies = data.get("cookies")
    if not isinstance(cookies, list):
        if strict:
            raise CredentialError("세션 상태의 cookies 는 목록이어야 합니다.")
        return data
    normalized: list[dict] = []
    for cookie in cookies:
        fixed = _normalize_cookie(cookie)
        if fixed is None:
            if strict:
                name = cookie.get("name") if isinstance(cookie, dict) else None
                where = f" ('{name}')" if isinstance(name, str) and name else ""
                raise CredentialError(
                    f"세션 쿠키{where}에 domain 또는 url 이 없습니다 "
                    "(쿠키마다 domain 이 있어야 합니다)."
                )
            continue
        normalized.append(fixed)
    result = dict(data)
    result["cookies"] = normalized
    return result


# ---- HAR(브라우저 네트워크 기록) → storage_state 변환 ----
#
# 브라우저 개발자도구·확장이 내보내는 HAR(HTTP Archive, JSON)에서 쿠키를 뽑아
# Playwright storage_state 로 만든다. 세션 상태를 손으로 추출(JSON 붙여넣기)하는
# 대신, 로그인한 상태로 기록한 HAR 을 올리면 같은 결과를 얻는다. HAR 에는
# localStorage 가 없으므로 origins 는 비우고 쿠키만 추출한다.

# HAR sameSite 표기 → Playwright sameSite 표기. 모르는 값은 버린다(선택 속성).
_SAMESITE_MAP = {
    "strict": "Strict",
    "lax": "Lax",
    "none": "None",
    "no_restriction": "None",
    "unspecified": None,
}


def _parse_har_expires(value: object) -> float | None:
    """HAR 쿠키 expires(ISO 8601 문자열)를 unix 초로 변환. 세션·해석불가면 None.

    타임존이 없는 값은 UTC 로 본다 — HAR 스펙은 오프셋을 요구하지만, 누락한
    비표준 내보내기에서 서버 로컬 타임존에 따라 만료가 흔들리지 않게 고정한다.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):              # 'Z'(UTC)를 fromisoformat 가 받는 형태로
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _har_cookie_to_pw(cookie: object, fallback_domain: str) -> dict | None:
    """HAR 쿠키 한 개를 Playwright 쿠키 dict 로 변환. 못 살리면 None.

    domain 이 없으면 요청 URL 의 호스트(fallback_domain)로 채운다. name·domain 이
    모두 없으면 못 살린다. value 가 빈 쿠키(삭제된 쿠키)는 호출부가 거른다.
    """
    if not isinstance(cookie, dict):
        return None
    name = cookie.get("name")
    if not isinstance(name, str) or not name:
        return None
    domain = cookie.get("domain") or fallback_domain
    if not domain:
        return None
    pw: dict = {
        "name": name,
        "value": cookie.get("value") if isinstance(cookie.get("value"), str) else "",
        "domain": domain,
        "path": cookie.get("path") or "/",
    }
    expires = _parse_har_expires(cookie.get("expires"))
    if expires is not None:
        pw["expires"] = expires
    if isinstance(cookie.get("httpOnly"), bool):
        pw["httpOnly"] = cookie["httpOnly"]
    if isinstance(cookie.get("secure"), bool):
        pw["secure"] = cookie["secure"]
    same_site = cookie.get("sameSite")
    if isinstance(same_site, str):
        mapped = _SAMESITE_MAP.get(same_site.lower(), None)
        if mapped is not None:
            pw["sameSite"] = mapped
    return pw


def _cookies_from_header(value: object, fallback_domain: str) -> list[dict]:
    """요청 Cookie 헤더("a=1; b=2")를 Playwright 쿠키 목록으로 파싱(폴백용).

    HAR 의 cookies 배열을 채우지 않는 내보내기 도구를 위해, 배열이 비었을 때만
    쓴다. 헤더에는 속성이 없으므로 name·value·domain·path 만 채운다.
    """
    if not isinstance(value, str) or not value.strip():
        return []
    out: list[dict] = []
    for part in value.split(";"):
        if "=" not in part:
            continue
        name, _, val = part.strip().partition("=")
        name = name.strip()
        if name and fallback_domain:
            out.append({"name": name, "value": val.strip(),
                        "domain": fallback_domain, "path": "/"})
    return out


def _header_value(headers: object, target: str) -> str | None:
    """HAR headers 목록에서 이름(대소문자 무시)에 맞는 첫 값 반환."""
    if not isinstance(headers, list):
        return None
    for h in headers:
        if isinstance(h, dict) and str(h.get("name", "")).lower() == target:
            value = h.get("value")
            return value if isinstance(value, str) else None
    return None


def _base_domain(host: str) -> str:
    """호스트의 등록 가능 도메인 근사 — 마지막 두 레이블 (IP·단일 레이블은 그대로).

    공개 접미사 목록(PSL)이 없어 정확하진 않지만(co.uk 등 과대 매칭 가능),
    같은 조직의 서브도메인·CDN·SSO 쿠키를 함께 남기는 스코프로는 충분하다.
    """
    host = host.strip(".").lower()
    if not host:
        return ""
    try:                                  # IP 리터럴은 그대로
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    return ".".join(labels[-2:])


def _cookie_in_site_scope(cookie_domain: str, base: str) -> bool:
    """쿠키 도메인이 대상 사이트의 등록 도메인(base) 범위에 드는지."""
    d = (cookie_domain or "").strip(".").lower()
    return bool(base) and (d == base or d.endswith("." + base))


def storage_state_from_har(raw: str | bytes, *, site_host: str | None = None) -> dict:
    """HAR(JSON)에서 쿠키를 추출해 Playwright storage_state dict 로 변환.

    반환: {"cookies": [...], "origins": []}. 항목을 시간순(HAR 기록 순서)으로
    훑어 같은 (name, domain, path) 쿠키는 마지막 값으로 갱신하고(세션 종료
    시점의 상태), 최종 값이 빈 쿠키(로그아웃 등으로 삭제된 쿠키)는 버린다.
    각 항목의 cookies 배열을 우선 쓰고, 배열이 비었으면 요청 Cookie 헤더로
    폴백한다. 쿠키를 하나도 못 찾으면 CredentialError 를 던진다.

    site_host 를 주면 그 사이트의 등록 도메인(`_base_domain`) 범위 쿠키만
    남긴다 — HAR 에 섞인 무관한 서드파티 쿠키(애널리틱스·다른 탭 세션 등)가
    자격증명에 빨려 들어가 캡처 때 그 서드파티로 흘러가는 것을 막는다
    (프로젝트의 origin 스코프 원칙과 일관). 그 도메인 쿠키가 하나도 없으면
    명확한 오류를 던진다 — 외부 IdP(다른 등록 도메인) SSO 처럼 범위를 벗어난
    경우엔 storage_state JSON 을 직접 붙여넣으면 된다.
    """
    if isinstance(raw, (bytes, bytearray)):
        if len(raw) > MAX_HAR_BYTES:
            raise CredentialError("HAR 파일이 너무 큽니다.")
        try:
            text = bytes(raw).decode("utf-8")
        except UnicodeDecodeError:
            raise CredentialError("HAR 파일을 UTF-8 로 읽을 수 없습니다.") from None
    else:
        if len(raw.encode("utf-8")) > MAX_HAR_BYTES:
            raise CredentialError("HAR 파일이 너무 큽니다.")
        text = raw
    try:
        har = json.loads(text)
    except json.JSONDecodeError:
        raise CredentialError("HAR 파일이 올바른 JSON 이 아닙니다.") from None
    log = har.get("log") if isinstance(har, dict) else None
    entries = log.get("entries") if isinstance(log, dict) else None
    if not isinstance(entries, list):
        raise CredentialError("올바른 HAR 파일이 아닙니다 (log.entries 가 없습니다).")

    # (name, domain, path) → 최종 쿠키. 시간순으로 마지막 값이 남는다.
    jar: dict[tuple[str, str, str], dict] = {}

    def _apply(cookies: object, fallback_domain: str) -> None:
        if not isinstance(cookies, list):
            return
        for c in cookies:
            pw = _har_cookie_to_pw(c, fallback_domain)
            if pw is not None:
                jar[(pw["name"], pw["domain"], pw["path"])] = pw

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        request = entry.get("request") if isinstance(entry.get("request"), dict) else {}
        response = entry.get("response") if isinstance(entry.get("response"), dict) else {}
        host = urlsplit(request.get("url", "") if isinstance(request.get("url"), str) else "").hostname or ""
        # 요청 쿠키(브라우저가 보낸 현재 상태) → 응답 쿠키(서버가 갱신/설정) 순으로
        # 적용해 응답이 우선한다. 배열이 비었으면 Cookie 헤더로 폴백.
        req_cookies = request.get("cookies")
        if not (isinstance(req_cookies, list) and req_cookies):
            req_cookies = _cookies_from_header(_header_value(request.get("headers"), "cookie"), host)
        _apply(req_cookies, host)
        _apply(response.get("cookies"), host)

    cookies = [c for c in jar.values() if c["value"] != ""]
    if not cookies:
        raise CredentialError("HAR 파일에서 쿠키를 찾지 못했습니다.")
    if site_host:
        base = _base_domain(site_host)
        scoped = [c for c in cookies if _cookie_in_site_scope(c["domain"], base)]
        if not scoped:
            raise CredentialError(
                "HAR 파일에 이 사이트 도메인의 쿠키가 없습니다 "
                "(다른 도메인 쿠키만 있어 가져오지 않았습니다)."
            )
        cookies = scoped
    return {"cookies": cookies, "origins": []}


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
        # 쿠키마다 domain/path(또는 url)를 갖추도록 정규화·검증 — 빠진 path 는
        # 채우고, domain·url 모두 없는 쿠키는 명확한 오류로 거른다 (Playwright
        # new_context 가 "Cookie should have a url or a domain/path pair" 로
        # 캡처 전체를 깨뜨리는 것을 입력 단계에서 차단).
        data = normalize_storage_state(data, strict=True)
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
    ttl_seconds: int | None = None,
) -> int:
    """payload 를 암호화해 자격증명을 저장하고 id 반환.

    crypto 키 미설정이면 crypto.SecretKeyMissing 이 전파된다(호출부가 처리).
    ttl_seconds 가 있으면 만료가 설정된 1회성(확장) 자격증명으로 저장된다.
    """
    secret = crypto.encrypt(json.dumps(payload, ensure_ascii=False))
    return db.create_site_credential(
        conn, site_id, label, kind, secret, created_by=created_by,
        ttl_seconds=ttl_seconds,
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
    kind = row["kind"]
    payload = json.loads(crypto.decrypt(row["secret"]))
    # 입력 검증 전에 저장됐거나 외부에서 들어온 storage_state 도 캡처가 받도록
    # 쿠키를 정규화한다 — 못 살리는 쿠키는 버리고 나머지로 진행 (strict=False).
    if kind == KIND_SESSION and isinstance(payload.get("storage_state"), dict):
        payload["storage_state"] = normalize_storage_state(
            payload["storage_state"], strict=False
        )
    return kind, payload


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
