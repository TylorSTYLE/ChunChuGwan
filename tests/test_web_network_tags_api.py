"""SvelteKit SPA 네트워크 태그 API(/api/web/system/network-tags) 테스트.

Phase C2 컷오버를 위해 SSR system_routes 의 네트워크 태그 관리(create/delete/merge)를
/api/web 으로 보강한 엔드포인트를 검증한다. SSR test_network_tags 의 단정에 대응한다.
"""
import pytest
from fastapi.testclient import TestClient

from chunchugwan import auth, config, db, storage
from chunchugwan.web import app as web_app

POST_HEADERS = {"X-Requested-With": "fetch", "Origin": "http://testserver"}


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "RESOURCES_DIR", tmp_path / "resources")
    monkeypatch.setattr(config, "DOCUMENTS_DIR", tmp_path / "documents")
    monkeypatch.setattr(config, "AUTH_ENABLED", True)
    web_app._active_jobs.clear()
    yield
    web_app._active_jobs.clear()


def make_user(email, password="userpass123", role="archiver"):
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


def admin_client():
    _, token = make_user("admin@test.co", role="admin")
    return client_for(token)


def make_tag(name, description=""):
    with db.connect() as conn:
        db.create_network_tag(conn, name, description)
        return db.get_network_tag_by_name(conn, name)["id"]


def attach_tag_to_new_page(tag_id, url):
    """페이지를 만들어 네트워크 태그에 연결(참조 발생)."""
    domain, slug = url.split("//", 1)[1].split("/", 1)[0], storage.url_to_slug(url)
    with db.connect() as conn:
        page_id = db.get_or_create_page(conn, url, domain, slug)
        conn.execute("UPDATE pages SET network_tag_id=? WHERE id=?", (tag_id, page_id))


# ---- 권한 게이트 ----


def test_network_tags_require_manage_system(tmp_db):
    _, arch = make_user("arch@test.co", role="archiver")
    assert client_for().post("/api/web/system/network-tags",
                             json={"name": "x"}, headers=POST_HEADERS).status_code == 401
    assert client_for(arch).post("/api/web/system/network-tags",
                                 json={"name": "x"}, headers=POST_HEADERS).status_code == 403


# ---- 생성 ----


def test_network_tag_create_and_list(tmp_db):
    c = admin_client()
    r = c.post("/api/web/system/network-tags",
               json={"name": "사무실 NAS", "description": "10.0.0.0/24"}, headers=POST_HEADERS)
    assert r.status_code == 200
    tags = c.get("/api/web/system").json()["network_tags"]
    assert any(t["name"] == "사무실 NAS" for t in tags)


def test_network_tag_create_empty_name(tmp_db):
    r = admin_client().post("/api/web/system/network-tags",
                            json={"name": "   "}, headers=POST_HEADERS)
    assert r.status_code == 400


def test_network_tag_create_duplicate(tmp_db):
    c = admin_client()
    c.post("/api/web/system/network-tags", json={"name": "dup"}, headers=POST_HEADERS)
    r = c.post("/api/web/system/network-tags", json={"name": "dup"}, headers=POST_HEADERS)
    assert r.status_code == 400


def test_network_tag_create_name_too_long(tmp_db):
    r = admin_client().post("/api/web/system/network-tags",
                            json={"name": "가" * 61}, headers=POST_HEADERS)
    assert r.status_code == 400


# ---- 삭제 ----


def test_network_tag_delete(tmp_db):
    tag_id = make_tag("temp")
    r = admin_client().post(f"/api/web/system/network-tags/{tag_id}/delete", headers=POST_HEADERS)
    assert r.status_code == 200
    with db.connect() as conn:
        assert db.get_network_tag(conn, tag_id) is None


def test_network_tag_delete_404(tmp_db):
    r = admin_client().post("/api/web/system/network-tags/ghost/delete", headers=POST_HEADERS)
    assert r.status_code == 404


def test_network_tag_delete_in_use(tmp_db):
    tag_id = make_tag("used")
    attach_tag_to_new_page(tag_id, "https://10.0.0.5/x")
    r = admin_client().post(f"/api/web/system/network-tags/{tag_id}/delete", headers=POST_HEADERS)
    assert r.status_code == 400


# ---- 병합 ----


def test_network_tag_merge_same(tmp_db):
    tag_id = make_tag("a")
    r = admin_client().post("/api/web/system/network-tags/merge",
                            json={"source": tag_id, "target": tag_id}, headers=POST_HEADERS)
    assert r.status_code == 400


def test_network_tag_merge_missing(tmp_db):
    tag_id = make_tag("a")
    r = admin_client().post("/api/web/system/network-tags/merge",
                            json={"source": tag_id, "target": "ghost"}, headers=POST_HEADERS)
    assert r.status_code == 404


def test_network_tag_merge_unreferenced(tmp_db):
    src, tgt = make_tag("src"), make_tag("tgt")
    r = admin_client().post("/api/web/system/network-tags/merge",
                            json={"source": src, "target": tgt}, headers=POST_HEADERS)
    assert r.status_code == 400  # 참조 없는 태그는 병합 불가
