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


def test_crawls_redirects_to_archives(client):
    """구 /crawls 목록은 통합 아카이브 목록(/archives)으로 리다이렉트한다."""
    res = client.get("/crawls", follow_redirects=False)
    assert res.status_code == 301
    assert res.headers["location"] == "/archives"


def test_archives_groups_by_site(client):
    """목록은 사이트(서브도메인) 단위 — 페이지와 크롤이 같은 사이트 행으로 묶인다."""
    make_snapshot("https://example.com/post")
    crawl, _ = crawler.start_crawl("https://example.com/docs/", source="web")
    res = client.get("/archives")
    assert res.status_code == 200
    with db.connect() as conn:
        site = db.get_site_by_key(conn, "example.com")
    # 페이지와 크롤이 사이트 행 하나로 합쳐진다
    assert res.text.count(f'href="/sites/{site["id"]}"') == 1
    assert "크롤 진행 중" in res.text
    # 사이트 상세에 페이지 행과 크롤 회차가 모두 보인다
    detail = client.get(f"/sites/{site['id']}")
    assert 'href="/page/1"' in detail.text
    assert f'href="/crawls/{crawl["id"]}"' in detail.text
    assert "example.com/docs/" in detail.text  # 회차 범위 표기
    assert "진행 중" in detail.text


def test_www_and_apex_share_site_row(client):
    """www 페이지와 apex 페이지는 같은 사이트 행 하나로 보인다."""
    make_snapshot("https://example.com/a")
    make_snapshot("https://www.example.com/b")
    res = client.get("/archives")
    with db.connect() as conn:
        site = db.get_site_by_key(conn, "example.com")
        assert db.count_sites(conn) == 1
    assert res.text.count(f'href="/sites/{site["id"]}"') == 1
    detail = client.get(f"/sites/{site['id']}")
    assert "https://example.com/a" in detail.text
    assert "https://www.example.com/b" in detail.text


def test_archives_shows_finished_crawl_status(client):
    """끝난 크롤은 사이트 상세에서 상태 뱃지(취소됨)로 보인다."""
    crawl, _ = crawler.start_crawl("https://example.com/docs/", source="web")
    with db.connect() as conn:
        db.cancel_crawl(conn, crawl["id"])
        site = db.get_site_by_key(conn, "example.com")
    res = client.get("/archives")
    # 진행 중 크롤이 없으면 폴링 목록도 비어 있다
    assert "const runningCrawls = []" in res.text
    detail = client.get(f"/sites/{site['id']}")
    assert "취소됨" in detail.text


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


