"""사이트 TLS 인증서 보관 — 수집·파싱, 버전 관리, 웹 표시, PEM 다운로드.

https 아카이빙 때 서버 리프 인증서를 받아 site_certificates 에 기록한다.
버전 식별은 DER sha256 지문 — 같은 인증서는 last_seen 갱신, 갱신된
인증서는 새 버전 행이 되고 이전 버전은 남는다.
"""
import json
import ssl
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chunchugwan import capture, certs, config, db, deletion, pipeline, storage
from chunchugwan.web import app as web_app

FIXTURES = Path(__file__).parent / "fixtures"

# conftest 의 자동 차단 픽스처(_no_cert_fetch)가 패치하기 전(모듈 임포트
# 시점)의 원본 — 실제 핸드셰이크 수집을 검증하는 테스트가 사용한다
_REAL_FETCH_CERT_INFO = certs.fetch_certificate_info


@pytest.fixture
def archive_env(tmp_path, monkeypatch):
    """임시 아카이브 루트 (인증 off)."""
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "RULES_PATH", tmp_path / "rules.json")
    monkeypatch.setattr(config, "RESOURCES_DIR", tmp_path / "resources")
    monkeypatch.setattr(config, "DOCUMENTS_DIR", tmp_path / "documents")
    monkeypatch.setattr(config, "AUTH_ENABLED", False)
    return tmp_path


def _fixture_der() -> bytes:
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    cert = x509.load_pem_x509_certificate(
        (FIXTURES / "selfsigned-cert.pem").read_bytes()
    )
    return cert.public_bytes(serialization.Encoding.DER)


def _info(fingerprint: str = "ab" * 32, host: str = "example.com") -> dict:
    """upsert 테스트용 파싱 결과 dict."""
    return {
        "host": host, "fingerprint": fingerprint,
        "subject": "CN=example.com", "issuer": "CN=Test CA", "serial": "1a2b",
        "san": json.dumps(["example.com", "www.example.com"]),
        "not_before": "2026-01-01T00:00:00+00:00",
        "not_after": "2026-12-31T23:59:59+00:00",
        "signature_algorithm": "sha256",
        "pem": "-----BEGIN CERTIFICATE-----\nMA==\n-----END CERTIFICATE-----\n",
    }


# ---- 파싱 ----


def test_parse_certificate_fields():
    info = certs.parse_certificate(_fixture_der())
    assert info["subject"] == "CN=localhost"
    assert info["issuer"] == "CN=localhost"  # 자체 서명 — 주체 = 발급자
    san = json.loads(info["san"])
    assert "localhost" in san and "127.0.0.1" in san
    assert len(info["fingerprint"]) == 64
    assert info["signature_algorithm"] == "sha256"
    assert info["pem"].startswith("-----BEGIN CERTIFICATE-----")
    assert info["not_before"] < info["not_after"]


# ---- 실제 TLS 핸드셰이크 수집 ----


def test_fetch_certificate_info_from_real_server(tmp_path):
    """자체 서명 https 서버에서도 인증서를 받는다 (검증 없는 수집)."""
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("<html></html>", encoding="utf-8")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(
        FIXTURES / "selfsigned-cert.pem", FIXTURES / "selfsigned-key.pem"
    )
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(site))
    )
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        info = _REAL_FETCH_CERT_INFO(f"https://127.0.0.1:{port}/index.html")
    finally:
        server.shutdown()
        thread.join(timeout=5)
    assert info is not None
    assert info["host"] == f"127.0.0.1:{port}"
    assert info["subject"] == "CN=localhost"
    assert info["fingerprint"] == certs.parse_certificate(_fixture_der())["fingerprint"]


def test_fetch_certificate_info_non_https():
    assert _REAL_FETCH_CERT_INFO("http://example.com/") is None


# ---- 버전 관리 (upsert) ----


def test_upsert_versions(archive_env):
    with db.connect() as conn:
        site_id = db.get_or_create_site(conn, "example.com")
        assert db.upsert_site_certificate(
            conn, site_id, _info("aa" * 32), verified=True
        ) is True  # 첫 버전
        assert db.upsert_site_certificate(
            conn, site_id, _info("aa" * 32), verified=True
        ) is False  # 같은 버전 — last_seen 갱신만
        assert db.upsert_site_certificate(
            conn, site_id, _info("bb" * 32), verified=True
        ) is True  # 갱신된 인증서 — 새 버전
        rows = db.list_site_certificates(conn, site_id)
        assert len(rows) == 2
        assert {r["fingerprint"] for r in rows} == {"aa" * 32, "bb" * 32}


