"""사이트 자격증명 SPA JSON API(/api/web/sites/{id}/credentials) — 권한·등록·HAR·삭제.

SSR /sites/{id}/credentials 와 같은 코어(credentials·crypto·HAR 파서)를 재사용하는
JSON 래퍼. 세션 쿠키 인증 + /api/web Origin(CSRF) 검사를 거친다.
"""
import json

import pytest
from fastapi.testclient import TestClient

from chunchugwan import auth, config, credentials, db, storage
from chunchugwan.web import app as web_app

URL = "https://example.com/post"
SITE_KEY = "example.com"
POST = {"X-Requested-With": "fetch", "Origin": "http://testserver"}


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "RESOURCES_DIR", tmp_path / "resources")
    monkeypatch.setattr(config, "DOCUMENTS_DIR", tmp_path / "documents")
    monkeypatch.setattr(config, "SECRET_KEY", "test-secret-key")


@pytest.fixture
def client(tmp_db):
    with db.connect() as conn:
        db.create_first_admin(conn, "boss@test.co", auth.hash_password("bosspass1234"))
        db.create_user(conn, "viewer@test.co", auth.hash_password("password1234"), role="viewer")
        db.get_or_create_page(conn, URL, "example.com", storage.url_to_slug(URL))
    web_app._active_jobs.clear()
    yield TestClient(web_app.app)
    web_app._active_jobs.clear()


def _sid() -> int:
    with db.connect() as conn:
        return db.get_site_by_key(conn, SITE_KEY)["id"]


def _login(c, email, pw):
    return c.post("/api/web/auth/login", json={"email": email, "password": pw})


def _login_admin(c):
    return _login(c, "boss@test.co", "bosspass1234")


def _har(cookies):
    entries = [{
        "request": {"url": "https://example.com/login", "cookies": [], "headers": []},
        "response": {"cookies": cookies, "headers": []},
    }]
    return json.dumps({"log": {"version": "1.2", "entries": entries}})


def test_list_admin_only(client):
    _login(client, "viewer@test.co", "password1234")
    assert client.get(f"/api/web/sites/{_sid()}/credentials").status_code == 403


def test_list_returns_meta(client):
    _login_admin(client)
    body = client.get(f"/api/web/sites/{_sid()}/credentials").json()
    assert body["site"]["site_key"] == SITE_KEY
    assert body["secret_key_configured"] is True
    assert {k["value"] for k in body["kinds"]} == set(credentials.KINDS)
    assert body["credentials"] == []


def test_create_http_basic(client):
    sid = _sid()
    _login_admin(client)
    r = client.post(
        f"/api/web/sites/{sid}/credentials",
        data={"label": "admin", "kind": "http_basic", "username": "u", "password": "secret-pw"},
        headers=POST,
    )
    assert r.status_code == 200 and r.json() == {"ok": True}
    body = client.get(f"/api/web/sites/{sid}/credentials").json()
    assert [c["label"] for c in body["credentials"]] == ["admin"]
    # 비밀은 목록에 노출되지 않는다
    assert "secret-pw" not in json.dumps(body)


def test_create_session_via_har(client):
    sid = _sid()
    _login_admin(client)
    har = _har([{"name": "sid", "value": "abc", "domain": "example.com", "path": "/"}])
    r = client.post(
        f"/api/web/sites/{sid}/credentials",
        data={"label": "har-sess", "kind": "session"},
        files={"har_file": ("login.har", har, "application/json")},
        headers=POST,
    )
    assert r.status_code == 200 and r.json() == {"ok": True}
    # 저장된 storage_state 에 HAR 쿠키가 추출돼 들어갔는지 확인
    with db.connect() as conn:
        cid = db.list_site_credentials(conn, sid)[0]["id"]
        payload = credentials.reveal(conn, cid)
    cookies = payload["storage_state"]["cookies"]
    assert cookies == [{"name": "sid", "value": "abc", "domain": "example.com", "path": "/"}]


def test_create_jwt(client):
    sid = _sid()
    _login_admin(client)
    r = client.post(
        f"/api/web/sites/{sid}/credentials",
        data={"label": "tok", "kind": "jwt", "token": "eyJ.aaa.bbb"}, headers=POST,
    )
    assert r.status_code == 200


def test_create_duplicate_label_rejected(client):
    sid = _sid()
    _login_admin(client)
    base = {"label": "dup", "kind": "http_basic", "username": "u", "password": "pw"}
    assert client.post(f"/api/web/sites/{sid}/credentials", data=base, headers=POST).status_code == 200
    assert client.post(f"/api/web/sites/{sid}/credentials", data=base, headers=POST).status_code == 400


def test_create_bad_kind(client):
    sid = _sid()
    _login_admin(client)
    r = client.post(
        f"/api/web/sites/{sid}/credentials",
        data={"label": "x", "kind": "nope"}, headers=POST,
    )
    assert r.status_code == 400


def test_create_blocked_without_secret_key(client, monkeypatch):
    monkeypatch.setattr(config, "SECRET_KEY", "")
    sid = _sid()
    _login_admin(client)
    r = client.post(
        f"/api/web/sites/{sid}/credentials",
        data={"label": "x", "kind": "http_basic", "username": "u", "password": "p"},
        headers=POST,
    )
    assert r.status_code == 400
    assert client.get(f"/api/web/sites/{sid}/credentials").json()["secret_key_configured"] is False


def test_delete(client):
    sid = _sid()
    _login_admin(client)
    client.post(
        f"/api/web/sites/{sid}/credentials",
        data={"label": "gone", "kind": "http_basic", "username": "u", "password": "pw"},
        headers=POST,
    )
    with db.connect() as conn:
        cid = db.list_site_credentials(conn, sid)[0]["id"]
    r = client.post(f"/api/web/sites/{sid}/credentials/{cid}/delete", headers=POST)
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert client.get(f"/api/web/sites/{sid}/credentials").json()["credentials"] == []


def test_delete_admin_only(client):
    sid = _sid()
    _login_admin(client)
    client.post(
        f"/api/web/sites/{sid}/credentials",
        data={"label": "keep", "kind": "http_basic", "username": "u", "password": "pw"},
        headers=POST,
    )
    with db.connect() as conn:
        cid = db.list_site_credentials(conn, sid)[0]["id"]
    other = TestClient(web_app.app)
    _login(other, "viewer@test.co", "password1234")
    assert other.post(f"/api/web/sites/{sid}/credentials/{cid}/delete", headers=POST).status_code == 403
