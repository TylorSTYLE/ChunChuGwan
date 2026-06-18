"""SvelteKit SPA 백업·복원·내보내기·가져오기 API(/api/web/system/*) 테스트.

Phase C2 컷오버를 위해 SSR system_routes 의 backup/export(다운로드)·restore/import
(업로드)를 /api/web 으로 보강한 엔드포인트를 검증한다. SSR test_backup·test_system 의
단정(확장자 검증·round-trip·권한)에 대응한다.
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


def make_user(email="admin@test.co", password="adminpass123", role="admin"):
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
    _, token = make_user()
    return client_for(token)


def seed_page():
    url = "https://example.com/p"
    with db.connect() as conn:
        page_id = db.get_or_create_page(conn, url, "example.com", storage.url_to_slug(url))
        d = storage.page_dir("example.com", storage.url_to_slug(url)) / "2026-06-01T00-00-00"
        d.mkdir(parents=True)
        (d / "content.md").write_text("본문", encoding="utf-8")
        db.insert_snapshot(conn, page_id, taken_at="2026-06-01T00:00:00+00:00",
                           dir_name="2026-06-01T00-00-00", content_hash="h1",
                           final_url=url, http_status=200, changed=1)


def upload(c, path, filename, content, **extra):
    return c.post(path, files={"file": (filename, content, "application/gzip")},
                  headers=POST_HEADERS, **extra)


# ---- 권한 게이트 ----


def test_backup_requires_manage_system(tmp_db):
    _, arch = make_user(email="arch@test.co", role="archiver")
    assert client_for().post("/api/web/system/backup", headers=POST_HEADERS).status_code == 401
    assert client_for(arch).post("/api/web/system/backup", headers=POST_HEADERS).status_code == 403


# ---- 다운로드 ----


def test_backup_download(tmp_db):
    seed_page()
    r = admin_client().post("/api/web/system/backup", headers=POST_HEADERS)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/gzip"
    assert r.content  # tar.gz 바이트


def test_export_download(tmp_db):
    seed_page()
    r = admin_client().post("/api/web/system/export", headers=POST_HEADERS)
    assert r.status_code == 200
    assert r.content


# ---- round-trip ----


def test_backup_restore_roundtrip(tmp_db):
    seed_page()
    c = admin_client()
    data = c.post("/api/web/system/backup", headers=POST_HEADERS).content
    r = upload(c, "/api/web/system/restore", "x.ccg.backup", data)
    assert r.status_code == 200
    assert "manifest" in r.json()


def test_export_import_roundtrip(tmp_db):
    seed_page()
    c = admin_client()
    data = c.post("/api/web/system/export", headers=POST_HEADERS).content
    r = upload(c, "/api/web/system/import", "x.ccg.export", data, data={"mode": "merge"})
    assert r.status_code == 200
    assert "added" in r.json()


# ---- 확장자·모드 검증 ----


def test_restore_rejects_bad_extension(tmp_db):
    r = upload(admin_client(), "/api/web/system/restore", "evil.txt", b"junk")
    assert r.status_code == 400


def test_import_rejects_bad_extension(tmp_db):
    r = upload(admin_client(), "/api/web/system/import", "evil.txt", b"junk", data={"mode": "merge"})
    assert r.status_code == 400


def test_import_rejects_bad_mode(tmp_db):
    r = upload(admin_client(), "/api/web/system/import", "x.ccg.export", b"x", data={"mode": "bogus"})
    assert r.status_code == 400
