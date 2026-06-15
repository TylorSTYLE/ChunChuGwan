"""라이브 챌린지 뷰어 라우트 — 권한·소유자·명령 큐 (web/app.py)."""
import pytest
from fastapi.testclient import TestClient

from chunchugwan import auth, config, db, live_challenge, storage
from chunchugwan.web import app as web_app

URL = "https://sd.test/article"


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
    yield TestClient(web_app.app)
    web_app._active_jobs.clear()


def _login(client, email, pw):
    return client.post("/login", data={"email": email, "password": pw}, follow_redirects=False)


def _make_needs_human(token="tok-abc") -> int:
    with db.connect() as conn:
        db.get_or_create_page(conn, URL, "sd.test", storage.url_to_slug(URL))
        db.enqueue_archive_job(conn, URL, source="cli")
        job = db.claim_due_archive_job(conn, "2099-01-01T00:00:00+00:00")
        db.mark_needs_human(conn, job["id"], token=token, viewport_w=1280, viewport_h=800)
    return job["id"]


def test_needs_human_admin_only(client):
    _login(client, "viewer@test.co", "password1234")
    assert client.get("/archive/needs-human").status_code == 403


def test_needs_human_lists_jobs(client):
    _make_needs_human()
    _login(client, "boss@test.co", "bosspass1234")
    r = client.get("/archive/needs-human")
    assert r.status_code == 200 and URL in r.text


def test_live_view_claims_session(client):
    jid = _make_needs_human()
    _login(client, "boss@test.co", "bosspass1234")
    assert client.get(f"/archive/jobs/{jid}/live").status_code == 200
    with db.connect() as conn:  # 여는 admin 이 소유자(입력 권한자)가 된다
        assert db.get_archive_job(conn, jid)["live_owner_id"] is not None


def test_live_view_404_for_non_needs_human(client):
    with db.connect() as conn:
        db.enqueue_archive_job(conn, "https://x.test/y", source="cli")
        j = db.claim_due_archive_job(conn, "2099-01-01T00:00:00+00:00")
    _login(client, "boss@test.co", "bosspass1234")
    assert client.get(f"/archive/jobs/{j['id']}/live").status_code == 404


def test_live_view_admin_only(client):
    jid = _make_needs_human()
    _login(client, "viewer@test.co", "password1234")
    assert client.get(f"/archive/jobs/{jid}/live").status_code == 403


def test_click_enqueues_command_for_owner(client):
    jid = _make_needs_human()
    _login(client, "boss@test.co", "bosspass1234")
    client.get(f"/archive/jobs/{jid}/live")   # 클레임
    r = client.post(f"/archive/jobs/{jid}/live/click", data={"x": "100", "y": "200"})
    assert r.status_code == 200
    with db.connect() as conn:
        cmd = conn.execute("SELECT kind, x, y FROM live_commands").fetchone()
    assert (cmd["kind"], cmd["x"], cmd["y"]) == ("click", 100, 200)


def test_input_blocked_for_non_owner_admin(client):
    jid = _make_needs_human()
    _login(client, "boss@test.co", "bosspass1234")
    client.get(f"/archive/jobs/{jid}/live")   # admin1 이 소유
    other = TestClient(web_app.app)
    _login(other, "boss2@test.co", "boss2pass1234")
    r = other.post(f"/archive/jobs/{jid}/live/click", data={"x": "1", "y": "2"})
    assert r.status_code == 403   # 소유자 아닌 admin 은 입력 불가


def test_key_and_cancel(client):
    jid = _make_needs_human()
    _login(client, "boss@test.co", "bosspass1234")
    client.get(f"/archive/jobs/{jid}/live")
    assert client.post(
        f"/archive/jobs/{jid}/live/key", data={"key": "hello", "kind": "text"}
    ).status_code == 200
    r = client.post(f"/archive/jobs/{jid}/live/cancel", follow_redirects=False)
    assert r.status_code == 303
    with db.connect() as conn:
        assert db.get_archive_job(conn, jid)["live_cancel"] == 1


def test_live_state_json(client):
    jid = _make_needs_human()
    _login(client, "boss@test.co", "bosspass1234")
    client.get(f"/archive/jobs/{jid}/live")
    s = client.get(f"/archive/jobs/{jid}/live/state").json()
    assert s["status"] == "needs_human" and s["owned"] is True


def test_live_shot_404_then_served(client):
    jid = _make_needs_human("tok-shot")
    _login(client, "boss@test.co", "bosspass1234")
    assert client.get(f"/archive/jobs/{jid}/live/shot").status_code == 404
    # worker 가 화면을 쓰면 서빙된다
    path = live_challenge.shot_path("tok-shot")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xd8\xff\xe0fakejpeg")
    r = client.get(f"/archive/jobs/{jid}/live/shot")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
