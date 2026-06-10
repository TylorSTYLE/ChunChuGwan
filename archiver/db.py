"""SQLite 인덱스 레이어. 모든 DB 접근은 이 모듈을 통해서만 한다."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
    id          INTEGER PRIMARY KEY,
    url         TEXT NOT NULL UNIQUE,   -- 정규화된 URL
    domain      TEXT NOT NULL,
    slug        TEXT NOT NULL,          -- 디렉토리명 {slug}-{hash8}
    created_at  TEXT NOT NULL           -- ISO 8601 UTC
);

CREATE TABLE IF NOT EXISTS snapshots (
    id            INTEGER PRIMARY KEY,
    page_id       INTEGER NOT NULL REFERENCES pages(id),
    taken_at      TEXT NOT NULL,        -- ISO 8601 UTC, 디렉토리명과 일치
    dir_name      TEXT NOT NULL,        -- 스냅샷 디렉토리명
    content_hash  TEXT NOT NULL,        -- 정규화 텍스트 SHA-256
    final_url     TEXT NOT NULL,        -- 리다이렉트 후 최종 URL
    http_status   INTEGER,
    changed       INTEGER NOT NULL DEFAULT 1,  -- 직전 스냅샷 대비 변경 여부
    note          TEXT
);

CREATE TABLE IF NOT EXISTS checks (
    id          INTEGER PRIMARY KEY,
    page_id     INTEGER NOT NULL REFERENCES pages(id),
    checked_at  TEXT NOT NULL,
    content_hash TEXT NOT NULL          -- 동일해서 저장 생략된 해시
);

CREATE INDEX IF NOT EXISTS idx_snapshots_page ON snapshots(page_id, taken_at);
CREATE INDEX IF NOT EXISTS idx_checks_page ON checks(page_id, checked_at);
"""


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """스키마가 보장된 커넥션을 컨텍스트로 제공."""
    config.ensure_dirs()
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _utcnow() -> str:
    """ISO 8601 UTC 현재 시각."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_or_create_page(conn: sqlite3.Connection, url: str, domain: str, slug: str) -> int:
    """정규화 URL로 page row를 찾거나 생성하고 id 반환."""
    row = conn.execute("SELECT id FROM pages WHERE url = ?", (url,)).fetchone()
    if row is not None:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO pages (url, domain, slug, created_at) VALUES (?, ?, ?, ?)",
        (url, domain, slug, _utcnow()),
    )
    return cur.lastrowid


def last_snapshot(conn: sqlite3.Connection, page_id: int) -> sqlite3.Row | None:
    """해당 페이지의 가장 최근 스냅샷 row (없으면 None)."""
    return conn.execute(
        "SELECT * FROM snapshots WHERE page_id = ? ORDER BY taken_at DESC, id DESC LIMIT 1",
        (page_id,),
    ).fetchone()


_SNAPSHOT_COLUMNS = frozenset(
    {"taken_at", "dir_name", "content_hash", "final_url", "http_status", "changed", "note"}
)


def insert_snapshot(conn: sqlite3.Connection, page_id: int, **fields) -> int:
    """스냅샷 row 삽입 후 id 반환. fields 키는 snapshots 컬럼만 허용."""
    unknown = set(fields) - _SNAPSHOT_COLUMNS
    if unknown:
        raise ValueError(f"snapshots에 없는 컬럼: {sorted(unknown)}")
    cols = ["page_id", *fields]
    placeholders = ", ".join("?" for _ in cols)
    cur = conn.execute(
        f"INSERT INTO snapshots ({', '.join(cols)}) VALUES ({placeholders})",
        (page_id, *fields.values()),
    )
    return cur.lastrowid


def insert_check(conn: sqlite3.Connection, page_id: int, content_hash: str) -> None:
    """콘텐츠 동일로 저장을 생략한 확인 기록 추가."""
    conn.execute(
        "INSERT INTO checks (page_id, checked_at, content_hash) VALUES (?, ?, ?)",
        (page_id, _utcnow(), content_hash),
    )


def list_pages(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """페이지 목록 + 스냅샷 수 + 마지막 캡처 시각 (대시보드/CLI list 용)."""
    return conn.execute(
        """
        SELECT p.*, COUNT(s.id) AS snapshot_count, MAX(s.taken_at) AS last_taken_at
        FROM pages p
        LEFT JOIN snapshots s ON s.page_id = p.id
        GROUP BY p.id
        ORDER BY last_taken_at DESC NULLS LAST, p.url
        """
    ).fetchall()


def list_snapshots(conn: sqlite3.Connection, page_id: int) -> list[sqlite3.Row]:
    """해당 페이지의 스냅샷 목록 (오래된 순 — history 번호 기준)."""
    return conn.execute(
        "SELECT * FROM snapshots WHERE page_id = ? ORDER BY taken_at ASC, id ASC",
        (page_id,),
    ).fetchall()
