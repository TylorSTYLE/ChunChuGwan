"""로컬 네트워크 태그 — db CRUD, 파이프라인/크롤러 게이트, 웹 UI 테스트.

사설 IP 대역(로컬 네트워크)은 시스템 설정의 태그를 지정해야 아카이빙할 수
있고, 루프백 주소는 항상 거부된다 (netcheck — CLAUDE.md 참조).
캡처는 모킹한다 — 게이트는 캡처 전에 동작해야 한다.
"""
import pytest
from fastapi.testclient import TestClient

from chunchugwan import capture, config, crawler, db, pipeline
from chunchugwan.web import app as web_app


@pytest.fixture
def archive_env(tmp_path, monkeypatch):
    """임시 아카이브 루트 (인증 off)."""
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "RULES_PATH", tmp_path / "rules.json")
    monkeypatch.setattr(config, "RESOURCES_DIR", tmp_path / "resources")
    monkeypatch.setattr(config, "DOCUMENTS_DIR", tmp_path / "documents")
    monkeypatch.setattr(config, "AUTH_ENABLED", False)
    return tmp_path


@pytest.fixture
def client(archive_env):
    with db.connect():
        pass  # 스키마 생성
    web_app._active_jobs.clear()
    yield TestClient(web_app.app)
    web_app._active_jobs.clear()


def _make_tag(name: str = "집 NAS", description: str = "거실 시놀로지") -> str:
    with db.connect() as conn:
        tag = db.create_network_tag(conn, name, description)
    return tag["id"]


def _fake_capture(monkeypatch, final_url: str | None = None):
    """캡처 모킹 — 호출 여부를 기록하고 고정 HTML 을 돌려준다."""
    calls: list[str] = []

    def fake(url, out_dir, remove_selectors=(), link_rewriter=None, session=None,
             resource_fallback=None, **kwargs):
        calls.append(url)
        return capture.CaptureResult(
            final_url=final_url or url, http_status=200, title="제목",
            raw_html="<html><body>내용</body></html>",
            content_html="<html><body>내용</body></html>",
        )

    monkeypatch.setattr(pipeline.capture, "capture", fake)
    return calls


# ---- db CRUD ----


def test_create_tag_issues_guid(archive_env):
    import uuid

    tag_id = _make_tag()
    uuid.UUID(tag_id)  # GUID 형식이 아니면 ValueError
    with db.connect() as conn:
        tag = db.get_network_tag(conn, tag_id)
        assert tag["name"] == "집 NAS"
        assert tag["description"] == "거실 시놀로지"
        assert db.get_network_tag_by_name(conn, "집 NAS")["id"] == tag_id
        assert [t["id"] for t in db.list_network_tags(conn)] == [tag_id]


def test_delete_tag(archive_env):
    tag_id = _make_tag()
    with db.connect() as conn:
        assert db.count_network_tag_refs(conn, tag_id) == 0
        assert db.delete_network_tag(conn, tag_id) is True
        assert db.get_network_tag(conn, tag_id) is None
        assert db.delete_network_tag(conn, tag_id) is False


# ---- 파이프라인 게이트 ----


def test_loopback_archive_rejected(archive_env, monkeypatch):
    calls = _fake_capture(monkeypatch)
    with pytest.raises(ValueError, match="루프백"):
        pipeline.archive_url("http://127.0.0.1:8765/")
    with pytest.raises(ValueError, match="루프백"):
        pipeline.archive_url("http://localhost/")
    assert calls == []  # 게이트는 캡처 전에 동작한다


def test_private_archive_requires_tag(archive_env, monkeypatch):
    calls = _fake_capture(monkeypatch)
    with pytest.raises(ValueError, match="로컬 네트워크 태그"):
        pipeline.archive_url("http://192.168.0.10/wiki")
    assert calls == []


def test_private_archive_with_tag_stores_tag(archive_env, monkeypatch):
    _fake_capture(monkeypatch)
    tag_id = _make_tag()
    outcome = pipeline.archive_url(
        "http://192.168.0.10/wiki", network_tag_id=tag_id
    )
    assert outcome.status == "new"
    with db.connect() as conn:
        page = db.get_page(conn, outcome.url)
        assert page["network_tag_id"] == tag_id
        assert db.count_network_tag_refs(conn, tag_id) == 1