# ---- 파이프라인 통합 ----


def _fake_capture(monkeypatch):
    def fake(url, out_dir, remove_selectors=(), link_rewriter=None, session=None,
             resource_fallback=None, insecure_tls=False):
        return capture.CaptureResult(
            final_url=url, http_status=200, title="제목",
            raw_html="<html><body>내용</body></html>",
            content_html="<html><body>내용</body></html>",
        )

    monkeypatch.setattr(pipeline.capture, "capture", fake)


def test_pipeline_records_certificate_and_renewal(archive_env, monkeypatch):
    _fake_capture(monkeypatch)
    fingerprints = iter(["aa" * 32, "aa" * 32, "cc" * 32])
    monkeypatch.setattr(
        pipeline.certs, "fetch_certificate_info",
        lambda url: _info(next(fingerprints)),
    )
    pipeline.archive_url("https://example.com/a")
    # 콘텐츠 동일(unchanged)이어도 인증서 확인은 기록된다
    pipeline.archive_url("https://example.com/a")
    # 인증서 갱신 — 콘텐츠가 그대로(저장 생략)여도 새 버전 행이 남는다
    outcome = pipeline.archive_url("https://example.com/a")
    assert outcome.status == "unchanged"
    with db.connect() as conn:
        site = db.get_site_by_key(conn, "example.com")
        rows = db.list_site_certificates(conn, site["id"])
    assert {r["fingerprint"] for r in rows} == {"aa" * 32, "cc" * 32}
    aa = next(r for r in rows if r["fingerprint"] == "aa" * 32)
    assert aa["verified"] == 1


def test_pipeline_skips_certificate_for_http(archive_env, monkeypatch):
    _fake_capture(monkeypatch)
    called: list[str] = []
    monkeypatch.setattr(
        pipeline.certs, "fetch_certificate_info",
        lambda url: called.append(url),
    )
    pipeline.archive_url("http://example.com/a")
    assert called == []  # http — 수집 시도 없음 (fetch 는 https 만)


# ---- 웹 표시·PEM 다운로드 ----


@pytest.fixture
def client(archive_env):
    with db.connect():
        pass
    web_app._active_jobs.clear()
    yield TestClient(web_app.app)
    web_app._active_jobs.clear()


def _seed_site_with_cert() -> tuple[int, int]:
    with db.connect() as conn:
        page_id = db.get_or_create_page(
            conn, "https://example.com/a", "example.com",
            storage.url_to_slug("https://example.com/a"),
        )
        site_id = db.get_page_by_id(conn, page_id)["site_id"]
        db.upsert_site_certificate(conn, site_id, _info("ab" * 32), verified=False)
        cert = db.list_site_certificates(conn, site_id)[0]
    return site_id, cert["id"]


def test_site_view_shows_certificate(client):
    site_id, _ = _seed_site_with_cert()
    res = client.get(f"/sites/{site_id}")
    assert "인증서" in res.text
    assert "CN=example.com" in res.text and "CN=Test CA" in res.text
    assert "현재" in res.text
    assert "검증 안 됨" in res.text  # verified=False 뱃지


def test_certificate_pem_download(client):
    site_id, cert_id = _seed_site_with_cert()
    res = client.get(f"/sites/{site_id}/certificates/{cert_id}.pem")
    assert res.status_code == 200
    assert res.text.startswith("-----BEGIN CERTIFICATE-----")
    assert "attachment" in res.headers["content-disposition"]
    # 다른 사이트 소속으로는 내려받을 수 없다
    assert client.get(f"/sites/{site_id + 99}/certificates/{cert_id}.pem").status_code == 404


# ---- 삭제 정리 ----


def test_site_delete_removes_certificates(archive_env):
    site_id, _ = _seed_site_with_cert()
    deletion.delete_site(site_id)
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM site_certificates").fetchall()
    assert rows == []


def test_prune_removes_certificates(archive_env):
    with db.connect() as conn:
        page_id = db.get_or_create_page(
            conn, "https://example.com/a", "example.com",
            storage.url_to_slug("https://example.com/a"),
        )
        site_id = db.get_page_by_id(conn, page_id)["site_id"]
        db.upsert_site_certificate(conn, site_id, _info(), verified=True)
        db.delete_page(conn, page_id)  # 마지막 소속 행 — 사이트 + 인증서 정리
        assert db.get_site(conn, site_id) is None
        assert conn.execute("SELECT COUNT(*) c FROM site_certificates").fetchone()["c"] == 0
