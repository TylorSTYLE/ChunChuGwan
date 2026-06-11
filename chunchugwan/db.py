"""SQLite 인덱스 레이어. 모든 DB 접근은 이 모듈을 통해서만 한다."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
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

CREATE TABLE IF NOT EXISTS schedules (
    id               INTEGER PRIMARY KEY,
    page_id          INTEGER NOT NULL UNIQUE REFERENCES pages(id),
    interval_seconds INTEGER NOT NULL,   -- 3600(1시간) ~ 604800(1주일), scheduler 가 검증
    next_run_at      TEXT NOT NULL,      -- ISO 8601 UTC
    last_run_at      TEXT,
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_schedules_next ON schedules(next_run_at);

CREATE TABLE IF NOT EXISTS archive_logs (
    id           INTEGER PRIMARY KEY,
    url          TEXT NOT NULL,          -- 정규화 URL (정규화 실패 시 입력 원본)
    domain       TEXT NOT NULL DEFAULT '',
    page_id      INTEGER REFERENCES pages(id),      -- 페이지 생성 전 실패면 NULL
    snapshot_id  INTEGER REFERENCES snapshots(id),  -- 새 스냅샷을 만든 경우에만
    source       TEXT NOT NULL DEFAULT 'cli',       -- 'cli' | 'web' | 'schedule'
    status       TEXT NOT NULL,          -- new|changed|unchanged|forced_same|error
    started_at   TEXT NOT NULL,          -- ISO 8601 UTC
    duration_ms  INTEGER NOT NULL DEFAULT 0,
    http_status  INTEGER,
    content_hash TEXT,
    error        TEXT,                   -- status='error' 일 때 예외 요약
    steps        TEXT                    -- 단계별 기록 JSON [{step, ms, detail}]
);
CREATE INDEX IF NOT EXISTS idx_archive_logs_page ON archive_logs(page_id, started_at);
CREATE INDEX IF NOT EXISTS idx_archive_logs_domain ON archive_logs(domain, started_at);

CREATE TABLE IF NOT EXISTS users (
    id                  INTEGER PRIMARY KEY,
    email               TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash       TEXT,               -- NULL = SSO 전용 계정
    totp_secret         TEXT,               -- NULL = 2FA 미설정
    totp_pending_secret TEXT,               -- 등록 확인 전 임시 시크릿
    totp_last_used_at   TEXT,               -- 마지막으로 사용된 코드의 시간창 (재사용 방지)
    is_admin            INTEGER NOT NULL DEFAULT 0,  -- 최초 구동 시 등록된 관리자
    display_name        TEXT,               -- 표시용 이름 (NULL = 이메일로 표시)
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS identities (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    provider    TEXT NOT NULL,              -- 예: 'authentik'
    subject     TEXT NOT NULL,              -- OIDC sub 클레임
    created_at  TEXT NOT NULL,
    UNIQUE (provider, subject)
);

CREATE TABLE IF NOT EXISTS webauthn_credentials (
    id            INTEGER PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id),
    credential_id TEXT NOT NULL UNIQUE,     -- base64url
    public_key    TEXT NOT NULL,            -- COSE 공개키 (base64url)
    sign_count    INTEGER NOT NULL DEFAULT 0,
    name          TEXT NOT NULL,            -- 사용자가 붙인 이름 (예: '맥북 Touch ID')
    created_at    TEXT NOT NULL,
    last_used_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_webauthn_user ON webauthn_credentials(user_id);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash  TEXT PRIMARY KEY,           -- 세션 토큰의 SHA-256 (원문은 쿠키에만 존재)
    user_id     INTEGER NOT NULL REFERENCES users(id),
    state       TEXT NOT NULL DEFAULT 'active',  -- 'active' | 'pending_totp'(2단계 대기)
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    webauthn_challenge TEXT                 -- 진행 중인 패스키 챌린지 (1회용, base64url)
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS oidc_states (
    state       TEXT PRIMARY KEY,
    nonce       TEXT NOT NULL,
    redirect_to TEXT NOT NULL DEFAULT '/',
    created_at  TEXT NOT NULL
);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """CREATE IF NOT EXISTS 로 커버되지 않는 기존 테이블 변경(컬럼 추가)."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
    if cols and "is_admin" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")
    if cols and "display_name" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN display_name TEXT")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)")}
    if cols and "webauthn_challenge" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN webauthn_challenge TEXT")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """스키마가 보장된 커넥션을 컨텍스트로 제공."""
    config.ensure_dirs()
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _utcnow() -> str:
    """ISO 8601 UTC 현재 시각."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_page(conn: sqlite3.Connection, url: str) -> sqlite3.Row | None:
    """정규화 URL로 page row 조회 (없으면 None). 읽기 전용 명령용."""
    return conn.execute("SELECT * FROM pages WHERE url = ?", (url,)).fetchone()


