"""사이트 전체 아카이브 (크롤).

시작 URL 과 같은 호스트의, 시작 URL 경로 프리픽스 이하 페이지들을
링크를 따라가며 시간 간격을 두고 순차 아카이빙한다. 페이지 저장 자체는
pipeline.archive_url 을 그대로 쓴다 (쓰기는 코어 모듈 원칙).

설계 노트:
- 큐는 DB(crawl_pages)에 있다 — 서버 재시작 후에도 이어서 진행된다.
- 페이지 클레임은 원자적 UPDATE(db.claim_due_crawl_page) — serve 폴링
  스레드와 CLI 가 같은 크롤을 동시에 봐도 중복 실행되지 않는다.
- 페이지 간 간격은 crawls.next_page_at 으로 강제 (대상 서버 부담 방지).
- 실패는 백오프 후 재시도(대기 시간·횟수는 시스템 설정 retry_backoff,
  최대 시도 = 대기 목록 길이 + 1), 초과 시 failed 로 남기고 대시보드에서
  일괄 재시도할 수 있다.
- 같은 시작 URL 의 크롤이 진행 중이면 새 등록은 그 크롤로 자동 병합된다
  (start_crawl 이 기존 크롤을 반환) — 같은 사이트를 두 번 돌지 않는다.
- 크롤 스케줄(crawl_schedules)은 기한이 되면 같은 옵션으로 새 크롤을
  등록한다 — run_due_schedules 참조. 같은 시작 URL 의 크롤이 진행 중이면
  병합하지 않고 끝날 때까지 미룬다 (주기적 재수집이 건너뛰어지지 않게).
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

from . import capture, config, db, netcheck, pipeline, scheduler, storage

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
    """정규화 URL 이 크롤 범위(같은 사이트 호스트 + 경로 프리픽스 이하)인지.

    스킴은 비교하지 않는다 — https 추정 보완·http 폴백(pipeline 참조)으로
    스킴이 갈리는 경우를 같은 페이지로 취급한다. 호스트는 사이트 키
    기준으로 비교한다 — www 와 apex 를 혼용하는 사이트의 내부 링크가
    범위 밖으로 버려지지 않게 한다 (다른 서브도메인은 여전히 범위 밖).
    """
    parts = urlsplit(url)
    return (
        storage.netloc_site_key(parts.netloc) == storage.netloc_site_key(scope_host)
        and (parts.path or "/").startswith(scope_path)
    )


def _validate_range(name: str, value: int, lo: int, hi: int) -> None:
    if not (lo <= value <= hi):
        raise ValueError(f"{name}은(는) {lo} 이상 {hi} 이하여야 합니다 (현재 {value})")


def validate_options(
    max_pages: int,
    max_depth: int,
    delay_seconds: int,
    conn: sqlite3.Connection | None = None,
) -> None:
    """크롤 옵션 범위 검증. 위반 시 ValueError.

    상한(최대값)은 시스템 설정(crawl_limits) 기준 — conn 이 주어지면 설정값을, None
    이면 config 기본 상한을 쓴다. 지연 하한은 항상 config.CRAWL_MIN_DELAY_SECONDS.
    """
    limits = (
        crawl_limits(conn)
        if conn is not None
        else {
            "max_pages": config.CRAWL_MAX_PAGES_LIMIT,
            "max_depth": config.CRAWL_MAX_DEPTH_LIMIT,
            "max_delay": config.CRAWL_MAX_DELAY_SECONDS,
        }
    )
    _validate_range("최대 페이지 수", max_pages, 1, limits["max_pages"])
    _validate_range("최대 깊이", max_depth, 0, limits["max_depth"])
    _validate_range(
        "페이지 간 간격(초)", delay_seconds,
        config.CRAWL_MIN_DELAY_SECONDS, limits["max_delay"],
    )


def validate_limits(max_pages: int, max_depth: int, max_delay: int) -> None:
    """크롤 상한(최대값) 설정값이 절대 천장(ceiling) 이내인지 검증. 위반 시 ValueError."""
    _validate_range("최대 페이지 상한", max_pages, 1, config.CRAWL_MAX_PAGES_CEILING)
    _validate_range("최대 깊이 상한", max_depth, 0, config.CRAWL_MAX_DEPTH_CEILING)
    _validate_range(
        "지연(초) 상한", max_delay,
        config.CRAWL_MIN_DELAY_SECONDS, config.CRAWL_MAX_DELAY_CEILING,
    )


# ---- 시스템 설정 (settings 테이블) ----
# 기본 옵션과 재시도 대기는 대시보드 시스템 화면에서 바꾼다. 값이 없거나
# 오염됐으면 config 기본값으로 폴백 — 설정 화면의 검증을 우회해 DB 를 직접
# 고친 경우에도 크롤이 멈추지 않게 한다.


def _setting_int(conn: sqlite3.Connection, key: str, default: int, lo: int, hi: int) -> int:
    """정수 설정 값 조회 — 없거나 숫자가 아니거나 범위 밖이면 default."""
    raw = db.get_setting(conn, key)
    try:
        value = int(raw) if raw is not None else default
    except ValueError:
        return default
    return value if lo <= value <= hi else default


def crawl_limits(conn: sqlite3.Connection) -> dict[str, int]:
    """크롤 옵션 상한(최대값) — 시스템 설정 기준, 없으면 config 기본 상한.

    관리자가 설정한 상한이며, 각 값은 절대 천장(ceiling) 이내로 폴백 검증한다.
    """
    return {
        "max_pages": _setting_int(
            conn, db.CRAWL_LIMIT_MAX_PAGES_KEY,
            config.CRAWL_MAX_PAGES_LIMIT, 1, config.CRAWL_MAX_PAGES_CEILING,
        ),
        "max_depth": _setting_int(
            conn, db.CRAWL_LIMIT_MAX_DEPTH_KEY,
            config.CRAWL_MAX_DEPTH_LIMIT, 0, config.CRAWL_MAX_DEPTH_CEILING,
        ),
        "max_delay": _setting_int(
            conn, db.CRAWL_LIMIT_MAX_DELAY_KEY,
            config.CRAWL_MAX_DELAY_SECONDS,
            config.CRAWL_MIN_DELAY_SECONDS, config.CRAWL_MAX_DELAY_CEILING,
        ),
    }


def crawl_defaults(conn: sqlite3.Connection) -> dict[str, int]:
    """크롤 옵션 기본값 (max_pages, max_depth, delay_seconds) — 시스템 설정 기준.

    상한(crawl_limits) 이내로 클램프한다 — 상한을 낮추면 기본값도 그 이하로 읽힌다.
    """
    limits = crawl_limits(conn)
    return {
        "max_pages": _setting_int(
            conn, db.CRAWL_DEFAULT_MAX_PAGES_KEY,
            min(config.CRAWL_DEFAULT_MAX_PAGES, limits["max_pages"]),
            1, limits["max_pages"],
        ),
        "max_depth": _setting_int(
            conn, db.CRAWL_DEFAULT_MAX_DEPTH_KEY,
            min(config.CRAWL_DEFAULT_MAX_DEPTH, limits["max_depth"]),
            0, limits["max_depth"],
        ),
        "delay_seconds": _setting_int(
            conn, db.CRAWL_DEFAULT_DELAY_KEY,
            min(config.CRAWL_DEFAULT_DELAY_SECONDS, limits["max_delay"]),
            config.CRAWL_MIN_DELAY_SECONDS, limits["max_delay"],
        ),
    }


def parse_backoff(text: str) -> tuple[int, ...]:
    """쉼표 구분 초 목록('300, 900') → 재시도 대기 튜플. 형식·범위 위반 시 ValueError.

    대기 목록 길이가 곧 재시도 횟수다 — 페이지당 최대 시도 = 길이 + 1.
    """
    try:
        values = tuple(int(p.strip()) for p in text.split(",") if p.strip())
    except ValueError:
        raise ValueError(
            "재시도 대기는 쉼표로 구분한 초 단위 숫자 목록이어야 합니다 (예: 300, 900)"
        )
    if not (1 <= len(values) <= config.CRAWL_RETRY_BACKOFF_MAX_STEPS):
        raise ValueError(
            f"재시도 대기는 1개 이상 {config.CRAWL_RETRY_BACKOFF_MAX_STEPS}개 이하여야 합니다"
        )
    for v in values:
        _validate_range(
            "재시도 대기(초)", v,
            config.CRAWL_RETRY_BACKOFF_MIN_SECONDS, config.CRAWL_RETRY_BACKOFF_MAX_SECONDS,
        )
    return values


def retry_backoff(conn: sqlite3.Connection) -> tuple[int, ...]:
    """실패 재시도 대기(초) 목록 — 시스템 설정 기준, 진행 중 크롤에도 즉시 적용."""
    raw = db.get_setting(conn, db.CRAWL_RETRY_BACKOFF_KEY)
    if raw is None:
        return config.CRAWL_RETRY_BACKOFF_SECONDS
    try:
        return parse_backoff(raw)
    except ValueError:
        return config.CRAWL_RETRY_BACKOFF_SECONDS


def _resolve_options(
    conn: sqlite3.Connection,
    max_pages: int | None,
    max_depth: int | None,
    delay_seconds: int | None,
) -> tuple[int, int, int]:
    """None 옵션을 시스템 설정 기본값으로 채우고 범위 검증 후 반환."""
    defaults = crawl_defaults(conn)
    max_pages = defaults["max_pages"] if max_pages is None else max_pages
    max_depth = defaults["max_depth"] if max_depth is None else max_depth
    delay_seconds = (
        defaults["delay_seconds"] if delay_seconds is None else delay_seconds
    )
    validate_options(max_pages, max_depth, delay_seconds, conn)
    return max_pages, max_depth, delay_seconds


def _check_network_tag(
    conn: sqlite3.Connection, host: str, network_tag_id: str | None
) -> str | None:
    """크롤·크롤 스케줄의 네트워크 게이트 (netcheck — pipeline 과 같은 정책).

    루프백은 ValueError. 사설 대역은 등록된 태그 id 필수, 공인이면 태그 무시.
    """
    kind = netcheck.classify_host(host)
    if kind == netcheck.LOOPBACK:
        raise ValueError(f"루프백 주소는 아카이빙할 수 없습니다: {host}")
    if kind != netcheck.PRIVATE:
        return None
    if not network_tag_id:
        raise ValueError(
            "로컬 네트워크(사설 IP) 사이트는 로컬 네트워크 태그를 지정해야 "
            "아카이빙할 수 있습니다 — 시스템 화면에서 태그를 만들고 "
            "새 아카이빙 화면에서 선택하세요"
        )
    if db.get_network_tag(conn, network_tag_id) is None:
        raise ValueError(f"등록되지 않은 로컬 네트워크 태그: {network_tag_id}")
    return network_tag_id


def start_crawl(
    url: str,
    *,
    max_pages: int | None = None,
    max_depth: int | None = None,
    delay_seconds: int | None = None,
    source: str = "web",
    requested_by: int | None = None,
    network_tag_id: str | None = None,
    credential_id: int | None = None,
) -> tuple[sqlite3.Row, bool]:
    """크롤을 등록하고(시작 URL 을 큐에 넣고) (크롤 row, 병합 여부)를 반환.

    같은 시작 URL 의 크롤이 이미 진행 중(running)이면 새 크롤을 만들지 않고
    그 크롤로 자동 병합한다 — 기존 row 와 merged=True 를 반환하며, 이번에
    넘긴 옵션은 범위 검증만 하고 버린다 (진행 중 크롤의 옵션 유지). 검사는
    best-effort — 정확히 동시에 등록하는 드문 경우에는 별도 크롤이
    만들어질 수 있다.

    실행은 등록과 분리되어 있다 — serve 의 크롤러 스레드 또는
    run_crawl/process_next 가 큐를 소비한다. 옵션이 None 이면 시스템 설정의
    기본값(crawl_defaults)을 쓴다. 옵션 범위 위반은 ValueError.
    network_tag_id 는 사설 대역(로컬 네트워크) 사이트일 때 필수 — 크롤이
    아카이빙하는 모든 페이지에 적용된다 (루프백은 항상 ValueError).
    명시적 http 시작 URL 은 https 지원이 확인되면 https 로 승격해 등록한다
    (pipeline.upgrade_http_to_https — 게이트 통과 후 프로브).
    """
    norm = storage.normalize_url(url)
    with db.connect() as conn:
        network_tag_id = _check_network_tag(
            conn, urlsplit(norm).hostname or "", network_tag_id
        )
    norm = pipeline.upgrade_http_to_https(norm)
    scope_host, scope_path = scope_of(norm)
    with db.connect() as conn:
        max_pages, max_depth, delay_seconds = _resolve_options(
            conn, max_pages, max_depth, delay_seconds
        )
        running = db.find_running_crawl(conn, norm)
        if running is not None:
            return running, True
        crawl_id = db.insert_crawl(
            conn,
            start_url=norm, scope_host=scope_host, scope_path=scope_path,
            max_pages=max_pages, max_depth=max_depth,
            delay_seconds=delay_seconds, source=source, requested_by=requested_by,
            network_tag_id=network_tag_id, credential_id=credential_id,
        )
        db.insert_crawl_page(conn, crawl_id, norm, 0)
        crawl = db.get_crawl(conn, crawl_id)
        assert crawl is not None  # insert_crawl 직후라 None 불가
        return crawl, False


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

    백오프는 시스템 설정(retry_backoff) 기준 — n차 실패 후 n번째 대기,
    대기 목록이 끝나면 failed. 오류 상세는 pipeline 이 archive_logs 에
    이미 남겼다 (source='crawl').
    """
    attempts = item["attempts"] + 1
    error = f"{type(exc).__name__}: {exc}".splitlines()[0][:500]
    with db.connect() as conn:
        backoff = retry_backoff(conn)
        next_attempt_at = (
            _iso(_utcnow() + timedelta(seconds=backoff[attempts - 1]))
            if attempts <= len(backoff) else None
        )
        db.fail_crawl_page(
            conn, item["id"],
            attempts=attempts, error=error, next_attempt_at=next_attempt_at,
        )
        crawl_done = (
            db.finish_crawl_if_done(conn, item["crawl_id"])
            if next_attempt_at is None else False
        )
    # 재시도 예약인지 최종 실패인지 구분해 남긴다 (archive_worker 와 같은 형식 —
    # 시도 횟수·전체 한도·다음 시도 시각). 전체 한도는 첫 시도 + 백오프 재시도 횟수.
    max_attempts = len(backoff) + 1
    if next_attempt_at:
        logger.warning(
            "크롤 페이지 실패: %s — %s (크롤 #%d, 시도 %d/%d, %s 재시도)",
            item["url"], error, item["crawl_id"], attempts, max_attempts, next_attempt_at,
        )
    else:
        logger.warning(
            "크롤 페이지 최종 실패: %s — %s (크롤 #%d, 시도 %d/%d 소진)",
            item["url"], error, item["crawl_id"], attempts, max_attempts,
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
    browser_session: "capture.BrowserSession | None" = None,
) -> CrawlStep | None:
    """기한이 된 크롤 페이지 하나를 처리. 처리할 것이 없으면 None.

    claim/release 는 대시보드의 진행 중 작업 레지스트리 연동 — claim 실패
    (수동 재아카이빙과 충돌) 시 클레임을 반납하고 다음 폴링에 맡긴다.
    archive_fn 은 테스트용 주입 지점 (기본 pipeline.archive_url).
    browser_session 은 페이지 간 브라우저 재사용 (capture.BrowserSession).
    """
    now = _utcnow()
    with db.connect() as conn:
        # 쓰기 일시중지 — 인스턴스 이전 OR 스토리지 마이그레이션 진행 중이면 크롤 처리 중단
        if db.writes_paused(conn):
            return None
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
            # browser_session·network_tag_id 는 줄 때만 넘긴다 — archive_fn
            # 주입(테스트)이 이 인자를 몰라도 동작하게.
            extra = {"browser_session": browser_session} if browser_session else {}
            if item["network_tag_id"]:
                extra["network_tag_id"] = item["network_tag_id"]
            if item["credential_id"]:
                extra["credential_id"] = item["credential_id"]
            outcome = archive_fn(
                url, source="crawl", link_rewriter=link_rewriter(item["crawl_id"]),
                **extra,
            )
        finally:
            if release is not None:
                release(url)
    except Exception as e:
        # 실패 로그(재시도/최종 구분·시도 횟수)는 _handle_failure 가 남긴다.
        return _handle_failure(item, e)

    with db.connect() as conn:
        db.finish_crawl_page(conn, item["id"], outcome.snapshot_id)
        enqueued = _enqueue_links(conn, item, outcome.page_links)
        crawl_done = db.finish_crawl_if_done(conn, item["crawl_id"])
    logger.info(
        "크롤 페이지 완료: %s — %s (크롤 #%d, 링크 +%d)",
        url, outcome.status, item["crawl_id"], enqueued,
    )
    return CrawlStep(
        crawl_id=item["crawl_id"], url=url, status=outcome.status,
        enqueued=enqueued, crawl_done=crawl_done,
    )


