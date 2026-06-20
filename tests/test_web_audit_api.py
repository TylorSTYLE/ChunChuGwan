"""감사 로그(/api/web/audit) + 로그 열람 권한 3종 — 신규 기능 검증.

- view_audit_logs / view_system_logs / view_archive_logs 는 기본 admin 만 보유.
- 감사 로그는 전용 audit_logs 테이블에 적재되고 system_logs 와 분리된다.
"""
import pytest
from fastapi.testclient import TestClient

from chunchugwan import auth, config, db
from chunchugwan.web import app as web_app

POST_HEADERS = {"X-Requested-With": "fetch", "Origin": "http://testserver"}


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    for name, sub in (
        ("ARCHIVE_ROOT", ""), ("SITES_DIR", "sites"), ("DB_PATH", "index.db"),
        ("CACHE_DIR", "cache"), ("RESOURCES_DIR", "resources"),
        ("DOCUMENTS_DIR", "documents"),
    ):
        monkeypatch.setattr(config, name, tmp_path / sub if sub else tmp_path)
    monkeypatch.setattr(config, "AUTH_ENABLED", True)


def _user(email, role):
    with db.connect() as conn:
        if role == "founder":
            uid = db.create_first_admin(conn, email, auth.hash_password("password1234"))
        else:
            uid = db.create_user(conn, email, auth.hash_password("password1234"), role=role)
        return db.issue_session(conn, uid) if hasattr(db, "issue_session") else auth.issue_session(conn, uid)


def _client(token=None):
    c = TestClient(web_app.app)
    if token:
        c.cookies.set(config.SESSION_COOKIE, token)
    return c


def test_admin_has_log_view_permissions(tmp_db):
    """관리자 그룹은 로그 열람 3종을 기본 보유한다."""
    with db.connect() as conn:
        db.create_first_admin(conn, "boss@test.co", auth.hash_password("x" * 12))
        presets = db.role_presets(conn)
    assert {"view_audit_logs", "view_system_logs", "view_archive_logs"} <= presets["admin"]
    # 다른 빌트인은 미보유
    for role in ("archive_manager", "archiver", "viewer"):
        assert not (
            {"view_audit_logs", "view_system_logs", "view_archive_logs"}
            & presets.get(role, frozenset())
        )


def test_audit_endpoint_admin_only(tmp_db):
    admin_t = _user("boss@test.co", "founder")
    viewer_t = _user("v@test.co", "viewer")
    assert _client(admin_t).get("/api/web/audit").status_code == 200
    assert _client(viewer_t).get("/api/web/audit").status_code == 403


def test_log_pages_admin_only(tmp_db):
    viewer_t = _user("v@test.co", "viewer")
    c = _client(viewer_t)
    assert c.get("/api/web/logs").status_code == 403
    assert c.get("/api/web/system/logs").status_code == 403


def test_me_flags_include_log_views(tmp_db):
    admin_t = _user("boss@test.co", "founder")
    flags = _client(admin_t).get("/api/web/me").json()["flags"]
    assert flags["can_view_audit_logs"] and flags["can_view_system_logs"]
    assert flags["can_view_archive_logs"] and flags["can_view_any_logs"]
    viewer_t = _user("v@test.co", "viewer")
    vflags = _client(viewer_t).get("/api/web/me").json()["flags"]
    assert not vflags["can_view_any_logs"]


def test_archive_action_audited_and_separated(tmp_db, monkeypatch):
    """새 아카이빙이 action='archive' 로 audit_logs 에 남고 system_logs 엔 안 남는다."""
    from chunchugwan import system_log
    admin_t = _user("boss@test.co", "founder")
    system_log.install("serve")
    try:
        r = _client(admin_t).post(
            "/api/web/archive", json={"url": "https://example.com/x"},
            headers=POST_HEADERS,
        )
        assert r.status_code == 202
        system_log.flush()
        with db.connect() as conn:
            audits = db.list_audit_logs(conn, action="archive")
            sys_rows = db.list_system_logs(conn, limit=100)
    finally:
        system_log.uninstall()
    assert any("example.com/x" in a["message"] for a in audits)
    assert not [r for r in sys_rows if r["logger"] == "chunchugwan.web.audit"]


def test_audit_default_page_size(tmp_db):
    """감사 로그 기본 페이지 크기 25, 선택지 10/25/50/100 (이전 기본 50 → 25)."""
    admin_t = _user("boss@test.co", "founder")
    c = _client(admin_t)
    body = c.get("/api/web/audit").json()
    assert body["limit"] == 25 and body["limits"] == [10, 25, 50, 100]
    assert body["page_num"] == 1
    assert c.get("/api/web/audit?limit=10").json()["limit"] == 10  # 10 허용
    assert c.get("/api/web/audit?limit=37").json()["limit"] == 25  # 허용 밖 → 25
