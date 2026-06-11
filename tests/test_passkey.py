"""패스키(WebAuthn) 2FA — 코어 검증 + 라우트 테스트.

실제 인증기 대신 ECDSA P-256 키로 서명하는 FakeAuthenticator 를 사용해
py_webauthn 검증 경로까지 통째로 테스트한다 (네트워크/브라우저 불필요).
"""
import base64
import hashlib
import json
import secrets

import cbor2
import pytest
from cryptography.hazmat.primitives.asymmetric.ec import (
    ECDSA,
    SECP256R1,
    generate_private_key,
)
from cryptography.hazmat.primitives.hashes import SHA256
from fastapi.testclient import TestClient

from chunchugwan import auth, config, db
from chunchugwan.web import app as web_app

ORIGIN = config.WEBAUTHN_ORIGINS[0]


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class FakeAuthenticator:
    """attestation 'none' + ES256 서명만 흉내내는 소프트 인증기."""

    def __init__(self):
        self.key = generate_private_key(SECP256R1())
        self.credential_id = secrets.token_bytes(16)
        self.sign_count = 0

    def _client_data(self, type_: str, challenge_b64u: str) -> bytes:
        return json.dumps(
            {"type": type_, "challenge": challenge_b64u, "origin": ORIGIN}
        ).encode()

    def create(self, options: dict) -> dict:
        """navigator.credentials.create() 응답에 해당하는 등록 credential."""
        client_data = self._client_data("webauthn.create", options["challenge"])
        rp_id_hash = hashlib.sha256(options["rp"]["id"].encode()).digest()
        pub = self.key.public_key().public_numbers()
        cose_key = cbor2.dumps(
            {1: 2, 3: -7, -1: 1,
             -2: pub.x.to_bytes(32, "big"), -3: pub.y.to_bytes(32, "big")}
        )
        # flags 0x45 = UP | UV | AT(자격증명 데이터 포함)
        auth_data = (
            rp_id_hash + bytes([0x45]) + (0).to_bytes(4, "big")
            + bytes(16) + len(self.credential_id).to_bytes(2, "big")
            + self.credential_id + cose_key
        )
        att_obj = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        return {
            "id": b64u(self.credential_id),
            "rawId": b64u(self.credential_id),
            "type": "public-key",
            "clientExtensionResults": {},
            "response": {
                "clientDataJSON": b64u(client_data),
                "attestationObject": b64u(att_obj),
                "transports": ["internal"],
            },
        }

    def get(self, options: dict) -> dict:
        """navigator.credentials.get() 응답에 해당하는 인증 credential."""
        client_data = self._client_data("webauthn.get", options["challenge"])
        rp_id_hash = hashlib.sha256(options["rpId"].encode()).digest()
        self.sign_count += 1
        auth_data = rp_id_hash + bytes([0x05]) + self.sign_count.to_bytes(4, "big")
        signature = self.key.sign(
            auth_data + hashlib.sha256(client_data).digest(), ECDSA(SHA256())
        )
        return {
            "id": b64u(self.credential_id),
            "rawId": b64u(self.credential_id),
            "type": "public-key",
            "clientExtensionResults": {},
            "response": {
                "clientDataJSON": b64u(client_data),
                "authenticatorData": b64u(auth_data),
                "signature": b64u(signature),
                "userHandle": None,
            },
        }


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")


@pytest.fixture
def client(tmp_db):
    """관리자 1명이 등록된 상태의 TestClient (최초 구동 통과)."""
    with db.connect() as conn:
        db.create_user(
            conn, "admin@test.co", auth.hash_password("adminpass123"), role="admin"
        )
    return TestClient(web_app.app)


def signup(client, email="a@b.co", password="12345678"):
    return client.post(
        "/signup", data={"email": email, "password": password},
        follow_redirects=False,
    )


def enroll_passkey(client, name="테스트 키") -> FakeAuthenticator:
    """로그인된 client 에 패스키를 등록하고 인증기를 반환."""
    authenticator = FakeAuthenticator()
    options = client.post("/settings/passkey/options").json()
    res = client.post(
        "/settings/passkey/register",
        json={"name": name, "credential": authenticator.create(options)},
    )
    assert res.status_code == 200 and res.json() == {"ok": True}
    return authenticator


