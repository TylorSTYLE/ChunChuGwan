"""확장 토큰(사용자 귀속 API 키) — 권한 파생·매 요청 재평가·격리·계정 화면 발급/폐기."""
import pytest
from fastapi.testclient import TestClient

from chunchugwan import auth, config, db, storage
from chunchugwan.web import app as web_app
from chunchugwan.web import permissions

URL = "https://example.com/post"


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "RESOURCES_DIR", tmp_path / "resources")
    monkeypatch.setattr(config, "DOCUMENTS_DIR", tmp_path / "documents")


def _seed():
    domain, slug = "example.com", storage.url_to_slug(URL)
    with db.connect() as conn:
        db.create_first_admin(conn, "boss@test.co", auth.hash_password("bosspass1234"))
        db.create_user(conn, "arch@test.co", auth.hash_password("password1234"), role="archiver")
        db.create_user(conn, "viewer@test.co", auth.hash_password("password1234"), role="viewer")
        db.create_user(conn, "pend@test.co", auth.hash_password("password1234"), role="pending")
        page_id = db.get_or_create_page(conn, URL, domain, slug)
        snap_dir = storage.page_dir(domain, slug) / "2026-06-01T00-00-00"
        snap_dir.mkdir(parents=True)
        (snap_dir / "content.md").write_text("본문", encoding="utf-8")
        db.insert_snapshot(
            conn, page_id, taken_at="2026-06-01T00:00:00+00:00",
            dir_name="2026-06-01T00-00-00", content_hash="h1",
            final_url=URL, http_status=200, changed=1,
        )


@pytest.fixture
def client(tmp_db):
    _seed()
    web_app._active_jobs.clear()
    yield TestClient(web_app.app)
    web_app._active_jobs.clear()


def _login(client, email, password="password1234"):
    return client.post(
        "/login", data={"email": email, "password": password}, follow_redirects=False
    )


def _uid(email):
    with db.connect() as conn:
        return db.get_user_by_email(conn, email)["id"]


def _set_role(email, role):
    with db.connect() as conn:
        conn.execute("UPDATE users SET role = ? WHERE email = ?", (role, email))


def _issue_owned(name, owner_email, *, can_view=True, can_archive=False):
    uid = _uid(owner_email)
    with db.connect() as conn:
        return auth.issue_api_key(
            conn, name, can_view=can_view, can_archive=can_archive,
            created_by=uid, owner_user_id=uid, ttl_seconds=None,
        )


def _headers(token):
    return {"Authorization": f"Bearer {token}"}


# ---- 권한 파생 ----


def test_token_permissions_for_role():
    assert permissions.token_permissions_for_role("admin") == (True, True)
    assert permissions.token_permissions_for_role("archiver") == (True, True)
    assert permissions.token_permissions_for_role("viewer") == (True, False)
    for role in ("pending", "blocked", "withdrawn"):
        assert permissions.token_permissions_for_role(role) == (False, False)


# ---- DB 격리 (시스템 키 vs 사용자 토큰) ----


def test_owner_and_system_keys_are_separated(client):
    uid = _uid("viewer@test.co")
    with db.connect() as conn:
        auth.issue_api_key(conn, "sys", can_view=True, can_archive=False,
                           created_by=1, ttl_seconds=None)
        auth.issue_api_key(conn, "ext", can_view=True, can_archive=False,
                           created_by=uid, owner_user_id=uid, ttl_seconds=None)
        system = db.list_system_api_keys(conn)
        owned = db.list_api_keys_for_owner(conn, uid)
    assert [k["name"] for k in system] == ["sys"]
    assert [k["name"] for k in owned] == ["ext"]
    assert owned[0]["owner_user_id"] == uid


def test_get_api_key_returns_regardless_of_expiry(client):
    token = _issue_owned("ext", "viewer@test.co")
    with db.connect() as conn:
        key = auth.resolve_api_key(conn, token)
        assert db.get_api_key(conn, key["id"])["name"] == "ext"
        assert db.get_api_key(conn, 99999) is None


