"""인증 코어(해싱/세션/TOTP) + 인증 라우트 테스트."""
import pyotp
import pytest
from fastapi.testclient import TestClient

from chunchugwan import auth, config, db
from chunchugwan.web import app as web_app


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """임시 아카이브 DB 환경."""
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "RESOURCES_DIR", tmp_path / "resources")
    monkeypatch.setattr(config, "DOCUMENTS_DIR", tmp_path / "documents")


@pytest.fixture
def client(tmp_db):
    """인증이 켜진 TestClient (쿠키 유지). 최초 구동을 끝낸 상태(관리자 존재).

    이 모듈의 signup 헬퍼는 가입 즉시 이용 가능한 계정을 전제하므로
    가입 초기 권한을 보기 전용으로 설정한다 (기본값은 pending — 승인 대기).
    """
    with db.connect() as conn:
        db.create_user(
            conn, "admin@test.co", auth.hash_password("adminpass123"), role="admin"
        )
        db.set_setting(conn, db.SIGNUP_DEFAULT_ROLE_KEY, "viewer")
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


def test_healthz_public(client):
    assert client.get("/healthz").status_code == 200


def test_favicon_svg_public(client):
    """파비콘은 인증 없이 서빙된다 (브라우저 자동 요청)."""
    res = client.get("/favicon.svg", follow_redirects=False)
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("image/svg+xml")


def test_favicon_svg_on_first_run(fresh_client):
    """최초 구동 상태에서도 파비콘은 /setup 으로 보내지 않고 서빙한다."""
    res = fresh_client.get("/favicon.svg", follow_redirects=False)
    assert res.status_code == 200


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


def test_migration_maps_is_admin_to_role(tmp_db):
    """is_admin 시절 스키마의 기존 DB 는 connect() 시 role/is_founder 로 보강된다.

    관리자 → admin(최초 관리자), 일반 사용자 → archiver (기존 동작 유지).
    """
    import sqlite3 as s

    config.ensure_dirs()
    raw = s.connect(config.DB_PATH)
    raw.execute(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, "
        "email TEXT NOT NULL UNIQUE COLLATE NOCASE, password_hash TEXT, "
        "totp_secret TEXT, totp_pending_secret TEXT, totp_last_used_at TEXT, "
        "is_admin INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)"
    )
    raw.execute(
        "INSERT INTO users (email, is_admin, created_at) "
        "VALUES ('boss@test.co', 1, '2026-01-01')"
    )
    raw.execute(
        "INSERT INTO users (email, created_at) VALUES ('old@test.co', '2026-01-02')"
    )
    raw.commit()
    raw.close()
    with db.connect() as conn:
        boss = db.get_user_by_email(conn, "boss@test.co")
        assert boss["role"] == "admin" and boss["is_founder"] == 1
        user = db.get_user_by_email(conn, "old@test.co")
        assert user["role"] == "archiver" and user["is_founder"] == 0


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


# ---- 계정 설정 (이름/패스워드 변경) ----


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


def test_resource_route_public_without_session(client):
    """/resource/ 는 인증 예외 — 샌드박스된 page.html(불투명 출처)의 하위
    자원 요청에는 SameSite 쿠키가 붙지 않아 세션 인증이 불가능하다."""
    res = client.get(f"/resource/{'a' * 64}.png", follow_redirects=False)
    assert res.status_code == 404  # 로그인 리다이렉트(302)가 아니라 '자원 없음'
