"""사이트 전체 아카이브(크롤러) 테스트 — 캡처 없이 가짜 파이프라인으로 검증."""
from datetime import datetime, timedelta, timezone

import pytest

from chunchugwan import config, crawler, db, pipeline, storage


@pytest.fixture
def archive_env(tmp_path, monkeypatch):
    """임시 아카이브 루트 (DB 만 사용)."""
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "RESOURCES_DIR", tmp_path / "resources")
    return tmp_path


def fake_outcome(url: str, status: str = "new", snapshot_id: int | None = None,
                 links: tuple[str, ...] = ()) -> pipeline.ArchiveOutcome:
    return pipeline.ArchiveOutcome(
        status=status, url=url, content_hash="0" * 64, snapshot_dir=None,
        taken_at=None, last_taken_at=None, http_status=200, title=None,
        snapshot_id=snapshot_id, page_links=list(links),
    )


def unblock_crawl(crawl_id: int) -> None:
    """페이지 간 간격 대기를 해제 (테스트 시간 진행 대용)."""
    with db.connect() as conn:
        conn.execute(
            "UPDATE crawls SET next_page_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
            (crawl_id,),
        )


# ---- 범위 ----


def test_scope_of_directory():
    assert crawler.scope_of("https://example.com/docs/intro") == ("example.com", "/docs/")
    assert crawler.scope_of("https://example.com/docs/") == ("example.com", "/docs/")
    assert crawler.scope_of("https://example.com/") == ("example.com", "/")


def test_in_scope():
    assert crawler.in_scope("https://example.com/docs/a", "example.com", "/docs/")
    assert not crawler.in_scope("https://example.com/blog/a", "example.com", "/docs/")
    assert not crawler.in_scope("https://sub.example.com/docs/a", "example.com", "/docs/")
    assert not crawler.in_scope("https://example.com:8080/docs/a", "example.com", "/docs/")
    # 스킴은 비교하지 않는다 (https 추정/http 폴백 대응)
    assert crawler.in_scope("http://example.com/docs/a", "example.com", "/docs/")


# ---- 등록 ----


def test_start_crawl_creates_queue(archive_env):
    row = crawler.start_crawl("https://example.com/docs/intro", source="cli")
    assert row["status"] == "running"
    assert row["scope_host"] == "example.com"
    assert row["scope_path"] == "/docs/"
    with db.connect() as conn:
        pages = db.list_crawl_pages(conn, row["id"])
    assert [p["url"] for p in pages] == ["https://example.com/docs/intro"]
    assert pages[0]["status"] == "pending" and pages[0]["depth"] == 0


def test_start_crawl_validates_options(archive_env):
    with pytest.raises(ValueError):
        crawler.start_crawl("https://example.com/", max_pages=0)
    with pytest.raises(ValueError):
        crawler.start_crawl("https://example.com/", max_depth=config.CRAWL_MAX_DEPTH_LIMIT + 1)
    with pytest.raises(ValueError):
        crawler.start_crawl("https://example.com/", delay_seconds=0)
    with pytest.raises(ValueError):
        crawler.start_crawl("not a url ::")


# ---- 링크 재작성 매핑 ----


def test_link_rewriter_maps_http_links_only():
    rewrite = crawler.link_rewriter(7)
    mapping = rewrite([
        "https://example.com/docs/a?utm_source=x",
        "mailto:someone@example.com",
        "javascript:void(0)",
        "https://other.example.org/page",   # 범위 밖도 리졸버로 (리졸버가 판정)
    ])
    assert mapping["https://example.com/docs/a?utm_source=x"] == (
        "/crawl/7/goto?url=https%3A%2F%2Fexample.com%2Fdocs%2Fa"
    )
    assert "mailto:someone@example.com" not in mapping
    assert "javascript:void(0)" not in mapping
    assert "https://other.example.org/page" in mapping


# ---- 처리 흐름 ----


