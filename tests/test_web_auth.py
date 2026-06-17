"""SPA 인증 JSON API(/api/web/auth) 테스트 — 로그인·2FA·가입·이메일 인증.

미인증 흐름이라 require_session 없이 호출된다(auth_gate 가 /api/ 통과). 세션 쿠키
발급과 pending 상태머신(pending_totp·pending_email_verify) 전이를 검증한다.
"""
import pyotp
import pytest
from fastapi.testclient import TestClient

from chunchugwan import auth, config, db, mailer
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


def client():
    return TestClient(web_app.app)


def mkuser(email="u@test.co", password="userpass123", role="archiver", totp=False):
    """사용자 생성. totp=True 면 TOTP 활성화하고 시크릿을 반환."""
    with db.connect() as conn:
        pw = auth.hash_password(password) if password else None
        uid = db.create_user(conn, email, pw, role=role)
        secret = None
        if totp:
            secret = auth.new_totp_secret()
            db.set_totp_pending(conn, uid, secret)
            db.confirm_totp(conn, uid)
    return uid, secret


def _login(c, email="u@test.co", password="userpass123"):
    return c.post(
        "/api/web/auth/login",
        json={"email": email, "password": password}, headers=POST_HEADERS,
    )


# ---- 로그인 ----


def test_login_success(tmp_db):
    mkuser(email="me@test.co")
    c = client()
    r = _login(c, "me@test.co")
    assert r.status_code == 200 and r.json()["status"] == "active"
    assert c.get("/api/web/me").json()["authenticated"] is True


def test_login_wrong_password(tmp_db):
    mkuser()
    assert _login(client(), password="wrong").status_code == 401


def test_login_unknown_email(tmp_db):
    assert _login(client(), email="nobody@test.co").status_code == 401


def test_login_blocked(tmp_db):
    mkuser(email="b@test.co", role="blocked")
    assert _login(client(), "b@test.co").status_code == 403


def test_login_withdrawn(tmp_db):
    mkuser(email="w@test.co", role="withdrawn")
    assert _login(client(), "w@test.co").status_code == 403


# ---- 2단계 인증 (TOTP) ----


def test_login_totp_flow(tmp_db):
    _, secret = mkuser(email="t@test.co", totp=True)
    c = client()
    r = _login(c, "t@test.co")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "totp" and body["has_totp"] is True
    # pending_totp 세션 — 아직 미인증
    assert c.get("/api/web/me").status_code == 401
    code = pyotp.TOTP(secret).now()
    r2 = c.post("/api/web/auth/login/totp", json={"code": code}, headers=POST_HEADERS)
    assert r2.status_code == 200 and r2.json()["status"] == "active"
    assert c.get("/api/web/me").json()["authenticated"] is True


def test_login_totp_wrong_code(tmp_db):
    mkuser(email="t2@test.co", totp=True)
    c = client()
    _login(c, "t2@test.co")
    r = c.post("/api/web/auth/login/totp", json={"code": "000000"}, headers=POST_HEADERS)
    assert r.status_code == 401


def test_totp_without_password_step(tmp_db):
    """pending_totp 세션 없이 /login/totp 호출하면 401."""
    mkuser(totp=True)
    r = client().post("/api/web/auth/login/totp", json={"code": "000000"}, headers=POST_HEADERS)
    assert r.status_code == 401


# ---- 회원 가입 ----


def test_signup_success(tmp_db):
    mkuser(email="admin@test.co", role="admin")  # first_run 게이트 회피(사용자 존재)
    with db.connect() as conn:
        db.set_setting(conn, db.SIGNUP_DEFAULT_ROLE_KEY, "viewer")
    c = client()
    r = c.post(
        "/api/web/auth/signup",
        json={"email": "new@test.co", "password": "newpass1234"}, headers=POST_HEADERS,
    )
    assert r.status_code == 200 and r.json()["status"] == "active"
    assert c.get("/api/web/me").json()["authenticated"] is True


def test_signup_duplicate(tmp_db):
    mkuser(email="dup@test.co")
    r = client().post(
        "/api/web/auth/signup",
        json={"email": "dup@test.co", "password": "newpass1234"}, headers=POST_HEADERS,
    )
    assert r.status_code == 400


def test_signup_disabled(tmp_db):
    mkuser(email="admin@test.co", role="admin")  # first_run 게이트 회피
    with db.connect() as conn:
        db.set_setting(conn, db.SIGNUP_ENABLED_KEY, "off")
    r = client().post(
        "/api/web/auth/signup",
        json={"email": "x@test.co", "password": "newpass1234"}, headers=POST_HEADERS,
    )
    assert r.status_code == 403


# ---- 이메일 본인 인증 ----


def test_login_email_verify_flow(tmp_db, monkeypatch):
    with db.connect() as conn:
        db.set_setting(conn, db.EMAIL_VERIFICATION_ENABLED_KEY, "on")
    captured: list[str] = []
    monkeypatch.setattr(mailer, "mail_enabled", lambda conn: True)
    monkeypatch.setattr(mailer, "resolve_config", lambda conn: type("C", (), {"enabled": True})())
    monkeypatch.setattr(
        mailer, "send_verification_code",
        lambda smtp, email, code, ttl: captured.append(code),
    )
    mkuser(email="v@test.co")  # email_verified=False(기본)
    c = client()
    r = _login(c, "v@test.co")
    assert r.status_code == 200 and r.json()["status"] == "email_verify"
    # pending_email_verify — 아직 미인증
    assert c.get("/api/web/me").status_code == 401
    st = c.get("/api/web/auth/verify-email/status").json()
    assert st["pending"] is True and st["email"] == "v@test.co"
    # 잘못된 코드
    assert c.post("/api/web/auth/verify-email", json={"code": "000000"}, headers=POST_HEADERS).status_code == 401
    # 메일로 나간 실제 코드로 인증 → active 승격
    assert captured
    r2 = c.post("/api/web/auth/verify-email", json={"code": captured[0]}, headers=POST_HEADERS)
    assert r2.status_code == 200 and r2.json()["status"] == "active"
    assert c.get("/api/web/me").json()["authenticated"] is True
