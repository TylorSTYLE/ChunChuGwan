"""시스템 메뉴 SMTP 설정 — 저장·암호화·환경변수 폴백·테스트 메일 발송."""
import smtplib

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


def test_smtp_settings_save_persists_and_encrypts(client):
    res = client.post(
        "/system/smtp-settings",
        data={"smtp_host": "smtp.example.com", "smtp_port": 465,
              "smtp_user": "bot@example.com", "smtp_password": "s3cret",
              "smtp_from": "noreply@example.com", "smtp_tls": "ssl"},
        follow_redirects=False,
    )
    assert res.status_code == 303 and "notice=" in res.headers["location"]
    with db.connect() as conn:
        stored = db.get_setting(conn, db.SMTP_PASSWORD_KEY)
        assert stored and stored != "s3cret"  # 평문이 아니라 암호문
        cfg = mailer.resolve_config(conn)
    assert cfg.host == "smtp.example.com" and cfg.port == 465
    assert cfg.user == "bot@example.com" and cfg.tls == "ssl"
    assert cfg.sender == "noreply@example.com"
    assert cfg.password == "s3cret"  # 복호화되어 발송에 쓰인다


def test_smtp_settings_page_round_trips_without_leaking_password(client):
    client.post(
        "/system/smtp-settings",
        data={"smtp_host": "smtp.example.com", "smtp_port": 587,
              "smtp_password": "s3cret", "smtp_tls": "starttls"},
    )
    page = client.get("/system").text
    assert "메일(SMTP) 설정" in page
    assert "smtp.example.com" in page  # 비밀번호 외 값은 폼에 채워진다
    assert "s3cret" not in page  # 비밀번호는 화면에 노출되지 않는다
    assert "저장된 비밀번호 삭제" in page  # 저장된 비밀번호가 있으면 삭제 옵션 노출


def test_smtp_blank_password_keeps_existing(client):
    client.post(
        "/system/smtp-settings",
        data={"smtp_host": "smtp.x", "smtp_port": 587,
              "smtp_password": "keepme", "smtp_tls": "starttls"},
    )
    client.post(  # 비밀번호 비우고 호스트만 변경 → 비밀번호 유지
        "/system/smtp-settings",
        data={"smtp_host": "smtp.y", "smtp_port": 587,
              "smtp_password": "", "smtp_tls": "starttls"},
    )
    with db.connect() as conn:
        cfg = mailer.resolve_config(conn)
    assert cfg.host == "smtp.y" and cfg.password == "keepme"


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


def test_smtp_settings_rejects_bad_tls(client):
    res = client.post(
        "/system/smtp-settings",
        data={"smtp_host": "smtp.x", "smtp_port": 587, "smtp_tls": "bogus"},
        follow_redirects=False,
    )
    assert res.status_code == 303 and "error=" in res.headers["location"]
    with db.connect() as conn:
        assert db.get_setting(conn, db.SMTP_HOST_KEY) is None  # 미저장


def test_smtp_settings_rejects_bad_port(client):
    res = client.post(
        "/system/smtp-settings",
        data={"smtp_host": "smtp.x", "smtp_port": 70000, "smtp_tls": "starttls"},
        follow_redirects=False,
    )
    assert res.status_code == 303 and "error=" in res.headers["location"]
    with db.connect() as conn:
        assert db.get_setting(conn, db.SMTP_HOST_KEY) is None


def test_smtp_password_requires_secret_key(client, monkeypatch):
    monkeypatch.setattr(config, "SECRET_KEY", "")  # 암호화 키 없음
    res = client.post(
        "/system/smtp-settings",
        data={"smtp_host": "smtp.x", "smtp_port": 587,
              "smtp_password": "nope", "smtp_tls": "starttls"},
        follow_redirects=False,
    )
    assert res.status_code == 303 and "error=" in res.headers["location"]
    with db.connect() as conn:  # 비밀번호를 못 쓰면 전체 저장을 거부
        assert db.get_setting(conn, db.SMTP_PASSWORD_KEY) is None
        assert db.get_setting(conn, db.SMTP_HOST_KEY) is None


def test_smtp_settings_without_password_ok_without_secret_key(client, monkeypatch):
    """비밀번호 없이는 암호화 키가 없어도 호스트 등 비밀번호 외 값을 저장한다."""
    monkeypatch.setattr(config, "SECRET_KEY", "")
    res = client.post(
        "/system/smtp-settings",
        data={"smtp_host": "smtp.x", "smtp_port": 25, "smtp_tls": "off"},
        follow_redirects=False,
    )
    assert res.status_code == 303 and "notice=" in res.headers["location"]
    with db.connect() as conn:
        cfg = mailer.resolve_config(conn)
    assert cfg.host == "smtp.x" and cfg.port == 25 and cfg.tls == "off"


# ---- /system/smtp-test ----


def test_smtp_test_sends_to_admin(auth_client, monkeypatch):
    sent = {}

    def fake_test(cfg, to_email):
        sent.update(to=to_email, host=cfg.host)

    monkeypatch.setattr(mailer, "send_test", fake_test)
    auth_client.post(
        "/system/smtp-settings",
        data={"smtp_host": "smtp.x", "smtp_port": 587, "smtp_tls": "starttls"},
    )
    res = auth_client.post("/system/smtp-test", follow_redirects=False)
    assert res.status_code == 303 and "notice=" in res.headers["location"]
    assert sent["to"] == "boss@test.co" and sent["host"] == "smtp.x"


def test_smtp_test_requires_host(auth_client, monkeypatch):
    called = {"n": 0}

    def fake_test(cfg, to_email):
        called["n"] += 1

    monkeypatch.setattr(mailer, "send_test", fake_test)
    res = auth_client.post("/system/smtp-test", follow_redirects=False)
    assert res.status_code == 303 and "error=" in res.headers["location"]
    assert called["n"] == 0  # 호스트 없으면 발송하지 않는다


def test_smtp_test_reports_failure(auth_client, monkeypatch):
    def boom(cfg, to_email):
        raise smtplib.SMTPException("connection refused")

    monkeypatch.setattr(mailer, "send_test", boom)
    auth_client.post(
        "/system/smtp-settings",
        data={"smtp_host": "smtp.x", "smtp_port": 587, "smtp_tls": "starttls"},
    )
    res = auth_client.post("/system/smtp-test", follow_redirects=False)
    assert res.status_code == 303 and "error=" in res.headers["location"]
