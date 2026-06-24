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


def test_capture_no_mobile_screenshot_by_default(site_url, tmp_path):
    """mobile_screenshot 기본값(False)에선 모바일 스크린샷을 만들지 않는다."""
    out = tmp_path / "out"
    out.mkdir()
    capture.capture(site_url, out)
    assert (out / "screenshot.png").is_file()
    assert not (out / "screenshot-mobile.png").is_file()


def test_capture_mobile_screenshot(tmp_path):
    """mobile_screenshot=True 면 안드로이드 크롬 모바일 컨텍스트로 같은 URL 을
    한 번 더 열어 데스크탑·모바일 두 스크린샷을 저장한다.

    - 모바일 요청은 안드로이드 크롬 UA 로 나간다(서버가 확인).
    - 반응형 페이지(viewport meta)는 모바일에서 device-width(390)로 렌더된다.
    """
    from http.server import BaseHTTPRequestHandler

    from chunchugwan import config

    seen_uas: list[str] = []
    html = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>resp</title></head><body><h1>모바일</h1><p>본문</p></body></html>"
    ).encode("utf-8")

    class _UAHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # 테스트 출력 오염 방지
            pass

        def do_GET(self):  # noqa: N802 (http.server 규약)
            seen_uas.append(self.headers.get("User-Agent", ""))
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)

    server = ThreadingHTTPServer(("127.0.0.1", 0), _UAHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    out = tmp_path / "out"
    out.mkdir()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/"
        capture.capture(url, out, mobile_screenshot=True)
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert (out / "screenshot.png").is_file()
    mobile = out / "screenshot-mobile.png"
    assert mobile.is_file()

    # 모바일 요청은 안드로이드 크롬 UA, 데스크탑 요청은 'Mobile' 없는 UA 로 나간다
    assert config.MOBILE_SCREENSHOT_USER_AGENT in seen_uas
    assert any("Mobile" not in ua for ua in seen_uas)

    # 반응형 페이지는 모바일 뷰포트에서 device-width(390)로 렌더된다
    with Image.open(mobile) as im:
        assert im.width == config.MOBILE_SCREENSHOT_WIDTH


def test_mobile_screenshot_skipped_on_loopback_redirect(tmp_path):
    """모바일 UA 로만 다른 호스트(localhost)의 루프백으로 리다이렉트되면 모바일
    스크린샷을 저장하지 않는다 — 대시보드 누수 방지(아키텍처 원칙 7).
    데스크탑 캡처는 리다이렉트되지 않으므로 정상 저장된다."""
    from http.server import BaseHTTPRequestHandler

    info: dict[str, int] = {}
    html = b"<!doctype html><html><body>landed</body></html>"

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # noqa: 출력 오염 방지
            pass

        def do_GET(self):  # noqa: N802
            ua = self.headers.get("User-Agent", "")
            if self.path == "/" and "Mobile" in ua:
                # 모바일 UA 만 다른 호스트(localhost)의 루프백으로 리다이렉트
                self.send_response(302)
                self.send_header("Location", f"http://localhost:{info['port']}/landed")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    info["port"] = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    out = tmp_path / "out"
    out.mkdir()
    try:
        capture.capture(
            f"http://127.0.0.1:{info['port']}/", out, mobile_screenshot=True
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert (out / "screenshot.png").is_file()             # 데스크탑은 정상 저장
    assert not (out / "screenshot-mobile.png").is_file()  # 루프백 리다이렉트 → 생략


def test_capture_records_resource_url_mapping(site_url, tmp_path):
    """인라인 성공 자원의 sha256 → 원본 URL 매핑이 기록된다 (보안 컨텍스트)."""
    out = tmp_path / "out"
    out.mkdir()
    result = capture.capture(site_url, out)
    base = site_url.rsplit("/", 1)[0]
    assert f"{base}/img.png" in result.resource_urls.values()
    assert f"{base}/font.woff2" in result.resource_urls.values()
    # sha 키가 실제 콘텐츠 해시와 일치한다
    import hashlib
    from urllib.request import urlopen

    img_sha = hashlib.sha256(urlopen(f"{base}/img.png").read()).hexdigest()
    assert result.resource_urls.get(img_sha) == f"{base}/img.png"


def test_resource_sha_falls_back_to_python_binding(site_url, tmp_path, monkeypatch):
    """crypto.subtle 이 없는 환경(http 페이지)에서도 sha 매핑이 기록된다.

    127.0.0.1 은 브라우저가 보안 컨텍스트로 취급하므로, JS 의 crypto.subtle
    분기를 막아 비보안 컨텍스트의 폴백 경로(Python 바인딩)를 재현한다.
    """
    monkeypatch.setattr(
        capture, "_INLINE_JS",
        capture._INLINE_JS.replace("if (crypto.subtle) {", "if (false) {"),
    )
    out = tmp_path / "out"
    out.mkdir()
    result = capture.capture(site_url, out)
    base = site_url.rsplit("/", 1)[0]
    assert f"{base}/img.png" in result.resource_urls.values()
    import hashlib
    from urllib.request import urlopen

    img_sha = hashlib.sha256(urlopen(f"{base}/img.png").read()).hexdigest()
    assert result.resource_urls.get(img_sha) == f"{base}/img.png"


def test_sha256_of_base64_matches_hashlib():
    import base64
    import hashlib

    data = b"binding-check"
    assert capture._sha256_of_base64(
        base64.b64encode(data).decode("ascii")
    ) == hashlib.sha256(data).hexdigest()


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
                  browser_args=(), session=None, resource_fallback=None,
                  insecure_tls=False, credential=None, live_session=None,
                  mobile_screenshot=False):
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
                  browser_args=(), session=None, resource_fallback=None,
                  insecure_tls=False, credential=None, live_session=None,
                  mobile_screenshot=False):
        calls.append(browser_args)
        raise capture.CaptureError(f"{url} 캡처 실패: net::ERR_NAME_NOT_RESOLVED")

    monkeypatch.setattr(capture, "_capture_once", fake_once)
    with pytest.raises(capture.CaptureError):
        capture.capture("https://example.com/", tmp_path)
    assert calls == [()]


