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


# ---- 승인 대기(pending) 계정 차단 ----


# ---- 회원 가입 허용 여부 ----


# ---- 시스템 화면의 가입 설정 ----


