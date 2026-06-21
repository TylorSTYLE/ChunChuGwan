"""내 아카이브 — requested_by 귀속(큐→로그), /settings/archives 본인 필터, 개인설정 메뉴."""
import pytest
from fastapi.testclient import TestClient

from chunchugwan import auth, config, db
from chunchugwan.web import app as web_app


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    for attr, val in (
        ("ARCHIVE_ROOT", tmp_path), ("SITES_DIR", tmp_path / "sites"),
        ("DB_PATH", tmp_path / "index.db"), ("CACHE_DIR", tmp_path / "cache"),
        ("RESOURCES_DIR", tmp_path / "resources"),
        ("DOCUMENTS_DIR", tmp_path / "documents"),
    ):
        monkeypatch.setattr(config, attr, val)


def _seed():
    with db.connect() as conn:
        db.create_first_admin(conn, "boss@test.co", auth.hash_password("bosspass1234"))
        db.create_user(conn, "alice@test.co", auth.hash_password("password1234"), role="archiver")
        db.create_user(conn, "bob@test.co", auth.hash_password("password1234"), role="archiver")


@pytest.fixture
def client(tmp_db):
    _seed()
    web_app._active_jobs.clear()
    yield TestClient(web_app.app)
    web_app._active_jobs.clear()


def _uid(email):
    with db.connect() as conn:
        return db.get_user_by_email(conn, email)["id"]


def _login(client, email, password="password1234"):
    return client.post(
        "/login", data={"email": email, "password": password}, follow_redirects=False
    )


def _log(url, requested_by, *, status="new", source="web"):
    with db.connect() as conn:
        return db.insert_archive_log(
            conn, url=url, domain="example.com", source=source,
            requested_by=requested_by, status=status,
            started_at="2026-06-10T00:00:00+00:00", duration_ms=10,
        )


# ---- requested_by 귀속 (큐 → 로그) ----


def test_enqueue_carries_requested_by(client):
    """web 요청자가 archive_jobs 에 실리고 클레임 결과 행에 그대로 남는다."""
    uid = _uid("alice@test.co")
    with db.connect() as conn:
        assert db.enqueue_archive_job(
            conn, "https://a.test/", source="web", requested_by=uid
        )
        job = db.claim_due_archive_job(conn, "2999-01-01T00:00:00+00:00")
    assert job["requested_by"] == uid


def test_log_filter_by_requester(client):
    a, b = _uid("alice@test.co"), _uid("bob@test.co")
    _log("https://a.test/1", a)
    _log("https://a.test/2", a, status="error")
    _log("https://b.test/1", b)
    _log("https://sys.test/", None, source="schedule")  # 주체 없음
    with db.connect() as conn:
        assert db.count_archive_logs(conn, requested_by=a) == 2
        assert db.count_archive_logs(conn, requested_by=b) == 1
        rows = db.list_archive_logs(conn, requested_by=a)
    assert {r["url"] for r in rows} == {"https://a.test/1", "https://a.test/2"}


# ---- /settings/archives 화면 ----


# ---- 개인설정 드롭다운 메뉴 ----


# ---- delete_user 정리 ----


def test_delete_user_clears_requested_by(client):
    a = _uid("alice@test.co")
    log_id = _log("https://a.test/keep", a)
    with db.connect() as conn:
        db.delete_user(conn, a)
        row = db.get_archive_log(conn, log_id)
    # 실행 이력은 보존하되 요청자 표기(FK)는 NULL 로 끊는다
    assert row is not None
    assert row["requested_by"] is None
