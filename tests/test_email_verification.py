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


def test_signup_gates_on_verification(client, sent):
    _enable_verification()
    res = _signup(client)
    assert res.status_code == 303
    assert res.headers["location"].startswith("/verify-email")
    assert _user("new@test.co")["email_verified"] == 0
    assert sent and sent[0][0] == "new@test.co"
    # 인증 화면은 코드 입력 폼을 보여준다
    page = client.get("/verify-email")
    assert page.status_code == 200
    assert 'name="code"' in page.text


def test_verify_completes_and_activates_session(client, sent):
    _enable_verification()
    _signup(client)
    code = sent[-1][1]
    # 틀린 코드는 거부
    bad = client.post(
        "/verify-email", data={"code": "000000", "next": "/"},
        follow_redirects=False,
    )
    assert bad.status_code == 401
    assert _user("new@test.co")["email_verified"] == 0
    # 맞는 코드로 인증 완료 + 세션 활성화
    ok = client.post(
        "/verify-email", data={"code": code, "next": "/"}, follow_redirects=False
    )
    assert ok.status_code == 303 and ok.headers["location"] == "/"
    assert _user("new@test.co")["email_verified"] == 1
    # 이제 active 세션이라 보호된 화면 접근 가능
    assert client.get("/", follow_redirects=False).status_code == 200


def test_signup_skips_verification_without_smtp(client, sent):
    _enable_verification(smtp=False)  # 기능은 켜졌지만 SMTP 미설정
    res = _signup(client)
    assert res.status_code == 303 and res.headers["location"] == "/"
    assert not sent
    assert client.get("/", follow_redirects=False).status_code == 200


# ---- 로그인 게이트 (기존 미인증 사용자) ----


def test_login_gates_unverified_user(client, sent):
    with db.connect() as conn:
        db.create_user(conn, "old@test.co", auth.hash_password("password1234"),
                       role="viewer")
    _enable_verification()
    res = _login(client, "old@test.co")
    assert res.status_code == 303
    assert res.headers["location"].startswith("/verify-email")
    assert sent and sent[-1][0] == "old@test.co"


def test_resend_issues_new_code(client, sent):
    _enable_verification()
    _signup(client)
    first = sent[-1][1]
    res = client.post(
        "/verify-email/resend", data={"next": "/"}, follow_redirects=False
    )
    assert res.status_code == 303 and "sent=1" in res.headers["location"]
    assert len(sent) == 2 and sent[-1][1] != ""
    # 새 코드로 인증되어야 한다 (이전 코드는 교체됨)
    new_code = sent[-1][1]
    ok = client.post(
        "/verify-email", data={"code": new_code, "next": "/"},
        follow_redirects=False,
    )
    assert ok.status_code == 303
    assert _user("new@test.co")["email_verified"] == 1


def test_verified_user_login_not_gated(client, sent):
    with db.connect() as conn:
        uid = db.create_user(conn, "ok@test.co", auth.hash_password("password1234"),
                             role="viewer")
        db.set_email_verified(conn, uid)
    _enable_verification()
    res = _login(client, "ok@test.co")
    assert res.status_code == 303 and res.headers["location"] == "/"


# ---- 개인 설정에서 인증 (기존 사용자, active 세션) ----


def test_account_self_verify(client, sent):
    """기능이 꺼진 채 로그인해 active 세션을 얻은 뒤, 기능을 켜고 개인 설정에서 인증."""
    with db.connect() as conn:
        db.create_user(conn, "self@test.co", auth.hash_password("password1234"),
                       role="viewer")
    _login(client, "self@test.co")  # 기능 off → 바로 active
    assert client.get("/settings/account").status_code == 200
    _enable_verification()
    # 계정 화면에 인증 섹션이 보인다
    assert "미인증" in client.get("/settings/account").text
    client.post("/verify-email/resend", data={"next": "/settings/account"},
                follow_redirects=False)
    code = sent[-1][1]
    res = client.post(
        "/verify-email", data={"code": code, "next": "/settings/account"},
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert res.headers["location"] == "/settings/account?ok=email_verified"
    assert _user("self@test.co")["email_verified"] == 1


# ---- SSO 계정은 제외 ----


def test_sso_account_not_required(tmp_db):
    _enable_verification()
    with db.connect() as conn:
        uid = db.create_user(conn, "sso@test.co", role="viewer")  # password_hash NULL
        sso = db.get_user_by_id(conn, uid)
        assert auth_routes._email_verification_required(conn, sso) is False


# ---- 시스템 설정 ----


def test_system_settings_save_and_validation(client):
    _login(client, "boss@test.co", "bosspass1234")
    res = client.post(
        "/system/email-verification-settings",
        data={"email_verification_enabled": "on",
              "email_verification_ttl_minutes": "45"},
        follow_redirects=False,
    )
    assert res.status_code == 303 and "notice=" in res.headers["location"]
    with db.connect() as conn:
        assert db.email_verification_enabled(conn) is True
        assert db.email_verification_ttl_minutes(conn) == 45
    # 범위 밖은 거부
    bad = client.post(
        "/system/email-verification-settings",
        data={"email_verification_enabled": "on",
              "email_verification_ttl_minutes": "1"},
        follow_redirects=False,
    )
    assert bad.status_code == 303 and "error=" in bad.headers["location"]


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
