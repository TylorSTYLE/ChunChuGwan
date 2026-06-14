"""캡처 엔진 seam + launch/UA 옵션 단위 테스트 (browser_engine.py / capture.py).

브라우저를 띄우지 않고 설정 분기만 검증한다.
"""
from chunchugwan import browser_engine, capture, config


def test_default_engine_is_playwright(monkeypatch):
    monkeypatch.setattr(config, "CAPTURE_ENGINE", "playwright")
    name, sync_playwright, error_cls, timeout_cls = browser_engine.get_engine()
    assert name == "playwright"
    assert callable(sync_playwright)
    assert issubclass(timeout_cls, BaseException)
    assert issubclass(error_cls, BaseException)


def test_patchright_request_falls_back_when_missing(monkeypatch):
    # patchright 미설치 환경이면 playwright 로 폴백, 설치돼 있으면 patchright.
    monkeypatch.setattr(config, "CAPTURE_ENGINE", "patchright")
    name, *_ = browser_engine.get_engine()
    assert name in ("patchright", "playwright")


def test_context_user_agent_default_is_fixed_ua(monkeypatch):
    monkeypatch.setattr(config, "CAPTURE_HEADFUL", False)
    assert capture._context_user_agent() == config.USER_AGENT


def test_context_user_agent_headful_drops_override(monkeypatch):
    # 헤드풀 스텔스에선 real Chrome 기본 UA 를 쓰도록 오버라이드 해제
    monkeypatch.setattr(config, "CAPTURE_HEADFUL", True)
    monkeypatch.setattr(config, "CAPTURE_FORCE_USER_AGENT", False)
    assert capture._context_user_agent() is None


def test_context_user_agent_headful_forced(monkeypatch):
    monkeypatch.setattr(config, "CAPTURE_HEADFUL", True)
    monkeypatch.setattr(config, "CAPTURE_FORCE_USER_AGENT", True)
    assert capture._context_user_agent() == config.USER_AGENT
