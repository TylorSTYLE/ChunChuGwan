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
    """API 키 발급 헬퍼 (기본 시스템 키 — DB/auth 계층 테스트용). 보기+아카이브, 영구."""
    options = {
        "can_view": True, "can_archive": True,
        "created_by": None, "ttl_seconds": None,
    }
    options.update(kwargs)
    with db.connect() as conn:
        return auth.issue_api_key(conn, "test-key", **options)


def _admin_id() -> int:
    with db.connect() as conn:
        return db.get_user_by_email(conn, "boss@test.co")["id"]


def _user_token(**kwargs) -> str:
    """admin 귀속 개인 API Key (/api/v1 은 개인 키 전용)."""
    aid = _admin_id()
    return _issue(owner_user_id=aid, created_by=aid, **kwargs)


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
    token = _user_token()
    r = client.get("/api/v1/pages", headers={"X-API-Key": token})
    assert r.status_code == 200


def test_api_updates_last_used(client):
    token = _user_token()
    client.get("/api/v1/pages", headers=_headers(token))
    with db.connect() as conn:
        key = auth.resolve_api_key(conn, token)
        assert key["last_used_at"] is not None


def test_touch_api_key_throttles_within_window(tmp_db):
    """스로틀 창 이내의 두 번째 touch 는 last_used_at 을 다시 쓰지 않는다."""
    token = _issue()
    with db.connect() as conn:
        key = auth.resolve_api_key(conn, token)
        db.touch_api_key(conn, key["id"])
    with db.connect() as conn:
        first = auth.resolve_api_key(conn, token)["last_used_at"]
    assert first is not None
    # 곧바로 다시 touch — 창 이내라 값이 그대로다 (쓰기 생략)
    with db.connect() as conn:
        db.touch_api_key(conn, key["id"])
        second = auth.resolve_api_key(conn, token)["last_used_at"]
    assert second == first

    # 마지막 사용 시각을 창 밖(과거)으로 돌리면 다시 갱신된다
    with db.connect() as conn:
        conn.execute(
            "UPDATE api_keys SET last_used_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
            (key["id"],),
        )
        db.touch_api_key(conn, key["id"])
        refreshed = auth.resolve_api_key(conn, token)["last_used_at"]
    assert refreshed != "2000-01-01T00:00:00+00:00"


def test_api_open_when_auth_disabled(client, monkeypatch):
    monkeypatch.setattr(config, "AUTH_ENABLED", False)
    assert client.get("/api/v1/pages").status_code == 200


# ---- 조회 (보기 권한) ----


def test_api_pages_list_and_url_filter(client):
    token = _user_token()
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
    token = _user_token()
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
    # 개인 키 권한은 소유자 역할에서 재평가 — view 없는 사용자라 403 (use_api_keys 만 부여).
    with db.connect() as conn:
        uid = db.create_user(conn, "noview@test.co", password_hash="x", role="viewer")
        db.set_permission_overrides(conn, uid, {"use_api_keys": True, "view": False})
    token = _issue(owner_user_id=uid, created_by=uid)
    assert client.get("/api/v1/pages", headers=_headers(token)).status_code == 403
    assert client.get("/api/v1/pages/1", headers=_headers(token)).status_code == 403
    assert client.get("/api/v1/snapshots/1", headers=_headers(token)).status_code == 403
    assert (
        client.get("/api/v1/snapshots/1/file/content.md", headers=_headers(token))
        .status_code == 403
    )


# ---- 아카이빙 트리거 (아카이브 권한) ----


def test_api_archive_triggers_pipeline(client):
    token = _user_token()
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
    token = _user_token()
    r = client.post(
        "/api/v1/archive", json={"url": "ftp://example.com/x"}, headers=_headers(token)
    )
    assert r.status_code == 400


def test_api_archive_denied_without_can_archive(client):
    # 개인 키 권한은 소유자 역할에서 재평가 — archive 없는 viewer 라 403 (use_api_keys 부여).
    with db.connect() as conn:
        uid = db.create_user(conn, "noarch@test.co", password_hash="x", role="viewer")
        db.set_permission_overrides(conn, uid, {"use_api_keys": True})
    token = _issue(owner_user_id=uid, created_by=uid)
    r = client.post(
        "/api/v1/archive", json={"url": "https://example.com/new"},
        headers=_headers(token),
    )
    assert r.status_code == 403


def test_api_archive_skips_duplicate_in_progress(client):
    token = _user_token()
    with db.connect() as conn:  # 같은 URL 이 이미 큐에 있는 상태
        db.enqueue_archive_job(conn, "https://example.com/busy", source="api")
    r = client.post(
        "/api/v1/archive", json={"url": "https://example.com/busy"},
        headers=_headers(token),
    )
    assert r.status_code == 202
    assert r.json()["queued"] is False


# ---- 관리 화면 ----


