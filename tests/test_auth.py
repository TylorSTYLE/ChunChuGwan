"""인증 코어(해싱/세션/TOTP) + 인증 라우트 테스트."""
import pyotp
import pytest

from archiver import auth, config, db


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """임시 아카이브 DB 환경."""
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")


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