# ---- 매 요청 권한 재평가 (_api_auth) ----


def test_owner_token_permissions_follow_current_role(client, monkeypatch):
    monkeypatch.setattr(
        web_app.pipeline, "archive_url",
        lambda url, force=False, source="cli": None,
    )
    # viewer 로 발급 — 저장 컬럼은 can_archive=0
    token = _issue_owned("ext", "viewer@test.co", can_view=True, can_archive=False)
    assert client.get("/api/v1/pages", headers=_headers(token)).status_code == 200
    assert client.post(
        "/api/v1/archive", json={"url": "https://example.com/x"},
        headers=_headers(token),
    ).status_code == 403
    # archiver 로 승격 → 저장 컬럼과 무관하게 현재 역할로 아카이브 허용
    _set_role("viewer@test.co", "archiver")
    assert client.post(
        "/api/v1/archive", json={"url": "https://example.com/x"},
        headers=_headers(token),
    ).status_code == 202
    # blocked 로 강등 → 토큰 무효(401)
    _set_role("viewer@test.co", "blocked")
    assert client.get("/api/v1/pages", headers=_headers(token)).status_code == 401


def test_owner_token_dies_with_owner(client):
    token = _issue_owned("ext", "viewer@test.co")
    with db.connect() as conn:
        db.delete_user(conn, _uid("viewer@test.co"))
    assert client.get("/api/v1/pages", headers=_headers(token)).status_code == 401


def test_system_key_permissions_use_stored_columns(client):
    """owner=NULL 시스템 키는 기존대로 저장 컬럼만 본다 (재평가 비대상)."""
    with db.connect() as conn:
        token = auth.issue_api_key(conn, "sys", can_view=False, can_archive=False,
                                   created_by=1, ttl_seconds=None)
    assert client.get("/api/v1/pages", headers=_headers(token)).status_code == 403


# ---- 계정 화면 발급/폐기 ----


def test_account_issues_owner_token(client):
    _login(client, "viewer@test.co")
    r = client.post(
        "/settings/account/extension-tokens",
        data={"name": "chrome-ext", "expiry": "permanent"}, follow_redirects=False,
    )
    assert r.status_code == 303
    assert "new_token=wccg_" in r.headers["location"]
    uid = _uid("viewer@test.co")
    with db.connect() as conn:
        owned = db.list_api_keys_for_owner(conn, uid)
    assert len(owned) == 1 and owned[0]["name"] == "chrome-ext"
    # viewer 역할 → 보기만 파생
    assert owned[0]["can_view"] == 1 and owned[0]["can_archive"] == 0


def test_account_archiver_token_gets_archive_perm(client):
    _login(client, "arch@test.co")
    client.post(
        "/settings/account/extension-tokens",
        data={"name": "ext", "expiry": "1d"}, follow_redirects=False,
    )
    with db.connect() as conn:
        owned = db.list_api_keys_for_owner(conn, _uid("arch@test.co"))
    assert owned[0]["can_archive"] == 1
    assert owned[0]["expires_at"] is not None


def test_account_revokes_own_token(client):
    _login(client, "viewer@test.co")
    client.post(
        "/settings/account/extension-tokens",
        data={"name": "ext", "expiry": "permanent"}, follow_redirects=False,
    )
    uid = _uid("viewer@test.co")
    with db.connect() as conn:
        token_id = db.list_api_keys_for_owner(conn, uid)[0]["id"]
    r = client.post(
        f"/settings/account/extension-tokens/{token_id}/delete", follow_redirects=False
    )
    assert r.status_code == 303
    with db.connect() as conn:
        assert db.list_api_keys_for_owner(conn, uid) == []


