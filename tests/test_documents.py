"""documents.py 테스트 — URL 필터·파일명 정제·다운로드 한도·문서 CAS
(로컬 서버, 외부 네트워크 없음)."""
import hashlib
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
        if self.path.startswith("/direct.php"):
            # download.php 류 — 확장자 없는 경로 + Content-Disposition 파일명
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header(
                "Content-Disposition",
                "attachment; filename*=UTF-8''"
                "%EB%8D%B0%EC%9D%B4%ED%84%B0%20%EC%8B%9C%ED%8A%B8.pdf",  # 데이터 시트.pdf
            )
            self.send_header("Content-Length", str(len(_PDF_BYTES)))
            self.end_headers()
            self.wfile.write(_PDF_BYTES)
            return
        routes = {
            "/ok.pdf": ("application/pdf", _PDF_BYTES, 200),
            "/second.pdf": ("application/pdf", _PDF_BYTES * 2, 200),
            "/login.pdf": ("text/html; charset=utf-8", b"<html>login</html>", 200),
            "/big.pdf": ("application/pdf", b"x" * 4096, 200),
            "/missing.pdf": ("application/pdf", b"", 404),
            "/blob": ("application/x-msdownload", b"MZ-not-a-document", 200),
            "/bundle.zip": ("application/zip", b"PK\x03\x04fake-zip", 200),
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


def test_download_documents_size_limit(doc_server, tmp_path):
    limits = documents.DocumentLimits(max_count=20, max_bytes=1024, timeout_seconds=30)
    url = f"{doc_server}/big.pdf"
    manifest, failed = documents.download_documents(
        [url], tmp_path / "files", limits=limits
    )
    assert manifest == [] and failed == [url]
    # 부분 다운로드 잔재가 남지 않는다
    assert list((tmp_path / "files").glob("*.pdf")) == []


def test_download_documents_count_limit(doc_server, tmp_path):
    limits = documents.DocumentLimits(
        max_count=1, max_bytes=50 * 1024 * 1024, timeout_seconds=30
    )
    urls = [f"{doc_server}/ok.pdf", f"{doc_server}/second.pdf"]
    manifest, failed = documents.download_documents(
        urls, tmp_path / "files", limits=limits
    )
    assert len(manifest) == 1 and manifest[0]["url"] == urls[0]
    assert failed == []


def test_download_documents_dedupes_links(doc_server, tmp_path):
    url = f"{doc_server}/ok.pdf"
    manifest, _failed = documents.download_documents([url, url], tmp_path / "files")
    assert len(manifest) == 1


# ---- direct_filename (URL 자체가 파일 다운로드인 경우의 파일명 결정) ----

def test_direct_filename_from_content_disposition():
    url = "https://example.com/files/download.php?file=x"
    name = documents.direct_filename(
        url, url, 'attachment; filename="annual report.pdf"', "application/octet-stream"
    )
    assert name.startswith("annual-report-") and name.endswith(".pdf")


def test_direct_filename_rfc2231_korean():
    cd = ("attachment; filename*=UTF-8''"
          "%EB%8D%B0%EC%9D%B4%ED%84%B0%20%EC%8B%9C%ED%8A%B8.pdf")
    url = "https://example.com/d.php"
    name = documents.direct_filename(url, url, cd, None)
    assert name.startswith("데이터-시트-") and name.endswith(".pdf")


def test_direct_filename_euckr_mojibake_repaired():
    """EUC-KR 원시 바이트 파일명(구형 한국 서버)이 latin-1 모지바케로 들어와도 복구한다."""
    mojibake = "데이터 시트.pdf".encode("euc-kr").decode("latin-1")
    url = "https://example.com/download.php?id=76"
    name = documents.direct_filename(url, url, f'attachment; filename="{mojibake}"', None)
    assert name is not None and name.startswith("데이터-시트-") and name.endswith(".pdf")


def test_direct_filename_cd_sanitizes_traversal():
    url = "https://example.com/d"
    name = documents.direct_filename(
        url, url, 'attachment; filename="../../etc/passwd.pdf"', None
    )
    assert "/" not in name and "\\" not in name and ".." not in name


def test_direct_filename_cd_bad_ext_falls_back_to_path():
    url = "https://example.com/docs/report.pdf"
    name = documents.direct_filename(url, url, 'attachment; filename="run.exe"', None)
    assert name.startswith("report-") and name.endswith(".pdf")


def test_direct_filename_from_query_value():
    url = ("https://example.com/files/download.php"
           "?file=%2Ffiles%2Fproduct.1%2F76_U9500H_%EB%8D%B0%EC%9D%B4%ED%84%B0"
           "+%EC%8B%9C%ED%8A%B8.pdf")
    name = documents.direct_filename(url, url, None, "application/octet-stream")
    assert name is not None and name.endswith(".pdf")
    assert "U9500H" in name


def test_direct_filename_content_type_fallback():
    url = "https://example.com/download?id=3"
    name = documents.direct_filename(url, url, None, "application/pdf")
    assert name.startswith("document-") and name.endswith(".pdf")


def test_direct_filename_undecidable():
    url = "https://example.com/download?id=3"
    assert documents.direct_filename(url, url, None, "application/x-msdownload") is None


def test_direct_filename_zip_content_type_fallback():
    url = "https://example.com/download?id=3"
    name = documents.direct_filename(url, url, None, "application/zip")
    assert name.startswith("document-") and name.endswith(".zip")


# ---- download_direct (로컬 HTTP 서버) ----

def test_download_direct_uses_content_disposition(doc_server, tmp_path):
    url = f"{doc_server}/direct.php?file=anything"
    dl = documents.download_direct(url, tmp_path / "files")
    assert dl.http_status == 200
    entry = dl.entry
    assert str(entry["file"]).startswith("데이터-시트-")
    assert str(entry["file"]).endswith(".pdf")
    assert entry["bytes"] == len(_PDF_BYTES)
    assert entry["sha256"] == hashlib.sha256(_PDF_BYTES).hexdigest()
    assert (tmp_path / "files" / str(entry["file"])).read_bytes() == _PDF_BYTES


def test_download_direct_rejects_html(doc_server, tmp_path):
    with pytest.raises(ValueError, match="HTML"):
        documents.download_direct(f"{doc_server}/login.pdf", tmp_path / "files")


def test_download_direct_undecidable_extension(doc_server, tmp_path):
    with pytest.raises(ValueError, match="화이트리스트"):
        documents.download_direct(f"{doc_server}/blob", tmp_path / "files")
    assert list((tmp_path / "files").iterdir()) == []


def test_download_direct_zip(doc_server, tmp_path):
    dl = documents.download_direct(f"{doc_server}/bundle.zip", tmp_path / "files")
    entry = dl.entry
    assert str(entry["file"]).startswith("bundle-")
    assert str(entry["file"]).endswith(".zip")
    assert entry["content_type"] == "application/zip"


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
    monkeypatch.setattr(config, "DOCUMENTS_DIR", tmp_path / "documents")

    doc_url = f"{doc_server}/ok.pdf"
    bad_url = f"{doc_server}/missing.pdf"
    html = "<html><body><p>본문 텍스트</p></body></html>"

    def fake_capture(url, out_dir, remove_selectors=(), link_rewriter=None,
                     session=None, resource_fallback=None, **kwargs):
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

    # 파일 본체는 스냅샷이 아니라 문서 CAS 에 저장된다
    assert not (outcome.snapshot_dir / "files").exists()
    name = documents.cas_name(str(entry["sha256"]), str(entry["file"]))
    assert name is not None
    assert documents.cas_path(name).read_bytes() == _PDF_BYTES

    with db.connect() as conn:
        # 스냅샷의 문서 참조 행이 기록된다
        row = db.get_snapshot_document(conn, outcome.snapshot_id, str(entry["file"]))
        assert row is not None and row["sha256"] == entry["sha256"]
        # 실행 로그에 documents 단계가 기록된다
        log = db.list_archive_logs(conn)[0]
    steps = [s["step"] for s in json.loads(log["steps"])]
    assert "documents" in steps

    # 내용이 같으면 unchanged — 문서를 다시 받지 않는다 (스냅샷도 없음)
    second = pipeline.archive_url("https://example.com/post")
    assert second.status == "unchanged" and second.documents == 0


def test_pipeline_archives_direct_download_url(doc_server, tmp_path, monkeypatch):
    """URL 자체가 파일 다운로드(CaptureDownloadError)면 문서 스냅샷으로 저장된다."""
    import json

    from chunchugwan import capture, db, netcheck, pipeline, storage

    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "RULES_PATH", tmp_path / "rules.json")
    monkeypatch.setattr(config, "RESOURCES_DIR", tmp_path / "resources")
    monkeypatch.setattr(config, "DOCUMENTS_DIR", tmp_path / "documents")
    # 픽스처 서버가 127.0.0.1 이라 루프백 게이트를 공인 취급으로 우회한다
    monkeypatch.setattr(netcheck, "classify_host", lambda host: netcheck.PUBLIC)

    def fake_capture(url, out_dir, **kwargs):
        raise capture.CaptureDownloadError(f"{url} 은 파일 다운로드 URL")

    monkeypatch.setattr(pipeline.capture, "capture", fake_capture)
    url = f"{doc_server}/direct.php?file=sheet.pdf"
    outcome = pipeline.archive_url(url)
    assert outcome.status == "new"
    assert outcome.documents == 1
    assert outcome.title and str(outcome.title).endswith(".pdf")

    meta = storage.read_meta(outcome.snapshot_dir)
    assert meta.documents is not None and len(meta.documents) == 1
    entry = meta.documents[0]
    assert meta.title == entry["file"]

    # 파일 본체는 문서 CAS 에 저장된다
    name = documents.cas_name(str(entry["sha256"]), str(entry["file"]))
    assert name is not None
    assert documents.cas_path(name).read_bytes() == _PDF_BYTES
    assert not (outcome.snapshot_dir / "files").exists()

    # 산출물: page.html(.gz)·content.md 는 있고, 페이지가 아니라 스크린샷은 없다
    files = {f["name"] for f in storage.snapshot_files(outcome.snapshot_dir)}
    assert "content.md" in files
    assert "page.html.gz" in files or "page.html" in files
    assert not any(n.startswith("screenshot") for n in files)
    content = (outcome.snapshot_dir / "content.md").read_text(encoding="utf-8")
    assert str(entry["sha256"]) in content

    with db.connect() as conn:
        row = db.get_snapshot_document(conn, outcome.snapshot_id, str(entry["file"]))
        assert row is not None and row["sha256"] == entry["sha256"]
        log = db.list_archive_logs(conn)[0]
    steps = [s["step"] for s in json.loads(log["steps"])]
    assert "download" in steps and "store" in steps

    # 같은 파일이면 unchanged — 새 스냅샷을 만들지 않는다
    second = pipeline.archive_url(url)
    assert second.status == "unchanged"
    assert second.snapshot_id == outcome.snapshot_id


