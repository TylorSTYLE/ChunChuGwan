"""이메일 본인 인증 — 가입·로그인 게이트, 재발송, 개인 설정 인증, 시스템 설정."""
import pytest
from fastapi.testclient import TestClient

from chunchugwan import auth, config, db, mailer
from chunchugwan.web import app as web_app
from chunchugwan.web import auth_routes


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """임시 아카이브 DB 환경 (인증 on)."""
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "RESOURCES_DIR", tmp_path / "resources")
    monkeypatch.setattr(config, "DOCUMENTS_DIR", tmp_path / "documents")


@pytest.fixture
def sent(monkeypatch):
    """발송된 인증 코드를 가로채는 가짜 메일러 — (이메일, 코드, ttl분) 목록."""
    box: list[tuple[str, str, int]] = []

    def fake_send(cfg, to_email, code, ttl_minutes):
        box.append((to_email, code, ttl_minutes))

    monkeypatch.setattr(mailer, "send_verification_code", fake_send)
    return box


@pytest.fixture
def client(tmp_db):
    """최초 관리자가 등록된 TestClient (가입 계정은 바로 쓸 수 있게 viewer)."""
    with db.connect() as conn:
        db.create_first_admin(conn, "boss@test.co", auth.hash_password("bosspass1234"))
        db.set_setting(conn, db.SIGNUP_DEFAULT_ROLE_KEY, "viewer")
    return TestClient(web_app.app)


def _enable_verification(*, smtp: bool = True, ttl_minutes: int = 30):
    """이메일 인증 기능을 켜고(선택) SMTP 호스트도 설정해 mail_enabled 가 되게 한다."""
    with db.connect() as conn:
        db.set_setting(conn, db.EMAIL_VERIFICATION_ENABLED_KEY, "on")
        db.set_setting(
            conn, db.EMAIL_VERIFICATION_TTL_MINUTES_KEY, str(ttl_minutes)
        )
        if smtp:
            db.set_setting(conn, db.SMTP_HOST_KEY, "smtp.test")


def _signup(client, email="new@test.co", password="password1234"):
    return client.post(
        "/signup", data={"email": email, "password": password},
        follow_redirects=False,
    )


def _login(client, email, password="password1234"):
    return client.post(
        "/login", data={"email": email, "password": password},
        follow_redirects=False,
    )


def _user(email):
    with db.connect() as conn:
        return db.get_user_by_email(conn, email)


# ---- 가입 게이트 ----


# ---- 로그인 게이트 (기존 미인증 사용자) ----


# ---- 개인 설정에서 인증 (기존 사용자, active 세션) ----


# ---- SSO 계정은 제외 ----


def test_sso_account_not_required(tmp_db):
    _enable_verification()
    with db.connect() as conn:
        uid = db.create_user(conn, "sso@test.co", role="viewer")  # password_hash NULL
        sso = db.get_user_by_id(conn, uid)
        assert auth_routes._email_verification_required(conn, sso) is False


# ---- 시스템 설정 ----


def test_ttl_clamped_on_read(tmp_db):
    with db.connect() as conn:
        db.set_setting(conn, db.EMAIL_VERIFICATION_TTL_MINUTES_KEY, "999999")
        assert db.email_verification_ttl_minutes(conn) == (
            config.EMAIL_VERIFICATION_TTL_MINUTES_MAX
        )
        db.set_setting(conn, db.EMAIL_VERIFICATION_TTL_MINUTES_KEY, "garbage")
        assert db.email_verification_ttl_minutes(conn) == (
            config.EMAIL_VERIFICATION_TTL_MINUTES_DEFAULT
        )
