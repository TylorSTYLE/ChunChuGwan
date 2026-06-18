"""SvelteKit SPA SMTP API(/api/web/system/smtp-*) 테스트.

Phase C2 컷오버를 위해 SSR system_routes 의 SMTP 설정·테스트 발송을 /api/web 으로
보강한 엔드포인트를 검증한다. SSR test_smtp_settings 의 단정(비밀번호 미노출·TLS/포트
검증·테스트 발송)에 대응한다.
"""
import pytest
from fastapi.testclient import TestClient

from chunchugwan import auth, config, crypto, db, mailer
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
    web_app._active_jobs.clear()
    yield
    web_app._active_jobs.clear()


def make_user(email="admin@test.co", password="adminpass123", role="admin"):
    with db.connect() as conn:
        pw = auth.hash_password(password) if password else None
        uid = db.create_user(conn, email, pw, role=role)
        token = auth.issue_session(conn, uid)
    return uid, token


def client_for(token=None):
    c = TestClient(web_app.app)
    if token:
        c.cookies.set(config.SESSION_COOKIE, token)
    return c


def admin_client():
    _, token = make_user()
    return client_for(token)


def _settings(**over):
    body = {"smtp_host": "smtp.test.co", "smtp_port": 587, "smtp_user": "u",
            "smtp_from": "noreply@test.co", "smtp_tls": "starttls"}
    body.update(over)
    return body


# ---- 권한 게이트 ----


def test_smtp_requires_manage_system(tmp_db):
    _, arch = make_user(email="arch@test.co", role="archiver")
    assert client_for(arch).post("/api/web/system/smtp-settings",
                                 json=_settings(), headers=POST_HEADERS).status_code == 403
    assert client_for().post("/api/web/system/smtp-test",
                             headers=POST_HEADERS).status_code == 401


# ---- 설정 저장 ----


def test_smtp_settings_save_roundtrip(tmp_db):
    c = admin_client()
    assert c.post("/api/web/system/smtp-settings",
                  json=_settings(smtp_host="mail.example.com"),
                  headers=POST_HEADERS).status_code == 200
    cfg = c.get("/api/web/system").json()["smtp_config"]
    assert cfg["host"] == "mail.example.com"
    assert cfg["port"] == 587
    assert cfg["has_password"] is False  # 비번 미입력


def test_smtp_settings_invalid_tls(tmp_db):
    r = admin_client().post("/api/web/system/smtp-settings",
                            json=_settings(smtp_tls="bogus"), headers=POST_HEADERS)
    assert r.status_code == 400


def test_smtp_settings_invalid_port(tmp_db):
    r = admin_client().post("/api/web/system/smtp-settings",
                            json=_settings(smtp_port=99999), headers=POST_HEADERS)
    assert r.status_code == 400


def test_smtp_settings_password_stored(tmp_db, monkeypatch):
    """비밀번호 입력 시 암호화 저장되고 has_password 로만 노출(평문 미노출)."""
    monkeypatch.setattr(crypto, "is_configured", lambda: True)
    monkeypatch.setattr(crypto, "encrypt", lambda s: "enc:" + s)
    c = admin_client()
    assert c.post("/api/web/system/smtp-settings",
                  json=_settings(smtp_password="secret123"),
                  headers=POST_HEADERS).status_code == 200
    cfg = c.get("/api/web/system").json()["smtp_config"]
    assert cfg["has_password"] is True
    assert "password" not in cfg  # 평문/암호문 모두 미노출


# ---- 테스트 발송 ----


def test_smtp_test_not_configured(tmp_db):
    assert admin_client().post("/api/web/system/smtp-test",
                               headers=POST_HEADERS).status_code == 400


def test_smtp_test_sends(tmp_db, monkeypatch):
    sent = {}
    monkeypatch.setattr(mailer, "send_test", lambda smtp, to: sent.update(to=to))
    c = admin_client()
    c.post("/api/web/system/smtp-settings", json=_settings(), headers=POST_HEADERS)
    r = c.post("/api/web/system/smtp-test", headers=POST_HEADERS)
    assert r.status_code == 200
    assert r.json()["email"] == "admin@test.co"
    assert sent["to"] == "admin@test.co"
