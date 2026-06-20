"""휴지통 숨김 표면(보안) + 엔드포인트 가드 — 휴지통 페이지의 스냅샷이 어떤
읽기/서빙 경로로도 새지 않고, 휴지통 관리 권한 없는 계정은 접근이 막히는지 검증."""
import pytest
from fastapi.testclient import TestClient

from chunchugwan import auth, config, db, deletion, searchindex, storage
from chunchugwan.web import app as web_app

DOMAIN = "example.com"
URL = "https://example.com/post"
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


def make_user(email="a@a.co", role="admin"):
    with db.connect() as conn:
        uid = db.create_user(conn, email, auth.hash_password("userpass123"), role=role)
        token = auth.issue_session(conn, uid)
    return uid, token


def client_for(token):
    c = TestClient(web_app.app)
    c.cookies.set(config.SESSION_COOKIE, token)
    return c


def seed():
    """검색 색인된 스냅샷 1개를 가진 페이지 → (page_id, snapshot_id)."""
    slug = storage.url_to_slug(URL)
    with db.connect() as conn:
        pid = db.get_or_create_page(conn, URL, DOMAIN, slug)
        d = storage.page_dir(DOMAIN, slug) / "2026-06-01T00-00-00"
        d.mkdir(parents=True)
        (d / "content.md").write_text("검색대상 본문입니다", encoding="utf-8")
        sid = db.insert_snapshot(
            conn, pid, taken_at="2026-06-01T00:00:00+00:00",
            dir_name="2026-06-01T00-00-00", content_hash="h",
            final_url=URL, http_status=200, changed=1,
        )
        db.backfill_snapshot_bytes(conn)
        searchindex.index_snapshot(conn, sid)
        site_id = db.get_site_by_key(conn, DOMAIN)["id"]
    return pid, sid, site_id


# ---- 숨김 표면: 휴지통 페이지의 스냅샷은 어디서도 보이지 않는다 ----


def test_trashed_page_hidden_from_sites_and_detail(tmp_db):
    pid, _, site_id = seed()
    _, token = make_user()
    cl = client_for(token)
    assert len(cl.get("/api/web/sites").json()["items"]) == 1
    deletion.delete_page(pid)
    assert cl.get("/api/web/sites").json()["items"] == []  # 목록에서 사라짐
    # 사이트 상세도 (사이트는 비었으므로) 페이지가 0
    detail = cl.get(f"/api/web/sites/{site_id}")
    assert detail.status_code in (200, 404)
    if detail.status_code == 200:
        assert detail.json()["page_count"] == 0


def test_trashed_page_timeline_404(tmp_db):
    pid, _, _ = seed()
    _, token = make_user()
    cl = client_for(token)
    assert cl.get(f"/api/web/pages/{pid}").status_code == 200
    deletion.delete_page(pid)
    assert cl.get(f"/api/web/pages/{pid}").status_code == 404


def test_trashed_snapshot_meta_and_file_404(tmp_db):
    pid, sid, _ = seed()
    _, token = make_user()
    cl = client_for(token)
    assert cl.get(f"/api/web/snapshots/{sid}").status_code == 200
    assert cl.get(f"/snapshot/{sid}/file/content.md").status_code == 200
    deletion.delete_page(pid)
    # 뷰어 메타·원본 파일 모두 404 — 휴지통 스냅샷은 열람·다운로드 불가
    assert cl.get(f"/api/web/snapshots/{sid}").status_code == 404
    assert cl.get(f"/snapshot/{sid}/file/content.md").status_code == 404


def test_trashed_page_excluded_from_search(tmp_db):
    pid, _, _ = seed()
    assert searchindex.search("검색대상").total == 1
    deletion.delete_page(pid)
    assert searchindex.search("검색대상").total == 0  # 검색에서 제외


def test_search_returns_after_restore(tmp_db):
    pid, _, _ = seed()
    deletion.delete_page(pid)
    with db.connect() as conn:
        tid = db.list_trash_entries(conn)[0]["id"]
    deletion.restore(tid)
    assert searchindex.search("검색대상").total == 1  # 복원하면 다시 검색됨


# ---- 엔드포인트 가드 ----


def test_trash_endpoints_require_manage_trash(tmp_db):
    pid, _, _ = seed()
    _, admin = make_user("admin@a.co", "admin")
    _, viewer = make_user("viewer@a.co", "viewer")
    a, v = client_for(admin), client_for(viewer)
    # 삭제(권한 delete) — admin 으로 휴지통에 넣는다
    r = a.post(f"/api/web/pages/{pid}/delete", headers=POST_HEADERS)
    assert r.status_code == 200 and r.json()["trashed"] is True
    with db.connect() as conn:
        tid = db.list_trash_entries(conn)[0]["id"]
    # viewer 는 휴지통 열람·복원·영구삭제 모두 403
    assert v.get("/api/web/trash").status_code == 403
    assert v.post(f"/api/web/trash/{tid}/restore", headers=POST_HEADERS).status_code == 403
    assert v.post(f"/api/web/trash/{tid}/purge", headers=POST_HEADERS).status_code == 403
    # admin 은 목록·복원 가능
    assert len(a.get("/api/web/trash").json()["entries"]) == 1
    assert a.post(f"/api/web/trash/{tid}/restore", headers=POST_HEADERS).status_code == 200


def test_trash_settings_round_trip(tmp_db):
    _, admin = make_user("admin@a.co", "admin")
    a = client_for(admin)
    ov = a.get("/api/web/system").json()
    assert ov["trash_enabled"] is True and ov["trash_retention_days"] == 30
    r = a.post(
        "/api/web/system/trash-settings",
        json={"trash_enabled": False, "trash_retention_days": 7},
        headers=POST_HEADERS,
    )
    assert r.status_code == 200
    ov = a.get("/api/web/system").json()
    assert ov["trash_enabled"] is False and ov["trash_retention_days"] == 7
    # 범위 밖은 400
    assert a.post(
        "/api/web/system/trash-settings",
        json={"trash_enabled": True, "trash_retention_days": 9999},
        headers=POST_HEADERS,
    ).status_code == 400
