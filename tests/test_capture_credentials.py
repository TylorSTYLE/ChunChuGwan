"""캡처 연동(2단계) — 자격증명을 Playwright 컨텍스트에 origin-스코프 주입.

순수 헬퍼(_context_options·url_origin·jwt 라우트 스코프)와 pipeline 의
복호화·전달을 검증한다. 실제 브라우저 주입 동작은 capture 구조에 맡긴다
(여기서는 옵션 빌드와 누수 방지 로직을 검증).
"""
import pytest

from chunchugwan import (
    capture, config, crawler, credentials, crypto, db, documents, pipeline,
    storage,
)

C = capture.CaptureCredential


# ---- url_origin (서드파티 누수 스코프 기준) ----


def test_url_origin():
    assert storage.url_origin("https://example.com/p?x=1#h") == "https://example.com"
    assert storage.url_origin("https://example.com:443/p") == "https://example.com"
    assert storage.url_origin("http://example.com:8080/p") == "http://example.com:8080"
    assert storage.url_origin("http://example.com/") == "http://example.com"
    assert storage.url_origin("https://WWW.Example.com/") == "https://www.example.com"


# ---- _context_options (종류별 주입) ----


def test_context_options_none():
    kw, jwt = capture._context_options(None, insecure_tls=False)
    assert "http_credentials" not in kw and "storage_state" not in kw
    assert "extra_http_headers" not in kw and jwt is None
    assert kw["ignore_https_errors"] is False


def test_context_options_http_basic_origin_scoped():
    cred = C("http_basic", {"username": "u", "password": "p"}, "https://example.com")
    kw, jwt = capture._context_options(cred, insecure_tls=False)
    assert kw["http_credentials"] == {
        "username": "u", "password": "p",
        "origin": "https://example.com", "send": "always",   # 이 origin 에만
    }
    assert jwt is None


def test_context_options_session():
    state = {"cookies": [{"name": "s", "value": "1"}], "origins": []}
    cred = C("session", {"storage_state": state}, "https://example.com")
    kw, jwt = capture._context_options(cred, insecure_tls=True)
    assert kw["storage_state"] == state
    assert kw["ignore_https_errors"] is True
    assert jwt is None


def test_context_options_jwt_not_context_wide():
    cred = C("jwt", {"token": "eyJ.x.y"}, "https://example.com")
    kw, jwt = capture._context_options(cred, insecure_tls=False)
    # 컨텍스트 전체 헤더로는 절대 안 붙인다 (서드파티 누수 방지)
    assert "extra_http_headers" not in kw
    assert jwt == "Bearer eyJ.x.y"


# ---- jwt origin-스코프 라우트 (보안 핵심) ----


class _FakeRequest:
    def __init__(self, url):
        self.url = url
        self.headers = {"user-agent": "x"}


class _FakeRoute:
    def __init__(self, url):
        self.request = _FakeRequest(url)
        self.continued_headers = "UNSET"

    def continue_(self, headers=None):
        self.continued_headers = headers


class _FakeContext:
    def __init__(self):
        self.handler = None

    def route(self, pattern, handler):
        self.handler = handler


def test_jwt_header_only_for_target_origin():
    ctx = _FakeContext()
    capture._install_origin_scoped_header(ctx, "https://example.com", "Bearer T")
    # 같은 origin → Authorization 추가
    r = _FakeRoute("https://example.com/api/data")
    ctx.handler(r)
    assert r.continued_headers["Authorization"] == "Bearer T"
    # 서드파티 origin → 헤더 없이 통과 (토큰 누수 없음)
    r2 = _FakeRoute("https://cdn.other.com/lib.js")
    ctx.handler(r2)
    assert r2.continued_headers is None
    # http 다운그레이드(다른 origin) → 토큰 안 보냄
    r3 = _FakeRoute("http://example.com/x")
    ctx.handler(r3)
    assert r3.continued_headers is None


# ---- credentials.reveal_for_capture ----


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "RESOURCES_DIR", tmp_path / "resources")
    monkeypatch.setattr(config, "DOCUMENTS_DIR", tmp_path / "documents")
    monkeypatch.setattr(config, "SECRET_KEY", "test-secret")


def test_reveal_for_capture(tmp_db):
    with db.connect() as conn:
        sid = db.get_or_create_site(conn, "example.com")
        cid = credentials.add(conn, sid, "a", "jwt", {"token": "t.t.t"}, created_by=None)
        assert credentials.reveal_for_capture(conn, cid) == ("jwt", {"token": "t.t.t"})
        assert credentials.reveal_for_capture(conn, 99999) is None


