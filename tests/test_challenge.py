"""봇 차단/사람 확인 챌린지 감지 — 저장·해시 오염 방지 (capture.py)."""
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from chunchugwan import capture

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


def test_reason_detects_turnstile_iframe_src():
    html = "<html><body><iframe src='https://challenges.cloudflare.com/...'></iframe></body></html>"
    assert capture.challenge_reason(html, None, "https://x/", None)


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
