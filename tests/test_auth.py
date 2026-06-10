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
    """인증이 켜진 TestClient (쿠키 유지). 최초 구동을 끝낸 상태(관리자 존재)."""
    with db.connect() as conn:
        db.create_user(
            conn, "admin@test.co", auth.hash_password("adminpass123"), is_admin=True
        )
    return TestClient(web_app.app)


@pytest.fixture
def fresh_client(tmp_db):
    """사용자가 0명인 최초 구동 상태의 TestClient."""
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


def test_security_headers(client):
    res = client.get("/login")
    assert res.headers["x-content-type-options"] == "nosniff"
    assert res.headers["x-frame-options"] == "SAMEORIGIN"
    assert res.headers["referrer-policy"] == "same-origin"
    assert "default-src 'self'" in res.headers["content-security-policy"]


# ---- 라우트: 최초 구동 관리자 등록 ----


def test_first_run_redirects_everything_to_setup(fresh_client):
    for path in ("/", "/login", "/signup", "/page/1"):
        res = fresh_client.get(path, follow_redirects=False)
        assert res.status_code == 302, path
        assert res.headers["location"] == "/setup", path
    assert fresh_client.get("/healthz").status_code == 200
    assert fresh_client.get("/setup").status_code == 200


def test_setup_registers_admin_and_logs_in(fresh_client):
    res = fresh_client.post(
        "/setup", data={"email": "boss@test.co", "password": "longpassword1"},
        follow_redirects=False,
    )
    assert res.status_code == 303 and res.headers["location"] == "/"
    assert fresh_client.get("/").status_code == 200  # 자동 로그인
    with db.connect() as conn:
        user = db.get_user_by_email(conn, "boss@test.co")
        assert user["is_admin"] == 1


def test_setup_hidden_and_blocked_after_registration(fresh_client):
    fresh_client.post(
        "/setup", data={"email": "boss@test.co", "password": "longpassword1"}
    )
    # 등록 후에는 페이지가 표시되지 않는다 (로그인 상태 → / 로 리다이렉트)
    res = fresh_client.get("/setup", follow_redirects=False)
    assert res.status_code == 302 and res.headers["location"] == "/"
    # 같은 API 로 추가 등록 불가
    res = fresh_client.post(
        "/setup", data={"email": "evil@test.co", "password": "longpassword1"}
    )
    assert res.status_code == 403
    # 미인증 상태에서도 /setup 은 더 이상 닿지 않는다 (로그인으로 리다이렉트)
    fresh_client.cookies.clear()
    res = fresh_client.get("/setup", follow_redirects=False)
    assert res.status_code == 302 and res.headers["location"].startswith("/login")
    with db.connect() as conn:
        assert db.count_users(conn) == 1


def test_setup_invalid_input(fresh_client):
    res = fresh_client.post("/setup", data={"email": "bad", "password": "longpassword1"})
    assert res.status_code == 400
    res = fresh_client.post("/setup", data={"email": "a@b.co", "password": "short"})
    assert res.status_code == 400
    with db.connect() as conn:
        assert db.count_users(conn) == 0


def test_admin_bootstrap_from_env(fresh_client, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_EMAIL", "env-admin@test.co")
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "envpassword1")
    # 첫 요청에서 환경변수 관리자가 자동 등록되고, /setup 이 아닌 /login 으로 간다
    res = fresh_client.get("/", follow_redirects=False)
    assert res.headers["location"].startswith("/login")
    with db.connect() as conn:
        user = db.get_user_by_email(conn, "env-admin@test.co")
        assert user is not None and user["is_admin"] == 1
    ok = fresh_client.post(
        "/login", data={"email": "env-admin@test.co", "password": "envpassword1"},
        follow_redirects=False,
    )
    assert ok.status_code == 303


def test_admin_bootstrap_invalid_env_falls_back_to_setup(fresh_client, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_EMAIL", "env-admin@test.co")
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "short")  # 정책 미달 — 무시
    res = fresh_client.get("/", follow_redirects=False)
    assert res.headers["location"] == "/setup"
    with db.connect() as conn:
        assert db.count_users(conn) == 0


