"""단발 아카이빙 작업 큐 + 소비자 테스트 (archive_worker.py / db.py). 캡처 없이 모킹."""
import pytest

from chunchugwan import archive_worker, config, crawler, db, live_challenge, pipeline, storage
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


# ---- 운영 로그 (상태 확인·오류 분석) ----

def test_process_next_logs_start_and_completion(archive_env, caplog):
    """작업 시작·완료를 INFO 로 남긴다 — job_id·source·결과 status 포함."""
    with db.connect() as conn:
        db.enqueue_archive_job(conn, URL, source="cli")
    with caplog.at_level("INFO", logger="chunchugwan.archive_worker"):
        archive_worker.process_next(archive_fn=lambda url, **kw: _outcome("changed"))
    msgs = [r.getMessage() for r in caplog.records]
    assert any("아카이빙 시작" in m and "source=cli" in m for m in msgs)
    assert any("아카이빙 완료" in m and "changed" in m for m in msgs)


def test_failure_retry_logs_attempt_context(archive_env, monkeypatch, caplog):
    """재시도 예약 실패는 시도 횟수·전체 한도와 함께 '재시도' 로 남긴다."""
    monkeypatch.setattr(crawler, "retry_backoff", lambda conn: (300,))  # 한도 2

    def boom(url, **kw):
        raise RuntimeError("캡처 실패")

    with db.connect() as conn:
        db.enqueue_archive_job(conn, URL, source="web")
    with caplog.at_level("WARNING", logger="chunchugwan.archive_worker"):
        step = archive_worker.process_next(archive_fn=boom)
    assert step.status == "retry"
    warn = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("시도 1/2" in m and "재시도" in m for m in warn)


def test_failure_final_logs_exhausted(archive_env, monkeypatch, caplog):
    """시도 소진된 최종 실패는 '최종 실패' 로 구분해 남긴다."""
    monkeypatch.setattr(crawler, "retry_backoff", lambda conn: ())  # 한도 1

    def boom(url, **kw):
        raise RuntimeError("캡처 실패")

    with db.connect() as conn:
        db.enqueue_archive_job(conn, URL, source="web")
    with caplog.at_level("WARNING", logger="chunchugwan.archive_worker"):
        step = archive_worker.process_next(archive_fn=boom)
    assert step.status == "failed"
    warn = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("최종 실패" in m and "1/1" in m for m in warn)


# ---- 사람 보조(라이브) 세션 주입 게이트 (_live_session_for) ----

_ITEM = {"id": 7, "network_tag_id": None}


def test_live_session_for_disabled_returns_none(monkeypatch):
    monkeypatch.setattr(config, "LIVE_CHALLENGE", False)
    assert archive_worker._live_session_for(_ITEM) is None


def test_live_session_for_stealth_creates_session(monkeypatch):
    monkeypatch.setattr(config, "LIVE_CHALLENGE", True)
    monkeypatch.setattr(config, "CAPTURE_ENGINE", "patchright")
    monkeypatch.setattr(config, "CAPTURE_HEADFUL", False)
    sess = archive_worker._live_session_for(_ITEM)
    assert isinstance(sess, live_challenge.LiveChallengeSession)
    assert sess.job_id == 7


def test_live_session_for_headful_creates_session(monkeypatch):
    monkeypatch.setattr(config, "LIVE_CHALLENGE", True)
    monkeypatch.setattr(config, "CAPTURE_ENGINE", "playwright")
    monkeypatch.setattr(config, "CAPTURE_HEADFUL", True)
    assert archive_worker._live_session_for(_ITEM) is not None


def test_live_session_for_gate_unmet_warns_once(monkeypatch, caplog):
    # 기능은 켰는데 엔진이 headless playwright → 라이브 미작동 + 한 번만 경고
    monkeypatch.setattr(config, "LIVE_CHALLENGE", True)
    monkeypatch.setattr(config, "CAPTURE_ENGINE", "playwright")
    monkeypatch.setattr(config, "CAPTURE_HEADFUL", False)
    monkeypatch.setattr(archive_worker, "_warned_live_gate", False)
    with caplog.at_level("WARNING", logger="chunchugwan.archive_worker"):
        assert archive_worker._live_session_for(_ITEM) is None
        assert archive_worker._live_session_for(_ITEM) is None  # 두 번째 호출
    warnings = [r for r in caplog.records if "patchright" in r.getMessage()]
    assert len(warnings) == 1  # 도배하지 않고 프로세스당 한 번만


def test_process_next_injects_live_session_when_enabled(archive_env, monkeypatch):
    monkeypatch.setattr(config, "LIVE_CHALLENGE", True)
    monkeypatch.setattr(config, "CAPTURE_ENGINE", "patchright")
    seen = {}

    def fake(url, force=False, source="web", **kw):
        seen["live_session"] = kw.get("live_session")
        return _outcome("new")

    with db.connect() as conn:
        db.enqueue_archive_job(conn, URL, source="web")
    archive_worker.process_next(archive_fn=fake)
    assert isinstance(seen["live_session"], live_challenge.LiveChallengeSession)


# ---- 결과 알림 상관 키 (job_id) ----

def test_process_next_passes_job_id_to_pipeline(archive_env):
    """worker 가 클레임한 작업 id 를 파이프라인으로 넘긴다 (로그까지 이어지는 상관 키)."""
    seen = {}

    def fake(url, force=False, source="web", **kw):
        seen["job_id"] = kw.get("job_id")
        return _outcome("new")

    with db.connect() as conn:
        db.enqueue_archive_job(conn, URL, source="api")
        job_id = db.get_active_archive_job_id(conn, URL)
    archive_worker.process_next(archive_fn=fake)
    assert seen["job_id"] == job_id


def test_archive_url_writes_job_id_to_log_on_failure(archive_env):
    """실패 로그(_log_failure)에도 job_id 가 남아 확장이 결과를 되찾을 수 있다."""
    with pytest.raises(ValueError):
        pipeline.archive_url("ftp://example.com", source="api", job_id=777)
    with db.connect() as conn:
        row = conn.execute(
            "SELECT job_id, status FROM archive_logs ORDER BY id DESC"
        ).fetchone()
    assert row is not None and row["status"] == "error" and row["job_id"] == 777
