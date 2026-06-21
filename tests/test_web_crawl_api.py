"""크롤 회차 SPA JSON API(/api/web/crawls) — 상세·필터·폴링·취소·재시도·재실행.

SSR /crawls/{id} 와 같은 코어를 재사용하는 JSON 래퍼. 인증 off(loopback)에서
데이터·액션 흐름을, 별도로 archiver 권한 게이트를 검증한다.
"""
import pytest
from fastapi.testclient import TestClient

from chunchugwan import auth, config, crawler, db, storage
from chunchugwan.web import app as web_app

START = "https://example.com/docs/"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "RESOURCES_DIR", tmp_path / "resources")
    monkeypatch.setattr(config, "DOCUMENTS_DIR", tmp_path / "documents")
    monkeypatch.setattr(config, "AUTH_ENABLED", False)
    with db.connect():
        pass
    web_app._active_jobs.clear()
    yield TestClient(web_app.app, headers={"X-Requested-With": "fetch"})
    web_app._active_jobs.clear()


def _crawl() -> dict:
    crawl, _ = crawler.start_crawl(START, source="web")
    return crawl


def _add_failed_page(crawl_id: int, url: str) -> int:
    with db.connect() as conn:
        db.insert_crawl_page(conn, crawl_id, url, 1)
        page = [p for p in db.list_crawl_pages(conn, crawl_id) if p["url"] == url][0]
        db.fail_crawl_page(conn, page["id"], attempts=3, error="boom", next_attempt_at=None)
    return page["id"]


def test_detail_shape(client):
    crawl = _crawl()
    body = client.get(f"/api/web/crawls/{crawl['id']}").json()
    assert body["crawl"]["start_url"] == START
    assert body["counts"]["total"] >= 1
    assert body["can_archive"] is True  # loopback
    assert isinstance(body["retry_backoff_labels"], list)
    assert body["max_attempts"] == len(body["retry_backoff_labels"]) + 1
    assert any(p["url"] == START for p in body["pages"])


def test_detail_status_filter(client):
    crawl = _crawl()
    _add_failed_page(crawl["id"], "https://example.com/docs/fail")
    failed = client.get(f"/api/web/crawls/{crawl['id']}", params={"status": "failed"}).json()
    urls = [p["url"] for p in failed["pages"]]
    assert "https://example.com/docs/fail" in urls and START not in urls
    assert failed["status_filter"] == "failed"
    # 잘못된 값은 전체
    allp = client.get(f"/api/web/crawls/{crawl['id']}", params={"status": "nope"}).json()
    assert allp["status_filter"] == ""


def test_status_poll(client):
    crawl = _crawl()
    s = client.get(f"/api/web/crawls/{crawl['id']}/status").json()
    assert s["status"] == "running" and "counts" in s


def test_cancel(client):
    crawl = _crawl()
    r = client.post(f"/api/web/crawls/{crawl['id']}/cancel")
    assert r.status_code == 200 and r.json() == {"ok": True}
    with db.connect() as conn:
        assert db.get_crawl(conn, crawl["id"])["status"] == "cancelled"


def test_retry_all_failed(client):
    crawl = _crawl()
    cp_id = _add_failed_page(crawl["id"], "https://example.com/docs/fail")
    r = client.post(f"/api/web/crawls/{crawl['id']}/retry")
    assert r.status_code == 200 and r.json() == {"ok": True}
    with db.connect() as conn:
        cp = conn.execute("SELECT status, attempts FROM crawl_pages WHERE id = ?", (cp_id,)).fetchone()
    assert cp["status"] == "pending" and cp["attempts"] == 0


def test_page_retry(client):
    crawl = _crawl()
    cp_id = _add_failed_page(crawl["id"], "https://example.com/docs/fail")
    r = client.post(f"/api/web/crawls/{crawl['id']}/pages/{cp_id}/retry")
    assert r.status_code == 200 and r.json() == {"ok": True}
    with db.connect() as conn:
        assert conn.execute(
            "SELECT status FROM crawl_pages WHERE id = ?", (cp_id,)
        ).fetchone()["status"] == "pending"


def test_page_retry_404_when_not_failed(client):
    crawl = _crawl()
    assert client.post(f"/api/web/crawls/{crawl['id']}/pages/999999/retry").status_code == 404


def test_rerun_returns_new_crawl(client):
    crawl = _crawl()
    with db.connect() as conn:
        db.cancel_crawl(conn, crawl["id"])
        site_id = crawl["site_id"]
    r = client.post(f"/api/web/sites/{site_id}/crawls/{crawl['id']}/rerun")
    assert r.status_code == 200
    body = r.json()
    assert "crawl_id" in body and isinstance(body["merged"], bool)


def test_detail_404(client):
    assert client.get("/api/web/crawls/999999").status_code == 404


def test_archive_req_crawl_options_are_strings():
    """회귀: /archive 는 폼 스타일 all-string 모델 — crawl 옵션은 문자열이어야 한다.

    SPA 의 type=number 입력이 number 로 직렬화되면 422 였다(C2 컷오버 회귀). 프론트가
    문자열로 변환해 보내므로 모델은 str 만 받고, 빈 문자열은 시스템 기본값을 뜻한다.
    """
    from pydantic import ValidationError
    from chunchugwan.web.web_api_routes import ArchiveReq

    ok = ArchiveReq(url="https://example.com/", site=True,
                    crawl_max_pages="5000", crawl_max_depth="10", crawl_delay="1")
    assert ok.crawl_max_pages == "5000"
    assert ArchiveReq(url="https://example.com/").crawl_max_pages == ""  # 미입력 = 기본값
    with pytest.raises(ValidationError):  # number 는 거부
        ArchiveReq(url="https://example.com/", crawl_max_pages=5000)


# ---- 권한 게이트 (AUTH on) ----


def test_action_requires_archiver(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "RESOURCES_DIR", tmp_path / "resources")
    monkeypatch.setattr(config, "DOCUMENTS_DIR", tmp_path / "documents")
    monkeypatch.setattr(config, "AUTH_ENABLED", True)
    with db.connect() as conn:
        db.create_first_admin(conn, "boss@test.co", auth.hash_password("bosspass1234"))
        db.create_user(conn, "viewer@test.co", auth.hash_password("password1234"), role="viewer")
    web_app._active_jobs.clear()
    c = TestClient(web_app.app, headers={"X-Requested-With": "fetch"})
    crawl, _ = crawler.start_crawl(START, source="web")
    c.post("/api/web/auth/login", json={"email": "viewer@test.co", "password": "password1234"})
    # viewer 는 상세는 보지만 액션(취소)은 막힌다
    assert c.get(f"/api/web/crawls/{crawl['id']}").json()["can_archive"] is False
    assert c.post(f"/api/web/crawls/{crawl['id']}/cancel").status_code == 403
    web_app._active_jobs.clear()
