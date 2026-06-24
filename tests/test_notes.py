"""페이지·사이트 메모(notes) 테스트 — DB 계층·정리·웹 API 권한 게이트."""
import pytest
from fastapi.testclient import TestClient

from chunchugwan import auth, config, db
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


def make_user(email, role="archiver", password="userpass123"):
    with db.connect() as conn:
        pw = auth.hash_password(password)
        uid = db.create_user(conn, email, pw, role=role)
        token = auth.issue_session(conn, uid)
    return uid, token


def client_for(token=None):
    c = TestClient(web_app.app)
    if token:
        c.cookies.set(config.SESSION_COOKIE, token)
    return c


def make_page(url="https://example.com/a"):
    with db.connect() as conn:
        return db.get_or_create_page(conn, url, "example.com", "a")


# ---- DB 계층 ----


def test_add_list_get_delete_note(tmp_db):
    page_id = make_page()
    with db.connect() as conn:
        n1 = db.add_note(conn, "page", page_id, "첫 메모",
                         author_user_id=1, author_label="tylor")
        n2 = db.add_note(conn, "page", page_id, "둘째 메모",
                         author_user_id=2, author_label="kim")
    with db.connect() as conn:
        notes = db.list_notes(conn, "page", page_id)
        assert [n["content"] for n in notes] == ["첫 메모", "둘째 메모"]  # 오래된 순
        assert notes[0]["author_label"] == "tylor"
        assert db.get_note(conn, n1)["content"] == "첫 메모"
    with db.connect() as conn:
        assert db.delete_note(conn, n1) is True
    with db.connect() as conn:
        remaining = db.list_notes(conn, "page", page_id)
        assert [n["id"] for n in remaining] == [n2]


def test_notes_scoped_by_kind(tmp_db):
    page_id = make_page()
    with db.connect() as conn:
        site_id = db.get_page_by_id(conn, page_id)["site_id"]
        db.add_note(conn, "page", page_id, "page memo",
                    author_user_id=None, author_label="x")
        db.add_note(conn, "site", site_id, "site memo",
                    author_user_id=None, author_label="x")
    with db.connect() as conn:
        assert len(db.list_notes(conn, "page", page_id)) == 1
        assert len(db.list_notes(conn, "site", site_id)) == 1


def test_delete_page_cleans_notes(tmp_db):
    page_id = make_page()
    with db.connect() as conn:
        db.add_note(conn, "page", page_id, "메모", author_user_id=None, author_label="x")
        db.delete_page(conn, page_id)
    with db.connect() as conn:
        assert db.list_notes(conn, "page", page_id) == []


# ---- 웹 API 권한 게이트 ----


def test_viewer_sees_notes_but_cannot_create(tmp_db):
    page_id = make_page()
    with db.connect() as conn:
        db.add_note(conn, "page", page_id, "보기용 메모",
                    author_user_id=None, author_label="시스템")
    _, vtoken = make_user("v@test.co", role="viewer")
    c = client_for(vtoken)
    body = c.get(f"/api/web/pages/{page_id}").json()
    assert body["can_memo_view"] is True
    assert body["can_memo_create"] is False
    assert [n["content"] for n in body["notes"]] == ["보기용 메모"]
    # viewer 는 등록 불가 (403)
    r = c.post(f"/api/web/pages/{page_id}/notes",
               json={"content": "x"}, headers=POST_HEADERS)
    assert r.status_code == 403


def test_archiver_creates_but_cannot_delete(tmp_db):
    page_id = make_page()
    _, atoken = make_user("a@test.co", role="archiver")
    c = client_for(atoken)
    r = c.post(f"/api/web/pages/{page_id}/notes",
               json={"content": "아카이버 메모"}, headers=POST_HEADERS)
    assert r.status_code == 201
    note_id = r.json()["id"]
    with db.connect() as conn:
        note = db.get_note(conn, note_id)
        assert note["content"] == "아카이버 메모"
        assert note["author_label"] == "a@test.co"  # display_name 없으면 email
    # 삭제는 권한 없음 (403)
    assert c.request("DELETE", f"/api/web/notes/{note_id}",
                     headers=POST_HEADERS).status_code == 403


def test_admin_deletes_note(tmp_db):
    page_id = make_page()
    with db.connect() as conn:
        nid = db.add_note(conn, "page", page_id, "지울 메모",
                          author_user_id=None, author_label="x")
    _, token = make_user("admin@test.co", role="admin")
    c = client_for(token)
    assert c.request("DELETE", f"/api/web/notes/{nid}",
                     headers=POST_HEADERS).status_code == 204
    with db.connect() as conn:
        assert db.get_note(conn, nid) is None


def test_empty_note_rejected(tmp_db):
    page_id = make_page()
    _, token = make_user("a@test.co", role="archiver")
    c = client_for(token)
    r = c.post(f"/api/web/pages/{page_id}/notes",
               json={"content": "   "}, headers=POST_HEADERS)
    assert r.status_code == 400


def test_site_note_via_api(tmp_db):
    page_id = make_page()
    with db.connect() as conn:
        site_id = db.get_page_by_id(conn, page_id)["site_id"]
    _, token = make_user("a@test.co", role="archiver")
    c = client_for(token)
    r = c.post(f"/api/web/sites/{site_id}/notes",
               json={"content": "사이트 메모"}, headers=POST_HEADERS)
    assert r.status_code == 201
    body = c.get(f"/api/web/sites/{site_id}").json()
    assert [n["content"] for n in body["notes"]] == ["사이트 메모"]
