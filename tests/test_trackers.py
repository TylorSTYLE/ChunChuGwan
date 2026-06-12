"""추적기 자동 제거 테스트. 로컬 HTTP 서버 + headless Chromium."""
import re
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest

from chunchugwan import capture, trackers

# 추적기 스크립트가 포함된 픽스처 HTML.
# 외부 스크립트 src 는 로컬 서버 경로로 제공하되, 추적기 도메인 문자열을
# 경로에 포함시켜 EXTERNAL_SELECTORS 의 *=(부분문자열) 매칭이 동작하도록 함.
TRACKER_HTML = """\
<!doctype html>
<html><head>
  <meta charset="utf-8"><title>tracker test</title>
  <!-- 외부 추적기 스크립트 (src 경로에 도메인 문자열 포함) -->
  <script async src="/t/googletagmanager.com/gtag/js?id=G-TEST"></script>
  <script async src="/t/connect.facebook.net/fbevents.js"></script>
  <script async src="/t/static.hotjar.com/c/hotjar.js"></script>
  <!-- GA4 인라인 초기화 -->
  <script>window.dataLayer=window.dataLayer||[];function gtag(){dataLayer.push(arguments);}gtag('js',new Date());gtag('config','G-TEST');</script>
  <!-- GTM 인라인 loader -->
  <script>(function(w,d,s,l,i){w[l]=w[l]||[];w[l].push({'gtm.start':new Date().getTime(),event:'gtm.js'});})(window,document,'script','dataLayer','GTM-TEST');</script>
  <!-- Facebook Pixel 인라인 -->
  <script>!function(f,b,e,v,n,t,s){n=f.fbq=function(){};}(window);fbq('init','FAKE');fbq('track','PageView');</script>
</head><body>
  <!-- GTM noscript fallback -->
  <noscript><iframe src="https://www.googletagmanager.com/ns.html?id=GTM-TEST" height="0" width="0"></iframe></noscript>
  <h1>추적기 테스트</h1>
  <p>아카이빙 테스트 콘텐츠</p>
</body></html>
"""


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path.startswith("/t/"):
            # 추적기 스크립트 URL — 빈 JS 로 200 응답
            body = b""
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            super().do_GET()


@pytest.fixture
def tracker_site_url(tmp_path):
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text(TRACKER_HTML, encoding="utf-8")

    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(_QuietHandler, directory=str(site))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/index.html"
    server.shutdown()
    thread.join(timeout=5)


def test_trackers_removed_from_page_html(tracker_site_url, tmp_path):
    """추적기 스크립트가 page.html 에서 제거되고 raw.html 에는 원본이 유지된다."""
    out = tmp_path / "out"
    out.mkdir()
    capture.capture(tracker_site_url, out)

    raw = (out / "raw.html").read_text(encoding="utf-8")
    page_html = (out / "page.html").read_text(encoding="utf-8")

    # raw.html 에는 원본 그대로 남아있어야 한다
    assert "googletagmanager.com" in raw
    assert "connect.facebook.net" in raw
    assert "static.hotjar.com" in raw

    # page.html 에서는 외부 추적기 스크립트 src 가 사라져야 한다
    assert "googletagmanager.com/gtag" not in page_html
    assert "connect.facebook.net" not in page_html
    assert "static.hotjar.com" not in page_html

    # 본문은 유지
    assert "아카이빙 테스트 콘텐츠" in page_html


def test_inline_tracker_scripts_removed(tracker_site_url, tmp_path):
    """인라인 추적기 초기화 코드가 page.html 에서 제거된다."""
    out = tmp_path / "out"
    out.mkdir()
    capture.capture(tracker_site_url, out)

    raw = (out / "raw.html").read_text(encoding="utf-8")
    page_html = (out / "page.html").read_text(encoding="utf-8")

    # raw.html 에는 인라인 코드가 남아있어야 한다
    assert "gtm.start" in raw
    assert "gtag('config'" in raw
    assert "fbq('init'" in raw

    # page.html 에서는 제거되어야 한다
    assert "gtm.start" not in page_html
    assert "gtag('config'" not in page_html
    assert "fbq('init'" not in page_html


def test_noscript_tracker_removed(tracker_site_url, tmp_path):
    """GTM noscript 폴백이 page.html 에서 제거된다."""
    out = tmp_path / "out"
    out.mkdir()
    capture.capture(tracker_site_url, out)

    raw = (out / "raw.html").read_text(encoding="utf-8")
    page_html = (out / "page.html").read_text(encoding="utf-8")

    assert "googletagmanager.com/ns.html" in raw
    assert "googletagmanager.com/ns.html" not in page_html


def test_raw_html_always_preserved(tracker_site_url, tmp_path):
    """raw.html 은 추적기 제거 후에도 원본 내용을 온전히 보존한다."""
    out = tmp_path / "out"
    out.mkdir()
    capture.capture(tracker_site_url, out)

    raw = (out / "raw.html").read_text(encoding="utf-8")
    # 추적기 제거가 raw.html 을 오염시키지 않아야 한다
    assert "googletagmanager.com" in raw
    assert "fbq('init'" in raw
    assert "gtm.start" in raw
    assert "아카이빙 테스트 콘텐츠" in raw


def test_inline_patterns_are_valid_regexps():
    """INLINE_PATTERNS 의 모든 항목이 유효한 정규식이어야 한다."""
    for pattern in trackers.INLINE_PATTERNS:
        re.compile(pattern)  # 예외 없으면 통과


def test_external_selectors_nonempty():
    assert len(trackers.EXTERNAL_SELECTORS) > 0
    assert len(trackers.INLINE_PATTERNS) > 0
