"""SvelteKit SPA 데이터 이전(마이그레이션) API(/api/web/system/migration/*) 테스트.

Phase C2 컷오버를 위해 SSR system_routes 의 이전 모드 enable/regenerate/disable 을
/api/web 으로 보강한 엔드포인트를 검증한다. 토큰은 1회만 노출되고 해시만 저장한다.
"""
import pytest
from fastapi.testclient import TestClient

from chunchugwan import auth, config, db
from chunchugwan.web import app as web_app

POST_HEADERS = {"X-Requested-With": "fetch", "Origin": "http://testserver"}


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "RESOURCES_DIR", tmp_path / "resources")
    monkeypatch.setattr(config, "DOCUMENTS_DIR", tmp_path / "documents")
    monkeypatch.setattr(config, "AUTH_ENABLED", True)
    web_app._active_jobs.clear()
    yield
    web_app._active_jobs.clear()


def make_user(email="admin@test.co", password="adminpass123", role="admin"):
    with db.connect() as conn:
        pw = auth.hash_password(password) if password else None
        uid = db.create_user(conn, email, pw, role=role)
        token = auth.issue_session(conn, uid)
    return uid, token


def client_for(token=None):
    c = TestClient(web_app.app)
    if token:
        c.cookies.set(config.SESSION_COOKIE, token)
    return c


def admin_client():
    _, token = make_user()
    return client_for(token)


def migration_on():
    with db.connect() as conn:
        return db.migration_mode_enabled(conn)


# ---- 권한 게이트 ----


def test_migration_requires_manage_system(tmp_db):
    _, arch = make_user(email="arch@test.co", role="archiver")
    assert client_for(arch).post("/api/web/system/migration/enable",
                                 headers=POST_HEADERS).status_code == 403
    assert client_for().post("/api/web/system/migration/enable",
                             headers=POST_HEADERS).status_code == 401


# ---- enable / regenerate / disable ----


def test_migration_enable_returns_token(tmp_db):
    c = admin_client()
    r = c.post("/api/web/system/migration/enable", headers=POST_HEADERS)
    assert r.status_code == 200
    assert r.json()["token"]  # 원문 1회 노출
    assert migration_on() is True
    assert c.get("/api/web/system").json()["migration_mode"] is True


def test_migration_regenerate_changes_token(tmp_db):
    c = admin_client()
    first = c.post("/api/web/system/migration/enable", headers=POST_HEADERS).json()["token"]
    second = c.post("/api/web/system/migration/regenerate", headers=POST_HEADERS).json()["token"]
    assert first != second
    assert migration_on() is True  # 재발급은 모드를 끄지 않는다


def test_migration_disable(tmp_db):
    c = admin_client()
    c.post("/api/web/system/migration/enable", headers=POST_HEADERS)
    r = c.post("/api/web/system/migration/disable", headers=POST_HEADERS)
    assert r.status_code == 200
    assert migration_on() is False


def test_migration_token_not_stored_plaintext(tmp_db):
    """발급 토큰은 해시만 저장 — 평문이 settings 에 남지 않는다(원칙 6)."""
    c = admin_client()
    token = c.post("/api/web/system/migration/enable", headers=POST_HEADERS).json()["token"]
    with db.connect() as conn:
        stored = db.get_setting(conn, db.MIGRATION_TOKEN_HASH_KEY)
    assert stored is not None and stored != token
