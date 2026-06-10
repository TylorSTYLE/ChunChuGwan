"""list / history / diff 명령 테스트. 캡처 없이 fixture 데이터로 검증."""
import pytest
from click.testing import CliRunner
from PIL import Image

from archiver import cli, config, db, storage


@pytest.fixture
def archive_env(tmp_path, monkeypatch):
    """임시 아카이브 루트에 페이지 1개 + 스냅샷 2개를 구성."""
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")

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


def test_serve_rejects_external_bind_without_auth(monkeypatch):
    monkeypatch.setattr(config, "AUTH_ENABLED", False)
    result = CliRunner().invoke(cli.main, ["serve", "--host", "0.0.0.0"])
    assert result.exit_code != 0
    assert "인증이 필수" in result.output
