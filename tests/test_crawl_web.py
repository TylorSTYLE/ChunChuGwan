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


def test_crawls_list_empty(client):
    res = client.get("/crawls")
    assert res.status_code == 200
    assert "아직 사이트 전체 아카이브가 없습니다." in res.text


def test_archive_form_has_site_option(client):
    res = client.get("/archive/new")
    assert res.status_code == 200
    assert 'name="site"' in res.text
    assert 'name="crawl_max_pages"' in res.text
    assert 'name="crawl_delay"' in res.text


def test_post_archive_site_creates_crawl(client):
    res = client.post(
        "/archive",
        data={
            "url": "example.com/docs/", "site": "on",
            "crawl_max_pages": "30", "crawl_max_depth": "2", "crawl_delay": "10",
        },
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert res.headers["location"] == "/crawls/1"
    with db.connect() as conn:
        crawl = db.get_crawl(conn, 1)
        pages = db.list_crawl_pages(conn, 1)
    assert crawl["start_url"] == "https://example.com/docs/"
    assert crawl["max_pages"] == 30 and crawl["max_depth"] == 2
    assert crawl["delay_seconds"] == 10 and crawl["source"] == "web"
    assert [p["url"] for p in pages] == ["https://example.com/docs/"]


def test_post_archive_site_rejects_bad_options(client):
    res = client.post(
        "/archive",
        data={"url": "example.com/docs/", "site": "on", "crawl_max_pages": "0"},
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert res.headers["location"].startswith("/archive/new?")
    with db.connect() as conn:
        assert db.list_crawls(conn) == []


def test_crawl_detail_and_status(client):
    crawl = crawler.start_crawl("https://example.com/docs/", source="web")
    res = client.get(f"/crawls/{crawl['id']}")
    assert res.status_code == 200
    assert "https://example.com/docs/" in res.text
    assert "example.com/docs/" in res.text  # 범위 표기

    status = client.get(f"/crawls/{crawl['id']}/status").json()
    assert status["status"] == "running"
    assert status["counts"]["pending"] == 1 and status["counts"]["total"] == 1

    assert client.get("/crawls/999").status_code == 404


def test_crawl_cancel_and_retry(client):
    crawl = crawler.start_crawl("https://example.com/docs/", source="web")
    res = client.post(f"/crawls/{crawl['id']}/cancel", follow_redirects=False)
    assert res.status_code == 303
    with db.connect() as conn:
        assert db.get_crawl(conn, crawl["id"])["status"] == "cancelled"
        conn.execute(
            "UPDATE crawl_pages SET status = 'failed', error = 'x' WHERE crawl_id = ?",
            (crawl["id"],),
        )
    res = client.post(f"/crawls/{crawl['id']}/retry", follow_redirects=False)
    assert res.status_code == 303
    with db.connect() as conn:
        assert db.get_crawl(conn, crawl["id"])["status"] == "running"
        assert db.list_crawl_pages(conn, crawl["id"])[0]["status"] == "pending"


def test_goto_redirects_to_crawl_snapshot(client):
    url = "https://example.com/docs/a"
    _, snap_id = make_snapshot(url)
    crawl = crawler.start_crawl("https://example.com/docs/", source="web")
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
    crawl = crawler.start_crawl("https://example.com/docs/", source="web")
    res = client.get(
        f"/crawl/{crawl['id']}/goto", params={"url": url}, follow_redirects=False
    )
    assert res.status_code == 302
    assert res.headers["location"] == f"/snapshot/{snap_id}"


def test_goto_missing_shows_original_link(client):
    crawl = crawler.start_crawl("https://example.com/docs/", source="web")
    res = client.get(
        f"/crawl/{crawl['id']}/goto",
        params={"url": "https://example.com/docs/none"},
    )
    assert res.status_code == 404
    assert "아카이브에 없는 페이지" in res.text
    assert 'href="https://example.com/docs/none"' in res.text


def test_goto_normalizes_url(client):
    """리졸버는 정규화된 URL 로 조회한다 (트래킹 파라미터 제거 등)."""
    url = "https://example.com/docs/a"
    _, snap_id = make_snapshot(url)
    crawl = crawler.start_crawl("https://example.com/docs/", source="web")
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
