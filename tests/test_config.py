"""config 환경변수 오버라이드 테스트."""

from __future__ import annotations

import importlib

from archiver import config


def test_dashboard_host_default(monkeypatch) -> None:
    """ARCHIVER_HOST 미설정 시 localhost 전용 바인딩."""
    monkeypatch.delenv("ARCHIVER_HOST", raising=False)
    try:
        importlib.reload(config)
        assert config.DASHBOARD_HOST == "127.0.0.1"
    finally:
        monkeypatch.undo()
        importlib.reload(config)


def test_dashboard_host_env_override(monkeypatch) -> None:
    """ARCHIVER_HOST 설정 시 바인딩 주소 오버라이드 (컨테이너용)."""
    monkeypatch.setenv("ARCHIVER_HOST", "0.0.0.0")
    try:
        importlib.reload(config)
        assert config.DASHBOARD_HOST == "0.0.0.0"
    finally:
        monkeypatch.undo()
        importlib.reload(config)
