"""봇 차단/사람 확인 챌린지 감지 — 저장·해시 오염 방지 (capture.py)."""
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from chunchugwan import capture, config

_CF_HTML = (
    "<!doctype html><html><head><title>Just a moment...</title></head>"
    "<body><div class='cf-turnstile'></div>"
    "<script src='/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1'></script>"
    "</body></html>"
)


# ---- 순수 함수: challenge_reason (브라우저 불필요) ----

def test_reason_detects_cloudflare_interstitial():
    assert capture.challenge_reason(_CF_HTML, 403, "https://x/", "Just a moment...")


def test_reason_detects_sciencedirect_block():
    html = ("<html><body><p>There was a problem providing the content you "
            "requested</p></body></html>")
    reason = capture.challenge_reason(
        html, 200, "https://www.sciencedirect.com/science/article/pii/X", "ScienceDirect"
    )
    assert reason is not None


def test_reason_widget_marker_blocks_on_error_status():
    # 약한 위젯 마커(Turnstile iframe)라도 응답이 차단(4xx/5xx)이면 차단으로 본다
    html = "<html><body><iframe src='https://challenges.cloudflare.com/...'></iframe></body></html>"
    assert capture.challenge_reason(html, 403, "https://x/", None)


def test_reason_embedded_turnstile_on_normal_page_passes():
    # 정상 200 페이지에 폼 스팸방지용으로 박힌 Turnstile 위젯은 오탐하지 않는다
    # (damoang.net/new 등 그누보드 계열 커뮤니티 — 본문은 멀쩡하다)
    html = (
        "<html><head><title>새글 - 다모앙</title></head><body>"
        "<ul class='new-list'><li>게시글 1</li><li>게시글 2</li></ul>"
        "<form id='search'><div class='cf-turnstile'></div>"
        "<script src='https://challenges.cloudflare.com/turnstile/v0/api.js'></script>"
        "</form></body></html>"
    )
    assert capture.challenge_reason(html, 200, "https://damoang.net/new", "새글 - 다모앙") is None
    # 단, 상태 미상(None — 통과 대기/라이브 재검사 폴링)에서는 보수적으로 차단으로
    # 둔다 (그 경로는 이미 진짜 챌린지로 진입한 뒤라, 위젯이 남아 있으면 미통과)
    assert capture.challenge_reason(html, None, "https://damoang.net/new", "새글 - 다모앙")


def test_reason_passes_normal_page():
    html = ("<html><head><title>본문 제목</title></head><body>"
            "<article>정상적인 본문 내용입니다.</article></body></html>")
    assert capture.challenge_reason(html, 200, "https://example.com/", "본문 제목") is None


def test_reason_status_alone_is_not_challenge():
    # 마커 없는 403/503 은 차단으로 보지 않는다 (정상 콘텐츠일 수 있음)
    html = "<html><body><h1>403 Forbidden</h1><p>접근 권한이 없습니다.</p></body></html>"
    assert capture.challenge_reason(html, 403, "https://x/", "403") is None


# ---- 통합: 로컬 서버가 챌린지 HTML 을 403 으로 서빙 → 캡처가 실패·미저장 ----

class _ChallengeHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = _CF_HTML.encode()
        self.send_response(403)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # 테스트 출력 오염 방지
        pass


@pytest.fixture
def challenge_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ChallengeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/"
    server.shutdown()
    thread.join(timeout=5)


def test_capture_raises_and_leaves_no_artifacts(challenge_url, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(capture.CaptureChallengeError):
        capture.capture(challenge_url, out)
    # 차단 페이지는 절대 저장되지 않는다 (해시 오염 방지)
    assert not (out / "raw.html").exists()
    assert not (out / "page.html").exists()
    assert not (out / "screenshot.png").exists()


# ---- 챌린지 자동 통과 대기 루프 (스텔스 캡처) ----

# 처음엔 챌린지 마커를 보이다가 0.5초 뒤 JS 로 제거하고 본문을 드러낸다 —
# Cloudflare 비상호작용 챌린지가 몇 초 뒤 자동 통과하는 흐름을 흉내낸다.
_SELF_CLEARING_HTML = (
    "<!doctype html><html><head><title>Just a moment...</title></head>"
    "<body><div id='ch' class='cf-turnstile'></div>"
    "<article id='real' style='display:none'><h1>통과 후 실제 본문</h1></article>"
    "<script>setTimeout(function(){"
    "document.getElementById('ch').remove();"
    "document.title='실제 페이지';"
    "document.getElementById('real').style.display='block';"
    "}, 500);</script></body></html>"
)


class _SelfClearingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = _SELF_CLEARING_HTML.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def self_clearing_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SelfClearingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/"
    server.shutdown()
    thread.join(timeout=5)


def test_stealth_waits_and_captures_self_clearing_challenge(
    self_clearing_url, tmp_path, monkeypatch
):
    # 스텔스(patchright) 활성 → 챌린지가 풀릴 때까지 기다렸다가 캡처한다
    monkeypatch.setattr(config, "CAPTURE_ENGINE", "patchright")
    monkeypatch.setattr(config, "CHALLENGE_WAIT_SECONDS", 8)
    monkeypatch.setattr(config, "CHALLENGE_WAIT_POLL_MS", 300)
    out = tmp_path / "out"
    out.mkdir()
    capture.capture(self_clearing_url, out)   # 풀리므로 CaptureChallengeError 없음
    assert (out / "raw.html").exists()
    assert "통과 후 실제 본문" in (out / "raw.html").read_text(encoding="utf-8")


def test_stealth_times_out_on_hard_challenge(challenge_url, tmp_path, monkeypatch):
    # 안 풀리는 하드 블록은 대기 시간 초과 후 차단으로 실패한다
    monkeypatch.setattr(config, "CAPTURE_ENGINE", "patchright")
    monkeypatch.setattr(config, "CHALLENGE_WAIT_SECONDS", 1)
    monkeypatch.setattr(config, "CHALLENGE_WAIT_POLL_MS", 300)
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(capture.CaptureChallengeError):
        capture.capture(challenge_url, out)
    assert not (out / "raw.html").exists()


def test_no_stealth_fails_immediately_without_waiting(challenge_url, tmp_path, monkeypatch):
    # 헤드리스 기본 경로는 대기하지 않고 즉시 실패한다 (기존 동작)
    monkeypatch.setattr(config, "CAPTURE_ENGINE", "playwright")
    monkeypatch.setattr(config, "CAPTURE_HEADFUL", False)
    monkeypatch.setattr(config, "CHALLENGE_WAIT_SECONDS", 30)  # 커도 대기 안 함
    import time as _t
    out = tmp_path / "out"
    out.mkdir()
    t0 = _t.monotonic()
    with pytest.raises(capture.CaptureChallengeError):
        capture.capture(challenge_url, out)
    assert _t.monotonic() - t0 < 20  # 30s 대기를 타지 않았다