# ---- pipeline: 페이지 자격증명 복호화·capture 전달 ----

URL = "https://example.com/secret"


def _stub_capture(monkeypatch):
    """capture.capture 를 가로채 credential 인자를 기록하고 캡처를 중단한다."""
    seen = {}

    def fake(url, out_dir, credential=None, **kwargs):
        seen["credential"] = credential
        raise capture.CaptureError(f"{url} 캡처 중단(테스트)")

    monkeypatch.setattr(pipeline.capture, "capture", fake)
    return seen


def _link_credential(kind="http_basic", payload=None):
    """example.com 에 자격증명을 만들고 URL 페이지에 연결, cred_id 반환."""
    payload = payload or {"username": "u", "password": "p"}
    with db.connect() as conn:
        sid = db.get_or_create_site(conn, "example.com")
        cid = credentials.add(conn, sid, "a", kind, payload, created_by=None)
        db.get_or_create_page(
            conn, URL, "example.com", storage.url_to_slug(URL), credential_id=cid
        )
    return cid


def test_pipeline_uses_stored_page_credential(tmp_db, monkeypatch):
    """재아카이빙(파라미터 없음)도 페이지에 저장된 자격증명을 capture 로 넘긴다."""
    seen = _stub_capture(monkeypatch)
    _link_credential("http_basic", {"username": "u", "password": "p"})
    with pytest.raises(Exception):
        pipeline.archive_url(URL)            # credential_id 파라미터 없이
    cred = seen["credential"]
    assert cred is not None
    assert cred.kind == "http_basic"
    assert cred.payload == {"username": "u", "password": "p"}
    assert cred.origin == "https://example.com"   # origin 스코프


def test_pipeline_no_credential_when_unlinked(tmp_db, monkeypatch):
    seen = _stub_capture(monkeypatch)
    with db.connect() as conn:
        db.get_or_create_page(conn, URL, "example.com", storage.url_to_slug(URL))
    with pytest.raises(Exception):
        pipeline.archive_url(URL)
    assert seen["credential"] is None


def test_pipeline_graceful_when_key_missing(tmp_db, monkeypatch):
    """키가 사라져 복호화가 안 되면 인증 없이 진행한다 (크래시·중단 없음)."""
    seen = _stub_capture(monkeypatch)
    _link_credential("jwt", {"token": "t.t.t"})
    monkeypatch.setattr(config, "SECRET_KEY", "")   # 키 제거
    with pytest.raises(Exception):
        pipeline.archive_url(URL)
    assert seen["credential"] is None


def test_pipeline_param_credential_takes_precedence(tmp_db, monkeypatch):
    """폼이 넘긴 credential_id 가 페이지 저장값보다 우선한다."""
    seen = _stub_capture(monkeypatch)
    _link_credential("http_basic", {"username": "old", "password": "p"})
    with db.connect() as conn:
        sid = db.get_site_by_key(conn, "example.com")["id"]
        new_id = credentials.add(
            conn, sid, "b", "jwt", {"token": "new.tok.en"}, created_by=None
        )
    with pytest.raises(Exception):
        pipeline.archive_url(URL, credential_id=new_id)
    assert seen["credential"].kind == "jwt"
    assert seen["credential"].payload == {"token": "new.tok.en"}


def test_pipeline_http_fallback_updates_credential_origin(tmp_db, monkeypatch):
    """https→http 폴백 시 credential.origin 도 http 로 갱신된다 (인증 누락 방지)."""
    seen = []

    def fake(url, out_dir, credential=None, **kwargs):
        seen.append((url, credential.origin if credential else None))
        if url.startswith("https://"):
            raise capture.CaptureConnectError(f"{url} 연결 실패(테스트)")
        raise capture.CaptureError(f"{url} 캡처 중단(테스트)")

    monkeypatch.setattr(pipeline.capture, "capture", fake)
    _link_credential("http_basic", {"username": "u", "password": "p"})
    with pytest.raises(Exception):
        pipeline.archive_url("example.com/secret")   # 스킴 생략 → https 추정 → http 폴백
    # https 시도는 https origin, http 폴백 재시도는 http origin 으로 맞춰진다
    assert ("https://example.com/secret", "https://example.com") in seen
    assert ("http://example.com/secret", "http://example.com") in seen


# ---- 크롤 자격증명 ----


