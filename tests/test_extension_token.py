"""확장 토큰(사용자 귀속 API 키) — 권한 파생·매 요청 재평가·격리·계정 화면 발급/폐기."""
import json

import pytest
from fastapi.testclient import TestClient

from chunchugwan import auth, config, crawler, credentials, db, storage
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


def test_owner_token_permissions_follow_current_role(client):
    # 단발 아카이빙은 큐에 enqueue 만 하므로 캡처 모킹이 필요 없다 (worker 가 소비)
    # viewer 에 use_api_keys 오버라이드 부여 — 보기 전용 토큰을 쓸 수 있게 한다
    with db.connect() as conn:
        db.set_permission_overrides(
            conn, _uid("viewer@test.co"), {"use_api_keys": True}
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
    # archiver 소유 토큰은 삭제 전엔 동작, 소유자 삭제 후 401
    token = _issue_owned("ext", "arch@test.co")
    assert client.get("/api/v1/pages", headers=_headers(token)).status_code == 200
    with db.connect() as conn:
        db.delete_user(conn, _uid("arch@test.co"))
    assert client.get("/api/v1/pages", headers=_headers(token)).status_code == 401


def test_system_key_permissions_use_stored_columns(client):
    """owner=NULL 시스템 키는 기존대로 저장 컬럼만 본다 (재평가 비대상)."""
    with db.connect() as conn:
        token = auth.issue_api_key(conn, "sys", can_view=False, can_archive=False,
                                   created_by=1, ttl_seconds=None)
    assert client.get("/api/v1/pages", headers=_headers(token)).status_code == 403


# ---- 계정 화면 발급/폐기 ----


def test_account_issues_owner_token(client):
    _login(client, "arch@test.co")
    r = client.post(
        "/settings/api-keys",
        data={"name": "chrome-ext", "can_view": "on", "expiry": "permanent"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "new_token=wccg_" in r.headers["location"]
    uid = _uid("arch@test.co")
    with db.connect() as conn:
        owned = db.list_api_keys_for_owner(conn, uid)
    assert len(owned) == 1 and owned[0]["name"] == "chrome-ext"
    # 보기만 선택 → 보기 권한만
    assert owned[0]["can_view"] == 1 and owned[0]["can_archive"] == 0


def test_account_archiver_can_select_archive_perm(client):
    _login(client, "arch@test.co")
    client.post(
        "/settings/api-keys",
        data={"name": "ext", "can_archive": "on", "expiry": "1d"},
        follow_redirects=False,
    )
    with db.connect() as conn:
        owned = db.list_api_keys_for_owner(conn, _uid("arch@test.co"))
    assert owned[0]["can_view"] == 0 and owned[0]["can_archive"] == 1
    assert owned[0]["expires_at"] is not None


def test_account_archiver_can_select_both_perms(client):
    _login(client, "arch@test.co")
    client.post(
        "/settings/api-keys",
        data={"name": "ext", "can_view": "on", "can_archive": "on",
              "expiry": "permanent"},
        follow_redirects=False,
    )
    with db.connect() as conn:
        owned = db.list_api_keys_for_owner(conn, _uid("arch@test.co"))
    assert owned[0]["can_view"] == 1 and owned[0]["can_archive"] == 1


def test_account_requires_one_permission(client):
    _login(client, "arch@test.co")
    r = client.post(
        "/settings/api-keys",
        data={"name": "ext", "expiry": "permanent"}, follow_redirects=False,
    )
    assert r.status_code == 400
    with db.connect() as conn:
        assert db.list_api_keys_for_owner(conn, _uid("arch@test.co")) == []


def test_account_viewer_cannot_use_api_keys(client):
    """viewer 는 개인 API Key 사용 권한이 없어 화면·발급이 모두 403."""
    _login(client, "viewer@test.co")
    assert client.get("/settings/api-keys").status_code == 403
    r = client.post(
        "/settings/api-keys",
        data={"name": "ext", "can_view": "on", "expiry": "permanent"},
        follow_redirects=False,
    )
    assert r.status_code == 403
    with db.connect() as conn:
        assert db.list_api_keys_for_owner(conn, _uid("viewer@test.co")) == []


def test_account_clamps_to_role_scope(client):
    """use_api_keys 는 있으나 아카이브 권한이 없는 사용자가 아카이브를 골라도 보기로 클램프."""
    with db.connect() as conn:
        db.set_permission_overrides(
            conn, _uid("viewer@test.co"), {"use_api_keys": True}
        )
    _login(client, "viewer@test.co")
    r = client.post(
        "/settings/api-keys",
        data={"name": "ext", "can_view": "on", "can_archive": "on",
              "expiry": "permanent"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    with db.connect() as conn:
        owned = db.list_api_keys_for_owner(conn, _uid("viewer@test.co"))
    assert owned[0]["can_view"] == 1 and owned[0]["can_archive"] == 0


def test_account_archive_only_rejected_when_no_archive_perm(client):
    """아카이브 권한 없는 사용자(use_api_keys 만)가 아카이브만 고르면 클램프 후 거부."""
    with db.connect() as conn:
        db.set_permission_overrides(
            conn, _uid("viewer@test.co"), {"use_api_keys": True}
        )
    _login(client, "viewer@test.co")
    r = client.post(
        "/settings/api-keys",
        data={"name": "ext", "can_archive": "on", "expiry": "permanent"},
        follow_redirects=False,
    )
    assert r.status_code == 400
    with db.connect() as conn:
        assert db.list_api_keys_for_owner(conn, _uid("viewer@test.co")) == []


def test_account_revokes_own_token(client):
    _login(client, "arch@test.co")
    client.post(
        "/settings/api-keys",
        data={"name": "ext", "can_view": "on", "expiry": "permanent"},
        follow_redirects=False,
    )
    uid = _uid("arch@test.co")
    with db.connect() as conn:
        token_id = db.list_api_keys_for_owner(conn, uid)[0]["id"]
    r = client.post(
        f"/settings/api-keys/{token_id}/delete", follow_redirects=False
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
        f"/settings/api-keys/{tok_id}/delete", follow_redirects=False
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
        f"/settings/api-keys/{sys_id}/delete", follow_redirects=False
    )
    assert r.status_code == 404


def test_pending_cannot_issue(client):
    _login(client, "pend@test.co")
    r = client.post(
        "/settings/api-keys",
        data={"name": "x", "expiry": "permanent"}, follow_redirects=False,
    )
    # pending 은 미들웨어가 /pending 으로 돌린다 — 어떤 경로로든 토큰은 생기지 않는다
    assert r.status_code in (302, 303, 403)
    with db.connect() as conn:
        assert db.list_api_keys_for_owner(conn, _uid("pend@test.co")) == []


def test_api_keys_page_shows_section(client):
    _login(client, "arch@test.co")
    res = client.get("/settings/api-keys")
    assert res.status_code == 200
    assert "개인 API Key" in res.text
    # 발급 폼(이름 입력)이 보여야 한다 — use_api_keys 권한 보유 시
    assert 'action="/settings/api-keys"' in res.text


def test_account_page_links_to_api_keys(client):
    """계정 화면은 토큰 표 대신 개인 API Key 화면으로의 링크만 남긴다 (권한 보유 시)."""
    _login(client, "arch@test.co")
    res = client.get("/settings/account")
    assert res.status_code == 200
    assert 'href="/settings/api-keys"' in res.text
    # 발급 표/폼은 더 이상 계정 화면에 없다
    assert 'action="/settings/api-keys"' not in res.text


def test_account_page_hides_api_keys_for_viewer(client):
    """use_api_keys 권한이 없는 viewer 는 계정 화면에 개인 API Key 링크가 없다."""
    _login(client, "viewer@test.co")
    res = client.get("/settings/account")
    assert res.status_code == 200
    assert 'href="/settings/api-keys"' not in res.text


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


# ---- 1회성 세션 자격증명 (POST /api/v1/auth-profiles → site_credentials) ----

_COOKIES = [{"name": "sid", "value": "secret", "domain": "example.com", "path": "/"}]


def _ext_token(email):
    return _issue_owned("ext", email, can_view=True, can_archive=True)


def _auth_profile(client, token, url="https://example.com/secret", cookies=None):
    return client.post(
        "/api/v1/auth-profiles",
        json={"url": url, "storage_state": {"cookies": cookies or _COOKIES, "origins": []}},
        headers=_headers(token),
    )


def test_auth_profile_creates_oneshot_session_credential(client, monkeypatch):
    monkeypatch.setattr(config, "SECRET_KEY", "test-secret-passphrase")
    token = _ext_token("arch@test.co")
    r = _auth_profile(client, token)
    assert r.status_code == 202 and r.json()["authenticated"] is True
    with db.connect() as conn:
        cred = conn.execute("SELECT * FROM site_credentials").fetchone()
        # kind=session, 1회성(expires_at 설정), 소유자=발급 사용자
        assert cred["kind"] == "session" and cred["expires_at"] is not None
        assert cred["created_by"] == _uid("arch@test.co")
        # 아카이빙 작업이 그 credential_id 로 큐에 들어갔다
        job = conn.execute(
            "SELECT * FROM archive_jobs WHERE credential_id = ?", (cred["id"],)
        ).fetchone()
        assert job is not None


def test_auth_profile_requires_secret_key(client, monkeypatch):
    monkeypatch.setattr(config, "SECRET_KEY", "")
    assert _auth_profile(client, _ext_token("arch@test.co")).status_code == 503


def test_auth_profile_rejects_system_key(client, monkeypatch):
    monkeypatch.setattr(config, "SECRET_KEY", "test-secret")
    with db.connect() as conn:
        sys_token = auth.issue_api_key(
            conn, "sys", can_view=True, can_archive=True, created_by=1, ttl_seconds=None
        )
    assert _auth_profile(client, sys_token).status_code == 403  # 사용자 토큰만


def test_auth_profile_https_only(client, monkeypatch):
    monkeypatch.setattr(config, "SECRET_KEY", "test-secret")
    r = _auth_profile(client, _ext_token("arch@test.co"), url="http://example.com/x")
    assert r.status_code == 400


def test_auth_profile_scopes_cookies_to_target(client, monkeypatch):
    monkeypatch.setattr(config, "SECRET_KEY", "test-secret")
    token = _ext_token("arch@test.co")
    foreign = [{"name": "s", "value": "v", "domain": "evil.com", "path": "/"}]
    assert _auth_profile(client, token, cookies=foreign).status_code == 400


# ---- 로그인 세션 포함 크롤 (POST /api/v1/crawl + storage_state → 인증 크롤) ----


def _auth_crawl(client, token, url="https://example.com/docs/", cookies=None):
    return client.post(
        "/api/v1/crawl",
        json={"url": url, "storage_state": {"cookies": cookies or _COOKIES, "origins": []}},
        headers=_headers(token),
    )


def test_crawl_with_session_creates_credential(client, monkeypatch):
    monkeypatch.setattr(config, "SECRET_KEY", "test-secret-passphrase")
    token = _ext_token("arch@test.co")
    r = _auth_crawl(client, token)
    assert r.status_code == 202
    with db.connect() as conn:
        cred = conn.execute("SELECT * FROM site_credentials").fetchone()
        # kind=session, 1회성(expires_at 설정), 소유자=발급 사용자
        assert cred["kind"] == "session" and cred["expires_at"] is not None
        assert cred["created_by"] == _uid("arch@test.co")
        # 크롤이 그 credential_id 로 등록됐다 (크롤 전 페이지에 적용)
        crawl = conn.execute(
            "SELECT * FROM crawls WHERE credential_id = ?", (cred["id"],)
        ).fetchone()
        assert crawl is not None


def test_crawl_without_session_has_no_credential(client):
    token = _ext_token("arch@test.co")
    r = client.post(
        "/api/v1/crawl", json={"url": "https://example.com/docs/"}, headers=_headers(token)
    )
    assert r.status_code == 202
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM site_credentials").fetchone()["c"] == 0
        assert conn.execute("SELECT credential_id FROM crawls").fetchone()["credential_id"] is None


def test_crawl_session_requires_secret_key(client, monkeypatch):
    monkeypatch.setattr(config, "SECRET_KEY", "")
    assert _auth_crawl(client, _ext_token("arch@test.co")).status_code == 503


def test_crawl_session_rejects_system_key(client, monkeypatch):
    monkeypatch.setattr(config, "SECRET_KEY", "test-secret")
    with db.connect() as conn:
        sys_token = auth.issue_api_key(
            conn, "sys", can_view=True, can_archive=True, created_by=1, ttl_seconds=None
        )
    assert _auth_crawl(client, sys_token).status_code == 403  # 사용자 토큰만


def test_crawl_session_https_only(client, monkeypatch):
    monkeypatch.setattr(config, "SECRET_KEY", "test-secret")
    assert _auth_crawl(client, _ext_token("arch@test.co"), url="http://example.com/x/").status_code == 400


def test_crawl_session_scopes_cookies_to_target(client, monkeypatch):
    monkeypatch.setattr(config, "SECRET_KEY", "test-secret")
    foreign = [{"name": "s", "value": "v", "domain": "evil.com", "path": "/"}]
    assert _auth_crawl(client, _ext_token("arch@test.co"), cookies=foreign).status_code == 400


def test_crawl_session_merged_discards_credential(client, monkeypatch):
    monkeypatch.setattr(config, "SECRET_KEY", "test-secret")
    token = _ext_token("arch@test.co")
    crawler.start_crawl("https://example.com/docs/", source="api")  # 같은 URL 선행 크롤(진행 중)
    r = _auth_crawl(client, token)  # 같은 시작 URL → 병합, 새 1회성 자격증명은 버려진다
    assert r.status_code == 202 and r.json()["merged"] is True
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM site_credentials").fetchone()["c"] == 0


def test_gc_keeps_credential_of_running_crawl(client, monkeypatch):
    """만료된 1회성 자격증명이라도 진행 중 크롤이 참조하면 GC 가 건드리지 않는다."""
    monkeypatch.setattr(config, "SECRET_KEY", "test-secret")
    with db.connect() as conn:
        site_id = db.get_or_create_site(conn, "example.com")
        payload = credentials.build_payload(
            credentials.KIND_SESSION,
            {"storage_state": json.dumps({"cookies": _COOKIES, "origins": []})},
        )
        cred_id = credentials.add(
            conn, site_id, "ext:gc", credentials.KIND_SESSION, payload,
            created_by=_uid("arch@test.co"), ttl_seconds=-3600,  # 이미 만료
        )
    crawl, _ = crawler.start_crawl(
        "https://example.com/docs/", source="api", credential_id=cred_id
    )
    with db.connect() as conn:
        # 진행 중(running) 크롤이 참조 → 만료됐어도 보존
        assert db.delete_expired_ext_credentials(conn) == 0
        assert db.get_site_credential(conn, cred_id) is not None
        # 크롤이 끝나면 다음 GC 가 정리한다
        conn.execute("UPDATE crawls SET status = 'done' WHERE id = ?", (crawl["id"],))
        assert db.delete_expired_ext_credentials(conn) == 1
        assert db.get_site_credential(conn, cred_id) is None


# ---- JWT 로그인 정보 (확장이 localStorage/sessionStorage 에서 감지 → jwt 자격증명) ----

# 서버는 토큰 구조를 검증하지 않는다(구조 판별은 확장이 함) — 비어있지 않고 공백 없으면 됨
_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.c2ln"


def _auth_profile_jwt(client, token, url="https://example.com/secret", jwt=_JWT):
    return client.post(
        "/api/v1/auth-profiles", json={"url": url, "jwt": jwt}, headers=_headers(token)
    )


def test_auth_profile_jwt_creates_oneshot_jwt_credential(client, monkeypatch):
    monkeypatch.setattr(config, "SECRET_KEY", "test-secret-passphrase")
    token = _ext_token("arch@test.co")
    r = _auth_profile_jwt(client, token)
    assert r.status_code == 202 and r.json()["authenticated"] is True
    with db.connect() as conn:
        cred = conn.execute("SELECT * FROM site_credentials").fetchone()
        # kind=jwt, 1회성, 소유자=발급 사용자, 토큰이 복호화로 되살아난다
        assert cred["kind"] == "jwt" and cred["expires_at"] is not None
        assert cred["created_by"] == _uid("arch@test.co")
        assert credentials.reveal(conn, cred["id"]) == {"token": _JWT}
        job = conn.execute(
            "SELECT * FROM archive_jobs WHERE credential_id = ?", (cred["id"],)
        ).fetchone()
        assert job is not None


def test_auth_profile_jwt_preferred_over_cookies(client, monkeypatch):
    """둘 다 실리면 JWT 가 우선 (확장이 JWT 를 감지하면 토큰으로 보낸다)."""
    monkeypatch.setattr(config, "SECRET_KEY", "test-secret")
    token = _ext_token("arch@test.co")
    r = client.post(
        "/api/v1/auth-profiles",
        json={"url": "https://example.com/x", "jwt": _JWT,
              "storage_state": {"cookies": _COOKIES, "origins": []}},
        headers=_headers(token),
    )
    assert r.status_code == 202
    with db.connect() as conn:
        assert conn.execute("SELECT kind FROM site_credentials").fetchone()["kind"] == "jwt"


def test_auth_profile_without_any_auth_rejected(client, monkeypatch):
    monkeypatch.setattr(config, "SECRET_KEY", "test-secret")
    r = client.post(
        "/api/v1/auth-profiles", json={"url": "https://example.com/x"},
        headers=_headers(_ext_token("arch@test.co")),
    )
    assert r.status_code == 400  # 세션 쿠키도 JWT 도 없음


def test_auth_profile_jwt_https_only(client, monkeypatch):
    monkeypatch.setattr(config, "SECRET_KEY", "test-secret")
    r = _auth_profile_jwt(client, _ext_token("arch@test.co"), url="http://example.com/x")
    assert r.status_code == 400


def test_auth_profile_jwt_rejects_whitespace_token(client, monkeypatch):
    monkeypatch.setattr(config, "SECRET_KEY", "test-secret")
    r = _auth_profile_jwt(client, _ext_token("arch@test.co"), jwt="bad token")
    assert r.status_code == 400  # build_payload(jwt) 가 공백·줄바꿈을 거부


def test_crawl_jwt_creates_credential(client, monkeypatch):
    monkeypatch.setattr(config, "SECRET_KEY", "test-secret")
    token = _ext_token("arch@test.co")
    r = client.post(
        "/api/v1/crawl", json={"url": "https://example.com/docs/", "jwt": _JWT},
        headers=_headers(token),
    )
    assert r.status_code == 202
    with db.connect() as conn:
        cred = conn.execute("SELECT * FROM site_credentials").fetchone()
        assert cred["kind"] == "jwt"
        assert credentials.reveal(conn, cred["id"]) == {"token": _JWT}
        crawl = conn.execute(
            "SELECT * FROM crawls WHERE credential_id = ?", (cred["id"],)
        ).fetchone()
        assert crawl is not None
