"""로그인 자격증명으로 캡처된 스냅샷의 접근 제한 — 소유자/관리자만 열람."""
import pytest
from fastapi.testclient import TestClient

from chunchugwan import auth, config, db, storage
from chunchugwan.web import app as web_app

URL = "https://example.com/secret"


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "RESOURCES_DIR", tmp_path / "resources")
    monkeypatch.setattr(config, "DOCUMENTS_DIR", tmp_path / "documents")


def _seed():
    with db.connect() as conn:
        db.create_first_admin(conn, "boss@test.co", auth.hash_password("bosspass1234"))
        db.create_user(conn, "arch@test.co", auth.hash_password("password1234"), role="archiver")
        db.create_user(conn, "arch2@test.co", auth.hash_password("password1234"), role="archiver")
        db.create_user(conn, "viewer@test.co", auth.hash_password("password1234"), role="viewer")


@pytest.fixture
def client(tmp_db):
    _seed()
    web_app._active_jobs.clear()
    yield TestClient(web_app.app)
    web_app._active_jobs.clear()


def _uid(email):
    with db.connect() as conn:
        return db.get_user_by_email(conn, email)["id"]


def _ext_token(email):
    uid = _uid(email)
    with db.connect() as conn:
        return auth.issue_api_key(conn, "ext", can_view=True, can_archive=True,
                                  created_by=uid, owner_user_id=uid, ttl_seconds=None)


def _system_token():
    with db.connect() as conn:
        return auth.issue_api_key(conn, "sys", can_view=True, can_archive=True,
                                  created_by=1, ttl_seconds=None)


def _headers(token):
    return {"Authorization": f"Bearer {token}"}


def _login(client, email, pw="password1234"):
    client.post("/login", data={"email": email, "password": pw}, follow_redirects=False)


def _insert_auth_snapshots(owner_id, specs):
    """specs: [(dir_suffix, content_hash, body, authenticated, authenticated_by)] → page_id."""
    domain, slug = "example.com", storage.url_to_slug(URL)
    with db.connect() as conn:
        page_id = db.get_or_create_page(conn, URL, domain, slug)
        for i, (chash, body, authed, by) in enumerate(specs):
            d = f"2026-06-0{i + 1}T00-00-00"
            snap_dir = storage.page_dir(domain, slug) / d
            snap_dir.mkdir(parents=True)
            (snap_dir / "content.md").write_text(body, encoding="utf-8")
            db.insert_snapshot(
                conn, page_id, taken_at=f"2026-06-0{i + 1}T00:00:00+00:00",
                dir_name=d, content_hash=chash, final_url=URL, http_status=200,
                changed=1, authenticated=authed, authenticated_by=by,
            )
    return page_id


# ---- _may_view_authenticated 로직 ----


def test_may_view_authenticated_logic(client):
    owner = _uid("arch@test.co")
    _insert_auth_snapshots(owner, [("h", "secret", 1, owner)])
    with db.connect() as conn:
        snap = db.get_snapshot(conn, 1)

    class _Req:
        def __init__(self, user=None, api_key=None):
            self.state = type("S", (), {"user": user, "api_key": api_key})()

    admin = {"role": "admin", "id": _uid("boss@test.co")}
    owner_user = {"role": "archiver", "id": owner}
    other = {"role": "archiver", "id": _uid("arch2@test.co")}
    assert web_app._may_view_authenticated(_Req(user=admin), snap) is True
    assert web_app._may_view_authenticated(_Req(user=owner_user), snap) is True
    assert web_app._may_view_authenticated(_Req(user=other), snap) is False
    assert web_app._may_view_authenticated(
        _Req(api_key={"owner_user_id": owner}), snap) is True
    assert web_app._may_view_authenticated(
        _Req(api_key={"owner_user_id": None}), snap) is False


# ---- API 접근 제한 ----


def test_api_authenticated_snapshot_owner_only(client):
    owner = _uid("arch@test.co")
    page_id = _insert_auth_snapshots(owner, [("hsecret", "일급비밀", 1, owner)])
    owner_tok, other_tok, sys_tok = (
        _ext_token("arch@test.co"), _ext_token("arch2@test.co"), _system_token()
    )
    # 메타데이터: 소유자만
    assert client.get("/api/v1/snapshots/1", headers=_headers(owner_tok)).status_code == 200
    assert client.get("/api/v1/snapshots/1", headers=_headers(other_tok)).status_code == 404
    assert client.get("/api/v1/snapshots/1", headers=_headers(sys_tok)).status_code == 404
    # 파일(content): 소유자만 (_load_snapshot 가드)
    r = client.get("/api/v1/snapshots/1/file/content.md", headers=_headers(owner_tok))
    assert r.status_code == 200 and "일급비밀" in r.text
    assert client.get(
        "/api/v1/snapshots/1/file/content.md", headers=_headers(other_tok)
    ).status_code == 404
    # 페이지 히스토리: 비소유자에겐 인증 스냅샷이 빠진다
    owner_snaps = client.get(f"/api/v1/pages/{page_id}", headers=_headers(owner_tok)).json()["snapshots"]
    other_snaps = client.get(f"/api/v1/pages/{page_id}", headers=_headers(other_tok)).json()["snapshots"]
    assert len(owner_snaps) == 1 and other_snaps == []


def test_regular_snapshot_unrestricted(client):
    owner = _uid("arch@test.co")
    _insert_auth_snapshots(owner, [("pub", "공개", 0, None)])  # authenticated=0
    other_tok = _ext_token("arch2@test.co")
    assert client.get("/api/v1/snapshots/1", headers=_headers(other_tok)).status_code == 200


# ---- 웹 뷰어·diff·타임라인 접근 제한 ----


def test_web_authenticated_snapshot_denied_to_other(client):
    owner = _uid("arch@test.co")
    _insert_auth_snapshots(owner, [("h", "secret", 1, owner)])
    _login(client, "viewer@test.co")
    assert client.get("/snapshot/1").status_code == 404  # 가드가 렌더 전에 차단


def test_web_diff_blocks_authenticated(client):
    owner = _uid("arch@test.co")
    page_id = _insert_auth_snapshots(owner, [
        ("h1", "비밀1", 1, owner), ("h2", "비밀2", 1, owner),
    ])
    _login(client, "viewer@test.co")
    assert client.get(f"/diff/{page_id}").status_code == 404
    _login(client, "arch@test.co")  # 소유자
    assert client.get(f"/diff/{page_id}").status_code == 200


def test_web_timeline_hides_authenticated(client):
    owner = _uid("arch@test.co")
    page_id = _insert_auth_snapshots(owner, [
        ("publichash01", "공개", 0, None), ("secrethash99", "비밀", 1, owner),
    ])
    _login(client, "viewer@test.co")
    body = client.get(f"/page/{page_id}").text
    assert "publichash01" in body and "secrethash99" not in body
    _login(client, "arch@test.co")
    assert "secrethash99" in client.get(f"/page/{page_id}").text


# ---- 사용자 삭제 FK ----


def test_delete_user_nulls_authenticated_by(client):
    owner = _uid("arch@test.co")
    page_id = _insert_auth_snapshots(owner, [("h", "secret", 1, owner)])
    with db.connect() as conn:
        db.delete_user(conn, owner)  # FK 위반 없이 성공
        snap = db.list_snapshots(conn, page_id)[0]
        assert snap["authenticated"] == 1 and snap["authenticated_by"] is None
