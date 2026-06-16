"""자체 서명 https 사이트의 아카이빙 — 인증서 오류 시 검증 무시 재시도.

폴백 사슬: https(검증) → 인증서 오류면 https(검증 무시, 로그 기록) →
그래도 실패면 기존 http 폴백. 캡처 자체의 insecure_tls 동작은
test_capture.py 의 실브라우저 테스트가 검증한다.
"""
import pytest

from chunchugwan import capture, config, db, pipeline

CERT_ERROR = "캡처 실패: net::ERR_CERT_AUTHORITY_INVALID"


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


def _result(url: str, document_links: list[str] | None = None) -> capture.CaptureResult:
    return capture.CaptureResult(
        final_url=url, http_status=200, title="제목",
        raw_html="<html><body>내용</body></html>",
        content_html="<html><body>내용</body></html>",
        document_links=document_links or [],
    )


def test_cert_error_retries_with_insecure_tls(archive_env, monkeypatch):
    """https 만 서빙하는 자체 서명 사이트 — 검증 무시 재시도로 성공한다."""
    calls: list[tuple[str, bool]] = []

    def fake(url, out_dir, remove_selectors=(), link_rewriter=None, session=None,
             resource_fallback=None, insecure_tls=False, **kwargs):
        calls.append((url, insecure_tls))
        if not insecure_tls:
            raise capture.CaptureConnectError(f"{url} {CERT_ERROR}")
        return _result(url)

    monkeypatch.setattr(pipeline.capture, "capture", fake)
    outcome = pipeline.archive_url("https://nas.example.com/")
    assert calls == [
        ("https://nas.example.com/", False),   # 1차 — 검증, 인증서 오류
        ("https://nas.example.com/", True),    # 2차 — 검증 무시, 성공
    ]
    assert outcome.status == "new"
    assert outcome.url == "https://nas.example.com/"
    with db.connect() as conn:
        assert db.get_page(conn, "https://nas.example.com/") is not None
        # 실행 로그 단계에 검증 무시 재시도가 남는다
        log = conn.execute(
            "SELECT steps FROM archive_logs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert "검증 무시로 https 재시도" in log["steps"]


def test_cert_error_falls_back_to_http_when_insecure_also_fails(
    archive_env, monkeypatch
):
    """검증 무시로도 https 가 안 되면 기존 http 폴백이 이어진다 (스킴 생략 입력)."""
    calls: list[tuple[str, bool]] = []

    def fake(url, out_dir, remove_selectors=(), link_rewriter=None, session=None,
             resource_fallback=None, insecure_tls=False, **kwargs):
        calls.append((url, insecure_tls))
        if url.startswith("https://"):
            raise capture.CaptureConnectError(f"{url} {CERT_ERROR}")
        return _result(url)

    monkeypatch.setattr(pipeline.capture, "capture", fake)
    outcome = pipeline.archive_url("example.com/a")
    assert calls == [
        ("https://example.com/a", False),
        ("https://example.com/a", True),
        ("http://example.com/a", False),
    ]
    assert outcome.url == "http://example.com/a"


def test_non_cert_error_skips_insecure_retry(archive_env, monkeypatch):
    """인증서와 무관한 실패는 검증 무시 재시도 없이 기존 폴백만 탄다."""
    calls: list[tuple[str, bool]] = []

    def fake(url, out_dir, remove_selectors=(), link_rewriter=None, session=None,
             resource_fallback=None, insecure_tls=False, **kwargs):
        calls.append((url, insecure_tls))
        if url.startswith("https://"):
            raise capture.CaptureConnectError(
                f"{url} 캡처 실패: net::ERR_CONNECTION_REFUSED"
            )
        return _result(url)

    monkeypatch.setattr(pipeline.capture, "capture", fake)
    outcome = pipeline.archive_url("example.com/a")
    assert calls == [
        ("https://example.com/a", False),
        ("http://example.com/a", False),
    ]
    assert outcome.url == "http://example.com/a"


def test_insecure_capture_downloads_documents_without_verify(
    archive_env, monkeypatch
):
    """검증 무시로 캡처한 사이트의 문서 다운로드도 인증서 검증을 끈다."""
    def fake(url, out_dir, remove_selectors=(), link_rewriter=None, session=None,
             resource_fallback=None, insecure_tls=False, **kwargs):
        if not insecure_tls:
            raise capture.CaptureConnectError(f"{url} {CERT_ERROR}")
        return _result(url, document_links=[f"{url}report.pdf"])

    seen_verify: list[bool] = []

    def fake_download(links, dest_dir, referer=None, verify=True, **kwargs):
        seen_verify.append(verify)
        return [], []

    monkeypatch.setattr(pipeline.capture, "capture", fake)
    monkeypatch.setattr(pipeline.documents, "download_documents", fake_download)
    pipeline.archive_url("https://nas.example.com/")
    assert seen_verify == [False]