def get_page_by_id(conn: sqlite3.Connection, page_id: int) -> sqlite3.Row | None:
    """id로 page row 조회 (없으면 None)."""
    return conn.execute("SELECT * FROM pages WHERE id = ?", (page_id,)).fetchone()


def get_snapshot(conn: sqlite3.Connection, snapshot_id: int) -> sqlite3.Row | None:
    """스냅샷 row + 소속 페이지 정보(page_url, domain, slug) 조회."""
    return conn.execute(
        """
        SELECT s.*, p.url AS page_url, p.domain, p.slug
        FROM snapshots s JOIN pages p ON p.id = s.page_id
        WHERE s.id = ?
        """,
        (snapshot_id,),
    ).fetchone()


def list_checks(conn: sqlite3.Connection, page_id: int, limit: int = 20) -> list[sqlite3.Row]:
    """해당 페이지의 최근 확인 기록 (최신 순)."""
    return conn.execute(
        "SELECT * FROM checks WHERE page_id = ? ORDER BY checked_at DESC LIMIT ?",
        (page_id, limit),
    ).fetchall()


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


_ARCHIVE_LOG_COLUMNS = frozenset(
    {
        "url", "domain", "page_id", "snapshot_id", "source", "status",
        "started_at", "duration_ms", "http_status", "content_hash", "error", "steps",
    }
)


def insert_archive_log(conn: sqlite3.Connection, **fields) -> int:
    """아카이브 실행 로그 한 행 삽입 후 id 반환. fields 키는 archive_logs 컬럼만 허용."""
    unknown = set(fields) - _ARCHIVE_LOG_COLUMNS
    if unknown:
        raise ValueError(f"archive_logs에 없는 컬럼: {sorted(unknown)}")
    cols = list(fields)
    placeholders = ", ".join("?" for _ in cols)
    cur = conn.execute(
        f"INSERT INTO archive_logs ({', '.join(cols)}) VALUES ({placeholders})",
        tuple(fields.values()),
    )
    return cur.lastrowid


