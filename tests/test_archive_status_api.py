"""확장 결과 알림 — GET /api/v1/archive/status, job_id 응답, 크롤 요청자 귀속.

확장은 제출한 단발 작업(job_id)·크롤(crawl_id)을 이 엔드포인트로 폴링해
완료/실패/사람확인 상태를 받아 데스크톱 알림을 띄운다. 상태는 소유자(토큰
주인)로 스코프되어 남의 작업은 unknown 으로만 보인다.
"""
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
    with db.connect() as conn:
        db.create_first_admin(conn, "boss@test.co", auth.hash_password("bosspass1234"))
    web_app._active_jobs.clear()
    yield TestClient(web_app.app)
    web_app._active_jobs.clear()


def _owned_token(email: str = "boss@test.co", role: str = "admin") -> tuple[int, str]:
    """그 사용자에게 귀속된 개인 확장 토큰을 발급해 (user_id, token) 반환."""
    with db.connect() as conn:
        user = db.get_user_by_email(conn, email)
        if user is None:
            db.create_user(conn, email, auth.hash_password("password1234"), role=role)
            user = db.get_user_by_email(conn, email)
        uid = user["id"]
        token = auth.issue_api_key(
            conn, f"ext-{email}", can_view=True, can_archive=True,
            created_by=uid, owner_user_id=uid, ttl_seconds=None,
        )
    return uid, token


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---- job_id 응답 + 활성 작업 상태 ----


def test_archive_returns_job_id_and_status_pending(client):
    _uid, token = _owned_token()
    r = client.post("/api/v1/archive", json={"url": URL}, headers=_h(token))
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    assert isinstance(job_id, int)

    s = client.get(f"/api/v1/archive/status?jobs={job_id}", headers=_h(token))
    assert s.status_code == 200
    assert s.json()["jobs"] == [
        {"id": job_id, "state": "pending", "url": storage.normalize_url(URL)}
    ]


def test_status_in_progress_then_needs_human(client):
    _uid, token = _owned_token()
    job_id = client.post(
        "/api/v1/archive", json={"url": URL}, headers=_h(token)
    ).json()["job_id"]

    with db.connect() as conn:
        db.claim_due_archive_job(conn, "2099-01-01T00:00:00+00:00")  # → in_progress
    s = client.get(f"/api/v1/archive/status?jobs={job_id}", headers=_h(token)).json()
    assert s["jobs"][0]["state"] == "in_progress"

    with db.connect() as conn:
        db.mark_needs_human(conn, job_id, token="livetok", viewport_w=390, viewport_h=844)
    s = client.get(f"/api/v1/archive/status?jobs={job_id}", headers=_h(token)).json()
    assert s["jobs"][0]["state"] == "needs_human"


def test_active_job_wins_over_stale_error_log(client):
    """재시도 중(작업이 pending) 이면 과거 error 로그가 아니라 활성 상태를 보인다."""
    _uid, token = _owned_token()
    job_id = client.post(
        "/api/v1/archive", json={"url": URL}, headers=_h(token)
    ).json()["job_id"]
    with db.connect() as conn:
        db.insert_archive_log(
            conn, url=storage.normalize_url(URL), domain="example.com", source="api",
            requested_by=_uid, status="error", started_at="2026-06-01T00:00:00+00:00",
            error="CaptureError: 1차 실패", job_id=job_id,
        )
    s = client.get(f"/api/v1/archive/status?jobs={job_id}", headers=_h(token)).json()
    assert s["jobs"][0]["state"] == "pending"  # 작업이 살아있으면 활성 상태 우선


# ---- 종결 상태 (로그 기반) ----


def test_status_succeeded_failed_unknown_from_logs(client):
    uid, token = _owned_token()
    with db.connect() as conn:
        page_id = db.get_or_create_page(
            conn, URL, "example.com", storage.url_to_slug(URL)
        )
        db.insert_archive_log(
            conn, url=URL, domain="example.com", page_id=page_id, source="api",
            requested_by=uid, status="changed", started_at="2026-06-01T00:00:00+00:00",
            http_status=200, job_id=101,
        )
        db.insert_archive_log(
            conn, url="https://example.com/x", domain="example.com", source="api",
            requested_by=uid, status="error", started_at="2026-06-01T00:00:00+00:00",
            error="CaptureError: boom", job_id=102,
        )
    s = client.get(
        "/api/v1/archive/status?jobs=101,102,999", headers=_h(token)
    ).json()
    by_id = {j["id"]: j for j in s["jobs"]}
    assert by_id[101]["state"] == "succeeded"
    assert by_id[101]["outcome"] == "changed"
    assert by_id[101]["page_id"] == page_id
    assert by_id[101]["http_status"] == 200
    assert by_id[102]["state"] == "failed"
    assert "boom" in by_id[102]["error"]
    assert by_id[999] == {"id": 999, "state": "unknown"}


