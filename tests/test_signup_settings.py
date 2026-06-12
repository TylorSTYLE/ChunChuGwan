"""가입 설정(허용 여부·초기 권한)과 승인 대기(pending) 계정 차단 테스트."""
import pytest
from fastapi.testclient import TestClient

from chunchugwan import auth, config, db
from chunchugwan.web import app as web_app


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """임시 아카이브 DB 환경 (인증은 기본값 on)."""
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "RESOURCES_DIR", tmp_path / "resources")
    monkeypatch.setattr(config, "DOCUMENTS_DIR", tmp_path / "documents")


@pytest.fixture
def client(tmp_db):
    """최초 관리자 + 보기 전용 사용자가 등록된 TestClient."""
    with db.connect() as conn:
        db.create_first_admin(conn, "boss@test.co", auth.hash_password("bosspass1234"))
        db.create_user(conn, "viewer@test.co", auth.hash_password("password1234"))
    return TestClient(web_app.app)


def _login(client, email: str, password: str = "password1234"):
    return client.post(
        "/login", data={"email": email, "password": password}, follow_redirects=False
    )


def _signup(client, email="new@test.co", password="password1234"):
    return client.post(
        "/signup", data={"email": email, "password": password},
        follow_redirects=False,
    )


def _user(email: str):
    with db.connect() as conn:
        return db.get_user_by_email(conn, email)


# ---- 설정 DB 계층 ----


def test_setting_roundtrip(tmp_db):
    with db.connect() as conn:
        assert db.get_setting(conn, "k") is None
        db.set_setting(conn, "k", "v1")
        assert db.get_setting(conn, "k") == "v1"
        db.set_setting(conn, "k", "v2")  # 교체
        assert db.get_setting(conn, "k") == "v2"


def test_signup_setting_defaults(tmp_db):
    with db.connect() as conn:
        assert db.signup_enabled(conn) is True
        assert db.signup_default_role(conn) == "pending"
        # 오염된 값은 안전한 기본(pending)으로 폴백
        db.set_setting(conn, db.SIGNUP_DEFAULT_ROLE_KEY, "admin")
        assert db.signup_default_role(conn) == "pending"


# ---- 가입 초기 권한 ----


@pytest.mark.parametrize("role", ["pending", "viewer", "archiver"])
def test_signup_uses_configured_role(client, role):
    with db.connect() as conn:
        db.set_setting(conn, db.SIGNUP_DEFAULT_ROLE_KEY, role)
    assert _signup(client).status_code == 303
    assert _user("new@test.co")["role"] == role


# ---- 승인 대기(pending) 계정 차단 ----


def test_pending_user_redirected_to_pending_page(client):
    _signup(client)  # 기본 초기 권한 = pending, 세션 쿠키가 심어진다
    for path in ("/", "/archives", "/page/1", "/system", "/settings/account",
                 "/archive/new", "/schedules", "/logs"):
        res = client.get(path, follow_redirects=False)
        assert res.status_code == 302, path
        assert res.headers["location"] == "/pending", path
    # 쓰기 요청도 안내 페이지로만 보내진다
    res = client.post(
        "/archive", data={"url": "https://example.com/x"}, follow_redirects=False
    )
    assert res.status_code == 302 and res.headers["location"] == "/pending"


def test_pending_page_shows_polite_message(client):
    _signup(client)
    res = client.get("/pending")
    assert res.status_code == 200
    assert "가입 승인 대기 중" in res.text
    assert "관리자의 승인을 기다리고 있습니다" in res.text
    assert "new@test.co" in res.text


def test_pending_user_can_logout(client):
    _signup(client)
    assert client.post("/logout", follow_redirects=False).status_code == 303
    # 로그아웃 후엔 보호 경로가 로그인으로 보낸다
    res = client.get("/pending", follow_redirects=False)
    assert res.status_code == 302 and res.headers["location"].startswith("/login")


