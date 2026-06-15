"""db.py 쿼리 함수 테스트. 임시 디렉토리에 격리된 DB 사용."""
import os
import sqlite3

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


def test_connect_uses_wal(conn):
    """WAL 저널 모드 — 쓰기가 대시보드 읽기를 막지 않게 한다."""
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_schema_ensured_once_per_process(tmp_path, monkeypatch):
    """스키마 보장(_migrate 포함)은 같은 DB 파일에 대해 프로세스당 1회만."""
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    calls = []
    original = db._migrate

    def counting_migrate(c):
        calls.append(1)
        original(c)

    monkeypatch.setattr(db, "_migrate", counting_migrate)
    with db.connect():
        pass
    with db.connect():
        pass
    assert len(calls) == 1

    # DB 파일 교체(복원 등) 후에는 캐시 무효화로 다시 보장된다
    db.invalidate_schema_cache()
    with db.connect():
        pass
    assert len(calls) == 2


def test_connect_upgrades_legacy_archive_jobs(tmp_path, monkeypatch):
    """라이브 챌린지 컬럼이 없는 구형 archive_jobs DB 도 connect 가 마이그레이션한다.

    needs_human_at 등 라이브 챌린지 컬럼은 _migrate 가 ALTER 로 추가한다. 그
    컬럼에 대한 인덱스가 SCHEMA 에 있으면 executescript(SCHEMA)가 컬럼 추가
    전에 인덱스를 만들려다 'no such column: needs_human_at' 으로 실패한다.
    """
    db_path = tmp_path / "index.db"
    monkeypatch.setattr(config, "ARCHIVE_ROOT", tmp_path)
    monkeypatch.setattr(config, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(config, "DB_PATH", db_path)

    # 라이브 챌린지 도입 이전 모양의 archive_jobs 테이블만 미리 만든다
    legacy = sqlite3.connect(db_path)
    legacy.executescript(
        """
        CREATE TABLE archive_jobs (
            id              INTEGER PRIMARY KEY,
            url             TEXT NOT NULL,
            force           INTEGER NOT NULL DEFAULT 0,
            source          TEXT NOT NULL DEFAULT 'web',
            status          TEXT NOT NULL DEFAULT 'pending',
            attempts        INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TEXT,
            claimed_at      TEXT,
            error           TEXT,
            created_at      TEXT NOT NULL
        );
        """
    )
    legacy.commit()
    legacy.close()
    db.invalidate_schema_cache()

    # connect 가 'no such column' 없이 컬럼·인덱스를 추가해야 한다
    with db.connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(archive_jobs)")}
        assert "needs_human_at" in cols
        assert "live_token" in cols
        assert "live_force_solve" in cols   # 신규 강제 진행 플래그도 ALTER 로 추가
        idx = {r["name"] for r in conn.execute("PRAGMA index_list(archive_jobs)")}
        assert "idx_archive_jobs_needs_human" in idx


@pytest.mark.skipif(os.geteuid() == 0, reason="root 는 권한 검사를 우회한다")
def test_connect_unwritable_dir_friendly_error(tmp_path, monkeypatch):
    """아카이브 디렉토리에 쓰기 권한이 없으면 원인을 알려주는 메시지로 실패한다."""
    root = tmp_path / "archive"
    (root / "sites").mkdir(parents=True)  # sites 가 있으면 ensure_dirs 는 통과
    root.chmod(0o555)
    monkeypatch.setattr(config, "ARCHIVE_ROOT", root)
    monkeypatch.setattr(config, "SITES_DIR", root / "sites")
    monkeypatch.setattr(config, "DB_PATH", root / "index.db")
    try:
        with pytest.raises(sqlite3.OperationalError, match="쓰기 권한"):
            with db.connect():
                pass
    finally:
        root.chmod(0o755)


@pytest.mark.skipif(os.geteuid() == 0, reason="root 는 권한 검사를 우회한다")
def test_ensure_dirs_unwritable_dir_friendly_error(tmp_path, monkeypatch):
    """sites 디렉토리 생성이 막혀도 원인을 알려주는 메시지로 실패한다."""
    root = tmp_path / "archive"
    root.mkdir()
    root.chmod(0o555)
    monkeypatch.setattr(config, "ARCHIVE_ROOT", root)
    monkeypatch.setattr(config, "SITES_DIR", root / "sites")
    try:
        with pytest.raises(PermissionError, match="쓰기 권한"):
            config.ensure_dirs()
    finally:
        root.chmod(0o755)