# ---- 코어 검증 (auth.py + py_webauthn) ----


def test_registration_options_and_verify():
    fake = FakeAuthenticator()
    options_json, challenge = auth.passkey_registration_options(1, "a@b.co", [])
    options = json.loads(options_json)
    assert options["rp"]["id"] == config.WEBAUTHN_RP_ID
    assert options["challenge"] == challenge

    verified = auth.verify_passkey_registration(fake.create(options), challenge)
    assert verified is not None
    assert verified["credential_id"] == b64u(fake.credential_id)
    assert verified["public_key"] and verified["sign_count"] == 0

    # 다른 챌린지로 검증하면 실패
    wrong = b64u(secrets.token_bytes(32))
    assert auth.verify_passkey_registration(fake.create(options), wrong) is None
    # 깨진 입력도 None (예외 누출 없음)
    assert auth.verify_passkey_registration({"garbage": 1}, challenge) is None


def test_authentication_verify_and_wrong_key():
    fake = FakeAuthenticator()
    reg_json, reg_challenge = auth.passkey_registration_options(1, "a@b.co", [])
    verified = auth.verify_passkey_registration(
        fake.create(json.loads(reg_json)), reg_challenge
    )

    options_json, challenge = auth.passkey_authentication_options(
        [verified["credential_id"]]
    )
    options = json.loads(options_json)
    assert options["rpId"] == config.WEBAUTHN_RP_ID

    new_count = auth.verify_passkey_authentication(
        fake.get(options), challenge, verified["public_key"], 0
    )
    assert new_count == 1

    # 같은 credential_id 를 사칭하는 다른 키의 서명은 거부
    imposter = FakeAuthenticator()
    imposter.credential_id = fake.credential_id
    assert (
        auth.verify_passkey_authentication(
            imposter.get(options), challenge, verified["public_key"], 0
        )
        is None
    )


# ---- DB ----


def test_passkey_crud(tmp_db):
    with db.connect() as conn:
        uid = db.create_user(conn, "a@b.co", auth.hash_password("12345678"))
        pid = db.create_passkey(conn, uid, "cred-1", "pk-1", 0, "키1")
        db.create_passkey(conn, uid, "cred-2", "pk-2", 0, "키2")
        assert db.count_passkeys(conn, uid) == 2
        assert [c["name"] for c in db.list_passkeys(conn, uid)] == ["키1", "키2"]
        assert db.get_passkey(conn, uid, "cred-1")["id"] == pid
        assert db.get_passkey(conn, uid + 1, "cred-1") is None  # 타 사용자 불가

        db.touch_passkey(conn, pid, 7)
        row = db.get_passkey(conn, uid, "cred-1")
        assert row["sign_count"] == 7 and row["last_used_at"] is not None

        assert db.delete_passkey(conn, uid + 1, pid) is False  # 소유자만 삭제
        assert db.delete_passkey(conn, uid, pid) is True
        assert db.count_passkeys(conn, uid) == 1


def test_session_challenge_consume_once(tmp_db):
    with db.connect() as conn:
        uid = db.create_user(conn, "a@b.co")
        token = auth.issue_session(conn, uid)
        token_hash = auth.hash_token(token)
        assert db.consume_session_challenge(conn, token_hash) is None
        db.set_session_challenge(conn, token_hash, "ch-1")
        assert db.consume_session_challenge(conn, token_hash) == "ch-1"
        assert db.consume_session_challenge(conn, token_hash) is None  # 1회용


# ---- 라우트: 등록 / 2단계 로그인 / 삭제 ----