def test_process_next_enqueues_in_scope_links(archive_env):
    row = crawler.start_crawl("https://example.com/docs/", delay_seconds=1)
    calls = []

    def fake(url, source, link_rewriter):
        calls.append((url, source))
        assert link_rewriter is not None
        return fake_outcome(url, links=(
            "https://example.com/docs/a",
            "https://example.com/docs/a#section",   # 정규화로 a 와 동일 — 중복 제거
            "https://example.com/blog/out-of-scope",
            "https://elsewhere.org/x",
            "mailto:x@y.z",
        ))

    step = crawler.process_next(archive_fn=fake)
    assert step is not None and step.status == "new"
    assert calls == [("https://example.com/docs/", "crawl")]
    assert step.enqueued == 1
    with db.connect() as conn:
        pages = db.list_crawl_pages(conn, row["id"])
    assert [(p["url"], p["status"], p["depth"]) for p in pages] == [
        ("https://example.com/docs/", "done", 0),
        ("https://example.com/docs/a", "pending", 1),
    ]


def test_process_next_respects_delay_between_pages(archive_env):
    crawler.start_crawl("https://example.com/docs/", delay_seconds=3600)

    def fake(url, source, link_rewriter):
        return fake_outcome(url, links=("https://example.com/docs/a",))

    assert crawler.process_next(archive_fn=fake) is not None
    # 간격이 지나지 않아 다음 페이지는 잡히지 않는다
    assert crawler.process_next(archive_fn=fake) is None


def test_process_next_respects_max_pages_and_depth(archive_env):
    row = crawler.start_crawl(
        "https://example.com/docs/", max_pages=2, max_depth=1, delay_seconds=1
    )

    def fake(url, source, link_rewriter):
        return fake_outcome(url, links=(
            "https://example.com/docs/a",
            "https://example.com/docs/b",
            "https://example.com/docs/c",
        ))

    step = crawler.process_next(archive_fn=fake)
    assert step.enqueued == 1  # max_pages=2 — 시작 페이지 포함이라 1개만 추가
    unblock_crawl(row["id"])
    step = crawler.process_next(archive_fn=fake)
    assert step.enqueued == 0  # depth 1 페이지의 링크는 max_depth 로 더 안 들어간다
    assert step.crawl_done
    with db.connect() as conn:
        assert db.get_crawl(conn, row["id"])["status"] == "done"


def test_process_next_records_snapshot_for_unchanged(archive_env):
    """내용이 같아 저장이 생략돼도 크롤 세트는 기존 스냅샷을 참조한다."""
    url = "https://example.com/docs/"
    with db.connect() as conn:
        page_id = db.get_or_create_page(conn, url, "example.com", storage.url_to_slug(url))
        snap_id = db.insert_snapshot(
            conn, page_id, taken_at="2026-06-01T00:00:00+00:00",
            dir_name="2026-06-01T00-00-00", content_hash="0" * 64,
            final_url=url, http_status=200, changed=1,
        )
    row = crawler.start_crawl(url, delay_seconds=1)

    def fake(u, source, link_rewriter):
        return fake_outcome(u, status="unchanged", snapshot_id=snap_id)

    step = crawler.process_next(archive_fn=fake)
    assert step.status == "unchanged" and step.crawl_done
    with db.connect() as conn:
        pages = db.list_crawl_pages(conn, row["id"])
        assert pages[0]["snapshot_id"] == snap_id
        assert db.find_crawl_snapshot(conn, row["id"], url) == snap_id


# ---- 실패 / 재시도 ----


def test_failure_schedules_backoff_retry(archive_env):
    row = crawler.start_crawl("https://example.com/docs/", delay_seconds=1)

    def boom(url, source, link_rewriter):
        raise RuntimeError("연결 실패")

    step = crawler.process_next(archive_fn=boom)
    assert step.status == "retry" and "연결 실패" in step.error
    with db.connect() as conn:
        page = db.list_crawl_pages(conn, row["id"])[0]
    assert page["status"] == "pending" and page["attempts"] == 1
    assert page["next_attempt_at"] is not None
    # 재시도 대기 중에는 잡히지 않는다 (크롤 간격을 풀어도)
    unblock_crawl(row["id"])
    assert crawler.process_next(archive_fn=boom) is None
    # 크롤은 아직 진행 중
    with db.connect() as conn:
        assert db.get_crawl(conn, row["id"])["status"] == "running"


