"""라이브 챌린지 SPA JSON API(/api/web/live) — 권한·소유자·명령 큐.

SSR /archive/jobs/{id}/live/* 와 같은 헬퍼·코어를 재사용하는 JSON 래퍼.
세션 쿠키 인증 + /api/web Origin(CSRF) 검사를 거친다.
"""
import pytest
from fastapi.testclient import TestClient

from chunchugwan import auth, config, db, live_challenge, storage
from chunchugwan.web import app as web_app

URL = "https://sd.test/article"
POST = {"X-Requested-With": "fetch", "Origin": "http://testserver"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "RESOURCES_DIR", tmp_path / "resources")
    monkeypatch.setattr(config, "DOCUMENTS_DIR", tmp_path / "documents")
    with db.connect() as conn:
        db.create_first_admin(conn, "boss@test.co", auth.hash_password("bosspass1234"))
        db.create_user(conn, "boss2@test.co", auth.hash_password("boss2pass1234"), role="admin")
        db.create_user(conn, "viewer@test.co", auth.hash_password("password1234"), role="viewer")
    web_app._active_jobs.clear()
    yield TestClient(web_app.app, headers={"X-Requested-With": "fetch"})
    web_app._active_jobs.clear()


def _login(c, email, pw):
    return c.post("/api/web/auth/login", json={"email": email, "password": pw})


def _make_needs_human(token="tok-abc") -> int:
    with db.connect() as conn:
        db.get_or_create_page(conn, URL, "sd.test", storage.url_to_slug(URL))
        db.enqueue_archive_job(conn, URL, source="cli")
        job = db.claim_due_archive_job(conn, "2099-01-01T00:00:00+00:00")
        db.mark_needs_human(conn, job["id"], token=token, viewport_w=1280, viewport_h=800)
    return job["id"]


def test_list_admin_only(client):
    _login(client, "viewer@test.co", "password1234")
    assert client.get("/api/web/live").status_code == 403


def test_list_jobs(client):
    _make_needs_human()
    _login(client, "boss@test.co", "bosspass1234")
    body = client.get("/api/web/live").json()
    assert [j["url"] for j in body["jobs"]] == [URL]
    assert body["jobs"][0]["held_by_other"] is False  # 아직 아무도 클레임 안 함


def test_view_claims_and_returns_meta(client):
    jid = _make_needs_human()
    _login(client, "boss@test.co", "bosspass1234")
    body = client.get(f"/api/web/live/{jid}").json()
    assert body["url"] == URL and body["owned"] is True
    assert body["viewport_w"] == 1280 and body["viewport_h"] == 800
    with db.connect() as conn:
        assert db.get_archive_job(conn, jid)["live_owner_id"] is not None


def test_view_404_when_not_needs_human(client):
    with db.connect() as conn:
        db.enqueue_archive_job(conn, "https://x.test/y", source="cli")
        j = db.claim_due_archive_job(conn, "2099-01-01T00:00:00+00:00")
    _login(client, "boss@test.co", "bosspass1234")
    assert client.get(f"/api/web/live/{j['id']}").status_code == 404


def test_view_admin_only(client):
    jid = _make_needs_human()
    _login(client, "viewer@test.co", "password1234")
    assert client.get(f"/api/web/live/{jid}").status_code == 403


def test_state_json(client):
    jid = _make_needs_human()
    _login(client, "boss@test.co", "bosspass1234")
    client.get(f"/api/web/live/{jid}")
    s = client.get(f"/api/web/live/{jid}/state").json()
    assert s["status"] == "needs_human" and s["owned"] is True


def test_click_enqueues_for_owner(client):
    jid = _make_needs_human()
    _login(client, "boss@test.co", "bosspass1234")
    client.get(f"/api/web/live/{jid}")  # 클레임
    r = client.post(
        f"/api/web/live/{jid}/click",
        json={"x": 100, "y": 200, "kind": "click"}, headers=POST,
    )
    assert r.status_code == 200 and r.json() == {"ok": True}
    with db.connect() as conn:
        cmd = conn.execute("SELECT kind, x, y FROM live_commands").fetchone()
    assert (cmd["kind"], cmd["x"], cmd["y"]) == ("click", 100, 200)


def test_click_blocked_for_non_owner_admin(client):
    jid = _make_needs_human()
    _login(client, "boss@test.co", "bosspass1234")
    client.get(f"/api/web/live/{jid}")  # boss 소유
    other = TestClient(web_app.app, headers={"X-Requested-With": "fetch"})
    _login(other, "boss2@test.co", "boss2pass1234")
    r = other.post(f"/api/web/live/{jid}/click", json={"x": 1, "y": 2}, headers=POST)
    assert r.status_code == 403


def test_key_and_cancel(client):
    jid = _make_needs_human()
    _login(client, "boss@test.co", "bosspass1234")
    client.get(f"/api/web/live/{jid}")
    assert client.post(
        f"/api/web/live/{jid}/key", json={"key": "hello", "kind": "text"}, headers=POST,
    ).status_code == 200
    r = client.post(f"/api/web/live/{jid}/cancel", headers=POST)
    assert r.status_code == 200 and r.json() == {"ok": True}
    with db.connect() as conn:
        assert db.get_archive_job(conn, jid)["live_cancel"] == 1


def test_solve_sets_flag_for_owner(client):
    jid = _make_needs_human()
    _login(client, "boss@test.co", "bosspass1234")
    client.get(f"/api/web/live/{jid}")
    r = client.post(f"/api/web/live/{jid}/solve", headers=POST)
    assert r.status_code == 200 and r.json() == {"ok": True}
    with db.connect() as conn:
        assert db.get_archive_job(conn, jid)["live_force_solve"] == 1


def test_solve_blocked_for_non_owner_admin(client):
    jid = _make_needs_human()
    _login(client, "boss@test.co", "bosspass1234")
    client.get(f"/api/web/live/{jid}")
    other = TestClient(web_app.app, headers={"X-Requested-With": "fetch"})
    _login(other, "boss2@test.co", "boss2pass1234")
    assert other.post(f"/api/web/live/{jid}/solve", headers=POST).status_code == 403


def test_solve_admin_only(client):
    jid = _make_needs_human()
    _login(client, "viewer@test.co", "password1234")
    assert client.post(f"/api/web/live/{jid}/solve", headers=POST).status_code == 403


def test_shot_404_then_served(client):
    jid = _make_needs_human("tok-shot-api")
    _login(client, "boss@test.co", "bosspass1234")
    assert client.get(f"/api/web/live/{jid}/shot").status_code == 404
    path = live_challenge.shot_path("tok-shot-api")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xd8\xff\xe0fakejpeg")
    r = client.get(f"/api/web/live/{jid}/shot")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
