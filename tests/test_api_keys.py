"""API 키 — 발급/만료(DB·auth 계층), REST API 인증/권한, 관리 화면 테스트."""
import pytest
from fastapi.testclient import TestClient

from chunchugwan import auth, config, db, storage
from chunchugwan.web import app as web_app


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """임시 아카이브 DB 환경 (인증은 기본값 on)."""
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "RESOURCES_DIR", tmp_path / "resources")
    monkeypatch.setattr(config, "DOCUMENTS_DIR", tmp_path / "documents")


URL = "https://example.com/post"


@pytest.fixture
def client(tmp_db):
    """관리자 + 보기 전용 사용자 + 스냅샷 1개가 있는 TestClient."""
    domain, slug = "example.com", storage.url_to_slug(URL)
    with db.connect() as conn:
        db.create_first_admin(conn, "boss@test.co", auth.hash_password("bosspass1234"))
        db.create_user(
            conn, "viewer@test.co", auth.hash_password("password1234"), role="viewer"
        )
        page_id = db.get_or_create_page(conn, URL, domain, slug)
        snap_dir = storage.page_dir(domain, slug) / "2026-06-01T00-00-00"
        snap_dir.mkdir(parents=True)
        (snap_dir / "content.md").write_text("본문 텍스트", encoding="utf-8")
        db.insert_snapshot(
            conn, page_id,
            taken_at="2026-06-01T00:00:00+00:00", dir_name="2026-06-01T00-00-00",
            content_hash="h1", final_url=URL, http_status=200, changed=1,
        )
    web_app._active_jobs.clear()
    yield TestClient(web_app.app)
    web_app._active_jobs.clear()


def _issue(**kwargs) -> str:
    """API 키 발급 헬퍼 — 기본은 보기+아카이브, 영구."""
    options = {
        "can_view": True, "can_archive": True,
        "created_by": None, "ttl_seconds": None,
    }
    options.update(kwargs)
    with db.connect() as conn:
        return auth.issue_api_key(conn, "test-key", **options)


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _login_admin(client):
    return client.post(
        "/login", data={"email": "boss@test.co", "password": "bosspass1234"},
        follow_redirects=False,
    )


# ---- DB / auth 계층 ----


def test_issue_and_resolve(tmp_db):
    token = _issue()
    assert token.startswith(auth.API_KEY_PREFIX)
    with db.connect() as conn:
        key = auth.resolve_api_key(conn, token)
        assert key is not None
        assert key["name"] == "test-key"
        assert key["can_view"] == 1 and key["can_archive"] == 1
        assert key["expires_at"] is None
        # 원문은 저장되지 않는다 — 해시·표시용 prefix 만
        assert key["token_hash"] == auth.hash_token(token)
        assert key["prefix"] == token[: auth.API_KEY_DISPLAY_CHARS]


def test_resolve_rejects_wrong_or_expired(tmp_db):
    token = _issue(ttl_seconds=-1)  # 이미 만료
    with db.connect() as conn:
        assert auth.resolve_api_key(conn, token) is None
        assert auth.resolve_api_key(conn, "wccg_nonexistent") is None
        assert auth.resolve_api_key(conn, "not-a-key") is None


def test_expiring_key_has_expires_at(tmp_db):
    token = _issue(ttl_seconds=86400)
    with db.connect() as conn:
        key = auth.resolve_api_key(conn, token)
        assert key["expires_at"] is not None


def test_delete_api_key_revokes(tmp_db):
    token = _issue()
    with db.connect() as conn:
        key = auth.resolve_api_key(conn, token)
        assert db.delete_api_key(conn, key["id"]) is True
        assert auth.resolve_api_key(conn, token) is None
        assert db.delete_api_key(conn, key["id"]) is False


# ---- REST API 인증 ----


def test_api_requires_key(client):
    assert client.get("/api/v1/pages").status_code == 401
    assert client.get("/api/v1/pages", headers=_headers("wccg_bad")).status_code == 401


