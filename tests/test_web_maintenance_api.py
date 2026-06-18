"""SvelteKit SPA 유지보수 API(/api/web/system/compact·search/reindex) 테스트.

Phase C2 컷오버를 위해 SSR system_routes 의 compact·전체 재색인을 /api/web 으로
보강한 엔드포인트를 검증한다. 재색인은 SSR 과 같은 인메모리 상태를 공유한다.
"""
import pytest
from fastapi.testclient import TestClient

from chunchugwan import auth, config, db, searchindex
from chunchugwan.web import app as web_app
from chunchugwan.web import maintenance

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


@pytest.fixture(autouse=True)
def _reset_reindex_state():
    """SSR 과 공유하는 모듈 전역 재색인 상태를 테스트 간 격리한다."""
    yield
    with maintenance._reindex_lock:
        maintenance._reindex_state.update(
            running=False, done=0, total=0, result=None, error=None, finished_at=None)


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


# ---- compact ----


def test_compact_requires_manage_system(tmp_db):
    _, arch = make_user(email="arch@test.co", role="archiver")
    assert client_for(arch).post("/api/web/system/compact", headers=POST_HEADERS).status_code == 403
    assert client_for().post("/api/web/system/compact", headers=POST_HEADERS).status_code == 401


def test_compact_nothing_to_do(tmp_db):
    """대상이 없으면 실행 없이 ran=False."""
    r = admin_client().post("/api/web/system/compact", headers=POST_HEADERS)
    assert r.status_code == 200
    assert r.json()["ran"] is False


# ---- reindex ----


def test_reindex_requires_manage_system(tmp_db):
    _, arch = make_user(email="arch@test.co", role="archiver")
    assert client_for(arch).post("/api/web/system/search/reindex",
                                 headers=POST_HEADERS).status_code == 403
    assert client_for(arch).get("/api/web/system/search/reindex/status").status_code == 403


def test_reindex_unavailable(tmp_db, monkeypatch):
    monkeypatch.setattr(searchindex, "available", lambda: False)
    r = admin_client().post("/api/web/system/search/reindex", headers=POST_HEADERS)
    assert r.status_code == 400


def test_reindex_starts_and_status(tmp_db, monkeypatch):
    monkeypatch.setattr(searchindex, "available", lambda: True)
    monkeypatch.setattr(searchindex, "reindex_all", lambda progress=None: 0)
    c = admin_client()
    r = c.post("/api/web/system/search/reindex", headers=POST_HEADERS)
    assert r.status_code == 200 and r.json()["started"] is True
    status = c.get("/api/web/system/search/reindex/status").json()
    assert "running" in status and "done" in status and "total" in status