# ---- 사이트 아카이브 스케줄 (주기적 재크롤) ----


def set_crawl_schedule(
    url: str,
    interval_seconds: int,
    run_at: str | None = None,
    *,
    max_pages: int | None = None,
    max_depth: int | None = None,
    delay_seconds: int | None = None,
    network_tag_id: str | None = None,
    credential_id: int | None = None,
) -> sqlite3.Row:
    """시작 URL 에 주기적 사이트 아카이브를 등록/변경하고 스케줄 row 반환.

    주기 규칙은 페이지 스케줄과 동일(1시간~1개월, 1일 단위 주기는 실행 시각
    지정 가능). 다음 실행은 지금 + 주기 — 보통 등록과 함께 첫 크롤을 따로
    시작하므로 즉시 실행하지 않는다. 옵션이 None 이면 시스템 설정 기본값.
    주기·옵션 범위 위반은 ValueError. network_tag_id 는 사설 대역 사이트일
    때 필수 — 주기 실행으로 만드는 크롤에 그대로 적용된다. 명시적 http
    시작 URL 은 https 지원이 확인되면 https 로 승격해 저장한다.
    """
    scheduler.validate_interval(interval_seconds)
    if run_at:
        scheduler.validate_run_at(run_at, interval_seconds)
    norm = storage.normalize_url(url)
    with db.connect() as conn:
        network_tag_id = _check_network_tag(
            conn, urlsplit(norm).hostname or "", network_tag_id
        )
    norm = pipeline.upgrade_http_to_https(norm)
    with db.connect() as conn:
        max_pages, max_depth, delay_seconds = _resolve_options(
            conn, max_pages, max_depth, delay_seconds
        )
        next_run = _iso(
            scheduler.next_run_after(_utcnow(), interval_seconds, run_at)
        )
        db.upsert_crawl_schedule(
            conn, norm,
            max_pages=max_pages, max_depth=max_depth, delay_seconds=delay_seconds,
            interval_seconds=interval_seconds, next_run_at=next_run,
            run_at_time=run_at, network_tag_id=network_tag_id,
            credential_id=credential_id,
        )
        schedule = db.get_crawl_schedule(conn, norm)
        assert schedule is not None  # upsert 직후라 None 불가
        return schedule


