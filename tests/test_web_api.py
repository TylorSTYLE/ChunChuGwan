"""SvelteKit SPA API(/api/web) 테스트 — 인증 가드·개인 설정·API 키 권한·2FA.

빅뱅 컷오버 전까지 SSR 과 공존하는 세션 인증 JSON API(web_api_routes.py)를
검증한다. 보안 민감한 부분(권한 클램프·IDOR·재확인)을 우선 커버한다.
"""
import pyotp
import pytest
from fastapi.testclient import TestClient

from chunchugwan import auth, config, db
from chunchugwan.web import app as web_app

# 변경 요청 헤더 — api.ts 가 싣는 것과 동일(CSRF 보강 + auth_gate Origin 검사 통과).
POST_HEADERS = {"X-Requested-With": "fetch", "Origin": "http://testserver"}


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """인증이 켜진 임시 아카이브 DB."""
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "RESOURCES_DIR", tmp_path / "resources")
    monkeypatch.setattr(config, "DOCUMENTS_DIR", tmp_path / "documents")
    monkeypatch.setattr(config, "AUTH_ENABLED", True)


def make_user(email="u@test.co", password="userpass123", role="archiver"):
    """사용자 + active 세션을 만들고 (user_id, token) 반환. password=None 이면 SSO 전용."""
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


# ---- 인증 가드 ----


def test_me_unauthenticated(tmp_db):
    """미인증 세션은 require_session 라우트에서 401."""
    make_user()  # 사용자는 있으나 쿠키 미설정
    r = client_for().get("/api/web/settings/account")
    assert r.status_code == 401


def test_account_get(tmp_db):
    _, token = make_user(email="me@test.co")
    r = client_for(token).get("/api/web/settings/account")
    assert r.status_code == 200
    body = r.json()
    assert body["email"] == "me@test.co"
    assert body["has_password"] is True
    assert body["passkeys"] == []


# ---- 계정 변경 (#8) ----


def test_account_name_change(tmp_db):
    uid, token = make_user()
    r = client_for(token).post(
        "/api/web/settings/account/name",
        json={"display_name": "  새 이름  "}, headers=POST_HEADERS,
    )
    assert r.status_code == 200
    with db.connect() as conn:
        assert db.get_user_by_id(conn, uid)["display_name"] == "새 이름"


def test_account_language_invalid(tmp_db):
    _, token = make_user()
    r = client_for(token).post(
        "/api/web/settings/account/language",
        json={"locale": "xx"}, headers=POST_HEADERS,
    )
    assert r.status_code == 400


def test_account_password_wrong_current(tmp_db):
    _, token = make_user(password="rightpass123")
    r = client_for(token).post(
        "/api/web/settings/account/password",
        json={"current_password": "wrongpass", "new_password": "newpass1234",
              "new_password2": "newpass1234"},
        headers=POST_HEADERS,
    )
    assert r.status_code == 401


def test_account_withdraw_admin_forbidden(tmp_db):
    _, token = make_user(email="admin@test.co", role="admin")
    r = client_for(token).post(
        "/api/web/settings/account/withdraw",
        json={"password": "userpass123"}, headers=POST_HEADERS,
    )
    assert r.status_code == 403  # 관리자는 탈퇴 불가


# ---- 개인 API 키 (#8) — 권한 클램프·IDOR ----


def test_personal_api_key_viewer_forbidden(tmp_db):
    """viewer 는 use_api_keys 권한이 없어 개인 키 발급 불가(403)."""
    _, token = make_user(role="viewer")
    r = client_for(token).post(
        "/api/web/settings/api-keys",
        json={"name": "k", "can_view": True}, headers=POST_HEADERS,
    )
    assert r.status_code == 403


def test_personal_api_key_permission_clamp(tmp_db):
    """archiver 가 권한 밖(delete 류는 토큰에 없음)·역할 범위로 클램프되어 발급된다."""
    _, token = make_user(role="archiver")
    c = client_for(token)
    r = c.post(
        "/api/web/settings/api-keys",
        json={"name": "mykey", "can_view": True, "can_archive": True},
        headers=POST_HEADERS,
    )
    assert r.status_code == 200
    assert r.json()["token"]
    keys = c.get("/api/web/settings/api-keys").json()["keys"]
    assert len(keys) == 1 and keys[0]["name"] == "mykey"
    # token_hash 등 비밀·내부 컬럼은 응답에 노출되지 않는다 (원칙 6)
    assert "token_hash" not in keys[0]
    assert "owner_user_id" not in keys[0]


def test_personal_api_key_idor(tmp_db):
    """타인 소유 키는 삭제할 수 없다(404 로 은폐)."""
    _, token_a = make_user(email="a@test.co", role="archiver")
    _, token_b = make_user(email="b@test.co", role="archiver")
    ca = client_for(token_a)
    ca.post("/api/web/settings/api-keys",
            json={"name": "akey", "can_view": True}, headers=POST_HEADERS)
    with db.connect() as conn:
        key_id = db.list_api_keys_for_owner(conn, 1)[0]["id"]
    # B 가 A 의 키 삭제 시도
    r = client_for(token_b).post(
        f"/api/web/settings/api-keys/{key_id}/delete", headers=POST_HEADERS
    )
    assert r.status_code == 404


