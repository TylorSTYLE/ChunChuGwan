"""SQLite 인덱스 레이어. 모든 DB 접근은 이 모듈을 통해서만 한다."""

from __future__ import annotations

import logging
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Iterator

from . import config, storage

logger = logging.getLogger(__name__)

# 쓰기 락 대기 한도(초) — WAL 에서도 쓰기는 한 번에 하나라, 동시 쓰기가
# 겹치면 이 시간만큼 기다린 뒤 OperationalError.
_BUSY_TIMEOUT_SECONDS = 30

# 스키마 보장(executescript + _migrate)은 프로세스당 DB 파일별 1회 —
# 매 커넥션마다 반복하면 요청 지연과 락 경합만 키운다. restore 처럼
# DB 파일 자체를 교체하는 코드는 invalidate_schema_cache() 를 호출할 것.
_schema_ready: set[str] = set()
_schema_lock = threading.Lock()


def invalidate_schema_cache() -> None:
    """스키마 보장 캐시 무효화 — DB 파일을 교체(복원 등)한 뒤 호출."""
    with _schema_lock:
        _schema_ready.clear()

SCHEMA = """
CREATE TABLE IF NOT EXISTS network_tags (
    id          TEXT PRIMARY KEY,       -- GUID (uuid4) — 생성 시 자동 발급
    name        TEXT NOT NULL UNIQUE,   -- 표시 이름 (예: '집 NAS')
    description TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sites (
    id          INTEGER PRIMARY KEY,
    site_key    TEXT NOT NULL UNIQUE,   -- 서브도메인 단위 키 (storage.site_key —
                                        --   www 제거 호스트, 기본 외 포트 포함)
    created_at  TEXT NOT NULL           -- ISO 8601 UTC
);

CREATE TABLE IF NOT EXISTS site_certificates (
    id           INTEGER PRIMARY KEY,
    site_id      INTEGER NOT NULL REFERENCES sites(id),
    host         TEXT NOT NULL,           -- 인증서를 받은 호스트[:포트] (www 변형 구분)
    fingerprint  TEXT NOT NULL,           -- DER sha256 hex — 버전 식별자
    subject      TEXT NOT NULL,
    issuer       TEXT NOT NULL,
    serial       TEXT NOT NULL,
    san          TEXT NOT NULL DEFAULT '[]',  -- SAN 목록 (JSON 배열)
    not_before   TEXT,
    not_after    TEXT,
    signature_algorithm TEXT,
    verified     INTEGER NOT NULL DEFAULT 1,  -- 캡처가 인증서 검증을 통과했는지
    pem          TEXT NOT NULL,           -- 인증서 원문 (PEM — 보관·다운로드)
    first_seen_at TEXT NOT NULL,          -- 이 버전을 처음/마지막으로 본 시각
    last_seen_at  TEXT NOT NULL,
    UNIQUE (site_id, host, fingerprint)
);
CREATE INDEX IF NOT EXISTS idx_site_certificates_site
    ON site_certificates(site_id, last_seen_at);

CREATE TABLE IF NOT EXISTS pages (
    id          INTEGER PRIMARY KEY,
    url         TEXT NOT NULL UNIQUE,   -- 정규화된 URL
    domain      TEXT NOT NULL,
    slug        TEXT NOT NULL,          -- 디렉토리명 {slug}-{hash8}
    site_id     INTEGER REFERENCES sites(id),  -- 소속 사이트 (생성 시 자동 연결)
    network_tag_id TEXT REFERENCES network_tags(id),  -- 사설 대역 페이지의 로컬 네트워크 태그
    credential_id INTEGER REFERENCES site_credentials(id),  -- 아카이빙 시 쓸 로그인 자격증명(선택)
    created_at  TEXT NOT NULL           -- ISO 8601 UTC
);
-- 주의: 마이그레이션으로 추가되는 컬럼(site_id 등)의 인덱스는 SCHEMA 가
-- 아니라 _migrate 에서 만든다 — executescript(SCHEMA)가 _migrate 보다 먼저
-- 실행되므로, 기존 테이블에 아직 없는 컬럼을 참조하면 스키마 보장이 깨진다.

CREATE TABLE IF NOT EXISTS snapshots (
    id            INTEGER PRIMARY KEY,
    page_id       INTEGER NOT NULL REFERENCES pages(id),
    taken_at      TEXT NOT NULL,        -- ISO 8601 UTC, 디렉토리명과 일치
    dir_name      TEXT NOT NULL,        -- 스냅샷 디렉토리명
    content_hash  TEXT NOT NULL,        -- 정규화 텍스트 SHA-256
    final_url     TEXT NOT NULL,        -- 리다이렉트 후 최종 URL
    http_status   INTEGER,
    changed       INTEGER NOT NULL DEFAULT 1,  -- 직전 스냅샷 대비 변경 여부
    note          TEXT,
    resources_indexed INTEGER NOT NULL DEFAULT 0, -- 자원 참조(snapshot_resources) 기록 여부.
                                                  --   0 이면 저장공간 최적화의 백필 대상
    css_externalized INTEGER NOT NULL DEFAULT 0,  -- 인라인 <style> 의 CAS 추출 여부.
                                                  --   0 이면 저장공간 최적화의 추출 대상
    search_indexed INTEGER NOT NULL DEFAULT 0     -- 텍스트 검색 인덱스(snapshot_fts) 반영 여부.
                                                  --   0 이면 'wccg search reindex' 백필 대상
);

CREATE TABLE IF NOT EXISTS checks (
    id          INTEGER PRIMARY KEY,
    page_id     INTEGER NOT NULL REFERENCES pages(id),
    checked_at  TEXT NOT NULL,
    content_hash TEXT NOT NULL          -- 동일해서 저장 생략된 해시
);

CREATE INDEX IF NOT EXISTS idx_snapshots_page ON snapshots(page_id, taken_at);
CREATE INDEX IF NOT EXISTS idx_checks_page ON checks(page_id, checked_at);

CREATE TABLE IF NOT EXISTS snapshot_resources (
    id           INTEGER PRIMARY KEY,
    snapshot_id  INTEGER NOT NULL REFERENCES snapshots(id),
    name         TEXT NOT NULL,         -- 자원 CAS 이름 (sha256 + 확장자, resources.py)
    url          TEXT,                  -- 원본 URL (모를 수 있음 — compact 백필 등)
    UNIQUE (snapshot_id, name)
);
CREATE INDEX IF NOT EXISTS idx_snapshot_resources_name ON snapshot_resources(name);
CREATE INDEX IF NOT EXISTS idx_snapshot_resources_url ON snapshot_resources(url);

CREATE TABLE IF NOT EXISTS snapshot_documents (
    id           INTEGER PRIMARY KEY,
    snapshot_id  INTEGER NOT NULL REFERENCES snapshots(id),
    url          TEXT NOT NULL,          -- 문서 원본 URL
    file         TEXT NOT NULL,          -- 정제된 파일명 (documents.document_filename)
    bytes        INTEGER NOT NULL,
    sha256       TEXT NOT NULL,          -- 문서 CAS 이름의 해시 부분 (documents.py)
    content_type TEXT NOT NULL,
    UNIQUE (snapshot_id, file)
);
CREATE INDEX IF NOT EXISTS idx_snapshot_documents_sha ON snapshot_documents(sha256);

CREATE TABLE IF NOT EXISTS schedules (
    id               INTEGER PRIMARY KEY,
    page_id          INTEGER NOT NULL UNIQUE REFERENCES pages(id),
    interval_seconds INTEGER NOT NULL,   -- 3600(1시간) ~ 2592000(1개월·30일), scheduler 가 검증
    next_run_at      TEXT NOT NULL,      -- ISO 8601 UTC
    last_run_at      TEXT,
    run_at_time      TEXT,               -- 'HH:MM' 서버 로컬 시간 (1일 단위 주기 전용)
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_schedules_next ON schedules(next_run_at);

CREATE TABLE IF NOT EXISTS crawls (
    id             INTEGER PRIMARY KEY,
    start_url      TEXT NOT NULL,      -- 정규화된 시작 URL
    scope_host     TEXT NOT NULL,      -- 범위: 같은 호스트(netloc)만
    scope_path     TEXT NOT NULL,      -- 범위: 이 경로 프리픽스 이하 ('/' 로 끝남)
    status         TEXT NOT NULL DEFAULT 'running',  -- running|done|cancelled
    max_pages      INTEGER NOT NULL,
    max_depth      INTEGER NOT NULL,
    delay_seconds  INTEGER NOT NULL,   -- 페이지 간 최소 간격 (대상 서버 부담 방지)
    source         TEXT NOT NULL DEFAULT 'web',      -- 'web' | 'cli'
    site_id        INTEGER REFERENCES sites(id),     -- 소속 사이트 (생성 시 자동 연결)
    network_tag_id TEXT REFERENCES network_tags(id), -- 사설 대역 크롤의 로컬 네트워크 태그
    credential_id  INTEGER REFERENCES site_credentials(id), -- 크롤 페이지에 적용할 로그인 자격증명(선택)
    created_at     TEXT NOT NULL,
    finished_at    TEXT,
    next_page_at   TEXT NOT NULL       -- 다음 페이지 처리 가능 시각 (ISO 8601 UTC)
);
CREATE INDEX IF NOT EXISTS idx_crawls_status ON crawls(status, next_page_at);

CREATE TABLE IF NOT EXISTS crawl_pages (
    id              INTEGER PRIMARY KEY,
    crawl_id        INTEGER NOT NULL REFERENCES crawls(id),
    url             TEXT NOT NULL,      -- 정규화 URL
    depth           INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'pending', -- pending|in_progress|done|failed
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,               -- 재시도 대기 시각 (NULL = 즉시 가능)
    claimed_at      TEXT,               -- in_progress 시작 시각 (중단 복구 판정용)
    snapshot_id     INTEGER REFERENCES snapshots(id),  -- 이 크롤에서 확인된 스냅샷
    error           TEXT,               -- 마지막 실패 사유
    UNIQUE (crawl_id, url)
);
CREATE INDEX IF NOT EXISTS idx_crawl_pages_status ON crawl_pages(crawl_id, status);

CREATE TABLE IF NOT EXISTS archive_jobs (
    id               INTEGER PRIMARY KEY,
    url              TEXT NOT NULL,                 -- 정규화 URL
    force            INTEGER NOT NULL DEFAULT 0,
    source           TEXT NOT NULL DEFAULT 'web',   -- cli|web|api
    network_tag_id   TEXT REFERENCES network_tags(id),         -- 사설 대역의 로컬 네트워크 태그
    credential_id    INTEGER REFERENCES site_credentials(id),  -- 적용할 로그인 자격증명(선택)
    interval_seconds INTEGER,             -- 아카이빙 후 자동 재아카이빙 주기 등록용(선택)
    run_at           TEXT,                -- 'HH:MM' 서버 로컬 (1일 단위 주기 실행 시각)
    status           TEXT NOT NULL DEFAULT 'pending', -- pending|in_progress (done/failed 는 삭제)
    attempts         INTEGER NOT NULL DEFAULT 0,
    next_attempt_at  TEXT,                -- 재시도 대기 시각 (NULL = 즉시 가능)
    claimed_at       TEXT,                -- in_progress 시작 시각 (중단 복구 판정용)
    error            TEXT,                -- 마지막 실패 사유 (재시도 대기 중 표시용)
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_archive_jobs_status ON archive_jobs(status, next_attempt_at);
-- 같은 URL 의 활성(대기·진행) 작업은 하나만 — 중복 enqueue 를 DB 레벨에서 차단
CREATE UNIQUE INDEX IF NOT EXISTS idx_archive_jobs_active
    ON archive_jobs(url) WHERE status IN ('pending', 'in_progress');

CREATE TABLE IF NOT EXISTS crawl_schedules (
    id               INTEGER PRIMARY KEY,
    start_url        TEXT NOT NULL UNIQUE,  -- 정규화된 시작 URL
    max_pages        INTEGER NOT NULL,      -- 실행 시 새 크롤에 적용할 옵션
    max_depth        INTEGER NOT NULL,
    delay_seconds    INTEGER NOT NULL,
    interval_seconds INTEGER NOT NULL,      -- schedules 와 같은 범위 (scheduler 가 검증)
    next_run_at      TEXT NOT NULL,         -- ISO 8601 UTC
    last_run_at      TEXT,
    run_at_time      TEXT,                  -- 'HH:MM' 서버 로컬 시간 (1일 단위 주기 전용)
    site_id          INTEGER REFERENCES sites(id),       -- 소속 사이트 (생성 시 자동 연결)
    network_tag_id   TEXT REFERENCES network_tags(id),  -- 사설 대역 사이트의 로컬 네트워크 태그
    credential_id    INTEGER REFERENCES site_credentials(id),  -- 주기 크롤에 적용할 로그인 자격증명(선택)
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_crawl_schedules_next ON crawl_schedules(next_run_at);

CREATE TABLE IF NOT EXISTS archive_logs (
    id           INTEGER PRIMARY KEY,
    url          TEXT NOT NULL,          -- 정규화 URL (정규화 실패 시 입력 원본)
    domain       TEXT NOT NULL DEFAULT '',
    page_id      INTEGER REFERENCES pages(id),      -- 페이지 생성 전 실패면 NULL
    snapshot_id  INTEGER REFERENCES snapshots(id),  -- 새 스냅샷을 만든 경우에만
    source       TEXT NOT NULL DEFAULT 'cli',       -- 'cli'|'web'|'schedule'|'api'|'crawl'
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

CREATE TABLE IF NOT EXISTS system_logs (
    id         INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,            -- ISO 8601 UTC
    level      TEXT NOT NULL,            -- INFO|WARNING|ERROR|CRITICAL
    logger     TEXT NOT NULL,            -- 로거 이름 (chunchugwan.capture 등)
    source     TEXT NOT NULL DEFAULT 'serve',  -- 적재 프로세스 ('serve'|'worker'|'cli')
    message    TEXT NOT NULL,
    traceback  TEXT                      -- 예외 로그(logger.exception)의 트레이스백
);
CREATE INDEX IF NOT EXISTS idx_system_logs_time ON system_logs(created_at);

CREATE TABLE IF NOT EXISTS users (
    id                  INTEGER PRIMARY KEY,
    email               TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash       TEXT,               -- NULL = SSO 전용 계정
    totp_secret         TEXT,               -- NULL = 2FA 미설정
    totp_pending_secret TEXT,               -- 등록 확인 전 임시 시크릿
    totp_last_used_at   TEXT,               -- 마지막으로 사용된 코드의 시간창 (재사용 방지)
    role                TEXT NOT NULL DEFAULT 'viewer',  -- admin|archiver|viewer|pending|blocked
    is_founder          INTEGER NOT NULL DEFAULT 0,  -- 최초 등록 관리자 (권한 변경 불가)
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

CREATE TABLE IF NOT EXISTS invites (
    id          INTEGER PRIMARY KEY,
    email       TEXT NOT NULL UNIQUE COLLATE NOCASE,
    token_hash  TEXT NOT NULL UNIQUE,    -- 초대 토큰의 SHA-256 (원문은 링크에만 존재)
    role        TEXT NOT NULL DEFAULT 'viewer',  -- 가입 시 부여할 권한 (blocked 제외)
    invited_by  INTEGER REFERENCES users(id),
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_keys (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,           -- 용도 식별 이름 (예: 'rss-bot')
    token_hash   TEXT NOT NULL UNIQUE,    -- 키 원문의 SHA-256 (원문은 발급 시 1회만 표시)
    prefix       TEXT NOT NULL,           -- 표시용 키 앞부분 (목록에서 식별용)
    can_view     INTEGER NOT NULL DEFAULT 1,   -- 아카이브 데이터 조회 허용
    can_archive  INTEGER NOT NULL DEFAULT 0,   -- 아카이빙 트리거 허용
    created_by   INTEGER REFERENCES users(id), -- 발급한 관리자 (기록용 — 관리는 공동)
    created_at   TEXT NOT NULL,
    expires_at   TEXT,                    -- ISO 8601 UTC, NULL = 영구
    last_used_at TEXT
);

CREATE TABLE IF NOT EXISTS site_credentials (
    id          INTEGER PRIMARY KEY,
    site_id     INTEGER NOT NULL REFERENCES sites(id),
    label       TEXT NOT NULL,           -- 사람이 식별하는 이름 (예: '관리자 계정')
    kind        TEXT NOT NULL,           -- 종류: http_basic | session (확장형)
    secret      TEXT NOT NULL,           -- 암호화된 payload (crypto.encrypt — 평문 금지)
    created_by  INTEGER REFERENCES users(id),  -- 등록한 관리자 (기록용)
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE (site_id, label)
);
CREATE INDEX IF NOT EXISTS idx_site_credentials_site
    ON site_credentials(site_id);

CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

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
    if cols and "display_name" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN display_name TEXT")
    if cols and "role" not in cols:
        # is_admin 시절 사용자: 관리자는 admin, 그 외에는 기존처럼 아카이빙이
        # 가능했으므로 archiver 로 매핑 (레거시 is_admin 컬럼은 그대로 둔다)
        conn.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'viewer'")
        if "is_admin" in cols:
            conn.execute(
                "UPDATE users SET role = CASE WHEN is_admin = 1 THEN 'admin' ELSE 'archiver' END"
            )
        else:
            conn.execute("UPDATE users SET role = 'archiver'")
    if cols and "is_founder" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN is_founder INTEGER NOT NULL DEFAULT 0")
        # 가장 먼저 등록된 관리자를 최초 관리자로 본다
        conn.execute(
            """
            UPDATE users SET is_founder = 1
            WHERE id = (SELECT MIN(id) FROM users WHERE role = 'admin')
            """
        )
    if cols and "timezone" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN timezone TEXT NOT NULL DEFAULT 'UTC'")
    if cols and "locale" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN locale TEXT NOT NULL DEFAULT 'ko'")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)")}
    if cols and "webauthn_challenge" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN webauthn_challenge TEXT")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(schedules)")}
    if cols and "run_at_time" not in cols:
        conn.execute("ALTER TABLE schedules ADD COLUMN run_at_time TEXT")
    # 로컬 네트워크 태그 — network_tags 테이블은 SCHEMA(executescript)가 먼저 만든다
    for table in ("pages", "crawls", "crawl_schedules"):
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if cols and "network_tag_id" not in cols:
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN network_tag_id TEXT REFERENCES network_tags(id)"
            )
    # 로그인 자격증명 참조 — site_credentials 는 SCHEMA 가 먼저 만든다
    for table in ("pages", "crawls", "crawl_schedules"):
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if cols and "credential_id" not in cols:
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN credential_id INTEGER REFERENCES site_credentials(id)"
            )
    # 자원 참조 인덱스 여부 — 0 인 스냅샷은 저장공간 최적화가 백필한다
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(snapshots)")}
    if cols and "resources_indexed" not in cols:
        conn.execute(
            "ALTER TABLE snapshots ADD COLUMN resources_indexed INTEGER NOT NULL DEFAULT 0"
        )
    # 인라인 <style> 추출 여부 — 0 인 스냅샷은 저장공간 최적화가 추출한다
    if cols and "css_externalized" not in cols:
        conn.execute(
            "ALTER TABLE snapshots ADD COLUMN css_externalized INTEGER NOT NULL DEFAULT 0"
        )
    # 텍스트 검색 인덱스 반영 여부 — 0 인 스냅샷은 'wccg search reindex' 백필 대상.
    # (마이그레이션은 컬럼만 추가하고 실제 색인은 명시 명령으로 채운다 — connect
    #  마다 content.md 수천 개를 읽어 첫 연결이 느려지는 것을 막는다.)
    if cols and "search_indexed" not in cols:
        conn.execute(
            "ALTER TABLE snapshots ADD COLUMN search_indexed INTEGER NOT NULL DEFAULT 0"
        )
    _ensure_search_index(conn)
    # 사이트(서브도메인 단위) — sites 테이블은 SCHEMA 가 먼저 만든다
    for table in ("pages", "crawls", "crawl_schedules"):
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if cols and "site_id" not in cols:
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN site_id INTEGER REFERENCES sites(id)"
            )
    # site_id 인덱스는 컬럼 추가 후에만 만들 수 있다 (SCHEMA 주의 주석 참조)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pages_site ON pages(site_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_crawls_site ON crawls(site_id)")
    _backfill_sites(conn)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    """이름의 테이블/가상테이블/뷰가 존재하는지 (FTS5 가용성 분기 등)."""
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
        (name,),
    ).fetchone() is not None


def _ensure_search_index(conn: sqlite3.Connection) -> None:
    """텍스트 검색용 FTS5 가상테이블 생성 (trigram 토크나이저).

    rowid = snapshots.id 라 결과를 snapshots/pages 와 JOIN 한다. 색인 컬럼은
    content(본문 = content.md + 첨부 문서 본문)·title·url. trigram 은 한국어
    부분문자열(길이 3+) 검색을 CJK 단어분절 없이 지원한다 (searchindex.py).

    SQLite 가 FTS5 없이 빌드된 환경에서는 생성이 실패하지만, 검색 기능만
    비활성화될 뿐 기존 아카이빙은 영향받지 않는다 (graceful degradation —
    인증 암호화가 키 부재 시 그 기능만 끄는 원칙 6 과 같은 방식).
    가상테이블은 ALTER 로 못 바꾸므로 executescript(SCHEMA)가 아니라 여기서
    try/except 로 만든다.
    """
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS snapshot_fts "
            "USING fts5(content, title, url, tokenize='trigram')"
        )
    except sqlite3.OperationalError as e:
        logger.warning(
            "SQLite FTS5 미지원 — 텍스트 검색 기능이 비활성화됩니다 (%s). "
            "기존 아카이빙에는 영향이 없습니다.", e
        )


def _backfill_sites(conn: sqlite3.Connection) -> None:
    """site_id 가 비어 있는 기존 행을 사이트에 자동 연결 (멱등).

    사이트 도입 전 데이터의 1회성 마이그레이션이지만, 행 단위로 조건을
    걸어 언제 다시 실행돼도 안전하다. URL 단위 작업이라 수 초면 끝난다.
    """
    for table, url_col in (
        ("pages", "url"), ("crawls", "start_url"), ("crawl_schedules", "start_url"),
    ):
        rows = conn.execute(
            f"SELECT id, {url_col} AS url FROM {table} WHERE site_id IS NULL"
        ).fetchall()
        for row in rows:
            site_id = get_or_create_site(conn, storage.site_key(row["url"]))
            conn.execute(
                f"UPDATE {table} SET site_id = ? WHERE id = ?", (site_id, row["id"])
            )


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """스키마가 보장된 커넥션을 컨텍스트로 제공.

    WAL 저널 모드를 쓴다 — 쓰기(아카이빙·크롤)가 대시보드 읽기를 막지
    않고, 여러 프로세스(serve·워커·CLI)가 같은 DB 를 봐도 안전하다.
    synchronous=NORMAL 은 WAL 권장 설정 — 프로세스 크래시에는 안전하고,
    정전 시 마지막 트랜잭션만 잃을 수 있다 (DB 손상 없음).
    """
    config.ensure_dirs()
    db_path = config.DB_PATH  # 한 번만 읽는다 — 커넥션과 스키마 캐시 키가 같은 파일을 봐야 한다
    try:
        conn = sqlite3.connect(db_path, timeout=_BUSY_TIMEOUT_SECONDS)
    except sqlite3.OperationalError as e:
        raise sqlite3.OperationalError(
            f"DB 파일을 열 수 없습니다: {db_path} — 아카이브 디렉토리의 "
            "쓰기 권한을 확인하세요 (도커 바인드 마운트라면 호스트 디렉토리 "
            "소유자가 컨테이너 사용자 uid 1000 과 다른 경우)"
        ) from e
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    _ensure_schema(conn, db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _ensure_schema(conn: sqlite3.Connection, db_path) -> None:
    """프로세스에서 처음 보는 DB 파일이면 WAL 전환 + 스키마 생성·마이그레이션.

    db_path 는 conn 을 연 경로 — config.DB_PATH 를 다시 읽으면 다른 스레드가
    경로를 바꿨을 때(테스트 등) 엉뚱한 키가 '준비됨'으로 오염될 수 있다.
    """
    key = str(db_path)
    with _schema_lock:
        if key in _schema_ready:
            return
        # journal_mode 는 DB 파일에 영구 저장된다 — 이후 커넥션은 자동 WAL
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()
        _schema_ready.add(key)


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
    """스냅샷 row + 소속 페이지 정보(page_url, domain, slug, network_tag_id) 조회."""
    return conn.execute(
        """
        SELECT s.*, p.url AS page_url, p.domain, p.slug, p.network_tag_id
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


