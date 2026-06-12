"""백업/복원(backup.py) 테스트. 두 아카이브 루트를 전환하며 검증."""
import json
import tarfile

import pytest
from click.testing import CliRunner

from chunchugwan import backup, cli, config, db, storage

URL_A = "https://example.com/post"
URL_B = "https://other.org/page"


def _patch_root(monkeypatch, root):
    """config 의 경로 전역을 임시 루트로 전환."""
    monkeypatch.setattr(config, "ARCHIVE_ROOT", root)
    monkeypatch.setattr(config, "SITES_DIR", root / "sites")
    monkeypatch.setattr(config, "DB_PATH", root / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", root / "cache")
    monkeypatch.setattr(config, "RESOURCES_DIR", root / "resources")
    monkeypatch.setattr(config, "RULES_PATH", root / "rules.json")


def _seed_page(url: str, dir_names: list[str], with_check: bool = False) -> None:
    """현재 루트에 페이지 1개 + 스냅샷(dir_names 개수만큼) + 파일 구성."""
    domain = url.split("/")[2]
    slug = storage.url_to_slug(url)
    with db.connect() as conn:
        page_id = db.get_or_create_page(conn, url, domain, slug)
        for i, dir_name in enumerate(dir_names):
            snap_dir = storage.page_dir(domain, slug) / dir_name
            snap_dir.mkdir(parents=True)
            text = f"{url} 본문 {i}"
            (snap_dir / "content.md").write_text(text, encoding="utf-8")
            db.insert_snapshot(
                conn, page_id,
                taken_at=dir_name[:10] + "T00:00:00+00:00",
                dir_name=dir_name, content_hash=storage.content_sha256(text),
                final_url=url, http_status=200, changed=1,
            )
        if with_check:
            db.insert_check(conn, page_id, "deadbeef")


@pytest.fixture
def roots(tmp_path, monkeypatch):
    """루트 A 에 데이터(페이지 2개·사용자·룰)를 구성하고 (A, B) 경로 반환."""
    root_a, root_b = tmp_path / "a", tmp_path / "b"
    _patch_root(monkeypatch, root_a)
    _seed_page(URL_A, ["2026-06-01T00-00-00", "2026-06-02T00-00-00"], with_check=True)
    _seed_page(URL_B, ["2026-06-03T00-00-00"])
    with db.connect() as conn:
        db.create_user(conn, "admin@example.com", password_hash="x", role="admin")
    config.RULES_PATH.write_text('{"example.com": {}}', encoding="utf-8")
    return root_a, root_b


def _counts() -> dict:
    with db.connect() as conn:
        return {
            t: conn.execute(f"SELECT COUNT(*) AS c FROM {t}").fetchone()["c"]
            for t in ("pages", "snapshots", "checks", "users", "archive_logs")
        }


# ---- 전체 백업/복원 ----


def test_backup_restore_roundtrip(roots, tmp_path, monkeypatch):
    root_a, root_b = roots
    out = backup.create_backup(tmp_path)
    assert out.name.startswith("chunchugwan-backup-")

    _patch_root(monkeypatch, root_b)
    backup.restore_backup(out)

    assert _counts() == {"pages": 2, "snapshots": 3, "checks": 1, "users": 1, "archive_logs": 0}
    slug = storage.url_to_slug(URL_A)
    content = storage.page_dir("example.com", slug) / "2026-06-01T00-00-00" / "content.md"
    assert content.read_text(encoding="utf-8") == f"{URL_A} 본문 0"
    assert json.loads(config.RULES_PATH.read_text(encoding="utf-8")) == {"example.com": {}}


def test_restore_replaces_existing_data(roots, tmp_path, monkeypatch):
    root_a, root_b = roots
    out = backup.create_backup(tmp_path / "full.tar.gz")

    _patch_root(monkeypatch, root_b)
    _seed_page("https://gone.example/x", ["2026-06-09T00-00-00"])
    backup.restore_backup(out)

    with db.connect() as conn:
        assert db.get_page(conn, "https://gone.example/x") is None
        assert db.get_page(conn, URL_A) is not None
    assert not (config.SITES_DIR / "gone.example").exists()


def test_restore_rejects_export_file(roots, tmp_path, monkeypatch):
    root_a, root_b = roots
    out = backup.export_archive(tmp_path / "export.tar.gz")
    _patch_root(monkeypatch, root_b)
    with pytest.raises(ValueError, match="전체 백업 파일이 아닙니다"):
        backup.restore_backup(out)


def test_read_manifest_rejects_foreign_tar(tmp_path):
    foreign = tmp_path / "foreign.tar.gz"
    payload = tmp_path / "x.txt"
    payload.write_text("x")
    with tarfile.open(foreign, "w:gz") as tar:
        tar.add(payload, arcname="x.txt")
    with pytest.raises(ValueError, match="춘추관 백업 파일이 아닙니다"):
        backup.read_manifest(foreign)


def test_restore_clears_stale_wal_files(roots, tmp_path, monkeypatch):
    """복원이 이전 DB 의 WAL 잔재(-wal/-shm)를 함께 지운다 — 남으면 새 DB 손상."""
    root_a, root_b = roots
    out = backup.create_backup(tmp_path / "bk.tar.gz")

    _patch_root(monkeypatch, root_b)
    config.ensure_dirs()
    (root_b / "index.db-wal").write_bytes(b"stale")
    (root_b / "index.db-shm").write_bytes(b"stale")
    backup.restore_backup(out)

    assert not (root_b / "index.db-wal").exists()
    assert not (root_b / "index.db-shm").exists()
    assert _counts()["pages"] == 2


# ---- 아카이브 내보내기/가져오기 ----


def test_export_import_into_empty_root(roots, tmp_path, monkeypatch):
    root_a, root_b = roots
    out = backup.export_archive(tmp_path)
    assert out.name.startswith("chunchugwan-export-")

    _patch_root(monkeypatch, root_b)
    with db.connect() as conn:
        db.create_user(conn, "b@example.com", password_hash="y")
    result = backup.import_archive(out, mode="merge")

    assert (result.pages_added, result.snapshots_added, result.checks_added) == (2, 3, 1)
    assert result.snapshots_skipped == 0
    # 인증 데이터는 export 에 포함되지 않고, 기존 사용자도 유지
    counts = _counts()
    assert counts["users"] == 1
    assert counts["pages"] == 2 and counts["snapshots"] == 3
    slug = storage.url_to_slug(URL_B)
    content = storage.page_dir("other.org", slug) / "2026-06-03T00-00-00" / "content.md"
    assert content.read_text(encoding="utf-8") == f"{URL_B} 본문 0"


def test_import_merge_is_idempotent(roots, tmp_path, monkeypatch):
    root_a, root_b = roots
    out = backup.export_archive(tmp_path / "e.tar.gz")
    _patch_root(monkeypatch, root_b)
    backup.import_archive(out, mode="merge")
    result = backup.import_archive(out, mode="merge")
    assert (result.pages_added, result.snapshots_added, result.checks_added) == (0, 0, 0)
    assert result.snapshots_skipped == 3
    assert _counts()["snapshots"] == 3


def test_import_merge_keeps_existing_pages(roots, tmp_path, monkeypatch):
    root_a, root_b = roots
    out = backup.export_archive(tmp_path / "e.tar.gz")
    _patch_root(monkeypatch, root_b)
    _seed_page("https://keep.example/y", ["2026-06-08T00-00-00"])
    backup.import_archive(out, mode="merge")
    with db.connect() as conn:
        assert db.get_page(conn, "https://keep.example/y") is not None
        assert db.get_page(conn, URL_A) is not None
    assert _counts()["pages"] == 3


def test_import_overwrite_replaces_archive_keeps_auth_and_logs(roots, tmp_path, monkeypatch):
    root_a, root_b = roots
    out = backup.export_archive(tmp_path / "e.tar.gz")

    _patch_root(monkeypatch, root_b)
    _seed_page("https://gone.example/x", ["2026-06-09T00-00-00"])
    with db.connect() as conn:
        db.create_user(conn, "b@example.com", password_hash="y")
        page = db.get_page(conn, "https://gone.example/x")
        db.insert_archive_log(
            conn, url="https://gone.example/x", domain="gone.example",
            page_id=page["id"], source="cli", status="new",
            started_at="2026-06-09T00:00:00+00:00",
        )
    backup.import_archive(out, mode="overwrite")

    counts = _counts()
    assert counts == {"pages": 2, "snapshots": 3, "checks": 1, "users": 1, "archive_logs": 1}
    with db.connect() as conn:
        assert db.get_page(conn, "https://gone.example/x") is None
        log = conn.execute("SELECT * FROM archive_logs").fetchone()
        assert log["page_id"] is None  # FK 만 비우고 행은 유지
    assert not (config.SITES_DIR / "gone.example").exists()


def test_import_rejects_full_backup(roots, tmp_path, monkeypatch):
    root_a, root_b = roots
    out = backup.create_backup(tmp_path / "full.tar.gz")
    _patch_root(monkeypatch, root_b)
    with pytest.raises(ValueError, match="아카이브 내보내기 파일이 아닙니다"):
        backup.import_archive(out)


def test_import_rejects_path_traversal_components(roots, tmp_path, monkeypatch):
    """archive.json 의 slug 에 경로 탈출 문자가 있으면 거부."""
    root_a, root_b = roots
    evil = tmp_path / "evil.tar.gz"
    manifest = {"kind": "archive", "format_version": 1, "created_at": "x", "counts": {}}
    data = {
        "pages": [{"url": URL_A, "domain": "example.com", "slug": "../evil", "created_at": "x"}],
        "snapshots": [], "checks": [],
    }
    src = tmp_path / "payload"
    src.mkdir()
    (src / backup.MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    (src / backup.ARCHIVE_DATA_NAME).write_text(json.dumps(data), encoding="utf-8")
    with tarfile.open(evil, "w:gz") as tar:
        tar.add(src / backup.MANIFEST_NAME, arcname=backup.MANIFEST_NAME)
        tar.add(src / backup.ARCHIVE_DATA_NAME, arcname=backup.ARCHIVE_DATA_NAME)

    _patch_root(monkeypatch, root_b)
    with pytest.raises(ValueError, match="잘못된 페이지 경로 구성요소"):
        backup.import_archive(evil)
    assert _counts()["pages"] == 0


# ---- CLI ----


def test_cli_backup_and_restore(roots, tmp_path, monkeypatch):
    root_a, root_b = roots
    runner = CliRunner()
    result = runner.invoke(cli.main, ["backup", str(tmp_path / "out.tar.gz")])
    assert result.exit_code == 0, result.output
    assert "백업 생성" in result.output

    _patch_root(monkeypatch, root_b)
    # 확인 프롬프트에서 거부하면 중단
    result = runner.invoke(cli.main, ["restore", str(tmp_path / "out.tar.gz")], input="n\n")
    assert result.exit_code != 0
    assert _counts()["pages"] == 0

    result = runner.invoke(cli.main, ["restore", str(tmp_path / "out.tar.gz"), "--yes"])
    assert result.exit_code == 0, result.output
    assert "복원 완료" in result.output
    assert _counts()["pages"] == 2


def test_cli_export_and_import(roots, tmp_path, monkeypatch):
    root_a, root_b = roots
    runner = CliRunner()
    result = runner.invoke(cli.main, ["export", str(tmp_path / "e.tar.gz")])
    assert result.exit_code == 0, result.output

    _patch_root(monkeypatch, root_b)
    result = runner.invoke(cli.main, ["import", str(tmp_path / "e.tar.gz")])
    assert result.exit_code == 0, result.output
    assert "페이지 +2" in result.output

    # overwrite 는 --yes 없이 프롬프트, 거부 시 중단
    result = runner.invoke(
        cli.main, ["import", str(tmp_path / "e.tar.gz"), "--mode", "overwrite"], input="n\n"
    )
    assert result.exit_code != 0

    result = runner.invoke(
        cli.main, ["import", str(tmp_path / "e.tar.gz"), "--mode", "overwrite", "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert "스킵 0" in result.output  # overwrite 후라 전부 새로 들어감


def test_cli_restore_rejects_export_file(roots, tmp_path, monkeypatch):
    root_a, root_b = roots
    out = backup.export_archive(tmp_path / "e.tar.gz")
    _patch_root(monkeypatch, root_b)
    result = CliRunner().invoke(cli.main, ["restore", str(out), "--yes"])
    assert result.exit_code != 0
    assert "wccg import" in result.output


# ---- 공유 자원 (resources/) 포함 ----


def test_backup_and_export_include_resources(roots, tmp_path, monkeypatch):
    root_a, root_b = roots
    name = "a" * 64 + ".png"
    src = config.RESOURCES_DIR / name[:2] / name
    src.parent.mkdir(parents=True)
    src.write_bytes(b"resource-bytes")

    full = backup.create_backup(tmp_path / "full.tar.gz")
    exported = backup.export_archive(tmp_path / "export.tar.gz")

    # 전체 복원 — 공유 자원이 그대로 돌아온다
    _patch_root(monkeypatch, root_b)
    backup.restore_backup(full)
    assert (config.RESOURCES_DIR / name[:2] / name).read_bytes() == b"resource-bytes"

    # 가져오기(merge) — 빈 루트에 공유 자원 포함 복원
    _patch_root(monkeypatch, tmp_path / "c")
    backup.import_archive(exported, mode="merge")
    assert (config.RESOURCES_DIR / name[:2] / name).read_bytes() == b"resource-bytes"
