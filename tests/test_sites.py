"""사이트(서브도메인 단위) 논리 모델 — 키 계산, 자동 소속, 백필, 삭제.

모든 페이지·크롤·크롤 스케줄은 사이트에 속한다. www 와 apex 는 같은
사이트, 다른 서브도메인·포트는 다른 사이트다 (CLAUDE.md 저장 구조 참조).
"""
import pytest

from chunchugwan import config, crawler, db, deletion, storage


@pytest.fixture
def archive_env(tmp_path, monkeypatch):
    """임시 아카이브 루트."""
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "RULES_PATH", tmp_path / "rules.json")
    monkeypatch.setattr(config, "RESOURCES_DIR", tmp_path / "resources")
    monkeypatch.setattr(config, "DOCUMENTS_DIR", tmp_path / "documents")
    return tmp_path


def _add_page(conn, url: str) -> int:
    norm = storage.normalize_url(url)
    from urllib.parse import urlsplit

    return db.get_or_create_page(
        conn, norm, urlsplit(norm).hostname or "", storage.url_to_slug(norm)
    )


# ---- 사이트 키 계산 ----


def test_site_key_strips_www():
    assert storage.site_key("https://www.example.com/a") == "example.com"
    assert storage.site_key("https://example.com/a") == "example.com"


def test_site_key_keeps_subdomain_and_port():
    assert storage.site_key("https://blog.example.com/") == "blog.example.com"
    assert storage.site_key("https://example.com:8443/") == "example.com:8443"
    assert storage.site_key("https://www.example.com:8443/") == "example.com:8443"


def test_site_key_ip_and_www_domain():
    # IP 호스트는 그대로, www 자체가 등록 도메인인 호스트는 제거하지 않는다
    assert storage.site_key("http://192.168.0.5:8080/x") == "192.168.0.5:8080"
    assert storage.site_key("https://www.com/") == "www.com"


def test_netloc_site_key():
    assert storage.netloc_site_key("www.example.com") == "example.com"
    assert storage.netloc_site_key("www.example.com:8080") == "example.com:8080"
    assert storage.netloc_site_key("example.com") == "example.com"


# ---- 자동 소속 (페이지·크롤·크롤 스케줄) ----


def test_page_creation_links_site(archive_env):
    with db.connect() as conn:
        page_id = _add_page(conn, "https://example.com/a")
        page = db.get_page_by_id(conn, page_id)
        site = db.get_site(conn, page["site_id"])
        assert site["site_key"] == "example.com"


def test_www_page_joins_apex_site(archive_env):
    with db.connect() as conn:
        apex = db.get_page_by_id(conn, _add_page(conn, "https://example.com/a"))
        www = db.get_page_by_id(conn, _add_page(conn, "https://www.example.com/b"))
        assert apex["site_id"] == www["site_id"]
        # 다른 서브도메인은 다른 사이트
        blog = db.get_page_by_id(conn, _add_page(conn, "https://blog.example.com/c"))
        assert blog["site_id"] != apex["site_id"]


def test_crawl_and_schedule_link_site(archive_env):
    with db.connect() as conn:
        page = db.get_page_by_id(conn, _add_page(conn, "https://www.example.com/a"))
        crawl_id = db.insert_crawl(
            conn,
            start_url="https://example.com/", scope_host="example.com",
            scope_path="/", max_pages=10, max_depth=2, delay_seconds=5,
            source="cli",
        )
        crawl = db.get_crawl(conn, crawl_id)
        assert crawl["site_id"] == page["site_id"]
        db.upsert_crawl_schedule(
            conn, "https://example.com/", max_pages=10, max_depth=2,
            delay_seconds=5, interval_seconds=3600, next_run_at="2026-01-01T00:00:00+00:00",
        )
        sched = db.get_crawl_schedule(conn, "https://example.com/")
        assert sched["site_id"] == page["site_id"]


# ---- 기존 데이터 자동 백필 (마이그레이션) ----


