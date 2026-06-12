"""공용 픽스처."""
import pytest

from chunchugwan import certs, netcheck, pipeline


@pytest.fixture(autouse=True)
def _no_dns(monkeypatch):
    """테스트가 실제 DNS 를 조회하지 않게 호스트명 해석을 차단한다.

    해석 실패(빈 튜플)는 공인 취급이라 example.com 같은 기존 픽스처
    URL 의 동작은 그대로다. IP 리터럴·localhost 판정은 해석 없이
    동작하므로 영향 없다. netcheck 자체 테스트는 _resolve 를 다시
    monkeypatch 해 시나리오를 만든다. 판정 캐시도 테스트 간 격리한다.
    """
    monkeypatch.setattr(netcheck, "_resolve", lambda host: ())
    netcheck._cache.clear()
    yield
    netcheck._cache.clear()


@pytest.fixture(autouse=True)
def _no_https_probe(monkeypatch):
    """테스트가 실제 https 승격 프로브(네트워크 요청)를 하지 않게 차단한다.

    기본은 미지원(False) — 명시적 http URL 픽스처가 그대로 http 로
    아카이빙되던 기존 동작을 유지한다. 승격 자체를 검증하는 테스트는
    pipeline._https_supported 를 다시 monkeypatch 한다.
    """
    monkeypatch.setattr(pipeline, "_https_supported", lambda url: False)


@pytest.fixture(autouse=True)
def _no_cert_fetch(monkeypatch):
    """테스트가 실제 TLS 핸드셰이크(인증서 수집)를 하지 않게 차단한다.

    인증서 기록을 검증하는 테스트는 certs.fetch_certificate_info 를 다시
    monkeypatch 해 파싱된 dict 를 주입한다.
    """
    monkeypatch.setattr(certs, "fetch_certificate_info", lambda url: None)
