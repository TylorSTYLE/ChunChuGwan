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


# ---- _launch 채널 폴백 (브라우저 없이 가짜 playwright 로 검증) ----

class _FakeChromium:
    def __init__(self, fail_on_channel=False):
        self.calls = []
        self.fail_on_channel = fail_on_channel

    def launch(self, **kwargs):
        self.calls.append(kwargs)
        if "channel" in kwargs and self.fail_on_channel:
            raise RuntimeError("Chromium distribution 'chrome' is not found")
        return ("browser", kwargs)


class _FakeP:
    def __init__(self, chromium):
        self.chromium = chromium


def test_launch_no_channel_is_headless_default(monkeypatch):
    monkeypatch.setattr(config, "CAPTURE_CHANNEL", "")
    monkeypatch.setattr(config, "CAPTURE_HEADFUL", False)
    monkeypatch.setattr(capture, "_channel_fallback", False)
    chromium = _FakeChromium()
    capture._launch(_FakeP(chromium))
    assert chromium.calls == [{"headless": True, "args": []}]


def test_launch_with_channel_passes_channel(monkeypatch):
    monkeypatch.setattr(config, "CAPTURE_CHANNEL", "chrome")
    monkeypatch.setattr(config, "CAPTURE_HEADFUL", True)
    monkeypatch.setattr(capture, "_channel_fallback", False)
    chromium = _FakeChromium(fail_on_channel=False)
    capture._launch(_FakeP(chromium))
    assert chromium.calls[0].get("channel") == "chrome"
    assert chromium.calls[0]["headless"] is False


def test_launch_channel_missing_falls_back_and_sticks(monkeypatch):
    # arm64 처럼 real Chrome 이 없으면: 첫 채널 시도 실패 → 번들 chromium 폴백,
    # 이후로는 채널 시도조차 안 한다 (실패-재시도 비용 1회만).
    monkeypatch.setattr(config, "CAPTURE_CHANNEL", "chrome")
    monkeypatch.setattr(config, "CAPTURE_HEADFUL", False)
    monkeypatch.setattr(capture, "_channel_fallback", False)
    chromium = _FakeChromium(fail_on_channel=True)
    p = _FakeP(chromium)

    capture._launch(p)
    assert chromium.calls[0].get("channel") == "chrome"   # 채널 시도
    assert "channel" not in chromium.calls[1]              # 번들로 폴백
    assert capture._channel_fallback is True

    chromium.calls.clear()
    capture._launch(p)
    assert chromium.calls == [{"headless": True, "args": []}]  # 더는 채널 시도 안 함


# ---- capture_mode_str (진단 로그) ----

def test_capture_mode_str_headless_default(monkeypatch):
    monkeypatch.setattr(config, "CAPTURE_ENGINE", "playwright")
    monkeypatch.setattr(config, "CAPTURE_HEADFUL", False)
    monkeypatch.setattr(config, "CAPTURE_CHANNEL", "")
    monkeypatch.setattr(capture, "_channel_fallback", False)
    s = capture.capture_mode_str()
    assert "playwright" in s and "headless" in s and "channel=-" in s


def test_capture_mode_str_stealth(monkeypatch):
    monkeypatch.setattr(config, "CAPTURE_HEADFUL", True)
    monkeypatch.setattr(config, "CAPTURE_CHANNEL", "chrome")
    monkeypatch.setattr(capture, "_channel_fallback", False)
    s = capture.capture_mode_str()
    assert "headful" in s and "channel=chrome" in s


def test_capture_mode_str_shows_fallback(monkeypatch):
    monkeypatch.setattr(config, "CAPTURE_CHANNEL", "chrome")
    monkeypatch.setattr(capture, "_channel_fallback", True)
    assert "폴백" in capture.capture_mode_str()
