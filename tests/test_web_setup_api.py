"""SPA 최초 설정·승인 대기 JSON API + auth_gate 통과 규칙.

first_run(사용자 0명) 중에는 /api/web/auth/setup* 만, pending 계정에는 /api/web/me·
/ui 만 통과시킨다 — SPA 가 setup·pending 화면을 직접 띄운다.
"""
import pytest
from fastapi.testclient import TestClient

from chunchugwan import auth, config, db, migration
from chunchugwan.web import app as web_app

POST = {"X-Requested-With": "fetch", "Origin": "http://testserver"}


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "RESOURCES_DIR", tmp_path / "resources")
    monkeypatch.setattr(config, "DOCUMENTS_DIR", tmp_path / "documents")
    monkeypatch.setattr(config, "AUTH_ENABLED", True)
    # 환경변수 부트스트랩 관리자가 없어야 first_run 이 유지된다
    monkeypatch.delenv("WCCG_ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("WCCG_ADMIN_PASSWORD", raising=False)
    # 네트워크 이전 전역 상태 격리 — 다른 테스트가 남긴 _pull_state 가 누수되지 않게
    monkeypatch.setattr(migration, "_pull_state", {"status": "idle"})
    with db.connect():
        pass


def client():
    return TestClient(web_app.app)


# ---- first_run: setup ----


def test_setup_status_needed_on_first_run(tmp_db):
    c = client()
    body = c.get("/api/web/auth/setup").json()
    assert body["needed"] is True and body["migration"]["status"] == "idle"


def test_first_run_blocks_other_api(tmp_db):
    # 설정 전에는 setup 외 /api 는 401 (게이트)
    assert client().get("/api/web/me").status_code == 401


def test_first_run_serves_spa_shell(tmp_db):
    # SPA 셸(/ui)은 first_run 중에도 로드된다 (설정 화면을 띄우려고)
    r = client().get("/ui", follow_redirects=False)
    assert r.status_code == 200


def test_setup_creates_admin_and_session(tmp_db):
    c = client()
    r = c.post(
        "/api/web/auth/setup",
        json={"email": "boss@test.co", "password": "bosspass1234"}, headers=POST,
    )
    assert r.status_code == 200 and r.json()["status"] == "active"
    # 세션이 서고, 설정 완료로 바뀐다
    assert c.get("/api/web/me").json()["authenticated"] is True
    assert c.get("/api/web/auth/setup").json()["needed"] is False


def test_setup_rejected_when_users_exist(tmp_db):
    with db.connect() as conn:
        db.create_first_admin(conn, "boss@test.co", auth.hash_password("bosspass1234"))
    r = client().post(
        "/api/web/auth/setup",
        json={"email": "x@test.co", "password": "anotherpw123"}, headers=POST,
    )
    assert r.status_code == 403


def test_setup_validates_credentials(tmp_db):
    r = client().post(
        "/api/web/auth/setup",
        json={"email": "bad", "password": "short"}, headers=POST,
    )
    assert r.status_code == 400


def test_migrate_status_open_on_first_run(tmp_db):
    assert client().get("/api/web/auth/setup/migrate/status").json()["status"] == "idle"


# ---- pending 계정 게이트 ----


def _make_pending_client(tmp_db):
    with db.connect() as conn:
        db.create_first_admin(conn, "boss@test.co", auth.hash_password("bosspass1234"))
        db.create_user(conn, "wait@test.co", auth.hash_password("waitpass1234"), role="pending")
    c = client()
    c.post("/login", data={"email": "wait@test.co", "password": "waitpass1234"})
    return c


def test_pending_me_allowed(tmp_db):
    c = _make_pending_client(tmp_db)
    me = c.get("/api/web/me").json()
    assert me["authenticated"] is True and me["user"]["role"] == "pending"


def test_pending_blocked_from_other_api(tmp_db):
    c = _make_pending_client(tmp_db)
    # pending 은 /me 외 다른 /api/web 은 /pending 으로 리다이렉트(302)
    r = c.get("/api/web/archives", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/pending"


def test_pending_serves_spa_shell(tmp_db):
    c = _make_pending_client(tmp_db)
    assert c.get("/ui/pending", follow_redirects=False).status_code == 200