def list_archive_logs(
    conn: sqlite3.Connection,
    *,
    domain: str | None = None,
    page_id: int | None = None,
    snapshot_id: int | None = None,
    status: str | None = None,
    limit: int = 100,
) -> list[sqlite3.Row]:
    """아카이브 실행 로그 (최신 순). 도메인/페이지/스냅샷/상태로 필터.

    스냅샷이 생긴 로그에는 디렉토리 위치(snap_domain, snap_slug, snap_dir_name)를
    함께 반환한다 — 대시보드가 저장된 파일 목록/용량을 조회하는 데 쓴다.
    """
    where: list[str] = []
    params: list[object] = []
    for cond, value in (
        ("al.domain = ?", domain),
        ("al.page_id = ?", page_id),
        ("al.snapshot_id = ?", snapshot_id),
        ("al.status = ?", status),
    ):
        if value is not None:
            where.append(cond)
            params.append(value)
    sql = """
        SELECT al.*, s.dir_name AS snap_dir_name,
               sp.domain AS snap_domain, sp.slug AS snap_slug
        FROM archive_logs al
        LEFT JOIN snapshots s ON s.id = al.snapshot_id
        LEFT JOIN pages sp ON sp.id = s.page_id
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY al.started_at DESC, al.id DESC LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()


def list_log_domains(conn: sqlite3.Connection) -> list[str]:
    """로그에 등장한 도메인 목록 (대시보드 필터 드롭다운용)."""
    rows = conn.execute(
        "SELECT DISTINCT domain FROM archive_logs WHERE domain != '' ORDER BY domain"
    ).fetchall()
    return [r["domain"] for r in rows]


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


def count_pages(conn: sqlite3.Connection) -> int:
    """전체 페이지 수 (현황 대시보드용)."""
    return conn.execute("SELECT COUNT(*) AS c FROM pages").fetchone()["c"]


def list_snapshot_dirs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """모든 스냅샷의 시각·디렉토리 위치 (id, taken_at, domain, slug, dir_name).

    현황 대시보드가 기간별 스냅샷 수와 디렉토리 용량을 집계하는 데 쓴다.
    """
    return conn.execute(
        """
        SELECT s.id, s.taken_at, p.domain, p.slug, s.dir_name
        FROM snapshots s JOIN pages p ON p.id = s.page_id
        """
    ).fetchall()


def list_recent_snapshots(conn: sqlite3.Connection, limit: int = 10) -> list[sqlite3.Row]:
    """최근 스냅샷 목록 (최신 순) + 페이지 정보 + 해당 페이지 첫 스냅샷 여부."""
    return conn.execute(
        """
        SELECT s.*, p.url AS page_url, p.domain, p.slug,
               NOT EXISTS (
                   SELECT 1 FROM snapshots s2
                   WHERE s2.page_id = s.page_id
                     AND (s2.taken_at < s.taken_at
                          OR (s2.taken_at = s.taken_at AND s2.id < s.id))
               ) AS is_first
        FROM snapshots s JOIN pages p ON p.id = s.page_id
        ORDER BY s.taken_at DESC, s.id DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()


# ---- 반복 아카이빙 스케줄 ----


def get_schedule(conn: sqlite3.Connection, page_id: int) -> sqlite3.Row | None:
    """페이지의 스케줄 row (+ 페이지 url) 조회 (없으면 None)."""
    return conn.execute(
        """
        SELECT sc.*, p.url FROM schedules sc JOIN pages p ON p.id = sc.page_id
        WHERE sc.page_id = ?
        """,
        (page_id,),
    ).fetchone()


