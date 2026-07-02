"""app.py 직접 정의 /api/web 자원 라우트의 인증 게이트 (보안 검토 V1).

라우터(web_api_routes)는 require_session 의존성으로 막지만, app.py 의
@app.get("/api/web/...") 라우트는 각자 _require_viewer 를 호출해야 한다. 진행 중
아카이빙·크롤 상세/상태가 무인증 노출되지 않는지 검증한다.
"""
import pytest
from fastapi.testclient import TestClient

from chunchugwan import auth, config, crawler, db
from chunchugwan.web import app as web_app

POST = {"X-Requested-With": "fetch", "Origin": "http://testserver"}


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "RESOURCES_DIR", tmp_path / "resources")
    monkeypatch.setattr(config, "DOCUMENTS_DIR", tmp_path / "documents")
    monkeypatch.setattr(config, "AUTH_ENABLED", True)
    monkeypatch.delenv("WCCG_ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("WCCG_ADMIN_PASSWORD", raising=False)
    web_app._active_jobs.clear()
    with db.connect():
        pass
    yield
    web_app._active_jobs.clear()


def client():
    return TestClient(web_app.app)


def _crawl_id() -> int:
    crawl, _ = crawler.start_crawl("https://example.com/docs/", source="web")
    return crawl["id"]


def _viewer_login(c):
    with db.connect() as conn:
        db.create_user(conn, "v@test.co", auth.hash_password("userpass123"), role="viewer")
    r = c.post("/api/web/auth/login",
               json={"email": "v@test.co", "password": "userpass123"}, headers=POST)
    assert r.status_code == 200


def _pending_login(c):
    with db.connect() as conn:
        db.create_user(conn, "p@test.co", auth.hash_password("userpass123"), role="pending")
    r = c.post("/api/web/auth/login",
               json={"email": "p@test.co", "password": "userpass123"}, headers=POST)
    assert r.status_code == 200  # pending 도 active 세션은 발급된다


# ---- 미인증 차단 (V1) ----


def test_active_requires_auth(tmp_db):
    assert client().get("/api/web/active").status_code == 401


def test_crawl_view_requires_auth(tmp_db):
    cid = _crawl_id()
    assert client().get(f"/api/web/crawls/{cid}").status_code == 401


def test_crawl_status_requires_auth(tmp_db):
    cid = _crawl_id()
    assert client().get(f"/api/web/crawls/{cid}/status").status_code == 401


def test_extension_redirects_require_auth(tmp_db):
    cid = _crawl_id()
    c = client()
    assert c.get(f"/extension/crawl/{cid}", follow_redirects=False).status_code == 401
    assert c.get("/extension/page/1", follow_redirects=False).status_code == 401
    # /extension/go 는 DB 를 조회해 결과별로 리다이렉트하므로 미인증 오라클 방지 가드 필요
    assert c.get(
        "/extension/go?url=https://example.com/x", follow_redirects=False
    ).status_code == 401


# ---- 승인 대기(pending) 계정 차단 (H1 — _require_viewer 는 view 권한 요구) ----


def test_pending_blocked_from_resource_routes(tmp_db):
    """pending 은 active 세션이 있어도 아카이브 자원(_require_viewer)에 접근 못 한다.

    auth_gate 의 pending 403 은 /api/ 에만 걸리므로, 루트 자원 라우트(확장 딥링크·
    스냅샷 파일·문서·diff)는 _require_viewer 가 직접 막아야 한다.
    """
    cid = _crawl_id()
    c = client()
    _pending_login(c)
    # 비-/api 자원 라우트 — pending 은 302 리다이렉트(정상 뷰어)가 아니라 401
    assert c.get(f"/extension/crawl/{cid}", follow_redirects=False).status_code == 401
    assert c.get("/extension/page/1", follow_redirects=False).status_code == 401


# ---- 로그인 시 정상 (회귀 없음) ----


def test_routes_ok_when_authenticated(tmp_db):
    cid = _crawl_id()
    c = client()
    _viewer_login(c)
    assert c.get("/api/web/active").status_code == 200
    assert c.get(f"/api/web/crawls/{cid}").status_code == 200
    assert c.get(f"/api/web/crawls/{cid}/status").status_code == 200
    # 확장 리다이렉트는 로그인 시 302
    assert c.get(f"/extension/crawl/{cid}", follow_redirects=False).status_code == 302


# ---- F4: CSRF — /api/web POST 헤더 게이트 ----


def test_csrf_blocks_post_without_origin_or_custom_header(tmp_db):
    # Origin/Referer·X-Requested-With 모두 없는 /api/web POST → 403(CSRF)
    r = client().post("/api/web/auth/login", json={"email": "x@test.co", "password": "p"})
    assert r.status_code == 403


def test_csrf_blocks_cross_origin_post(tmp_db):
    r = client().post(
        "/api/web/auth/login", json={"email": "x@test.co", "password": "p"},
        headers={"Origin": "http://evil.example", "X-Requested-With": "fetch"},
    )
    assert r.status_code == 403


def test_csrf_allows_custom_header_without_origin(tmp_db):
    # 동일 출처 SPA 패턴 — Origin 없이도 커스텀 헤더가 있으면 통과(인증 자체는 401)
    r = client().post(
        "/api/web/auth/login", json={"email": "x@test.co", "password": "p"},
        headers={"X-Requested-With": "fetch"},
    )
    assert r.status_code == 401  # CSRF 통과 후 자격 불일치


def test_csrf_exempt_for_api_v1(tmp_db):
    # /api/v1 은 Bearer/X-API-Key 자격이라 Origin 검사 면제 (쿠키 CSRF 무관)
    r = client().post("/api/v1/archive", json={"url": "https://example.com"})
    assert r.status_code != 403  # 401(키 없음)이지 CSRF 403 아님
