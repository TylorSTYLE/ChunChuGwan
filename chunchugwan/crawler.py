"""사이트 전체 아카이브 (크롤).

시작 URL 과 같은 호스트의, 시작 URL 경로 프리픽스 이하 페이지들을
링크를 따라가며 시간 간격을 두고 순차 아카이빙한다. 페이지 저장 자체는
pipeline.archive_url 을 그대로 쓴다 (쓰기는 코어 모듈 원칙).

설계 노트:
- 큐는 DB(crawl_pages)에 있다 — 서버 재시작 후에도 이어서 진행된다.
- 페이지 클레임은 원자적 UPDATE(db.claim_due_crawl_page) — serve 폴링
  스레드와 CLI 가 같은 크롤을 동시에 봐도 중복 실행되지 않는다.
- 페이지 간 간격은 crawls.next_page_at 으로 강제 (대상 서버 부담 방지).
- 실패는 백오프 후 재시도(최대 config.CRAWL_MAX_ATTEMPTS 회), 초과 시
  failed 로 남기고 대시보드에서 일괄 재시도할 수 있다.
- 캡처 시 page.html 의 앵커를 `/crawl/{id}/goto?url=...` 리졸버로 재작성해
  아카이브 안에서 링크 이동이 되게 한다 (뷰어 샌드박스의
  allow-top-navigation-by-user-activation 와 한 쌍 — web/app.py 보안 노트).
- 내용이 직전 스냅샷과 같은 페이지는 파이프라인 규칙대로 새 스냅샷을
  만들지 않고, 크롤 세트가 기존 스냅샷을 참조한다 (snapshot_id).
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Sequence
from urllib.parse import quote, urlsplit

from . import config, db, pipeline, storage

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def scope_of(start_url: str) -> tuple[str, str]:
    """정규화된 시작 URL → (호스트, 경로 프리픽스).

    프리픽스는 시작 URL 의 디렉토리 — `/docs/intro` 는 `/docs/` 이하,
    `/docs/` 는 그대로 `/docs/` 이하를 범위로 본다 (wget --no-parent 방식).
    """
    parts = urlsplit(start_url)
    path = parts.path or "/"
    prefix = path if path.endswith("/") else path[: path.rfind("/") + 1]
    return parts.netloc, prefix


def in_scope(url: str, scope_host: str, scope_path: str) -> bool:
    """정규화 URL 이 크롤 범위(같은 호스트 + 경로 프리픽스 이하)인지.

    스킴은 비교하지 않는다 — https 추정 보완·http 폴백(pipeline 참조)으로
    스킴이 갈리는 경우를 같은 페이지로 취급한다.
    """
    parts = urlsplit(url)
    return parts.netloc == scope_host and (parts.path or "/").startswith(scope_path)


def _validate_range(name: str, value: int, lo: int, hi: int) -> None:
    if not (lo <= value <= hi):
        raise ValueError(f"{name}은(는) {lo} 이상 {hi} 이하여야 합니다 (현재 {value})")


def start_crawl(
    url: str,
    *,
    max_pages: int = config.CRAWL_DEFAULT_MAX_PAGES,
    max_depth: int = config.CRAWL_DEFAULT_MAX_DEPTH,
    delay_seconds: int = config.CRAWL_DEFAULT_DELAY_SECONDS,
    source: str = "web",
) -> sqlite3.Row:
    """크롤을 등록하고(시작 URL 을 큐에 넣고) 크롤 row 를 반환.

    실행은 등록과 분리되어 있다 — serve 의 크롤러 스레드 또는
    run_crawl/process_next 가 큐를 소비한다. 옵션 범위 위반은 ValueError.
    """
    norm = storage.normalize_url(url)
    _validate_range("최대 페이지 수", max_pages, 1, config.CRAWL_MAX_PAGES_LIMIT)
    _validate_range("최대 깊이", max_depth, 0, config.CRAWL_MAX_DEPTH_LIMIT)
    _validate_range(
        "페이지 간 간격(초)", delay_seconds,
        config.CRAWL_MIN_DELAY_SECONDS, config.CRAWL_MAX_DELAY_SECONDS,
    )
    scope_host, scope_path = scope_of(norm)
    with db.connect() as conn:
        crawl_id = db.insert_crawl(
            conn,
            start_url=norm, scope_host=scope_host, scope_path=scope_path,
            max_pages=max_pages, max_depth=max_depth,
            delay_seconds=delay_seconds, source=source,
        )
        db.insert_crawl_page(conn, crawl_id, norm, 0)
        return db.get_crawl(conn, crawl_id)


def _normalize_http(href: str) -> str | None:
    """브라우저가 해석한 절대 href → 정규화 URL. http(s) 가 아니면 None.

    명시적 http(s) 스킴을 요구한다 — normalize_url 의 스킴 추정 보완이
    mailto: 같은 비웹 링크를 엉뚱한 URL 로 만드는 것을 막는다.
    """
    if not href.startswith(("http://", "https://")):
        return None
    try:
        return storage.normalize_url(href)
    except ValueError:
        return None


def link_rewriter(crawl_id: int) -> Callable[[Sequence[str]], dict[str, str]]:
    """page.html 앵커 재작성 매핑 생성기 — capture.LinkRewriter.

    http(s) 링크를 전부 리졸버로 보낸다 (범위 밖 포함 — 리졸버가 스냅샷
    유무를 판정하고, 없으면 원본 링크를 안내한다). mailto:/javascript: 등
    비웹 링크는 그대로 둔다.
    """

    def rewrite(hrefs: Sequence[str]) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for href in hrefs:
            norm = _normalize_http(href)
            if norm is None:
                continue
            mapping[href] = f"/crawl/{crawl_id}/goto?url={quote(norm, safe='')}"
        return mapping

    return rewrite


def _scoped_links(
    links: Sequence[str], scope_host: str, scope_path: str
) -> list[str]:
    """앵커 href 목록 → 큐 추가 후보 (정규화·범위 필터·중복 제거, 순서 보존)."""
    out: dict[str, None] = {}
    for href in links:
        norm = _normalize_http(href)
        if norm is not None and in_scope(norm, scope_host, scope_path):
            out[norm] = None
    return list(out)


@dataclass
class CrawlStep:
    """process_next 한 번의 처리 결과 (CLI 진행 표시용)."""

    crawl_id: int
    url: str
    status: str          # ArchiveOutcome.status | 'retry' | 'failed' | 'skipped'
    error: str | None = None
    enqueued: int = 0    # 이번에 큐에 추가된 링크 수
    crawl_done: bool = False


def _handle_failure(item: sqlite3.Row, exc: Exception) -> CrawlStep:
    """실패 기록 — 시도 횟수가 남았으면 백오프 후 재시도, 아니면 failed.

    오류 상세는 pipeline 이 archive_logs 에 이미 남겼다 (source='crawl').
    """
    attempts = item["attempts"] + 1
    error = f"{type(exc).__name__}: {exc}".splitlines()[0][:500]
    next_attempt_at: str | None = None
    if attempts < config.CRAWL_MAX_ATTEMPTS:
        backoff = config.CRAWL_RETRY_BACKOFF_SECONDS[
            min(attempts - 1, len(config.CRAWL_RETRY_BACKOFF_SECONDS) - 1)
        ]
        next_attempt_at = _iso(_utcnow() + timedelta(seconds=backoff))
    with db.connect() as conn:
        db.fail_crawl_page(
            conn, item["id"],
            attempts=attempts, error=error, next_attempt_at=next_attempt_at,
        )
        crawl_done = (
            db.finish_crawl_if_done(conn, item["crawl_id"])
            if next_attempt_at is None else False
        )
    return CrawlStep(
        crawl_id=item["crawl_id"], url=item["url"],
        status="retry" if next_attempt_at else "failed",
        error=error, crawl_done=crawl_done,
    )


def _enqueue_links(
    conn: sqlite3.Connection, item: sqlite3.Row, links: Sequence[str]
) -> int:
    """발견된 링크를 깊이/페이지 수 한도 안에서 큐에 추가하고 추가 수 반환."""
    if item["depth"] >= item["max_depth"]:
        return 0
    budget = item["max_pages"] - db.count_crawl_pages(conn, item["crawl_id"])
    enqueued = 0
    for link in _scoped_links(links, item["scope_host"], item["scope_path"]):
        if budget <= 0:
            break
        if db.insert_crawl_page(conn, item["crawl_id"], link, item["depth"] + 1):
            enqueued += 1
            budget -= 1
    return enqueued


def process_next(
    *,
    crawl_id: int | None = None,
    claim: Callable[[str], bool] | None = None,
    release: Callable[[str], None] | None = None,
    archive_fn: Callable[..., pipeline.ArchiveOutcome] = pipeline.archive_url,
) -> CrawlStep | None:
    """기한이 된 크롤 페이지 하나를 처리. 처리할 것이 없으면 None.

    claim/release 는 대시보드의 진행 중 작업 레지스트리 연동 — claim 실패
    (수동 재아카이빙과 충돌) 시 클레임을 반납하고 다음 폴링에 맡긴다.
    archive_fn 은 테스트용 주입 지점 (기본 pipeline.archive_url).
    """
    now = _utcnow()
    with db.connect() as conn:
        recovered = db.recover_stale_crawl_pages(
            conn, _iso(now - timedelta(seconds=config.CRAWL_STALE_CLAIM_SECONDS))
        )
        if recovered:
            logger.warning("중단된 크롤 페이지 %d개 복구 (pending 으로)", recovered)
        item = db.claim_due_crawl_page(conn, _iso(now), crawl_id)
    if item is None:
        return None

    url = item["url"]
    if claim is not None and not claim(url):
        with db.connect() as conn:
            db.release_crawl_page(conn, item["id"])
        return CrawlStep(crawl_id=item["crawl_id"], url=url, status="skipped")

    try:
        try:
            outcome = archive_fn(
                url, source="crawl", link_rewriter=link_rewriter(item["crawl_id"])
            )
        finally:
            if release is not None:
                release(url)
    except Exception as e:
        logger.warning("크롤 페이지 실패: %s — %s", url, e)
        return _handle_failure(item, e)

    with db.connect() as conn:
        db.finish_crawl_page(conn, item["id"], outcome.snapshot_id)
        enqueued = _enqueue_links(conn, item, outcome.page_links)
        crawl_done = db.finish_crawl_if_done(conn, item["crawl_id"])
    return CrawlStep(
        crawl_id=item["crawl_id"], url=url, status=outcome.status,
        enqueued=enqueued, crawl_done=crawl_done,
    )


def run_crawl(
    crawl_id: int,
    *,
    on_step: Callable[[CrawlStep], None] | None = None,
    archive_fn: Callable[..., pipeline.ArchiveOutcome] = pipeline.archive_url,
) -> sqlite3.Row:
    """크롤 하나를 완료/취소될 때까지 동기 실행 (CLI 용).

    페이지 간 간격·재시도 백오프 동안은 대기한다. 완료된 크롤 row 반환.
    """
    while True:
        with db.connect() as conn:
            crawl = db.get_crawl(conn, crawl_id)
        if crawl is None:
            raise ValueError(f"크롤 없음: {crawl_id}")
        if crawl["status"] != "running":
            return crawl
        step = process_next(crawl_id=crawl_id, archive_fn=archive_fn)
        if step is not None and on_step is not None:
            on_step(step)
        if step is None:
            time.sleep(1)


def run_loop(
    stop: threading.Event,
    *,
    poll_seconds: int = config.CRAWLER_POLL_SECONDS,
    claim: Callable[[str], bool] | None = None,
    release: Callable[[str], None] | None = None,
) -> None:
    """stop 이 설정될 때까지 크롤 큐를 소비 (serve 백그라운드 스레드용).

    처리할 페이지가 있으면 즉시 다음으로 넘어가고 (간격은 next_page_at 이
    강제한다), 없을 때만 poll_seconds 만큼 쉰다.
    """
    logger.info("크롤러 시작 (폴링 %ds)", poll_seconds)
    while not stop.is_set():
        step = None
        try:
            step = process_next(claim=claim, release=release)
        except Exception:
            logger.exception("크롤러 폴링 실패")
        if step is not None and step.crawl_done:
            logger.info("크롤 완료: #%d", step.crawl_id)
        if step is None and stop.wait(poll_seconds):
            break
    logger.info("크롤러 종료")
