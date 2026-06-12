"""공용 픽스처."""
import pytest

from chunchugwan import netcheck


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
