"""인증 코어(해싱/세션/TOTP) + 인증 라우트 테스트."""
import pyotp
import pytest
from fastapi.testclient import TestClient

from archiver import auth, config, db
from archiver.web import app as web_app


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """임시 아카이브 DB 환경."""
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")


@pytest.fixture
def client(tmp_db):
    """인증이 켜진 TestClient (쿠키 유지)."""
    return TestClient(web_app.app)


def signup(client, email="a@b.co", password="12345678"):
    """가입 헬퍼 — 세션 쿠키가 client 에 심어진다."""
    return client.post(
        "/signup", data={"email": email, "password": password},
        follow_redirects=False,
    )


# ---- 패스워드 ----


def test_password_roundtrip():
    h = auth.hash_password("correct horse battery")
    assert h != "correct horse battery"
    assert auth.verify_password(h, "correct horse battery")
    assert not auth.verify_password(h, "wrong password")


def test_validate_credentials():
    assert auth.validate_credentials("a@b.co", "12345678") is None
    assert auth.validate_credentials("not-an-email", "12345678") is not None
    assert auth.validate_credentials("a@b.co", "short") is not None


# ---- 세션 ----


def test_session_issue_and_resolve(tmp_db):
    with db.connect() as conn:
        uid = db.create_user(conn, "a@b.co", auth.hash_password("12345678"))
        token = auth.issue_session(conn, uid)
        sess = auth.resolve_session(conn, token)
        assert sess is not None
        assert sess["user_id"] == uid and sess["state"] == "active"
        # 토큰 원문은 DB에 없어야 한다
        rows = conn.execute("SELECT token_hash FROM sessions").fetchall()
        assert all(r["token_hash"] != token for r in rows)


def test_session_expiry(tmp_db):
    with db.connect() as conn:
        uid = db.create_user(conn, "a@b.co")
        token = auth.issue_session(conn, uid, ttl_seconds=-1)
        assert auth.resolve_session(conn, token) is None
        db.delete_expired_sessions(conn)
        assert conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"] == 0


def test_session_delete(tmp_db):
    with db.connect() as conn:
        uid = db.create_user(conn, "a@b.co")
        token = auth.issue_session(conn, uid)
        db.delete_session(conn, auth.hash_token(token))
        assert auth.resolve_session(conn, token) is None


def test_pending_session_state(tmp_db):
    with db.connect() as conn:
        uid = db.create_user(conn, "a@b.co")
        token = auth.issue_session(conn, uid, state="pending_totp", ttl_seconds=600)
        assert auth.resolve_session(conn, token)["state"] == "pending_totp"
        db.activate_session(conn, auth.hash_token(token), ttl_seconds=3600)
        assert auth.resolve_session(conn, token)["state"] == "active"


# ---- TOTP ----


def test_totp_verify_and_replay():
    secret = auth.new_totp_secret()
    code = pyotp.TOTP(secret).now()
    window = auth.verify_totp(secret, code, last_used=None)
    assert window is not None
    # 같은 시간창 재사용 거부
    assert auth.verify_totp(secret, code, last_used=window) is None
    # 틀린 코드 거부
    assert auth.verify_totp(secret, "000000", last_used=None) is None
    assert auth.verify_totp(secret, "abc123", last_used=None) is None


def test_totp_pending_confirm_flow(tmp_db):
    with db.connect() as conn:
        uid = db.create_user(conn, "a@b.co", auth.hash_password("12345678"))
        secret = auth.new_totp_secret()
        db.set_totp_pending(conn, uid, secret)
        user = db.get_user_by_id(conn, uid)
        assert user["totp_secret"] is None and user["totp_pending_secret"] == secret
        db.confirm_totp(conn, uid)
        user = db.get_user_by_id(conn, uid)
        assert user["totp_secret"] == secret and user["totp_pending_secret"] is None
        db.disable_totp(conn, uid)
        assert db.get_user_by_id(conn, uid)["totp_secret"] is None


def test_totp_qr_data_uri():
    uri = auth.totp_provisioning_uri(auth.new_totp_secret(), "a@b.co")
    assert uri.startswith("otpauth://totp/")
    assert auth.qr_data_uri(uri).startswith("data:image/png;base64,")


# ---- 사용자 / OIDC state ----


def test_user_email_case_insensitive(tmp_db):
    with db.connect() as conn:
        db.create_user(conn, "User@Example.com")
        assert db.get_user_by_email(conn, "user@example.com") is not None


def test_oidc_state_consume_once(tmp_db):
    with db.connect() as conn:
        db.create_oidc_state(conn, "st1", "n1", "/page/1")
        row = db.consume_oidc_state(conn, "st1")
        assert row["nonce"] == "n1" and row["redirect_to"] == "/page/1"
        assert db.consume_oidc_state(conn, "st1") is None  # 1회용


def test_oidc_state_expired(tmp_db):
    with db.connect() as conn:
        db.create_oidc_state(conn, "st1", "n1", "/")
        assert db.consume_oidc_state(conn, "st1", max_age_seconds=-1) is None


# ---- 라우트: 보호 / 로그인 / 가입 / 로그아웃 ----


def test_unauthenticated_redirects_to_login(client):
    res = client.get("/", follow_redirects=False)
    assert res.status_code == 302
    assert res.headers["location"] == "/login?next=%2F"


