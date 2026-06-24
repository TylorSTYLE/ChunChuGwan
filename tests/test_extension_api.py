"""확장용 REST API 표면 — POST /api/v1/crawl, network_tag, GET /go 딥링크, CSRF 면제."""
import pytest
from fastapi.testclient import TestClient

from chunchugwan import __version__, auth, config, db, netcheck, storage
from chunchugwan.web import app as web_app

URL = "https://example.com/post"
ING_PAGE = "<!DOCTYPE html><html><body><h1>적재 제목</h1><p>본문입니다.</p></body></html>"
ING_RAW = "<html><body><h1>적재 제목</h1><p>본문입니다.</p></body></html>"


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
    """admin 귀속 개인 API Key (/api/v1 은 개인 키 전용). 권한 제어 테스트는
    owner_user_id 로 다른 역할 사용자를, 시스템 키 거부 테스트는 owner_user_id=None 을 준다."""
    aid = _admin_id()
    options = {"can_view": True, "can_archive": True,
               "owner_user_id": aid, "created_by": aid, "ttl_seconds": None}
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


# ---- GET /api/v1/version ----


def test_api_version_returns_server_version(client):
    token = _issue()
    r = client.get("/api/v1/version", headers=_headers(token))
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == __version__
    assert "extension_version" in body  # 확장 버전(서버 앱 버전과 독립)


def test_api_version_requires_token(client):
    assert client.get("/api/v1/version").status_code == 401


def test_api_invalid_token_ip_throttled(client):
    """무효 토큰 반복 인증 실패 → IP 인증보호로 429 (auth_throttle, api_key_ip 버킷)."""
    with db.connect() as conn:
        limit = db.auth_throttle_settings(conn)["login_ip_limit"]
    last = None
    for _ in range(limit + 1):
        last = client.get("/api/v1/version", headers=_headers("wccg_bad_token"))
    assert last.status_code == 429
    assert "retry-after" in {k.lower() for k in last.headers}


def test_api_system_key_rejected(client):
    """시스템 키(owner=NULL)는 /api/v1 인증 대상이 아니다 — 401 (개인 키 전용)."""
    token = _issue(owner_user_id=None, created_by=None)
    assert client.get("/api/v1/version", headers=_headers(token)).status_code == 401


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
    # 개인 키 권한은 소유자 역할에서 재평가 — archive 없는 viewer 라 403 (use_api_keys 는 부여).
    with db.connect() as conn:
        uid = db.create_user(conn, "viewer-crawl@test.co", password_hash="x", role="viewer")
        db.set_permission_overrides(conn, uid, {"use_api_keys": True})
    token = _issue(owner_user_id=uid, created_by=uid)
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
        "/settings/api-keys",
        data={"name": "x", "expiry": "permanent"},
        headers={"Origin": "https://evil.example"}, follow_redirects=False,
    )
    assert r.status_code == 403  # 세션 POST 는 Origin 검사 유지


# ---- GET /go 딥링크 ----


# ---- POST /api/v1/ingest (확장 클라이언트 캡처 적재) ----


def _admin_id() -> int:
    with db.connect() as conn:
        return db.get_user_by_email(conn, "boss@test.co")["id"]


def _user_token(**kwargs) -> str:
    """admin 에게 귀속된 사용자 확장 토큰 (ingest 요구사항)."""
    return _issue(owner_user_id=_admin_id(), created_by=_admin_id(), **kwargs)


def _ingest_files():
    return {
        "page_html": ("page.html", ING_PAGE, "text/html"),
        "raw_html": ("raw.html", ING_RAW, "text/html"),
    }


