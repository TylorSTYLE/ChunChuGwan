"""1회성 인증 자격증명 — 암호화, 캡슐 CRUD/TTL, auth-profiles 엔드포인트,
인증 스냅샷 접근 제한, 시스템 설정, 백업 제외, same-origin 누출 차단."""
import base64
import json
import types

import pytest
from fastapi.testclient import TestClient

from chunchugwan import auth, backup, capture, config, credentials, db, storage
from chunchugwan.web import app as web_app

VALID_KEY = base64.b64encode(b"0123456789abcdef0123456789abcdef").decode()


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "RESOURCES_DIR", tmp_path / "resources")
    monkeypatch.setattr(config, "DOCUMENTS_DIR", tmp_path / "documents")


def _seed_users():
    with db.connect() as conn:
        db.create_first_admin(conn, "boss@test.co", auth.hash_password("bosspass1234"))
        db.create_user(conn, "arch@test.co", auth.hash_password("password1234"), role="archiver")
        db.create_user(conn, "arch2@test.co", auth.hash_password("password1234"), role="archiver")
        db.create_user(conn, "viewer@test.co", auth.hash_password("password1234"), role="viewer")


@pytest.fixture
def client(tmp_db, monkeypatch):
    monkeypatch.setattr(config, "CREDENTIAL_KEY", VALID_KEY)
    _seed_users()
    web_app._active_jobs.clear()
    yield TestClient(web_app.app)
    web_app._active_jobs.clear()


def _uid(email):
    with db.connect() as conn:
        return db.get_user_by_email(conn, email)["id"]


def _ext_token(email):
    uid = _uid(email)
    with db.connect() as conn:
        return auth.issue_api_key(
            conn, "ext", can_view=True, can_archive=True,
            created_by=uid, owner_user_id=uid, ttl_seconds=None,
        )


def _system_token():
    with db.connect() as conn:
        return auth.issue_api_key(
            conn, "sys", can_view=True, can_archive=True,
            created_by=1, ttl_seconds=None,
        )


def _headers(token):
    return {"Authorization": f"Bearer {token}"}


def _cookie_state(domain="example.com"):
    return {"cookies": [{"name": "sid", "value": "secret",
                         "domain": domain, "path": "/"}], "origins": []}


# ---- 암호화 (credentials.py) ----


def test_credentials_roundtrip(monkeypatch):
    monkeypatch.setattr(config, "CREDENTIAL_KEY", VALID_KEY)
    blob = credentials.encrypt(b"hello")
    assert blob != b"hello"
    assert credentials.decrypt(blob) == b"hello"


def test_credentials_tamper_detected(monkeypatch):
    monkeypatch.setattr(config, "CREDENTIAL_KEY", VALID_KEY)
    blob = bytearray(credentials.encrypt(b"hello"))
    blob[-1] ^= 0x01
    with pytest.raises(Exception):
        credentials.decrypt(bytes(blob))


def test_credentials_hex_key(monkeypatch):
    monkeypatch.setattr(config, "CREDENTIAL_KEY", "ab" * 32)  # 64 hex = 32 bytes
    assert credentials.decrypt(credentials.encrypt(b"x")) == b"x"


def test_credentials_disabled_without_key(monkeypatch):
    monkeypatch.setattr(config, "CREDENTIAL_KEY", "")
    assert credentials.is_enabled() is False
    with pytest.raises(credentials.CredentialKeyError):
        credentials.encrypt(b"x")


def test_credentials_bad_length_rejected(monkeypatch):
    monkeypatch.setattr(config, "CREDENTIAL_KEY", base64.b64encode(b"short").decode())
    with pytest.raises(credentials.CredentialKeyError):
        credentials.encrypt(b"x")


# ---- 캡슐 CRUD / TTL GC ----


def test_capsule_crud_and_expiry_gc(tmp_db):
    _seed_users()
    uid = _uid("arch@test.co")
    with db.connect() as conn:
        live = db.create_auth_capsule(
            conn, url="https://x/", scope_host="x", owner_user_id=uid,
            ciphertext=b"c", network_tag_id=None, ttl_seconds=3600,
        )
        expired = db.create_auth_capsule(
            conn, url="https://y/", scope_host="y", owner_user_id=uid,
            ciphertext=b"c", network_tag_id=None, ttl_seconds=-10,
        )
        assert db.get_auth_capsule(conn, live) is not None
        assert db.delete_expired_auth_capsules(conn) == 1  # 만료분만
        assert db.get_auth_capsule(conn, live) is not None
        assert db.get_auth_capsule(conn, expired) is None
        db.delete_auth_capsule(conn, live)
        assert db.get_auth_capsule(conn, live) is None


