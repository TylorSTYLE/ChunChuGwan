"""C2 컷오버 — SPA 보강 엔드포인트 테스트.

빅뱅 컷오버로 SSR 을 제거하면서 SPA 에 짝이 없던 기능을 /api/web 으로 옮긴 것:
초대 수락(/api/web/auth/invite), 실패 작업 재시도(logs·site failed·retry-all),
사이트 단위 내보내기(/api/web/sites/{id}/export). SSR 핸들러와 동일 계약인지 검증한다.
"""
import pytest
from fastapi.testclient import TestClient

from chunchugwan import auth, config, db
from chunchugwan.web import app as web_app

POST_HEADERS = {"X-Requested-With": "fetch", "Origin": "http://testserver"}


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "RESOURCES_DIR", tmp_path / "resources")
    monkeypatch.setattr(config, "DOCUMENTS_DIR", tmp_path / "documents")
    monkeypatch.setattr(config, "AUTH_ENABLED", True)


def make_user(email="u@test.co", password="userpass123", role="archiver"):
    with db.connect() as conn:
        pw = auth.hash_password(password) if password else None
        uid = db.create_user(conn, email, pw, role=role)
        token = auth.issue_session(conn, uid)
    return uid, token


def client_for(token=None):
    c = TestClient(web_app.app)
    if token:
        c.cookies.set(config.SESSION_COOKIE, token)
    return c


def make_invite(email="invitee@test.co", role="viewer", token="invtok-abcdef123456"):
    """원문 토큰으로 초대를 만들고 원문 토큰을 반환 (저장은 해시)."""
    with db.connect() as conn:
        admin_id = db.create_user(conn, "admin@test.co", auth.hash_password("adminpass123"), role="admin")
        db.create_invite(conn, email, auth.hash_token(token), role, admin_id, ttl_seconds=86400)
    return token


# ---- 초대 수락 (미인증, /api/web/auth/invite) ----


def test_invite_status_valid(tmp_db):
    token = make_invite()
    r = client_for().get(f"/api/web/auth/invite/{token}")
    assert r.status_code == 200
    assert r.json()["email"] == "invitee@test.co"


def test_invite_status_invalid(tmp_db):
    make_invite()
    r = client_for().get("/api/web/auth/invite/wrong-token")
    assert r.status_code == 404


def test_invite_accept_creates_user_and_session(tmp_db):
    token = make_invite(email="newbie@test.co", role="viewer")
    r = client_for().post(
        f"/api/web/auth/invite/{token}",
        json={"password": "newpass12345"}, headers=POST_HEADERS,
    )
    assert r.status_code == 200
    assert r.json()["status"] == "active"
    assert config.SESSION_COOKIE in r.cookies  # 즉시 로그인
    with db.connect() as conn:
        user = db.get_user_by_email(conn, "newbie@test.co")
        assert user is not None and user["role"] == "viewer"
        assert db.get_invite_by_token(conn, auth.hash_token(token)) is None  # 1회용 소진


def test_invite_accept_short_password(tmp_db):
    token = make_invite()
    r = client_for().post(
        f"/api/web/auth/invite/{token}",
        json={"password": "short"}, headers=POST_HEADERS,
    )
    assert r.status_code == 400


def test_invite_accept_already_registered(tmp_db):
    token = make_invite(email="dup@test.co")
    make_user(email="dup@test.co")  # 초대 후 같은 이메일이 일반 가입
    r = client_for().post(
        f"/api/web/auth/invite/{token}",
        json={"password": "newpass12345"}, headers=POST_HEADERS,
    )
    assert r.status_code == 400
    with db.connect() as conn:  # 무효 초대는 정리된다
        assert db.get_invite_by_token(conn, auth.hash_token(token)) is None


# ---- 로그 실패 재시도 (/api/web/logs/{id}/retry) ----


def _error_log(url="https://example.com/x", requester=None, status="error"):
    with db.connect() as conn:
        return db.insert_archive_log(
            conn, url=url, domain="example.com", status=status,
            started_at="2026-06-01T00:00:00+00:00", requested_by=requester,
        )


def test_log_retry_error_enqueues(tmp_db):
    uid, token = make_user(role="archiver")
    log_id = _error_log(requester=uid)
    r = client_for(token).post(f"/api/web/logs/{log_id}/retry", headers=POST_HEADERS)
    assert r.status_code == 200 and r.json()["queued"] is True


def test_log_retry_non_error_rejected(tmp_db):
    _, token = make_user(role="archiver")
    log_id = _error_log(status="new")
    r = client_for(token).post(f"/api/web/logs/{log_id}/retry", headers=POST_HEADERS)
    assert r.status_code == 400


def test_log_retry_missing(tmp_db):
    _, token = make_user(role="archiver")
    r = client_for(token).post("/api/web/logs/999/retry", headers=POST_HEADERS)
    assert r.status_code == 404


# ---- 사이트 실패 재시도 · 내보내기 ----


def _site_page_with_failed_log(url="https://shop.example.com/p"):
    """사이트 소속 페이지 + 최신 실패 로그를 만들고 (site_id, log_id) 반환."""
    with db.connect() as conn:
        pid = db.get_or_create_page(conn, url, "shop.example.com", "p")
        page = db.get_page_by_id(conn, pid)
        log_id = db.insert_archive_log(
            conn, url=url, domain="shop.example.com", status="error",
            started_at="2026-06-01T00:00:00+00:00", page_id=pid,
        )
    return page["site_id"], log_id


def test_site_failed_retry(tmp_db):
    _, token = make_user(role="archiver")
    site_id, log_id = _site_page_with_failed_log()
    r = client_for(token).post(
        f"/api/web/sites/{site_id}/failed/{log_id}/retry", headers=POST_HEADERS
    )
    assert r.status_code == 200 and r.json()["queued"] is True


def test_site_failed_retry_missing(tmp_db):
    _, token = make_user(role="archiver")
    site_id, _ = _site_page_with_failed_log()
    r = client_for(token).post(
        f"/api/web/sites/{site_id}/failed/99999/retry", headers=POST_HEADERS
    )
    assert r.status_code == 404


def test_site_failed_retry_all(tmp_db):
    _, token = make_user(role="archiver")
    site_id, _ = _site_page_with_failed_log()
    r = client_for(token).post(
        f"/api/web/sites/{site_id}/failed/retry-all", headers=POST_HEADERS
    )
    assert r.status_code == 200
    assert r.json()["queued"] == 1


def test_site_export(tmp_db):
    _, token = make_user(role="archiver")
    site_id, _ = _site_page_with_failed_log()
    r = client_for(token).post(f"/api/web/sites/{site_id}/export", headers=POST_HEADERS)
    assert r.status_code == 200
    assert ".ccg.export" in r.headers.get("content-disposition", "")


def test_retry_requires_archive_permission(tmp_db):
    """viewer 는 재시도 권한이 없다 (403)."""
    _, token = make_user(email="v@test.co", role="viewer")
    log_id = _error_log()
    r = client_for(token).post(f"/api/web/logs/{log_id}/retry", headers=POST_HEADERS)
    assert r.status_code == 403
