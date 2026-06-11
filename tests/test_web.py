"""대시보드 라우트 테스트. 캡처 없이 fixture 데이터로 검증."""
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from chunchugwan import config, db, storage
from chunchugwan.web import app as web_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    """임시 아카이브(페이지 1개 + 스냅샷 2개 + check 1개) 위의 TestClient."""
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "AUTH_ENABLED", False)  # 인증은 test_auth.py 에서 검증

    url = "https://example.com/post"
    domain, slug = "example.com", storage.url_to_slug(url)
    contents = ["첫 줄\n둘째 줄", "첫 줄\n둘째 줄 수정됨\n셋째 줄"]
    dir_names = ["2026-06-01T00-00-00", "2026-06-02T00-00-00"]

    with db.connect() as conn:
        page_id = db.get_or_create_page(conn, url, domain, slug)
        for i, (text, dir_name) in enumerate(zip(contents, dir_names)):
            snap_dir = storage.page_dir(domain, slug) / dir_name
            snap_dir.mkdir(parents=True)
            (snap_dir / "content.md").write_text(text, encoding="utf-8")
            (snap_dir / "page.html").write_text(
                "<html><body><script>alert(1)</script>본문</body></html>", encoding="utf-8"
            )
            Image.new("RGB", (8, 8), (255 - i * 255,) * 3).save(snap_dir / "screenshot.png")
            db.insert_snapshot(
                conn, page_id,
                taken_at=f"2026-06-0{i + 1}T00:00:00+00:00", dir_name=dir_name,
                content_hash=storage.content_sha256(text),
                final_url=url, http_status=200, changed=1,
            )
        db.insert_check(conn, page_id, storage.content_sha256(contents[1]))

    web_app._active_jobs.clear()  # 다른 테스트의 진행 목록 잔재 제거
    yield TestClient(web_app.app)
    web_app._active_jobs.clear()


