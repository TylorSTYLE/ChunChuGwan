"""스케줄러(주기적 재아카이빙) 테스트. 캡처 없이 pipeline 을 모킹해 검증."""
from datetime import datetime, timedelta, timezone

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
    monkeypatch.setattr(config, "RESOURCES_DIR", tmp_path / "resources")
    monkeypatch.setattr(config, "DOCUMENTS_DIR", tmp_path / "documents")
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
    [
        ("1h", 3600), ("90m", 5400), ("6H", 21600), (" 3d ", 259200),
        ("1w", 604800), ("2w", 1209600), ("1mo", 2592000), ("1MO", 2592000),
    ],
)
def test_parse_interval(text, expected):
    assert scheduler.parse_interval(text) == expected


@pytest.mark.parametrize("text", ["30m", "2mo", "31d", "5w", "0h", "abc", "1x", ""])
def test_parse_interval_invalid(text):
    with pytest.raises(ValueError):
        scheduler.parse_interval(text)


def test_format_interval():
    assert scheduler.format_interval(3600) == "1시간"
    assert scheduler.format_interval(5400) == "1시간 30분"
    assert scheduler.format_interval(90000) == "1일 1시간"
    assert scheduler.format_interval(604800) == "1주"
    assert scheduler.format_interval(2592000) == "1개월"


def test_format_schedule():
    assert scheduler.format_schedule(86400, None) == "1일"
    assert scheduler.format_schedule(86400, "09:00") == "1일 · 09:00"


# ---- 실행 시각 (run_at) ----


def test_validate_run_at_ok():
    scheduler.validate_run_at("09:00", 86400)
    scheduler.validate_run_at("23:59", 3 * 86400)
    scheduler.validate_run_at("00:00", 7 * 86400)


@pytest.mark.parametrize("at", ["24:00", "9:00", "09:60", "0900", "", "abc"])
def test_validate_run_at_bad_format(at):
    with pytest.raises(ValueError, match="실행 시각 형식"):
        scheduler.validate_run_at(at, 86400)


def test_validate_run_at_requires_daily_interval():
    with pytest.raises(ValueError, match="1일 단위 주기"):
        scheduler.validate_run_at("09:00", 3600)
    with pytest.raises(ValueError, match="1일 단위 주기"):
        scheduler.validate_run_at("09:00", 86400 + 3600)


def test_next_run_after_without_run_at():
    from datetime import datetime, timezone

    ref = datetime(2026, 6, 1, 8, 0, tzinfo=timezone.utc)
    got = scheduler.next_run_after(ref, 3600, None)
    assert got == datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)


def test_next_run_after_aligns_to_run_at():
    from datetime import datetime, timezone

    # 1일 주기, 09:00 — 기준 + 1일의 날짜에서 09:00 으로 정렬
    ref = datetime(2026, 6, 1, 8, 30, tzinfo=timezone.utc)
    got = scheduler.next_run_after(ref, 86400, "09:00", tz=timezone.utc)
    assert got == datetime(2026, 6, 2, 9, 0, tzinfo=timezone.utc)

    # 실행이 늦어진 경우(15:00 종료)에도 다음 날 09:00 으로 재정렬
    ref = datetime(2026, 6, 1, 15, 0, tzinfo=timezone.utc)
    got = scheduler.next_run_after(ref, 86400, "09:00", tz=timezone.utc)
    assert got == datetime(2026, 6, 2, 9, 0, tzinfo=timezone.utc)

    # 1주 주기 — 등록 요일 유지 + 시각 정렬
    ref = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
    got = scheduler.next_run_after(ref, 7 * 86400, "09:00", tz=timezone.utc)
    assert got == datetime(2026, 6, 8, 9, 0, tzinfo=timezone.utc)


def test_set_schedule_with_run_at(archive_env):
    from datetime import datetime

    row = scheduler.set_schedule(archive_env, 86400, run_at="09:00")
    assert row["run_at_time"] == "09:00"
    # 다음 실행 시각(UTC 저장)을 로컬로 되돌리면 정확히 09:00
    nxt = datetime.fromisoformat(row["next_run_at"]).astimezone()
    assert (nxt.hour, nxt.minute) == (9, 0)

    row = scheduler.set_schedule(archive_env, 86400)  # 시각 없이 갱신하면 해제
    assert row["run_at_time"] is None


def test_set_schedule_rejects_run_at_for_hourly(archive_env):
    with pytest.raises(ValueError, match="1일 단위 주기"):
        scheduler.set_schedule(archive_env, 3600, run_at="09:00")


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
        scheduler.set_schedule(archive_env, 31 * 86400)


def test_remove_schedule(archive_env):
    scheduler.set_schedule(archive_env, 3600)
    assert scheduler.remove_schedule(archive_env) is True
    assert scheduler.remove_schedule(archive_env) is False
    assert scheduler.remove_schedule("https://example.com/missing") is False


# ---- 다음 실행 시각 변경 ----


