"""list / history / diff 명령 테스트. 캡처 없이 fixture 데이터로 검증."""
import pytest
from click.testing import CliRunner
from PIL import Image

from chunchugwan import cli, config, db, storage


@pytest.fixture
def archive_env(tmp_path, monkeypatch):
    """임시 아카이브 루트에 페이지 1개 + 스냅샷 2개를 구성."""
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "RESOURCES_DIR", tmp_path / "resources")
    monkeypatch.setattr(config, "DOCUMENTS_DIR", tmp_path / "documents")

    url = "https://example.com/post"
    domain, slug = "example.com", storage.url_to_slug(url)
    contents = ["첫 줄\n둘째 줄", "첫 줄\n둘째 줄 수정됨\n셋째 줄"]
    dir_names = ["2026-06-01T00-00-00", "2026-06-02T00-00-00"]
    shot_colors = [(255, 255, 255), (0, 0, 0)]

    with db.connect() as conn:
        page_id = db.get_or_create_page(conn, url, domain, slug)
        for i, (text, dir_name) in enumerate(zip(contents, dir_names)):
            snap_dir = storage.page_dir(domain, slug) / dir_name
            snap_dir.mkdir(parents=True)
            (snap_dir / "content.md").write_text(text, encoding="utf-8")
            Image.new("RGB", (8, 8), shot_colors[i]).save(snap_dir / "screenshot.png")
            db.insert_snapshot(
                conn, page_id,
                taken_at=f"2026-06-0{i + 1}T00:00:00+00:00", dir_name=dir_name,
                content_hash=storage.content_sha256(text),
                final_url=url, http_status=200, changed=1,
            )
    return url


def test_list(archive_env):
    result = CliRunner().invoke(cli.main, ["list"])
    assert result.exit_code == 0
    assert "https://example.com/post" in result.output
    assert "2" in result.output  # 스냅샷 수


def test_history(archive_env):
    result = CliRunner().invoke(cli.main, ["history", archive_env])
    assert result.exit_code == 0
    lines = [l for l in result.output.splitlines() if l.strip().startswith(("1 ", "2 "))]
    assert len(lines) == 2
    assert "[신규]" in result.output and "[변경]" in result.output


def test_history_unknown_url(archive_env):
    result = CliRunner().invoke(cli.main, ["history", "https://example.com/missing"])
    assert result.exit_code != 0
    assert "아카이브에 없는 URL" in result.output


def test_diff_latest_two(archive_env):
    result = CliRunner().invoke(cli.main, ["diff", archive_env])
    assert result.exit_code == 0
    assert "+2줄 / -1줄" in result.output
    assert "-둘째 줄" in result.output and "+둘째 줄 수정됨" in result.output
    assert "스크린샷 변경 픽셀 100.00%" in result.output  # 흰색 → 검은색


def test_diff_explicit_range(archive_env):
    result = CliRunner().invoke(cli.main, ["diff", archive_env, "--from", "1", "--to", "2"])
    assert result.exit_code == 0
    assert "+2줄 / -1줄" in result.output


def test_diff_bad_range(archive_env):
    result = CliRunner().invoke(cli.main, ["diff", archive_env, "--from", "2", "--to", "1"])
    assert result.exit_code != 0
    assert "잘못된 범위" in result.output


def test_delete_page(archive_env):
    """확인 프롬프트 거부 시 보존, --yes 면 전체 삭제."""
    aborted = CliRunner().invoke(cli.main, ["delete", archive_env], input="n\n")
    assert aborted.exit_code != 0
    result = CliRunner().invoke(cli.main, ["delete", archive_env, "--yes"])
    assert result.exit_code == 0
    assert "스냅샷 2개" in result.output
    with db.connect() as conn:
        assert db.get_page(conn, archive_env) is None


def test_delete_single_snapshot(archive_env):
    result = CliRunner().invoke(
        cli.main, ["delete", archive_env, "--snapshot", "1", "--yes"]
    )
    assert result.exit_code == 0
    assert "2026-06-01T00-00-00" in result.output
    with db.connect() as conn:
        page = db.get_page(conn, archive_env)
        snaps = db.list_snapshots(conn, page["id"])
    assert len(snaps) == 1 and snaps[0]["changed"] == 1  # 남은 것이 첫 스냅샷


def test_delete_bad_snapshot_index(archive_env):
    result = CliRunner().invoke(
        cli.main, ["delete", archive_env, "--snapshot", "9", "--yes"]
    )
    assert result.exit_code != 0
    assert "잘못된 번호" in result.output


def test_serve_rejects_external_bind_without_auth(monkeypatch):
    monkeypatch.setattr(config, "AUTH_ENABLED", False)
    result = CliRunner().invoke(cli.main, ["serve", "--host", "0.0.0.0"])
    assert result.exit_code != 0
    assert "인증이 필수" in result.output