def test_idor_cannot_delete_others_token(client):
    _issue_owned("vtok", "viewer@test.co")
    with db.connect() as conn:
        tok_id = db.list_api_keys_for_owner(conn, _uid("viewer@test.co"))[0]["id"]
    _login(client, "arch@test.co")  # 다른 사용자
    r = client.post(
        f"/settings/account/extension-tokens/{tok_id}/delete", follow_redirects=False
    )
    assert r.status_code == 404
    with db.connect() as conn:
        assert len(db.list_api_keys_for_owner(conn, _uid("viewer@test.co"))) == 1


def test_account_cannot_delete_system_key(client):
    with db.connect() as conn:
        auth.issue_api_key(conn, "sys", can_view=True, can_archive=False,
                           created_by=1, ttl_seconds=None)
        sys_id = db.list_system_api_keys(conn)[0]["id"]
    _login(client, "viewer@test.co")
    r = client.post(
        f"/settings/account/extension-tokens/{sys_id}/delete", follow_redirects=False
    )
    assert r.status_code == 404


def test_pending_cannot_issue(client):
    _login(client, "pend@test.co")
    r = client.post(
        "/settings/account/extension-tokens",
        data={"name": "x", "expiry": "permanent"}, follow_redirects=False,
    )
    # pending 은 미들웨어가 /pending 으로 돌린다 — 어떤 경로로든 토큰은 생기지 않는다
    assert r.status_code in (302, 303, 403)
    with db.connect() as conn:
        assert db.list_api_keys_for_owner(conn, _uid("pend@test.co")) == []


def test_account_page_shows_extension_section(client):
    _login(client, "viewer@test.co")
    res = client.get("/settings/account")
    assert res.status_code == 200
    assert "확장 토큰" in res.text


# ---- 관리 화면 격리 / 사용자 삭제 ----


def test_system_screen_hides_user_tokens(client):
    uid = _uid("viewer@test.co")
    with db.connect() as conn:
        auth.issue_api_key(conn, "syskey", can_view=True, can_archive=False,
                           created_by=1, ttl_seconds=None)
        auth.issue_api_key(conn, "usertok", can_view=True, can_archive=False,
                           created_by=uid, owner_user_id=uid, ttl_seconds=None)
    _login(client, "boss@test.co", "bosspass1234")
    res = client.get("/system/api-keys")
    assert "syskey" in res.text
    assert "usertok" not in res.text


def test_admin_cannot_delete_user_token_via_system(client):
    uid = _uid("viewer@test.co")
    with db.connect() as conn:
        auth.issue_api_key(conn, "usertok", can_view=True, can_archive=False,
                           created_by=uid, owner_user_id=uid, ttl_seconds=None)
        tok_id = db.list_api_keys_for_owner(conn, uid)[0]["id"]
    _login(client, "boss@test.co", "bosspass1234")
    r = client.post(f"/system/api-keys/{tok_id}/delete", follow_redirects=False)
    assert r.status_code == 404
    with db.connect() as conn:
        assert len(db.list_api_keys_for_owner(conn, uid)) == 1


def test_delete_user_cleans_owner_tokens_keeps_system_keys(client):
    uid = _uid("viewer@test.co")
    with db.connect() as conn:
        # 이 사용자가 발급자(created_by)인 시스템 키 + 본인 귀속 확장 토큰
        auth.issue_api_key(conn, "sys", can_view=True, can_archive=False,
                           created_by=uid, ttl_seconds=None)
        auth.issue_api_key(conn, "ext", can_view=True, can_archive=False,
                           created_by=uid, owner_user_id=uid, ttl_seconds=None)
        db.delete_user(conn, uid)
        # 본인 귀속 토큰은 폐기, 시스템 키는 보존하되 발급자 표기만 끊김
        assert db.list_api_keys_for_owner(conn, uid) == []
        system = db.list_system_api_keys(conn)
        assert [k["name"] for k in system] == ["sys"]
        assert system[0]["created_by"] is None