def test_migration_adds_is_admin_to_old_db(tmp_db):
    """is_admin 도입 전 스키마의 기존 DB 도 connect() 시 자동 보강된다."""
    import sqlite3 as s

    config.ensure_dirs()
    raw = s.connect(config.DB_PATH)
    raw.execute(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, "
        "email TEXT NOT NULL UNIQUE COLLATE NOCASE, password_hash TEXT, "
        "totp_secret TEXT, totp_pending_secret TEXT, totp_last_used_at TEXT, "
        "created_at TEXT NOT NULL)"
    )
    raw.execute(
        "INSERT INTO users (email, created_at) VALUES ('old@test.co', '2026-01-01')"
    )
    raw.commit()
    raw.close()
    with db.connect() as conn:
        user = db.get_user_by_email(conn, "old@test.co")
        assert user["is_admin"] == 0


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


# ---- 계정 설정 (이름/패스워드 변경) ----


def test_change_display_name(client):
    signup(client)
    res = client.post(
        "/settings/account/name", data={"display_name": "  홍길동  "},
        follow_redirects=False,
    )
    assert res.status_code == 303
    with db.connect() as conn:
        assert db.get_user_by_email(conn, "a@b.co")["display_name"] == "홍길동"
    # 헤더에 이메일 대신 표시 이름이 노출된다
    assert "홍길동" in client.get("/settings/account").text
    # 빈 입력이면 이름 제거 (이메일 표시로 복귀)
    client.post("/settings/account/name", data={"display_name": ""})
    with db.connect() as conn:
        assert db.get_user_by_email(conn, "a@b.co")["display_name"] is None


def test_change_display_name_rejects_invalid(client):
    signup(client)
    too_long = client.post(
        "/settings/account/name", data={"display_name": "가" * 51}
    )
    assert too_long.status_code == 400
    control = client.post(
        "/settings/account/name", data={"display_name": "줄\n바꿈"}
    )
    assert control.status_code == 400
    with db.connect() as conn:
        assert db.get_user_by_email(conn, "a@b.co")["display_name"] is None


def test_change_password_and_invalidate_other_sessions(client):
    signup(client)
    # 다른 기기의 세션을 흉내
    with db.connect() as conn:
        uid = db.get_user_by_email(conn, "a@b.co")["id"]
        other_token = auth.issue_session(conn, uid)
    res = client.post(
        "/settings/account/password",
        data={"current_password": "12345678", "new_password": "newpass99",
              "new_password2": "newpass99"},
        follow_redirects=False,
    )
    assert res.status_code == 303
    # 현재 세션은 유지되고 다른 세션은 무효화된다
    assert client.get("/settings/account").status_code == 200
    with db.connect() as conn:
        assert auth.resolve_session(conn, other_token) is None
    # 새 패스워드로만 로그인된다
    client.cookies.clear()
    bad = client.post("/login", data={"email": "a@b.co", "password": "12345678"})
    assert bad.status_code == 401
    ok = client.post(
        "/login", data={"email": "a@b.co", "password": "newpass99"},
        follow_redirects=False,
    )
    assert ok.status_code == 303


def test_change_password_rejects_wrong_current(client):
    signup(client)
    res = client.post(
        "/settings/account/password",
        data={"current_password": "wrongpass", "new_password": "newpass99",
              "new_password2": "newpass99"},
    )
    assert res.status_code == 401
    # 변경되지 않음 — 기존 패스워드로 로그인 가능
    client.cookies.clear()
    ok = client.post(
        "/login", data={"email": "a@b.co", "password": "12345678"},
        follow_redirects=False,
    )
    assert ok.status_code == 303


def test_change_password_rejects_short_or_mismatch(client):
    signup(client)
    short = client.post(
        "/settings/account/password",
        data={"current_password": "12345678", "new_password": "short",
              "new_password2": "short"},
    )
    assert short.status_code == 400
    mismatch = client.post(
        "/settings/account/password",
        data={"current_password": "12345678", "new_password": "newpass99",
              "new_password2": "different9"},
    )
    assert mismatch.status_code == 400


def test_change_password_sso_only_rejected(client):
    with db.connect() as conn:
        uid = db.create_user(conn, "sso@b.co")  # password_hash NULL = SSO 전용
        token = auth.issue_session(conn, uid)
    client.cookies.set(config.SESSION_COOKIE, token)
    res = client.post(
        "/settings/account/password",
        data={"current_password": "x", "new_password": "newpass99",
              "new_password2": "newpass99"},
    )
    assert res.status_code == 400
    assert "SSO 전용" in client.get("/settings/account").text