# ---- 문서 CAS ----

def test_cas_name_validation():
    sha = "ab" * 32
    assert documents.cas_name(sha, "report-12345678.pdf") == sha + ".pdf"
    assert documents.is_valid_cas_name(sha + ".pdf")
    assert not documents.is_valid_cas_name(sha + ".html")    # 문서 확장자 아님
    assert not documents.is_valid_cas_name(sha + ".pdf/../x")
    assert not documents.is_valid_cas_name("zz" * 32 + ".pdf")  # hex 아님
    assert documents.cas_name(sha, "x.exe") is None
    assert documents.cas_name("짧은해시", "x.pdf") is None


def test_ingest_into_cas_dedupes(tmp_path, monkeypatch):
    """같은 내용은 한 번만 저장되고, 이전 후 임시 files 디렉토리는 정리된다."""
    monkeypatch.setattr(config, "DOCUMENTS_DIR", tmp_path / "documents")
    sha = hashlib.sha256(b"same-bytes").hexdigest()
    for i in (1, 2):
        files_dir = tmp_path / f"tmp{i}" / "files"
        files_dir.mkdir(parents=True)
        (files_dir / f"doc{i}-aaaaaaa{i}.pdf").write_bytes(b"same-bytes")
        manifest = [{
            "url": f"https://example.com/doc{i}.pdf",
            "file": f"doc{i}-aaaaaaa{i}.pdf", "bytes": 10,
            "sha256": sha, "content_type": "application/pdf",
        }]
        documents.ingest_into_cas(files_dir, manifest)
        assert len(manifest) == 1
        assert not files_dir.exists()
    stored = list((tmp_path / "documents").glob("*/*"))
    assert [p.name for p in stored] == [sha + ".pdf"]
    assert stored[0].read_bytes() == b"same-bytes"