def set_crawl_schedule_next_run(schedule_id: int, next_run: datetime) -> sqlite3.Row:
    """크롤 스케줄의 다음 실행 시각을 직접 변경하고 갱신된 row 반환.

    없는 id 면 ValueError. naive datetime 은 UTC 로 간주, 과거 시각도
    허용한다 — 다음 폴링 회차에서 즉시 실행된다.
    """
    if next_run.tzinfo is None:
        next_run = next_run.replace(tzinfo=timezone.utc)
    with db.connect() as conn:
        if not db.set_crawl_schedule_next_run(
            conn, schedule_id, _iso(next_run.astimezone(timezone.utc))
        ):
            raise ValueError(f"등록된 크롤 스케줄이 없습니다: {schedule_id}")
        schedule = db.get_crawl_schedule_by_id(conn, schedule_id)
        assert schedule is not None  # 직전 갱신 성공(True) 이후라 None 불가
        return schedule


def remove_crawl_schedule(url: str) -> bool:
    """시작 URL 의 크롤 스케줄 해제. 등록이 없었으면 False. 잘못된 URL 은 ValueError."""
    norm = storage.normalize_url(url)
    with db.connect() as conn:
        sched = db.get_crawl_schedule(conn, norm)
        if sched is None:
            return False
        return db.delete_crawl_schedule(conn, sched["id"])


