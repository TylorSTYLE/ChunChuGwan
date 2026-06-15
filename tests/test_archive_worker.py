"""단발 아카이빙 작업 큐 + 소비자 테스트 (archive_worker.py / db.py). 캡처 없이 모킹."""
import pytest

from chunchugwan import archive_worker, config, crawler, db, storage
from chunchugwan.pipeline import ArchiveOutcome

URL = "https://example.com/post"


@pytest.fixture
def archive_env(tmp_path, monkeypatch):
    """임시 아카이브 루트에 페이지 1개를 구성."""
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "RESOURCES_DIR", tmp_path / "resources")
    monkeypatch.setattr(config, "DOCUMENTS_DIR", tmp_path / "documents")
    with db.connect() as conn:
        db.get_or_create_page(conn, URL, "example.com", storage.url_to_slug(URL))
    return URL


def _outcome(status: str = "new") -> ArchiveOutcome:
    return ArchiveOutcome(
        status=status, url=URL, content_hash="0" * 64, snapshot_dir=None,
        taken_at=None, last_taken_at=None, http_status=200, title="t",
        snapshot_id=1,
    )


# ---- 큐 함수(db.py) ----

def test_enqueue_blocks_active_duplicate(archive_env):
    with db.connect() as conn:
        assert db.enqueue_archive_job(conn, URL, source="web") is True
        assert db.enqueue_archive_job(conn, URL, source="web") is False  # 활성 중복
        cnt = conn.execute("SELECT COUNT(*) c FROM archive_jobs").fetchone()["c"]
    assert cnt == 1


def test_claim_is_atomic(archive_env):
    with db.connect() as conn:
        db.enqueue_archive_job(conn, URL, source="web")
        now = "2099-01-01T00:00:00+00:00"
        first = db.claim_due_archive_job(conn, now)
        second = db.claim_due_archive_job(conn, now)
    assert first is not None and second is None  # 동시 클레임은 한쪽만 성공


def test_recover_stale_resets_to_pending(archive_env):
    with db.connect() as conn:
        db.enqueue_archive_job(conn, URL, source="web")
        db.claim_due_archive_job(conn, "2000-01-01T00:00:00+00:00")  # claimed_at=과거
        recovered = db.recover_stale_archive_jobs(conn, "2099-01-01T00:00:00+00:00")
        status = conn.execute("SELECT status FROM archive_jobs").fetchone()["status"]
    assert recovered == 1 and status == "pending"


def test_finish_deletes_job(archive_env):
    with db.connect() as conn:
        db.enqueue_archive_job(conn, URL, source="web")
        job = db.claim_due_archive_job(conn, "2099-01-01T00:00:00+00:00")
        db.finish_archive_job(conn, job["id"])
        assert db.list_active_archive_jobs(conn) == []


# ---- 소비자(process_next) ----

def test_process_next_runs_finishes_and_returns_status(archive_env):
    calls = []

    def fake(url, force=False, source="web", **kw):
        calls.append((url, force, source))
        return _outcome("new")

    with db.connect() as conn:
        db.enqueue_archive_job(conn, URL, force=True, source="cli")
    step = archive_worker.process_next(archive_fn=fake)
    assert step is not None and step.status == "new" and step.url == URL
    assert calls == [(URL, True, "cli")]   # force·source 가 그대로 전달
    with db.connect() as conn:
        assert db.list_active_archive_jobs(conn) == []   # 완료 작업 삭제
    assert archive_worker.process_next(archive_fn=fake) is None  # 더 없음


def test_failure_without_retry_deletes_job(archive_env, monkeypatch):
    monkeypatch.setattr(crawler, "retry_backoff", lambda conn: ())  # 재시도 없음

    def boom(url, **kw):
        raise RuntimeError("캡처 실패")

    with db.connect() as conn:
        db.enqueue_archive_job(conn, URL, source="web")
    step = archive_worker.process_next(archive_fn=boom)
    assert step.status == "failed"
    with db.connect() as conn:
        assert db.list_active_archive_jobs(conn) == []  # 최종 실패는 큐에서 삭제


def test_failure_with_retry_keeps_pending_with_backoff(archive_env, monkeypatch):
    monkeypatch.setattr(crawler, "retry_backoff", lambda conn: (300,))  # 1회 재시도

    def boom(url, **kw):
        raise RuntimeError("캡처 실패")

    with db.connect() as conn:
        db.enqueue_archive_job(conn, URL, source="web")
    step = archive_worker.process_next(archive_fn=boom)
    assert step.status == "retry"
    with db.connect() as conn:
        job = conn.execute(
            "SELECT status, attempts, next_attempt_at, error FROM archive_jobs"
        ).fetchone()
    assert job["status"] == "pending" and job["attempts"] == 1
    assert job["next_attempt_at"] is not None and "캡처 실패" in job["error"]


def test_interval_registers_schedule_after_capture(archive_env):
    with db.connect() as conn:
        db.enqueue_archive_job(conn, URL, source="web", interval_seconds=3600)
        page_id = db.get_page(conn, URL)["id"]
    archive_worker.process_next(archive_fn=lambda url, **kw: _outcome("new"))
    with db.connect() as conn:
        sched = db.get_schedule(conn, page_id)
    assert sched is not None and sched["interval_seconds"] == 3600


def test_claim_conflict_releases_and_skips(archive_env):
    with db.connect() as conn:
        db.enqueue_archive_job(conn, URL, source="web")
    step = archive_worker.process_next(
        claim=lambda url: False, archive_fn=lambda url, **kw: _outcome()
    )
    assert step.status == "skipped"
    with db.connect() as conn:
        status = conn.execute("SELECT status FROM archive_jobs").fetchone()["status"]
    assert status == "pending"  # 반납되어 다음 폴링에서 재시도