def test_private_rearchive_inherits_page_tag(archive_env, monkeypatch):
    """태그로 만든 페이지는 이후 태그 없이도 재아카이빙된다 (스케줄 경로)."""
    _fake_capture(monkeypatch)
    tag_id = _make_tag()
    pipeline.archive_url("http://192.168.0.10/wiki", network_tag_id=tag_id)
    outcome = pipeline.archive_url("http://192.168.0.10/wiki", source="schedule")
    assert outcome.status == "unchanged"


def test_private_archive_unknown_tag_rejected(archive_env, monkeypatch):
    calls = _fake_capture(monkeypatch)
    with pytest.raises(ValueError, match="등록되지 않은"):
        pipeline.archive_url("http://10.0.0.2/", network_tag_id="no-such-guid")
    assert calls == []


def test_public_archive_ignores_tag(archive_env, monkeypatch):
    """공인 주소는 태그 없이 그대로 — 태그를 줘도 페이지에 남기지 않는다."""
    _fake_capture(monkeypatch)
    tag_id = _make_tag()
    outcome = pipeline.archive_url("https://example.com/post", network_tag_id=tag_id)
    assert outcome.status == "new"
    with db.connect() as conn:
        assert db.get_page(conn, outcome.url)["network_tag_id"] is None


def test_redirect_to_loopback_rejected(archive_env, monkeypatch):
    """공인 주소가 루프백으로 리다이렉트되면 저장하지 않는다."""
    _fake_capture(monkeypatch, final_url="http://127.0.0.1:8765/system")
    with pytest.raises(ValueError, match="루프백"):
        pipeline.archive_url("https://example.com/post")
    with db.connect() as conn:
        assert db.get_page(conn, "https://example.com/post") is None


def test_redirect_to_private_without_tag_rejected(archive_env, monkeypatch):
    _fake_capture(monkeypatch, final_url="http://192.168.0.10/secret")
    with pytest.raises(ValueError, match="로컬 네트워크"):
        pipeline.archive_url("https://example.com/post")


# ---- 크롤러 게이트 ----


def test_start_crawl_loopback_rejected(archive_env):
    with pytest.raises(ValueError, match="루프백"):
        crawler.start_crawl("http://127.0.0.1:8765/docs/")


def test_start_crawl_private_requires_tag(archive_env):
    with pytest.raises(ValueError, match="로컬 네트워크 태그"):
        crawler.start_crawl("http://192.168.0.10/docs/")
    with pytest.raises(ValueError, match="등록되지 않은"):
        crawler.start_crawl("http://192.168.0.10/docs/", network_tag_id="bogus")


def test_start_crawl_private_with_tag(archive_env):
    tag_id = _make_tag()
    crawl, merged = crawler.start_crawl(
        "http://192.168.0.10/docs/", network_tag_id=tag_id
    )
    assert merged is False
    assert crawl["network_tag_id"] == tag_id


def test_process_next_passes_crawl_tag(archive_env):
    """크롤 페이지 처리 시 크롤의 태그가 archive_fn 으로 전달된다."""
    tag_id = _make_tag()
    crawler.start_crawl("http://192.168.0.10/docs/", network_tag_id=tag_id)
    seen = {}

    def fake_archive(url, source, link_rewriter=None, **kwargs):
        seen.update(kwargs, url=url)
        return pipeline.ArchiveOutcome(
            status="new", url=url, content_hash="0" * 64, snapshot_dir=None,
            taken_at="2026-06-13T00:00:00+00:00", last_taken_at=None,
            http_status=200, title=None,
        )

    step = crawler.process_next(archive_fn=fake_archive)
    assert step is not None and step.status == "new"
    assert seen["network_tag_id"] == tag_id


def test_crawl_schedule_keeps_tag(archive_env):
    """크롤 스케줄도 태그를 보관한다 — 주기 실행이 같은 태그로 크롤을 만든다."""
    tag_id = _make_tag()
    sched = crawler.set_crawl_schedule(
        "http://192.168.0.10/docs/", 86400, network_tag_id=tag_id
    )
    assert sched["network_tag_id"] == tag_id
    with pytest.raises(ValueError, match="로컬 네트워크 태그"):
        crawler.set_crawl_schedule("http://192.168.0.11/docs/", 86400)


def test_tag_in_use_counts_crawl_refs(archive_env):
    tag_id = _make_tag()
    crawler.start_crawl("http://192.168.0.10/docs/", network_tag_id=tag_id)
    with db.connect() as conn:
        assert db.count_network_tag_refs(conn, tag_id) == 1


# ---- 시스템 화면 (태그 관리) ----


