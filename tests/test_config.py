"""config 환경변수 오버라이드 테스트."""

from __future__ import annotations

import importlib

from chunchugwan import config


def test_dashboard_host_default(monkeypatch) -> None:
    """WCCG_HOST 미설정 시 localhost 전용 바인딩."""
    monkeypatch.delenv("WCCG_HOST", raising=False)
    try:
        importlib.reload(config)
        assert config.DASHBOARD_HOST == "127.0.0.1"
    finally:
        monkeypatch.undo()
        importlib.reload(config)


def test_dashboard_host_env_override(monkeypatch) -> None:
    """WCCG_HOST 설정 시 바인딩 주소 오버라이드 (컨테이너용)."""
    monkeypatch.setenv("WCCG_HOST", "0.0.0.0")
    try:
        importlib.reload(config)
        assert config.DASHBOARD_HOST == "0.0.0.0"
    finally:
        monkeypatch.undo()
        importlib.reload(config)


def test_dotenv_file_loaded(monkeypatch, tmp_path) -> None:
    """CWD 의 .env 가 로드돼 config 값에 반영된다 (python-dotenv).

    .env 에 둔 키는 monkeypatch 로도 함께 추적(delenv)해, undo() 가 load_dotenv 의
    os.environ 변경까지 되돌리도록 한다 (테스트 간 누수 방지).
    """
    monkeypatch.delenv("WCCG_SESSION_TTL_DAYS", raising=False)
    monkeypatch.delenv("WCCG_SECRET_KEY", raising=False)
    (tmp_path / ".env").write_text(
        "WCCG_SESSION_TTL_DAYS=99\nWCCG_SECRET_KEY=from-dotenv\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    try:
        importlib.reload(config)
        assert config.SESSION_TTL_DAYS == 99
        assert config.SECRET_KEY == "from-dotenv"
    finally:
        monkeypatch.undo()
        importlib.reload(config)


def test_real_env_overrides_dotenv(monkeypatch, tmp_path) -> None:
    """실제 환경변수가 .env 보다 우선한다 (load_dotenv override=False)."""
    (tmp_path / ".env").write_text("WCCG_SESSION_TTL_DAYS=99\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WCCG_SESSION_TTL_DAYS", "7")
    try:
        importlib.reload(config)
        assert config.SESSION_TTL_DAYS == 7
    finally:
        monkeypatch.undo()
        importlib.reload(config)


def test_missing_dotenv_is_noop(monkeypatch, tmp_path) -> None:
    """.env 가 없는 디렉토리에서도 오류 없이 기본값을 쓴다."""
    monkeypatch.delenv("WCCG_SESSION_TTL_DAYS", raising=False)
    monkeypatch.chdir(tmp_path)  # 빈 임시 디렉토리 — .env 없음
    try:
        importlib.reload(config)
        assert config.SESSION_TTL_DAYS == 14
    finally:
        monkeypatch.undo()
        importlib.reload(config)