def test_api_session_cookie_is_not_enough(client):
    """웹 세션 쿠키로는 API 에 접근할 수 없다 — 키 전용 경로."""
    _login_admin(client)
    assert client.get("/api/v1/pages").status_code == 401


def test_api_key_via_x_api_key_header(client):
    token = _issue()
    r = client.get("/api/v1/pages", headers={"X-API-Key": token})
    assert r.status_code == 200


def test_api_updates_last_used(client):
    token = _issue()
    client.get("/api/v1/pages", headers=_headers(token))
    with db.connect() as conn:
        key = auth.resolve_api_key(conn, token)
        assert key["last_used_at"] is not None


def test_api_open_when_auth_disabled(client, monkeypatch):
    monkeypatch.setattr(config, "AUTH_ENABLED", False)
    assert client.get("/api/v1/pages").status_code == 200


# ---- 조회 (보기 권한) ----


def test_api_pages_list_and_url_filter(client):
    token = _issue()
    body = client.get("/api/v1/pages", headers=_headers(token)).json()
    assert len(body["pages"]) == 1
    assert body["pages"][0]["url"] == URL
    assert body["pages"][0]["snapshot_count"] == 1

    # url 쿼리는 정규화 후 일치 — 트래킹 파라미터가 붙어도 같은 페이지
    r = client.get(
        "/api/v1/pages", params={"url": URL + "?utm_source=x"}, headers=_headers(token)
    )
    assert len(r.json()["pages"]) == 1
    r = client.get(
        "/api/v1/pages", params={"url": "https://other.com/"}, headers=_headers(token)
    )
    assert r.json()["pages"] == []


def test_api_page_detail_and_snapshot(client):
    token = _issue()
    page_id = client.get("/api/v1/pages", headers=_headers(token)).json()["pages"][0]["id"]
    body = client.get(f"/api/v1/pages/{page_id}", headers=_headers(token)).json()
    assert body["url"] == URL
    assert len(body["snapshots"]) == 1
    snap = body["snapshots"][0]
    assert snap["content_hash"] == "h1"

    detail = client.get(f"/api/v1/snapshots/{snap['id']}", headers=_headers(token)).json()
    assert detail["page_url"] == URL

    r = client.get(snap["files"]["content.md"], headers=_headers(token))
    assert r.status_code == 200
    assert "본문 텍스트" in r.text


def test_api_view_denied_without_can_view(client):
    token = _issue(can_view=False)
    assert client.get("/api/v1/pages", headers=_headers(token)).status_code == 403
    assert client.get("/api/v1/pages/1", headers=_headers(token)).status_code == 403
    assert client.get("/api/v1/snapshots/1", headers=_headers(token)).status_code == 403
    assert (
        client.get("/api/v1/snapshots/1/file/content.md", headers=_headers(token))
        .status_code == 403
    )


# ---- 아카이빙 트리거 (아카이브 권한) ----


def test_api_archive_triggers_pipeline(client):
    token = _issue()
    r = client.post(
        "/api/v1/archive", json={"url": "https://example.com/new?utm_source=x"},
        headers=_headers(token),
    )
    assert r.status_code == 202
    body = r.json()
    assert body["queued"] is True and body["url"] == "https://example.com/new"
    # 확장이 결과를 추적할 수 있게 큐에 등록된 작업 id 를 함께 돌려준다
    assert isinstance(body["job_id"], int)
    # 정규화된 URL 로 source='api' 작업이 큐에 등록된다 (worker 가 캡처)
    with db.connect() as conn:
        jobs = conn.execute("SELECT url, force, source FROM archive_jobs").fetchall()
    assert [(j["url"], j["force"], j["source"]) for j in jobs] == [
        ("https://example.com/new", 0, "api")
    ]


def test_api_archive_rejects_invalid_url(client):
    token = _issue()
    r = client.post(
        "/api/v1/archive", json={"url": "ftp://example.com/x"}, headers=_headers(token)
    )
    assert r.status_code == 400