def list_schedules(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """전체 스케줄 목록 (+ 페이지 url, 다음 실행이 가까운 순)."""
    return conn.execute(
        """
        SELECT sc.*, p.url FROM schedules sc JOIN pages p ON p.id = sc.page_id
        ORDER BY sc.next_run_at, sc.id
        """
    ).fetchall()


def list_due_schedules(conn: sqlite3.Connection, now_iso: str) -> list[sqlite3.Row]:
    """다음 실행 시각이 지난 스케줄 목록 (+ 페이지 url).

    저장 형식이 동일한 ISO 8601 UTC 라 문자열 비교로 기한을 판정한다.
    """
    return conn.execute(
        """
        SELECT sc.*, p.url FROM schedules sc JOIN pages p ON p.id = sc.page_id
        WHERE sc.next_run_at <= ?
        ORDER BY sc.next_run_at, sc.id
        """,
        (now_iso,),
    ).fetchall()


def upsert_schedule(
    conn: sqlite3.Connection, page_id: int, interval_seconds: int, next_run_at: str
) -> None:
    """페이지 스케줄 등록 (이미 있으면 주기·다음 실행 시각만 교체)."""
    conn.execute(
        """
        INSERT INTO schedules (page_id, interval_seconds, next_run_at, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(page_id) DO UPDATE SET
            interval_seconds = excluded.interval_seconds,
            next_run_at = excluded.next_run_at
        """,
        (page_id, interval_seconds, next_run_at, _utcnow()),
    )


def delete_schedule(conn: sqlite3.Connection, page_id: int) -> bool:
    """페이지 스케줄 해제. 등록이 없었으면 False."""
    cur = conn.execute("DELETE FROM schedules WHERE page_id = ?", (page_id,))
    return cur.rowcount == 1


def mark_schedule_run(
    conn: sqlite3.Connection, schedule_id: int, last_run_at: str, next_run_at: str
) -> None:
    """스케줄 실행 완료 기록 — 마지막 실행 시각과 다음 실행 시각 갱신."""
    conn.execute(
        "UPDATE schedules SET last_run_at = ?, next_run_at = ? WHERE id = ?",
        (last_run_at, next_run_at, schedule_id),
    )


# ---- 사용자 ----
# 주의: SCHEMA 는 CREATE IF NOT EXISTS 라 새 테이블 추가는 자동이지만
# 기존 테이블에 컬럼을 추가하는 변경은 별도 마이그레이션이 필요하다.


def _later(seconds: int) -> str:
    """지금으로부터 seconds 뒤의 ISO 8601 UTC 시각."""
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(
        timespec="seconds"
    )


def get_user_by_email(conn: sqlite3.Connection, email: str) -> sqlite3.Row | None:
    """이메일로 사용자 조회 (대소문자 무시, 없으면 None)."""
    return conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()


def get_user_by_id(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    """id로 사용자 조회 (없으면 None)."""
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def create_user(
    conn: sqlite3.Connection,
    email: str,
    password_hash: str | None = None,
    is_admin: bool = False,
) -> int:
    """사용자 생성 후 id 반환. password_hash=None 이면 SSO 전용 계정."""
    cur = conn.execute(
        "INSERT INTO users (email, password_hash, is_admin, created_at) VALUES (?, ?, ?, ?)",
        (email, password_hash, int(is_admin), _utcnow()),
    )
    return cur.lastrowid


def count_users(conn: sqlite3.Connection) -> int:
    """전체 사용자 수 (0 이면 최초 구동으로 판단)."""
    return conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]


def create_first_admin(
    conn: sqlite3.Connection, email: str, password_hash: str
) -> int | None:
    """users 가 비어 있을 때만 관리자를 생성 (원자적). 이미 사용자가 있으면 None.

    최초 구동 등록 API 가 관리자 등록 후 재사용되는 것을 INSERT 단계에서 차단한다.
    """
    cur = conn.execute(
        """
        INSERT INTO users (email, password_hash, is_admin, created_at)
        SELECT ?, ?, 1, ? WHERE NOT EXISTS (SELECT 1 FROM users)
        """,
        (email, password_hash, _utcnow()),
    )
    return cur.lastrowid if cur.rowcount == 1 else None


def set_display_name(conn: sqlite3.Connection, user_id: int, name: str | None) -> None:
    """표시용 사용자 이름 변경 (None 이면 제거 — 이메일로 표시)."""
    conn.execute("UPDATE users SET display_name = ? WHERE id = ?", (name, user_id))


def set_password_hash(conn: sqlite3.Connection, user_id: int, password_hash: str) -> None:
    """패스워드 해시 교체 (패스워드 변경)."""
    conn.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id)
    )


def delete_user(conn: sqlite3.Connection, user_id: int) -> None:
    """사용자와 종속 데이터(세션·OIDC 연결·패스키)를 일괄 삭제 (계정 탈퇴)."""
    conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM identities WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM webauthn_credentials WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))


def set_totp_pending(conn: sqlite3.Connection, user_id: int, secret: str) -> None:
    """TOTP 등록 확인 대기 시크릿 저장 (재발급 시 덮어씀)."""
    conn.execute(
        "UPDATE users SET totp_pending_secret = ? WHERE id = ?", (secret, user_id)
    )