def test_post_archive_site_merges_into_running_crawl(client):
    """같은 시작 URL 의 크롤이 진행 중이면 그 진행 화면으로 병합 알림과 함께 보낸다."""
    crawl, _ = crawler.start_crawl("https://example.com/docs/", source="web")
    res = client.post(
        "/archive",
        data={"url": "example.com/docs/", "site": "on"},
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert res.headers["location"] == f"/crawls/{crawl['id']}?merged=1"
    with db.connect() as conn:
        assert len(db.list_crawls(conn)) == 1  # 새 크롤이 만들어지지 않는다

    page = client.get(res.headers["location"])
    assert "병합되었습니다" in page.text
    # 병합 파라미터 없이 열면 알림이 없다
    assert "병합되었습니다" not in client.get(f"/crawls/{crawl['id']}").text


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
    crawl, _ = crawler.start_crawl("https://example.com/docs/", source="web")
    res = client.get(f"/crawls/{crawl['id']}")
    assert res.status_code == 200
    assert "https://example.com/docs/" in res.text
    assert "example.com/docs/" in res.text  # 범위 표기

    status = client.get(f"/crawls/{crawl['id']}/status").json()
    assert status["status"] == "running"
    assert status["counts"]["pending"] == 1 and status["counts"]["total"] == 1

    assert client.get("/crawls/999").status_code == 404


def test_crawl_cancel_and_retry(client):
    crawl, _ = crawler.start_crawl("https://example.com/docs/", source="web")
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


def test_crawl_detail_rerun_button(client):
    """회차 상세 — 끝난 크롤에는 '다시 아카이빙' 버튼이 보이고 진행 중에는 없다."""
    crawl, _ = crawler.start_crawl("https://example.com/docs/", source="web")
    rerun_action = f"/sites/{crawl['site_id']}/crawls/{crawl['id']}/rerun"
    # 진행 중에는 안 보인다 (취소·실패 일괄 재시도만)
    assert rerun_action not in client.get(f"/crawls/{crawl['id']}").text
    # 취소되어 끝나면 같은 옵션으로 다시 실행하는 버튼이 보인다
    client.post(f"/crawls/{crawl['id']}/cancel")
    assert rerun_action in client.get(f"/crawls/{crawl['id']}").text


def _add_failed_page(crawl_id: int, url: str) -> int:
    """크롤에 실패(failed) 페이지 한 줄 추가 후 crawl_page id 반환."""
    with db.connect() as conn:
        db.insert_crawl_page(conn, crawl_id, url, 1)
        page = [p for p in db.list_crawl_pages(conn, crawl_id) if p["url"] == url][0]
        db.fail_crawl_page(
            conn, page["id"], attempts=3, error="boom", next_attempt_at=None
        )
    return page["id"]


def test_site_crawl_list_retry_failed_button(client):
    """사이트 회차 목록 — 실패 페이지가 있는 회차에만 '실패 일괄 재시도' 버튼이
    보이고, 누르면 실패 페이지만 큐로 되돌아온다 (전체 재실행과 구분)."""
    crawl, _ = crawler.start_crawl("https://example.com/docs/", source="web")
    retry_action = f'action="/crawls/{crawl["id"]}/retry"'
    with db.connect() as conn:
        site = db.get_site_by_key(conn, "example.com")
    # 실패가 없으면 회차 행에 실패 재시도 버튼이 없다
    assert retry_action not in client.get(f"/sites/{site['id']}").text
    # 실패 페이지가 생기면 버튼이 보인다
    cp_id = _add_failed_page(crawl["id"], "https://example.com/docs/fail")
    assert retry_action in client.get(f"/sites/{site['id']}").text
    # 누르면 실패 페이지만 pending 으로 되돌아온다 (성공 페이지는 그대로)
    res = client.post(f"/crawls/{crawl['id']}/retry", follow_redirects=False)
    assert res.status_code == 303
    with db.connect() as conn:
        cp = conn.execute(
            "SELECT * FROM crawl_pages WHERE id = ?", (cp_id,)
        ).fetchone()
    assert cp["status"] == "pending" and cp["attempts"] == 0


def test_crawl_detail_status_filter(client):
    """페이지 목록은 ?status= 로 상태별 필터링된다 (잘못된 값은 전체)."""
    crawl, _ = crawler.start_crawl("https://example.com/docs/", source="web")
    _add_failed_page(crawl["id"], "https://example.com/docs/fail")
    pending_cell = '<td class="mono">https://example.com/docs/</td>'

    res = client.get(f"/crawls/{crawl['id']}", params={"status": "failed"})
    assert res.status_code == 200
    assert "https://example.com/docs/fail" in res.text
    assert pending_cell not in res.text
    assert f'href="/crawls/{crawl["id"]}?status=pending"' in res.text  # 필터 링크

    res = client.get(f"/crawls/{crawl['id']}", params={"status": "pending"})
    assert "https://example.com/docs/fail" not in res.text
    assert pending_cell in res.text

    # 잘못된 값은 전체 목록
    res = client.get(f"/crawls/{crawl['id']}", params={"status": "nope"})
    assert "https://example.com/docs/fail" in res.text
    assert pending_cell in res.text


def test_crawl_page_retry_single(client):
    """실패 페이지 하나만 재시도 — pending 복귀, 끝난 크롤은 다시 열린다."""
    crawl, _ = crawler.start_crawl("https://example.com/docs/", source="web")
    cp_id = _add_failed_page(crawl["id"], "https://example.com/docs/fail")
    with db.connect() as conn:
        ok_page = [
            p for p in db.list_crawl_pages(conn, crawl["id"])
            if p["url"] == "https://example.com/docs/"
        ][0]
        db.finish_crawl_page(conn, ok_page["id"], None)
        assert db.finish_crawl_if_done(conn, crawl["id"])

    # 진행 화면의 실패 행에 재시도 버튼이 보인다
    res = client.get(f"/crawls/{crawl['id']}")
    assert f"/crawls/{crawl['id']}/pages/{cp_id}/retry" in res.text

    res = client.post(
        f"/crawls/{crawl['id']}/pages/{cp_id}/retry",
        params={"status": "failed"}, follow_redirects=False,
    )
    assert res.status_code == 303
    assert res.headers["location"].startswith(f"/crawls/{crawl['id']}?")
    assert "status=failed" in res.headers["location"]  # 필터 유지
    assert "notice=" in res.headers["location"]
    with db.connect() as conn:
        row = [
            p for p in db.list_crawl_pages(conn, crawl["id"]) if p["id"] == cp_id
        ][0]
        assert row["status"] == "pending"
        assert row["attempts"] == 0 and row["error"] is None
        # 끝난 크롤이 다시 열린다. done 페이지는 그대로
        assert db.get_crawl(conn, crawl["id"])["status"] == "running"
        assert db.crawl_page_counts(conn, crawl["id"])["done"] == 1

    # 실패 상태가 아닌 페이지·없는 페이지·없는 크롤은 404
    assert client.post(
        f"/crawls/{crawl['id']}/pages/{ok_page['id']}/retry"
    ).status_code == 404
    assert client.post(f"/crawls/{crawl['id']}/pages/9999/retry").status_code == 404
    assert client.post(f"/crawls/999/pages/{cp_id}/retry").status_code == 404


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


def test_archive_form_uses_settings_defaults(client):
    """크롤 옵션 폼의 초깃값은 시스템 설정의 기본값이다."""
    with db.connect() as conn:
        db.set_setting(conn, db.CRAWL_DEFAULT_MAX_PAGES_KEY, "42")
        db.set_setting(conn, db.CRAWL_DEFAULT_DELAY_KEY, "11")
    res = client.get("/archive/new")
    assert 'name="crawl_max_pages" value="42"' in res.text
    assert 'name="crawl_delay" value="11"' in res.text


def test_post_archive_site_with_interval_registers_schedule(client):
    """사이트 아카이브 + 주기 선택 → 크롤과 크롤 스케줄이 함께 등록된다."""
    res = client.post(
        "/archive",
        data={
            "url": "example.com/docs/", "site": "on",
            "crawl_max_pages": "30", "crawl_max_depth": "2", "crawl_delay": "10",
            "interval": "86400", "run_at": "09:00",
        },
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert res.headers["location"] == "/crawls/1"
    with db.connect() as conn:
        sched = db.get_crawl_schedule(conn, "https://example.com/docs/")
    assert sched is not None
    assert sched["interval_seconds"] == 86400 and sched["run_at_time"] == "09:00"
    # 크롤 옵션이 스케줄에도 그대로 저장된다
    assert sched["max_pages"] == 30 and sched["max_depth"] == 2
    assert sched["delay_seconds"] == 10


def test_post_archive_site_without_interval_has_no_schedule(client):
    client.post(
        "/archive", data={"url": "example.com/docs/", "site": "on", "interval": "0"},
        follow_redirects=False,
    )
    with db.connect() as conn:
        assert db.list_crawl_schedules(conn) == []


def test_schedules_page_lists_crawl_schedule(client):
    crawler.set_crawl_schedule("https://example.com/docs/", 12 * 3600, max_pages=20)
    res = client.get("/schedules")
    assert res.status_code == 200
    assert "사이트 아카이브" in res.text
    assert "https://example.com/docs/" in res.text
    assert "20 · 5 · 5s" in res.text  # 옵션 (페이지·깊이·간격)


def test_crawl_schedule_change_interval_and_delete(client):
    sched = crawler.set_crawl_schedule("https://example.com/docs/", 3600, max_pages=20)
    res = client.post(
        f"/crawl-schedules/{sched['id']}", data={"interval": "86400"},
        follow_redirects=False,
    )
    assert res.status_code == 303
    with db.connect() as conn:
        updated = db.get_crawl_schedule_by_id(conn, sched["id"])
    assert updated["interval_seconds"] == 86400
    assert updated["max_pages"] == 20  # 옵션은 유지

    res = client.post(
        f"/crawl-schedules/{sched['id']}/next-run",
        data={"next_run": "2099-01-02T03:04", "tz_offset": "0"},
        follow_redirects=False,
    )
    assert res.status_code == 303
    with db.connect() as conn:
        updated = db.get_crawl_schedule_by_id(conn, sched["id"])
    assert updated["next_run_at"] == "2099-01-02T03:04:00+00:00"

    res = client.post(
        f"/crawl-schedules/{sched['id']}/delete", follow_redirects=False
    )
    assert res.status_code == 303
    with db.connect() as conn:
        assert db.get_crawl_schedule_by_id(conn, sched["id"]) is None
    # 없는 스케줄은 404
    assert client.post(f"/crawl-schedules/{sched['id']}/delete").status_code == 404


def test_crawl_detail_shows_retry_backoff(client):
    """크롤 진행 화면에 실패 재시도 대기(시스템 설정)가 표시된다."""
    with db.connect() as conn:
        db.set_setting(conn, db.CRAWL_RETRY_BACKOFF_KEY, "60,120")
    crawl, _ = crawler.start_crawl("https://example.com/docs/", source="web")
    res = client.get(f"/crawls/{crawl['id']}")
    assert "실패 재시도" in res.text
    assert "1분 → 2분" in res.text
    assert "최대 3회 시도" in res.text


def test_system_crawl_settings_saved(client):
    res = client.post(
        "/system/crawl-settings",
        data={
            "crawl_max_pages": "50", "crawl_max_depth": "3",
            "crawl_delay": "7", "crawl_retry_backoff": "60, 120",
        },
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert "notice=" in res.headers["location"]
    with db.connect() as conn:
        assert crawler.crawl_defaults(conn) == {
            "max_pages": 50, "max_depth": 3, "delay_seconds": 7,
        }
        assert crawler.retry_backoff(conn) == (60, 120)
    # 시스템 화면에 현재 값이 보인다
    res = client.get("/system")
    assert 'value="50"' in res.text
    assert 'value="60, 120"' in res.text


def test_system_crawl_settings_rejects_bad_values(client):
    res = client.post(
        "/system/crawl-settings",
        data={
            "crawl_max_pages": "0", "crawl_max_depth": "3",
            "crawl_delay": "7", "crawl_retry_backoff": "60",
        },
        follow_redirects=False,
    )
    assert "error=" in res.headers["location"]
    res = client.post(
        "/system/crawl-settings",
        data={
            "crawl_max_pages": "50", "crawl_max_depth": "3",
            "crawl_delay": "7", "crawl_retry_backoff": "abc",
        },
        follow_redirects=False,
    )
    assert "error=" in res.headers["location"]
    with db.connect() as conn:
        assert db.get_setting(conn, db.CRAWL_DEFAULT_MAX_PAGES_KEY) is None


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