def test_api_archive_denied_without_can_archive(client):
    token = _issue(can_archive=False)
    r = client.post(
        "/api/v1/archive", json={"url": "https://example.com/new"},
        headers=_headers(token),
    )
    assert r.status_code == 403


def test_api_archive_skips_duplicate_in_progress(client):
    token = _issue()
    with db.connect() as conn:  # 같은 URL 이 이미 큐에 있는 상태
        db.enqueue_archive_job(conn, "https://example.com/busy", source="api")
    r = client.post(
        "/api/v1/archive", json={"url": "https://example.com/busy"},
        headers=_headers(token),
    )
    assert r.status_code == 202
    assert r.json()["queued"] is False


# ---- 관리 화면 ----


def test_api_keys_page_admin_only(client):
    client.post(
        "/login", data={"email": "viewer@test.co", "password": "password1234"},
        follow_redirects=False,
    )
    assert client.get("/api/v1/pages").status_code == 401  # 세션은 API 와 무관
    assert client.get("/system/api-keys").status_code == 403
    assert (
        client.post("/system/api-keys", data={"name": "x", "can_view": "on"})
        .status_code == 403
    )


def test_api_keys_page_renders(client):
    _issue(ttl_seconds=86400)
    _login_admin(client)
    res = client.get("/system/api-keys", params={"new_key": "wccg_demo123"})
    assert res.status_code == 200
    assert "test-key" in res.text          # 발급된 키 목록
    assert "wccg_demo123" in res.text      # 방금 발급된 키 원문 (1회 표시)
    assert 'action="/system/api-keys"' in res.text  # 발급 폼
    for label in ("영구", "1일", "1개월", "1년", "사용자 지정"):
        assert label in res.text


def test_admin_creates_key_via_ui(client):
    _login_admin(client)
    r = client.post(
        "/system/api-keys",
        data={"name": "rss-bot", "can_view": "on", "expiry": "1d"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "new_key=wccg_" in r.headers["location"]
    with db.connect() as conn:
        keys = db.list_api_keys(conn)
    assert len(keys) == 1
    assert keys[0]["name"] == "rss-bot"
    assert keys[0]["can_view"] == 1 and keys[0]["can_archive"] == 0
    assert keys[0]["expires_at"] is not None
    assert keys[0]["creator_email"] == "boss@test.co"


def test_admin_creates_permanent_and_custom_expiry(client):
    _login_admin(client)
    client.post(
        "/system/api-keys",
        data={"name": "forever", "can_archive": "on", "expiry": "permanent"},
        follow_redirects=False,
    )
    client.post(
        "/system/api-keys",
        data={"name": "custom", "can_view": "on", "expiry": "custom",
              "custom_days": "7"},
        follow_redirects=False,
    )
    with db.connect() as conn:
        keys = {k["name"]: k for k in db.list_api_keys(conn)}
    assert keys["forever"]["expires_at"] is None
    assert keys["custom"]["expires_at"] is not None


def test_create_key_rejects_bad_input(client):
    _login_admin(client)
    # 권한 미선택
    r = client.post(
        "/system/api-keys", data={"name": "x", "expiry": "permanent"},
        follow_redirects=False,
    )
    assert "error=" in r.headers["location"]
    # 사용자 지정 일 수 범위 밖
    r = client.post(
        "/system/api-keys",
        data={"name": "x", "can_view": "on", "expiry": "custom", "custom_days": "0"},
        follow_redirects=False,
    )
    assert "error=" in r.headers["location"]
    with db.connect() as conn:
        assert db.list_api_keys(conn) == []


def test_admin_deletes_key_via_ui(client):
    token = _issue()
    _login_admin(client)
    with db.connect() as conn:
        key_id = db.list_api_keys(conn)[0]["id"]
    r = client.post(f"/system/api-keys/{key_id}/delete", follow_redirects=False)
    assert r.status_code == 303
    with db.connect() as conn:
        assert auth.resolve_api_key(conn, token) is None