def test_pending_login_lands_on_pending_page(client):
    _signup(client)
    client.cookies.clear()
    res = _login(client, "new@test.co")
    assert res.status_code == 303  # 로그인 자체는 성공 (차단과 다르다)
    res = client.get("/", follow_redirects=True)
    assert res.status_code == 200 and "가입 승인 대기 중" in res.text


def test_pending_page_redirects_normal_user_home(client):
    _login(client, "viewer@test.co")
    res = client.get("/pending", follow_redirects=False)
    assert res.status_code == 302 and res.headers["location"] == "/"


def test_admin_approves_pending_user(client):
    """관리자가 권한을 부여하면(승인) 즉시 서비스를 이용할 수 있다."""
    _signup(client)
    client.cookies.clear()
    _login(client, "boss@test.co", "bosspass1234")
    uid = _user("new@test.co")["id"]
    res = client.post(
        f"/system/users/{uid}/role", data={"role": "viewer"}, follow_redirects=False
    )
    assert res.status_code == 303 and "notice=" in res.headers["location"]
    client.cookies.clear()
    _login(client, "new@test.co")
    assert client.get("/", follow_redirects=False).status_code == 200


# ---- 회원 가입 허용 여부 ----


def test_signup_disabled_blocks_signup(client):
    with db.connect() as conn:
        db.set_setting(conn, db.SIGNUP_ENABLED_KEY, "off")
    res = client.get("/signup", follow_redirects=False)
    assert res.status_code == 302 and res.headers["location"] == "/login"
    assert _signup(client).status_code == 403
    assert _user("new@test.co") is None


def test_login_page_hides_signup_link_when_disabled(client):
    assert 'href="/signup"' in client.get("/login").text
    with db.connect() as conn:
        db.set_setting(conn, db.SIGNUP_ENABLED_KEY, "off")
    assert 'href="/signup"' not in client.get("/login").text


def test_signup_disabled_does_not_block_invites(client):
    """가입이 꺼져 있어도 관리자 초대 링크로는 가입할 수 있다."""
    import secrets

    with db.connect() as conn:
        db.set_setting(conn, db.SIGNUP_ENABLED_KEY, "off")
        token = secrets.token_urlsafe(32)
        db.create_invite(
            conn, "invited@test.co", auth.hash_token(token), "viewer",
            invited_by=None, ttl_seconds=3600,
        )
    res = client.post(
        f"/invite/{token}", data={"password": "password1234"},
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert _user("invited@test.co")["role"] == "viewer"


# ---- 시스템 화면의 가입 설정 ----


def test_system_settings_update(client):
    _login(client, "boss@test.co", "bosspass1234")
    page = client.get("/system").text
    assert "가입 설정" in page and 'name="signup_default_role"' in page
    res = client.post(
        "/system/settings",
        data={"signup_enabled": "on", "signup_default_role": "viewer"},
        follow_redirects=False,
    )
    assert res.status_code == 303 and "notice=" in res.headers["location"]
    with db.connect() as conn:
        assert db.signup_enabled(conn) is True
        assert db.signup_default_role(conn) == "viewer"
    # 체크박스 해제 = 폼 필드 부재 = off
    res = client.post(
        "/system/settings", data={"signup_default_role": "pending"},
        follow_redirects=False,
    )
    assert res.status_code == 303
    with db.connect() as conn:
        assert db.signup_enabled(conn) is False
        assert db.signup_default_role(conn) == "pending"


def test_system_settings_rejects_invalid_role(client):
    _login(client, "boss@test.co", "bosspass1234")
    assert client.post(
        "/system/settings", data={"signup_default_role": "admin"}
    ).status_code == 400
    with db.connect() as conn:
        assert db.signup_default_role(conn) == "pending"


def test_system_settings_requires_admin(client):
    _login(client, "viewer@test.co")
    assert client.post(
        "/system/settings", data={"signup_default_role": "viewer"}
    ).status_code == 403
