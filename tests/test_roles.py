"""사용자 권한(역할) — DB 계층, 라우트 가드, 사용자 관리 화면 테스트."""
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


@pytest.fixture
def client(tmp_db):
    """최초 관리자(founder) + 역할별 사용자가 등록된 TestClient."""
    with db.connect() as conn:
        db.create_first_admin(conn, "boss@test.co", auth.hash_password("bosspass1234"))
        for email, role in (
            ("archiver@test.co", "archiver"),
            ("viewer@test.co", "viewer"),
            ("blocked@test.co", "blocked"),
        ):
            db.create_user(conn, email, auth.hash_password("password1234"), role=role)
    return TestClient(web_app.app)


def _login(client, email: str, password: str = "password1234"):
    return client.post(
        "/login", data={"email": email, "password": password}, follow_redirects=False
    )


def _user(email: str):
    with db.connect() as conn:
        return db.get_user_by_email(conn, email)


# ---- DB 계층 ----


def test_create_user_default_role_is_viewer(tmp_db):
    with db.connect() as conn:
        uid = db.create_user(conn, "a@b.co")
        assert db.get_user_by_id(conn, uid)["role"] == "viewer"


def test_create_user_rejects_unknown_role(tmp_db):
    with db.connect() as conn:
        with pytest.raises(ValueError):
            db.create_user(conn, "a@b.co", role="superuser")


def test_set_role_and_validation(tmp_db):
    with db.connect() as conn:
        uid = db.create_user(conn, "a@b.co")
        assert db.set_role(conn, uid, "archiver") is True
        assert db.get_user_by_id(conn, uid)["role"] == "archiver"
        with pytest.raises(ValueError):
            db.set_role(conn, uid, "root")


def test_set_role_refuses_founder(tmp_db):
    with db.connect() as conn:
        uid = db.create_first_admin(conn, "boss@test.co", "x")
        assert db.set_role(conn, uid, "viewer") is False
        assert db.get_user_by_id(conn, uid)["role"] == "admin"


# ---- 아카이빙 권한 가드 ----


def test_viewer_cannot_trigger_archive(client):
    _login(client, "viewer@test.co")
    assert client.post("/archive", data={"url": "https://example.com/x"}).status_code == 403
    assert client.get("/archive/new").status_code == 403
    assert client.post("/page/1/rearchive").status_code == 403
    # 화면에서도 새 아카이빙 메뉴/버튼이 보이지 않는다
    page = client.get("/archives").text
    assert 'href="/archive/new"' not in page


def test_viewer_cannot_manage_schedule(client):
    """주기적 재아카이빙 설정/해제도 아카이빙 트리거 — viewer 는 403."""
    _login(client, "viewer@test.co")
    assert client.post("/page/1/schedule", data={"interval": 3600}).status_code == 403
    assert (
        client.post(
            "/page/1/schedule/next-run", data={"next_run": "2099-01-01T00:00"}
        ).status_code
        == 403
    )
    assert client.post("/page/1/schedule/delete").status_code == 403


def test_viewer_sees_schedules_readonly(client):
    """viewer 도 스케줄 화면은 볼 수 있지만 변경/해제 폼은 보이지 않는다."""
    _login(client, "viewer@test.co")
    res = client.get("/schedules")
    assert res.status_code == 200
    assert "주기 변경" not in res.text
    assert "/schedule/next-run" not in res.text
    assert "/schedule/delete" not in res.text


def test_archiver_can_manage_schedule(client):
    _login(client, "archiver@test.co")
    # 없는 페이지 — 권한 통과 후 404
    assert client.post("/page/999/schedule", data={"interval": 3600}).status_code == 404
    assert (
        client.post(
            "/page/999/schedule/next-run", data={"next_run": "2099-01-01T00:00"}
        ).status_code
        == 404
    )
    assert client.post("/page/999/schedule/delete").status_code == 404


def test_archiver_can_trigger_archive(client):
    _login(client, "archiver@test.co")
    # 스킴이 거부되는 URL — 권한은 통과하고 검증 단계에서 에러 리다이렉트
    res = client.post(
        "/archive", data={"url": "ftp://example.com/x"}, follow_redirects=False
    )
    assert res.status_code == 303 and "error=" in res.headers["location"]
    # 없는 페이지 재아카이빙 — 권한 통과 후 404
    assert client.post("/page/999/rearchive").status_code == 404
    # 새 아카이빙 메뉴·등록 화면 접근 가능
    assert 'href="/archive/new"' in client.get("/archives").text
    assert client.get("/archive/new").status_code == 200


def test_archiver_has_no_admin_menus(client):
    _login(client, "archiver@test.co")
    assert client.get("/system").status_code == 403
    assert client.get("/system/users").status_code == 403
    page = client.get("/").text
    assert 'href="/system"' not in page and 'href="/system/users"' not in page


# ---- 차단된 계정 ----


def test_blocked_user_cannot_login(client):
    res = _login(client, "blocked@test.co")
    assert res.status_code == 403
    assert "차단된 계정" in res.text


def test_blocked_user_existing_session_rejected(client):
    """차단 전에 발급된 세션이 남아 있어도 미들웨어가 막는다."""
    with db.connect() as conn:
        uid = db.get_user_by_email(conn, "blocked@test.co")["id"]
        token = auth.issue_session(conn, uid)
    client.cookies.set(config.SESSION_COOKIE, token)
    res = client.get("/")
    assert res.status_code == 403 and "차단된 계정" in res.text
    # 로그아웃은 가능
    assert client.post("/logout", follow_redirects=False).status_code == 303