@dataclass
class ScheduleStep:
    """run_due_schedules 항목별 결과."""

    start_url: str
    status: str          # 'started' | 'deferred' | 'error'
    crawl_id: int | None = None
    error: str | None = None


def run_due_schedules(*, source: str = "schedule") -> list[ScheduleStep]:
    """기한이 된 크롤 스케줄로 새 크롤을 등록하고 다음 실행 시각을 갱신.

    등록만 한다 — 페이지 처리는 크롤러(run_loop / `wccg crawl run`)가
    큐를 소비하며 진행한다. 같은 시작 URL 의 크롤이 아직 진행 중이면
    이번 회차는 미룬다 (next_run_at 유지 — 크롤이 끝난 뒤 폴링에서 시작).
    next_run_at 갱신은 원자적 클레임이라 serve 폴링과 cron 이 동시에 봐도
    중복 등록되지 않는다. 등록 실패도 next_run_at 은 미뤄 연속 재시도를 막는다.
    """
    with db.connect() as conn:
        # 쓰기 일시중지 — 인스턴스 이전 OR 스토리지 마이그레이션 진행 중이면 크롤 스케줄 실행 중단
        if db.writes_paused(conn):
            return []
        due = db.list_due_crawl_schedules(conn, _iso(_utcnow()))

    results: list[ScheduleStep] = []
    for sched in due:
        url = sched["start_url"]
        with db.connect() as conn:
            if db.find_running_crawl(conn, url) is not None:
                results.append(ScheduleStep(start_url=url, status="deferred"))
                continue
            now = _utcnow()
            next_run = scheduler.next_run_after(
                now, sched["interval_seconds"], sched["run_at_time"]
            )
            claimed = db.claim_crawl_schedule(
                conn, sched["id"], sched["next_run_at"],
                last_run_at=_iso(now), next_run_at=_iso(next_run),
            )
        if not claimed:
            continue  # 다른 프로세스가 이미 실행했다
        try:
            crawl, merged = start_crawl(
                url,
                max_pages=sched["max_pages"], max_depth=sched["max_depth"],
                delay_seconds=sched["delay_seconds"], source=source,
                network_tag_id=sched["network_tag_id"],
                credential_id=sched["credential_id"],
            )
            if merged:
                # 미루기 검사 직후 다른 경로가 같은 크롤을 등록한 드문 경우 —
                # 이번 회차는 그 크롤에 병합된 것으로 친다 (next_run_at 은 이미 갱신됨)
                logger.info("크롤 스케줄 병합: %s → 진행 중 크롤 #%d", url, crawl["id"])
            results.append(
                ScheduleStep(start_url=url, status="started", crawl_id=crawl["id"])
            )
        except ValueError as e:
            logger.warning("크롤 스케줄 등록 실패: %s — %s", url, e)
            results.append(ScheduleStep(start_url=url, status="error", error=str(e)))
    return results


