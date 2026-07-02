"""확장 진입 경로(/extension/*) 리다이렉트 + 새 아카이빙 네트워크 태그·자격증명 연결.

- 확장은 /extension/* 만 쓰고 서버가 정식 SPA 경로로 302 한다(화면 구조와 분리).
- 새 아카이빙은 네트워크 태그 목록(/network-tags)·자격증명 연결을 받는다.
"""
import pytest
from fastapi.testclient import TestClient

from chunchugwan import auth, config, db
from chunchugwan.web import app as web_app

POST_HEADERS = {"X-Requested-With": "fetch", "Origin": "http://testserver"}


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    for name, sub in (
        ("ARCHIVE_ROOT", ""), ("SITES_DIR", "sites"), ("DB_PATH", "index.db"),
        ("CACHE_DIR", "cache"), ("RESOURCES_DIR", "resources"),
        ("DOCUMENTS_DIR", "documents"),
    ):
        monkeypatch.setattr(config, name, tmp_path / sub if sub else tmp_path)
    monkeypatch.setattr(config, "AUTH_ENABLED", True)


def _client(role="admin", email="boss@test.co"):
    with db.connect() as conn:
        if role == "founder":
            uid = db.create_first_admin(conn, email, auth.hash_password("x" * 12))
        else:
            uid = db.create_user(conn, email, auth.hash_password("x" * 12), role=role)
        token = auth.issue_session(conn, uid)
    c = TestClient(web_app.app)
    c.cookies.set(config.SESSION_COOKIE, token)
    return c


def _seed_page(url="https://example.com/p"):
    with db.connect() as conn:
        pid = db.get_or_create_page(conn, url, "example.com", "p-abcd1234")
        page = db.get_page_by_id(conn, pid)
        return pid, page["site_id"]


# ---- 확장 진입 경로 ----


def test_extension_page_redirects_to_nested(tmp_db):
    pid, site_id = _seed_page()
    c = _client("founder")
    r = c.get(f"/extension/page/{pid}", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == f"/archive/sites/{site_id}/page/{pid}"


def test_extension_static_redirects(tmp_db):
    c = _client("founder")
    cases = {
        "/extension/archives": "/settings/archives",
        "/extension/needs-human": "/archive/needs-human",
        "/extension/crawl/5": "/crawls/5",
        "/extension/token": "/settings/api-keys#ext-token-form",
    }
    for path, target in cases.items():
        r = c.get(path, follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == target


def test_extension_go_known_and_unknown(tmp_db):
    pid, site_id = _seed_page("https://example.com/known")
    c = _client("founder")
    r = c.get("/extension/go?url=https://example.com/known", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == f"/archive/sites/{site_id}/page/{pid}"
    r2 = c.get("/extension/go?url=https://nope.test/x", follow_redirects=False)
    assert r2.status_code == 302 and r2.headers["location"].startswith("/archive/new?url=")


# ---- 새 아카이빙: 네트워크 태그·자격증명 ----


def test_network_tags_endpoint_gated(tmp_db):
    archiver = _client("archiver", "a@test.co")
    viewer = _client("viewer", "v@test.co")
    assert archiver.get("/api/web/network-tags").status_code == 200
    assert viewer.get("/api/web/network-tags").status_code == 403


def test_network_tags_endpoint_includes_crawl_defaults(tmp_db):
    """새 아카이빙 폼이 사이트 아카이브 기본값을 폼에 미리 채울 수 있게 함께 내려준다."""
    with db.connect() as conn:
        db.set_setting(conn, db.CRAWL_DEFAULT_MAX_PAGES_KEY, "42")
        db.set_setting(conn, db.CRAWL_DEFAULT_MAX_DEPTH_KEY, "4")
        db.set_setting(conn, db.CRAWL_DEFAULT_DELAY_KEY, "7")
    archiver = _client("archiver", "a@test.co")
    body = archiver.get("/api/web/network-tags").json()
    assert body["crawl_defaults"] == {"max_pages": 42, "max_depth": 4, "delay": 7}


def test_archive_credentials_endpoint_gated(tmp_db):
    admin = _client("founder")
    viewer = _client("viewer", "v@test.co")
    # 관리자(자격증명 관리)는 200, 권한 없는 viewer 는 403
    assert admin.get("/api/web/archive/credentials?url=https://x.test").status_code == 200
    assert viewer.get("/api/web/archive/credentials?url=https://x.test").status_code == 403


def test_logs_include_site_id(tmp_db):
    """아카이브 로그 행에 page_site_id 가 실린다 (중첩 링크용)."""
    with db.connect() as conn:
        pid = db.get_or_create_page(conn, "https://example.com/l", "example.com", "l-abcd1234")
        db.insert_archive_log(
            conn, url="https://example.com/l", domain="example.com", source="web",
            status="new", started_at="2026-06-18T00:00:00+00:00", duration_ms=5,
            page_id=pid,
        )
    c = _client("founder")
    rows = c.get("/api/web/logs").json()["items"]
    assert rows and "page_site_id" in rows[0]["log"]