def test_set_next_run(archive_env):
    scheduler.set_schedule(archive_env, 3600)
    target = datetime(2099, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    row = scheduler.set_next_run(archive_env, target)
    assert row["next_run_at"] == "2099-01-02T03:04:05+00:00"
    assert row["interval_seconds"] == 3600  # 주기는 그대로


def test_set_next_run_naive_is_utc_and_other_tz_converted(archive_env):
    scheduler.set_schedule(archive_env, 3600)
    row = scheduler.set_next_run(archive_env, datetime(2099, 1, 1, 12, 0, 0))
    assert row["next_run_at"] == "2099-01-01T12:00:00+00:00"

    kst = timezone(timedelta(hours=9))
    row = scheduler.set_next_run(
        archive_env, datetime(2099, 1, 1, 12, 0, 0, tzinfo=kst)
    )
    assert row["next_run_at"] == "2099-01-01T03:00:00+00:00"


def test_set_next_run_past_makes_due(archive_env, monkeypatch):
    """과거 시각으로 바꾸면 다음 폴링에서 즉시 실행된다."""
    monkeypatch.setattr(
        pipeline, "archive_url", lambda url, force=False, source="cli": _outcome()
    )
    scheduler.set_schedule(archive_env, 3600)
    scheduler.set_next_run(
        archive_env, datetime(2000, 1, 1, tzinfo=timezone.utc)
    )
    results = scheduler.run_due()
    assert [(r.url, r.status) for r in results] == [(URL, "changed")]


def test_set_next_run_without_schedule(archive_env):
    target = datetime(2099, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="등록된 스케줄이 없는"):
        scheduler.set_next_run(archive_env, target)  # 페이지는 있지만 스케줄 없음
    with pytest.raises(ValueError, match="등록된 스케줄이 없는"):
        scheduler.set_next_run("https://example.com/missing", target)


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


def test_run_due_logs_archive_result(archive_env, monkeypatch, caplog):
    """스케줄 발화 성공을 INFO 로 남긴다 — 정상 동작도 로그로 확인되게 (url·status)."""
    monkeypatch.setattr(pipeline, "archive_url", lambda *a, **k: _outcome("changed"))
    _make_due()
    with caplog.at_level("INFO", logger="chunchugwan.scheduler"):
        scheduler.run_due()
    msgs = [r.getMessage() for r in caplog.records]
    assert any("스케줄 아카이빙" in m and "changed" in m for m in msgs)


def test_run_due_advances_aligned_to_run_at(archive_env, monkeypatch):
    from datetime import datetime

    monkeypatch.setattr(pipeline, "archive_url", lambda *a, **k: _outcome())
    with db.connect() as conn:
        page = db.get_page(conn, URL)
        db.upsert_schedule(conn, page["id"], 86400, PAST, "09:00")
    scheduler.run_due()
    row = _schedule_row()
    assert row["next_run_at"] > scheduler._iso(scheduler._utcnow())
    nxt = datetime.fromisoformat(row["next_run_at"]).astimezone()
    assert (nxt.hour, nxt.minute) == (9, 0)  # 다음 실행도 로컬 09:00 정렬 유지


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


def test_cli_schedule_next(archive_env):
    runner = CliRunner()
    runner.invoke(cli.main, ["schedule", "add", archive_env, "--every", "12h"])

    result = runner.invoke(
        cli.main, ["schedule", "next", archive_env, "2099-01-02T03:04:05+00:00"]
    )
    assert result.exit_code == 0
    assert "2099-01-02T03:04:05+00:00" in result.output
    assert _schedule_row()["next_run_at"] == "2099-01-02T03:04:05+00:00"

    # 타임존 없는 입력은 로컬 시간으로 해석돼 UTC 로 저장된다
    result = runner.invoke(
        cli.main, ["schedule", "next", archive_env, "2099-06-01T09:00"]
    )
    assert result.exit_code == 0
    expected = datetime(2099, 6, 1, 9, 0).astimezone(timezone.utc)
    assert _schedule_row()["next_run_at"] == expected.isoformat(timespec="seconds")


def test_cli_schedule_next_errors(archive_env):
    runner = CliRunner()
    result = runner.invoke(cli.main, ["schedule", "next", archive_env, "not-a-date"])
    assert result.exit_code != 0
    assert "잘못된 시각 형식" in result.output

    result = runner.invoke(
        cli.main, ["schedule", "next", archive_env, "2099-01-01T00:00"]
    )
    assert result.exit_code != 0
    assert "등록된 스케줄이 없는" in result.output


def test_cli_schedule_add_with_at(archive_env):
    runner = CliRunner()
    result = runner.invoke(
        cli.main, ["schedule", "add", archive_env, "--every", "1d", "--at", "09:00"]
    )
    assert result.exit_code == 0
    assert "1일 · 09:00" in result.output

    result = runner.invoke(cli.main, ["schedule", "list"])
    assert "1일 · 09:00" in result.output

    # 시간 단위 주기에는 --at 불가
    result = runner.invoke(
        cli.main, ["schedule", "add", archive_env, "--every", "6h", "--at", "09:00"]
    )
    assert result.exit_code != 0
    assert "1일 단위 주기" in result.output


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