def run_crawl(
    crawl_id: int,
    *,
    on_step: Callable[[CrawlStep], None] | None = None,
    archive_fn: Callable[..., pipeline.ArchiveOutcome] = pipeline.archive_url,
) -> sqlite3.Row:
    """크롤 하나를 완료/취소될 때까지 동기 실행 (CLI 용).

    페이지 간 간격·재시도 백오프 동안은 대기한다. 완료된 크롤 row 반환.
    브라우저는 크롤 동안 재사용한다 (BrowserSession).
    """
    session = (
        capture.BrowserSession() if archive_fn is pipeline.archive_url else None
    )
    try:
        while True:
            with db.connect() as conn:
                crawl = db.get_crawl(conn, crawl_id)
            if crawl is None:
                raise ValueError(f"크롤 없음: {crawl_id}")
            if crawl["status"] != "running":
                return crawl
            step = process_next(
                crawl_id=crawl_id, archive_fn=archive_fn, browser_session=session
            )
            if step is not None and on_step is not None:
                on_step(step)
            if step is None:
                time.sleep(1)
    finally:
        if session is not None:
            session.close()


def run_loop(
    stop: threading.Event,
    *,
    poll_seconds: int = config.CRAWLER_POLL_SECONDS,
    claim: Callable[[str], bool] | None = None,
    release: Callable[[str], None] | None = None,
    run_schedules: bool = True,
) -> None:
    """stop 이 설정될 때까지 크롤 큐를 소비 (serve·워커 백그라운드 스레드용).

    기한이 된 크롤 스케줄도 매 회차 등록한다 (run_due_schedules) —
    같은 프로세스에 크롤 스레드가 여럿이면(워커) 한 스레드만
    run_schedules=True 로 두면 된다 (중복 등록은 안 되지만 폴링 낭비).
    처리할 페이지가 있으면 즉시 다음으로 넘어가고 (간격은 next_page_at 이
    강제한다), 없을 때만 poll_seconds 만큼 쉰다. 브라우저는 페이지 간
    재사용하고, 큐가 비면 내려서 메모리 점유를 피한다.
    """
    logger.info("크롤러 시작 (폴링 %ds)", poll_seconds)
    with capture.BrowserSession() as session:
        last_active = time.monotonic()
        while not stop.is_set():
            step = None
            try:
                if run_schedules:
                    for fired in run_due_schedules():
                        if fired.status == "started":
                            logger.info(
                                "크롤 스케줄 실행: %s → #%d",
                                fired.start_url, fired.crawl_id,
                            )
                step = process_next(
                    claim=claim, release=release, browser_session=session
                )
            except Exception:
                logger.exception("크롤러 폴링 실패")
            if step is not None and step.crawl_done:
                logger.info("크롤 완료: #%d", step.crawl_id)
            if step is not None:
                last_active = time.monotonic()
            else:
                # 큐가 빈 뒤 grace 를 넘겨야 close — 페이지 간 간격(next_page_at)
                # 때문에 잠깐 비는 사이마다 재기동하는 스래싱을 막는다 (close 는 멱등).
                if time.monotonic() - last_active >= config.BROWSER_IDLE_CLOSE_SECONDS:
                    session.close()
                if stop.wait(poll_seconds):
                    break
    logger.info("크롤러 종료")
