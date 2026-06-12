"""http URL 의 https 승격 — 등록 시 https 지원 확인 후 https 로 아카이빙.

스킴 생략 입력의 https 추정(normalize_url)과 짝을 이루는 반대 방향 확인:
명시적 http:// URL 도 같은 사이트가 유효한 인증서로 https 를 서빙하면
(HSTS 사이트 포함 — https 응답이 리다이렉트여도 지원) https 로 등록한다.
신규 페이지에만 적용 — 이미 http 로 쌓인 페이지는 그대로 둔다.
"""
import httpx
import pytest

from chunchugwan import capture, config, crawler, db, pipeline

# conftest 의 자동 차단 픽스처(_no_https_probe)가 패치하기 전(모듈 임포트
# 시점)의 원본 — 판정 로직 자체를 검증하는 테스트가 사용한다
_REAL_HTTPS_SUPPORTED = pipeline._https_supported


@pytest.fixture
def archive_env(tmp_path, monkeypatch):
    """임시 아카이브 루트."""
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "RULES_PATH", tmp_path / "rules.json")
    monkeypatch.setattr(config, "RESOURCES_DIR", tmp_path / "resources")
    monkeypatch.setattr(config, "DOCUMENTS_DIR", tmp_path / "documents")
    return tmp_path


def _fake_capture(monkeypatch):
    """캡처 모킹 — 호출된 URL 을 기록한다."""
    calls: list[str] = []

    def fake(url, out_dir, remove_selectors=(), link_rewriter=None, session=None,
             resource_fallback=None):
        calls.append(url)
        return capture.CaptureResult(
            final_url=url, http_status=200, title="제목",
            raw_html="<html><body>내용</body></html>",
            content_html="<html><body>내용</body></html>",
        )

    monkeypatch.setattr(pipeline.capture, "capture", fake)
    return calls


def _probe(monkeypatch, supported: bool):
    """https 프로브 모킹 — 호출된 URL 을 기록한다 (conftest 기본값 오버라이드)."""
    calls: list[str] = []

    def fake(url):
        calls.append(url)
        return supported

    monkeypatch.setattr(pipeline, "_https_supported", fake)
    return calls


# ---- 프로브 판정 (_https_supported) ----


def _mock_https_response(monkeypatch, status_code: int | Exception):
    seen: list[str] = []

    def fake_get(url, **kwargs):
        seen.append(url)
        if isinstance(status_code, Exception):
            raise status_code
        return httpx.Response(status_code, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    return seen


@pytest.mark.parametrize(
    "status,expected",
    [(200, True), (301, True), (308, True), (403, False), (404, False)],
)
def test_https_supported_by_status(monkeypatch, status, expected):
    seen = _mock_https_response(monkeypatch, status)
    assert _REAL_HTTPS_SUPPORTED("http://example.com/a") is expected
    assert seen == ["https://example.com/a"]  # 같은 경로의 https 를 확인


def test_https_supported_false_on_error(monkeypatch):
    # 연결 실패·인증서 오류(자체 서명 등)는 미지원 — http 유지
    _mock_https_response(monkeypatch, httpx.ConnectError("boom"))
    assert _REAL_HTTPS_SUPPORTED("http://example.com/a") is False


# ---- 파이프라인 통합 ----


def test_new_http_page_upgraded_to_https(archive_env, monkeypatch):
    captured = _fake_capture(monkeypatch)
    probed = _probe(monkeypatch, supported=True)
    outcome = pipeline.archive_url("http://example.com/a")
    assert probed == ["http://example.com/a"]
    assert captured == ["https://example.com/a"]  # 캡처부터 https
    assert outcome.url == "https://example.com/a"
    with db.connect() as conn:
        assert db.get_page(conn, "https://example.com/a") is not None
        assert db.get_page(conn, "http://example.com/a") is None


def test_http_kept_when_https_unsupported(archive_env, monkeypatch):
    captured = _fake_capture(monkeypatch)
    _probe(monkeypatch, supported=False)
    outcome = pipeline.archive_url("http://example.com/a")
    assert captured == ["http://example.com/a"]
    assert outcome.url == "http://example.com/a"


def test_existing_http_page_not_probed(archive_env, monkeypatch):
    """이미 http 로 쌓인 페이지는 승격하지 않는다 — 히스토리 유지, 프로브 생략."""
    _fake_capture(monkeypatch)
    _probe(monkeypatch, supported=False)
    pipeline.archive_url("http://example.com/a")  # http 페이지 생성

    probed = _probe(monkeypatch, supported=True)  # 이제 https 가 생겼더라도
    outcome = pipeline.archive_url("http://example.com/a")  # 동일 콘텐츠 — 확인 기록
    assert probed == []  # 기존 페이지 — 프로브하지 않음
    assert outcome.url == "http://example.com/a"


def test_inferred_https_not_probed(archive_env, monkeypatch):
    """스킴 생략 입력은 이미 https 추정 — 프로브 대상이 아니다."""
    captured = _fake_capture(monkeypatch)
    probed = _probe(monkeypatch, supported=True)
    pipeline.archive_url("example.com/a")
    assert probed == []
    assert captured == ["https://example.com/a"]


def test_hsts_redirect_counts_as_https_support(archive_env, monkeypatch):
    """HSTS 사이트(https 응답이 301 리다이렉트)도 https 로 승격된다."""
    captured = _fake_capture(monkeypatch)
    _mock_https_response(monkeypatch, 301)
    # conftest 의 프로브 차단을 풀고 실제 판정 로직을 쓴다 (httpx 만 모킹)
    monkeypatch.setattr(pipeline, "_https_supported", _REAL_HTTPS_SUPPORTED)
    outcome = pipeline.archive_url("http://hsts.example.com/a")
    assert captured == ["https://hsts.example.com/a"]
    assert outcome.url == "https://hsts.example.com/a"


# ---- 크롤 등록 ----


def test_start_crawl_upgrades_start_url(archive_env, monkeypatch):
    _probe(monkeypatch, supported=True)
    crawl, merged = crawler.start_crawl("http://example.com/docs/", source="cli")
    assert merged is False
    assert crawl["start_url"] == "https://example.com/docs/"
    assert crawl["scope_host"] == "example.com"
    with db.connect() as conn:
        pages = db.list_crawl_pages(conn, crawl["id"])
    assert [p["url"] for p in pages] == ["https://example.com/docs/"]


def test_crawl_schedule_upgrades_start_url(archive_env, monkeypatch):
    _probe(monkeypatch, supported=True)
    sched = crawler.set_crawl_schedule("http://example.com/", 3600)
    assert sched["start_url"] == "https://example.com/"
