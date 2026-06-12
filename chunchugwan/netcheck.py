"""호스트의 네트워크 대역 판정 — 루프백 / 사설(로컬 네트워크) / 공인.

로컬 네트워크 아카이빙 게이트가 사용한다 (pipeline·crawler·웹 폼 공유):
- 루프백(127.0.0.0/8, ::1, localhost, 0.0.0.0)은 항상 아카이빙 금지 —
  대시보드 자신 같은 로컬 서비스가 아카이브로 새는 것을 막는다.
- 사설 대역(RFC1918·링크 로컬·ULA 등)은 시스템 설정의 로컬 네트워크
  태그를 지정해야 아카이빙할 수 있다.

판정은 이 서버의 DNS 해석 기준이다 — 같은 이름이라도 서버가 LAN 안에
있을 때만 사설 IP 로 풀릴 수 있다. 해석 실패는 공인으로 취급한다
(캡처도 같은 리졸버를 쓰므로 어차피 실패한다). 해석 결과는 잠시
캐시한다 — 스케줄·크롤이 같은 호스트를 반복 판정하는 비용을 줄인다.
"""

from __future__ import annotations

import ipaddress
import socket
import threading
import time

LOOPBACK = "loopback"   # 항상 아카이빙 금지
PRIVATE = "private"     # 로컬 네트워크 태그 필요
PUBLIC = "public"       # 제한 없음

_IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

_CACHE_TTL_SECONDS = 300
_cache: dict[str, tuple[float, str]] = {}
_cache_lock = threading.Lock()


def _resolve(host: str) -> tuple[_IPAddress, ...]:
    """호스트명 → IP 목록 (서버 리졸버 기준). 해석 실패는 빈 튜플."""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, OSError):
        return ()
    ips: list[_IPAddress] = []
    for info in infos:
        try:
            ips.append(ipaddress.ip_address(info[4][0]))
        except ValueError:
            continue
    return tuple(ips)


def _classify_ip(ip: _IPAddress) -> str:
    """IP 하나의 대역 판정. IPv4-mapped IPv6 는 IPv4 로 풀어서 본다."""
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    # 0.0.0.0 은 브라우저가 로컬 머신으로 해석한다 — 루프백과 동급으로 금지
    if ip.is_loopback or ip.is_unspecified:
        return LOOPBACK
    if ip.is_private or ip.is_link_local:
        return PRIVATE
    return PUBLIC


def _classify_ips(ips: tuple[_IPAddress, ...]) -> str:
    """IP 목록의 종합 판정 — 가장 제한적인 대역을 따른다."""
    kinds = {_classify_ip(ip) for ip in ips}
    if LOOPBACK in kinds:
        return LOOPBACK
    if PRIVATE in kinds:
        return PRIVATE
    return PUBLIC


def classify_host(host: str) -> str:
    """호스트(urlsplit().hostname — 소문자, 괄호 제거됨)의 대역 판정.

    IP 리터럴과 localhost 계열은 DNS 없이 즉시 판정하고, 호스트명은
    해석 결과를 TTL 캐시한다. 해석 실패는 PUBLIC.
    """
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return PUBLIC
    if host == "localhost" or host.endswith(".localhost"):
        return LOOPBACK
    try:
        return _classify_ip(ipaddress.ip_address(host))
    except ValueError:
        pass

    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(host)
        if hit is not None and hit[0] > now:
            return hit[1]
    ips = _resolve(host)
    kind = _classify_ips(ips) if ips else PUBLIC
    with _cache_lock:
        _cache[host] = (now + _CACHE_TTL_SECONDS, kind)
    return kind


def classify_url(url: str) -> str:
    """URL 의 호스트 대역 판정 — classify_host 위임."""
    from urllib.parse import urlsplit

    return classify_host(urlsplit(url).hostname or "")