def test_index(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "https://example.com/post" in res.text


def test_timeline(client):
    res = client.get("/page/1")
    assert res.status_code == 200
    assert "2026-06-01T00:00:00+00:00" in res.text
    assert "변경 없음 확인 기록" in res.text


def test_timeline_404(client):
    assert client.get("/page/999").status_code == 404


def test_snapshot_view_sandboxed_iframe(client):
    res = client.get("/snapshot/1")
    assert res.status_code == 200
    assert 'sandbox=""' in res.text
    assert 'sandbox="allow' not in res.text  # iframe에 allow-* 토큰 금지


def test_snapshot_file_whitelist(client):
    ok = client.get("/snapshot/1/file/content.md")
    assert ok.status_code == 200 and "첫 줄" in ok.text
    page = client.get("/snapshot/1/file/page.html")
    assert page.status_code == 200
    assert page.headers["content-security-policy"] == "sandbox"
    assert client.get("/snapshot/1/file/meta.json").status_code == 404
    assert client.get("/snapshot/1/file/..%2F..%2Findex.db").status_code == 404


def test_diff_default_latest_two(client):
    res = client.get("/diff/1")
    assert res.status_code == 200
    assert "+2줄" in res.text and "-1줄" in res.text
    assert "둘째 줄 수정됨" in res.text


def test_diff_shows_pixel_ratio(client):
    res = client.get("/diff/1")
    assert res.status_code == 200
    assert "변경 픽셀 100.00%" in res.text  # 흰색 → 검은색


def test_shotdiff_image(client):
    res = client.get("/diff/1/shotdiff?from=1&to=2")
    assert res.status_code == 200
    assert res.headers["content-type"] == "image/png"


def test_diff_bad_range(client):
    assert client.get("/diff/1?from=2&to=1").status_code == 400


def test_rearchive_triggers_pipeline(client, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        web_app.pipeline, "archive_url",
        lambda url, force=False, source="cli": calls.append(url),
    )
    res = client.post("/page/1/rearchive", follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"] == "/page/1?queued=1"
    assert calls == ["https://example.com/post"]


def test_rearchive_unknown_page(client):
    assert client.post("/page/999/rearchive").status_code == 404


def test_archive_new_url_triggers_pipeline(client, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        web_app.pipeline, "archive_url",
        lambda url, force=False, source="cli": calls.append(url),
    )
    res = client.post(
        "/archive",
        data={"url": "https://example.com/new?utm_source=x"},
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert res.headers["location"].startswith("/?queued=")
    # 정규화된 URL(트래킹 파라미터 제거)로 파이프라인 호출
    assert calls == ["https://example.com/new"]


def test_archive_invalid_url_rejected(client, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        web_app.pipeline, "archive_url",
        lambda url, force=False, source="cli": calls.append(url),
    )
    res = client.post("/archive", data={"url": "ftp://example.com/x"}, follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"].startswith("/?error=")
    assert calls == []


def test_index_shows_queued_banner(client):
    res = client.get("/?queued=https%3A%2F%2Fexample.com%2Fnew")
    assert res.status_code == 200
    assert "백그라운드에서 시작" in res.text


def test_archive_registers_active_job_and_clears_on_finish(client, monkeypatch):
    seen: list[list[str]] = []
    monkeypatch.setattr(
        web_app.pipeline, "archive_url",
        lambda url, force=False, source="cli": seen.append(
            sorted(web_app._active_snapshot())
        ),
    )
    res = client.post(
        "/archive", data={"url": "https://example.com/new"}, follow_redirects=False
    )
    assert res.status_code == 303
    # 파이프라인 실행 중에는 진행 목록에 있고, 끝나면 비워진다
    assert seen == [["https://example.com/new"]]
    assert client.get("/archive/active").json() == {"active": []}


def test_active_job_cleared_even_on_failure(client, monkeypatch):
    def boom(url, force=False, source="cli"):
        raise RuntimeError("캡처 실패")

    monkeypatch.setattr(web_app.pipeline, "archive_url", boom)
    client.post("/archive", data={"url": "https://example.com/new"}, follow_redirects=False)
    assert client.get("/archive/active").json() == {"active": []}


def test_index_shows_active_jobs(client):
    web_app._register_job("https://example.com/post")       # 기존 페이지 재아카이빙
    web_app._register_job("https://example.com/brand-new")  # 아직 pages 행 없는 신규 URL
    res = client.get("/")
    assert res.status_code == 200
    assert res.text.count("아카이빙 중") == 2
    assert "https://example.com/brand-new" in res.text
    # 진행 중인 페이지에는 재아카이빙 버튼을 숨긴다
    assert "재아카이빙" not in res.text


def test_archive_active_endpoint_sorted(client):
    web_app._register_job("https://b.example.com/x")
    web_app._register_job("https://a.example.com/x")
    assert client.get("/archive/active").json() == {
        "active": ["https://a.example.com/x", "https://b.example.com/x"]
    }


def test_archive_duplicate_url_not_requeued(client, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        web_app.pipeline, "archive_url",
        lambda url, force=False, source="cli": calls.append(url),
    )
    web_app._register_job("https://example.com/new")  # 이미 진행 중인 상태
    res = client.post(
        "/archive", data={"url": "https://example.com/new"}, follow_redirects=False
    )
    assert res.status_code == 303
    assert calls == []


def test_rearchive_duplicate_not_requeued(client, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        web_app.pipeline, "archive_url",
        lambda url, force=False, source="cli": calls.append(url),
    )
    web_app._register_job("https://example.com/post")
    res = client.post("/page/1/rearchive", follow_redirects=False)
    assert res.status_code == 303
    assert calls == []


def test_schedule_set_and_shown(client):
    res = client.post("/page/1/schedule", data={"interval": "3600"}, follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"] == "/page/1"

    timeline = client.get("/page/1")
    assert "자동 재아카이빙" in timeline.text
    assert "1시간" in timeline.text and "다음 실행" in timeline.text

    index = client.get("/")
    assert "1시간" in index.text  # 목록의 '자동' 컬럼

    with db.connect() as conn:
        sched = db.get_schedule(conn, 1)
    assert sched["interval_seconds"] == 3600


def test_schedule_rejects_out_of_range_interval(client):
    assert client.post("/page/1/schedule", data={"interval": "60"}).status_code == 400
    assert (
        client.post("/page/1/schedule", data={"interval": str(8 * 86400)}).status_code
        == 400
    )
    with db.connect() as conn:
        assert db.get_schedule(conn, 1) is None


def test_schedule_delete(client):
    client.post("/page/1/schedule", data={"interval": "86400"})
    res = client.post("/page/1/schedule/delete", follow_redirects=False)
    assert res.status_code == 303
    with db.connect() as conn:
        assert db.get_schedule(conn, 1) is None


def test_schedule_unknown_page(client):
    assert client.post("/page/999/schedule", data={"interval": "3600"}).status_code == 404
    assert client.post("/page/999/schedule/delete").status_code == 404
