"""db.py 쿼리 함수 테스트. 임시 디렉토리에 격리된 DB 사용."""
import pytest

from chunchugwan import config, db


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    with db.connect() as c:
        yield c


def test_get_or_create_page_idempotent(conn):
    a = db.get_or_create_page(conn, "https://example.com/", "example.com", "root-abcd1234")
    b = db.get_or_create_page(conn, "https://example.com/", "example.com", "root-abcd1234")
    assert a == b


def test_snapshot_roundtrip(conn):
    page_id = db.get_or_create_page(conn, "https://example.com/a", "example.com", "a-abcd1234")
    assert db.last_snapshot(conn, page_id) is None

    db.insert_snapshot(
        conn, page_id,
        taken_at="2026-06-10T00:00:00+00:00", dir_name="2026-06-10T00-00-00",
        content_hash="h1", final_url="https://example.com/a", http_status=200, changed=1,
    )
    db.insert_snapshot(
        conn, page_id,
        taken_at="2026-06-11T00:00:00+00:00", dir_name="2026-06-11T00-00-00",
        content_hash="h2", final_url="https://example.com/a", http_status=200, changed=1,
    )

    last = db.last_snapshot(conn, page_id)
    assert last is not None and last["content_hash"] == "h2"

    snaps = db.list_snapshots(conn, page_id)
    assert [s["content_hash"] for s in snaps] == ["h1", "h2"]  # 오래된 순


def test_insert_snapshot_rejects_unknown_column(conn):
    page_id = db.get_or_create_page(conn, "https://example.com/b", "example.com", "b-abcd1234")
    with pytest.raises(ValueError):
        db.insert_snapshot(conn, page_id, taken_at="t", bogus="x")


def test_list_pages_counts(conn):
    p1 = db.get_or_create_page(conn, "https://example.com/x", "example.com", "x-abcd1234")
    db.get_or_create_page(conn, "https://example.com/y", "example.com", "y-abcd1234")
    db.insert_snapshot(
        conn, p1,
        taken_at="2026-06-10T00:00:00+00:00", dir_name="2026-06-10T00-00-00",
        content_hash="h1", final_url="https://example.com/x", changed=1,
    )
    db.insert_check(conn, p1, "h1")

    pages = {row["url"]: row for row in db.list_pages(conn)}
    assert pages["https://example.com/x"]["snapshot_count"] == 1
    assert pages["https://example.com/y"]["snapshot_count"] == 0
    assert pages["https://example.com/y"]["last_taken_at"] is None