# ---- 사용자 관리 화면 ----


def test_users_page_admin_only(client):
    _login(client, "viewer@test.co")
    assert client.get("/system/users").status_code == 403
    client.cookies.clear()
    _login(client, "boss@test.co", "bosspass1234")
    res = client.get("/system/users")
    assert res.status_code == 200
    for email in ("boss@test.co", "archiver@test.co", "viewer@test.co", "blocked@test.co"):
        assert email in res.text
    assert "최초 관리자" in res.text
    assert 'href="/system/users"' in client.get("/").text  # 헤더 메뉴 노출


def test_admin_changes_role(client):
    _login(client, "boss@test.co", "bosspass1234")
    uid = _user("viewer@test.co")["id"]
    res = client.post(
        f"/system/users/{uid}/role", data={"role": "archiver"}, follow_redirects=False
    )
    assert res.status_code == 303 and "notice=" in res.headers["location"]
    assert _user("viewer@test.co")["role"] == "archiver"


def test_role_change_rejects_invalid_and_missing(client):
    _login(client, "boss@test.co", "bosspass1234")
    uid = _user("viewer@test.co")["id"]
    assert client.post(
        f"/system/users/{uid}/role", data={"role": "root"}
    ).status_code == 400
    assert client.post(
        "/system/users/9999/role", data={"role": "viewer"}
    ).status_code == 404


def test_founder_role_cannot_be_changed(client):
    _login(client, "boss@test.co", "bosspass1234")
    uid = _user("boss@test.co")["id"]
    res = client.post(
        f"/system/users/{uid}/role", data={"role": "viewer"}, follow_redirects=False
    )
    assert res.status_code == 303 and "error=" in res.headers["location"]
    assert _user("boss@test.co")["role"] == "admin"
    # 화면에서도 변경 폼 대신 '변경 불가' 표기
    page = client.get("/system/users").text
    assert "변경 불가" in page


def test_blocking_user_invalidates_sessions(client):
    with db.connect() as conn:
        uid = db.get_user_by_email(conn, "archiver@test.co")["id"]
        other_token = auth.issue_session(conn, uid)  # 차단 대상의 활성 세션
    _login(client, "boss@test.co", "bosspass1234")
    res = client.post(
        f"/system/users/{uid}/role", data={"role": "blocked"}, follow_redirects=False
    )
    assert res.status_code == 303
    with db.connect() as conn:
        assert auth.resolve_session(conn, other_token) is None
    assert _user("archiver@test.co")["role"] == "blocked"


def test_admin_changes_display_name(client):
    _login(client, "boss@test.co", "bosspass1234")
    uid = _user("viewer@test.co")["id"]
    res = client.post(
        f"/system/users/{uid}/name", data={"display_name": "홍길동"},
        follow_redirects=False,
    )
    assert res.status_code == 303 and "notice=" in res.headers["location"]
    assert _user("viewer@test.co")["display_name"] == "홍길동"
    # 빈 입력 = 이름 제거
    res = client.post(
        f"/system/users/{uid}/name", data={"display_name": "  "},
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert _user("viewer@test.co")["display_name"] is None


def test_name_change_rejects_invalid_and_missing(client):
    _login(client, "boss@test.co", "bosspass1234")
    uid = _user("viewer@test.co")["id"]
    res = client.post(
        f"/system/users/{uid}/name", data={"display_name": "긴" * 51},
        follow_redirects=False,
    )
    assert res.status_code == 303 and "error=" in res.headers["location"]
    assert _user("viewer@test.co")["display_name"] is None
    assert client.post(
        "/system/users/9999/name", data={"display_name": "x"}
    ).status_code == 404


def test_name_change_requires_admin(client):
    _login(client, "viewer@test.co")
    uid = _user("viewer@test.co")["id"]
    assert client.post(
        f"/system/users/{uid}/name", data={"display_name": "x"}
    ).status_code == 403


def test_admin_force_logout(client):
    with db.connect() as conn:
        uid = db.get_user_by_email(conn, "archiver@test.co")["id"]
        other_token = auth.issue_session(conn, uid)
    _login(client, "boss@test.co", "bosspass1234")
    res = client.post(f"/system/users/{uid}/logout", follow_redirects=False)
    assert res.status_code == 303 and "notice=" in res.headers["location"]
    with db.connect() as conn:
        assert auth.resolve_session(conn, other_token) is None
    # 권한은 그대로 — 차단과 달리 다시 로그인할 수 있다
    assert _user("archiver@test.co")["role"] == "archiver"
    assert client.post("/system/users/9999/logout").status_code == 404


def test_force_logout_requires_admin(client):
    _login(client, "viewer@test.co")
    uid = _user("archiver@test.co")["id"]
    assert client.post(f"/system/users/{uid}/logout").status_code == 403


def test_signup_defaults_to_viewer(client):
    res = client.post(
        "/signup", data={"email": "new@test.co", "password": "password1234"},
        follow_redirects=False,
    )
    assert res.status_code == 303
    user = _user("new@test.co")
    assert user["role"] == "viewer" and user["is_founder"] == 0


# ---- 인증 off (loopback) ----


def test_auth_off_allows_everything(tmp_db, monkeypatch):
    monkeypatch.setattr(config, "AUTH_ENABLED", False)
    client = TestClient(web_app.app)
    assert client.get("/system/users").status_code == 200
    res = client.post(
        "/archive", data={"url": "ftp://example.com/x"}, follow_redirects=False
    )
    assert res.status_code == 303  # 권한 가드 통과 (URL 검증 에러 리다이렉트)
