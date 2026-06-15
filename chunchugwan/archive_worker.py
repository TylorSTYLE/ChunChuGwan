"""단발 아카이빙 작업 큐(archive_jobs) 소비자.

대시보드의 새/재아카이빙·REST API·CLI `add` 는 캡처를 직접 실행하지 않고
archive_jobs 큐에 작업을 넣는다. 이 모듈의 소비 루프가 worker(또는 serve 단일
프로세스)에서 큐를 꺼내 pipeline.archive_url 을 호출한다 — 캡처 실행 지점을
한 곳으로 통일해, 봇 차단 우회용 스텔스 설정(WCCG_CAPTURE_*)이 소비 프로세스에만
있으면 되게 한다.

크롤(crawler.py)과 같은 'DB 큐 + 원자적 클레임 + 폴링' 패턴이며, 클레임이
db 원자적 UPDATE 라 serve·worker·CLI 동시 실행에 안전하다. 회차·범위·링크
추적·페이싱이 없는 단발이라 crawl_pages 보다 단순하다.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from . import capture, config, crawler, db, live_challenge, pipeline, scheduler

logger = logging.getLogger(__name__)

# 사람 보조(라이브 챌린지)가 기능은 켜졌으나 엔진 게이트 미달로 비활성일 때
# 한 번만 경고하기 위한 프로세스 단위 플래그 (매 작업마다 로그를 도배하지 않게).
_warned_live_gate = False


def _live_session_for(item) -> "live_challenge.LiveChallengeSession | None":
    """이 작업에 줄 라이브 챌린지 세션. 기능 켜짐 + 스텔스 엔진(patchright/headful)일
    때만 만든다 (헤드리스 기본은 사람이 눌러도 가망이 없어 진입하지 않는다).

    기능은 켰는데 엔진이 patchright/headful 이 아니면, 자동으로 못 푼 챌린지가
    조용히 실패로만 떨어진다 — 왜 사람 단계로 안 넘어갔는지 한 번 경고로 남겨
    /system/logs 에서 원인을 알 수 있게 한다 (C)."""
    if not config.LIVE_CHALLENGE:
        return None
    if config.CAPTURE_ENGINE == "patchright" or config.CAPTURE_HEADFUL:
        return live_challenge.LiveChallengeSession(
            item["id"], network_tag_id=item["network_tag_id"]
        )
    global _warned_live_gate
    if not _warned_live_gate:
        _warned_live_gate = True
        logger.warning(
            "WCCG_LIVE_CHALLENGE=on 이지만 캡처 엔진이 스텔스(patchright/headful)가 "
            "아니라 사람 보조가 비활성입니다 — 자동으로 못 푼 챌린지는 그대로 실패 "
            "처리됩니다. WCCG_CAPTURE_ENGINE=patchright 또는 WCCG_CAPTURE_HEADFUL=on "
            "으로 설정하세요."
        )
    return None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


@dataclass
class ArchiveStep:
    """process_next 한 번의 처리 결과 (CLI 진행 표시용)."""

    url: str
    status: str          # ArchiveOutcome.status | 'retry' | 'failed' | 'skipped'
    error: str | None = None


def _handle_failure(item, exc: Exception) -> ArchiveStep:
    """실패 기록 — 시도 횟수가 남았으면 백오프 후 재시도(pending), 아니면 큐에서 삭제.

    백오프는 크롤과 같은 시스템 설정(crawler.retry_backoff) 기준. 오류 상세는
    pipeline 이 archive_logs 에 이미 남겼다 (source 보존).
    """
    attempts = item["attempts"] + 1
    error = f"{type(exc).__name__}: {exc}".splitlines()[0][:500]
    with db.connect() as conn:
        backoff = crawler.retry_backoff(conn)
        next_attempt_at = (
            _iso(_utcnow() + timedelta(seconds=backoff[attempts - 1]))
            if attempts <= len(backoff) else None
        )
        db.fail_archive_job(
            conn, item["id"],
            attempts=attempts, error=error, next_attempt_at=next_attempt_at,
        )
    return ArchiveStep(
        url=item["url"],
        status="retry" if next_attempt_at else "failed",
        error=error,
    )


def process_next(
    *,
    claim: Callable[[str], bool] | None = None,
    release: Callable[[str], None] | None = None,
    archive_fn: Callable[..., pipeline.ArchiveOutcome] | None = None,
    browser_session: "capture.BrowserSession | None" = None,
) -> ArchiveStep | None:
    """기한이 된 아카이빙 작업 하나를 처리. 처리할 것이 없으면 None.

    claim/release 는 크롤·스케줄과 공유하는 진행 중 작업 레지스트리 연동 —
    같은 URL 이 스케줄·크롤로도 동시에 돌지 않게 한다(같은 프로세스 한정).
    archive_fn 은 테스트 주입 지점, browser_session 은 작업 간 브라우저 재사용.
    """
    now = _utcnow()
    with db.connect() as conn:
        recovered = db.recover_stale_archive_jobs(
            conn, _iso(now - timedelta(seconds=config.CRAWL_STALE_CLAIM_SECONDS))
        )
        if recovered:
            logger.warning("중단된 아카이빙 작업 %d개 복구 (pending 으로)", recovered)
        item = db.claim_due_archive_job(conn, _iso(now))
    if item is None:
        return None

    url = item["url"]
    if claim is not None and not claim(url):
        with db.connect() as conn:
            db.release_archive_job(conn, item["id"])
        return ArchiveStep(url=url, status="skipped")

    try:
        try:
            # browser_session·network_tag_id·credential_id 는 있을 때만 넘긴다 —
            # archive_fn 주입(테스트)이 이 인자를 몰라도 동작하게.
            extra = {"browser_session": browser_session} if browser_session else {}
            if item["network_tag_id"]:
                extra["network_tag_id"] = item["network_tag_id"]
            if item["credential_id"]:
                extra["credential_id"] = item["credential_id"]
            if item["requested_by"]:
                extra["requested_by"] = item["requested_by"]
            # 사람 보조(라이브) 모드: 자동으로 안 풀리는 인터랙티브 챌린지를
            # 대시보드에서 사람이 풀 수 있게 세션을 주입한다. 스텔스 + 기능 켜짐일
            # 때만 (헤드리스 기본은 가망 없어 진입 안 함). 작업에 바인딩.
            live_session = _live_session_for(item)
            if live_session is not None:
                extra["live_session"] = live_session
            # 기본 캡처 함수는 호출 시점에 참조한다 (테스트의 monkeypatch 반영)
            fn = archive_fn if archive_fn is not None else pipeline.archive_url
            outcome = fn(
                url, force=bool(item["force"]), source=item["source"], **extra,
            )
        finally:
            if release is not None:
                release(url)
    except Exception as e:
        logger.warning("아카이빙 작업 실패: %s — %s", url, e)
        return _handle_failure(item, e)

    with db.connect() as conn:
        db.finish_archive_job(conn, item["id"])
        if item["credential_id"]:
            # 확장 1회성 세션 자격증명은 소비 후 폐기 (영속 자격증명은 보존).
            # 실패·재시도 분은 만료 GC(delete_expired_ext_credentials)가 정리한다.
            db.delete_ephemeral_credential(conn, item["credential_id"])
    # interval 이 있으면 아카이빙 후 자동 재아카이빙 주기를 등록한다 — 신규 URL 은
    # 아카이빙이 끝나야 pages 행이 생기므로 등록을 여기서 한다.
    if item["interval_seconds"]:
        try:
            scheduler.set_schedule(url, item["interval_seconds"], run_at=item["run_at"])
        except ValueError as e:
            logger.warning("자동 재아카이빙 등록 실패: %s — %s", url, e)
    return ArchiveStep(url=url, status=outcome.status)


def run_loop(
    stop: threading.Event,
    *,
    poll_seconds: int = config.ARCHIVE_POLL_SECONDS,
    claim: Callable[[str], bool] | None = None,
    release: Callable[[str], None] | None = None,
) -> None:
    """stop 이 설정될 때까지 단발 아카이빙 큐를 소비 (serve·worker 백그라운드 스레드용).

    처리할 작업이 있으면 즉시 다음으로 넘어가고, 없을 때만 poll_seconds 만큼
    쉰다. 브라우저는 작업 간 재사용하고, 큐가 비면 내려서 메모리 점유를 피한다.
    """
    logger.info("아카이빙 워커 시작 (폴링 %ds)", poll_seconds)
    # 재시작 — 사람 확인 대기였던 작업은 살아있던 라이브 page 가 사라졌으므로
    # 라이브 상태를 풀고 pending 으로 떨궈 다시 시도하게 한다.
    with db.connect() as conn:
        reset = db.sweep_orphan_needs_human(conn)
    if reset:
        logger.warning("재시작 — 사람 확인 대기였던 작업 %d개를 복구", len(reset))
    with capture.BrowserSession() as session:
        while not stop.is_set():
            step = None
            try:
                step = process_next(
                    claim=claim, release=release, browser_session=session
                )
            except Exception:
                logger.exception("아카이빙 워커 폴링 실패")
            if step is None:
                session.close()  # 다음 작업에서 재기동 — 유휴 중 점유 방지
                if stop.wait(poll_seconds):
                    break
    logger.info("아카이빙 워커 종료")
