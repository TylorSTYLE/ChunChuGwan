"""확장용 REST API 표면 — POST /api/v1/crawl, network_tag, GET /go 딥링크, CSRF 면제."""
import pytest
from fastapi.testclient import TestClient

from chunchugwan import auth, config, db, storage
from chunchugwan.web import app as web_app

URL = "https://example.com/post"


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "RESOURCES_DIR", tmp_path / "resources")
    monkeypatch.setattr(config, "DOCUMENTS_DIR", tmp_path / "documents")


@pytest.fixture
def client(tmp_db):
    domain, slug = "example.com", storage.url_to_slug(URL)
    with db.connect() as conn:
        db.create_first_admin(conn, "boss@test.co", auth.hash_password("bosspass1234"))
        page_id = db.get_or_create_page(conn, URL, domain, slug)
        snap_dir = storage.page_dir(domain, slug) / "2026-06-01T00-00-00"
        snap_dir.mkdir(parents=True)
        (snap_dir / "content.md").write_text("본문", encoding="utf-8")
        db.insert_snapshot(
            conn, page_id, taken_at="2026-06-01T00:00:00+00:00",
            dir_name="2026-06-01T00-00-00", content_hash="h1",
            final_url=URL, http_status=200, changed=1,
        )
    web_app._active_jobs.clear()
    yield TestClient(web_app.app)
    web_app._active_jobs.clear()


def _issue(**kwargs) -> str:
    options = {"can_view": True, "can_archive": True,
               "created_by": None, "ttl_seconds": None}
    options.update(kwargs)
    with db.connect() as conn:
        return auth.issue_api_key(conn, "key", **options)


def _headers(token):
    return {"Authorization": f"Bearer {token}"}


def _login_admin(client):
    return client.post(
        "/login", data={"email": "boss@test.co", "password": "bosspass1234"},
        follow_redirects=False,
    )


# ---- POST /api/v1/crawl ----


def test_api_crawl_registers_and_merges(client):
    token = _issue()
    r = client.post(
        "/api/v1/crawl", json={"url": "https://example.com/section/"},
        headers=_headers(token),
    )
    assert r.status_code == 202
    body = r.json()
    assert body["merged"] is False
    assert isinstance(body["crawl_id"], int)
    assert set(body["counts"]) >= {"pending", "done", "failed", "total"}
    # 같은 시작 URL 재호출 → 진행 중 크롤로 병합
    r2 = client.post(
        "/api/v1/crawl", json={"url": "https://example.com/section/"},
        headers=_headers(token),
    )
    assert r2.json()["merged"] is True
    assert r2.json()["crawl_id"] == body["crawl_id"]


def test_api_crawl_requires_archive_perm(client):
    token = _issue(can_archive=False)
    r = client.post(
        "/api/v1/crawl", json={"url": "https://example.com/x/"}, headers=_headers(token)
    )
    assert r.status_code == 403


def test_api_crawl_rejects_loopback(client):
    token = _issue()
    r = client.post(
        "/api/v1/crawl", json={"url": "http://localhost:8000/"}, headers=_headers(token)
    )
    assert r.status_code == 400


def test_api_crawl_private_requires_valid_tag(client):
    token = _issue()
    # 태그 없음 → 400
    assert client.post(
        "/api/v1/crawl", json={"url": "https://192.168.0.10/"}, headers=_headers(token)
    ).status_code == 400
    # 미등록 태그 → 400
    assert client.post(
        "/api/v1/crawl",
        json={"url": "https://192.168.0.10/", "network_tag": "no-such-tag"},
        headers=_headers(token),
    ).status_code == 400
    # 유효 태그 → 202
    with db.connect() as conn:
        tag = db.create_network_tag(conn, "home")
    r = client.post(
        "/api/v1/crawl",
        json={"url": "https://192.168.0.10/", "network_tag": tag["id"]},
        headers=_headers(token),
    )
    assert r.status_code == 202


# ---- POST /api/v1/archive network_tag ----


def test_api_archive_private_with_tag(client):
    token = _issue()
    assert client.post(
        "/api/v1/archive", json={"url": "https://192.168.0.10/x"},
        headers=_headers(token),
    ).status_code == 400
    with db.connect() as conn:
        tag = db.create_network_tag(conn, "home")
    r = client.post(
        "/api/v1/archive",
        json={"url": "https://192.168.0.10/x", "network_tag": tag["id"]},
        headers=_headers(token),
    )
    assert r.status_code == 202


# ---- CSRF 면제 (/api/ POST) ----


def test_api_post_csrf_exempt_with_foreign_origin(client):
    token = _issue()
    r = client.post(
        "/api/v1/archive", json={"url": "https://example.com/new"},
        headers={**_headers(token), "Origin": "https://evil.example"},
    )
    assert r.status_code == 202  # /api/ 는 Origin 검사 면제


def test_non_api_post_still_csrf_protected(client):
    _login_admin(client)
    r = client.post(
        "/settings/account/extension-tokens",
        data={"name": "x", "expiry": "permanent"},
        headers={"Origin": "https://evil.example"}, follow_redirects=False,
    )
    assert r.status_code == 403  # 세션 POST 는 Origin 검사 유지


# ---- GET /go 딥링크 ----


def test_go_redirects_to_timeline(client):
    _login_admin(client)
    with db.connect() as conn:
        page_id = db.get_page(conn, URL)["id"]
    r = client.get("/go", params={"url": URL}, follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == f"/page/{page_id}"


def test_go_scheme_swap_finds_https_page(client):
    _login_admin(client)
    with db.connect() as conn:
        page_id = db.get_page(conn, URL)["id"]
    # http 로 물어도 https 로 저장된 페이지를 찾아준다
    r = client.get(
        "/go", params={"url": "http://example.com/post"}, follow_redirects=False
    )
    assert r.status_code == 302
    assert r.headers["location"] == f"/page/{page_id}"


def test_go_missing_returns_404(client):
    _login_admin(client)
    r = client.get(
        "/go", params={"url": "https://nowhere.example/zzz"}, follow_redirects=False
    )
    assert r.status_code == 404
    assert "아카이브된 스냅샷이 없습니다" in r.text


def test_go_requires_session(client):
    r = client.get("/go", params={"url": URL}, follow_redirects=False)
    assert r.status_code in (302, 307)
    assert "/login" in r.headers["location"]
