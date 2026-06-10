"""대시보드 라우트 테스트. 캡처 없이 fixture 데이터로 검증."""
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from archiver import config, db, storage
from archiver.web import app as web_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    """임시 아카이브(페이지 1개 + 스냅샷 2개 + check 1개) 위의 TestClient."""
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")

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

    return TestClient(web_app.app)


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
    monkeypatch.setattr(web_app.pipeline, "archive_url", lambda url, force=False: calls.append(url))
    res = client.post("/page/1/rearchive", follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"] == "/page/1?queued=1"
    assert calls == ["https://example.com/post"]


def test_rearchive_unknown_page(client):
    assert client.post("/page/999/rearchive").status_code == 404