# ---- POST /api/v1/auth-profiles ----


def test_auth_profile_creates_encrypted_capsule(client, monkeypatch):
    seen = {}

    def fake_run(url, force=False, interval_seconds=None, run_at=None,
                 source="web", network_tag_id=None, auth_capsule_id=None):
        seen["capsule_id"] = auth_capsule_id  # 삭제하지 않고 검사만

    monkeypatch.setattr(web_app, "_run_archive", fake_run)
    token = _ext_token("arch@test.co")
    ss = _cookie_state()
    r = client.post(
        "/api/v1/auth-profiles",
        json={"url": "https://example.com/secret", "storage_state": ss},
        headers=_headers(token),
    )
    assert r.status_code == 202
    assert r.json()["authenticated"] is True
    with db.connect() as conn:
        cap = db.get_auth_capsule(conn, seen["capsule_id"])
    assert cap is not None
    assert cap["owner_user_id"] == _uid("arch@test.co")
    assert cap["ciphertext"] != json.dumps(ss).encode("utf-8")  # 평문 저장 아님
    # 검증·정제 후 — cookies 만 남고 origins(localStorage)는 드롭된다
    assert json.loads(credentials.decrypt(cap["ciphertext"])) == {"cookies": ss["cookies"]}


def test_auth_profile_consumes_and_deletes_capsule(client, monkeypatch):
    captured = {}

    def fake_archive(url, **kw):
        captured.update(url=url, **kw)
        return types.SimpleNamespace(status="ok", url=url, snapshot_id=None)

    monkeypatch.setattr(web_app.pipeline, "archive_url", fake_archive)
    token = _ext_token("arch@test.co")
    ss = _cookie_state()
    r = client.post(
        "/api/v1/auth-profiles",
        json={"url": "https://example.com/secret", "storage_state": ss},
        headers=_headers(token),
    )
    assert r.status_code == 202
    # 백그라운드 _run_archive 가 캡슐을 복호해 storage_state 를 넘기고 삭제했다
    # (검증·정제 후 cookies 만 — origins 드롭)
    assert captured["storage_state"] == {"cookies": ss["cookies"]}
    assert captured["authenticated_by"] == _uid("arch@test.co")
    with db.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM auth_capsules"
        ).fetchone()["c"] == 0


def test_auth_profile_requires_credential_key(client, monkeypatch):
    monkeypatch.setattr(config, "CREDENTIAL_KEY", "")
    token = _ext_token("arch@test.co")
    r = client.post(
        "/api/v1/auth-profiles",
        json={"url": "https://example.com/x", "storage_state": _cookie_state()},
        headers=_headers(token),
    )
    assert r.status_code == 503


def test_auth_profile_rejects_system_key(client):
    token = _system_token()  # owner 없음
    r = client.post(
        "/api/v1/auth-profiles",
        json={"url": "https://example.com/x", "storage_state": _cookie_state()},
        headers=_headers(token),
    )
    assert r.status_code == 403


def test_auth_profile_validates_storage_state(client):
    token = _ext_token("arch@test.co")
    # 빈 쿠키
    assert client.post(
        "/api/v1/auth-profiles",
        json={"url": "https://example.com/x", "storage_state": {"cookies": []}},
        headers=_headers(token),
    ).status_code == 400
    # 대상 호스트 밖 도메인 쿠키
    assert client.post(
        "/api/v1/auth-profiles",
        json={"url": "https://example.com/x",
              "storage_state": _cookie_state(domain="evil.com")},
        headers=_headers(token),
    ).status_code == 400


def test_auth_profile_blocks_loopback(client, monkeypatch):
    monkeypatch.setattr(web_app, "_run_archive", lambda *a, **k: None)
    token = _ext_token("arch@test.co")
    r = client.post(
        "/api/v1/auth-profiles",
        json={"url": "http://localhost/x",
              "storage_state": _cookie_state(domain="localhost")},
        headers=_headers(token),
    )
    assert r.status_code == 400


# ---- 인증 스냅샷 접근 제한 ----


