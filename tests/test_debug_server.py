"""디버그 진단 서버 (web/debug_server.py) — 토글·읽기·트리거·시크릿 비노출.

별도 포트 서버를 실제로 띄우지 않고 build_app() 으로 만든 ASGI 앱을 TestClient 로
직접 두드린다. 캡처 트리거는 브라우저를 띄우지 않는 검증 경로(잘못된 URL·루프백
거부·빈 큐)만 확인한다.
"""
from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chunchugwan import config, db
from chunchugwan.web import debug_server


# ---- 설정 토글 ----

def test_debug_disabled_by_default(monkeypatch) -> None:
    """WCCG_DEBUG 미설정 시 기본 비활성 — 릴리스에서 포트가 안 열린다."""
    monkeypatch.delenv("WCCG_DEBUG", raising=False)
    try:
        importlib.reload(config)
        assert config.DEBUG_ENABLED is False
        assert config.DEBUG_HOST == "127.0.0.1"   # 기본 루프백
        assert config.DEBUG_PORT == 8799
        assert config.DEBUG_TOKEN == ""
    finally:
        monkeypatch.undo()
        importlib.reload(config)


def test_debug_env_overrides(monkeypatch) -> None:
    """WCCG_DEBUG=on + 호스트/포트/토큰 오버라이드."""
    monkeypatch.setenv("WCCG_DEBUG", "on")
    monkeypatch.setenv("WCCG_DEBUG_HOST", "0.0.0.0")
    monkeypatch.setenv("WCCG_DEBUG_PORT", "9100")
    monkeypatch.setenv("WCCG_DEBUG_TOKEN", "  sekret  ")
    try:
        importlib.reload(config)
        assert config.DEBUG_ENABLED is True
        assert config.DEBUG_HOST == "0.0.0.0"
        assert config.DEBUG_PORT == 9100
        assert config.DEBUG_TOKEN == "sekret"      # strip 적용
    finally:
        monkeypatch.undo()
        importlib.reload(config)


def test_maybe_start_noop_when_disabled(monkeypatch) -> None:
    """비활성이면 스레드를 띄우지 않는다 (uvicorn 호출 안 함)."""
    monkeypatch.setattr(config, "DEBUG_ENABLED", False)
    debug_server._server_thread = None
    debug_server.maybe_start("test")
    assert debug_server._server_thread is None


# ---- 앱 동작 ----

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "RESOURCES_DIR", tmp_path / "resources")
    monkeypatch.setattr(config, "DOCUMENTS_DIR", tmp_path / "documents")
    monkeypatch.setattr(config, "AUTH_ENABLED", False)
    with db.connect():  # 스키마 보장 (빈 DB 생성)
        pass
    return TestClient(debug_server.build_app("test"))


def test_index_lists_endpoints(client) -> None:
    body = client.get("/debug").json()
    assert body["source"] == "test"
    assert any("/debug/health" in e for e in body["read"])
    assert any("/debug/capture" in e for e in body["trigger"])


def test_health(client) -> None:
    body = client.get("/debug/health").json()
    assert body["ok"] is True
    assert body["source"] == "test"
    assert body["version"]
    assert isinstance(body["worker_threads"], list)
    assert body["debug_bind"].endswith(str(config.DEBUG_PORT))


def test_db_state(client) -> None:
    body = client.get("/debug/db").json()
    assert isinstance(body["row_counts"], dict)
    assert "snapshots" in body["row_counts"]      # 코어 테이블 존재
    assert body["quick_check"] == "ok"
    assert body["journal_mode"].lower() == "wal"


def test_queues_shape(client) -> None:
    body = client.get("/debug/queues").json()
    assert isinstance(body["archive_jobs"], dict)
    assert body["writes_paused"] is False
    assert body["migration_mode"] is False
    assert "due_now" in body["schedules"]


def test_logs_tail(client) -> None:
    body = client.get("/debug/logs", params={"tail": 5}).json()
    assert isinstance(body["logs"], list)
    assert body["count"] == len(body["logs"])


def test_search_and_storage(client) -> None:
    search = client.get("/debug/search").json()
    assert "available" in search
    storage = client.get("/debug/storage").json()
    assert storage["backend"] == "local"
    assert storage["writes_paused"] is False


def test_config_never_leaks_secrets(client, monkeypatch) -> None:
    """원칙 6 — 시크릿 값은 응답에 절대 들어가지 않고 '설정됨 여부' 만 노출."""
    monkeypatch.setattr(config, "SECRET_KEY", "TOPSECRET-KEY")
    monkeypatch.setattr(config, "SMTP_PASSWORD", "smtp-pw-XYZ")
    monkeypatch.setattr(config, "S3_SECRET_ACCESS_KEY", "s3-secret-ABC")
    resp = client.get("/debug/config")
    raw = resp.text
    assert "TOPSECRET-KEY" not in raw
    assert "smtp-pw-XYZ" not in raw
    assert "s3-secret-ABC" not in raw
    sc = resp.json()["secrets_configured"]
    assert sc["secret_key"] is True
    assert sc["smtp_password"] is True
    assert sc["s3_secret"] is True


