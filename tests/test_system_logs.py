"""시스템 로그 — DB 계층, logging 핸들러, /system/logs 화면·권한, 메뉴 노출."""
import logging

import pytest
from fastapi.testclient import TestClient

from chunchugwan import auth, config, db, system_log
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
            ("pending@test.co", "pending"),
        ):
            db.create_user(conn, email, auth.hash_password("password1234"), role=role)
    return TestClient(web_app.app)


def _login(client, email: str, password: str = "password1234"):
    return client.post(
        "/login", data={"email": email, "password": password}, follow_redirects=False
    )


def _insert(conn, **overrides) -> int:
    fields = {
        "created_at": "2026-06-13T10:00:00+00:00",
        "level": "WARNING",
        "logger": "chunchugwan.capture",
        "source": "serve",
        "message": "테스트 경고",
        "traceback": None,
    }
    fields.update(overrides)
    return db.insert_system_log(conn, **fields)


# ---- DB 계층 ----


def test_insert_and_filter_system_logs(tmp_db):
    with db.connect() as conn:
        _insert(conn, level="INFO", source="cli", message="시작")
        _insert(conn, level="ERROR", source="worker", message="실패",
                created_at="2026-06-13T11:00:00+00:00")
        assert db.count_system_logs(conn) == 2
        errors = db.list_system_logs(conn, level="ERROR")
        assert [r["message"] for r in errors] == ["실패"]
        workers = db.list_system_logs(conn, source="worker")
        assert [r["message"] for r in workers] == ["실패"]
        # 최신 순
        rows = db.list_system_logs(conn)
        assert [r["message"] for r in rows] == ["실패", "시작"]


def test_system_logs_date_filter(tmp_db):
    with db.connect() as conn:
        _insert(conn, created_at="2026-06-10T09:00:00+00:00", message="옛날")
        _insert(conn, created_at="2026-06-13T09:00:00+00:00", message="오늘")
        rows = db.list_system_logs(conn, date_from="2026-06-13")
        assert [r["message"] for r in rows] == ["오늘"]
        rows = db.list_system_logs(conn, date_to="2026-06-10")
        assert [r["message"] for r in rows] == ["옛날"]
        assert db.count_system_logs(conn, date_from="2026-06-01") == 2


def test_prune_system_logs(tmp_db):
    with db.connect() as conn:
        for i in range(10):
            _insert(conn, message=f"기록 {i}")
        assert db.prune_system_logs(conn, keep=3) == 7
        rows = db.list_system_logs(conn)
        assert [r["message"] for r in rows] == ["기록 9", "기록 8", "기록 7"]
        # 보관 한도보다 적으면 아무것도 안 지운다
        assert db.prune_system_logs(conn, keep=100) == 0


# ---- logging 핸들러 ----


@pytest.fixture
def installed(tmp_db):
    """DB 핸들러 설치 + 테스트 후 제거."""
    system_log.install("cli")
    yield
    system_log.flush()
    system_log.uninstall()


def test_handler_writes_warning(installed):
    logging.getLogger("chunchugwan.test_syslog").warning("디스크 경고 %d", 42)
    system_log.flush()
    with db.connect() as conn:
        rows = db.list_system_logs(conn, level="WARNING")
    assert any(
        r["message"] == "디스크 경고 42" and r["logger"] == "chunchugwan.test_syslog"
        and r["source"] == "cli"
        for r in rows
    )


def test_handler_captures_info(installed):
    """기본 로깅 설정(WARNING)이어도 DB 에는 INFO 부터 적재된다."""
    logging.getLogger("chunchugwan.test_syslog").info("워커 시작")
    system_log.flush()
    with db.connect() as conn:
        rows = db.list_system_logs(conn, level="INFO")
    assert any(r["message"] == "워커 시작" for r in rows)


def test_handler_records_traceback(installed):
    logger = logging.getLogger("chunchugwan.test_syslog")
    try:
        raise ValueError("폭발")
    except ValueError:
        logger.exception("아카이빙 실패")
    system_log.flush()
    with db.connect() as conn:
        rows = db.list_system_logs(conn, level="ERROR")
    row = next(r for r in rows if r["message"] == "아카이빙 실패")
    assert "ValueError: 폭발" in row["traceback"]


def test_install_is_idempotent(installed):
    system_log.install("serve")  # 재설치 — 핸들러는 하나, 출처만 갱신
    handlers = [
        h for h in logging.getLogger("chunchugwan").handlers
        if isinstance(h, system_log.DBLogHandler)
    ]
    assert len(handlers) == 1
    logging.getLogger("chunchugwan.test_syslog").warning("출처 확인")
    system_log.flush()
    with db.connect() as conn:
        rows = db.list_system_logs(conn)
    assert next(r for r in rows if r["message"] == "출처 확인")["source"] == "serve"


def test_install_rejects_unknown_source(tmp_db):
    with pytest.raises(ValueError):
        system_log.install("cron")


# ---- /system/logs 화면 · 권한 ----


