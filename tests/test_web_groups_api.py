"""SvelteKit SPA 권한 그룹 API(/api/web/system/groups) 테스트 — 목록·생성·편집·삭제.

Phase C2 컷오버로 제거되는 SSR 그룹 화면(system_routes.groups_view 와 변경 POST)의 검증
로직을 JSON API 기준으로 대체한다. SSR test_permission_groups 의 단정(빌트인/커스텀·
member_count·빌트인 삭제 거부·라스트-관리자 잠김 방지)에 대응한다.
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


def make_user(email, password="userpass123", role="archiver"):
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
    _, token = make_user("admin@test.co", role="admin")
    return client_for(token)


# ---- 권한 게이트 ----


def test_groups_requires_manage_system(tmp_db):
    _, arch = make_user("arch@test.co", role="archiver")
    assert client_for().get("/api/web/system/groups").status_code == 401
    assert client_for(arch).get("/api/web/system/groups").status_code == 403


# ---- 목록 ----


def test_groups_list_builtin_and_member_count(tmp_db):
    make_user("v@test.co", role="viewer")  # viewer 그룹 멤버 1
    body = admin_client().get("/api/web/system/groups").json()
    by_name = {g["name"]: g for g in body["groups"]}
    assert by_name["admin"]["is_builtin"] is True
    assert by_name["viewer"]["is_builtin"] is True
    assert by_name["viewer"]["member_count"] >= 1
    assert "permissions_catalog" in body


# ---- 생성·삭제 ----


def test_group_create_and_delete(tmp_db):
    c = admin_client()
    r = c.post("/api/web/system/groups",
               json={"name": "editors", "label": "편집자", "permissions": ["view", "archive"]},
               headers=POST_HEADERS)
    assert r.status_code == 200
    groups = c.get("/api/web/system/groups").json()["groups"]
    editors = next(g for g in groups if g["name"] == "editors")
    assert editors["is_builtin"] is False
    assert set(editors["permissions"]) == {"view", "archive"}
    # 멤버 없으면 삭제 가능
    assert c.post("/api/web/system/groups/editors/delete",
                  headers=POST_HEADERS).status_code == 200


def test_group_delete_builtin_forbidden(tmp_db):
    r = admin_client().post("/api/web/system/groups/admin/delete", headers=POST_HEADERS)
    assert r.status_code == 400


def test_group_delete_with_members_forbidden(tmp_db):
    c = admin_client()
    c.post("/api/web/system/groups",
           json={"name": "editors", "label": "편집자", "permissions": ["view"]},
           headers=POST_HEADERS)
    make_user("member@test.co", role="editors")
    r = c.post("/api/web/system/groups/editors/delete", headers=POST_HEADERS)
    assert r.status_code == 400


def test_group_delete_404(tmp_db):
    r = admin_client().post("/api/web/system/groups/ghost/delete", headers=POST_HEADERS)
    assert r.status_code == 404
