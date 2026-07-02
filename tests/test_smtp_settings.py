"""시스템 메뉴 SMTP 설정 — 저장·암호화·환경변수 폴백·테스트 메일 발송."""

import pytest
from fastapi.testclient import TestClient

from chunchugwan import auth, config, db, mailer
from chunchugwan.web import app as web_app


def _patch_root(monkeypatch, root):
    monkeypatch.setattr(config, "ARCHIVE_ROOT", root)
    monkeypatch.setattr(config, "SITES_DIR", root / "sites")
    monkeypatch.setattr(config, "DB_PATH", root / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", root / "cache")
    monkeypatch.setattr(config, "RESOURCES_DIR", root / "resources")
    monkeypatch.setattr(config, "DOCUMENTS_DIR", root / "documents")


def _clear_env_smtp(monkeypatch):
    """환경변수 SMTP 기본값을 비워 DB 우선/폴백을 명확히 검증한다."""
    monkeypatch.setattr(config, "SMTP_HOST", "")
    monkeypatch.setattr(config, "SMTP_PORT", 587)
    monkeypatch.setattr(config, "SMTP_USER", "")
    monkeypatch.setattr(config, "SMTP_PASSWORD", "")
    monkeypatch.setattr(config, "SMTP_FROM", "")
    monkeypatch.setattr(config, "SMTP_TLS", "starttls")


@pytest.fixture
def client(tmp_path, monkeypatch):
    """인증 off + 암호화 키 설정 — 설정 저장·해석 검증용."""
    monkeypatch.setattr(config, "AUTH_ENABLED", False)
    monkeypatch.setattr(config, "SECRET_KEY", "test-secret-key")
    _clear_env_smtp(monkeypatch)
    _patch_root(monkeypatch, tmp_path / "a")
    with db.connect():
        pass  # 스키마 생성
    return TestClient(web_app.app)


@pytest.fixture
def auth_client(tmp_path, monkeypatch):
    """관리자 로그인 상태의 TestClient — 테스트 메일(본인 이메일) 검증용."""
    monkeypatch.setattr(config, "AUTH_ENABLED", True)
    monkeypatch.setattr(config, "SECRET_KEY", "test-secret-key")
    _clear_env_smtp(monkeypatch)
    _patch_root(monkeypatch, tmp_path / "b")
    with db.connect() as conn:
        db.create_first_admin(conn, "boss@test.co", auth.hash_password("bosspass1234"))
    c = TestClient(web_app.app)
    c.post(
        "/login", data={"email": "boss@test.co", "password": "bosspass1234"},
        follow_redirects=False,
    )
    return c


# ---- resolve_config (DB 우선, 환경변수 폴백) ----


def test_resolve_config_falls_back_to_env(client, monkeypatch):
    monkeypatch.setattr(config, "SMTP_HOST", "env.smtp")
    monkeypatch.setattr(config, "SMTP_USER", "envuser")
    monkeypatch.setattr(config, "SMTP_PASSWORD", "envpass")
    with db.connect() as conn:
        cfg = mailer.resolve_config(conn)
    assert cfg.host == "env.smtp" and cfg.user == "envuser"
    assert cfg.password == "envpass" and cfg.enabled
    assert cfg.sender == "envuser"  # from 비면 user 로 폴백


def test_db_settings_override_env(client, monkeypatch):
    monkeypatch.setattr(config, "SMTP_HOST", "env.smtp")
    with db.connect() as conn:
        db.set_setting(conn, db.SMTP_HOST_KEY, "db.smtp")
        db.set_setting(conn, db.SMTP_PORT_KEY, "2525")
        cfg = mailer.resolve_config(conn)
    assert cfg.host == "db.smtp" and cfg.port == 2525


def test_mail_enabled_reflects_db(client):
    with db.connect() as conn:
        assert mailer.mail_enabled(conn) is False
        db.set_setting(conn, db.SMTP_HOST_KEY, "smtp.x")
        assert mailer.mail_enabled(conn) is True


def test_resolve_config_corrupt_values_fall_back(client):
    """포트가 정수가 아니거나 TLS 가 미지원이면 안전한 기본값으로 폴백한다."""
    with db.connect() as conn:
        db.set_setting(conn, db.SMTP_PORT_KEY, "쓰레기")
        db.set_setting(conn, db.SMTP_TLS_KEY, "bogus")
        cfg = mailer.resolve_config(conn)
    assert cfg.port == 587 and cfg.tls == "starttls"


# ---- /system/smtp-settings ----


def test_smtp_clear_password(client):
    client.post(
        "/system/smtp-settings",
        data={"smtp_host": "smtp.x", "smtp_port": 587,
              "smtp_password": "bye", "smtp_tls": "starttls"},
    )
    client.post(
        "/system/smtp-settings",
        data={"smtp_host": "smtp.x", "smtp_port": 587, "smtp_password": "",
              "smtp_clear_password": "on", "smtp_tls": "starttls"},
    )
    with db.connect() as conn:
        assert db.get_setting(conn, db.SMTP_PASSWORD_KEY) is None
        cfg = mailer.resolve_config(conn)
    assert cfg.password == ""  # 환경변수도 비어 있어 빈 값


# ---- /system/smtp-test ----


