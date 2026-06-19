"""인증 무차별 대입 방어(rate limit)·사용자 열거 완화·최초 설정 토큰 테스트.

보안 검토 F1(throttle)·F2(가입 일반화)·F3(setup 토큰)을 검증한다. throttle 카운터는
인증 실패(4xx)로 롤백돼도 별도 트랜잭션으로 커밋되어 누적된다.
"""
import pytest
from fastapi.testclient import TestClient

from chunchugwan import auth, config, db, mailer
from chunchugwan.web import app as web_app

POST = {"X-Requested-With": "fetch", "Origin": "http://testserver"}


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "RESOURCES_DIR", tmp_path / "resources")
    monkeypatch.setattr(config, "DOCUMENTS_DIR", tmp_path / "documents")
    monkeypatch.setattr(config, "AUTH_ENABLED", True)
    monkeypatch.setattr(config, "SETUP_TOKEN", "")
    monkeypatch.delenv("WCCG_ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("WCCG_ADMIN_PASSWORD", raising=False)


def client():
    return TestClient(web_app.app)


def mkuser(email="u@test.co", password="userpass123", role="archiver"):
    with db.connect() as conn:
        pw = auth.hash_password(password) if password else None
        return db.create_user(conn, email, pw, role=role)


def _login(c, email="u@test.co", password="userpass123"):
    return c.post(
        "/api/web/auth/login",
        json={"email": email, "password": password}, headers=POST,
    )


def _set(key, value):
    with db.connect() as conn:
        db.set_setting(conn, key, str(value))


def _enable_mail(monkeypatch):
    captured: list[str] = []
    monkeypatch.setattr(mailer, "mail_enabled", lambda conn: True)
    monkeypatch.setattr(
        mailer, "resolve_config", lambda conn: type("C", (), {"enabled": True})())
    monkeypatch.setattr(
        mailer, "send_verification_code",
        lambda smtp, email, code, ttl: captured.append(code),
    )
    return captured


# ---- db.throttle_hit 단위 ----


def test_throttle_hit_unit(tmp_db):
    with db.connect() as conn:
        # 한도 3, 창 60초 — 4번째 시도가 차단된다
        for i in range(3):
            allowed, retry = db.throttle_hit(conn, "t", "k", 3, 60)
            assert allowed and retry == 0, i
        allowed, retry = db.throttle_hit(conn, "t", "k", 3, 60)
        assert not allowed and retry > 0
        # clear 하면 다시 허용
        db.throttle_clear(conn, "t", "k")
        allowed, _ = db.throttle_hit(conn, "t", "k", 3, 60)
        assert allowed


def test_throttle_settings_clamp(tmp_db):
    with db.connect() as conn:
        db.set_setting(conn, db.AUTH_LOGIN_LIMIT_KEY, "999999")   # 범위 초과
        db.set_setting(conn, db.AUTH_LOGIN_IP_LIMIT_KEY, "oops")  # 오염
        s = db.auth_throttle_settings(conn)
    assert s["login_limit"] == config.AUTH_THROTTLE_LIMIT_MAX
    assert s["login_ip_limit"] == config.AUTH_LOGIN_IP_LIMIT_DEFAULT


# ---- F1: 로그인 throttle ----


def test_login_throttle_blocks(tmp_db):
    mkuser(email="lt@test.co")
    _set(db.AUTH_LOGIN_LIMIT_KEY, 3)
    c = client()
    for _ in range(3):
        assert _login(c, "lt@test.co", "wrong").status_code == 401
    r = _login(c, "lt@test.co", "wrong")
    assert r.status_code == 429 and "Retry-After" in r.headers


def test_login_throttle_cleared_on_success(tmp_db):
    mkuser(email="lc@test.co")
    _set(db.AUTH_LOGIN_LIMIT_KEY, 3)
    c = client()
    assert _login(c, "lc@test.co", "wrong").status_code == 401
    assert _login(c, "lc@test.co", "wrong").status_code == 401
    assert _login(c, "lc@test.co").status_code == 200  # 성공 → 카운터 초기화
    # 다시 실패해도 누적이 리셋되어 곧바로 429 가 아니다
    assert _login(c, "lc@test.co", "wrong").status_code == 401


def test_login_throttle_disabled(tmp_db):
    mkuser(email="ld@test.co")
    _set(db.AUTH_LOGIN_LIMIT_KEY, 2)
    _set(db.AUTH_THROTTLE_ENABLED_KEY, "off")
    c = client()
    for _ in range(5):
        assert _login(c, "ld@test.co", "wrong").status_code == 401  # 절대 429 아님


def test_unknown_email_login_is_401(tmp_db):
    # F2: 미존재 이메일도 (존재 이메일과 같은) 401 — 상태코드로 열거 불가
    assert _login(client(), "nobody@test.co").status_code == 401


# ---- F1: 2단계(TOTP) throttle ----


def test_totp_throttle_blocks(tmp_db):
    with db.connect() as conn:
        uid = db.create_user(conn, "tt@test.co", auth.hash_password("userpass123"),
                             role="archiver")
        secret = auth.new_totp_secret()
        db.set_totp_pending(conn, uid, secret)
        db.confirm_totp(conn, uid)
    _set(db.AUTH_TOTP_LIMIT_KEY, 2)
    c = client()
    assert _login(c, "tt@test.co").json()["status"] == "totp"
    for _ in range(2):
        assert c.post("/api/web/auth/login/totp", json={"code": "000000"},
                      headers=POST).status_code == 401
    r = c.post("/api/web/auth/login/totp", json={"code": "000000"}, headers=POST)
    assert r.status_code == 429


# ---- F1: 이메일 코드 throttle (초과 시 코드 폐기) ----


def test_email_verify_throttle_discards_code(tmp_db, monkeypatch):
    _enable_mail(monkeypatch)
    _set(db.EMAIL_VERIFICATION_ENABLED_KEY, "on")
    _set(db.AUTH_EMAIL_VERIFY_LIMIT_KEY, 2)
    mkuser(email="ev@test.co")
    c = client()
    assert _login(c, "ev@test.co").json()["status"] == "email_verify"
    for _ in range(2):
        assert c.post("/api/web/auth/verify-email", json={"code": "000000"},
                      headers=POST).status_code == 401
    r = c.post("/api/web/auth/verify-email", json={"code": "000000"}, headers=POST)
    assert r.status_code == 429
    # 코드가 폐기됐다 — DB 에 인증 행이 없다
    with db.connect() as conn:
        uid = db.get_user_by_email(conn, "ev@test.co")["id"]
        assert db.get_email_verification(conn, uid) is None


def test_email_resend_throttle(tmp_db, monkeypatch):
    _enable_mail(monkeypatch)
    _set(db.EMAIL_VERIFICATION_ENABLED_KEY, "on")
    _set(db.AUTH_EMAIL_RESEND_LIMIT_KEY, 2)
    mkuser(email="rs@test.co")
    c = client()
    assert _login(c, "rs@test.co").json()["status"] == "email_verify"
    for _ in range(2):
        assert c.post("/api/web/auth/verify-email/resend", headers=POST).status_code == 200
    r = c.post("/api/web/auth/verify-email/resend", headers=POST)
    assert r.status_code == 429


# ---- F2: 가입 일반화 (메일 켜진 경우 중복도 동일 응답·계정 미생성) ----


def test_signup_duplicate_generalized_with_mail(tmp_db, monkeypatch):
    _enable_mail(monkeypatch)
    _set(db.EMAIL_VERIFICATION_ENABLED_KEY, "on")
    mkuser(email="dupe@test.co")  # 이미 존재 (first_run 게이트도 회피)
    r = client().post(
        "/api/web/auth/signup",
        json={"email": "dupe@test.co", "password": "newpass1234"}, headers=POST,
    )
    # 신규 가입과 동일하게 email_verify — 존재 여부 비노출
    assert r.status_code == 200 and r.json()["status"] == "email_verify"
    # 중복이라 새 계정·세션을 만들지 않는다 (사용자 수 불변, 쿠키 미발급)
    with db.connect() as conn:
        assert db.count_users(conn) == 1
    assert "set-cookie" not in {k.lower() for k in r.headers}


def test_signup_new_with_mail_creates_and_verifies(tmp_db, monkeypatch):
    _enable_mail(monkeypatch)
    _set(db.EMAIL_VERIFICATION_ENABLED_KEY, "on")
    mkuser(email="admin@test.co", role="admin")  # first_run 회피
    r = client().post(
        "/api/web/auth/signup",
        json={"email": "fresh@test.co", "password": "newpass1234"}, headers=POST,
    )
    assert r.status_code == 200 and r.json()["status"] == "email_verify"
    with db.connect() as conn:
        assert db.get_user_by_email(conn, "fresh@test.co") is not None


# ---- F3: 최초 설정 토큰 ----


def test_setup_token_required_flag(tmp_db, monkeypatch):
    monkeypatch.setattr(config, "SETUP_TOKEN", "s3cret")
    body = client().get("/api/web/auth/setup").json()
    assert body["needed"] is True and body["token_required"] is True


def test_setup_rejected_without_token(tmp_db, monkeypatch):
    monkeypatch.setattr(config, "SETUP_TOKEN", "s3cret")
    r = client().post(
        "/api/web/auth/setup",
        json={"email": "admin@test.co", "password": "adminpass123"}, headers=POST,
    )
    assert r.status_code == 403
    with db.connect() as conn:
        assert db.count_users(conn) == 0  # 관리자 선점 차단


def test_setup_accepted_with_token(tmp_db, monkeypatch):
    monkeypatch.setattr(config, "SETUP_TOKEN", "s3cret")
    r = client().post(
        "/api/web/auth/setup",
        json={"email": "admin@test.co", "password": "adminpass123"},
        headers={**POST, "X-Setup-Token": "s3cret"},
    )
    assert r.status_code == 200 and r.json()["status"] == "active"


def test_setup_no_token_when_unset(tmp_db):
    # SETUP_TOKEN 미설정이면 종전대로 토큰 없이 셋업 가능
    r = client().post(
        "/api/web/auth/setup",
        json={"email": "admin@test.co", "password": "adminpass123"}, headers=POST,
    )
    assert r.status_code == 200


# ---- 시스템 설정 연동 ----


def test_auth_throttle_settings_save_and_read(tmp_db):
    mkuser(email="adm@test.co", role="admin")
    c = client()
    _login(c, "adm@test.co")
    payload = {
        "auth_throttle_enabled": True, "login_limit": 7, "login_ip_limit": 20,
        "login_window_minutes": 10, "totp_limit": 5,
        "email_verify_limit": 4, "email_resend_limit": 3,
    }
    assert c.post("/api/web/system/auth-throttle-settings", json=payload,
                  headers=POST).status_code == 200
    s = c.get("/api/web/system").json()
    assert s["auth_throttle"]["login_limit"] == 7
    assert s["auth_throttle"]["login_window_minutes"] == 10
    assert s["auth_throttle_enabled"] is True


def test_auth_throttle_settings_out_of_range(tmp_db):
    mkuser(email="adm2@test.co", role="admin")
    c = client()
    _login(c, "adm2@test.co")
    payload = {
        "auth_throttle_enabled": True,
        "login_limit": config.AUTH_THROTTLE_LIMIT_MAX + 1, "login_ip_limit": 20,
        "login_window_minutes": 10, "totp_limit": 5,
        "email_verify_limit": 4, "email_resend_limit": 3,
    }
    assert c.post("/api/web/system/auth-throttle-settings", json=payload,
                  headers=POST).status_code == 400