def test_passkey_enroll_and_two_step_login(client):
    signup(client)
    fake = enroll_passkey(client)
    client.post("/logout")

    # 1단계: 패스워드 → pending 세션 + 2단계 페이지로
    res = client.post(
        "/login", data={"email": "a@b.co", "password": "12345678", "next": "/"},
        follow_redirects=False,
    )
    assert res.status_code == 303 and res.headers["location"].startswith("/login/totp")
    assert client.get("/", follow_redirects=False).status_code == 302  # 아직 미인증
    page = client.get("/login/totp")
    assert "패스키로 인증" in page.text

    # 2단계: 패스키 인증
    options = client.post("/login/passkey/options").json()
    out = client.post(
        "/login/passkey", json={"credential": fake.get(options), "next": "/page/1"}
    )
    assert out.status_code == 200
    assert out.json() == {"ok": True, "next": "/page/1"}
    assert client.get("/").status_code == 200  # active 세션

    with db.connect() as conn:
        uid = db.get_user_by_email(conn, "a@b.co")["id"]
        cred = db.list_passkeys(conn, uid)[0]
        assert cred["sign_count"] == 1 and cred["last_used_at"] is not None


def test_passkey_login_rejects_replay_and_bad_input(client):
    signup(client)
    fake = enroll_passkey(client)
    client.post("/logout")
    client.post(
        "/login", data={"email": "a@b.co", "password": "12345678"},
        follow_redirects=False,
    )

    # 옵션 없이 바로 검증 → 챌린지 없음
    res = client.post("/login/passkey", json={"credential": fake.get(
        {"rpId": config.WEBAUTHN_RP_ID, "challenge": b64u(secrets.token_bytes(32))}
    )})
    assert res.status_code == 400

    # 챌린지는 1회용 — 같은 응답 재전송은 거부
    options = client.post("/login/passkey/options").json()
    credential = fake.get(options)
    assert client.post("/login/passkey", json={"credential": credential}).status_code == 200
    assert client.post("/login/passkey", json={"credential": credential}).status_code in (400, 401)


def test_passkey_login_options_require_pending_session(client):
    assert client.post("/login/passkey/options").status_code == 401
    assert client.post("/login/passkey", json={"credential": {}}).status_code == 401


def test_passkey_register_requires_challenge_and_rejects_duplicate(client):
    signup(client)
    fake = FakeAuthenticator()
    options = client.post("/settings/passkey/options").json()
    credential = fake.create(options)

    # 챌린지를 소비한 뒤 다시 등록 시도 → 400
    assert client.post(
        "/settings/passkey/register", json={"name": "k", "credential": credential}
    ).status_code == 200
    assert client.post(
        "/settings/passkey/register", json={"name": "k", "credential": credential}
    ).status_code == 400

    # 같은 credential_id 재등록 차단 (새 챌린지로 시도)
    options2 = client.post("/settings/passkey/options").json()
    dup = client.post(
        "/settings/passkey/register",
        json={"name": "k2", "credential": fake.create(options2)},
    )
    assert dup.status_code == 400
    assert "이미 등록된" in dup.json()["detail"]


def test_passkey_delete_requires_password(client):
    signup(client)
    enroll_passkey(client)
    with db.connect() as conn:
        uid = db.get_user_by_email(conn, "a@b.co")["id"]
        pid = db.list_passkeys(conn, uid)[0]["id"]

    bad = client.post(f"/settings/passkey/{pid}/delete", data={"password": "wrongpass"})
    assert bad.status_code == 401
    ok = client.post(
        f"/settings/passkey/{pid}/delete", data={"password": "12345678"},
        follow_redirects=False,
    )
    assert ok.status_code == 303
    with db.connect() as conn:
        assert db.count_passkeys(conn, uid) == 0

    # 패스키가 모두 삭제되면 단일 단계 로그인으로 복귀
    client.post("/logout")
    res = client.post(
        "/login", data={"email": "a@b.co", "password": "12345678"},
        follow_redirects=False,
    )
    assert res.headers["location"] == "/"


def test_passkey_setup_page_and_sso_only_account(client, tmp_db):
    signup(client)
    res = client.get("/settings/passkey")
    assert res.status_code == 200 and "패스키 등록" in res.text

    # SSO 전용 계정(password_hash NULL)은 등록 옵션이 거부된다
    with db.connect() as conn:
        sso_uid = db.create_user(conn, "sso@b.co")
        token = auth.issue_session(conn, sso_uid)
    client.cookies.set(config.SESSION_COOKIE, token)
    assert client.post("/settings/passkey/options").status_code == 400
    page = client.get("/settings/passkey")
    assert "SSO 전용 계정" in page.text