def confirm_totp(conn: sqlite3.Connection, user_id: int) -> None:
    """대기 중 시크릿을 정식 totp_secret 으로 승격."""
    conn.execute(
        """
        UPDATE users SET totp_secret = totp_pending_secret, totp_pending_secret = NULL
        WHERE id = ? AND totp_pending_secret IS NOT NULL
        """,
        (user_id,),
    )


def disable_totp(conn: sqlite3.Connection, user_id: int) -> None:
    """TOTP 해제 (시크릿/대기 시크릿/재사용 기록 모두 제거)."""
    conn.execute(
        """
        UPDATE users SET totp_secret = NULL, totp_pending_secret = NULL,
                         totp_last_used_at = NULL
        WHERE id = ?
        """,
        (user_id,),
    )


def set_totp_last_used(conn: sqlite3.Connection, user_id: int, window: str) -> None:
    """마지막으로 검증에 성공한 TOTP 시간창 기록 (코드 재사용 방지)."""
    conn.execute(
        "UPDATE users SET totp_last_used_at = ? WHERE id = ?", (window, user_id)
    )


# ---- 패스키 (WebAuthn) ----


def list_passkeys(conn: sqlite3.Connection, user_id: int) -> list[sqlite3.Row]:
    """사용자의 패스키 목록 (등록 순)."""
    return conn.execute(
        "SELECT * FROM webauthn_credentials WHERE user_id = ? ORDER BY created_at, id",
        (user_id,),
    ).fetchall()


def count_passkeys(conn: sqlite3.Connection, user_id: int) -> int:
    """사용자의 패스키 개수 (0 초과면 2단계 인증 대상)."""
    return conn.execute(
        "SELECT COUNT(*) AS c FROM webauthn_credentials WHERE user_id = ?", (user_id,)
    ).fetchone()["c"]


def get_passkey(
    conn: sqlite3.Connection, user_id: int, credential_id: str
) -> sqlite3.Row | None:
    """credential_id 로 해당 사용자의 패스키 조회 (없으면 None)."""
    return conn.execute(
        "SELECT * FROM webauthn_credentials WHERE user_id = ? AND credential_id = ?",
        (user_id, credential_id),
    ).fetchone()


def create_passkey(
    conn: sqlite3.Connection,
    user_id: int,
    credential_id: str,
    public_key: str,
    sign_count: int,
    name: str,
) -> int:
    """패스키 등록 후 id 반환. credential_id 중복이면 IntegrityError."""
    cur = conn.execute(
        """
        INSERT INTO webauthn_credentials
            (user_id, credential_id, public_key, sign_count, name, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (user_id, credential_id, public_key, sign_count, name, _utcnow()),
    )
    return cur.lastrowid


def touch_passkey(conn: sqlite3.Connection, passkey_id: int, sign_count: int) -> None:
    """인증 성공 시 sign_count 와 마지막 사용 시각 갱신."""
    conn.execute(
        "UPDATE webauthn_credentials SET sign_count = ?, last_used_at = ? WHERE id = ?",
        (sign_count, _utcnow(), passkey_id),
    )


def delete_passkey(conn: sqlite3.Connection, user_id: int, passkey_id: int) -> bool:
    """사용자 소유 패스키 삭제. 소유가 아니거나 없으면 False."""
    cur = conn.execute(
        "DELETE FROM webauthn_credentials WHERE id = ? AND user_id = ?",
        (passkey_id, user_id),
    )
    return cur.rowcount == 1


def set_session_challenge(
    conn: sqlite3.Connection, token_hash: str, challenge: str
) -> None:
    """세션에 진행 중인 WebAuthn 챌린지 저장 (재요청 시 덮어씀)."""
    conn.execute(
        "UPDATE sessions SET webauthn_challenge = ? WHERE token_hash = ?",
        (challenge, token_hash),
    )


def consume_session_challenge(conn: sqlite3.Connection, token_hash: str) -> str | None:
    """세션의 챌린지를 꺼내고 즉시 비운다 (1회용)."""
    row = conn.execute(
        "SELECT webauthn_challenge FROM sessions WHERE token_hash = ?", (token_hash,)
    ).fetchone()
    if row is None or row["webauthn_challenge"] is None:
        return None
    conn.execute(
        "UPDATE sessions SET webauthn_challenge = NULL WHERE token_hash = ?",
        (token_hash,),
    )
    return row["webauthn_challenge"]


# ---- 세션 ----


def create_session(
    conn: sqlite3.Connection,
    token_hash: str,
    user_id: int,
    state: str,
    ttl_seconds: int,
) -> None:
    """세션 row 생성. state 는 'active' 또는 'pending_totp'."""
    conn.execute(
        """
        INSERT INTO sessions (token_hash, user_id, state, created_at, expires_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (token_hash, user_id, state, _utcnow(), _later(ttl_seconds)),
    )


def get_session(conn: sqlite3.Connection, token_hash: str) -> sqlite3.Row | None:
    """만료되지 않은 세션 조회 (없거나 만료면 None).

    저장 형식이 동일한 ISO 8601 UTC 라 문자열 비교로 만료를 판정한다.
    """
    return conn.execute(
        "SELECT * FROM sessions WHERE token_hash = ? AND expires_at > ?",
        (token_hash, _utcnow()),
    ).fetchone()


def activate_session(
    conn: sqlite3.Connection, token_hash: str, ttl_seconds: int
) -> None:
    """pending_totp 세션을 active 로 승격하고 만료를 연장."""
    conn.execute(
        "UPDATE sessions SET state = 'active', expires_at = ? WHERE token_hash = ?",
        (_later(ttl_seconds), token_hash),
    )


def delete_session(conn: sqlite3.Connection, token_hash: str) -> None:
    """세션 삭제 (로그아웃)."""
    conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))