def test_worker_command_runs_with_resolved_count(monkeypatch):
    import signal

    from chunchugwan import worker as worker_mod

    called = {}
    monkeypatch.setattr(
        worker_mod, "run",
        lambda stop, crawl_workers: called.setdefault("n", crawl_workers),
    )
    old_int = signal.getsignal(signal.SIGINT)
    old_term = signal.getsignal(signal.SIGTERM)
    try:
        result = CliRunner().invoke(cli.main, ["worker", "--workers", "3"])
        assert result.exit_code == 0
        assert called["n"] == 3

        result = CliRunner().invoke(cli.main, ["worker", "--workers", "0"])
        assert result.exit_code != 0
        assert "1 이상" in result.output
    finally:
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)


# ---- add (큐 등록) + archive run (큐 소비) ----


def test_add_enqueues_job(archive_env):
    res = CliRunner().invoke(cli.main, ["add", "https://example.com/new?utm_source=x"])
    assert res.exit_code == 0
    assert "큐에 추가" in res.output
    with db.connect() as conn:  # 정규화된 URL 로 source='cli' 작업이 등록된다
        jobs = conn.execute("SELECT url, source FROM archive_jobs").fetchall()
    assert [(j["url"], j["source"]) for j in jobs] == [("https://example.com/new", "cli")]


def test_add_skips_duplicate(archive_env):
    with db.connect() as conn:
        db.enqueue_archive_job(conn, "https://example.com/new", source="cli")
    res = CliRunner().invoke(cli.main, ["add", "https://example.com/new"])
    assert res.exit_code == 0 and "이미 큐에 있어" in res.output
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM archive_jobs").fetchone()["c"] == 1


def test_archive_run_drains_queue(archive_env, monkeypatch):
    from chunchugwan import archive_worker
    from chunchugwan.pipeline import ArchiveOutcome

    monkeypatch.setattr(
        archive_worker.pipeline, "archive_url",
        lambda url, **kw: ArchiveOutcome(
            status="new", url=url, content_hash="0" * 64, snapshot_dir=None,
            taken_at=None, last_taken_at=None, http_status=200, title="t", snapshot_id=1,
        ),
    )
    with db.connect() as conn:
        db.enqueue_archive_job(conn, "https://example.com/new", source="cli")
    res = CliRunner().invoke(cli.main, ["archive", "run"])
    assert res.exit_code == 0
    assert "https://example.com/new" in res.output and "신규" in res.output
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM archive_jobs").fetchone()["c"] == 0


def test_archive_run_empty(archive_env):
    res = CliRunner().invoke(cli.main, ["archive", "run"])
    assert res.exit_code == 0 and "처리할 아카이빙 작업이 없습니다" in res.output


# ---- compact (저장공간 최적화) ----


def test_compact_converts_existing_snapshots(archive_env, monkeypatch):
    import base64

    monkeypatch.setattr(config, "RESOURCE_MIN_BYTES", 16)
    domain, slug = "example.com", storage.url_to_slug(archive_env)
    base = storage.page_dir(domain, slug)
    snap_dir = base / "2026-06-01T00-00-00"
    data = b"R" * 64
    (snap_dir / "page.html").write_text(
        f'<img src="data:image/png;base64,{base64.b64encode(data).decode()}">',
        encoding="utf-8",
    )
    (snap_dir / "raw.html").write_text("<html>원본</html>", encoding="utf-8")
    for d in ("2026-06-01T00-00-00", "2026-06-02T00-00-00"):
        (base / d / "meta.json").write_text("{}", encoding="utf-8")

    result = CliRunner().invoke(cli.main, ["compact", "--yes"])
    assert result.exit_code == 0, result.output
    assert "변환 2/2개" in result.output
    assert "공유 자원 1개 추출" in result.output

    assert (snap_dir / "page.html.gz").is_file()
    assert not (snap_dir / "page.html").exists()
    assert (snap_dir / "raw.html.gz").is_file()
    assert (snap_dir / "screenshot.webp").is_file()
    assert not (snap_dir / "screenshot.png").exists()
    assert list(config.RESOURCES_DIR.glob("*/*"))  # 추출된 공유 자원

    # 멱등 — 두 번째 실행은 변환·백필할 것이 없다
    again = CliRunner().invoke(cli.main, ["compact", "--yes"])
    assert "최적화할 항목이 없습니다" in again.output

    # 변환 후에도 diff 동작 (WebP 스크린샷)
    d = CliRunner().invoke(cli.main, ["diff", archive_env])
    assert d.exit_code == 0
    assert "스크린샷 변경 픽셀 100.00%" in d.output