# ---- 태그 병합 (같은 IP:포트의 중복 태그 정리) ----
# 같은 사설 IP·포트(= 같은 site_id)를 가리키는 두 태그를 하나로 합친다. 출처
# 태그의 참조(페이지·크롤·크롤 스케줄)를 대상으로 옮기고 출처는 삭제하며, 두
# 태그의 site_id 집합이 완전히 같을 때만 허용한다.


def test_network_tag_site_ids_collects_across_tables(archive_env, monkeypatch):
    """태그의 site_id 집합은 페이지·크롤·크롤 스케줄을 모두 모은다 (NULL 제외)."""
    _fake_capture(monkeypatch)
    tag_id = _make_tag()
    pipeline.archive_url("http://192.168.0.10/wiki", network_tag_id=tag_id)
    crawler.start_crawl("http://192.168.0.11/docs/", network_tag_id=tag_id)
    crawler.set_crawl_schedule(
        "http://192.168.0.12/x/", 86400, network_tag_id=tag_id
    )
    with db.connect() as conn:
        # 세 개의 서로 다른 IP = 세 개의 서로 다른 site_id
        assert len(db.network_tag_site_ids(conn, tag_id)) == 3
        # 참조가 없는 태그는 빈 집합
        empty = db.create_network_tag(conn, "빈 태그")
        assert db.network_tag_site_ids(conn, empty["id"]) == set()


def test_merge_moves_all_three_tables(archive_env, monkeypatch):
    """병합은 페이지·크롤·크롤 스케줄 참조를 모두 대상으로 옮기고 행 수를 반환한다."""
    _fake_capture(monkeypatch)
    src = _make_tag("출처")
    tgt = _make_tag("대상")
    pipeline.archive_url("http://192.168.0.10/wiki", network_tag_id=src)
    crawler.start_crawl("http://192.168.0.11/docs/", network_tag_id=src)
    crawler.set_crawl_schedule("http://192.168.0.12/x/", 86400, network_tag_id=src)
    with db.connect() as conn:
        moved = db.merge_network_tags(conn, src, tgt)
        assert moved == {"pages": 1, "crawls": 1, "crawl_schedules": 1}
        assert db.count_network_tag_refs(conn, src) == 0
        assert db.count_network_tag_refs(conn, tgt) == 3


def test_merge_deletes_source_tag(archive_env, monkeypatch):
    """병합 후 출처 태그는 사라지고 대상 태그는 남는다."""
    _fake_capture(monkeypatch)
    src = _make_tag("출처")
    tgt = _make_tag("대상")
    pipeline.archive_url("http://192.168.0.10/wiki", network_tag_id=src)
    with db.connect() as conn:
        db.merge_network_tags(conn, src, tgt)
        assert db.get_network_tag(conn, src) is None
        assert db.get_network_tag(conn, tgt) is not None


# ---- 새 아카이빙 폼 ----


# ---- 목록·상세 화면 표시 ----
# 같은 IP 대역의 다른 사설 네트워크를 구분할 수 있도록 아카이브 목록·사이트
# 상세·크롤·타임라인·스냅샷 화면 모두 태그 이름을 뱃지로 보여준다.


def test_public_views_show_no_tag_badge(client, monkeypatch):
    """공인 주소 아카이브에는 어떤 화면에도 태그 뱃지가 없다."""
    _make_tag()
    _fake_capture(monkeypatch)
    outcome = pipeline.archive_url("https://example.com/post")
    with db.connect() as conn:
        page = db.get_page(conn, outcome.url)
    assert "집 NAS" not in client.get("/archives").text
    assert "집 NAS" not in client.get(f"/sites/{page['site_id']}").text
    assert "집 NAS" not in client.get(f"/page/{page['id']}").text
    assert "집 NAS" not in client.get("/").text
    assert "집 NAS" not in client.get("/logs").text


# ---- REST API ----


def test_api_archive_loopback_rejected(client):
    res = client.post("/api/v1/archive", json={"url": "http://127.0.0.1:9999/"})
    assert res.status_code == 400
    assert "루프백" in res.json()["detail"]


def test_api_archive_private_requires_tagged_page(client, monkeypatch):
    res = client.post("/api/v1/archive", json={"url": "http://192.168.0.10/wiki"})
    assert res.status_code == 400
    # 웹 UI 에서 태그를 지정해 만든 페이지는 API 재아카이빙 허용
    tag_id = _make_tag()
    _fake_capture(monkeypatch)
    pipeline.archive_url("http://192.168.0.10/wiki", network_tag_id=tag_id)
    res = client.post("/api/v1/archive", json={"url": "http://192.168.0.10/wiki"})
    assert res.status_code == 202