def test_failure_exhausts_attempts_then_fails(archive_env):
    row = crawler.start_crawl("https://example.com/docs/", delay_seconds=1)

    def boom(url, source, link_rewriter):
        raise RuntimeError("계속 실패")

    for _ in range(config.CRAWL_MAX_ATTEMPTS):
        unblock_crawl(row["id"])
        with db.connect() as conn:  # 재시도 대기 해제
            conn.execute(
                "UPDATE crawl_pages SET next_attempt_at = NULL WHERE crawl_id = ?",
                (row["id"],),
            )
        step = crawler.process_next(archive_fn=boom)
    assert step.status == "failed" and step.crawl_done
    with db.connect() as conn:
        page = db.list_crawl_pages(conn, row["id"])[0]
        assert page["status"] == "failed"
        assert page["attempts"] == config.CRAWL_MAX_ATTEMPTS
        assert db.get_crawl(conn, row["id"])["status"] == "done"


def test_retry_failed_reopens_crawl(archive_env):
    row = crawler.start_crawl("https://example.com/docs/", delay_seconds=1)
    with db.connect() as conn:
        conn.execute(
            "UPDATE crawl_pages SET status = 'failed', attempts = 3, error = 'x' "
            "WHERE crawl_id = ?", (row["id"],),
        )
        conn.execute(
            "UPDATE crawls SET status = 'done', finished_at = '2026-01-01T00:00:00+00:00' "
            "WHERE id = ?", (row["id"],),
        )
    with db.connect() as conn:
        assert db.retry_failed_crawl_pages(conn, row["id"]) == 1
    with db.connect() as conn:
        crawl = db.get_crawl(conn, row["id"])
        page = db.list_crawl_pages(conn, row["id"])[0]
    assert crawl["status"] == "running" and crawl["finished_at"] is None
    assert page["status"] == "pending" and page["attempts"] == 0 and page["error"] is None


# ---- 취소 / 클레임 ----


def test_cancelled_crawl_is_not_processed(archive_env):
    row = crawler.start_crawl("https://example.com/docs/", delay_seconds=1)
    with db.connect() as conn:
        assert db.cancel_crawl(conn, row["id"])
    assert crawler.process_next(archive_fn=lambda *a, **k: fake_outcome("x")) is None
    with db.connect() as conn:
        assert db.get_crawl(conn, row["id"])["status"] == "cancelled"


def test_claim_conflict_releases_page(archive_env):
    """진행 중 작업 레지스트리와 충돌하면 클레임을 반납한다."""
    row = crawler.start_crawl("https://example.com/docs/", delay_seconds=1)
    step = crawler.process_next(
        claim=lambda url: False, archive_fn=lambda *a, **k: fake_outcome("x")
    )
    assert step.status == "skipped"
    with db.connect() as conn:
        page = db.list_crawl_pages(conn, row["id"])[0]
    assert page["status"] == "pending" and page["attempts"] == 0


def test_stale_in_progress_recovered(archive_env):
    """중단된 프로세스가 남긴 in_progress 는 일정 시간 후 복구된다."""
    row = crawler.start_crawl("https://example.com/docs/", delay_seconds=1)
    stale = (
        datetime.now(timezone.utc)
        - timedelta(seconds=config.CRAWL_STALE_CLAIM_SECONDS + 60)
    ).isoformat(timespec="seconds")
    with db.connect() as conn:
        conn.execute(
            "UPDATE crawl_pages SET status = 'in_progress', claimed_at = ? "
            "WHERE crawl_id = ?", (stale, row["id"]),
        )
    step = crawler.process_next(archive_fn=lambda *a, **k: fake_outcome("x"))
    assert step is not None and step.status == "new"


# ---- 삭제 연동 ----


def test_snapshot_delete_clears_crawl_reference(archive_env):
    url = "https://example.com/docs/"
    with db.connect() as conn:
        page_id = db.get_or_create_page(conn, url, "example.com", storage.url_to_slug(url))
        snap_id = db.insert_snapshot(
            conn, page_id, taken_at="2026-06-01T00:00:00+00:00",
            dir_name="2026-06-01T00-00-00", content_hash="0" * 64,
            final_url=url, http_status=200, changed=1,
        )
    row = crawler.start_crawl(url, delay_seconds=1)
    with db.connect() as conn:
        page = db.list_crawl_pages(conn, row["id"])[0]
        db.finish_crawl_page(conn, page["id"], snap_id)
    with db.connect() as conn:
        assert db.delete_snapshot(conn, snap_id)
    with db.connect() as conn:
        assert db.list_crawl_pages(conn, row["id"])[0]["snapshot_id"] is None
