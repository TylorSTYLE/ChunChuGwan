"""시스템 로그 — Python logging 레코드를 DB(system_logs 테이블)에 적재.

대시보드 /system/logs (관리자 전용)가 보여준다. serve·워커·CLI 가 같은
DB 에 쓰므로 프로세스가 나뉘어도 한 화면에서 모아 볼 수 있다.
아카이빙 실행 결과(archive_logs)와 달리 앱 동작 자체의 기록 —
경고/오류와 워커·캡처 단계의 INFO 로그가 대상이다.

emit 은 큐에 넣기만 하고 쓰기는 전용 스레드가 한다 — 로그를 남긴
스레드가 쓰기 트랜잭션을 들고 있을 때 같은 스레드의 두 번째 커넥션이
락 대기로 멈추는 것(자기 자신과의 경합)을 피한다.
"""

from __future__ import annotations

import atexit
import logging
import queue
import threading
import time
from datetime import datetime, timezone

from . import config, db

# chunchugwan 네임스페이스 로거에만 단다 — uvicorn 액세스 로그 등은 제외.
_LOGGER_NAME = "chunchugwan"
_PRUNE_EVERY = 500   # 이 횟수 적재마다 보관 한도 초과분 정리

_source = "cli"      # 적재 프로세스 종류 — install() 이 설정
_install_lock = threading.Lock()


class DBLogHandler(logging.Handler):
    """레코드를 system_logs 에 기록하는 비차단 핸들러.

    emit 은 레코드를 직렬화해 큐에 넣고 즉시 반환한다. 쓰기 스레드의
    적재 실패(DB 락 등)는 조용히 무시한다 — 로그가 본 작업을 깨면 안 된다.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self._queue: queue.Queue[dict] = queue.Queue()
        self._count = 0
        self._writer = threading.Thread(
            target=self._drain, name="wccg-system-log", daemon=True
        )
        self._writer.start()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            traceback = None
            if record.exc_info and record.exc_info != (None, None, None):
                traceback = logging.Formatter().formatException(record.exc_info)
            self._queue.put({
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "level": record.levelname,
                "logger": record.name,
                "source": _source,
                "message": record.getMessage(),
                "traceback": traceback,
            })
        except Exception:
            self.handleError(record)

    def _drain(self) -> None:
        while True:
            fields = self._queue.get()
            try:
                with db.connect() as conn:
                    db.insert_system_log(conn, **fields)
                    self._count += 1
                    if self._count % _PRUNE_EVERY == 1:  # 첫 적재 + 이후 주기적으로
                        db.prune_system_logs(conn, keep=config.SYSTEM_LOG_MAX_ROWS)
            except Exception:
                pass  # DB 락·권한 등 적재 실패는 무시
            finally:
                self._queue.task_done()

    def flush(self, timeout: float = 5.0) -> None:
        """큐에 쌓인 레코드의 적재 완료를 대기 (프로세스 종료·테스트용)."""
        deadline = time.monotonic() + timeout
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.02)


def install(source: str) -> None:
    """chunchugwan 로거에 DB 핸들러를 단다 (이미 달려 있으면 출처만 갱신).

    source 는 적재 프로세스 종류 (db.SYSTEM_LOG_SOURCES) — 화면의 출처 필터.
    INFO 레코드도 적재하도록 로거 레벨을 INFO 로 낮추되, 콘솔(stderr) 출력
    수준은 루트 핸들러 레벨이 결정하므로 바뀌지 않는다 (cli.main 참조).
    """
    global _source
    if source not in db.SYSTEM_LOG_SOURCES:
        raise ValueError(f"알 수 없는 시스템 로그 출처: {source}")
    with _install_lock:
        _source = source
        app_logger = logging.getLogger(_LOGGER_NAME)
        if app_logger.getEffectiveLevel() > logging.INFO:
            app_logger.setLevel(logging.INFO)
        if not any(isinstance(h, DBLogHandler) for h in app_logger.handlers):
            handler = DBLogHandler()
            app_logger.addHandler(handler)
            # 프로세스 종료 직전 큐 잔량을 마저 적재 (데몬 스레드라 강제 종료됨)
            atexit.register(handler.flush)


def flush(timeout: float = 5.0) -> None:
    """설치된 핸들러의 큐 적재 완료를 대기 (테스트용)."""
    for handler in logging.getLogger(_LOGGER_NAME).handlers:
        if isinstance(handler, DBLogHandler):
            handler.flush(timeout)


def uninstall() -> None:
    """DB 핸들러 제거 (테스트 정리용)."""
    with _install_lock:
        app_logger = logging.getLogger(_LOGGER_NAME)
        for handler in [h for h in app_logger.handlers if isinstance(h, DBLogHandler)]:
            app_logger.removeHandler(handler)
            atexit.unregister(handler.flush)