# ---- 사이트 (서브도메인 단위 그룹) ----
# 모든 페이지·크롤·크롤 스케줄은 사이트에 속한다. 키는 storage.site_key —
# www 와 apex 는 같은 사이트, 다른 서브도메인·포트는 다른 사이트.
# 사이트 행은 첫 소속 행이 생길 때 자동 생성되고, 마지막 소속 행이
# 사라지면 자동 삭제된다 (prune_site_if_empty).


def get_or_create_site(conn: sqlite3.Connection, site_key: str) -> int:
    """사이트 키로 site row 를 찾거나 생성하고 id 반환."""
    row = conn.execute(
        "SELECT id FROM sites WHERE site_key = ?", (site_key,)
    ).fetchone()
    if row is not None:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO sites (site_key, created_at) VALUES (?, ?)",
        (site_key, _utcnow()),
    )
    return cur.lastrowid


def get_site(conn: sqlite3.Connection, site_id: int) -> sqlite3.Row | None:
    """id 로 site row 조회 (없으면 None)."""
    return conn.execute("SELECT * FROM sites WHERE id = ?", (site_id,)).fetchone()


def get_site_by_key(conn: sqlite3.Connection, site_key: str) -> sqlite3.Row | None:
    """사이트 키로 site row 조회 (없으면 None)."""
    return conn.execute(
        "SELECT * FROM sites WHERE site_key = ?", (site_key,)
    ).fetchone()


def _site_is_empty(conn: sqlite3.Connection, site_id: int) -> bool:
    """사이트에 소속 행(페이지·크롤·크롤 스케줄)이 하나도 없는지."""
    row = conn.execute(
        """
        SELECT NOT EXISTS (SELECT 1 FROM pages WHERE site_id = :id)
           AND NOT EXISTS (SELECT 1 FROM crawls WHERE site_id = :id)
           AND NOT EXISTS (SELECT 1 FROM crawl_schedules WHERE site_id = :id) AS empty
        """,
        {"id": site_id},
    ).fetchone()
    return bool(row["empty"])


def prune_site_if_empty(conn: sqlite3.Connection, site_id: int | None) -> bool:
    """소속 행(페이지·크롤·크롤 스케줄)이 하나도 없으면 사이트 행 삭제.

    페이지·크롤 스케줄 삭제 경로가 호출한다 — 크롤 회차는 개별 삭제가
    없으므로(사이트 단위 삭제뿐) 회차가 남아 있는 한 사이트도 남는다.
    인증서 이력·로그인 자격증명은 사이트의 부가 기록이라 함께 지운다
    (자격증명은 FK 로 사이트를 참조하므로 사이트 행 삭제 전에 비워야 한다).
    """
    if site_id is None or not _site_is_empty(conn, site_id):
        return False
    conn.execute("DELETE FROM site_credentials WHERE site_id = ?", (site_id,))
    conn.execute("DELETE FROM site_certificates WHERE site_id = ?", (site_id,))
    cur = conn.execute("DELETE FROM sites WHERE id = ?", (site_id,))
    return cur.rowcount == 1


def prune_empty_sites(conn: sqlite3.Connection) -> int:
    """소속 행이 하나도 없는 사이트 행 일괄 삭제 (가져오기 overwrite 등 정리용)."""
    empty_filter = """
          NOT EXISTS (SELECT 1 FROM pages WHERE site_id = sites.id)
          AND NOT EXISTS (SELECT 1 FROM crawls WHERE site_id = sites.id)
          AND NOT EXISTS (SELECT 1 FROM crawl_schedules WHERE site_id = sites.id)
    """
    conn.execute(
        "DELETE FROM site_credentials WHERE site_id IN "
        f"(SELECT id FROM sites WHERE {empty_filter})"
    )
    conn.execute(
        "DELETE FROM site_certificates WHERE site_id IN "
        f"(SELECT id FROM sites WHERE {empty_filter})"
    )
    cur = conn.execute(f"DELETE FROM sites WHERE {empty_filter}")
    return cur.rowcount


def count_sites(conn: sqlite3.Connection) -> int:
    """전체 사이트 수 (현황 대시보드용)."""
    return conn.execute("SELECT COUNT(*) AS c FROM sites").fetchone()["c"]


_CERT_INFO_COLUMNS = (
    "fingerprint", "subject", "issuer", "serial", "san",
    "not_before", "not_after", "signature_algorithm", "pem",
)


def upsert_site_certificate(
    conn: sqlite3.Connection,
    site_id: int,
    info: dict,
    *,
    verified: bool,
) -> bool:
    """사이트 인증서 기록 — 새 버전이면 행 추가, 같은 버전이면 last_seen 갱신.

    버전 식별은 (site_id, host, fingerprint). 갱신된 인증서는 새 행이 되고
    이전 행은 남아 버전 이력이 보존된다. info 는 certs.fetch_certificate_info
    형식. 반환은 새 버전 행을 만들었는지 여부.
    """
    now = _utcnow()
    cur = conn.execute(
        """
        UPDATE site_certificates
        SET last_seen_at = ?, verified = ?
        WHERE site_id = ? AND host = ? AND fingerprint = ?
        """,
        (now, int(verified), site_id, info["host"], info["fingerprint"]),
    )
    if cur.rowcount == 1:
        return False
    conn.execute(
        f"""
        INSERT INTO site_certificates
            (site_id, host, {', '.join(_CERT_INFO_COLUMNS)},
             verified, first_seen_at, last_seen_at)
        VALUES ({', '.join('?' for _ in range(len(_CERT_INFO_COLUMNS) + 5))})
        """,
        (site_id, info["host"], *(info[c] for c in _CERT_INFO_COLUMNS),
         int(verified), now, now),
    )
    return True


def list_site_certificates(
    conn: sqlite3.Connection, site_id: int
) -> list[sqlite3.Row]:
    """사이트의 인증서 버전 이력 (호스트별 최근 확인 순) — 사이트 상세용."""
    return conn.execute(
        """
        SELECT * FROM site_certificates
        WHERE site_id = ? ORDER BY host, last_seen_at DESC, id DESC
        """,
        (site_id,),
    ).fetchall()


