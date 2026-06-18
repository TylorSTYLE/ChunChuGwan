"""사용자 액션 감사 로그 — 액션 라우트가 요청 주체를 시스템 로그에 남긴다."""
import logging
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from chunchugwan import archive_worker, auth, config, db, system_log
from chunchugwan.web import app as web_app

_AUDIT_LOGGER = "chunchugwan.web.audit"


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
    """최초 관리자(founder) + 아카이버가 등록된 TestClient."""
    with db.connect() as conn:
        db.create_first_admin(conn, "boss@test.co", auth.hash_password("bosspass1234"))
        db.create_user(
            conn, "archiver@test.co", auth.hash_password("password1234"),
            role="archiver",
        )
    return TestClient(web_app.app)


def _login(client, email: str, password: str = "password1234"):
    return client.post(
        "/login", data={"email": email, "password": password}, follow_redirects=False
    )


def _audit_messages(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == _AUDIT_LOGGER]


@pytest.fixture
def audit_logs(caplog):
    caplog.set_level(logging.INFO, logger=_AUDIT_LOGGER)
    return caplog


def _fake_archive(monkeypatch):
    """캡처(worker 큐 소비)를 무력화 — 감사 로그는 enqueue 시점에 남는다."""
    monkeypatch.setattr(
        archive_worker.pipeline, "archive_url",
        lambda url, force=False, source="cli", **kw: SimpleNamespace(status="archived"),
    )


def test_api_archive_logs_key_name(client, monkeypatch, audit_logs):
    """REST API 아카이빙 트리거는 API 키 이름이 요청 주체로 기록된다."""
    _fake_archive(monkeypatch)
    with db.connect() as conn:
        token = auth.issue_api_key(
            conn, "bot", can_view=True, can_archive=True,
            created_by=None, ttl_seconds=None,
        )
    res = client.post(
        "/api/v1/archive", json={"url": "https://example.com/api-page"},
        headers={"X-API-Key": token},
    )
    assert res.status_code == 202
    messages = _audit_messages(audit_logs)
    assert any(
        "새 아카이빙 등록(API): https://example.com/api-page" in m
        and "(요청자: API 키 'bot')" in m
        for m in messages
    )