def test_ingest_into_cas_cross_device(tmp_path, monkeypatch):
    """스테이징과 아카이브가 다른 파일시스템이면(EXDEV) 복사 폴백으로 저장된다."""
    import errno
    import os
    from pathlib import Path

    monkeypatch.setattr(config, "DOCUMENTS_DIR", tmp_path / "documents")
    real_replace = os.replace

    def fake_replace(src, dst):
        # 스테이징 → CAS 직접 이동만 실패시킨다 (폴백의 같은 디렉토리 교체는 통과)
        if Path(src).parent != Path(dst).parent:
            raise OSError(errno.EXDEV, "Invalid cross-device link", str(src))
        real_replace(src, dst)

    monkeypatch.setattr(documents.os, "replace", fake_replace)
    files_dir = tmp_path / "tmp" / "files"
    files_dir.mkdir(parents=True)
    (files_dir / "doc-aaaaaaaa.pdf").write_bytes(b"cross-device")
    sha = hashlib.sha256(b"cross-device").hexdigest()
    manifest = [{
        "url": "https://example.com/doc.pdf",
        "file": "doc-aaaaaaaa.pdf", "bytes": 12,
        "sha256": sha, "content_type": "application/pdf",
    }]
    documents.ingest_into_cas(files_dir, manifest)
    assert len(manifest) == 1
    assert not files_dir.exists()
    stored = documents.cas_path(sha + ".pdf")
    assert stored.read_bytes() == b"cross-device"
    assert list(stored.parent.glob("*.tmp")) == []  # 임시 파일 잔재 없음