def test_backfill_links_existing_rows(archive_env):
    with db.connect() as conn:
        page_id = _add_page(conn, "https://www.example.com/a")
        # 사이트 도입 전 데이터를 흉내 — site_id 를 비우고 사이트 행 제거
        conn.execute("UPDATE pages SET site_id = NULL")
        conn.execute("DELETE FROM sites")
        db._backfill_sites(conn)
        page = db.get_page_by_id(conn, page_id)
        site = db.get_site(conn, page["site_id"])
        assert site["site_key"] == "example.com"


# ---- 크롤 범위의 www 통합 ----


def test_in_scope_www_and_apex_are_same_site():
    assert crawler.in_scope("https://www.example.com/docs/a", "example.com", "/docs/")
    assert crawler.in_scope("https://example.com/docs/a", "www.example.com", "/docs/")
    assert not crawler.in_scope("https://blog.example.com/docs/a", "example.com", "/docs/")
    assert not crawler.in_scope("https://www.example.com:8080/docs/a", "example.com", "/docs/")


# ---- 삭제와 사이트 정리 ----


def test_last_page_delete_prunes_site(archive_env):
    with db.connect() as conn:
        a = _add_page(conn, "https://example.com/a")
        b = _add_page(conn, "https://www.example.com/b")
        site_id = db.get_page_by_id(conn, a)["site_id"]
        db.delete_page(conn, a)
        assert db.get_site(conn, site_id) is not None  # 페이지가 남아 있음
        db.delete_page(conn, b)
        assert db.get_site(conn, site_id) is None  # 마지막 소속 행 — 정리됨


def test_site_with_crawl_survives_page_delete(archive_env):
    with db.connect() as conn:
        page_id = _add_page(conn, "https://example.com/a")
        site_id = db.get_page_by_id(conn, page_id)["site_id"]
        db.insert_crawl(
            conn,
            start_url="https://example.com/", scope_host="example.com",
            scope_path="/", max_pages=10, max_depth=2, delay_seconds=5,
            source="cli",
        )
        db.delete_page(conn, page_id)
        assert db.get_site(conn, site_id) is not None  # 크롤 회차가 남아 있음


def test_crawl_schedule_remove_prunes_site(archive_env):
    with db.connect() as conn:
        db.upsert_crawl_schedule(
            conn, "https://example.com/", max_pages=10, max_depth=2,
            delay_seconds=5, interval_seconds=3600, next_run_at="2026-01-01T00:00:00+00:00",
        )
        sched = db.get_crawl_schedule(conn, "https://example.com/")
        assert db.get_site(conn, sched["site_id"]) is not None
        db.delete_crawl_schedule(conn, sched["id"])
        assert db.get_site(conn, sched["site_id"]) is None


def test_delete_site_removes_everything(archive_env):
    with db.connect() as conn:
        a = _add_page(conn, "https://example.com/a")
        _add_page(conn, "https://www.example.com/b")
        site_id = db.get_page_by_id(conn, a)["site_id"]
        crawl_id = db.insert_crawl(
            conn,
            start_url="https://example.com/", scope_host="example.com",
            scope_path="/", max_pages=10, max_depth=2, delay_seconds=5,
            source="cli",
        )
        db.insert_crawl_page(conn, crawl_id, "https://example.com/a", 0)
        db.upsert_crawl_schedule(
            conn, "https://example.com/", max_pages=10, max_depth=2,
            delay_seconds=5, interval_seconds=3600, next_run_at="2026-01-01T00:00:00+00:00",
        )
    result = deletion.delete_site(site_id)
    assert result.pages_deleted == 2
    assert result.crawls_deleted == 1
    with db.connect() as conn:
        assert db.get_site(conn, site_id) is None
        assert db.count_pages(conn) == 0
        assert db.get_crawl(conn, crawl_id) is None
        assert db.get_crawl_schedule(conn, "https://example.com/") is None


def test_delete_site_missing_returns_none(archive_env):
    with db.connect():
        pass
    assert deletion.delete_site(12345) is None
