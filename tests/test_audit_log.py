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


def test_archive_new_logs_actor(client, monkeypatch, audit_logs):
    """새 아카이빙 등록이 요청한 사용자 이메일과 함께 기록된다."""
    _fake_archive(monkeypatch)
    _login(client, "archiver@test.co")
    res = client.post(
        "/archive", data={"url": "https://example.com/page"}, follow_redirects=False
    )
    assert res.status_code == 303
    messages = _audit_messages(audit_logs)
    assert any(
        "새 아카이빙 등록: https://example.com/page" in m
        and "(요청자: archiver@test.co)" in m
        for m in messages
    )


def test_settings_change_logs_actor(client, audit_logs):
    """가입 설정 변경이 관리자 이메일과 함께 기록된다."""
    _login(client, "boss@test.co", "bosspass1234")
    res = client.post(
        "/system/settings",
        data={"signup_enabled": "on", "signup_default_role": "viewer"},
        follow_redirects=False,
    )
    assert res.status_code == 303
    messages = _audit_messages(audit_logs)
    assert any(
        "가입 설정 변경" in m and "(요청자: boss@test.co)" in m for m in messages
    )


def test_role_change_logs_actor_and_target(client, audit_logs):
    """사용자 권한 변경이 대상 이메일·새 권한·요청자와 함께 기록된다."""
    _login(client, "boss@test.co", "bosspass1234")
    with db.connect() as conn:
        target = db.get_user_by_email(conn, "archiver@test.co")
    res = client.post(
        f"/system/users/{target['id']}/role", data={"role": "viewer"},
        follow_redirects=False,
    )
    assert res.status_code == 303
    messages = _audit_messages(audit_logs)
    assert any(
        "사용자 권한 변경: archiver@test.co" in m and "(요청자: boss@test.co)" in m
        for m in messages
    )


def test_network_tag_create_and_delete_logged(client, audit_logs):
    """로컬 네트워크 태그 추가·삭제가 기록된다."""
    _login(client, "boss@test.co", "bosspass1234")
    client.post(
        "/system/network-tags", data={"name": "NAS"}, follow_redirects=False
    )
    with db.connect() as conn:
        tag = db.get_network_tag_by_name(conn, "NAS")
    client.post(
        f"/system/network-tags/{tag['id']}/delete", follow_redirects=False
    )
    messages = _audit_messages(audit_logs)
    assert any("로컬 네트워크 태그 추가: 'NAS'" in m for m in messages)
    assert any("로컬 네트워크 태그 삭제: 'NAS'" in m for m in messages)


def test_api_key_create_logged(client, audit_logs):
    """API 키 발급이 키 이름·권한·요청자와 함께 기록된다."""
    _login(client, "boss@test.co", "bosspass1234")
    res = client.post(
        "/system/api-keys",
        data={"name": "bot", "can_view": "on", "expiry": "permanent"},
        follow_redirects=False,
    )
    assert res.status_code == 303
    messages = _audit_messages(audit_logs)
    assert any(
        "API 키 발급: 'bot' (권한: 보기)" in m and "(요청자: boss@test.co)" in m
        for m in messages
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


def test_audit_log_lands_in_system_logs_db(client):
    """감사 로그가 system_logs 테이블에 적재된다 (시스템 로그 화면의 데이터 소스)."""
    system_log.install("serve")
    try:
        _login(client, "boss@test.co", "bosspass1234")
        client.post(
            "/system/network-tags", data={"name": "내부망"}, follow_redirects=False
        )
        system_log.flush()
        with db.connect() as conn:
            rows = db.list_system_logs(conn, limit=50, offset=0)
    finally:
        system_log.uninstall()
    audit_rows = [r for r in rows if r["logger"] == _AUDIT_LOGGER]
    assert audit_rows, "감사 로그가 system_logs 에 적재되어야 한다"
    assert any(
        "로컬 네트워크 태그 추가: '내부망'" in r["message"]
        and "boss@test.co" in r["message"]
        for r in audit_rows
    )
