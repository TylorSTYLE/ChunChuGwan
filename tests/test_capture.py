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
  <a href="report.pdf">보고서 (PDF)</a>
  <a href="report.pdf#page=2">보고서 2쪽</a>
  <a href="other.html">다른 글</a>
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
    (site / "report.pdf").write_bytes(b"%PDF-1.4 fixture")

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
    # 잘못된 셀렉터(":::bad")는 무시되고 나머지는 정상 적용되어야 한다
    result = capture.capture(site_url, out, remove_selectors=(".ad", ":::bad"))

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
    # content_html 은 인라인 전 raw_html 기준 — base64 데이터가 섞이면 안 된다
    assert "data:image" not in result.content_html
    assert "url(data:" not in result.content_html

    # 문서 링크 수집 — 절대 URL 로 수집되고, fragment 만 다른 링크는
    # 같은 문서로 합쳐지며, HTML 등 비문서 링크는 제외된다
    base = site_url.rsplit("/", 1)[0]
    assert result.document_links == [f"{base}/report.pdf"]


def test_capture_without_rules_keeps_content(site_url, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    result = capture.capture(site_url, out)
    assert "광고 위젯 문구" in result.content_html


@pytest.fixture
def cross_origin_url(tmp_path):
    """다른 포트(=다른 origin)의 자원을 참조하는 페이지.

    자원 서버는 CORS 헤더를 주지 않으므로 페이지 컨텍스트 fetch() 는 막히고,
    context.request 폴백 경로가 실행된다 (pstatic.net 등 실서비스 재현).
    """
    asset = tmp_path / "asset"
    asset.mkdir()
    Image.new("RGB", (4, 4), (0, 0, 255)).save(asset / "remote.png")
    asset_server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(_QuietHandler, directory=str(asset))
    )
    asset_thread = threading.Thread(target=asset_server.serve_forever, daemon=True)
    asset_thread.start()
    asset_base = f"http://127.0.0.1:{asset_server.server_address[1]}"

    site = tmp_path / "xo-site"
    site.mkdir()
    (site / "index.html").write_text(
        f"""<!doctype html>
<html><head><meta charset="utf-8"><title>xo</title></head><body>
  <img src="{asset_base}/remote.png" alt="원격">
  <img src="{asset_base}/missing.png" alt="없음">
</body></html>
""",
        encoding="utf-8",
    )
    site_server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(_QuietHandler, directory=str(site))
    )
    site_thread = threading.Thread(target=site_server.serve_forever, daemon=True)
    site_thread.start()

    yield f"http://127.0.0.1:{site_server.server_address[1]}/index.html"
    site_server.shutdown()
    asset_server.shutdown()
    site_thread.join(timeout=5)
    asset_thread.join(timeout=5)


def test_capture_cors_blocked_image_inlined_via_fallback(cross_origin_url, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    capture.capture(cross_origin_url, out)

    page_html = (out / "page.html").read_text(encoding="utf-8")
    # CORS 로 fetch() 가 막혀도 context.request 폴백으로 인라인된다
    assert "remote.png" not in page_html
    assert "data:image/png;base64," in page_html
    # 폴백으로도 못 받는 자원(404)만 원본 URL 유지
    assert "missing.png" in page_html


def test_capture_retries_with_http2_disabled(monkeypatch, tmp_path):
    """net::ERR_HTTP2_* 실패는 HTTP/2 를 끄고 한 번 더 시도한다 (구형 IIS 등)."""
    calls = []

    def fake_once(url, out_dir, remove_selectors=(), link_rewriter=None,
                  browser_args=()):
        calls.append(browser_args)
        if not browser_args:
            raise capture.CaptureError(
                f"{url} 캡처 실패: Page.goto: net::ERR_HTTP2_PROTOCOL_ERROR at {url}"
            )
        return "재시도 성공"

    monkeypatch.setattr(capture, "_capture_once", fake_once)
    assert capture.capture("https://example.com/", tmp_path) == "재시도 성공"
    assert calls == [(), ("--disable-http2",)]


def test_capture_does_not_retry_other_errors(monkeypatch, tmp_path):
    """HTTP/2 와 무관한 실패는 그대로 던진다 (불필요한 재시도 금지)."""
    calls = []

    def fake_once(url, out_dir, remove_selectors=(), link_rewriter=None,
                  browser_args=()):
        calls.append(browser_args)
        raise capture.CaptureError(f"{url} 캡처 실패: net::ERR_NAME_NOT_RESOLVED")

    monkeypatch.setattr(capture, "_capture_once", fake_once)
    with pytest.raises(capture.CaptureError):
        capture.capture("https://example.com/", tmp_path)
    assert calls == [()]


def test_capture_connect_error_on_closed_port(tmp_path):
    # 아무도 리슨하지 않는 포트 → ERR_CONNECTION_REFUSED → CaptureConnectError
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with pytest.raises(capture.CaptureConnectError):
        capture.capture(f"https://127.0.0.1:{port}/", tmp_path)
