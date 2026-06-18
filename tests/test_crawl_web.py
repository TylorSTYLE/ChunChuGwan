"""사이트 전체 아카이브 대시보드 라우트 테스트 — 캡처 없이 fixture 데이터로 검증."""
import pytest
from fastapi.testclient import TestClient

from chunchugwan import config, crawler, db, storage
from chunchugwan.web import app as web_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    """임시 아카이브 위의 TestClient (인증 off — 인증은 test_auth.py 에서 검증)."""
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "RESOURCES_DIR", tmp_path / "resources")
    monkeypatch.setattr(config, "DOCUMENTS_DIR", tmp_path / "documents")
    monkeypatch.setattr(config, "AUTH_ENABLED", False)
    with db.connect():
        pass  # 스키마 생성
    web_app._active_jobs.clear()
    yield TestClient(web_app.app)
    web_app._active_jobs.clear()


def make_snapshot(url: str) -> tuple[int, int]:
    """페이지 + 스냅샷 한 쌍 생성 후 (page_id, snapshot_id) 반환."""
    with db.connect() as conn:
        page_id = db.get_or_create_page(
            conn, url, "example.com", storage.url_to_slug(url)
        )
        snap_id = db.insert_snapshot(
            conn, page_id, taken_at="2026-06-01T00:00:00+00:00",
            dir_name="2026-06-01T00-00-00", content_hash="0" * 64,
            final_url=url, http_status=200, changed=1,
        )
    return page_id, snap_id


def _add_failed_page(crawl_id: int, url: str) -> int:
    """크롤에 실패(failed) 페이지 한 줄 추가 후 crawl_page id 반환."""
    with db.connect() as conn:
        db.insert_crawl_page(conn, crawl_id, url, 1)
        page = [p for p in db.list_crawl_pages(conn, crawl_id) if p["url"] == url][0]
        db.fail_crawl_page(
            conn, page["id"], attempts=3, error="boom", next_attempt_at=None
        )
    return page["id"]


def test_goto_redirects_to_crawl_snapshot(client):
    url = "https://example.com/docs/a"
    _, snap_id = make_snapshot(url)
    crawl, _ = crawler.start_crawl("https://example.com/docs/", source="web")
    with db.connect() as conn:
        db.insert_crawl_page(conn, crawl["id"], url, 1)
        page = [p for p in db.list_crawl_pages(conn, crawl["id"]) if p["url"] == url][0]
        db.finish_crawl_page(conn, page["id"], snap_id)
    res = client.get(
        f"/crawl/{crawl['id']}/goto", params={"url": url}, follow_redirects=False
    )
    assert res.status_code == 302
    assert res.headers["location"] == f"/snapshot/{snap_id}"


def test_goto_falls_back_to_latest_snapshot(client):
    """크롤 세트에 없는 URL 은 해당 URL 의 최신 스냅샷으로 폴백한다."""
    url = "https://example.com/elsewhere"
    _, snap_id = make_snapshot(url)
    crawl, _ = crawler.start_crawl("https://example.com/docs/", source="web")
    res = client.get(
        f"/crawl/{crawl['id']}/goto", params={"url": url}, follow_redirects=False
    )
    assert res.status_code == 302
    assert res.headers["location"] == f"/snapshot/{snap_id}"


def test_goto_missing_shows_original_link(client):
    crawl, _ = crawler.start_crawl("https://example.com/docs/", source="web")
    res = client.get(
        f"/crawl/{crawl['id']}/goto",
        params={"url": "https://example.com/docs/none"},
    )
    assert res.status_code == 404
    assert "아카이브에 없는 페이지" in res.text
    assert 'href="https://example.com/docs/none"' in res.text


def test_post_archive_site_without_interval_has_no_schedule(client):
    client.post(
        "/archive", data={"url": "example.com/docs/", "site": "on", "interval": "0"},
        follow_redirects=False,
    )
    with db.connect() as conn:
        assert db.list_crawl_schedules(conn) == []


def test_goto_normalizes_url(client):
    """리졸버는 정규화된 URL 로 조회한다 (트래킹 파라미터 제거 등)."""
    url = "https://example.com/docs/a"
    _, snap_id = make_snapshot(url)
    crawl, _ = crawler.start_crawl("https://example.com/docs/", source="web")
    with db.connect() as conn:
        db.insert_crawl_page(conn, crawl["id"], url, 1)
        page = [p for p in db.list_crawl_pages(conn, crawl["id"]) if p["url"] == url][0]
        db.finish_crawl_page(conn, page["id"], snap_id)
    res = client.get(
        f"/crawl/{crawl['id']}/goto",
        params={"url": "https://example.com/docs/a?utm_source=x"},
        follow_redirects=False,
    )
    assert res.status_code == 302
    assert res.headers["location"] == f"/snapshot/{snap_id}"
