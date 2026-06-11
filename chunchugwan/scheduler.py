"""주기적 재아카이빙 스케줄러.

페이지마다 반복 주기(최소 1시간 ~ 최대 1주일)를 등록하면 기한이 된
페이지를 자동으로 다시 아카이빙한다. 실행 경로는 두 가지:

- 대시보드(serve) 프로세스의 백그라운드 폴링 스레드 (`run_loop`)
- `wccg schedule run` — 기한이 된 스케줄을 한 번 실행 (cron 용)

실행 자체는 pipeline.archive_url 을 그대로 사용하므로 결과는
archive_logs(source='schedule')에 기록되고, 콘텐츠가 동일하면
기존 규칙대로 checks 기록만 남는다.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from . import db, pipeline, storage

logger = logging.getLogger(__name__)

MIN_INTERVAL_SECONDS = 3600          # 1시간
MAX_INTERVAL_SECONDS = 7 * 86400     # 1주일

_INTERVAL_RE = re.compile(r"^(\d+)\s*([mhdw])$", re.IGNORECASE)
_UNIT_SECONDS = {"m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}


def validate_interval(seconds: int) -> None:
    """반복 주기 범위 검증 — 1시간 이상 1주일 이하. 위반 시 ValueError."""
    if not (MIN_INTERVAL_SECONDS <= seconds <= MAX_INTERVAL_SECONDS):
        raise ValueError("반복 주기는 1시간(1h) 이상 1주일(1w) 이하여야 합니다")


def parse_interval(text: str) -> int:
    """'90m'/'6h'/'3d'/'1w' 형식을 초로 변환. 형식·범위 위반 시 ValueError."""
    m = _INTERVAL_RE.match(text.strip())
    if m is None:
        raise ValueError(f"잘못된 주기 형식: {text!r} (예: 1h, 90m, 12h, 3d, 1w)")
    seconds = int(m.group(1)) * _UNIT_SECONDS[m.group(2).lower()]
    validate_interval(seconds)
    return seconds


def format_interval(seconds: int) -> str:
    """초를 '1일 12시간' 식 표기로 (목록·대시보드 표시용)."""
    parts: list[str] = []
    for unit, label in ((7 * 86400, "주"), (86400, "일"), (3600, "시간"), (60, "분")):
        n, seconds = divmod(seconds, unit)
        if n:
            parts.append(f"{n}{label}")
    return " ".join(parts) or "0분"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def set_schedule(url: str, interval_seconds: int) -> sqlite3.Row:
    """페이지에 반복 주기를 등록/변경하고 스케줄 row 반환.

    아카이브에 없는 URL 이거나 주기가 범위 밖이면 ValueError.
    다음 실행 시각은 지금 + 주기 (등록 직후 즉시 실행하지 않는다).
    """
    validate_interval(interval_seconds)
    norm = storage.normalize_url(url)
    with db.connect() as conn:
        page = db.get_page(conn, norm)
        if page is None:
            raise ValueError(f"아카이브에 없는 URL: {norm} — 먼저 아카이빙(add)하세요")
        next_run = _iso(_utcnow() + timedelta(seconds=interval_seconds))
        db.upsert_schedule(conn, page["id"], interval_seconds, next_run)
        return db.get_schedule(conn, page["id"])


def remove_schedule(url: str) -> bool:
    """페이지의 스케줄 해제. 등록이 없었으면 False. 잘못된 URL 은 ValueError."""
    norm = storage.normalize_url(url)
    with db.connect() as conn:
        page = db.get_page(conn, norm)
        if page is None:
            return False
        return db.delete_schedule(conn, page["id"])


@dataclass
class DueResult:
    """run_due 항목별 결과."""

    url: str
    status: str          # ArchiveOutcome.status | 'error' | 'skipped'
    error: str | None = None


def run_due(
    *,
    source: str = "schedule",
    claim: Callable[[str], bool] | None = None,
    release: Callable[[str], None] | None = None,
) -> list[DueResult]:
    """기한이 된 스케줄을 순차 실행하고 다음 실행 시각을 갱신.

    claim/release 는 대시보드의 진행 중 작업 레지스트리 연동용 —
    claim 이 False 면 (수동 재아카이빙과 충돌) 이번 회차는 건너뛰고
    next_run_at 도 그대로 두어 다음 폴링에서 재시도한다.
    실패한 실행도 next_run_at 은 주기만큼 미뤄 연속 재시도를 막는다
    (오류 내용은 pipeline 이 archive_logs 에 남긴다).
    """
    with db.connect() as conn:
        due = db.list_due_schedules(conn, _iso(_utcnow()))

    results: list[DueResult] = []
    for sched in due:
        url = sched["url"]
        if claim is not None and not claim(url):
            results.append(DueResult(url=url, status="skipped"))
            continue
        try:
            try:
                outcome = pipeline.archive_url(url, source=source)
                results.append(DueResult(url=url, status=outcome.status))
            except Exception as e:
                logger.exception("스케줄 아카이빙 실패: %s", url)
                results.append(
                    DueResult(url=url, status="error", error=f"{type(e).__name__}: {e}")
                )
        finally:
            if release is not None:
                release(url)
        finished = _utcnow()
        with db.connect() as conn:
            db.mark_schedule_run(
                conn,
                sched["id"],
                last_run_at=_iso(finished),
                next_run_at=_iso(finished + timedelta(seconds=sched["interval_seconds"])),
            )
    return results


def run_loop(
    stop: threading.Event,
    *,
    poll_seconds: int = 60,
    claim: Callable[[str], bool] | None = None,
    release: Callable[[str], None] | None = None,
) -> None:
    """stop 이 설정될 때까지 주기적으로 run_due 실행 (serve 백그라운드 스레드용)."""
    logger.info("스케줄러 시작 (폴링 %ds)", poll_seconds)
    while not stop.wait(poll_seconds):
        try:
            run_due(claim=claim, release=release)
        except Exception:
            logger.exception("스케줄러 폴링 실패")
    logger.info("스케줄러 종료")