# ---- 내 아카이브 (#8) ----


def test_my_archives_empty(tmp_db):
    _, token = make_user()
    r = client_for(token).get("/api/web/settings/archives")
    assert r.status_code == 200
    body = r.json()
    assert body["items"] == [] and body["total"] == 0


def _insert_my_log(requester, status="new", domain="example.com"):
    with db.connect() as conn:
        return db.insert_archive_log(
            conn, url=f"https://{domain}/x", domain=domain, status=status,
            started_at="2026-06-01T00:00:00+00:00", requested_by=requester,
        )


def test_my_archives_own_only(tmp_db):
    """내 아카이브는 본인이 요청한 로그만 보인다(requested_by 필터)."""
    uid_a, token_a = make_user(email="a@test.co", role="archiver")
    uid_b, _ = make_user(email="b@test.co", role="archiver")
    _insert_my_log(uid_a, domain="mine.com")
    _insert_my_log(uid_b, domain="theirs.com")
    body = client_for(token_a).get("/api/web/settings/archives").json()
    assert body["total"] == 1
    assert body["items"][0]["log"]["domain"] == "mine.com"


def test_my_archives_status_filter(tmp_db):
    uid, token = make_user(role="archiver")
    _insert_my_log(uid, status="new")
    _insert_my_log(uid, status="error")
    c = client_for(token)
    assert c.get("/api/web/settings/archives").json()["total"] == 2
    errors = c.get("/api/web/settings/archives?status=error").json()
    assert errors["total"] == 1
    assert errors["items"][0]["log"]["status"] == "error"


# ---- 계정 이메일 인증 표시·SSO 탈퇴 (#9 잔여) ----


def test_account_email_verified_display(tmp_db):
    uid, token = make_user(email="me@test.co")
    c = client_for(token)
    assert c.get("/api/web/settings/account").json()["email_verified"] is False
    with db.connect() as conn:
        db.set_email_verified(conn, uid)
    assert c.get("/api/web/settings/account").json()["email_verified"] is True


def test_account_sso_withdraw(tmp_db):
    """SSO 전용(패스워드 없음) 계정은 확인 이메일 입력으로 탈퇴한다."""
    _, token = make_user(email="sso@test.co", password=None, role="viewer")
    c = client_for(token)
    # 이메일 불일치 → 400
    assert c.post("/api/web/settings/account/withdraw",
                  json={"confirm": "wrong@test.co"}, headers=POST_HEADERS).status_code == 400
    # 이메일 일치 → 탈퇴
    assert c.post("/api/web/settings/account/withdraw",
                  json={"confirm": "sso@test.co"}, headers=POST_HEADERS).status_code == 200


# ---- 2단계 인증 TOTP (#9) ----


def test_totp_setup_confirm_disable(tmp_db):
    _, token = make_user(password="totppass123")
    c = client_for(token)
    setup = c.post("/api/web/settings/totp/setup", headers=POST_HEADERS).json()
    assert setup["secret"] and setup["qr"].startswith("data:image")
    code = pyotp.TOTP(setup["secret"]).now()
    assert c.post("/api/web/settings/totp/confirm",
                  json={"code": code}, headers=POST_HEADERS).status_code == 200
    assert c.get("/api/web/settings/account").json()["totp_enabled"] is True
    # 잘못된 패스워드로는 해제 불가
    assert c.post("/api/web/settings/totp/disable",
                  json={"password": "wrong"}, headers=POST_HEADERS).status_code == 401
    assert c.post("/api/web/settings/totp/disable",
                  json={"password": "totppass123"}, headers=POST_HEADERS).status_code == 200


def test_totp_confirm_wrong_code(tmp_db):
    _, token = make_user()
    c = client_for(token)
    c.post("/api/web/settings/totp/setup", headers=POST_HEADERS)
    r = c.post("/api/web/settings/totp/confirm",
               json={"code": "000000"}, headers=POST_HEADERS)
    assert r.status_code == 400


# ---- 패스키 (#9) — 단위(권한 가드). 등록 플로우는 가상 인증기 E2E 로 별도 검증 ----


def test_passkey_options_sso_forbidden(tmp_db):
    """SSO 전용(패스워드 없음) 계정은 패스키를 등록할 수 없다(400)."""
    _, token = make_user(email="sso@test.co", password=None)
    r = client_for(token).post("/api/web/settings/passkey/options", headers=POST_HEADERS)
    assert r.status_code == 400


# ---- i18n 카탈로그 ----


def test_i18n_catalog(tmp_db):
    _, token = make_user()
    c = client_for(token)
    cat = c.get("/api/web/i18n/en").json()
    assert cat["현황"] == "Overview"
    assert "perm|보기" not in cat  # ctx 키는 제외(평문만)
    assert c.get("/api/web/i18n/xx").status_code == 404
    assert client_for().get("/api/web/i18n/en").status_code == 200  # 공개(미인증 로그인 화면용)
