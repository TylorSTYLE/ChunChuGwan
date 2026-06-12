"""OIDC(Authentik) 라우트 테스트 — IdP 호출은 전부 monkeypatch (네트워크 의존 0)."""
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from chunchugwan import auth, config, db, oidc
from chunchugwan.web import app as web_app

CLAIMS = {"sub": "ak-user-1", "email": "sso@example.com", "email_verified": True}


@pytest.fixture
def client(tmp_path, monkeypatch):
    """OIDC 가 설정된 환경의 TestClient. IdP 함수는 가짜로 대체."""
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "RESOURCES_DIR", tmp_path / "resources")
    monkeypatch.setattr(config, "OIDC_ISSUER", "https://idp.test/application/o/arc")
    monkeypatch.setattr(config, "OIDC_CLIENT_ID", "cid")
    monkeypatch.setattr(config, "OIDC_CLIENT_SECRET", "sec")

    monkeypatch.setattr(
        oidc, "build_authorize_url",
        lambda state, nonce: f"https://idp.test/auth?state={state}&nonce={nonce}",
    )
    monkeypatch.setattr(oidc, "exchange_code", lambda code: {"id_token": f"jwt-{code}"})
    # nonce 가 oidc_states 에 저장된 값으로 넘어오는지까지 검증
    def fake_validate(id_token, nonce):
        assert id_token.startswith("jwt-") and nonce
        return dict(CLAIMS)

    monkeypatch.setattr(oidc, "validate_id_token", fake_validate)
    # 최초 구동 모드를 벗어나도록 관리자 1명 사전 등록
    with db.connect() as conn:
        db.create_user(
            conn, "admin@test.co", auth.hash_password("adminpass123"), role="admin"
        )
    return TestClient(web_app.app)


def start_login(client) -> str:
    """/auth/oidc/login 을 호출해 IdP 로 넘기는 state 를 반환."""
    res = client.get("/auth/oidc/login?next=/page/1", follow_redirects=False)
    assert res.status_code == 302
    loc = res.headers["location"]
    assert loc.startswith("https://idp.test/auth?")
    return parse_qs(urlsplit(loc).query)["state"][0]


def test_oidc_login_redirects_to_idp(client):
    state = start_login(client)
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM oidc_states WHERE state = ?", (state,)
        ).fetchone()
    assert row is not None and row["redirect_to"] == "/page/1"


def test_callback_provisions_new_user(client):
    state = start_login(client)
    res = client.get(
        f"/auth/oidc/callback?code=abc&state={state}", follow_redirects=False
    )
    assert res.status_code == 303 and res.headers["location"] == "/page/1"
    with db.connect() as conn:
        user = db.get_user_by_email(conn, "sso@example.com")
        assert user is not None and user["password_hash"] is None  # SSO 전용
        # 자동 프로비저닝도 가입 초기 권한 설정(기본 권한없음)을 따른다
        assert user["role"] == "pending"
        ident = db.get_identity(conn, "authentik", "ak-user-1")
        assert ident["user_id"] == user["id"]
    # 세션은 발급되지만 승인 전이라 안내 페이지로만 보내진다
    assert client.get("/healthz").status_code == 200
    res = client.get("/", follow_redirects=False)
    assert res.status_code == 302 and res.headers["location"] == "/pending"


def test_callback_provision_role_follows_setting(client):
    """가입 초기 권한을 보기 전용으로 바꾸면 SSO 자동 생성도 따라간다."""
    with db.connect() as conn:
        db.set_setting(conn, db.SIGNUP_DEFAULT_ROLE_KEY, "viewer")
    state = start_login(client)
    client.get(f"/auth/oidc/callback?code=abc&state={state}")
    with db.connect() as conn:
        assert db.get_user_by_email(conn, "sso@example.com")["role"] == "viewer"
    assert client.get("/", follow_redirects=False).status_code == 200


def test_callback_existing_sub_reuses_user(client):
    for _ in range(2):
        client.cookies.clear()  # 이전 로그인의 (pending) 세션을 끊고 재로그인
        state = start_login(client)
        client.get(f"/auth/oidc/callback?code=abc&state={state}")
    with db.connect() as conn:
        count = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    assert count == 2  # 사전 등록된 관리자 + SSO 사용자 1명 (재로그인은 재사용)


def test_callback_links_existing_email_account(client):
    with db.connect() as conn:
        uid = db.create_user(conn, "sso@example.com", auth.hash_password("12345678"))
    state = start_login(client)
    client.get(f"/auth/oidc/callback?code=abc&state={state}")
    with db.connect() as conn:
        assert db.get_identity(conn, "authentik", "ak-user-1")["user_id"] == uid
        assert conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 2


def test_callback_unverified_email_cannot_link(client, monkeypatch):
    monkeypatch.setattr(
        oidc, "validate_id_token",
        lambda id_token, nonce: {**CLAIMS, "email_verified": False},
    )
    with db.connect() as conn:
        db.create_user(conn, "sso@example.com", auth.hash_password("12345678"))
    state = start_login(client)
    res = client.get(f"/auth/oidc/callback?code=abc&state={state}")
    assert res.status_code == 403


def test_callback_bad_state(client):
    assert client.get("/auth/oidc/callback?code=abc&state=bogus").status_code == 400


def test_callback_state_single_use(client):
    state = start_login(client)
    client.get(f"/auth/oidc/callback?code=abc&state={state}")
    client.cookies.clear()
    assert client.get(f"/auth/oidc/callback?code=abc&state={state}").status_code == 400


def test_callback_idp_error(client):
    assert client.get("/auth/oidc/callback?error=access_denied").status_code == 400


def test_oidc_disabled_404(client, monkeypatch):
    monkeypatch.setattr(config, "OIDC_ISSUER", "")
    assert client.get("/auth/oidc/login").status_code == 404


def test_login_page_shows_sso_button(client):
    res = client.get("/login")
    assert "Authentik" in res.text and "/auth/oidc/login" in res.text