def test_api_ingest_creates_extension_snapshot(client):
    token = _user_token()
    r = client.post(
        "/api/v1/ingest", data={"url": "https://example.com/ingested"},
        files=_ingest_files(), headers=_headers(token),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "new" and body["snapshot_id"] is not None
    with db.connect() as conn:
        snap = conn.execute(
            "SELECT * FROM snapshots WHERE id=?", (body["snapshot_id"],)).fetchone()
        page = conn.execute("SELECT * FROM pages WHERE id=?", (body["page_id"],)).fetchone()
        log = conn.execute("SELECT * FROM archive_logs ORDER BY id DESC LIMIT 1").fetchone()
    assert snap["origin"] == "extension" and page["client_captured"] == 1
    assert log["source"] == "extension" and log["requested_by"] == _admin_id()


def test_api_ingest_applies_protect(client):
    """protect=False 면 적재된 페이지가 공유 허용(cluster_protect=0)으로 표시된다."""
    token = _user_token()
    r = client.post(
        "/api/v1/ingest",
        data={"url": "https://example.com/shared", "protect": "false"},
        files=_ingest_files(), headers=_headers(token),
    )
    assert r.status_code == 200, r.text
    with db.connect() as conn:
        page = conn.execute(
            "SELECT cluster_protect FROM pages WHERE id=?", (r.json()["page_id"],)
        ).fetchone()
    assert page["cluster_protect"] == 0


def test_api_ingest_requires_user_token(client):
    """시스템 키(owner=NULL)는 /api/v1 인증 자체가 거부된다 — 401 (개인 키 전용)."""
    token = _issue(owner_user_id=None, created_by=None)
    r = client.post(
        "/api/v1/ingest", data={"url": "https://example.com/x"},
        files=_ingest_files(), headers=_headers(token),
    )
    assert r.status_code == 401


def test_api_ingest_requires_archive_perm(client):
    """사용자 토큰 권한은 소유자 현재 역할에서 재평가 — 아카이브 권한 없으면 403.

    개인 API Key 사용 권한(use_api_keys)은 줘서 토큰 자체는 유효하게 하되,
    아카이브 권한이 없는 viewer 라 아카이빙(ingest)만 막힌다.
    """
    with db.connect() as conn:
        uid = db.create_user(conn, "viewer@test.co", password_hash="x", role="viewer")
        db.set_permission_overrides(conn, uid, {"use_api_keys": True})
    token = _issue(owner_user_id=uid, created_by=uid)
    r = client.post(
        "/api/v1/ingest", data={"url": "https://example.com/x"},
        files=_ingest_files(), headers=_headers(token),
    )
    assert r.status_code == 403


def test_api_token_requires_use_api_keys_permission(client):
    """개인 API Key 사용 권한이 없는 소유자의 토큰은 401 (사용 자체가 차단)."""
    with db.connect() as conn:
        uid = db.create_user(conn, "noapi@test.co", password_hash="x", role="viewer")
    token = _issue(owner_user_id=uid, created_by=uid)
    assert client.get("/api/v1/pages", headers=_headers(token)).status_code == 401


def test_api_ingest_size_cap(client, monkeypatch):
    monkeypatch.setattr(config, "INGEST_MAX_BYTES", 10)  # 작은 상한 → 정상 업로드도 초과
    monkeypatch.setattr(config, "INGEST_MAX_MB", 0)
    token = _user_token()
    r = client.post(
        "/api/v1/ingest", data={"url": "https://example.com/x"},
        files=_ingest_files(), headers=_headers(token),
    )
    assert r.status_code == 413


def test_api_ingest_private_needs_tag(client, monkeypatch):
    monkeypatch.setattr(netcheck, "classify_host", lambda h: netcheck.PRIVATE)
    token = _user_token()
    r = client.post(
        "/api/v1/ingest", data={"url": "https://intranet.example/x"},
        files=_ingest_files(), headers=_headers(token),
    )
    assert r.status_code == 422
    assert r.json()["detail"]["needs_network_tag"] is True


def test_api_network_tags_list_and_create(client):
    token = _user_token()  # admin → manage_system 보유
    created = client.post(
        "/api/v1/network-tags", json={"name": "사무실", "description": "본사"},
        headers=_headers(token),
    )
    assert created.status_code == 201
    tag_id = created.json()["id"]
    listed = client.get("/api/v1/network-tags", headers=_headers(token))
    assert listed.status_code == 200
    assert any(t["id"] == tag_id and t["name"] == "사무실" for t in listed.json()["tags"])


def test_api_network_tags_create_requires_manage_system(client):
    """archiver 는 태그 생성 불가(403)이나 목록 조회는 가능(아카이브 권한)."""
    with db.connect() as conn:
        uid = db.create_user(conn, "arch@test.co", password_hash="x", role="archiver")
    token = _issue(owner_user_id=uid, created_by=uid)
    r = client.post(
        "/api/v1/network-tags", json={"name": "데이터센터"}, headers=_headers(token)
    )
    assert r.status_code == 403
    assert client.get("/api/v1/network-tags", headers=_headers(token)).status_code == 200