def test_delete_cas_removes_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DOCUMENTS_DIR", tmp_path / "documents")
    sha = hashlib.sha256(b"bytes").hexdigest()
    name = sha + ".pdf"
    path = documents.cas_path(name)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"bytes")
    documents.delete_cas([name, "../../etc/passwd", "없는이름.pdf"])
    assert not path.exists()
    assert not path.parent.exists()  # 빈 버킷 디렉토리도 정리


def _legacy_snapshot(tmp_path, *, url="https://example.com/post", body=b"%PDF doc"):
    """files/ 에 문서를 가진 구형 스냅샷 + DB 행 생성 → (snap_dir, snapshot_id)."""
    from chunchugwan import db, storage

    domain, slug = "example.com", storage.url_to_slug(url)
    dir_name = "2026-06-01T00-00-00"
    snap_dir = storage.page_dir(domain, slug) / dir_name
    (snap_dir / "files").mkdir(parents=True)
    (snap_dir / "files" / "report-12345678.pdf").write_bytes(body)
    storage.write_meta(snap_dir, storage.SnapshotMeta(
        url=url, final_url=url, taken_at="2026-06-01T00:00:00+00:00",
        content_hash="h", http_status=200, title=None,
        documents=[{
            "url": "https://example.com/files/report.pdf",
            "file": "report-12345678.pdf", "bytes": len(body),
            "sha256": "1234", "content_type": "application/pdf",  # 오염된 해시
        }],
    ))
    with db.connect() as conn:
        page_id = db.get_or_create_page(conn, url, domain, slug)
        snapshot_id = db.insert_snapshot(
            conn, page_id, taken_at="2026-06-01T00:00:00+00:00",
            dir_name=dir_name, content_hash="h", final_url=url,
            http_status=200, changed=1,
        )
    return snap_dir, snapshot_id


def test_compact_legacy_documents_moves_to_cas(tmp_path, monkeypatch):
    """구형 files/ 문서가 CAS 로 이전되고 참조 행이 생긴다 (해시는 재계산)."""
    from chunchugwan import db

    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "DOCUMENTS_DIR", tmp_path / "documents")

    body = b"%PDF legacy doc"
    snap_dir, snapshot_id = _legacy_snapshot(tmp_path, body=body)
    assert documents.has_legacy_documents(snap_dir)

    stats = documents.compact_legacy_documents()
    assert stats.moved == 1
    assert stats.before_bytes == len(body) and stats.after_bytes == len(body)

    sha = hashlib.sha256(body).hexdigest()
    assert documents.cas_path(sha + ".pdf").read_bytes() == body
    assert not (snap_dir / "files").exists()
    assert not documents.has_legacy_documents(snap_dir)
    with db.connect() as conn:
        row = db.get_snapshot_document(conn, snapshot_id, "report-12345678.pdf")
    assert row is not None and row["sha256"] == sha  # meta 의 오염 해시가 아니다

    # 멱등 — 다시 실행해도 이전할 것이 없다
    assert documents.compact_legacy_documents().moved == 0
