"""백그라운드 아카이빙 워커 — 대시보드 프로세스 밖에서 큐를 소비한다.

`wccg worker` 가 실행한다. 단발 아카이빙 큐(archive_jobs — 새/재아카이빙·
API·CLI add)·페이지 스케줄(schedules)·크롤 큐(crawl_pages)·크롤 스케줄
(crawl_schedules)을 이 프로세스에서 처리해, 아카이빙의 CPU 부하(렌더링·
추출·압축)가 대시보드(serve) 응답을 막지 않게 한다.
serve 와 함께 쓸 때는 serve 쪽 내장 폴링을 WCCG_SCHEDULER=off 로 끈다
(docker-compose.yml 의 dashboard + worker 구성 참조).

크롤 스레드를 여러 개 두면 서로 다른 크롤(사이트)이 병렬로 진행된다 —
같은 크롤은 한 번에 한 페이지만 처리되므로(db.claim_due_crawl_page 의
in_progress 배제 + next_page_at 간격) 대상 서버 부담은 순차 실행과 같다.
클레임이 모두 DB 원자적 UPDATE 라 serve·CLI 와 동시에 돌아도 안전하다.
"""

from __future__ import annotations

import logging
import threading

from . import archive_worker, cluster_sync, config, crawler, scheduler

logger = logging.getLogger(__name__)


class JobRegistry:
    """프로세스 내 진행 중 URL 레지스트리.

    스케줄러 스레드와 크롤 스레드가 같은 URL 을 동시에 아카이빙하지 않게
    한다 (web/app.py 의 _register_job/_unregister_job 과 같은 역할).
    """

    def __init__(self) -> None:
        self._active: set[str] = set()
        self._lock = threading.Lock()

    def claim(self, url: str) -> bool:
        """진행 목록에 등록. 이미 진행 중인 URL 이면 False."""
        with self._lock:
            if url in self._active:
                return False
            self._active.add(url)
            return True

    def release(self, url: str) -> None:
        """진행 목록에서 제거 (완료/실패 공통)."""
        with self._lock:
            self._active.discard(url)


def run(stop: threading.Event, *, crawl_workers: int = 1) -> None:
    """stop 이 설정될 때까지 스케줄러 1개 + 크롤 스레드 crawl_workers개 운영."""
    registry = JobRegistry()
    threads = [
        threading.Thread(
            target=scheduler.run_loop,
            args=(stop,),
            kwargs={
                "poll_seconds": config.SCHEDULER_POLL_SECONDS,
                "claim": registry.claim,
                "release": registry.release,
            },
            name="wccg-worker-scheduler",
            daemon=True,
        ),
        threading.Thread(
            target=archive_worker.run_loop,
            args=(stop,),
            kwargs={"claim": registry.claim, "release": registry.release},
            name="wccg-worker-archive",
            daemon=True,
        ),
        # 클러스터 조정 루프 — 피어별 권한 갱신·델타 동기화 (B 측에서만 동작,
        # 피어가 없으면 사실상 no-op). scheduler 와 같은 폴링 게이트(WCCG_SCHEDULER) 아래.
        threading.Thread(
            target=cluster_sync.run_loop,
            args=(stop,),
            name="wccg-worker-cluster",
            daemon=True,
        ),
    ]
    for i in range(crawl_workers):
        threads.append(
            threading.Thread(
                target=crawler.run_loop,
                args=(stop,),
                kwargs={
                    "claim": registry.claim,
                    "release": registry.release,
                    # 크롤 스케줄 폴링은 첫 스레드만 — 나머지는 큐 소비 전용
                    "run_schedules": i == 0,
                },
                name=f"wccg-worker-crawl-{i + 1}",
                daemon=True,
            )
        )
    for thread in threads:
        thread.start()
    logger.info("워커 시작 — 크롤 스레드 %d개", crawl_workers)
    stop.wait()
    for thread in threads:
        thread.join(timeout=5)
    logger.info("워커 종료")
