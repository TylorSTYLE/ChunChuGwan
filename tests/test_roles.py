"""사용자 권한(역할) — DB 계층, 라우트 가드, 사용자 관리 화면 테스트."""
import pytest
from fastapi.testclient import TestClient

from chunchugwan import auth, config, db
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


@pytest.fixture
def client(tmp_db):
    """최초 관리자(founder) + 역할별 사용자가 등록된 TestClient."""
    with db.connect() as conn:
        db.create_first_admin(conn, "boss@test.co", auth.hash_password("bosspass1234"))
        for email, role in (
            ("archiver@test.co", "archiver"),
            ("viewer@test.co", "viewer"),
            ("blocked@test.co", "blocked"),
            ("withdrawn@test.co", "withdrawn"),
        ):
            db.create_user(conn, email, auth.hash_password("password1234"), role=role)
    return TestClient(web_app.app)


def _login(client, email: str, password: str = "password1234"):
    return client.post(
        "/login", data={"email": email, "password": password}, follow_redirects=False
    )


def _user(email: str):
    with db.connect() as conn:
        return db.get_user_by_email(conn, email)


# ---- DB 계층 ----


def test_create_user_default_role_is_viewer(tmp_db):
    with db.connect() as conn:
        uid = db.create_user(conn, "a@b.co")
        assert db.get_user_by_id(conn, uid)["role"] == "viewer"


def test_create_user_rejects_unknown_role(tmp_db):
    with db.connect() as conn:
        with pytest.raises(ValueError):
            db.create_user(conn, "a@b.co", role="superuser")


def test_set_role_and_validation(tmp_db):
    with db.connect() as conn:
        uid = db.create_user(conn, "a@b.co")
        assert db.set_role(conn, uid, "archiver") is True
        assert db.get_user_by_id(conn, uid)["role"] == "archiver"
        with pytest.raises(ValueError):
            db.set_role(conn, uid, "root")


def test_set_role_refuses_founder(tmp_db):
    with db.connect() as conn:
        uid = db.create_first_admin(conn, "boss@test.co", "x")
        assert db.set_role(conn, uid, "viewer") is False
        assert db.get_user_by_id(conn, uid)["role"] == "admin"


# ---- 아카이빙 권한 가드 ----


def _seed_error_log() -> int:
    with db.connect() as conn:
        return db.insert_archive_log(
            conn, url="https://x.co/a", domain="x.co", source="web",
            status="error", started_at="2026-06-13T00:00:00+00:00",
            duration_ms=10, error="boom",
        )


def _seed_needs_human(url: str = "https://sd.test/article") -> int:
    """사람 확인(라이브 진입) 상태의 작업 1건을 만든다."""
    with db.connect() as conn:
        db.enqueue_archive_job(conn, url, source="web")
        job = db.claim_due_archive_job(conn, "2099-01-01T00:00:00+00:00")
        db.mark_needs_human(conn, job["id"], token="tok", viewport_w=1280, viewport_h=800)
    return job["id"]


def test_viewer_sees_schedules_readonly(client):
    """viewer 도 스케줄 화면은 볼 수 있지만 변경/해제 폼은 보이지 않는다."""
    _login(client, "viewer@test.co")
    res = client.get("/schedules")
    assert res.status_code == 200
    assert "주기 변경" not in res.text
    assert "/schedule/next-run" not in res.text
    assert "/schedule/delete" not in res.text


# ---- 차단된 계정 ----


# ---- 탈퇴한 계정 ----


def test_withdraw_user_db(tmp_db):
    """탈퇴는 권한 변경 + 세션 무효화 — 계정 정보는 남는다. founder 는 불가."""
    with db.connect() as conn:
        boss_id = db.create_first_admin(conn, "boss@test.co", "x")
        uid = db.create_user(conn, "a@b.co")
        token = auth.issue_session(conn, uid)
        db.withdraw_user(conn, uid)
        assert db.get_user_by_id(conn, uid)["role"] == "withdrawn"
        assert auth.resolve_session(conn, token) is None
        db.withdraw_user(conn, uid)  # 멱등 — 이미 탈퇴여도 에러 없음
        # founder 는 탈퇴 처리되지 않는다
        db.withdraw_user(conn, boss_id)
        assert db.get_user_by_id(conn, boss_id)["role"] == "admin"


# ---- 계정 정보 삭제 (관리자) ----


# ---- 사용자 관리 화면 ----


# ---- 인증 off (loopback) ----