def test_latest_log_wins_when_job_retried_then_succeeded(client):
    """삭제된 작업에 error→success 로그가 둘 다 있으면 최신(성공)이 종결 상태."""
    uid, token = _owned_token()
    with db.connect() as conn:
        db.insert_archive_log(
            conn, url=URL, domain="example.com", source="api", requested_by=uid,
            status="error", started_at="2026-06-01T00:00:00+00:00",
            error="boom", job_id=303,
        )
        db.insert_archive_log(
            conn, url=URL, domain="example.com", source="api", requested_by=uid,
            status="new", started_at="2026-06-01T00:01:00+00:00", job_id=303,
        )
    s = client.get("/api/v1/archive/status?jobs=303", headers=_h(token)).json()
    assert s["jobs"][0]["state"] == "succeeded"
    assert s["jobs"][0]["outcome"] == "new"


# ---- 소유자 스코프 ----


def test_status_scoped_to_token_owner(client):
    uid, token = _owned_token()
    _other_id, other_token = _owned_token("other@test.co", role="archiver")
    with db.connect() as conn:
        db.insert_archive_log(
            conn, url=URL, domain="example.com", source="api", requested_by=uid,
            status="new", started_at="2026-06-01T00:00:00+00:00", job_id=201,
        )
    mine = client.get("/api/v1/archive/status?jobs=201", headers=_h(token)).json()
    assert mine["jobs"][0]["state"] == "succeeded"
    theirs = client.get(
        "/api/v1/archive/status?jobs=201", headers=_h(other_token)
    ).json()
    assert theirs["jobs"][0]["state"] == "unknown"


# ---- 크롤: 요청자 귀속 + 상태 ----


def test_crawl_records_requester_and_status(client):
    uid, token = _owned_token()
    r = client.post(
        "/api/v1/crawl", json={"url": "https://example.com/sec/"}, headers=_h(token)
    )
    assert r.status_code == 202
    crawl_id = r.json()["crawl_id"]
    with db.connect() as conn:
        assert db.get_crawl(conn, crawl_id)["requested_by"] == uid

    s = client.get(f"/api/v1/archive/status?crawls={crawl_id}", headers=_h(token)).json()
    assert s["crawls"][0]["status"] == "running"
    assert "counts" in s["crawls"][0]

    with db.connect() as conn:  # 모든 페이지 done → 크롤 마감
        conn.execute("UPDATE crawl_pages SET status='done' WHERE crawl_id=?", (crawl_id,))
        db.finish_crawl_if_done(conn, crawl_id)
    s = client.get(f"/api/v1/archive/status?crawls={crawl_id}", headers=_h(token)).json()
    assert s["crawls"][0]["status"] == "done"


def test_crawl_status_scoped_to_owner(client):
    _uid, token = _owned_token()
    crawl_id = client.post(
        "/api/v1/crawl", json={"url": "https://example.com/sec/"}, headers=_h(token)
    ).json()["crawl_id"]
    _other, other_token = _owned_token("o2@test.co", role="archiver")
    s = client.get(
        f"/api/v1/archive/status?crawls={crawl_id}", headers=_h(other_token)
    ).json()
    assert s["crawls"][0] == {"id": crawl_id, "status": "unknown"}


# ---- 입력 처리 ----


def test_status_empty_and_invalid_ids(client):
    _uid, token = _owned_token()
    assert client.get("/api/v1/archive/status", headers=_h(token)).json() == {
        "jobs": [], "crawls": []
    }
    s = client.get("/api/v1/archive/status?jobs=abc,,12x&crawls=", headers=_h(token)).json()
    assert s == {"jobs": [], "crawls": []}  # 잘못된 토큰은 무시


def test_status_requires_token_when_auth_on(client):
    assert client.get("/api/v1/archive/status?jobs=1").status_code == 401