def test_account_page_links_2fa_settings(client):
    """2FA(TOTP)·패스키 설정은 계정 메뉴 안에서 접근한다."""
    signup(client)
    page = client.get("/settings/account").text
    assert "/settings/totp" in page
    assert "/settings/passkey" in page
    # 헤더에는 계정 링크만 남고 개별 2FA 링크는 없다
    index = client.get("/").text
    assert "/settings/account" in index
    assert "/settings/totp" not in index
    assert "/settings/passkey" not in index


def test_account_page_danger_zone_only_for_non_admin(client):
    """위험 영역(계정 삭제)은 관리자가 아닌 계정에만 보인다."""
    signup(client)
    assert "위험 영역" in client.get("/settings/account").text
    client.cookies.clear()
    client.post(
        "/login", data={"email": "admin@test.co", "password": "adminpass123"},
        follow_redirects=False,
    )
    admin_page = client.get("/settings/account").text
    assert "위험 영역" not in admin_page
    assert "/settings/account/delete" not in admin_page


def test_delete_account(client):
    signup(client)
    with db.connect() as conn:
        uid = db.get_user_by_email(conn, "a@b.co")["id"]
        other_token = auth.issue_session(conn, uid)  # 다른 기기의 세션
    res = client.post(
        "/settings/account/delete", data={"password": "12345678"},
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert res.headers["location"] == "/login"
    with db.connect() as conn:
        assert db.get_user_by_email(conn, "a@b.co") is None
        assert auth.resolve_session(conn, other_token) is None
        assert conn.execute(
            "SELECT COUNT(*) c FROM sessions WHERE user_id = ?", (uid,)
        ).fetchone()["c"] == 0
    # 삭제 후에는 인증이 풀린다
    assert client.get("/", follow_redirects=False).status_code in (302, 303)


def test_delete_account_rejects_wrong_password(client):
    signup(client)
    res = client.post("/settings/account/delete", data={"password": "wrongpass"})
    assert res.status_code == 401
    with db.connect() as conn:
        assert db.get_user_by_email(conn, "a@b.co") is not None


def test_delete_account_rejects_admin(client):
    client.post(
        "/login", data={"email": "admin@test.co", "password": "adminpass123"},
        follow_redirects=False,
    )
    res = client.post(
        "/settings/account/delete", data={"password": "adminpass123"}
    )
    assert res.status_code == 403
    with db.connect() as conn:
        assert db.get_user_by_email(conn, "admin@test.co") is not None


def test_delete_account_sso_only_confirms_email(client):
    """SSO 전용 계정은 패스워드가 없으므로 이메일 입력으로 확인한다."""
    with db.connect() as conn:
        uid = db.create_user(conn, "sso@b.co")  # password_hash NULL = SSO 전용
        db.create_identity(conn, uid, "authentik", "sub-1")
        token = auth.issue_session(conn, uid)
    client.cookies.set(config.SESSION_COOKIE, token)
    bad = client.post("/settings/account/delete", data={"confirm": "other@b.co"})
    assert bad.status_code == 400
    res = client.post(
        "/settings/account/delete", data={"confirm": "SSO@b.co"},
        follow_redirects=False,
    )
    assert res.status_code == 303
    with db.connect() as conn:
        assert db.get_user_by_email(conn, "sso@b.co") is None
        assert conn.execute(
            "SELECT COUNT(*) c FROM identities WHERE user_id = ?", (uid,)
        ).fetchone()["c"] == 0


def test_display_name_migration_adds_column(tmp_db):
    """display_name 이전 스키마의 DB 도 connect 시 컬럼이 추가된다."""
    import sqlite3

    config.ensure_dirs()
    raw = sqlite3.connect(config.DB_PATH)
    raw.execute(
        """CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            email TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT, totp_secret TEXT, totp_pending_secret TEXT,
            totp_last_used_at TEXT, is_admin INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )"""
    )
    raw.commit()
    raw.close()
    with db.connect() as conn:
        uid = db.create_user(conn, "old@b.co")
        db.set_display_name(conn, uid, "마이그레이션")
        assert db.get_user_by_id(conn, uid)["display_name"] == "마이그레이션"