def test_db_crawl_credential_claim_and_fk_cleanup(tmp_db):
    with db.connect() as conn:
        sid = db.get_or_create_site(conn, "example.com")
        cid = credentials.add(conn, sid, "a", "jwt", {"token": "t"}, created_by=None)
        crid = db.insert_crawl(
            conn, start_url="https://example.com/", scope_host="example.com",
            scope_path="/", max_pages=5, max_depth=1, delay_seconds=1,
            source="web", credential_id=cid,
        )
        db.insert_crawl_page(conn, crid, "https://example.com/", 0)
        item = db.claim_due_crawl_page(conn, db._utcnow())
        assert item["credential_id"] == cid          # claim 이 크롤 자격증명을 싣는다
        db.delete_site_credential(conn, cid)
        assert db.get_crawl(conn, crid)["credential_id"] is None   # FK 정리


def test_crawl_process_next_passes_credential(tmp_db):
    """크롤 페이지 처리 시 크롤의 자격증명이 archive_fn 으로 전달된다."""
    with db.connect() as conn:
        sid = db.get_or_create_site(conn, "example.com")
        cid = credentials.add(conn, sid, "a", "jwt", {"token": "t"}, created_by=None)
    crawler.start_crawl("https://example.com/docs/", credential_id=cid)
    seen = {}

    def fake_archive(url, source, link_rewriter=None, **kwargs):
        seen.update(kwargs, url=url)
        return pipeline.ArchiveOutcome(
            status="new", url=url, content_hash="0" * 64, snapshot_dir=None,
            taken_at="2026-06-13T00:00:00+00:00", last_taken_at=None,
            http_status=200, title=None,
        )

    step = crawler.process_next(archive_fn=fake_archive)
    assert step is not None and step.status == "new"
    assert seen["credential_id"] == cid


def test_crawl_schedule_keeps_credential(tmp_db):
    with db.connect() as conn:
        sid = db.get_or_create_site(conn, "example.com")
        cid = credentials.add(conn, sid, "a", "jwt", {"token": "t"}, created_by=None)
    sched = crawler.set_crawl_schedule(
        "https://example.com/docs/", 86400, credential_id=cid
    )
    assert sched["credential_id"] == cid


# ---- 문서 다운로드 자격증명 ----


def test_httpx_auth_per_kind():
    import base64
    assert credentials.httpx_auth("http_basic", {"username": "u", "password": "p"}) == {
        "headers": {"Authorization": "Basic " + base64.b64encode(b"u:p").decode()}
    }
    assert credentials.httpx_auth("jwt", {"token": "t.t.t"}) == {
        "headers": {"Authorization": "Bearer t.t.t"}
    }
    ck = [{"name": "s", "value": "1", "domain": "example.com", "path": "/"}]
    assert credentials.httpx_auth("session", {"storage_state": {"cookies": ck}}) == {
        "cookies": ck
    }
    assert credentials.httpx_auth("session", {"storage_state": {"cookies": []}}) == {}


def test_download_documents_origin_scoped(tmp_path, monkeypatch):
    """문서 다운로드 자격증명은 같은 origin 문서에만 실린다 (서드파티 누수 방지)."""
    seen = []

    def fake_one(url, dest_dir, headers, verify=True, cookies=None):
        seen.append((url, headers.get("Authorization")))
        return {"url": url, "file": "x", "bytes": 1, "sha256": "h",
                "content_type": "application/pdf"}

    monkeypatch.setattr(documents, "_download_one", fake_one)
    documents.download_documents(
        ["https://example.com/a.pdf", "https://cdn.other.com/b.pdf"],
        tmp_path,
        auth={"headers": {"Authorization": "Bearer T"}},
        auth_origin="https://example.com",
    )
    by_url = dict(seen)
    assert by_url["https://example.com/a.pdf"] == "Bearer T"   # 같은 origin → 인증
    assert by_url["https://cdn.other.com/b.pdf"] is None       # 서드파티 → 미인증


def test_pipeline_document_branch_passes_auth(tmp_db, monkeypatch):
    """URL 이 다운로드로 전환되면 페이지 자격증명을 download_direct 로 넘긴다."""
    seen = {}

    def fake_capture(url, out_dir, credential=None, **kwargs):
        raise capture.CaptureDownloadError(f"{url} 다운로드 URL")

    def fake_direct(url, dest_dir, verify=True, auth=None):
        seen["auth"] = auth
        raise ValueError("중단(테스트)")

    monkeypatch.setattr(pipeline.capture, "capture", fake_capture)
    monkeypatch.setattr(pipeline.documents, "download_direct", fake_direct)
    _link_credential("jwt", {"token": "tok"})
    with pytest.raises(Exception):
        pipeline.archive_url(URL)
    assert seen["auth"] == {"headers": {"Authorization": "Bearer tok"}}
