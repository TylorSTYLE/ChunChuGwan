"""SvelteKit SPA 사용자 관리 API(/api/web/system/users) 테스트 — 목록·역할·권한·삭제·초대.

Phase C2 컷오버로 제거되는 SSR 사용자 화면(system_routes.users_view 와 변경 POST)의 검증
로직을 JSON API 기준으로 대체한다. SSR test_roles·test_permissions_granular 의 단정
(라스트-관리자 잠김 방지·founder/withdrawn 불변·manage_users vs manage_system 분리)에 대응.
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


def make_user(email, password="userpass123", role="archiver", overrides=None):
    with db.connect() as conn:
        pw = auth.hash_password(password) if password else None
        uid = db.create_user(conn, email, pw, role=role)
        if overrides is not None:
            db.set_permission_overrides(conn, uid, overrides)
        token = auth.issue_session(conn, uid)
    return uid, token


def make_founder(email="boss@test.co", password="bosspass123"):
    with db.connect() as conn:
        uid = db.create_first_admin(conn, email, auth.hash_password(password))
    return uid


def client_for(token=None):
    c = TestClient(web_app.app)
    if token:
        c.cookies.set(config.SESSION_COOKIE, token)
    return c


# ---- 권한 게이트 ----


def test_users_requires_manage_users(tmp_db):
    _, arch = make_user("arch@test.co", role="archiver")
    assert client_for().get("/api/web/system/users").status_code == 401
    assert client_for(arch).get("/api/web/system/users").status_code == 403


def test_manage_users_vs_manage_system_split(tmp_db):
    """manage_users 만 가진 계정: /system/users 통과, /system(manage_system) 거부."""
    _, token = make_user("u@test.co", role="viewer", overrides={"manage_users": True})
    c = client_for(token)
    assert c.get("/api/web/system/users").status_code == 200
    assert c.get("/api/web/system").status_code == 403


# ---- 목록 ----


def test_users_list(tmp_db):
    make_founder()
    _, admin = make_user("admin@test.co", role="admin")
    body = client_for(admin).get("/api/web/system/users").json()
    emails = {u["email"] for u in body["users"]}
    assert {"boss@test.co", "admin@test.co"} <= emails
    assert "roles" in body and "user_perms" in body
    assert "permissions_catalog" in body


# ---- 역할 변경 ----


def test_user_role_change(tmp_db):
    _, admin = make_user("admin@test.co", role="admin")
    target, _ = make_user("v@test.co", role="viewer")
    r = client_for(admin).post(f"/api/web/system/users/{target}/role",
                               json={"role": "archiver"}, headers=POST_HEADERS)
    assert r.status_code == 200
    with db.connect() as conn:
        assert db.get_user_by_id(conn, target)["role"] == "archiver"


def test_user_role_invalid(tmp_db):
    _, admin = make_user("admin@test.co", role="admin")
    target, _ = make_user("v@test.co", role="viewer")
    r = client_for(admin).post(f"/api/web/system/users/{target}/role",
                               json={"role": "superboss"}, headers=POST_HEADERS)
    assert r.status_code == 400


def test_user_role_founder_protected(tmp_db):
    founder = make_founder()
    _, admin = make_user("admin@test.co", role="admin")
    r = client_for(admin).post(f"/api/web/system/users/{founder}/role",
                               json={"role": "viewer"}, headers=POST_HEADERS)
    assert r.status_code == 400


def test_user_role_withdrawn_protected(tmp_db):
    _, admin = make_user("admin@test.co", role="admin")
    target, _ = make_user("w@test.co", role="viewer")
    with db.connect() as conn:
        db.set_role(conn, target, "withdrawn")
    r = client_for(admin).post(f"/api/web/system/users/{target}/role",
                               json={"role": "archiver"}, headers=POST_HEADERS)
    assert r.status_code == 400


def test_user_role_last_manage_users_blocked(tmp_db):
    """유일한 manage_users 보유 계정의 자기 강등은 거부(라스트-관리자 잠김 방지)."""
    uid, admin = make_user("admin@test.co", role="admin")
    r = client_for(admin).post(f"/api/web/system/users/{uid}/role",
                               json={"role": "viewer"}, headers=POST_HEADERS)
    assert r.status_code == 400


# ---- 세분 권한 ----


def test_user_permissions_override(tmp_db):
    _, admin = make_user("admin@test.co", role="admin")
    target, _ = make_user("v@test.co", role="viewer")
    # viewer 에게 archive 권한 추가
    r = client_for(admin).post(
        f"/api/web/system/users/{target}/permissions",
        json={"permissions": ["view", "search", "archive"]}, headers=POST_HEADERS)
    assert r.status_code == 200
    with db.connect() as conn:
        eff = db.effective_permissions(
            "viewer", db.get_user_by_id(conn, target)["permission_overrides"])
    assert "archive" in eff


def test_user_permissions_founder_protected(tmp_db):
    founder = make_founder()
    _, admin = make_user("admin@test.co", role="admin")
    r = client_for(admin).post(f"/api/web/system/users/{founder}/permissions",
                               json={"permissions": ["view"]}, headers=POST_HEADERS)
    assert r.status_code == 400


# ---- 삭제 ----


def test_user_delete_email_mismatch(tmp_db):
    _, admin = make_user("admin@test.co", role="admin")
    target, _ = make_user("v@test.co", role="viewer")
    r = client_for(admin).post(f"/api/web/system/users/{target}/delete",
                               json={"email": "wrong@test.co"}, headers=POST_HEADERS)
    assert r.status_code == 400


def test_user_delete_success_and_founder_protected(tmp_db):
    founder = make_founder()
    _, admin = make_user("admin@test.co", role="admin")
    target, _ = make_user("v@test.co", role="viewer")
    c = client_for(admin)
    # founder 는 삭제 불가
    assert c.post(f"/api/web/system/users/{founder}/delete",
                  json={"email": "boss@test.co"}, headers=POST_HEADERS).status_code == 400
    # 이메일 일치 시 삭제
    assert c.post(f"/api/web/system/users/{target}/delete",
                  json={"email": "v@test.co"}, headers=POST_HEADERS).status_code == 200
    with db.connect() as conn:
        assert db.get_user_by_id(conn, target) is None


# ---- 초대 ----


def test_user_invite(tmp_db):
    _, admin = make_user("admin@test.co", role="admin")
    c = client_for(admin)
    r = c.post("/api/web/system/users/invite",
               json={"email": "new@test.co", "role": "viewer"}, headers=POST_HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["email"] == "new@test.co"
    assert "/invite/" in body["link"]
    # 같은 이메일 재초대 시도 → 이미 초대/가입 충돌은 아니나 가입자 중복은 거부
    make_user("dup@test.co", role="viewer")
    dup = c.post("/api/web/system/users/invite",
                 json={"email": "dup@test.co", "role": "viewer"}, headers=POST_HEADERS)
    assert dup.status_code == 400


def test_invite_delete_404(tmp_db):
    _, admin = make_user("admin@test.co", role="admin")
    r = client_for(admin).post("/api/web/system/users/invite/9999/delete",
                               headers=POST_HEADERS)
    assert r.status_code == 404
