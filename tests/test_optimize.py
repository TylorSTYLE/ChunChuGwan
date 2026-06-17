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


def test_compact_refreshes_snapshot_bytes(archive_env):
    """압축 변환으로 디렉토리 형태가 바뀌면 snapshots.bytes 가 실제 용량으로 갱신된다."""
    from PIL import Image

    dir_name = "2026-06-01T00-00-00"
    slug = storage.url_to_slug(URL)
    snap_dir = storage.page_dir("example.com", slug) / dir_name
    snap_dir.mkdir(parents=True)
    # 구형(비압축) 산출물 — compact 가 page.html.gz·webp 로 변환한다
    (snap_dir / "content.md").write_text("본문", encoding="utf-8")
    (snap_dir / "page.html").write_text(
        "<html><body>" + "본문 " * 500 + "</body></html>", encoding="utf-8"
    )
    Image.new("RGB", (64, 64), (123, 200, 50)).save(snap_dir / "screenshot.png")
    storage.write_meta(snap_dir, storage.SnapshotMeta(
        url=URL, final_url=URL, taken_at="2026-06-01T00:00:00+00:00",
        content_hash="0" * 64, http_status=200, title=None,
    ))
    with db.connect() as conn:
        page_id = db.get_or_create_page(conn, URL, "example.com", slug)
        snap_id = db.insert_snapshot(
            conn, page_id, taken_at="2026-06-01T00:00:00+00:00", dir_name=dir_name,
            content_hash="0" * 64, final_url=URL, http_status=200, changed=1,
            bytes=999999,  # 일부러 틀린 값 — compact 후 실제값으로 맞춰져야 한다
        )

    result = optimize.run()
    assert result.compact.converted == 1

    with db.connect() as conn:
        stored = conn.execute("SELECT bytes FROM snapshots WHERE id=?", (snap_id,)).fetchone()[0]
    assert stored == storage.snapshot_dir_bytes(snap_dir)
    assert stored != 999999


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
    compactable, css_pending, unindexed = optimize.pending_counts()
    assert (compactable, css_pending, unindexed) == (0, 1, 1)
    optimize.run()
    assert optimize.pending_counts() == (0, 0, 0)


# ---- 인라인 <style> 추출 ----


def test_styles_externalized_from_gz(archive_env, monkeypatch):
    monkeypatch.setattr(config, "RESOURCE_MIN_BYTES", 16)
    css = "body { color: #000; font-size: 14px; }"
    snap_id = _seed_snapshot(f"<html><style>{css}</style>본문</html>")

    result = optimize.run()
    assert result.styles_snapshots == 1
    assert result.styles_extracted == 1

    name = hashlib.sha256(css.encode()).hexdigest() + ".css"
    snap_dir = (
        storage.page_dir("example.com", storage.url_to_slug(URL))
        / "2026-06-01T00-00-00"
    )
    html = gzip.decompress((snap_dir / "page.html.gz").read_bytes()).decode("utf-8")
    assert f'<link rel="stylesheet" href="/resource/{name}">' in html
    assert "<style" not in html and "본문" in html
    assert gzip.decompress(resources.resource_path(name).read_bytes()) == css.encode()

    with db.connect() as conn:
        rows = conn.execute(
            "SELECT snapshot_id, name FROM snapshot_resources"
        ).fetchall()
        assert (snap_id, name) in [(r["snapshot_id"], r["name"]) for r in rows]
        assert db.count_css_pending_snapshots(conn) == 0

    # 참조된 스타일 자원은 sweep 에서 살아남고, 두 번째 실행은 대상이 없다
    assert resources.resource_path(name).is_file()
    again = optimize.run()
    assert again.styles_snapshots == 0 and again.styles_extracted == 0


def test_styles_pass_marks_snapshot_without_extractable(archive_env):
    _seed_snapshot("<html>스타일 없음</html>")  # 추출할 블록 없음
    result = optimize.run()
    assert result.styles_snapshots == 0
    with db.connect() as conn:
        assert db.count_css_pending_snapshots(conn) == 0  # 그래도 완료 표시