def test_token_gate(client, monkeypatch) -> None:
    """WCCG_DEBUG_TOKEN 설정 시 X-Debug-Token 헤더가 없으면 401."""
    monkeypatch.setattr(config, "DEBUG_TOKEN", "letmein")
    assert client.get("/debug/health").status_code == 401
    ok = client.get("/debug/health", headers={"X-Debug-Token": "letmein"})
    assert ok.status_code == 200


def test_capture_requires_url(client) -> None:
    assert client.post("/debug/capture", json={}).status_code == 400


def test_capture_rejects_loopback(client) -> None:
    """루프백 대상은 netcheck 가 거부 — 브라우저를 띄우기 전에 400 (원칙 7)."""
    resp = client.post("/debug/capture", json={"url": "http://127.0.0.1:1234/"})
    assert resp.status_code == 400


def test_triggers_on_empty_queue(client) -> None:
    """빈 큐에서 트리거 — 처리할 것이 없어 네트워크 없이 즉시 반환."""
    sched = client.post("/debug/run/scheduler").json()
    assert sched["processed"] == 0 and sched["results"] == []
    arch = client.post("/debug/run/archive").json()
    assert arch["processed"] is False


# ---- 보강된 진단/제어 표면 ----

def test_index_lists_new_endpoints(client) -> None:
    body = client.get("/debug").json()
    assert any("/debug/inspect" in e for e in body["read"])
    assert any("/debug/run/crawl" in e for e in body["trigger"])
    assert any("/debug/run/recover-stale" in e for e in body["trigger"])


def test_crawl_and_challenge_reads(client) -> None:
    assert client.get("/debug/crawls").json() == {"count": 0, "crawls": []}
    assert client.get("/debug/challenges").json() == {"count": 0, "jobs": []}
    # 빈 크롤 실패 조회
    f = client.get("/debug/crawl/1/failures").json()
    assert f["crawl_id"] == 1 and f["failed"] == 0


def test_queue_and_storage_extras(client) -> None:
    q = client.get("/debug/queues").json()
    assert "storage_migration_active" in q          # 멈춤 원인 한 가지 더
    s = client.get("/debug/storage").json()
    assert s["backend"] == "local"
    assert "migration" in s                          # 진행률/요약 병합


def test_new_queue_triggers_empty(client) -> None:
    """크롤·크롤스케줄·스테일복구 트리거 — 빈 상태에서 네트워크 없이 즉시 반환."""
    assert client.post("/debug/run/crawl").json() == {"processed": False}
    cs = client.post("/debug/run/crawl-schedules").json()
    assert cs["processed"] == 0 and cs["results"] == []
    rec = client.post("/debug/run/recover-stale").json()
    assert rec == {"recovered_archive_jobs": 0, "recovered_crawl_pages": 0}


def test_inspect_unknown_url_404(client) -> None:
    r = client.get("/debug/inspect", params={"url": "https://nope.example/x"})
    assert r.status_code == 404


def test_logs_q_filter(client) -> None:
    r = client.get("/debug/logs", params={"q": "절대없는문자열zzz", "tail": 5})
    assert r.status_code == 200 and r.json()["logs"] == []


def test_release_build_excludes_debug_server(tmp_path) -> None:
    """릴리스 빌드 모사 — debug_server.py 를 제거해도 app/worker 가 import 되고,
    WCCG_DEBUG=on 이어도 디버그 서버가 없어 graceful no-op 한다(원칙: 코드 자체 부재)."""
    import chunchugwan.web as webpkg

    src = Path(webpkg.__file__).parent / "debug_server.py"
    bak = src.with_name("debug_server.py.bak")
    src.rename(bak)
    try:
        code = (
            "import os, tempfile, importlib\n"
            "os.environ['WCCG_DEBUG']='on'; os.environ['WCCG_AUTH']='off'\n"
            "os.environ['WCCG_ROOT']=tempfile.mkdtemp()\n"
            "from chunchugwan.web import app as a\n"
            "assert a.debug_server is None, 'app.debug_server must be None when removed'\n"
            "try:\n"
            "    importlib.import_module('chunchugwan.web.debug_server')\n"
            "    raise SystemExit('debug_server still importable')\n"
            "except ImportError:\n"
            "    pass\n"
            "print('OK')\n"
        )
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        assert out.returncode == 0, f"stdout={out.stdout!r} stderr={out.stderr!r}"
        assert "OK" in out.stdout
    finally:
        bak.rename(src)