def get_site_certificate(
    conn: sqlite3.Connection, site_id: int, cert_id: int
) -> sqlite3.Row | None:
    """사이트 소속 인증서 행 조회 (소속이 아니면 None) — PEM 다운로드 검증용."""
    return conn.execute(
        "SELECT * FROM site_certificates WHERE id = ? AND site_id = ?",
        (cert_id, site_id),
    ).fetchone()


def list_sites_overview(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """사이트 목록 + 페이지·스냅샷·크롤 회차·스케줄 집계 (아카이브 목록 화면용).

    last_activity_at 은 마지막 스냅샷과 마지막 크롤 활동(완료 또는 생성) 중
    더 최근 시각 — 목록 정렬 기준이다.
    """
    return conn.execute(
        """
        SELECT st.*,
               (SELECT COUNT(*) FROM pages p WHERE p.site_id = st.id) AS page_count,
               (SELECT COUNT(*) FROM snapshots s JOIN pages p ON p.id = s.page_id
                 WHERE p.site_id = st.id) AS snapshot_count,
               (SELECT COUNT(*) FROM crawls c WHERE c.site_id = st.id) AS crawl_count,
               (SELECT COUNT(*) FROM crawls c
                 WHERE c.site_id = st.id AND c.status = 'running') AS running_crawl_count,
               (SELECT COUNT(*) FROM schedules sc JOIN pages p ON p.id = sc.page_id
                 WHERE p.site_id = st.id)
                 + (SELECT COUNT(*) FROM crawl_schedules cs
                    WHERE cs.site_id = st.id) AS schedule_count,
               MAX(
                   COALESCE((SELECT MAX(s.taken_at) FROM snapshots s
                             JOIN pages p ON p.id = s.page_id
                             WHERE p.site_id = st.id), ''),
                   COALESCE((SELECT MAX(COALESCE(c.finished_at, c.created_at))
                             FROM crawls c WHERE c.site_id = st.id), '')
               ) AS last_activity_at
        FROM sites st
        ORDER BY last_activity_at DESC, st.site_key
        """
    ).fetchall()


def get_or_create_page(
    conn: sqlite3.Connection,
    url: str,
    domain: str,
    slug: str,
    network_tag_id: str | None = None,
    credential_id: int | None = None,
) -> int:
    """정규화 URL로 page row를 찾거나 생성하고 id 반환.

    소속 사이트(site_id)는 URL 에서 계산해 자동 연결한다 — 같은 서브도메인의
    사이트가 있으면 거기 속하고, 없으면 사이트가 함께 만들어진다.
    network_tag_id 를 주면 기존 페이지의 태그도 갱신한다 — 태그 없이
    만들어진 사설 대역 페이지를 새 아카이빙 폼에서 태그를 골라 다시
    제출하는 것이 태그 지정/변경 경로다.
    credential_id 를 주면 아카이빙에 쓸 로그인 자격증명을 페이지에 연결한다
    (기존 페이지도 갱신) — 새 아카이빙 폼에서 도메인의 자격증명을 골라
    연결하는 경로다. 캡처 연동 단계에서 이 자격증명을 실제 로그인에 쓴다.
    """
    row = conn.execute(
        "SELECT id, network_tag_id, credential_id FROM pages WHERE url = ?", (url,)
    ).fetchone()
    if row is not None:
        if network_tag_id is not None and row["network_tag_id"] != network_tag_id:
            conn.execute(
                "UPDATE pages SET network_tag_id = ? WHERE id = ?",
                (network_tag_id, row["id"]),
            )
        if credential_id is not None and row["credential_id"] != credential_id:
            conn.execute(
                "UPDATE pages SET credential_id = ? WHERE id = ?",
                (credential_id, row["id"]),
            )
        return row["id"]
    site_id = get_or_create_site(conn, storage.site_key(url))
    cur = conn.execute(
        """
        INSERT INTO pages
            (url, domain, slug, site_id, network_tag_id, credential_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (url, domain, slug, site_id, network_tag_id, credential_id, _utcnow()),
    )
    return cur.lastrowid


def last_snapshot(conn: sqlite3.Connection, page_id: int) -> sqlite3.Row | None:
    """해당 페이지의 가장 최근 스냅샷 row (없으면 None)."""
    return conn.execute(
        "SELECT * FROM snapshots WHERE page_id = ? ORDER BY taken_at DESC, id DESC LIMIT 1",
        (page_id,),
    ).fetchone()


_SNAPSHOT_COLUMNS = frozenset(
    {"taken_at", "dir_name", "content_hash", "final_url", "http_status", "changed",
     "note", "resources_indexed", "css_externalized", "search_indexed"}
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


def _adjacent_snapshot(
    conn: sqlite3.Connection, snap: sqlite3.Row, *, after: bool
) -> sqlite3.Row | None:
    """(taken_at, id) 순서상 바로 앞/뒤 스냅샷 (없으면 None)."""
    op, order = (">", "ASC") if after else ("<", "DESC")
    return conn.execute(
        f"""
        SELECT * FROM snapshots
        WHERE page_id = ?
          AND (taken_at {op} ? OR (taken_at = ? AND id {op} ?))
        ORDER BY taken_at {order}, id {order} LIMIT 1
        """,
        (snap["page_id"], snap["taken_at"], snap["taken_at"], snap["id"]),
    ).fetchone()


def delete_snapshot(conn: sqlite3.Connection, snapshot_id: int) -> bool:
    """스냅샷 row 삭제. 없는 id 면 False.

    - archive_logs 의 snapshot_id 참조는 NULL 로 해제 (실행 이력은 보존).
    - 바로 다음 스냅샷의 changed 를 '새 직전 스냅샷' 기준으로 재계산해
      히스토리의 변경 표시가 어긋나지 않게 한다 (예: A→B→A 에서 B 를
      지우면 마지막 스냅샷은 '동일'이 된다). 첫 스냅샷이 되면 changed=1.
    """
    snap = conn.execute(
        "SELECT * FROM snapshots WHERE id = ?", (snapshot_id,)
    ).fetchone()
    if snap is None:
        return False
    prev = _adjacent_snapshot(conn, snap, after=False)
    nxt = _adjacent_snapshot(conn, snap, after=True)
    conn.execute(
        "UPDATE archive_logs SET snapshot_id = NULL WHERE snapshot_id = ?",
        (snapshot_id,),
    )
    conn.execute(
        "UPDATE crawl_pages SET snapshot_id = NULL WHERE snapshot_id = ?",
        (snapshot_id,),
    )
    conn.execute(
        "DELETE FROM snapshot_documents WHERE snapshot_id = ?", (snapshot_id,)
    )
    conn.execute(
        "DELETE FROM snapshot_resources WHERE snapshot_id = ?", (snapshot_id,)
    )
    if _table_exists(conn, "snapshot_fts"):
        conn.execute("DELETE FROM snapshot_fts WHERE rowid = ?", (snapshot_id,))
    conn.execute("DELETE FROM snapshots WHERE id = ?", (snapshot_id,))
    if nxt is not None:
        changed = 1 if prev is None else int(prev["content_hash"] != nxt["content_hash"])
        conn.execute(
            "UPDATE snapshots SET changed = ? WHERE id = ?", (changed, nxt["id"])
        )
    return True


def delete_page(conn: sqlite3.Connection, page_id: int) -> bool:
    """페이지와 종속 데이터(스냅샷·확인 기록·스케줄)를 일괄 삭제. 없으면 False.

    archive_logs 는 실행 이력으로 보존하되 page_id/snapshot_id 참조만 해제한다.
    사이트의 마지막 소속 행이었다면 사이트 행도 함께 삭제된다.
    """
    page = get_page_by_id(conn, page_id)
    if page is None:
        return False
    conn.execute(
        """
        UPDATE archive_logs SET snapshot_id = NULL
        WHERE snapshot_id IN (SELECT id FROM snapshots WHERE page_id = ?)
        """,
        (page_id,),
    )
    conn.execute(
        "UPDATE archive_logs SET page_id = NULL WHERE page_id = ?", (page_id,)
    )
    conn.execute(
        """
        UPDATE crawl_pages SET snapshot_id = NULL
        WHERE snapshot_id IN (SELECT id FROM snapshots WHERE page_id = ?)
        """,
        (page_id,),
    )
    conn.execute(
        """
        DELETE FROM snapshot_documents
        WHERE snapshot_id IN (SELECT id FROM snapshots WHERE page_id = ?)
        """,
        (page_id,),
    )
    conn.execute(
        """
        DELETE FROM snapshot_resources
        WHERE snapshot_id IN (SELECT id FROM snapshots WHERE page_id = ?)
        """,
        (page_id,),
    )
    if _table_exists(conn, "snapshot_fts"):
        conn.execute(
            "DELETE FROM snapshot_fts WHERE rowid IN "
            "(SELECT id FROM snapshots WHERE page_id = ?)",
            (page_id,),
        )
    conn.execute("DELETE FROM checks WHERE page_id = ?", (page_id,))
    conn.execute("DELETE FROM schedules WHERE page_id = ?", (page_id,))
    conn.execute("DELETE FROM snapshots WHERE page_id = ?", (page_id,))
    conn.execute("DELETE FROM pages WHERE id = ?", (page_id,))
    prune_site_if_empty(conn, page["site_id"])
    return True


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


def get_archive_log(conn: sqlite3.Connection, log_id: int) -> sqlite3.Row | None:
    """아카이브 로그 한 행 조회 (없으면 None) — 행 단위 동작(재시도)용."""
    return conn.execute(
        "SELECT * FROM archive_logs WHERE id = ?", (log_id,)
    ).fetchone()


def _archive_log_where(
    *,
    domain: str | None,
    page_id: int | None,
    snapshot_id: int | None,
    status: str | None,
    date_from: str | None,
    date_to: str | None,
) -> tuple[str, list[object]]:
    """archive_logs 필터 WHERE 절 조립 (list/count 공용).

    date_from/date_to 는 YYYY-MM-DD. started_at 이 ISO 8601 이므로 하한은
    문자열 비교로, 상한은 다음날 0시 미만으로 비교한다 (해당 날짜 포함).
    """
    where: list[str] = []
    params: list[object] = []
    for cond, value in (
        ("al.domain = ?", domain),
        ("al.page_id = ?", page_id),
        ("al.snapshot_id = ?", snapshot_id),
        ("al.status = ?", status),
        ("al.started_at >= ?", date_from),
        ("al.started_at < DATE(?, '+1 day')", date_to),
    ):
        if value is not None:
            where.append(cond)
            params.append(value)
    return (" WHERE " + " AND ".join(where)) if where else "", params


def list_archive_logs(
    conn: sqlite3.Connection,
    *,
    domain: str | None = None,
    page_id: int | None = None,
    snapshot_id: int | None = None,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[sqlite3.Row]:
    """아카이브 실행 로그 (최신 순). 도메인/페이지/스냅샷/상태/기간으로 필터.

    스냅샷이 생긴 로그에는 디렉토리 위치(snap_domain, snap_slug, snap_dir_name)를
    함께 반환한다 — 대시보드가 저장된 파일 목록/용량을 조회하는 데 쓴다.
    사설 대역 페이지의 로그 구분용으로 로컬 네트워크 태그 이름·설명도 붙인다.
    """
    where_sql, params = _archive_log_where(
        domain=domain, page_id=page_id, snapshot_id=snapshot_id,
        status=status, date_from=date_from, date_to=date_to,
    )
    sql = """
        SELECT al.*, s.dir_name AS snap_dir_name,
               sp.domain AS snap_domain, sp.slug AS snap_slug,
               nt.name AS network_tag_name,
               nt.description AS network_tag_description,
               lp.network_tag_id
        FROM archive_logs al
        LEFT JOIN snapshots s ON s.id = al.snapshot_id
        LEFT JOIN pages sp ON sp.id = s.page_id
        LEFT JOIN pages lp ON lp.id = al.page_id
        LEFT JOIN network_tags nt ON nt.id = lp.network_tag_id
    """
    sql += where_sql
    sql += " ORDER BY al.started_at DESC, al.id DESC LIMIT ? OFFSET ?"
    params += [limit, offset]
    return conn.execute(sql, params).fetchall()


def count_archive_logs(
    conn: sqlite3.Connection,
    *,
    domain: str | None = None,
    page_id: int | None = None,
    snapshot_id: int | None = None,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> int:
    """필터 조건에 맞는 아카이브 로그 총 건수 (페이징용)."""
    where_sql, params = _archive_log_where(
        domain=domain, page_id=page_id, snapshot_id=snapshot_id,
        status=status, date_from=date_from, date_to=date_to,
    )
    row = conn.execute(
        "SELECT COUNT(*) FROM archive_logs al" + where_sql, params
    ).fetchone()
    return row[0]


def list_snapshot_archive_logs(
    conn: sqlite3.Connection, page_id: int
) -> list[sqlite3.Row]:
    """페이지의 스냅샷 생성 실행 로그 (snapshot_id 가 있는 행만).

    타임라인 화면이 스냅샷별 단계 소요·오류를 펼쳐 보이는 데 쓴다.
    """
    return conn.execute(
        """
        SELECT * FROM archive_logs
        WHERE page_id = ? AND snapshot_id IS NOT NULL
        ORDER BY id
        """,
        (page_id,),
    ).fetchall()


def list_log_domains(conn: sqlite3.Connection) -> list[str]:
    """로그에 등장한 도메인 목록 (대시보드 필터 드롭다운용)."""
    rows = conn.execute(
        "SELECT DISTINCT domain FROM archive_logs WHERE domain != '' ORDER BY domain"
    ).fetchall()
    return [r["domain"] for r in rows]


# ---- 시스템 로그 (system_logs — system_log.py 의 logging 핸들러가 적재) ----

SYSTEM_LOG_LEVELS = ("INFO", "WARNING", "ERROR", "CRITICAL")
SYSTEM_LOG_SOURCES = ("serve", "worker", "cli")


def insert_system_log(
    conn: sqlite3.Connection,
    *,
    created_at: str,
    level: str,
    logger: str,
    source: str,
    message: str,
    traceback: str | None = None,
) -> int:
    """시스템 로그 한 행 삽입 후 id 반환."""
    cur = conn.execute(
        "INSERT INTO system_logs (created_at, level, logger, source, message, traceback)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (created_at, level, logger, source, message, traceback),
    )
    return cur.lastrowid


def _system_log_where(
    *,
    level: str | None,
    source: str | None,
    date_from: str | None,
    date_to: str | None,
) -> tuple[str, list[object]]:
    """system_logs 필터 WHERE 절 조립 (list/count 공용). 날짜 의미는 archive_logs 와 동일."""
    where: list[str] = []
    params: list[object] = []
    for cond, value in (
        ("level = ?", level),
        ("source = ?", source),
        ("created_at >= ?", date_from),
        ("created_at < DATE(?, '+1 day')", date_to),
    ):
        if value is not None:
            where.append(cond)
            params.append(value)
    return (" WHERE " + " AND ".join(where)) if where else "", params


def list_system_logs(
    conn: sqlite3.Connection,
    *,
    level: str | None = None,
    source: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[sqlite3.Row]:
    """시스템 로그 (최신 순). 레벨/출처/기간으로 필터."""
    where_sql, params = _system_log_where(
        level=level, source=source, date_from=date_from, date_to=date_to,
    )
    sql = (
        "SELECT * FROM system_logs" + where_sql
        + " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
    )
    return conn.execute(sql, params + [limit, offset]).fetchall()


def count_system_logs(
    conn: sqlite3.Connection,
    *,
    level: str | None = None,
    source: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> int:
    """필터 조건에 맞는 시스템 로그 총 건수 (페이징용)."""
    where_sql, params = _system_log_where(
        level=level, source=source, date_from=date_from, date_to=date_to,
    )
    row = conn.execute("SELECT COUNT(*) FROM system_logs" + where_sql, params).fetchone()
    return row[0]


def prune_system_logs(conn: sqlite3.Connection, keep: int) -> int:
    """최신 keep 건만 남기고 오래된 시스템 로그 삭제. 삭제 건수 반환."""
    cur = conn.execute(
        "DELETE FROM system_logs WHERE id < ("
        " SELECT COALESCE(MIN(id), 0) FROM ("
        "  SELECT id FROM system_logs ORDER BY id DESC LIMIT ?))",
        (keep,),
    )
    return cur.rowcount


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
    """모든 스냅샷의 시각·디렉토리 위치 (id, taken_at, site_id, domain, slug, dir_name).

    현황 대시보드의 기간별 집계와 아카이브 목록의 사이트별 용량 합산에 쓴다.
    """
    return conn.execute(
        """
        SELECT s.id, s.taken_at, p.site_id, p.domain, p.slug, s.dir_name
        FROM snapshots s JOIN pages p ON p.id = s.page_id
        """
    ).fetchall()


def list_recent_snapshots(conn: sqlite3.Connection, limit: int = 10) -> list[sqlite3.Row]:
    """최근 스냅샷 목록 (최신 순) + 페이지 정보 + 해당 페이지 첫 스냅샷 여부.

    사설 대역 페이지 구분용으로 로컬 네트워크 태그 이름·설명도 붙인다.
    """
    return conn.execute(
        """
        SELECT s.*, p.url AS page_url, p.domain, p.slug, p.network_tag_id,
               nt.name AS network_tag_name,
               nt.description AS network_tag_description,
               NOT EXISTS (
                   SELECT 1 FROM snapshots s2
                   WHERE s2.page_id = s.page_id
                     AND (s2.taken_at < s.taken_at
                          OR (s2.taken_at = s.taken_at AND s2.id < s.id))
               ) AS is_first
        FROM snapshots s
        JOIN pages p ON p.id = s.page_id
        LEFT JOIN network_tags nt ON nt.id = p.network_tag_id
        ORDER BY s.taken_at DESC, s.id DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()


# ---- 스냅샷의 공유 자원 참조 (자원 CAS — resources.py) ----
# page.html.gz 가 /resource/{name} 으로 참조하는 자원의 인덱스. 스냅샷 삭제
# 시 참조가 0 이 된 CAS 파일을 GC 하고, 캡처의 자원 인라인 실패 시 같은
# URL 의 과거 캡처본을 재사용하는 폴백 조회에 쓴다.


def insert_snapshot_resources(
    conn: sqlite3.Connection, snapshot_id: int, refs: list[dict]
) -> None:
    """스냅샷의 자원 참조 행들 삽입 (refs: [{name, url}], url 은 None 가능).

    INSERT OR IGNORE — compact 백필 재실행 등 같은 (snapshot_id, name)
    재기록에 멱등.
    """
    conn.executemany(
        """
        INSERT OR IGNORE INTO snapshot_resources (snapshot_id, name, url)
        VALUES (?, ?, ?)
        """,
        [(snapshot_id, r["name"], r.get("url")) for r in refs],
    )


def find_resource_by_url(conn: sqlite3.Connection, url: str) -> str | None:
    """URL 로 가장 최근에 저장된 자원의 CAS 이름 조회 (없으면 None).

    캡처의 자원 인라인 실패 폴백 — 같은 URL 을 과거에 성공적으로 받아둔
    적이 있으면 그 콘텐츠를 재사용한다.
    """
    row = conn.execute(
        "SELECT name FROM snapshot_resources WHERE url = ? ORDER BY id DESC LIMIT 1",
        (url,),
    ).fetchone()
    return row["name"] if row else None


def list_snapshot_resource_refs(
    conn: sqlite3.Connection, snapshot_ids: list[int]
) -> list[str]:
    """해당 스냅샷들이 참조하는 자원 CAS 이름 목록 (중복 제거) — 삭제 GC 용."""
    if not snapshot_ids:
        return []
    marks = ", ".join("?" for _ in snapshot_ids)
    return [
        r["name"]
        for r in conn.execute(
            f"SELECT DISTINCT name FROM snapshot_resources "
            f"WHERE snapshot_id IN ({marks})",
            snapshot_ids,
        )
    ]


def list_resource_refs_by_names(
    conn: sqlite3.Connection, names: list[str]
) -> list[str]:
    """해당 이름들을 참조하는 자원 이름 목록 — 삭제 후 잔존 참조 확인용."""
    if not names:
        return []
    marks = ", ".join("?" for _ in names)
    return [
        r["name"]
        for r in conn.execute(
            f"SELECT DISTINCT name FROM snapshot_resources WHERE name IN ({marks})",
            names,
        )
    ]


def count_unindexed_snapshots(conn: sqlite3.Connection) -> int:
    """자원 참조가 아직 기록되지 않은 스냅샷 수 — 최적화 백필 대상."""
    return conn.execute(
        "SELECT COUNT(*) AS c FROM snapshots WHERE resources_indexed = 0"
    ).fetchone()["c"]


def list_unindexed_snapshots(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """자원 참조 백필 대상 스냅샷 (+ 디렉토리 위치) — 저장공간 최적화용."""
    return conn.execute(
        """
        SELECT s.id, p.domain, p.slug, s.dir_name
        FROM snapshots s JOIN pages p ON p.id = s.page_id
        WHERE s.resources_indexed = 0 ORDER BY s.id
        """
    ).fetchall()


def mark_snapshot_resources_indexed(
    conn: sqlite3.Connection, snapshot_id: int
) -> None:
    """스냅샷의 자원 참조 기록 완료 표시 — 이후 백필 대상에서 제외된다."""
    conn.execute(
        "UPDATE snapshots SET resources_indexed = 1 WHERE id = ?", (snapshot_id,)
    )


def count_css_pending_snapshots(conn: sqlite3.Connection) -> int:
    """인라인 <style> 추출이 아직 안 된 스냅샷 수 — 최적화 대상."""
    return conn.execute(
        "SELECT COUNT(*) AS c FROM snapshots WHERE css_externalized = 0"
    ).fetchone()["c"]


def list_css_pending_snapshots(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """인라인 <style> 추출 대상 스냅샷 (+ 디렉토리 위치) — 저장공간 최적화용.

    final_url 은 상대 CSS 참조 절대화 기준으로 함께 내려준다.
    """
    return conn.execute(
        """
        SELECT s.id, p.domain, p.slug, s.dir_name, s.final_url
        FROM snapshots s JOIN pages p ON p.id = s.page_id
        WHERE s.css_externalized = 0 ORDER BY s.id
        """
    ).fetchall()


def mark_snapshot_css_externalized(
    conn: sqlite3.Connection, snapshot_id: int
) -> None:
    """스냅샷의 인라인 <style> 추출 완료 표시 — 이후 추출 대상에서 제외된다."""
    conn.execute(
        "UPDATE snapshots SET css_externalized = 1 WHERE id = ?", (snapshot_id,)
    )


def list_all_resource_names(conn: sqlite3.Connection) -> set[str]:
    """참조 중인 자원 CAS 이름 전체 — 고아 자원 정리(sweep)의 기준."""
    return {
        r["name"]
        for r in conn.execute("SELECT DISTINCT name FROM snapshot_resources")
    }


# ---- 텍스트 검색 인덱스 (snapshot_fts — FTS5 trigram, searchindex.py) ----
# 색인 본문은 스냅샷의 content.md(정규화 텍스트) + 첨부 문서 본문. rowid =
# snapshots.id 라 결과를 snapshots/pages 와 JOIN 한다. 색인 쓰기/조회는 모두
# 여기를 거친다 (원칙 1 — 쓰기는 코어). 텍스트 조립·문서 추출·스니펫 생성은
# searchindex.py 가 맡는다.


def search_index_available(conn: sqlite3.Connection) -> bool:
    """FTS5 검색 인덱스 테이블이 존재하는지 (FTS5 미지원 환경이면 False)."""
    return _table_exists(conn, "snapshot_fts")


def clear_search_index(conn: sqlite3.Connection) -> None:
    """검색 인덱스 전체 비우기 (가져오기 overwrite 등) — 테이블 없으면 무시."""
    if _table_exists(conn, "snapshot_fts"):
        conn.execute("DELETE FROM snapshot_fts")


def upsert_snapshot_fts(
    conn: sqlite3.Connection,
    snapshot_id: int,
    content: str,
    title: str | None,
    url: str | None,
) -> None:
    """스냅샷의 검색 인덱스 행 갱신 (rowid=snapshot_id). 재색인에 멱등."""
    conn.execute(
        "INSERT OR REPLACE INTO snapshot_fts (rowid, content, title, url) "
        "VALUES (?, ?, ?, ?)",
        (snapshot_id, content, title or "", url or ""),
    )


def count_unindexed_search_snapshots(conn: sqlite3.Connection) -> int:
    """검색 인덱스에 아직 반영되지 않은 스냅샷 수 — reindex 백필 대상."""
    return conn.execute(
        "SELECT COUNT(*) AS c FROM snapshots WHERE search_indexed = 0"
    ).fetchone()["c"]


def list_unindexed_search_snapshots(
    conn: sqlite3.Connection, limit: int | None = None
) -> list[sqlite3.Row]:
    """검색 인덱스 백필 대상 스냅샷 (+ 위치·URL) — content.md 읽기용."""
    sql = """
        SELECT s.id, s.dir_name, p.url AS page_url, p.domain, p.slug
        FROM snapshots s JOIN pages p ON p.id = s.page_id
        WHERE s.search_indexed = 0 ORDER BY s.id
    """
    if limit is not None:
        return conn.execute(sql + " LIMIT ?", (limit,)).fetchall()
    return conn.execute(sql).fetchall()


def mark_snapshot_search_indexed(conn: sqlite3.Connection, snapshot_id: int) -> None:
    """스냅샷의 검색 인덱스 반영 완료 표시 — 이후 백필 대상에서 제외."""
    conn.execute(
        "UPDATE snapshots SET search_indexed = 1 WHERE id = ?", (snapshot_id,)
    )


def reset_search_indexed(conn: sqlite3.Connection) -> None:
    """모든 스냅샷을 미색인으로 표시 (전체 재색인 — reindex --all)."""
    conn.execute("UPDATE snapshots SET search_indexed = 0")


# 검색 결과 행의 공통 투영 — FTS/LIKE 양쪽이 같은 형태를 돌려준다.
# (url 은 pages.url 과 snapshot_fts.url 이 겹치므로 반드시 테이블로 한정한다.)
_SEARCH_SELECT = """
    SELECT snapshot_fts.rowid AS snapshot_id, s.page_id, s.taken_at, s.changed,
           p.url AS page_url, p.domain,
           snapshot_fts.content AS content, snapshot_fts.title AS title
    FROM snapshot_fts
    JOIN snapshots s ON s.id = snapshot_fts.rowid
    JOIN pages p ON p.id = s.page_id
"""
# 같은 URL 의 여러 스냅샷 중 최신 1건만 (현재 본문 검색 토글)
_SEARCH_LATEST_CLAUSE = (
    " AND s.id = (SELECT id FROM snapshots s2 WHERE s2.page_id = s.page_id"
    " ORDER BY s2.taken_at DESC, s2.id DESC LIMIT 1)"
)


def search_snapshots_fts(
    conn: sqlite3.Connection,
    match: str,
    *,
    domain: str | None = None,
    latest_only: bool = False,
    limit: int = 20,
    offset: int = 0,
) -> list[sqlite3.Row]:
    """FTS MATCH 검색 (bm25 랭킹 순). match 는 searchindex 가 조립한 안전한 질의."""
    sql = _SEARCH_SELECT + " WHERE snapshot_fts MATCH ?"
    params: list[object] = [match]
    if domain:
        sql += " AND p.domain = ?"
        params.append(domain)
    if latest_only:
        sql += _SEARCH_LATEST_CLAUSE
    sql += " ORDER BY bm25(snapshot_fts), s.taken_at DESC, s.id DESC LIMIT ? OFFSET ?"
    params += [limit, offset]
    return conn.execute(sql, params).fetchall()


def count_search_snapshots_fts(
    conn: sqlite3.Connection,
    match: str,
    *,
    domain: str | None = None,
    latest_only: bool = False,
) -> int:
    """FTS MATCH 결과 총 건수 (페이징용)."""
    sql = (
        "SELECT COUNT(*) AS c FROM snapshot_fts "
        "JOIN snapshots s ON s.id = snapshot_fts.rowid "
        "JOIN pages p ON p.id = s.page_id WHERE snapshot_fts MATCH ?"
    )
    params: list[object] = [match]
    if domain:
        sql += " AND p.domain = ?"
        params.append(domain)
    if latest_only:
        sql += _SEARCH_LATEST_CLAUSE
    return conn.execute(sql, params).fetchone()["c"]


def _like_where(patterns: list[str]) -> tuple[str, list[object]]:
    """짧은 쿼리(LIKE 폴백)의 WHERE 절 — 각 패턴이 content/title/url 중 하나에
    부분일치(AND). 패턴은 searchindex 가 % _ \\ 이스케이프해 만든다."""
    clause = " AND ".join(
        "(snapshot_fts.content LIKE ? ESCAPE '\\' "
        "OR snapshot_fts.title LIKE ? ESCAPE '\\' "
        "OR snapshot_fts.url LIKE ? ESCAPE '\\')"
        for _ in patterns
    )
    params: list[object] = []
    for p in patterns:
        params += [p, p, p]
    return clause, params


def search_snapshots_like(
    conn: sqlite3.Connection,
    patterns: list[str],
    *,
    domain: str | None = None,
    latest_only: bool = False,
    limit: int = 20,
    offset: int = 0,
) -> list[sqlite3.Row]:
    """LIKE 부분일치 검색 (최신순) — trigram 이 못 잡는 1~2글자 쿼리 폴백."""
    clause, params = _like_where(patterns)
    sql = _SEARCH_SELECT + " WHERE " + clause
    if domain:
        sql += " AND p.domain = ?"
        params.append(domain)
    if latest_only:
        sql += _SEARCH_LATEST_CLAUSE
    sql += " ORDER BY s.taken_at DESC, s.id DESC LIMIT ? OFFSET ?"
    params += [limit, offset]
    return conn.execute(sql, params).fetchall()


def count_search_snapshots_like(
    conn: sqlite3.Connection,
    patterns: list[str],
    *,
    domain: str | None = None,
    latest_only: bool = False,
) -> int:
    """LIKE 폴백 결과 총 건수 (페이징용)."""
    clause, params = _like_where(patterns)
    sql = (
        "SELECT COUNT(*) AS c FROM snapshot_fts "
        "JOIN snapshots s ON s.id = snapshot_fts.rowid "
        "JOIN pages p ON p.id = s.page_id WHERE " + clause
    )
    if domain:
        sql += " AND p.domain = ?"
        params.append(domain)
    if latest_only:
        sql += _SEARCH_LATEST_CLAUSE
    return conn.execute(sql, params).fetchone()["c"]


# ---- 함께 저장된 문서 파일 (문서 CAS 참조) ----


def insert_snapshot_documents(
    conn: sqlite3.Connection, snapshot_id: int, manifest: list[dict]
) -> None:
    """스냅샷의 문서 참조 행들 삽입 (manifest: documents.download_documents 형식).

    INSERT OR IGNORE — compact 재실행 등 같은 (snapshot_id, file) 재기록에 멱등.
    """
    conn.executemany(
        """
        INSERT OR IGNORE INTO snapshot_documents
            (snapshot_id, url, file, bytes, sha256, content_type)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (snapshot_id, d["url"], d["file"], d["bytes"], d["sha256"],
             d["content_type"])
            for d in manifest
        ],
    )


def get_snapshot_document(
    conn: sqlite3.Connection, snapshot_id: int, file: str
) -> sqlite3.Row | None:
    """스냅샷의 문서 참조 행을 파일명으로 조회 (없으면 None) — 다운로드 라우트용."""
    return conn.execute(
        "SELECT * FROM snapshot_documents WHERE snapshot_id = ? AND file = ?",
        (snapshot_id, file),
    ).fetchone()


def find_document(
    conn: sqlite3.Connection, sha256: str, file: str
) -> sqlite3.Row | None:
    """(해시, 파일명) 조합이 기록된 문서 참조 행 조회 (없으면 None).

    문서 목록 화면의 다운로드 라우트가 임의 (해시, 이름) 조합으로 CAS 파일을
    노출하지 않도록, 실제 기록된 조합만 허용하는 검증에 쓴다.
    """
    return conn.execute(
        "SELECT * FROM snapshot_documents WHERE sha256 = ? AND file = ? LIMIT 1",
        (sha256, file),
    ).fetchone()


def list_snapshot_document_refs(
    conn: sqlite3.Connection, snapshot_ids: list[int]
) -> list[sqlite3.Row]:
    """해당 스냅샷들이 참조하는 (sha256, file) 목록 (중복 제거) — 삭제 GC 용."""
    if not snapshot_ids:
        return []
    marks = ", ".join("?" for _ in snapshot_ids)
    return conn.execute(
        f"SELECT DISTINCT sha256, file FROM snapshot_documents "
        f"WHERE snapshot_id IN ({marks})",
        snapshot_ids,
    ).fetchall()


def list_document_refs_by_shas(
    conn: sqlite3.Connection, shas: list[str]
) -> list[sqlite3.Row]:
    """해당 해시들을 참조하는 (sha256, file) 목록 — 삭제 후 잔존 참조 확인용."""
    if not shas:
        return []
    marks = ", ".join("?" for _ in shas)
    return conn.execute(
        f"SELECT DISTINCT sha256, file FROM snapshot_documents "
        f"WHERE sha256 IN ({marks})",
        shas,
    ).fetchall()


def list_document_groups(
    conn: sqlite3.Connection, limit: int = 100, offset: int = 0
) -> list[sqlite3.Row]:
    """문서 목록 화면용 — 같은 내용(sha256)을 한 행으로 묶은 목록.

    그룹별 참조 스냅샷/페이지 수와 최근 저장 시각을 집계하고, 표시용
    파일명·출처는 가장 최근 참조 행의 값을 쓴다 (최근 저장 순)."""
    return conn.execute(
        """
        SELECT g.sha256, g.snapshot_count, g.page_count, g.first_seen, g.last_seen,
               d.file, d.url, d.bytes, d.content_type, d.snapshot_id,
               s.page_id, p.url AS page_url, p.site_id, st.site_key
        FROM (
            SELECT d2.sha256 AS sha256, MAX(d2.id) AS doc_id,
                   COUNT(*) AS snapshot_count,
                   COUNT(DISTINCT s2.page_id) AS page_count,
                   MIN(s2.taken_at) AS first_seen, MAX(s2.taken_at) AS last_seen
            FROM snapshot_documents d2 JOIN snapshots s2 ON s2.id = d2.snapshot_id
            GROUP BY d2.sha256
        ) g
        JOIN snapshot_documents d ON d.id = g.doc_id
        JOIN snapshots s ON s.id = d.snapshot_id
        JOIN pages p ON p.id = s.page_id
        LEFT JOIN sites st ON st.id = p.site_id
        ORDER BY g.last_seen DESC, g.sha256
        LIMIT ? OFFSET ?
        """,
        (limit, offset),
    ).fetchall()


def document_totals(conn: sqlite3.Connection) -> sqlite3.Row:
    """문서 목록 화면 요약 — 고유 문서 수(groups)·저장 용량(unique_bytes)·
    중복 제거로 절약된 용량(saved_bytes = 참조 수 - 1 만큼의 중복분)."""
    return conn.execute(
        """
        SELECT COUNT(*) AS groups,
               COALESCE(SUM(bytes), 0) AS unique_bytes,
               COALESCE(SUM(bytes * (refs - 1)), 0) AS saved_bytes
        FROM (
            SELECT MAX(bytes) AS bytes, COUNT(*) AS refs
            FROM snapshot_documents GROUP BY sha256
        )
        """
    ).fetchone()


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
    """전체 스케줄 목록 (+ 페이지 url, 다음 실행이 가까운 순).

    사설 대역 페이지 구분용으로 로컬 네트워크 태그 이름·설명도 붙인다.
    """
    return conn.execute(
        """
        SELECT sc.*, p.url, p.network_tag_id,
               nt.name AS network_tag_name,
               nt.description AS network_tag_description
        FROM schedules sc
        JOIN pages p ON p.id = sc.page_id
        LEFT JOIN network_tags nt ON nt.id = p.network_tag_id
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
    conn: sqlite3.Connection,
    page_id: int,
    interval_seconds: int,
    next_run_at: str,
    run_at_time: str | None = None,
) -> None:
    """페이지 스케줄 등록 (이미 있으면 주기·실행 시각·다음 실행 시각만 교체)."""
    conn.execute(
        """
        INSERT INTO schedules (page_id, interval_seconds, next_run_at, run_at_time, created_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(page_id) DO UPDATE SET
            interval_seconds = excluded.interval_seconds,
            next_run_at = excluded.next_run_at,
            run_at_time = excluded.run_at_time
        """,
        (page_id, interval_seconds, next_run_at, run_at_time, _utcnow()),
    )


def set_schedule_next_run(
    conn: sqlite3.Connection, page_id: int, next_run_at: str
) -> bool:
    """페이지 스케줄의 다음 실행 시각만 변경. 등록이 없었으면 False."""
    cur = conn.execute(
        "UPDATE schedules SET next_run_at = ? WHERE page_id = ?",
        (next_run_at, page_id),
    )
    return cur.rowcount == 1


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


# ---- 사이트 전체 아카이브 (크롤) ----


def insert_crawl(
    conn: sqlite3.Connection,
    *,
    start_url: str,
    scope_host: str,
    scope_path: str,
    max_pages: int,
    max_depth: int,
    delay_seconds: int,
    source: str,
    network_tag_id: str | None = None,
    credential_id: int | None = None,
) -> int:
    """크롤 row 생성 후 id 반환. next_page_at 은 지금 — 즉시 시작 가능.

    소속 사이트(site_id)는 시작 URL 에서 계산해 자동 연결한다.
    """
    now = _utcnow()
    site_id = get_or_create_site(conn, storage.site_key(start_url))
    cur = conn.execute(
        """
        INSERT INTO crawls
            (start_url, scope_host, scope_path, max_pages, max_depth,
             delay_seconds, source, site_id, network_tag_id, credential_id,
             created_at, next_page_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (start_url, scope_host, scope_path, max_pages, max_depth,
         delay_seconds, source, site_id, network_tag_id, credential_id, now, now),
    )
    return cur.lastrowid


def get_crawl(conn: sqlite3.Connection, crawl_id: int) -> sqlite3.Row | None:
    """크롤 row 조회 (없으면 None)."""
    return conn.execute("SELECT * FROM crawls WHERE id = ?", (crawl_id,)).fetchone()


def list_crawls(conn: sqlite3.Connection, limit: int = 100) -> list[sqlite3.Row]:
    """크롤 목록 (최신 순) + 상태별 페이지 수 집계 (목록 화면용)."""
    return conn.execute(
        """
        SELECT c.*,
               COUNT(cp.id) AS total_count,
               COALESCE(SUM(cp.status = 'done'), 0) AS done_count,
               COALESCE(SUM(cp.status = 'failed'), 0) AS failed_count,
               COALESCE(SUM(cp.status IN ('pending', 'in_progress')), 0) AS pending_count
        FROM crawls c LEFT JOIN crawl_pages cp ON cp.crawl_id = c.id
        GROUP BY c.id ORDER BY c.id DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()


def crawl_page_counts(conn: sqlite3.Connection, crawl_id: int) -> dict[str, int]:
    """크롤의 상태별 페이지 수 (집계 키: pending/in_progress/done/failed/total)."""
    counts = {"pending": 0, "in_progress": 0, "done": 0, "failed": 0}
    for row in conn.execute(
        "SELECT status, COUNT(*) AS c FROM crawl_pages WHERE crawl_id = ? GROUP BY status",
        (crawl_id,),
    ):
        counts[row["status"]] = row["c"]
    counts["total"] = sum(counts.values())
    return counts


def list_crawl_pages(
    conn: sqlite3.Connection, crawl_id: int, status: str | None = None
) -> list[sqlite3.Row]:
    """크롤의 페이지 목록 (발견 순) + 스냅샷의 page_id (타임라인 링크용).

    status 를 주면 해당 상태(pending/in_progress/done/failed)만 추린다.
    """
    sql = """
        SELECT cp.*, s.page_id AS snapshot_page_id
        FROM crawl_pages cp LEFT JOIN snapshots s ON s.id = cp.snapshot_id
        WHERE cp.crawl_id = ?
    """
    params: list[object] = [crawl_id]
    if status is not None:
        sql += " AND cp.status = ?"
        params.append(status)
    sql += " ORDER BY cp.id"
    return conn.execute(sql, params).fetchall()


def insert_crawl_page(
    conn: sqlite3.Connection, crawl_id: int, url: str, depth: int
) -> bool:
    """크롤 큐에 URL 추가. 이미 있는 URL 이면 False (UNIQUE 무시)."""
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO crawl_pages (crawl_id, url, depth)
        VALUES (?, ?, ?)
        """,
        (crawl_id, url, depth),
    )
    return cur.rowcount == 1


def count_crawl_pages(conn: sqlite3.Connection, crawl_id: int) -> int:
    """크롤 큐의 전체 URL 수 (max_pages 한도 판정용)."""
    return conn.execute(
        "SELECT COUNT(*) AS c FROM crawl_pages WHERE crawl_id = ?", (crawl_id,)
    ).fetchone()["c"]


def claim_due_crawl_page(
    conn: sqlite3.Connection, now_iso: str, crawl_id: int | None = None
) -> sqlite3.Row | None:
    """기한이 된 크롤에서 대기 페이지 하나를 원자적으로 클레임.

    조건: 크롤이 running 이고 next_page_at(페이지 간 간격)이 지났으며,
    같은 크롤에 in_progress 페이지가 없고(크롤당 동시 처리 1개 — 워커가
    여럿이어도 병렬 단위는 크롤이라 대상 서버 부담은 순차와 같다),
    페이지가 pending 이고 재시도 대기(next_attempt_at)가 끝났을 것.
    클레임과 동시에 크롤의 next_page_at 을 delay 만큼 미뤄 같은 크롤의
    다른 페이지가 간격 안에 잡히지 않게 한다. UPDATE 의 status 조건이
    멀티 프로세스(serve 폴링 + 워커 + CLI) 경합을 막는다 — 경합 시 None.
    """
    sql = """
        SELECT cp.*, c.scope_host, c.scope_path, c.max_pages, c.max_depth,
               c.delay_seconds, c.network_tag_id, c.credential_id
        FROM crawl_pages cp JOIN crawls c ON c.id = cp.crawl_id
        WHERE c.status = 'running' AND c.next_page_at <= ?
          AND cp.status = 'pending'
          AND (cp.next_attempt_at IS NULL OR cp.next_attempt_at <= ?)
          AND NOT EXISTS (
              SELECT 1 FROM crawl_pages busy
              WHERE busy.crawl_id = c.id AND busy.status = 'in_progress'
          )
    """
    params: list[object] = [now_iso, now_iso]
    if crawl_id is not None:
        sql += " AND c.id = ?"
        params.append(crawl_id)
    sql += " ORDER BY cp.crawl_id, cp.depth, cp.id LIMIT 1"
    row = conn.execute(sql, params).fetchone()
    if row is None:
        return None
    cur = conn.execute(
        """
        UPDATE crawl_pages SET status = 'in_progress', claimed_at = ?
        WHERE id = ? AND status = 'pending'
        """,
        (now_iso, row["id"]),
    )
    if cur.rowcount != 1:
        return None
    conn.execute(
        "UPDATE crawls SET next_page_at = ? WHERE id = ?",
        (_later(row["delay_seconds"]), row["crawl_id"]),
    )
    return row


def release_crawl_page(conn: sqlite3.Connection, crawl_page_id: int) -> None:
    """클레임 반납 (in_progress → pending, 시도 수 미증가) — 다음 폴링에서 재시도."""
    conn.execute(
        """
        UPDATE crawl_pages SET status = 'pending', claimed_at = NULL
        WHERE id = ? AND status = 'in_progress'
        """,
        (crawl_page_id,),
    )


def recover_stale_crawl_pages(conn: sqlite3.Connection, cutoff_iso: str) -> int:
    """클레임 후 오래 방치된 in_progress 를 pending 으로 복구 (프로세스 중단 대비)."""
    cur = conn.execute(
        """
        UPDATE crawl_pages SET status = 'pending', claimed_at = NULL
        WHERE status = 'in_progress' AND claimed_at <= ?
        """,
        (cutoff_iso,),
    )
    return cur.rowcount


def finish_crawl_page(
    conn: sqlite3.Connection, crawl_page_id: int, snapshot_id: int | None
) -> None:
    """페이지 처리 성공 기록 — done + 이 크롤에서 확인된 스냅샷 참조."""
    conn.execute(
        """
        UPDATE crawl_pages
        SET status = 'done', snapshot_id = ?, error = NULL,
            next_attempt_at = NULL, claimed_at = NULL
        WHERE id = ?
        """,
        (snapshot_id, crawl_page_id),
    )


def fail_crawl_page(
    conn: sqlite3.Connection,
    crawl_page_id: int,
    *,
    attempts: int,
    error: str,
    next_attempt_at: str | None,
) -> None:
    """페이지 처리 실패 기록 — 재시도 시각이 있으면 pending 으로 돌리고, 없으면 failed."""
    status = "pending" if next_attempt_at else "failed"
    conn.execute(
        """
        UPDATE crawl_pages
        SET status = ?, attempts = ?, error = ?, next_attempt_at = ?, claimed_at = NULL
        WHERE id = ?
        """,
        (status, attempts, error, next_attempt_at, crawl_page_id),
    )


# ---- 단발 아카이빙 작업 큐 (archive_jobs — archive_worker.py 가 소비) ----

def enqueue_archive_job(
    conn: sqlite3.Connection,
    url: str,
    *,
    force: bool = False,
    source: str = "web",
    network_tag_id: str | None = None,
    credential_id: int | None = None,
    interval_seconds: int | None = None,
    run_at: str | None = None,
) -> bool:
    """단발 아카이빙 작업을 큐에 추가. 같은 URL 의 활성 작업이 이미 있으면 무시(False).

    중복 차단은 부분 UNIQUE 인덱스(idx_archive_jobs_active)가 한다 — INSERT OR
    IGNORE 가 활성 중복이면 0행을 넣고 False 를 반환한다 (현재 _register_job 의
    중복-방지 역할 대체).
    """
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO archive_jobs
            (url, force, source, network_tag_id, credential_id,
             interval_seconds, run_at, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
        """,
        (url, 1 if force else 0, source, network_tag_id, credential_id,
         interval_seconds, run_at, _utcnow()),
    )
    return cur.rowcount == 1


def claim_due_archive_job(
    conn: sqlite3.Connection, now_iso: str
) -> sqlite3.Row | None:
    """기한이 된 대기 작업 하나를 원자적으로 클레임 (in_progress). 없으면 None.

    조건부 UPDATE 의 status='pending' 가 멀티 프로세스(serve 폴링 + worker +
    CLI) 경합을 막는다 — 경합 시 rowcount!=1 → None.
    """
    row = conn.execute(
        """
        SELECT * FROM archive_jobs
        WHERE status = 'pending'
          AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
        ORDER BY id LIMIT 1
        """,
        (now_iso,),
    ).fetchone()
    if row is None:
        return None
    cur = conn.execute(
        """
        UPDATE archive_jobs SET status = 'in_progress', claimed_at = ?
        WHERE id = ? AND status = 'pending'
        """,
        (now_iso, row["id"]),
    )
    if cur.rowcount != 1:
        return None
    return row


def release_archive_job(conn: sqlite3.Connection, job_id: int) -> None:
    """클레임 반납 (in_progress → pending, 시도 수 미증가) — 다음 폴링에서 재시도."""
    conn.execute(
        """
        UPDATE archive_jobs SET status = 'pending', claimed_at = NULL
        WHERE id = ? AND status = 'in_progress'
        """,
        (job_id,),
    )


def recover_stale_archive_jobs(conn: sqlite3.Connection, cutoff_iso: str) -> int:
    """클레임 후 오래 방치된 in_progress 를 pending 으로 복구 (프로세스 중단 대비)."""
    cur = conn.execute(
        """
        UPDATE archive_jobs SET status = 'pending', claimed_at = NULL
        WHERE status = 'in_progress' AND claimed_at <= ?
        """,
        (cutoff_iso,),
    )
    return cur.rowcount


def finish_archive_job(conn: sqlite3.Connection, job_id: int) -> None:
    """작업 성공 — 큐에서 삭제한다. 결과는 archive_logs 가 보존한다."""
    conn.execute("DELETE FROM archive_jobs WHERE id = ?", (job_id,))


def fail_archive_job(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    attempts: int,
    error: str,
    next_attempt_at: str | None,
) -> None:
    """작업 실패 — 재시도 시각이 있으면 pending 으로 되돌리고, 없으면(최종 실패)
    큐에서 삭제한다. 최종 실패 사유는 pipeline 이 archive_logs(error)에 남겼다."""
    if next_attempt_at:
        conn.execute(
            """
            UPDATE archive_jobs
            SET status = 'pending', attempts = ?, error = ?,
                next_attempt_at = ?, claimed_at = NULL
            WHERE id = ?
            """,
            (attempts, error, next_attempt_at, job_id),
        )
    else:
        conn.execute("DELETE FROM archive_jobs WHERE id = ?", (job_id,))


def list_active_archive_jobs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """활성(pending|in_progress) 작업의 url·시각 — 진행 표시(_active_snapshot)용."""
    return conn.execute(
        """
        SELECT url, COALESCE(claimed_at, created_at) AS activity_at
        FROM archive_jobs WHERE status IN ('pending', 'in_progress')
        """
    ).fetchall()


def finish_crawl_if_done(conn: sqlite3.Connection, crawl_id: int) -> bool:
    """처리할 페이지가 안 남았으면 크롤을 done 으로 마감. 마감했으면 True."""
    cur = conn.execute(
        """
        UPDATE crawls SET status = 'done', finished_at = ?
        WHERE id = ? AND status = 'running' AND NOT EXISTS (
            SELECT 1 FROM crawl_pages
            WHERE crawl_id = ? AND status IN ('pending', 'in_progress')
        )
        """,
        (_utcnow(), crawl_id, crawl_id),
    )
    return cur.rowcount == 1


def cancel_crawl(conn: sqlite3.Connection, crawl_id: int) -> bool:
    """진행 중 크롤 취소. running 이 아니었으면 False. 처리 중 페이지는
    현재 건만 마치고 멈춘다 (클레임 시 status='running' 조건)."""
    cur = conn.execute(
        """
        UPDATE crawls SET status = 'cancelled', finished_at = ?
        WHERE id = ? AND status = 'running'
        """,
        (_utcnow(), crawl_id),
    )
    return cur.rowcount == 1


def retry_failed_crawl_pages(conn: sqlite3.Connection, crawl_id: int) -> int:
    """failed 페이지를 일괄 재시도 대상으로 되돌리고, 크롤을 다시 연다.

    cancelled 크롤이면 남은 pending 도 함께 재개된다. 반환은 되돌린 페이지 수.
    """
    cur = conn.execute(
        """
        UPDATE crawl_pages
        SET status = 'pending', attempts = 0, next_attempt_at = NULL, error = NULL
        WHERE crawl_id = ? AND status = 'failed'
        """,
        (crawl_id,),
    )
    conn.execute(
        """
        UPDATE crawls SET status = 'running', finished_at = NULL, next_page_at = ?
        WHERE id = ? AND status IN ('done', 'cancelled') AND EXISTS (
            SELECT 1 FROM crawl_pages
            WHERE crawl_id = ? AND status IN ('pending', 'in_progress')
        )
        """,
        (_utcnow(), crawl_id, crawl_id),
    )
    return cur.rowcount


def get_failed_crawl_page(
    conn: sqlite3.Connection, crawl_id: int, crawl_page_id: int
) -> sqlite3.Row | None:
    """크롤 소속 실패 페이지 행 조회 — 단건 재시도 검증용.

    소속이 아니거나 실패 상태가 아니면 None.
    """
    return conn.execute(
        "SELECT * FROM crawl_pages WHERE id = ? AND crawl_id = ? AND status = 'failed'",
        (crawl_page_id, crawl_id),
    ).fetchone()


def resolve_failed_crawl_pages(
    conn: sqlite3.Connection, url: str, snapshot_id: int | None
) -> list[int]:
    """같은 URL 의 failed 크롤 페이지를 다른 경로의 아카이빙 성공으로 done 처리.

    수동 단일 아카이빙·스케줄이 같은 주소를 성공적으로 아카이빙했다면
    크롤의 실패 기록은 더 이상 유효하지 않다 — done 으로 돌리고 이번에
    확인된 스냅샷을 연결한다. 반환은 갱신된 행들의 crawl_id 목록
    (중복 제거 — 호출 쪽이 finish_crawl_if_done 으로 마감을 재평가).
    """
    rows = conn.execute(
        "SELECT id, crawl_id FROM crawl_pages WHERE url = ? AND status = 'failed'",
        (url,),
    ).fetchall()
    for row in rows:
        finish_crawl_page(conn, row["id"], snapshot_id)
    return sorted({row["crawl_id"] for row in rows})


def find_crawl_snapshot(
    conn: sqlite3.Connection, crawl_id: int, url: str
) -> int | None:
    """크롤 세트 안에서 URL 의 스냅샷 id 조회 (없으면 None) — 링크 리졸버용."""
    row = conn.execute(
        """
        SELECT snapshot_id FROM crawl_pages
        WHERE crawl_id = ? AND url = ? AND snapshot_id IS NOT NULL
        """,
        (crawl_id, url),
    ).fetchone()
    return row["snapshot_id"] if row else None


def find_running_crawl(conn: sqlite3.Connection, start_url: str) -> sqlite3.Row | None:
    """같은 시작 URL 의 진행 중(running) 크롤 조회 (없으면 None).

    start_crawl 의 자동 병합(진행 중이면 새 크롤을 만들지 않음)과 크롤
    스케줄의 미루기(이전 실행이 끝나기 전에 쌓지 않음)에 쓴다.
    """
    return conn.execute(
        "SELECT * FROM crawls WHERE start_url = ? AND status = 'running' "
        "ORDER BY id DESC LIMIT 1",
        (start_url,),
    ).fetchone()


def list_site_pages(
    conn: sqlite3.Connection,
    site_id: int,
    *,
    limit: int | None = None,
    offset: int = 0,
) -> list[sqlite3.Row]:
    """사이트 소속 페이지 목록 + 스냅샷 수·마지막 캡처 시각 (사이트 상세/삭제용).

    limit 을 주면 offset 부터 그만큼만 반환한다 (사이트 상세 페이징용).
    사설 대역 페이지 구분용으로 로컬 네트워크 태그 이름·설명도 붙인다.
    """
    sql = """
        SELECT p.*, nt.name AS network_tag_name,
               nt.description AS network_tag_description,
               COUNT(s.id) AS snapshot_count, MAX(s.taken_at) AS last_taken_at
        FROM pages p
        LEFT JOIN snapshots s ON s.page_id = p.id
        LEFT JOIN network_tags nt ON nt.id = p.network_tag_id
        WHERE p.site_id = ?
        GROUP BY p.id
        ORDER BY last_taken_at DESC NULLS LAST, p.url
        """
    params: list[object] = [site_id]
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params += [limit, offset]
    return conn.execute(sql, params).fetchall()


def site_page_totals(conn: sqlite3.Connection, site_id: int) -> sqlite3.Row:
    """사이트 소속 페이지·스냅샷 총수 (사이트 상세 헤더·페이징용)."""
    return conn.execute(
        """
        SELECT COUNT(DISTINCT p.id) AS page_count, COUNT(s.id) AS snapshot_count
        FROM pages p LEFT JOIN snapshots s ON s.page_id = p.id
        WHERE p.site_id = ?
        """,
        (site_id,),
    ).fetchone()


def list_site_snapshot_dirs(
    conn: sqlite3.Connection, site_id: int
) -> list[sqlite3.Row]:
    """사이트 소속 스냅샷의 시각·디렉토리 위치 (page_id, taken_at, domain, slug, dir_name).

    사이트 상세가 페이지별·사이트 전체 저장 용량 합산과 최신 스냅샷의
    타이틀 조회에 쓴다.
    """
    return conn.execute(
        """
        SELECT s.page_id, s.taken_at, p.domain, p.slug, s.dir_name
        FROM snapshots s JOIN pages p ON p.id = s.page_id
        WHERE p.site_id = ?
        """,
        (site_id,),
    ).fetchall()


def list_site_failed_logs(
    conn: sqlite3.Connection, site_id: int
) -> list[sqlite3.Row]:
    """사이트 소속 페이지 중 최근 실행이 실패인 로그 목록 (+ 페이지 url, 최신 순).

    URL 별 최신 archive_logs 행이 status='error' 인 것만 — 이후 실행이
    성공하면 최신 행이 바뀌어 목록에서 자연히 사라진다. 페이지 행이 생기기
    전에 실패한 신규 URL(page_id NULL)은 소속 사이트를 알 수 없어 포함하지
    않는다 (크롤 중 실패한 신규 URL 은 크롤 진행 화면이 보여준다).
    """
    return conn.execute(
        """
        SELECT al.*, p.url AS page_url
        FROM archive_logs al
        JOIN pages p ON p.id = al.page_id
        JOIN (
            SELECT al2.url AS url, MAX(al2.id) AS max_id
            FROM archive_logs al2
            JOIN pages p2 ON p2.id = al2.page_id
            WHERE p2.site_id = ?
            GROUP BY al2.url
        ) last ON al.id = last.max_id
        WHERE al.status = 'error'
        ORDER BY al.started_at DESC, al.id DESC
        """,
        (site_id,),
    ).fetchall()


def get_site_failed_log(
    conn: sqlite3.Connection, site_id: int, log_id: int
) -> sqlite3.Row | None:
    """사이트 소속 실패 로그 행 조회 (+ 페이지 url) — 재시도 검증용.

    소속이 아니거나 실패 로그가 아니면 None.
    """
    return conn.execute(
        """
        SELECT al.*, p.url AS page_url
        FROM archive_logs al JOIN pages p ON p.id = al.page_id
        WHERE al.id = ? AND p.site_id = ? AND al.status = 'error'
        """,
        (log_id, site_id),
    ).fetchone()


def list_site_failed_crawl_pages(
    conn: sqlite3.Connection, site_id: int
) -> list[sqlite3.Row]:
    """사이트 소속 크롤에서 마지막 시도가 실패인 크롤 페이지 목록 (URL 별 최신 행).

    URL 별 최신 crawl_pages 행이 failed 인 것만 — 이후 크롤에서 성공하면
    최신 행이 바뀌어 목록에서 자연히 사라진다. 크롤 실패 후 직접 아카이빙이
    성공한 URL(최신 archive_logs 가 성공)도 제외한다. 페이지 행이 생기기 전에
    실패한 신규 URL 을 사이트 상세의 실패 목록에 보여주는 용도 —
    list_site_failed_logs 가 못 다루는 빈틈을 메운다. failed_at 은 그 URL 의
    최신 아카이브 로그 시각 (크롤 페이지 행에는 실패 시각이 없다, 없으면 NULL).
    """
    return conn.execute(
        """
        SELECT cp.*, c.start_url,
               (SELECT al.started_at FROM archive_logs al
                WHERE al.url = cp.url ORDER BY al.id DESC LIMIT 1) AS failed_at
        FROM crawl_pages cp
        JOIN crawls c ON c.id = cp.crawl_id
        JOIN (
            SELECT cp2.url AS url, MAX(cp2.id) AS max_id
            FROM crawl_pages cp2
            JOIN crawls c2 ON c2.id = cp2.crawl_id
            WHERE c2.site_id = ?
            GROUP BY cp2.url
        ) last ON cp.id = last.max_id
        WHERE cp.status = 'failed'
          AND COALESCE((
              SELECT al.status FROM archive_logs al
              WHERE al.url = cp.url ORDER BY al.id DESC LIMIT 1
          ), 'error') = 'error'
        ORDER BY failed_at DESC NULLS LAST, cp.id DESC
        """,
        (site_id,),
    ).fetchall()


def get_site_failed_crawl_page(
    conn: sqlite3.Connection, site_id: int, crawl_page_id: int
) -> sqlite3.Row | None:
    """사이트 소속 실패 크롤 페이지 행 조회 — 재시도 검증용.

    소속이 아니거나 실패 상태가 아니면 None.
    """
    return conn.execute(
        """
        SELECT cp.* FROM crawl_pages cp JOIN crawls c ON c.id = cp.crawl_id
        WHERE cp.id = ? AND c.site_id = ? AND cp.status = 'failed'
        """,
        (crawl_page_id, site_id),
    ).fetchone()


def retry_failed_crawl_page(conn: sqlite3.Connection, crawl_page_id: int) -> None:
    """실패 크롤 페이지 하나를 재시도 대상으로 되돌리고, 끝난 크롤이면 다시 연다.

    retry_failed_crawl_pages(일괄)의 단건 버전 — 크롤러가 큐에서 다시 집어간다.
    """
    conn.execute(
        """
        UPDATE crawl_pages
        SET status = 'pending', attempts = 0, next_attempt_at = NULL, error = NULL
        WHERE id = ? AND status = 'failed'
        """,
        (crawl_page_id,),
    )
    conn.execute(
        """
        UPDATE crawls SET status = 'running', finished_at = NULL, next_page_at = ?
        WHERE id = (SELECT crawl_id FROM crawl_pages WHERE id = ?)
          AND status IN ('done', 'cancelled')
        """,
        (_utcnow(), crawl_page_id),
    )


def list_site_crawls(conn: sqlite3.Connection, site_id: int) -> list[sqlite3.Row]:
    """사이트 소속 크롤 회차 목록 (최신 순) + 상태별 페이지 수 집계.

    사설 대역 크롤 구분용으로 로컬 네트워크 태그 이름·설명도 붙인다.
    """
    return conn.execute(
        """
        SELECT c.*, nt.name AS network_tag_name,
               nt.description AS network_tag_description,
               COUNT(cp.id) AS total_count,
               COALESCE(SUM(cp.status = 'done'), 0) AS done_count,
               COALESCE(SUM(cp.status = 'failed'), 0) AS failed_count,
               COALESCE(SUM(cp.status IN ('pending', 'in_progress')), 0) AS pending_count
        FROM crawls c
        LEFT JOIN crawl_pages cp ON cp.crawl_id = c.id
        LEFT JOIN network_tags nt ON nt.id = c.network_tag_id
        WHERE c.site_id = ?
        GROUP BY c.id ORDER BY c.id DESC
        """,
        (site_id,),
    ).fetchall()


def list_site_schedules(conn: sqlite3.Connection, site_id: int) -> list[sqlite3.Row]:
    """사이트 소속 페이지들의 재아카이빙 스케줄 (+ 페이지 url) — 사이트 상세용."""
    return conn.execute(
        """
        SELECT sc.*, p.url FROM schedules sc JOIN pages p ON p.id = sc.page_id
        WHERE p.site_id = ? ORDER BY sc.next_run_at, sc.id
        """,
        (site_id,),
    ).fetchall()


def list_site_crawl_schedules(
    conn: sqlite3.Connection, site_id: int
) -> list[sqlite3.Row]:
    """사이트 소속 크롤 스케줄 목록 — 사이트 상세용."""
    return conn.execute(
        "SELECT * FROM crawl_schedules WHERE site_id = ? ORDER BY next_run_at, id",
        (site_id,),
    ).fetchall()


def delete_site_crawls(conn: sqlite3.Connection, site_id: int) -> int:
    """사이트 소속 크롤 회차와 페이지 큐를 일괄 삭제. 반환은 삭제된 크롤 수.

    사이트 단위 삭제 전용 — 크롤 회차의 개별 삭제 경로는 없다.
    """
    conn.execute(
        """
        DELETE FROM crawl_pages
        WHERE crawl_id IN (SELECT id FROM crawls WHERE site_id = ?)
        """,
        (site_id,),
    )
    cur = conn.execute("DELETE FROM crawls WHERE site_id = ?", (site_id,))
    return cur.rowcount


def delete_site_crawl_schedules(conn: sqlite3.Connection, site_id: int) -> int:
    """사이트 소속 크롤 스케줄 일괄 삭제. 반환은 삭제된 스케줄 수."""
    cur = conn.execute(
        "DELETE FROM crawl_schedules WHERE site_id = ?", (site_id,)
    )
    return cur.rowcount


# ---- 사이트 아카이브 스케줄 (주기적 재크롤) ----


def get_crawl_schedule(conn: sqlite3.Connection, start_url: str) -> sqlite3.Row | None:
    """시작 URL 의 크롤 스케줄 row 조회 (없으면 None)."""
    return conn.execute(
        "SELECT * FROM crawl_schedules WHERE start_url = ?", (start_url,)
    ).fetchone()


def get_crawl_schedule_by_id(
    conn: sqlite3.Connection, schedule_id: int
) -> sqlite3.Row | None:
    """id 로 크롤 스케줄 row 조회 (없으면 None)."""
    return conn.execute(
        "SELECT * FROM crawl_schedules WHERE id = ?", (schedule_id,)
    ).fetchone()


def list_crawl_schedules(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """전체 크롤 스케줄 목록 (다음 실행이 가까운 순).

    사설 대역 크롤 구분용으로 로컬 네트워크 태그 이름·설명도 붙인다.
    """
    return conn.execute(
        """
        SELECT cs.*, nt.name AS network_tag_name,
               nt.description AS network_tag_description
        FROM crawl_schedules cs
        LEFT JOIN network_tags nt ON nt.id = cs.network_tag_id
        ORDER BY cs.next_run_at, cs.id
        """
    ).fetchall()


def list_due_crawl_schedules(
    conn: sqlite3.Connection, now_iso: str
) -> list[sqlite3.Row]:
    """다음 실행 시각이 지난 크롤 스케줄 목록 (ISO 8601 UTC 문자열 비교)."""
    return conn.execute(
        "SELECT * FROM crawl_schedules WHERE next_run_at <= ? ORDER BY next_run_at, id",
        (now_iso,),
    ).fetchall()


def upsert_crawl_schedule(
    conn: sqlite3.Connection,
    start_url: str,
    *,
    max_pages: int,
    max_depth: int,
    delay_seconds: int,
    interval_seconds: int,
    next_run_at: str,
    run_at_time: str | None = None,
    network_tag_id: str | None = None,
    credential_id: int | None = None,
) -> None:
    """시작 URL 의 크롤 스케줄 등록 (이미 있으면 옵션·주기·다음 실행 시각 교체).

    소속 사이트(site_id)는 시작 URL 에서 계산해 자동 연결한다.
    """
    site_id = get_or_create_site(conn, storage.site_key(start_url))
    conn.execute(
        """
        INSERT INTO crawl_schedules
            (start_url, max_pages, max_depth, delay_seconds, interval_seconds,
             next_run_at, run_at_time, site_id, network_tag_id, credential_id,
             created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(start_url) DO UPDATE SET
            max_pages = excluded.max_pages,
            max_depth = excluded.max_depth,
            delay_seconds = excluded.delay_seconds,
            interval_seconds = excluded.interval_seconds,
            next_run_at = excluded.next_run_at,
            run_at_time = excluded.run_at_time,
            site_id = excluded.site_id,
            network_tag_id = excluded.network_tag_id,
            credential_id = excluded.credential_id
        """,
        (start_url, max_pages, max_depth, delay_seconds, interval_seconds,
         next_run_at, run_at_time, site_id, network_tag_id, credential_id, _utcnow()),
    )


def set_crawl_schedule_next_run(
    conn: sqlite3.Connection, schedule_id: int, next_run_at: str
) -> bool:
    """크롤 스케줄의 다음 실행 시각만 변경. 등록이 없었으면 False."""
    cur = conn.execute(
        "UPDATE crawl_schedules SET next_run_at = ? WHERE id = ?",
        (next_run_at, schedule_id),
    )
    return cur.rowcount == 1


def delete_crawl_schedule(conn: sqlite3.Connection, schedule_id: int) -> bool:
    """크롤 스케줄 해제. 등록이 없었으면 False.

    사이트의 마지막 소속 행이었다면 사이트 행도 함께 삭제된다.
    """
    sched = get_crawl_schedule_by_id(conn, schedule_id)
    if sched is None:
        return False
    conn.execute("DELETE FROM crawl_schedules WHERE id = ?", (schedule_id,))
    prune_site_if_empty(conn, sched["site_id"])
    return True


def claim_crawl_schedule(
    conn: sqlite3.Connection,
    schedule_id: int,
    observed_next_run_at: str,
    *,
    last_run_at: str,
    next_run_at: str,
) -> bool:
    """크롤 스케줄 실행 클레임 — next_run_at 이 관측값일 때만 갱신 (원자적).

    serve 폴링 스레드와 cron(`wccg schedule run`)이 같은 기한을 동시에
    봐도 한쪽만 True 를 받아 크롤을 등록한다.
    """
    cur = conn.execute(
        """
        UPDATE crawl_schedules SET last_run_at = ?, next_run_at = ?
        WHERE id = ? AND next_run_at = ?
        """,
        (last_run_at, next_run_at, schedule_id, observed_next_run_at),
    )
    return cur.rowcount == 1


# ---- 사용자 ----
# 주의: SCHEMA 는 CREATE IF NOT EXISTS 라 새 테이블 추가는 자동이지만
# 기존 테이블에 컬럼을 추가하는 변경은 별도 마이그레이션이 필요하다.

# 권한 역할. admin=관리자, archiver=아카이빙 가능, viewer=보기만,
# pending=권한없음(가입 승인 대기 — 안내 페이지 외 접근 불가), blocked=차단,
# withdrawn=탈퇴(본인 탈퇴로만 진입 — 로그인 거부, 관리자가 계정 정보를
# 삭제해야 같은 이메일로 다시 가입/초대할 수 있다)
ROLES = ("admin", "archiver", "viewer", "pending", "blocked", "withdrawn")
# 관리자가 부여할 수 있는 역할 — 탈퇴는 본인 탈퇴로만 진입한다
ASSIGNABLE_ROLES = ("admin", "archiver", "viewer", "pending", "blocked")
ROLE_LABELS = {
    "admin": "관리자",
    "archiver": "아카이브",
    "viewer": "보기 전용",
    "pending": "권한없음",
    "blocked": "차단됨",
    "withdrawn": "탈퇴",
}


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
    role: str = "viewer",
) -> int:
    """사용자 생성 후 id 반환. password_hash=None 이면 SSO 전용 계정.

    role 기본값은 보기 전용(viewer)이지만, 회원 가입·SSO 자동 생성 경로는
    signup_default_role() 설정값(기본 pending)을 명시적으로 넘긴다.
    """
    if role not in ROLES:
        raise ValueError(f"알 수 없는 역할: {role!r}")
    cur = conn.execute(
        "INSERT INTO users (email, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
        (email, password_hash, role, _utcnow()),
    )
    return cur.lastrowid


def count_users(conn: sqlite3.Connection) -> int:
    """전체 사용자 수 (0 이면 최초 구동으로 판단)."""
    return conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]


def create_first_admin(
    conn: sqlite3.Connection, email: str, password_hash: str
) -> int | None:
    """users 가 비어 있을 때만 최초 관리자를 생성 (원자적). 이미 사용자가 있으면 None.

    최초 구동 등록 API 가 관리자 등록 후 재사용되는 것을 INSERT 단계에서 차단한다.
    is_founder=1 — 이 계정의 권한은 이후 변경할 수 없다.
    """
    cur = conn.execute(
        """
        INSERT INTO users (email, password_hash, role, is_founder, created_at)
        SELECT ?, ?, 'admin', 1, ? WHERE NOT EXISTS (SELECT 1 FROM users)
        """,
        (email, password_hash, _utcnow()),
    )
    return cur.lastrowid if cur.rowcount == 1 else None


def list_users(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """전체 사용자 목록 (등록 순) + 2FA/OIDC/활성 세션 집계 (사용자 관리 화면용)."""
    return conn.execute(
        """
        SELECT u.*,
            (SELECT COUNT(*) FROM webauthn_credentials w WHERE w.user_id = u.id)
                AS passkey_count,
            (SELECT COUNT(*) FROM identities i WHERE i.user_id = u.id)
                AS identity_count,
            (SELECT COUNT(*) FROM sessions s
              WHERE s.user_id = u.id AND s.expires_at > ?) AS session_count
        FROM users u ORDER BY u.created_at, u.id
        """,
        (_utcnow(),),
    ).fetchall()


def set_role(conn: sqlite3.Connection, user_id: int, role: str) -> bool:
    """사용자 권한 변경. 최초 관리자(is_founder)는 변경 불가 — False 반환."""
    if role not in ROLES:
        raise ValueError(f"알 수 없는 역할: {role!r}")
    cur = conn.execute(
        "UPDATE users SET role = ? WHERE id = ? AND is_founder = 0", (role, user_id)
    )
    return cur.rowcount == 1


def delete_user_sessions(conn: sqlite3.Connection, user_id: int) -> None:
    """해당 사용자의 세션 전체 삭제 (차단 시 즉시 로그아웃)."""
    conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def set_display_name(conn: sqlite3.Connection, user_id: int, name: str | None) -> None:
    """표시용 사용자 이름 변경 (None 이면 제거 — 이메일로 표시)."""
    conn.execute("UPDATE users SET display_name = ? WHERE id = ?", (name, user_id))


def set_user_timezone(conn: sqlite3.Connection, user_id: int, tz_name: str) -> None:
    """사용자 타임존 변경 (IANA 이름, 예: Asia/Seoul)."""
    conn.execute("UPDATE users SET timezone = ? WHERE id = ?", (tz_name, user_id))


def set_user_locale(conn: sqlite3.Connection, user_id: int, locale: str) -> None:
    """사용자 표시 언어 변경 (예: ko, en)."""
    conn.execute("UPDATE users SET locale = ? WHERE id = ?", (locale, user_id))


def set_password_hash(conn: sqlite3.Connection, user_id: int, password_hash: str) -> None:
    """패스워드 해시 교체 (패스워드 변경)."""
    conn.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id)
    )


def withdraw_user(conn: sqlite3.Connection, user_id: int) -> None:
    """계정 탈퇴 — 권한을 탈퇴 상태로 바꾸고 모든 세션을 무효화.

    계정 정보는 남는다(이메일 UNIQUE 유지 — 재가입 불가). 관리자가
    delete_user 로 계정 정보를 삭제해야 같은 이메일로 다시 가입/초대할 수 있다.
    """
    conn.execute(
        "UPDATE users SET role = 'withdrawn' WHERE id = ? AND is_founder = 0",
        (user_id,),
    )
    conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def delete_user(conn: sqlite3.Connection, user_id: int) -> None:
    """사용자와 종속 데이터(세션·OIDC 연결·패스키)를 일괄 삭제 (관리자의 계정 정보 삭제)."""
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


# ---- 초대 ----

# 초대로 부여할 수 있는 권한 (차단 계정을 초대하는 것은 의미가 없다)
INVITABLE_ROLES = ("admin", "archiver", "viewer")


def create_invite(
    conn: sqlite3.Connection,
    email: str,
    token_hash: str,
    role: str,
    invited_by: int | None,
    ttl_seconds: int,
) -> int:
    """초대 생성 후 id 반환. 같은 이메일의 기존 초대는 교체된다 (이전 링크 무효화)."""
    if role not in INVITABLE_ROLES:
        raise ValueError(f"초대할 수 없는 역할: {role!r}")
    conn.execute("DELETE FROM invites WHERE email = ?", (email,))
    cur = conn.execute(
        """
        INSERT INTO invites (email, token_hash, role, invited_by, created_at, expires_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (email, token_hash, role, invited_by, _utcnow(), _later(ttl_seconds)),
    )
    return cur.lastrowid


def get_invite_by_token(conn: sqlite3.Connection, token_hash: str) -> sqlite3.Row | None:
    """만료되지 않은 초대를 토큰 해시로 조회 (없거나 만료면 None)."""
    return conn.execute(
        "SELECT * FROM invites WHERE token_hash = ? AND expires_at > ?",
        (token_hash, _utcnow()),
    ).fetchone()


def list_invites(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """만료되지 않은 초대 목록 (+ 초대한 사용자 이메일, 최신 순)."""
    return conn.execute(
        """
        SELECT i.*, u.email AS inviter_email
        FROM invites i LEFT JOIN users u ON u.id = i.invited_by
        WHERE i.expires_at > ?
        ORDER BY i.created_at DESC, i.id DESC
        """,
        (_utcnow(),),
    ).fetchall()


def delete_invite(conn: sqlite3.Connection, invite_id: int) -> bool:
    """초대 취소 (가입 완료 시에도 호출 — 1회용). 없으면 False."""
    cur = conn.execute("DELETE FROM invites WHERE id = ?", (invite_id,))
    return cur.rowcount == 1


def delete_expired_invites(conn: sqlite3.Connection) -> None:
    """만료 초대 일괄 삭제 (기회적 정리용)."""
    conn.execute("DELETE FROM invites WHERE expires_at <= ?", (_utcnow(),))


# ---- 로컬 네트워크 태그 ----
# 사설 IP 대역(로컬 네트워크)을 구분하는 태그. 사설 대역 URL 은 태그를
# 지정해야 아카이빙할 수 있다 (게이트는 pipeline·crawler — netcheck 참조).
# id 는 GUID(uuid4) — 이름을 바꿔도 페이지·크롤의 참조가 유지된다.


def list_network_tags(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """로컬 네트워크 태그 목록 (이름순) + 참조 수(페이지·크롤·크롤 스케줄)."""
    return conn.execute(
        """
        SELECT nt.*,
               (SELECT COUNT(*) FROM pages p WHERE p.network_tag_id = nt.id)
                   AS page_count,
               (SELECT COUNT(*) FROM crawls c WHERE c.network_tag_id = nt.id)
                 + (SELECT COUNT(*) FROM crawl_schedules cs
                    WHERE cs.network_tag_id = nt.id) AS crawl_count
        FROM network_tags nt ORDER BY nt.name
        """
    ).fetchall()


def list_site_network_tags(
    conn: sqlite3.Connection, site_id: int | None = None
) -> list[sqlite3.Row]:
    """사이트별로 소속 페이지·크롤이 참조하는 로컬 네트워크 태그 목록.

    사이트 자체에는 태그 컬럼이 없다 — 같은 IP 대역의 다른 사설 네트워크를
    아카이브 목록·사이트 상세에서 구분할 수 있도록, 참조 중인 태그를 모아
    사이트 행에 뱃지로 보여주기 위한 집계다 (site_id, 태그 id·이름·설명).
    site_id 를 주면 해당 사이트 것만 반환한다.
    """
    sql = """
        SELECT refs.site_id, nt.id, nt.name, nt.description
        FROM (
            SELECT DISTINCT site_id, network_tag_id FROM pages
             WHERE network_tag_id IS NOT NULL AND site_id IS NOT NULL
            UNION
            SELECT DISTINCT site_id, network_tag_id FROM crawls
             WHERE network_tag_id IS NOT NULL AND site_id IS NOT NULL
        ) refs JOIN network_tags nt ON nt.id = refs.network_tag_id
        """
    params: list[object] = []
    if site_id is not None:
        sql += " WHERE refs.site_id = ?"
        params.append(site_id)
    sql += " ORDER BY refs.site_id, nt.name"
    return conn.execute(sql, params).fetchall()


def get_network_tag(conn: sqlite3.Connection, tag_id: str) -> sqlite3.Row | None:
    """태그 id(GUID)로 조회 (없으면 None)."""
    return conn.execute(
        "SELECT * FROM network_tags WHERE id = ?", (tag_id,)
    ).fetchone()


def get_network_tag_by_name(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    """태그 이름으로 조회 (없으면 None) — 중복 이름 사전 검사용."""
    return conn.execute(
        "SELECT * FROM network_tags WHERE name = ?", (name,)
    ).fetchone()


def create_network_tag(
    conn: sqlite3.Connection, name: str, description: str = ""
) -> sqlite3.Row:
    """로컬 네트워크 태그 생성 (id 는 GUID 자동 발급) 후 row 반환.

    이름 중복은 UNIQUE 제약으로 IntegrityError — 호출부가 사전 검사한다.
    """
    tag_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO network_tags (id, name, description, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (tag_id, name, description, _utcnow()),
    )
    return get_network_tag(conn, tag_id)


def count_network_tag_refs(conn: sqlite3.Connection, tag_id: str) -> int:
    """태그를 참조하는 행 수 (pages + crawls + crawl_schedules) — 삭제 가드용."""
    return conn.execute(
        """
        SELECT (SELECT COUNT(*) FROM pages WHERE network_tag_id = ?)
             + (SELECT COUNT(*) FROM crawls WHERE network_tag_id = ?)
             + (SELECT COUNT(*) FROM crawl_schedules WHERE network_tag_id = ?) AS c
        """,
        (tag_id, tag_id, tag_id),
    ).fetchone()["c"]


def delete_network_tag(conn: sqlite3.Connection, tag_id: str) -> bool:
    """태그 삭제. 없었으면 False. 참조 중 삭제는 호출부가 막는다 (count_network_tag_refs)."""
    cur = conn.execute("DELETE FROM network_tags WHERE id = ?", (tag_id,))
    return cur.rowcount > 0


def network_tag_site_ids(conn: sqlite3.Connection, tag_id: str) -> set[int]:
    """태그를 참조하는 pages·crawls·crawl_schedules 의 site_id 집합 (NULL 제외).

    storage.site_key 가 호스트+포트라 같은 IP:포트 = 같은 site_id 다. 두 태그의
    이 집합을 비교하면 '같은 사설 네트워크'를 가리키는지 판정할 수 있다 (병합 가드용).
    """
    rows = conn.execute(
        """
        SELECT site_id FROM pages
         WHERE network_tag_id = ? AND site_id IS NOT NULL
        UNION
        SELECT site_id FROM crawls
         WHERE network_tag_id = ? AND site_id IS NOT NULL
        UNION
        SELECT site_id FROM crawl_schedules
         WHERE network_tag_id = ? AND site_id IS NOT NULL
        """,
        (tag_id, tag_id, tag_id),
    ).fetchall()
    return {row["site_id"] for row in rows}


def merge_network_tags(
    conn: sqlite3.Connection, source_id: str, target_id: str
) -> dict[str, int]:
    """source 태그의 모든 참조를 target 으로 옮긴 뒤 source 태그를 삭제한다.

    pages·crawls·crawl_schedules 의 network_tag_id 를 일괄 갱신하고(테이블별
    이전 행 수를 dict 로 반환) source 를 지운다. 검증(같은 사이트·미존재·동일
    태그)은 호출부가 한다 — 여기선 단순 이전이다. UPDATE 가 모두 끝난 뒤
    DELETE 하므로 FK(foreign_keys=ON) 위반이 없다.
    """
    moved: dict[str, int] = {}
    for table in ("pages", "crawls", "crawl_schedules"):
        cur = conn.execute(
            f"UPDATE {table} SET network_tag_id = ? WHERE network_tag_id = ?",
            (target_id, source_id),
        )
        moved[table] = cur.rowcount
    conn.execute("DELETE FROM network_tags WHERE id = ?", (source_id,))
    return moved


# ---- 설정 (key-value) ----
# 대시보드에서 변경 가능한 런타임 설정. 환경변수 설정(config.py)과 달리
# DB 에 저장돼 재시작 없이 반영되고 백업/복원에 포함된다.

SIGNUP_ENABLED_KEY = "signup_enabled"            # 'on' | 'off' (기본 on)
SIGNUP_DEFAULT_ROLE_KEY = "signup_default_role"  # SIGNUP_ROLES 중 하나 (기본 pending)

# 사이트 전체 아카이브 기본 옵션·실패 재시도 대기. 값 해석과 범위 검증은
# crawler.crawl_defaults / crawler.retry_backoff 가 맡는다 (오염 시 config 기본값).
CRAWL_DEFAULT_MAX_PAGES_KEY = "crawl_default_max_pages"
CRAWL_DEFAULT_MAX_DEPTH_KEY = "crawl_default_max_depth"
CRAWL_DEFAULT_DELAY_KEY = "crawl_default_delay_seconds"
CRAWL_RETRY_BACKOFF_KEY = "crawl_retry_backoff_seconds"  # 쉼표 구분 초 목록 (예: '300,900')

# 회원 가입(셀프 가입·SSO 자동 생성)으로 만든 계정에 부여할 수 있는 초기 권한.
# 기본은 권한없음(pending) — 관리자가 사용자 관리에서 승인(권한 변경)해야 한다.
SIGNUP_ROLES = ("pending", "viewer", "archiver")


def get_setting(conn: sqlite3.Connection, key: str) -> str | None:
    """설정 값 조회 (없으면 None)."""
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row is not None else None


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    """설정 값 저장 (있으면 교체)."""
    conn.execute(
        """
        INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                       updated_at = excluded.updated_at
        """,
        (key, value, _utcnow()),
    )


def signup_enabled(conn: sqlite3.Connection) -> bool:
    """로그인 화면의 회원 가입 허용 여부 (기본 허용)."""
    return get_setting(conn, SIGNUP_ENABLED_KEY) != "off"


def signup_default_role(conn: sqlite3.Connection) -> str:
    """회원 가입으로 생성되는 계정의 초기 권한 (기본·값 오염 시 pending)."""
    role = get_setting(conn, SIGNUP_DEFAULT_ROLE_KEY)
    return role if role in SIGNUP_ROLES else "pending"


# ---- API 키 ----


def create_api_key(
    conn: sqlite3.Connection,
    name: str,
    token_hash: str,
    prefix: str,
    *,
    can_view: bool,
    can_archive: bool,
    created_by: int | None,
    ttl_seconds: int | None,
) -> int:
    """API 키 row 생성 후 id 반환. ttl_seconds=None 이면 영구 키."""
    expires_at = _later(ttl_seconds) if ttl_seconds is not None else None
    cur = conn.execute(
        """
        INSERT INTO api_keys
            (name, token_hash, prefix, can_view, can_archive,
             created_by, created_at, expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (name, token_hash, prefix, int(can_view), int(can_archive),
         created_by, _utcnow(), expires_at),
    )
    return cur.lastrowid


def get_api_key_by_hash(
    conn: sqlite3.Connection, token_hash: str
) -> sqlite3.Row | None:
    """유효한(만료되지 않은) API 키를 토큰 해시로 조회 (없거나 만료면 None)."""
    return conn.execute(
        """
        SELECT * FROM api_keys
        WHERE token_hash = ? AND (expires_at IS NULL OR expires_at > ?)
        """,
        (token_hash, _utcnow()),
    ).fetchone()


def list_api_keys(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """전체 API 키 목록 (+ 발급자 이메일, 등록 순). 만료된 키도 포함 — 관리 화면용."""
    return conn.execute(
        """
        SELECT k.*, u.email AS creator_email,
               (k.expires_at IS NOT NULL AND k.expires_at <= ?) AS expired
        FROM api_keys k LEFT JOIN users u ON u.id = k.created_by
        ORDER BY k.created_at, k.id
        """,
        (_utcnow(),),
    ).fetchall()


def touch_api_key(conn: sqlite3.Connection, key_id: int) -> None:
    """API 키 마지막 사용 시각 갱신."""
    conn.execute(
        "UPDATE api_keys SET last_used_at = ? WHERE id = ?", (_utcnow(), key_id)
    )


def delete_api_key(conn: sqlite3.Connection, key_id: int) -> bool:
    """API 키 폐기 — 즉시 무효화된다. 없으면 False."""
    cur = conn.execute("DELETE FROM api_keys WHERE id = ?", (key_id,))
    return cur.rowcount == 1


# ---- 사이트 로그인 자격증명 ----
# 아카이빙 대상 사이트에 춘추관이 로그인하기 위한 외부 자격증명. secret 은
# 암호문만 저장한다 (crypto 로 대칭 암호화 — CLAUDE.md 원칙 6 예외). 쓰기는
# credentials.py 코어 모듈을 거친다.


def create_site_credential(
    conn: sqlite3.Connection,
    site_id: int,
    label: str,
    kind: str,
    secret: str,
    *,
    created_by: int | None,
) -> int:
    """사이트 자격증명 row 생성 후 id 반환. secret 은 암호문(평문 금지).

    같은 사이트 안에서 label 은 UNIQUE — 호출부가 중복을 먼저 검사한다.
    """
    now = _utcnow()
    cur = conn.execute(
        """
        INSERT INTO site_credentials
            (site_id, label, kind, secret, created_by, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (site_id, label, kind, secret, created_by, now, now),
    )
    return cur.lastrowid


def get_site_credential(
    conn: sqlite3.Connection, cred_id: int
) -> sqlite3.Row | None:
    """자격증명 id 로 조회 (암호문 secret 포함, 없으면 None)."""
    return conn.execute(
        "SELECT * FROM site_credentials WHERE id = ?", (cred_id,)
    ).fetchone()


def list_site_credentials(
    conn: sqlite3.Connection, site_id: int
) -> list[sqlite3.Row]:
    """사이트의 자격증명 목록 (라벨순, 암호문 secret 제외 — 관리 화면용)."""
    return conn.execute(
        """
        SELECT c.id, c.site_id, c.label, c.kind, c.created_by,
               c.created_at, c.updated_at, u.email AS creator_email
        FROM site_credentials c LEFT JOIN users u ON u.id = c.created_by
        WHERE c.site_id = ? ORDER BY c.label, c.id
        """,
        (site_id,),
    ).fetchall()


def get_site_credential_by_label(
    conn: sqlite3.Connection, site_id: int, label: str
) -> sqlite3.Row | None:
    """사이트의 라벨로 자격증명 조회 (중복 검사용, 없으면 None)."""
    return conn.execute(
        "SELECT * FROM site_credentials WHERE site_id = ? AND label = ?",
        (site_id, label),
    ).fetchone()


def count_site_credentials(conn: sqlite3.Connection, site_id: int) -> int:
    """사이트의 자격증명 수 (사이트 상세 표시용)."""
    return conn.execute(
        "SELECT COUNT(*) AS c FROM site_credentials WHERE site_id = ?", (site_id,)
    ).fetchone()["c"]


def delete_site_credential(conn: sqlite3.Connection, cred_id: int) -> bool:
    """자격증명 삭제. 없으면 False.

    이 자격증명을 연결한 페이지·크롤·크롤 스케줄(credential_id)은 NULL 로
    끊는다 — FK 가 RESTRICT 라 끊지 않으면 삭제가 막힌다.
    """
    for table in ("pages", "crawls", "crawl_schedules"):
        conn.execute(
            f"UPDATE {table} SET credential_id = NULL WHERE credential_id = ?",
            (cred_id,),
        )
    cur = conn.execute("DELETE FROM site_credentials WHERE id = ?", (cred_id,))
    return cur.rowcount == 1


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