def _insert_auth_snapshot(owner_id):
    url = "https://example.com/secret"
    domain, slug = "example.com", storage.url_to_slug(url)
    with db.connect() as conn:
        page_id = db.get_or_create_page(conn, url, domain, slug)
        snap_dir = storage.page_dir(domain, slug) / "2026-06-02T00-00-00"
        snap_dir.mkdir(parents=True)
        (snap_dir / "content.md").write_text("일급비밀", encoding="utf-8")
        sid = db.insert_snapshot(
            conn, page_id, taken_at="2026-06-02T00:00:00+00:00",
            dir_name="2026-06-02T00-00-00", content_hash="hs",
            final_url=url, http_status=200, changed=1,
            authenticated=1, authenticated_by=owner_id,
        )
    return page_id, sid


def test_authenticated_snapshot_owner_only_via_api(client):
    owner = _uid("arch@test.co")
    page_id, sid = _insert_auth_snapshot(owner)
    owner_tok = _ext_token("arch@test.co")
    other_tok = _ext_token("arch2@test.co")
    sys_tok = _system_token()
    # 메타데이터: 소유자만
    assert client.get(f"/api/v1/snapshots/{sid}", headers=_headers(owner_tok)).status_code == 200
    assert client.get(f"/api/v1/snapshots/{sid}", headers=_headers(other_tok)).status_code == 404
    assert client.get(f"/api/v1/snapshots/{sid}", headers=_headers(sys_tok)).status_code == 404
    # 파일(content): 소유자만 (_load_snapshot 가드)
    r = client.get(f"/api/v1/snapshots/{sid}/file/content.md", headers=_headers(owner_tok))
    assert r.status_code == 200 and "일급비밀" in r.text
    assert client.get(
        f"/api/v1/snapshots/{sid}/file/content.md", headers=_headers(other_tok)
    ).status_code == 404
    # 페이지 히스토리 목록: 비소유자에겐 인증 스냅샷이 빠진다
    owner_snaps = client.get(f"/api/v1/pages/{page_id}", headers=_headers(owner_tok)).json()["snapshots"]
    other_snaps = client.get(f"/api/v1/pages/{page_id}", headers=_headers(other_tok)).json()["snapshots"]
    assert len(owner_snaps) == 1 and other_snaps == []


def test_authenticated_snapshot_denied_to_other_viewer_web(client):
    owner = _uid("arch@test.co")
    _, sid = _insert_auth_snapshot(owner)
    client.post("/login", data={"email": "viewer@test.co", "password": "password1234"},
                follow_redirects=False)
    assert client.get(f"/snapshot/{sid}").status_code == 404  # 가드가 렌더 전에 차단


# ---- 시스템 설정 ----


def test_system_credential_settings(client):
    client.post("/login", data={"email": "boss@test.co", "password": "bosspass1234"},
                follow_redirects=False)
    r = client.post("/system/credential-settings",
                    data={"credential_ttl_hours": "48"}, follow_redirects=False)
    assert r.status_code == 303
    with db.connect() as conn:
        assert db.credential_ttl_hours(conn) == 48
    # 범위 밖 → error
    r = client.post("/system/credential-settings",
                    data={"credential_ttl_hours": "99999"}, follow_redirects=False)
    assert "error=" in r.headers["location"]


# ---- 백업 제외 / same-origin 누출 차단 ----


def test_backup_strips_auth_capsules(tmp_db, tmp_path):
    _seed_users()
    uid = _uid("arch@test.co")
    with db.connect() as conn:
        db.create_auth_capsule(conn, url="https://x/", scope_host="x",
                               owner_user_id=uid, ciphertext=b"c",
                               network_tag_id=None, ttl_seconds=3600)
    import sqlite3
    copy = tmp_path / "copy.db"
    backup._consistent_db_copy(copy)
    with sqlite3.connect(copy) as c:
        assert c.execute("SELECT COUNT(*) FROM auth_capsules").fetchone()[0] == 1
    backup._strip_auth_capsules(copy)
    with sqlite3.connect(copy) as c:
        assert c.execute("SELECT COUNT(*) FROM auth_capsules").fetchone()[0] == 0


def test_fetch_via_context_blocks_cross_origin_when_authenticated():
    page = types.SimpleNamespace(url="https://example.com/page")
    # 인증 캡처(same_origin_only)면 다른 호스트 자원은 컨텍스트를 건드리지 않고 None
    assert capture._fetch_via_context(
        page, "https://cdn.other.com/x.png", same_origin_only=True
    ) is None
