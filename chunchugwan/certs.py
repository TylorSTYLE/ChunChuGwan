"""사이트 TLS 인증서 수집·파싱 — https 아카이빙의 부가 기록.

https 페이지를 아카이빙할 때 서버의 리프 인증서를 받아 파싱하고
(site_certificates 테이블 — db.py), 인증서가 갱신되면 새 버전 행을
추가한다. 버전 식별자는 인증서 DER 의 sha256 지문 — 같은 인증서를 다시
보면 last_seen_at 만 갱신되고, 다른 인증서(갱신·교체)는 새 행이 된다.
이전 버전 행은 남아 버전별 이력이 보존된다.

수집은 캡처와 별도의 TLS 핸드셰이크로 한다 — Playwright 는 서버 인증서를
노출하지 않는다. 검증 없이(CERT_NONE) 인증서 바이트만 받는다: 검증
통과 여부는 캡처 결과(insecure_tls)가 이미 알고 있고, 자체 서명
사이트의 인증서도 기록해야 하기 때문이다. 수집·파싱 실패는 아카이빙을
막지 않는다 (호출부가 None 처리).
"""

from __future__ import annotations

import json
import logging
import socket
import ssl
from urllib.parse import urlsplit

from . import config

logger = logging.getLogger(__name__)


def fetch_certificate_info(url: str) -> dict | None:
    """https URL 호스트의 리프 인증서를 받아 파싱한 dict 반환 (실패 시 None).

    반환 키: host, fingerprint, subject, issuer, serial, san(JSON 문자열),
    not_before, not_after, signature_algorithm, pem.
    """
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        return None
    host, port = parts.hostname, parts.port or 443
    try:
        der = _fetch_der(host, port)
        info = parse_certificate(der)
    except Exception as e:
        logger.warning("인증서 수집 실패(건너뜀): %s — %s", parts.netloc, e)
        return None
    info["host"] = parts.netloc
    return info


def _fetch_der(host: str, port: int) -> bytes:
    """TLS 핸드셰이크로 서버 리프 인증서 DER 바이트를 받는다.

    검증 없이 받는다 (모듈 docstring 참조) — SNI 는 호스트명으로 보낸다.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection(
        (host, port), timeout=config.HTTPS_PROBE_TIMEOUT_SECONDS
    ) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as tls:
            der = tls.getpeercert(binary_form=True)
    if not der:
        raise ValueError("서버가 인증서를 제시하지 않음")
    return der


def parse_certificate(der: bytes) -> dict:
    """인증서 DER → 표시·저장용 필드 dict.

    지문(sha256)이 버전 식별자다. PEM 원문도 함께 담아 보관·다운로드에 쓴다.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization

    cert = x509.load_der_x509_certificate(der)
    try:
        san_ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        san = [_general_name_str(n) for n in san_ext.value]
    except x509.ExtensionNotFound:
        san = []
    try:
        sig_alg = cert.signature_hash_algorithm.name
    except Exception:
        sig_alg = ""  # Ed25519 등 해시 분리형 서명
    return {
        "fingerprint": cert.fingerprint(hashes.SHA256()).hex(),
        "subject": cert.subject.rfc4514_string(),
        "issuer": cert.issuer.rfc4514_string(),
        "serial": format(cert.serial_number, "x"),
        "san": json.dumps(san, ensure_ascii=False),
        "not_before": cert.not_valid_before_utc.isoformat(timespec="seconds"),
        "not_after": cert.not_valid_after_utc.isoformat(timespec="seconds"),
        "signature_algorithm": sig_alg,
        "pem": cert.public_bytes(serialization.Encoding.PEM).decode("ascii"),
    }


def _general_name_str(name) -> str:
    """SAN 항목 → 표시 문자열 (DNS 명, IP 등)."""
    value = getattr(name, "value", name)
    return str(value)
