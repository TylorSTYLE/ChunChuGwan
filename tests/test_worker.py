"""아카이빙 워커(worker.py) 테스트 — 스레드 구성과 진행 중 레지스트리."""
import threading

from chunchugwan import worker


def test_job_registry_claims_once():
    reg = worker.JobRegistry()
    assert reg.claim("https://example.com/")
    assert not reg.claim("https://example.com/")
    reg.release("https://example.com/")
    assert reg.claim("https://example.com/")
    reg.release("https://nowhere.example.com/")  # 없는 URL 해제는 무해


def test_run_spawns_scheduler_and_crawl_threads(monkeypatch):
    """스케줄러 1개 + 크롤 N개, 크롤 스케줄 폴링은 한 스레드만, 레지스트리 공유."""
    calls = []
    lock = threading.Lock()

    def fake_scheduler_loop(stop, **kwargs):
        with lock:
            calls.append(("scheduler", kwargs))

    def fake_crawler_loop(stop, **kwargs):
        with lock:
            calls.append(("crawl", kwargs))

    monkeypatch.setattr(worker.scheduler, "run_loop", fake_scheduler_loop)
    monkeypatch.setattr(worker.crawler, "run_loop", fake_crawler_loop)

    stop = threading.Event()
    stop.set()  # 루프 스텁이 즉시 반환 — 스레드 구성만 검증
    worker.run(stop, crawl_workers=3)

    scheduler_kwargs = [kw for name, kw in calls if name == "scheduler"]
    crawl_kwargs = [kw for name, kw in calls if name == "crawl"]
    assert len(scheduler_kwargs) == 1
    assert len(crawl_kwargs) == 3
    assert sorted(kw["run_schedules"] for kw in crawl_kwargs) == [False, False, True]
    # 모든 스레드가 같은 레지스트리를 공유해야 같은 URL 의 동시 실행이 막힌다
    registries = {kw["claim"].__self__ for _, kw in calls}
    assert len(registries) == 1
