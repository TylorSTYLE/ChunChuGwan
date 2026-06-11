"""스케줄러(주기적 재아카이빙) 테스트. 캡처 없이 pipeline 을 모킹해 검증."""
import pytest
from click.testing import CliRunner

from chunchugwan import backup, cli, config, db, pipeline, scheduler, storage
from chunchugwan.pipeline import ArchiveOutcome

URL = "https://example.com/post"
PAST = "2000-01-01T00:00:00+00:00"


@pytest.fixture
def archive_env(tmp_path, monkeypatch):
    """임시 아카이브 루트에 페이지 1개를 구성하고 정규화 URL 반환."""
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    with db.connect() as conn:
        db.get_or_create_page(conn, URL, "example.com", storage.url_to_slug(URL))
    return URL


def _outcome(status: str = "changed") -> ArchiveOutcome:
    return ArchiveOutcome(
        status=status, url=URL, content_hash="0" * 64, snapshot_dir=None,
        taken_at=None, last_taken_at=None, http_status=200, title=None,
    )


def _schedule_row():
    with db.connect() as conn:
        page = db.get_page(conn, URL)
        return db.get_schedule(conn, page["id"])


# ---- 주기 파싱/검증 ----


@pytest.mark.parametrize(
    "text,expected",
    [("1h", 3600), ("90m", 5400), ("6H", 21600), (" 3d ", 259200), ("1w", 604800)],
)
def test_parse_interval(text, expected):
    assert scheduler.parse_interval(text) == expected


@pytest.mark.parametrize("text", ["30m", "2w", "8d", "0h", "abc", "1x", ""])
def test_parse_interval_invalid(text):
    with pytest.raises(ValueError):
        scheduler.parse_interval(text)


def test_format_interval():
    assert scheduler.format_interval(3600) == "1시간"
    assert scheduler.format_interval(5400) == "1시간 30분"
    assert scheduler.format_interval(90000) == "1일 1시간"
    assert scheduler.format_interval(604800) == "1주"


# ---- 등록/해제 ----


def test_set_schedule_creates_and_updates(archive_env):
    row = scheduler.set_schedule(archive_env, 3600)
    assert row["interval_seconds"] == 3600
    assert row["next_run_at"] > scheduler._iso(scheduler._utcnow())  # 미래 시각

    row = scheduler.set_schedule(archive_env, 86400)  # 같은 페이지 — 갱신
    assert row["interval_seconds"] == 86400
    with db.connect() as conn:
        assert len(db.list_schedules(conn)) == 1


def test_set_schedule_unknown_url(archive_env):
    with pytest.raises(ValueError, match="아카이브에 없는 URL"):
        scheduler.set_schedule("https://example.com/missing", 3600)


def test_set_schedule_rejects_out_of_range(archive_env):
    with pytest.raises(ValueError):
        scheduler.set_schedule(archive_env, 60)
    with pytest.raises(ValueError):
        scheduler.set_schedule(archive_env, 8 * 86400)


def test_remove_schedule(archive_env):
    scheduler.set_schedule(archive_env, 3600)
    assert scheduler.remove_schedule(archive_env) is True
    assert scheduler.remove_schedule(archive_env) is False
    assert scheduler.remove_schedule("https://example.com/missing") is False


# ---- 실행 ----


def _make_due(interval: int = 3600) -> None:
    """기한이 지난 스케줄을 직접 구성."""
    with db.connect() as conn:
        page = db.get_page(conn, URL)
        db.upsert_schedule(conn, page["id"], interval, PAST)


def test_run_due_archives_and_advances(archive_env, monkeypatch):
    calls = []
    monkeypatch.setattr(
        pipeline, "archive_url",
        lambda url, force=False, source="cli": calls.append((url, source)) or _outcome(),
    )
    _make_due()
    results = scheduler.run_due()
    assert calls == [(URL, "schedule")]
    assert [(r.url, r.status) for r in results] == [(URL, "changed")]
    row = _schedule_row()
    assert row["last_run_at"] is not None
    assert row["next_run_at"] > scheduler._iso(scheduler._utcnow())


def test_run_due_nothing_due(archive_env, monkeypatch):
    monkeypatch.setattr(
        pipeline, "archive_url", lambda *a, **k: pytest.fail("실행되면 안 됨")
    )
    scheduler.set_schedule(archive_env, 3600)  # next_run_at 이 미래
    assert scheduler.run_due() == []


def test_run_due_error_still_advances(archive_env, monkeypatch):
    def boom(url, force=False, source="cli"):
        raise RuntimeError("캡처 실패")

    monkeypatch.setattr(pipeline, "archive_url", boom)
    _make_due()
    results = scheduler.run_due()
    assert results[0].status == "error"
    assert "캡처 실패" in results[0].error
    assert _schedule_row()["next_run_at"] > PAST  # 실패해도 다음 회차로 미룸


def test_run_due_claim_skips_without_advancing(archive_env, monkeypatch):
    monkeypatch.setattr(
        pipeline, "archive_url", lambda *a, **k: pytest.fail("실행되면 안 됨")
    )
    _make_due()
    released = []
    results = scheduler.run_due(claim=lambda url: False, release=released.append)
    assert results[0].status == "skipped"
    assert released == []                      # claim 실패 시 release 호출 없음
    assert _schedule_row()["next_run_at"] == PAST  # 다음 폴링에서 재시도


def test_wipe_archive_data_clears_schedules(archive_env):
    """overwrite 가져오기의 데이터 비우기가 schedules FK 를 위반하지 않는다."""
    scheduler.set_schedule(archive_env, 3600)
    with db.connect() as conn:
        backup._wipe_archive_data(conn)
        assert db.list_schedules(conn) == []


# ---- CLI ----


def test_cli_schedule_add_list_remove(archive_env):
    runner = CliRunner()
    result = runner.invoke(cli.main, ["schedule", "add", archive_env, "--every", "12h"])
    assert result.exit_code == 0
    assert "12시간" in result.output

    result = runner.invoke(cli.main, ["schedule", "list"])
    assert result.exit_code == 0
    assert archive_env in result.output and "12시간" in result.output

    result = runner.invoke(cli.main, ["schedule", "remove", archive_env])
    assert result.exit_code == 0
    result = runner.invoke(cli.main, ["schedule", "remove", archive_env])
    assert result.exit_code != 0
    assert "등록된 스케줄이 없는" in result.output


def test_cli_schedule_add_invalid_interval(archive_env):
    result = CliRunner().invoke(
        cli.main, ["schedule", "add", archive_env, "--every", "10m"]
    )
    assert result.exit_code != 0
    assert "1시간(1h) 이상" in result.output


def test_cli_schedule_run(archive_env, monkeypatch):
    monkeypatch.setattr(pipeline, "archive_url", lambda *a, **k: _outcome("unchanged"))
    _make_due()
    result = CliRunner().invoke(cli.main, ["schedule", "run"])
    assert result.exit_code == 0
    assert "unchanged" in result.output

    result = CliRunner().invoke(cli.main, ["schedule", "run"])
    assert "실행할 스케줄이 없습니다" in result.output