def test_browser_session_reuses_and_relaunches(site_url, tmp_path):
    """세션은 캡처 간 같은 브라우저를 재사용하고, close 후에는 재기동한다."""
    out1, out2, out3 = tmp_path / "o1", tmp_path / "o2", tmp_path / "o3"
    for out in (out1, out2, out3):
        out.mkdir()
    with capture.BrowserSession() as session:
        capture.capture(site_url, out1, session=session)
        first = session._browser
        assert first is not None and first.is_connected()

        capture.capture(site_url, out2, session=session)
        assert session._browser is first  # 같은 브라우저 재사용

        session.close()  # 유휴 시 내려도 (큐 빈 폴링) 다음 캡처가 재기동한다
        capture.capture(site_url, out3, session=session)
        assert session._browser is not None and session._browser is not first
    assert (out3 / "raw.html").is_file()
    assert session._browser is None  # __exit__ 가 정리


def test_capture_download_url_raises_download_error(tmp_path):
    """탐색이 파일 다운로드로 전환되면 CaptureDownloadError (download.php 등).

    Content-Disposition: attachment 는 PDF 뷰어 유무와 무관하게 항상
    다운로드를 강제하므로 브라우저 버전에 안정적인 픽스처다.
    """
    from http.server import BaseHTTPRequestHandler

    class _AttachmentHandler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (http.server 규약)
            body = b"%PDF-1.4 fixture"
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Disposition", "attachment; filename=report.pdf")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), _AttachmentHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/download.php?file=report.pdf"
        with pytest.raises(capture.CaptureDownloadError) as exc:
            capture.capture(url, tmp_path)
        # 브라우저가 받은 파일을 실제로 저장하고 정보를 실어 온다 (httpx 재요청 불필요)
        err = exc.value
        assert err.download_path is not None and err.download_path.is_file()
        assert err.download_path.read_bytes() == b"%PDF-1.4 fixture"
        assert err.suggested_filename == "report.pdf"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_capture_proceeds_when_subresource_hangs(tmp_path, monkeypatch):
    """응답 없는 하위 자원이 load 를 막아도 현재 DOM 으로 캡처를 완료한다.

    재현: 본문에 박힌 죽은 외부 이미지(연결이 영영 안 끝나는 호스트) —
    networkidle·load 둘 다 미도달이지만 DOM 은 이미 파싱된 상태다.
    """
    import time
    from http.server import BaseHTTPRequestHandler

    class _HangingImageHandler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (http.server 규약)
            if self.path == "/hang.png":
                time.sleep(30)  # 응답 없이 매달린다 (요청별 스레드라 격리됨)
                return
            body = (
                '<html><head><meta charset="utf-8"><title>hang</title></head>'
                '<body><p>매달린 자원 본문</p><img src="/hang.png"></body></html>'
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    monkeypatch.setattr(capture.config, "PAGE_LOAD_TIMEOUT_MS", 2_000)
    monkeypatch.setattr(capture.config, "RESOURCE_FETCH_TIMEOUT_MS", 2_000)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HangingImageHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        out = tmp_path / "out"
        out.mkdir()
        result = capture.capture(f"http://127.0.0.1:{server.server_address[1]}/", out)
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert "매달린 자원 본문" in result.raw_html
    assert result.http_status is None  # load 미도달 폴백 — 응답 객체 없음
    assert (out / "screenshot.png").is_file()
    # 매달린 이미지는 자원별 fetch 타임아웃으로 건너뛰고 원본 URL 이 남는다
    page_html = (out / "page.html").read_text(encoding="utf-8")
    assert "/hang.png" in page_html


def test_capture_completes_despite_busy_network(tmp_path, monkeypatch):
    """networkidle 에 영영 도달하지 않는 페이지(주기적 폴링)도 load 후 상한 대기로 완료한다.

    재현: 200ms 간격 fetch 폴링 — 네트워크가 잠잠해지는 500ms 구간이 없어
    networkidle 미도달. load 는 정상 도달이므로 응답 객체(http_status)는
    유지된 채 짧은 추가 대기 후 진행돼야 한다 (재네비게이션 없음).
    """
    import time
    from http.server import BaseHTTPRequestHandler

    class _PollingHandler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (http.server 규약)
            if self.path == "/ping":
                self.send_response(204)
                self.end_headers()
                return
            body = (
                '<html><head><meta charset="utf-8"><title>busy</title></head>'
                "<body><p>폴링 본문</p>"
                "<script>setInterval(() => fetch('/ping'), 200)</script>"
                "</body></html>"
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    monkeypatch.setattr(capture.config, "NETWORK_IDLE_TIMEOUT_MS", 1_000)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _PollingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        out = tmp_path / "out"
        out.mkdir()
        t0 = time.monotonic()
        result = capture.capture(f"http://127.0.0.1:{server.server_address[1]}/", out)
        elapsed = time.monotonic() - t0
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert result.http_status == 200  # load 도달 — 응답 객체 유지
    assert "폴링 본문" in result.raw_html
    assert (out / "page.html").is_file()
    # networkidle 30초 타임아웃 + 재네비게이션 경로였다면 60초 — 상한 대기 확인
    assert elapsed < 20


def test_capture_connect_error_on_closed_port(tmp_path):
    # 아무도 리슨하지 않는 포트 → ERR_CONNECTION_REFUSED → CaptureConnectError
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with pytest.raises(capture.CaptureConnectError):
        capture.capture(f"https://127.0.0.1:{port}/", tmp_path)


@pytest.fixture
def self_signed_site(tmp_path):
    """자체 서명 인증서로 https 만 서빙하는 사이트 (사설 NAS 재현)."""
    import ssl
    from pathlib import Path

    site = tmp_path / "ssl-site"
    site.mkdir()
    (site / "index.html").write_text(
        '<html><head><meta charset="utf-8"></head>'
        "<body><p>자체 서명 본문</p></body></html>", encoding="utf-8"
    )
    fixtures = Path(__file__).parent / "fixtures"
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(
        fixtures / "selfsigned-cert.pem", fixtures / "selfsigned-key.pem"
    )
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(_QuietHandler, directory=str(site))
    )
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"https://127.0.0.1:{server.server_address[1]}/index.html"
    server.shutdown()
    thread.join(timeout=5)


def test_capture_self_signed_rejected_by_default(self_signed_site, tmp_path):
    """기본은 인증서 검증 — 자체 서명은 인증서 오류로 실패한다."""
    out = tmp_path / "out-default"
    out.mkdir()
    with pytest.raises(capture.CaptureConnectError) as exc:
        capture.capture(self_signed_site, out)
    assert capture.is_cert_error(exc.value)


def test_capture_self_signed_with_insecure_tls(self_signed_site, tmp_path):
    """insecure_tls=True 면 검증을 무시하고 https 캡처가 성공한다."""
    out = tmp_path / "out-insecure"
    out.mkdir()
    result = capture.capture(self_signed_site, out, insecure_tls=True)
    assert result.http_status == 200
    assert "자체 서명 본문" in result.raw_html
    assert (out / "page.html").is_file()


def test_generic_link_rewriter_maps_http_links_to_goto():
    """단일 페이지 캡처용 리라이터 — http(s) 앵커를 /goto 리졸버로, 비웹은 스킵."""
    rw = capture.generic_link_rewriter()
    mapping = rw([
        "https://example.com/a",
        "http://example.com/b",
        "mailto:x@y.z",
        "javascript:void(0)",
    ])
    assert mapping["https://example.com/a"] == (
        "/goto?url=https%3A%2F%2Fexample.com%2Fa"
    )
    assert mapping["http://example.com/b"].startswith("/goto?url=")
    assert "mailto:x@y.z" not in mapping       # 비웹 스킴은 재작성 안 함
    assert "javascript:void(0)" not in mapping