def test_redirect_preserves_query(client):
    res = client.get("/diff/1?from=1&to=2", follow_redirects=False)
    assert res.status_code == 302
    assert res.headers["location"] == "/login?next=%2Fdiff%2F1%3Ffrom%3D1%26to%3D2"


def test_healthz_public(client):
    assert client.get("/healthz").status_code == 200


def test_login_page_public(client):
    res = client.get("/login")
    assert res.status_code == 200
    assert "로그인" in res.text


def test_signup_then_authenticated(client):
    res = signup(client)
    assert res.status_code == 303 and res.headers["location"] == "/"
    assert client.get("/").status_code == 200  # 쿠키로 인증됨


def test_signup_duplicate_email(client):
    signup(client)
    client.cookies.clear()
    res = signup(client)
    assert res.status_code == 400
    assert "이미 가입된 이메일" in res.text


def test_signup_invalid_input(client):
    res = client.post("/signup", data={"email": "bad", "password": "12345678"})
    assert res.status_code == 400
    res = client.post("/signup", data={"email": "a@b.co", "password": "short"})
    assert res.status_code == 400


def test_login_success_and_failure(client):
    signup(client)
    client.cookies.clear()
    bad = client.post("/login", data={"email": "a@b.co", "password": "wrongpass"})
    assert bad.status_code == 401
    assert "올바르지 않습니다" in bad.text
    ok = client.post(
        "/login", data={"email": "a@b.co", "password": "12345678", "next": "/page/1"},
        follow_redirects=False,
    )
    assert ok.status_code == 303 and ok.headers["location"] == "/page/1"
    cookie = ok.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie


def test_login_rejects_open_redirect(client):
    signup(client)
    client.cookies.clear()
    res = client.post(
        "/login",
        data={"email": "a@b.co", "password": "12345678", "next": "//evil.com"},
        follow_redirects=False,
    )
    assert res.headers["location"] == "/"


def test_logout(client):
    signup(client)
    res = client.post("/logout", follow_redirects=False)
    assert res.status_code == 303
    assert client.get("/", follow_redirects=False).status_code == 302


def test_csrf_origin_mismatch_rejected(client):
    signup(client)
    res = client.post(
        "/logout", headers={"origin": "https://evil.com"}, follow_redirects=False
    )
    assert res.status_code == 403


def test_csrf_same_origin_allowed(client):
    signup(client)
    res = client.post(
        "/logout", headers={"origin": "http://testserver"}, follow_redirects=False
    )
    assert res.status_code == 303


# ---- 라우트: TOTP 등록 + 2단계 로그인 ----


def enroll_totp(client) -> str:
    """가입된 상태에서 TOTP 를 등록하고 시크릿을 반환."""
    res = client.get("/settings/totp")
    assert res.status_code == 200 and "data:image/png;base64," in res.text
    with db.connect() as conn:
        secret = db.get_user_by_email(conn, "a@b.co")["totp_pending_secret"]
    res = client.post(
        "/settings/totp", data={"code": pyotp.TOTP(secret).now()},
        follow_redirects=False,
    )
    assert res.status_code == 303
    return secret


def test_totp_enroll_and_two_step_login(client):
    import time

    signup(client)
    secret = enroll_totp(client)
    client.post("/logout")

    # 1단계: 패스워드 → pending 세션 + /login/totp 로 이동
    res = client.post(
        "/login", data={"email": "a@b.co", "password": "12345678", "next": "/"},
        follow_redirects=False,
    )
    assert res.status_code == 303 and res.headers["location"].startswith("/login/totp")
    # pending 상태로는 보호 라우트 접근 불가
    assert client.get("/", follow_redirects=False).status_code == 302

    # 2단계: 잘못된 코드 거부
    bad = client.post("/login/totp", data={"code": "000000", "next": "/"})
    assert bad.status_code == 401

    # 등록 시 사용한 시간창과 겹치지 않게 다음 창 코드 사용 (valid_window=1 허용 범위)
    code = pyotp.TOTP(secret).at(time.time() + 30)
    ok = client.post(
        "/login/totp", data={"code": code, "next": "/"}, follow_redirects=False
    )
    assert ok.status_code == 303 and ok.headers["location"] == "/"
    assert client.get("/").status_code == 200


def test_totp_page_requires_pending_session(client):
    res = client.get("/login/totp", follow_redirects=False)
    assert res.status_code == 302 and res.headers["location"] == "/login"


def test_totp_enroll_wrong_code(client):
    signup(client)
    client.get("/settings/totp")
    res = client.post("/settings/totp", data={"code": "000000"})
    assert res.status_code == 400
    with db.connect() as conn:
        assert db.get_user_by_email(conn, "a@b.co")["totp_secret"] is None


def test_totp_disable_requires_password(client):
    signup(client)
    enroll_totp(client)
    bad = client.post("/settings/totp/disable", data={"password": "wrongpass"})
    assert bad.status_code == 401
    ok = client.post(
        "/settings/totp/disable", data={"password": "12345678"},
        follow_redirects=False,
    )
    assert ok.status_code == 303
    with db.connect() as conn:
        assert db.get_user_by_email(conn, "a@b.co")["totp_secret"] is None
