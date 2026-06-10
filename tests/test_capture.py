"""capture.py 통합 테스트. 로컬 HTTP 서버 + headless chromium (외부 네트워크 없음)."""
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest
from PIL import Image

from chunchugwan import capture

INDEX_HTML = """<!doctype html>
<html><head>
  <meta charset="utf-8"><title>fixture</title>
  <link rel="stylesheet" href="style.css">
</head><body>
  <article><h1>본문 제목</h1><p>본문 내용입니다.</p></article>
  <div class="ad">광고 위젯 문구</div>
  <img src="img.png" alt="그림">
</body></html>
"""

STYLE_CSS = """@font-face { font-family: F; src: url('font.woff2'); }
body { color: #111; }
"""


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):  # 테스트 출력 오염 방지
        pass


@pytest.fixture
def site_url(tmp_path):
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    (site / "style.css").write_text(STYLE_CSS, encoding="utf-8")
    Image.new("RGB", (4, 4), (255, 0, 0)).save(site / "img.png")
    (site / "font.woff2").write_bytes(b"\x00fake-font-bytes")

    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(_QuietHandler, directory=str(site))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/index.html"
    server.shutdown()
    thread.join(timeout=5)


def test_capture_artifacts_and_inlining(site_url, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    result = capture.capture(site_url, out, remove_selectors=(".ad",))

    assert result.http_status == 200
    assert result.title == "fixture"
    assert (out / "screenshot.png").is_file()

    # raw.html / page.html(저장 산출물)에는 광고 노드가 그대로 남는다
    raw = (out / "raw.html").read_text(encoding="utf-8")
    assert "광고 위젯 문구" in raw

    page_html = (out / "page.html").read_text(encoding="utf-8")
    assert "data:image" in page_html              # 이미지 인라인
    assert "url(data:" in page_html               # 폰트 인라인
    assert 'rel="stylesheet"' not in page_html    # CSS는 <style>로 치환
    assert "광고 위젯 문구" in page_html

    # 추출용 content_html 에서만 셀렉터 제거
    assert "광고 위젯 문구" not in result.content_html
    assert "본문 내용입니다." in result.content_html


def test_capture_without_rules_keeps_content(site_url, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    result = capture.capture(site_url, out)
    assert "광고 위젯 문구" in result.content_html
