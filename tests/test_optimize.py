"""저장공간 최적화(optimize.py) — 참조 백필과 고아 자원 정리.

압축 변환 자체는 test_resources.py 가 검증한다. 여기서는 최적화가 더하는
두 단계를 본다: 참조 미기록 스냅샷의 page.html.gz 스캔 백필과, 백필이 끝난
뒤에만 실행되는 고아 자원 sweep(유예 창 포함).
"""
import gzip
import hashlib
import os
import time

import pytest

from chunchugwan import config, db, optimize, resources, storage

URL = "https://example.com/post"


@pytest.fixture
def archive_env(tmp_path, monkeypatch):
    """임시 아카이브 루트."""
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "RULES_PATH", tmp_path / "rules.json")
    monkeypatch.setattr(config, "RESOURCES_DIR", tmp_path / "resources")
    monkeypatch.setattr(config, "DOCUMENTS_DIR", tmp_path / "documents")
    return tmp_path


def _store_resource(data: bytes, ext: str = ".png") -> str:
    return resources._store(data, ext)


def _age(name: str, seconds: int) -> None:
    """CAS 파일의 mtime 을 과거로 — sweep 유예 창 테스트용."""
    path = resources.resource_path(name)
    past = time.time() - seconds
    os.utime(path, (past, past))


def _seed_snapshot(html: str | None, *, indexed: bool = False) -> int:
    """페이지 + 스냅샷(html 이 있으면 page.html.gz 포함) 생성 후 snapshot_id."""
    dir_name = "2026-06-01T00-00-00"
    slug = storage.url_to_slug(URL)
    with db.connect() as conn:
        page_id = db.get_or_create_page(conn, URL, "example.com", slug)
        snap_id = db.insert_snapshot(
            conn, page_id, taken_at="2026-06-01T00:00:00+00:00", dir_name=dir_name,
            content_hash="0" * 64, final_url=URL, http_status=200, changed=1,
            resources_indexed=int(indexed),
        )
    if html is not None:
        snap_dir = storage.page_dir("example.com", slug) / dir_name
        snap_dir.mkdir(parents=True, exist_ok=True)
        (snap_dir / "page.html.gz").write_bytes(gzip.compress(html.encode("utf-8")))
        (snap_dir / "meta.json").write_text("{}", encoding="utf-8")
    return snap_id


def test_backfill_scans_page_html(archive_env):
    name = _store_resource(b"R" * 5000)
    snap_id = _seed_snapshot(f'<img src="/resource/{name}">')
    result = optimize.run()
    assert result.indexed == 1
    assert not result.sweep_skipped
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM snapshot_resources").fetchall()
        assert [(r["snapshot_id"], r["name"], r["url"]) for r in rows] == [
            (snap_id, name, None)
        ]
        assert db.count_unindexed_snapshots(conn) == 0
    # 참조된 자원은 sweep 에서 살아남는다
    assert resources.resource_path(name).is_file()


def test_backfill_marks_missing_dir_indexed(archive_env):
    _seed_snapshot(None)  # 파일이 지워진 스냅샷 — 참조할 자원 없음
    result = optimize.run()
    assert result.indexed == 1
    with db.connect() as conn:
        assert db.count_unindexed_snapshots(conn) == 0


def test_sweep_deletes_old_orphans_keeps_recent(archive_env):
    _seed_snapshot("<html></html>")  # 인덱스할 스냅샷 (참조 없음)
    old_orphan = _store_resource(b"O" * 5000)
    _age(old_orphan, config.RESOURCE_ORPHAN_GRACE_SECONDS + 60)
    recent_orphan = _store_resource(b"N" * 5000)  # 방금 생성 — 유예 창 보호

    result = optimize.run()
    assert result.swept == 1
    assert result.swept_bytes == 5000
    assert not resources.resource_path(old_orphan).exists()
    assert resources.resource_path(recent_orphan).is_file()


def test_sweep_skipped_until_backfill_complete(archive_env):
    _seed_snapshot("<html></html>")  # 아직 인덱스되지 않은 스냅샷
    orphan = _store_resource(b"O" * 5000)
    _age(orphan, config.RESOURCE_ORPHAN_GRACE_SECONDS + 60)
    swept, swept_bytes, skipped = optimize._sweep_orphans()
    assert skipped is True and swept == 0
    assert resources.resource_path(orphan).is_file()


def test_pending_counts(archive_env):
    _seed_snapshot("<html></html>")
    compactable, unindexed = optimize.pending_counts()
    assert (compactable, unindexed) == (0, 1)
    optimize.run()
    assert optimize.pending_counts() == (0, 0)