def delete_other_sessions(
    conn: sqlite3.Connection, user_id: int, keep_token_hash: str
) -> None:
    """해당 사용자의 다른 세션 일괄 삭제 (패스워드 변경 시 강제 로그아웃)."""
    conn.execute(
        "DELETE FROM sessions WHERE user_id = ? AND token_hash != ?",
        (user_id, keep_token_hash),
    )


def delete_expired_sessions(conn: sqlite3.Connection) -> None:
    """만료 세션 일괄 삭제 (기회적 정리용)."""
    conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (_utcnow(),))


# ---- OIDC ----


def get_identity(
    conn: sqlite3.Connection, provider: str, subject: str
) -> sqlite3.Row | None:
    """(provider, sub)로 연결된 identity 조회 (없으면 None)."""
    return conn.execute(
        "SELECT * FROM identities WHERE provider = ? AND subject = ?",
        (provider, subject),
    ).fetchone()


def create_identity(
    conn: sqlite3.Connection, user_id: int, provider: str, subject: str
) -> None:
    """사용자에 OIDC identity 연결."""
    conn.execute(
        """
        INSERT INTO identities (user_id, provider, subject, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (user_id, provider, subject, _utcnow()),
    )


def create_oidc_state(
    conn: sqlite3.Connection, state: str, nonce: str, redirect_to: str
) -> None:
    """OIDC 로그인 시작 시 state/nonce 기록."""
    conn.execute(
        "INSERT INTO oidc_states (state, nonce, redirect_to, created_at) VALUES (?, ?, ?, ?)",
        (state, nonce, redirect_to, _utcnow()),
    )


def consume_oidc_state(
    conn: sqlite3.Connection, state: str, max_age_seconds: int = 600
) -> sqlite3.Row | None:
    """state 를 조회 후 즉시 삭제 (1회용). 기한 초과면 None."""
    row = conn.execute(
        "SELECT * FROM oidc_states WHERE state = ?", (state,)
    ).fetchone()
    if row is None:
        return None
    conn.execute("DELETE FROM oidc_states WHERE state = ?", (state,))
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)).isoformat(
        timespec="seconds"
    )
    if row["created_at"] <= cutoff:
        return None
    return row
