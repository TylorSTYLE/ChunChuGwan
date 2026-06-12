"""netcheck — 호스트 네트워크 대역 판정 테스트."""
import ipaddress

import pytest

from chunchugwan import netcheck


# ---- IP 리터럴 (DNS 없이 판정) ----


@pytest.mark.parametrize("host", ["127.0.0.1", "127.8.9.10", "::1", "0.0.0.0"])
def test_loopback_literals(host):
    assert netcheck.classify_host(host) == netcheck.LOOPBACK


@pytest.mark.parametrize(
    "host",
    ["192.168.0.10", "10.1.2.3", "172.16.5.5", "169.254.1.1", "fd00::1", "fe80::1"],
)
def test_private_literals(host):
    assert netcheck.classify_host(host) == netcheck.PRIVATE


@pytest.mark.parametrize("host", ["8.8.8.8", "1.1.1.1", "2606:4700::1111"])
def test_public_literals(host):
    assert netcheck.classify_host(host) == netcheck.PUBLIC


def test_ipv4_mapped_ipv6_unwrapped():
    """IPv4-mapped IPv6 는 IPv4 로 풀어서 판정한다."""
    assert netcheck.classify_host("::ffff:192.168.0.1") == netcheck.PRIVATE
    assert netcheck.classify_host("::ffff:127.0.0.1") == netcheck.LOOPBACK


# ---- localhost 계열 ----


@pytest.mark.parametrize("host", ["localhost", "LOCALHOST", "foo.localhost", "localhost."])
def test_localhost_names(host):
    assert netcheck.classify_host(host) == netcheck.LOOPBACK


def test_empty_host_is_public():
    assert netcheck.classify_host("") == netcheck.PUBLIC


# ---- 호스트명 (모킹된 해석) ----


def _ips(*addrs):
    return tuple(ipaddress.ip_address(a) for a in addrs)


def test_hostname_resolving_to_private(monkeypatch):
    monkeypatch.setattr(netcheck, "_resolve", lambda host: _ips("192.168.1.7"))
    assert netcheck.classify_host("nas.home.lan") == netcheck.PRIVATE


def test_hostname_resolving_to_public(monkeypatch):
    monkeypatch.setattr(netcheck, "_resolve", lambda host: _ips("93.184.216.34"))
    assert netcheck.classify_host("example.com") == netcheck.PUBLIC


def test_mixed_records_pick_most_restrictive(monkeypatch):
    """공인 + 루프백이 섞이면 루프백 — DNS 리바인딩식 우회 방지."""
    monkeypatch.setattr(
        netcheck, "_resolve", lambda host: _ips("93.184.216.34", "127.0.0.1")
    )
    assert netcheck.classify_host("tricky.example") == netcheck.LOOPBACK


def test_unresolved_hostname_is_public(monkeypatch):
    """해석 실패는 공인 취급 — 캡처도 같은 리졸버라 어차피 실패한다."""
    monkeypatch.setattr(netcheck, "_resolve", lambda host: ())
    assert netcheck.classify_host("no-such-host.invalid") == netcheck.PUBLIC


def test_resolution_is_cached(monkeypatch):
    calls = []

    def fake_resolve(host):
        calls.append(host)
        return _ips("10.0.0.5")

    monkeypatch.setattr(netcheck, "_resolve", fake_resolve)
    assert netcheck.classify_host("nas.home.lan") == netcheck.PRIVATE
    assert netcheck.classify_host("nas.home.lan") == netcheck.PRIVATE
    assert calls == ["nas.home.lan"]


# ---- URL 헬퍼 ----


def test_classify_url():
    assert netcheck.classify_url("http://192.168.0.1:8080/admin") == netcheck.PRIVATE
    assert netcheck.classify_url("http://127.0.0.1:8765/") == netcheck.LOOPBACK
    assert netcheck.classify_url("https://[::1]/x") == netcheck.LOOPBACK
