"""documents.py 테스트 — URL 필터·파일명 정제·다운로드 한도 (로컬 서버, 외부 네트워크 없음)."""
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from chunchugwan import config, documents


# ---- is_document_url ----

@pytest.mark.parametrize("url", [
    "https://example.com/report.pdf",
    "https://example.com/files/발표자료.PPTX",          # 대문자 확장자
    "https://example.com/%EB%B3%B4%EA%B3%A0%EC%84%9C.hwp",  # 퍼센트 인코딩 한글
    "http://example.com/a/b/c.docx?download=1#page2",   # 쿼리/fragment 무시
    "https://example.com/deck.key",
])
def test_is_document_url_true(url):
    assert documents.is_document_url(url)


@pytest.mark.parametrize("url", [
    "https://example.com/page.html",
    "https://example.com/image.png",
    "https://example.com/report",                # 확장자 없음
    "https://example.com/report.pdf.html",       # 마지막 확장자 기준
    "ftp://example.com/report.pdf",              # http(s) 외 스킴
    "javascript:alert(1)",
    "mailto:a@b.com",
])
def test_is_document_url_false(url):
    assert not documents.is_document_url(url)


# ---- document_filename ----

def test_document_filename_keeps_stem_and_ext():
    name = documents.document_filename("https://example.com/files/annual-report.pdf")
    assert name.startswith("annual-report-")
    assert name.endswith(".pdf")


def test_document_filename_decodes_korean():
    name = documents.document_filename(
        "https://example.com/%EB%B3%B4%EA%B3%A0%EC%84%9C.hwp"
    )
    assert name.startswith("보고서-")
    assert name.endswith(".hwp")


def test_document_filename_sanitizes_traversal():
    name = documents.document_filename("https://example.com/..%2F..%2Fetc.pdf")
    assert "/" not in name and "\\" not in name
    assert ".." not in name


def test_document_filename_unique_per_url():
    a = documents.document_filename("https://example.com/a/report.pdf")
    b = documents.document_filename("https://example.com/b/report.pdf")
    assert a != b


def test_document_filename_empty_stem_fallback():
    name = documents.document_filename("https://example.com/%2e%2e.pdf")
    assert name.startswith("document-")


# ---- download_documents (로컬 HTTP 서버) ----

_PDF_BYTES = b"%PDF-1.4 fake-pdf-content"


class _DocHandler(BaseHTTPRequestHandler):
    """확장자는 .pdf 지만 응답 성격이 다른 경로들을 서빙하는 픽스처 서버."""

    def do_GET(self):  # noqa: N802 (http.server 규약)
        routes = {
            "/ok.pdf": ("application/pdf", _PDF_BYTES, 200),
            "/second.pdf": ("application/pdf", _PDF_BYTES * 2, 200),
            "/login.pdf": ("text/html; charset=utf-8", b"<html>login</html>", 200),
            "/big.pdf": ("application/pdf", b"x" * 4096, 200),
            "/missing.pdf": ("application/pdf", b"", 404),
        }
        if self.path not in routes:
            self.send_error(404)
            return
        ctype, body, status = routes[self.path]
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # 테스트 출력 오염 방지
        pass


@pytest.fixture
def doc_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _DocHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    thread.join(timeout=5)


def test_download_documents_success(doc_server, tmp_path):
    url = f"{doc_server}/ok.pdf"
    manifest, failed = documents.download_documents([url], tmp_path / "files")
    assert failed == []
    assert len(manifest) == 1
    entry = manifest[0]
    assert entry["url"] == url
    assert entry["bytes"] == len(_PDF_BYTES)
    assert entry["content_type"] == "application/pdf"
    saved = tmp_path / "files" / entry["file"]
    assert saved.read_bytes() == _PDF_BYTES


def test_download_documents_skips_html_response(doc_server, tmp_path):
    """확장자가 .pdf 라도 HTML 응답(로그인/오류 페이지)은 저장하지 않는다."""
    url = f"{doc_server}/login.pdf"
    manifest, failed = documents.download_documents([url], tmp_path / "files")
    assert manifest == [] and failed == [url]
    files_dir = tmp_path / "files"
    assert not files_dir.is_dir() or list(files_dir.iterdir()) == []


def test_download_documents_failure_does_not_block_others(doc_server, tmp_path):
    urls = [f"{doc_server}/missing.pdf", f"{doc_server}/ok.pdf"]
    manifest, failed = documents.download_documents(urls, tmp_path / "files")
    assert [m["url"] for m in manifest] == [urls[1]]
    assert failed == [urls[0]]


def test_download_documents_size_limit(doc_server, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DOCUMENT_MAX_BYTES", 1024)
    url = f"{doc_server}/big.pdf"
    manifest, failed = documents.download_documents([url], tmp_path / "files")
    assert manifest == [] and failed == [url]
    # 부분 다운로드 잔재가 남지 않는다
    assert list((tmp_path / "files").glob("*.pdf")) == []


def test_download_documents_count_limit(doc_server, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DOCUMENT_MAX_COUNT", 1)
    urls = [f"{doc_server}/ok.pdf", f"{doc_server}/second.pdf"]
    manifest, failed = documents.download_documents(urls, tmp_path / "files")
    assert len(manifest) == 1 and manifest[0]["url"] == urls[0]
    assert failed == []


def test_download_documents_dedupes_links(doc_server, tmp_path):
    url = f"{doc_server}/ok.pdf"
    manifest, _failed = documents.download_documents([url, url], tmp_path / "files")
    assert len(manifest) == 1


# ---- 파이프라인 통합 (캡처는 가짜, 문서 다운로드는 로컬 서버) ----

def test_pipeline_archives_linked_documents(doc_server, tmp_path, monkeypatch):
    import json

    from chunchugwan import capture, db, pipeline, storage

    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "RULES_PATH", tmp_path / "rules.json")
    monkeypatch.setattr(config, "RESOURCES_DIR", tmp_path / "resources")

    doc_url = f"{doc_server}/ok.pdf"
    bad_url = f"{doc_server}/missing.pdf"
    html = "<html><body><p>본문 텍스트</p></body></html>"

    def fake_capture(url, out_dir, remove_selectors=(), link_rewriter=None):
        (out_dir / "raw.html").write_text(html, encoding="utf-8")
        (out_dir / "page.html").write_text(html, encoding="utf-8")
        return capture.CaptureResult(
            final_url=url, http_status=200, title="제목",
            raw_html=html, content_html=html,
            document_links=[doc_url, bad_url],
        )

    monkeypatch.setattr(pipeline.capture, "capture", fake_capture)
    outcome = pipeline.archive_url("https://example.com/post")
    assert outcome.status == "new"
    assert outcome.documents == 1  # 실패한 링크는 세지 않는다

    meta = storage.read_meta(outcome.snapshot_dir)
    assert meta.documents is not None and len(meta.documents) == 1
    entry = meta.documents[0]
    assert entry["url"] == doc_url
    saved = outcome.snapshot_dir / "files" / str(entry["file"])
    assert saved.read_bytes() == _PDF_BYTES

    # 실행 로그에 documents 단계가 기록된다
    with db.connect() as conn:
        log = db.list_archive_logs(conn)[0]
    steps = [s["step"] for s in json.loads(log["steps"])]
    assert "documents" in steps

    # 내용이 같으면 unchanged — 문서를 다시 받지 않는다 (스냅샷도 없음)
    second = pipeline.archive_url("https://example.com/post")
    assert second.status == "unchanged" and second.documents == 0
