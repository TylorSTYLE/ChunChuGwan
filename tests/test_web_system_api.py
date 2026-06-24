"""SvelteKit SPA 시스템 API(/api/web/system) 테스트 — 개요·설정 6종·시스템 로그·권한 게이트.

Phase C2 컷오버로 제거되는 SSR 시스템 화면(system_routes.system_view/system_logs_view 와
설정 POST)의 검증 로직을 JSON API 기준으로 대체한다. SSR test_system·test_signup_settings·
test_system_logs·test_permissions_granular(manage_system 게이트)의 단정에 대응한다.
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
    web_app._active_jobs.clear()
    yield
    web_app._active_jobs.clear()


def make_user(email="admin@test.co", password="adminpass123", role="admin"):
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


def admin_client():
    _, token = make_user()
    return client_for(token)


# ---- 권한 게이트 ----


def test_system_requires_session(tmp_db):
    make_user()
    assert client_for().get("/api/web/system").status_code == 401


def test_system_requires_manage_system(tmp_db):
    _, token = make_user(email="arch@test.co", role="archiver")
    c = client_for(token)
    assert c.get("/api/web/system").status_code == 403
    assert c.get("/api/web/system/logs").status_code == 403
    # 설정 변경도 manage_system 필요
    r = c.post("/api/web/system/settings",
               json={"signup_enabled": True, "signup_default_role": "pending"},
               headers=POST_HEADERS)
    assert r.status_code == 403


# ---- 개요 ----


def test_system_overview(tmp_db):
    body = admin_client().get("/api/web/system").json()
    assert body["counts"]["users"] == 1
    assert "signup_enabled" in body
    assert "network_tags" in body
    assert body["smtp_config"]["has_password"] is False
    assert "migration_mode" in body
    assert body["version"]
    # 저장 사용량은 GET /system/usage 로 분리됐고, 프론트가 안 읽던 비싼 필드는 제거됨
    # (페이지 진입을 막던 sites/ 전체 스캔 — 개요 응답에서 빠져야 한다).
    assert "usage" not in body
    assert "optimize_pending" not in body
    assert "search" not in body


def test_system_usage(tmp_db):
    body = admin_client().get("/api/web/system/usage").json()
    assert set(body["usage"]) >= {"db", "sites", "resources", "documents"}
    assert all(isinstance(v, int) for v in body["usage"].values())


def test_system_usage_requires_manage_system(tmp_db):
    assert client_for().get("/api/web/system/usage").status_code == 401
    _, arch = make_user(email="arch@test.co", role="archiver")
    assert client_for(arch).get("/api/web/system/usage").status_code == 403


# ---- 가입 설정 ----


def test_signup_settings_roundtrip(tmp_db):
    c = admin_client()
    r = c.post("/api/web/system/settings",
               json={"signup_enabled": True, "signup_default_role": "viewer"},
               headers=POST_HEADERS)
    assert r.status_code == 200
    body = c.get("/api/web/system").json()
    assert body["signup_enabled"] is True
    assert body["signup_default_role"] == "viewer"


def test_signup_settings_bad_role(tmp_db):
    r = admin_client().post(
        "/api/web/system/settings",
        json={"signup_enabled": True, "signup_default_role": "superboss"},
        headers=POST_HEADERS)
    assert r.status_code == 400


# ---- 이메일 인증 설정 ----


def test_email_verification_ttl_range(tmp_db):
    c = admin_client()
    too_big = config.EMAIL_VERIFICATION_TTL_MINUTES_MAX + 1
    assert c.post("/api/web/system/email-verification-settings",
                  json={"email_verification_enabled": True,
                        "email_verification_ttl_minutes": too_big},
                  headers=POST_HEADERS).status_code == 400
    ok = config.EMAIL_VERIFICATION_TTL_MINUTES_MIN
    assert c.post("/api/web/system/email-verification-settings",
                  json={"email_verification_enabled": True,
                        "email_verification_ttl_minutes": ok},
                  headers=POST_HEADERS).status_code == 200
    body = c.get("/api/web/system").json()
    assert body["email_verification_ttl_minutes"] == ok


# ---- 크롤 기본 설정 ----


def test_crawl_settings_validation(tmp_db):
    c = admin_client()
    over = config.CRAWL_MAX_PAGES_LIMIT + 1
    assert c.post("/api/web/system/crawl-settings",
                  json={"crawl_max_pages": over, "crawl_max_depth": 2, "crawl_delay": 5},
                  headers=POST_HEADERS).status_code == 400
    assert c.post("/api/web/system/crawl-settings",
                  json={"crawl_max_pages": 10, "crawl_max_depth": 2, "crawl_delay": 5},
                  headers=POST_HEADERS).status_code == 200
    body = c.get("/api/web/system").json()
    assert body["crawl_defaults"]["max_pages"] == 10


def test_crawl_limits_settings(tmp_db):
    c = admin_client()
    # 상한 설정 저장 → GET /system 에 반영 (재시도 대기도 이 섹션에서 저장)
    assert c.post("/api/web/system/crawl-limits",
                  json={"crawl_max_pages": 20000, "crawl_max_depth": 30,
                        "crawl_max_delay": 7200, "crawl_retry_backoff": "60, 120"},
                  headers=POST_HEADERS).status_code == 200
    body = c.get("/api/web/system").json()
    assert body["crawl_limits"]["max_pages"] == 20000
    assert body["crawl_limits"]["max_depth"] == 30
    assert body["crawl_limits"]["max_delay"] == 7200
    assert body["crawl_retry_backoff"] == "60, 120"
    # 절대 천장(ceiling) 초과는 거부
    over = config.CRAWL_MAX_PAGES_CEILING + 1
    assert c.post("/api/web/system/crawl-limits",
                  json={"crawl_max_pages": over, "crawl_max_depth": 5,
                        "crawl_max_delay": 60, "crawl_retry_backoff": "60"},
                  headers=POST_HEADERS).status_code == 400


def test_crawl_settings_rejects_above_limit(tmp_db):
    """기본값은 설정된 상한을 초과할 수 없다 — 상한을 낮춘 뒤 그 이상 기본값은 거부."""
    c = admin_client()
    assert c.post("/api/web/system/crawl-limits",
                  json={"crawl_max_pages": 100, "crawl_max_depth": 5,
                        "crawl_max_delay": 60, "crawl_retry_backoff": "60"},
                  headers=POST_HEADERS).status_code == 200
    assert c.post("/api/web/system/crawl-settings",
                  json={"crawl_max_pages": 200, "crawl_max_depth": 2, "crawl_delay": 5},
                  headers=POST_HEADERS).status_code == 400


# ---- 자격증명 TTL ----


def test_credential_ttl_range(tmp_db):
    c = admin_client()
    over = config.EXT_CREDENTIAL_TTL_HOURS_MAX + 1
    assert c.post("/api/web/system/credential-settings",
                  json={"ext_credential_ttl_hours": over},
                  headers=POST_HEADERS).status_code == 400
    assert c.post("/api/web/system/credential-settings",
                  json={"ext_credential_ttl_hours": config.EXT_CREDENTIAL_TTL_HOURS_MIN},
                  headers=POST_HEADERS).status_code == 200


# ---- 캡처 설정 ----


def test_capture_settings_roundtrip(tmp_db):
    c = admin_client()
    assert c.post("/api/web/system/capture-settings",
                  json={"mobile_screenshot_enabled": True},
                  headers=POST_HEADERS).status_code == 200
    assert c.get("/api/web/system").json()["mobile_screenshot_enabled"] is True


# ---- 문서 한도 ----


def test_document_settings_range(tmp_db):
    c = admin_client()
    bad = {"document_max_count": config.DOCUMENT_MAX_COUNT_MAX + 1,
           "document_max_mb": 10, "document_fetch_timeout": 30}
    assert c.post("/api/web/system/document-settings", json=bad,
                  headers=POST_HEADERS).status_code == 400
    good = {"document_max_count": config.DOCUMENT_MAX_COUNT_MIN,
            "document_max_mb": config.DOCUMENT_MAX_MB_MIN,
            "document_fetch_timeout": config.DOCUMENT_FETCH_TIMEOUT_MIN}
    assert c.post("/api/web/system/document-settings", json=good,
                  headers=POST_HEADERS).status_code == 200


# ---- 시스템 로그 ----


def insert_syslog(level="INFO", source="cli", message="msg",
                  created_at="2026-06-13T10:00:00+00:00"):
    with db.connect() as conn:
        db.insert_system_log(conn, created_at=created_at, level=level,
                             logger="chunchugwan.cli", source=source,
                             message=message, traceback=None)


def test_system_logs_filter(tmp_db):
    insert_syslog(level="INFO", source="cli", message="시작")
    insert_syslog(level="ERROR", source="worker", message="실패")
    c = admin_client()
    allrows = c.get("/api/web/system/logs").json()
    assert allrows["total"] == 2
    errors = c.get("/api/web/system/logs?level=ERROR").json()
    assert errors["total"] == 1 and errors["logs"][0]["level"] == "ERROR"
    workers = c.get("/api/web/system/logs?source=worker").json()
    assert workers["total"] == 1


def test_system_logs_invalid_level_ignored(tmp_db):
    insert_syslog()
    body = admin_client().get("/api/web/system/logs?level=BOGUS").json()
    assert body["level"] == ""  # 화이트리스트 밖은 무시
    assert body["total"] == 1


def test_system_logs_default_page_size(tmp_db):
    """시스템 로그 기본 페이지 크기 25, 선택지 10/25/50/100 (이전 기본 50 → 25)."""
    insert_syslog()
    c = admin_client()
    body = c.get("/api/web/system/logs").json()
    assert body["limit"] == 25 and body["limits"] == [10, 25, 50, 100]
    assert body["page_num"] == 1
    assert c.get("/api/web/system/logs?limit=10").json()["limit"] == 10  # 10 허용
    assert c.get("/api/web/system/logs?limit=37").json()["limit"] == 25  # 허용 밖 → 25
