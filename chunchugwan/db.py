"""SQLite 인덱스 레이어. 모든 DB 접근은 이 모듈을 통해서만 한다."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager

try:
    import fcntl  # POSIX(리눅스·macOS) 전용 — 프로세스 간 마이그레이션 직렬화에 쓴다
except ImportError:  # pragma: no cover - Windows. 도커(리눅스) 멀티프로세스 시나리오엔 무관
    fcntl = None  # type: ignore[assignment]
from datetime import datetime, timedelta, timezone
from typing import Iterator, Sequence

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
# 최초 구동(사용자 0명) 판정 래치 — first_run_needed 가 1회 확인 후 세팅한다.
_users_exist_latch = False


def invalidate_schema_cache() -> None:
    """스키마 보장 캐시 무효화 — DB 파일을 교체(복원 등)한 뒤 호출.

    역할 프리셋 캐시도 함께 무효화한다 — 복원은 같은 경로에 다른 내용의 DB 를
    넣어 (경로, 버전) 키가 우연히 겹칠 수 있으므로 강제로 다음 호출에서 재로드.
    """
    global _presets_version, _presets_cache_path, _users_exist_latch
    with _schema_lock:
        _schema_ready.clear()
    _presets_version = _PRESETS_UNSET
    _presets_cache_path = None
    _users_exist_latch = False  # 복원이 DB 를 비울 수 있으므로 최초 구동 판정도 재평가

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
    search_indexed INTEGER NOT NULL DEFAULT 0,    -- 텍스트 검색 인덱스(snapshot_fts) 반영 여부.
                                                  --   0 이면 'wccg search reindex' 백필 대상
    bytes         INTEGER NOT NULL DEFAULT 0,     -- 스냅샷 디렉토리 파일 용량 합(비정규화).
                                                  --   캡처/compact 시 1회 기록, 용량 집계가
                                                  --   파일시스템 stat 대신 SUM(bytes) 를 쓴다
    title         TEXT                            -- 캡처 당시 페이지 제목(meta.json 사본).
                                                  --   목록·상세의 현재 제목 표시가 meta.json
                                                  --   반복 파싱 대신 이 컬럼을 쓴다
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
    requested_by   INTEGER REFERENCES users(id),     -- 요청한 사용자 (web/확장 토큰) — 결과 알림 귀속
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
    requested_by     INTEGER REFERENCES users(id),  -- 요청한 사용자 (web/확장 토큰). 로그로 이어진다
    network_tag_id   TEXT REFERENCES network_tags(id),         -- 사설 대역의 로컬 네트워크 태그
    credential_id    INTEGER REFERENCES site_credentials(id),  -- 적용할 로그인 자격증명(선택)
    interval_seconds INTEGER,             -- 아카이빙 후 자동 재아카이빙 주기 등록용(선택)
    run_at           TEXT,                -- 'HH:MM' 서버 로컬 (1일 단위 주기 실행 시각)
    status           TEXT NOT NULL DEFAULT 'pending', -- pending|in_progress (done/failed 는 삭제)
    attempts         INTEGER NOT NULL DEFAULT 0,
    next_attempt_at  TEXT,                -- 재시도 대기 시각 (NULL = 즉시 가능)
    claimed_at       TEXT,                -- in_progress 시작 시각 (중단 복구 판정용)
    error            TEXT,                -- 마지막 실패 사유 (재시도 대기 중 표시용)
    created_at       TEXT NOT NULL,
    -- 사람 보조 챌린지 해결(라이브) — 자동 통과 실패 시 worker 가 채운다
    needs_human_at   TEXT,                -- 사람 확인 필요 진입 시각 (NULL = 자동 진행)
    live_token       TEXT,                -- 라이브 화면/명령 경로 키 (예측불가 난수)
    live_owner_id    INTEGER REFERENCES users(id),  -- 세션을 클레임한 admin (입력 권한자)
    live_cancel      INTEGER NOT NULL DEFAULT 0,     -- 1 이면 사람이 취소 → worker 가 중단
    live_force_solve INTEGER NOT NULL DEFAULT 0,     -- 1 이면 사람이 '확인 완료' → 현재 페이지로 강제 진행
    live_viewport_w  INTEGER,             -- 라이브 뷰포트 (좌표 매핑용, worker 가 기록)
    live_viewport_h  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_archive_jobs_status ON archive_jobs(status, next_attempt_at);
-- 같은 URL 의 활성(대기·진행) 작업은 하나만 — 중복 enqueue 를 DB 레벨에서 차단
CREATE UNIQUE INDEX IF NOT EXISTS idx_archive_jobs_active
    ON archive_jobs(url) WHERE status IN ('pending', 'in_progress');
-- needs_human_at(라이브 챌린지) 인덱스는 SCHEMA 에 두지 않는다 — 그 컬럼은
-- _migrate 가 ALTER 로 추가하므로, 기존 DB 에서 executescript(SCHEMA)가 이
-- 인덱스를 먼저 만들려다 'no such column' 으로 실패한다 (site_id 인덱스와 같은
-- 이유, 439행 주석 참조). 인덱스는 _migrate 가 컬럼 추가 후 만든다.

-- 라이브 챌린지 세션의 사람 입력 명령 큐 (대시보드 INSERT → worker 가 재생)
CREATE TABLE IF NOT EXISTS live_commands (
    id          INTEGER PRIMARY KEY,
    live_token  TEXT NOT NULL,
    seq         INTEGER NOT NULL,         -- 명령 순서 (타이밍·드래그 재현)
    kind        TEXT NOT NULL,            -- click | move | down | up | key | text
    x           INTEGER,                  -- 0~viewport (worker 가 클램프)
    y           INTEGER,
    key         TEXT,                     -- 키 입력/문자열
    delay_ms    INTEGER NOT NULL DEFAULT 0, -- 직전 명령 이후 간격 (타이밍 재현)
    created_at  TEXT NOT NULL,
    consumed_at TEXT                      -- worker 가 재생하면 채움
);
CREATE INDEX IF NOT EXISTS idx_live_commands_token ON live_commands(live_token, seq);

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
    requested_by INTEGER REFERENCES users(id),       -- 요청한 사용자 (web/확장 토큰). 그 외(cli/schedule/crawl)는 NULL
    status       TEXT NOT NULL,          -- new|changed|unchanged|forced_same|error
    started_at   TEXT NOT NULL,          -- ISO 8601 UTC
    duration_ms  INTEGER NOT NULL DEFAULT 0,
    http_status  INTEGER,
    content_hash TEXT,
    error        TEXT,                   -- status='error' 일 때 예외 요약
    steps        TEXT,                   -- 단계별 기록 JSON [{step, ms, detail}]
    job_id       INTEGER                 -- 이 로그를 만든 archive_jobs.id (FK 아님 — 작업은
                                         --   완료 시 삭제되므로, 삭제된 id 를 보존해 확장이
                                         --   요청한 작업의 결과를 되찾는 상관 키로 쓴다)
);
CREATE INDEX IF NOT EXISTS idx_archive_logs_page ON archive_logs(page_id, started_at);
CREATE INDEX IF NOT EXISTS idx_archive_logs_domain ON archive_logs(domain, started_at);
-- idx_archive_logs_job(job_id) 은 _migrate 가 만든다 — job_id 는 마이그레이션으로
-- 추가되는 컬럼이라 SCHEMA 단계(executescript)에서 인덱스를 걸면 구형 DB 에서
-- "no such column: job_id" 로 깨진다 (requested_by 인덱스와 같은 이유로 _migrate 전용).

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

CREATE TABLE IF NOT EXISTS audit_logs (
    id            INTEGER PRIMARY KEY,
    created_at    TEXT NOT NULL,          -- ISO 8601 UTC
    actor         TEXT NOT NULL,          -- 요청 주체 (이메일 / API 키 이름 / 익명)
    actor_user_id INTEGER,                -- 세션 사용자 id (있으면, FK 없는 상관 키)
    action        TEXT NOT NULL,          -- 액션 종류 (db.AUDIT_ACTIONS)
    target        TEXT,                   -- 대상 식별 (URL·스냅샷·문서명 등)
    message       TEXT NOT NULL           -- 사람이 읽는 한국어 원문 (요청자 포함)
);
CREATE INDEX IF NOT EXISTS idx_audit_logs_time ON audit_logs(created_at);

CREATE TABLE IF NOT EXISTS users (
    id                  INTEGER PRIMARY KEY,
    email               TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash       TEXT,               -- NULL = SSO 전용 계정
    totp_secret         TEXT,               -- NULL = 2FA 미설정
    totp_pending_secret TEXT,               -- 등록 확인 전 임시 시크릿
    totp_last_used_at   TEXT,               -- 마지막으로 사용된 코드의 시간창 (재사용 방지)
    role                TEXT NOT NULL DEFAULT 'viewer',  -- admin|archiver|viewer|pending|blocked (권한 프리셋)
    permission_overrides TEXT NOT NULL DEFAULT '{}',  -- 프리셋과 다른 세분 권한 가감 (JSON {권한: bool})
    is_founder          INTEGER NOT NULL DEFAULT 0,  -- 최초 등록 관리자 (권한 변경 불가)
    display_name        TEXT,               -- 표시용 이름 (NULL = 이메일로 표시)
    email_verified      INTEGER NOT NULL DEFAULT 0,  -- 이메일 본인 인증 완료 여부 (SSO 계정은 IdP 가 검증)
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

CREATE TABLE IF NOT EXISTS permission_groups (
    name        TEXT PRIMARY KEY,            -- users.role 에 저장되는 정규화 키 ([a-z0-9_])
    label       TEXT NOT NULL,               -- 표시 라벨 (커스텀은 i18n 폴백으로 원문 출력)
    permissions TEXT NOT NULL DEFAULT '[]',  -- JSON 배열, db.PERMISSIONS 부분집합
    is_builtin  INTEGER NOT NULL DEFAULT 0,  -- admin/archiver/viewer = 1 (삭제·개명 불가)
    sort_order  INTEGER NOT NULL DEFAULT 100,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS oidc_states (
    state       TEXT PRIMARY KEY,
    nonce       TEXT NOT NULL,
    redirect_to TEXT NOT NULL DEFAULT '/',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS email_verifications (
    user_id     INTEGER PRIMARY KEY REFERENCES users(id),  -- 사용자당 1개 (재발송 시 교체)
    code_hash   TEXT NOT NULL,           -- 인증 코드의 SHA-256 (원문은 메일에만 존재)
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS auth_throttle (
    bucket       TEXT NOT NULL,          -- 보호 대상 분류 (login | login_ip | totp | …)
    key          TEXT NOT NULL,          -- 버킷 내 카운트 단위 (이메일·IP·세션·user_id)
    window_start TEXT NOT NULL,          -- 현재 고정 윈도우 시작 시각 (ISO 8601 UTC)
    count        INTEGER NOT NULL,       -- 윈도우 내 시도 횟수
    PRIMARY KEY (bucket, key)
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
    if cols and "permission_overrides" not in cols:
        # 역할 프리셋과 다른 세분 권한 가감 — 기본은 빈 dict(프리셋 그대로)
        conn.execute(
            "ALTER TABLE users ADD COLUMN permission_overrides TEXT NOT NULL DEFAULT '{}'"
        )
    if cols and "timezone" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN timezone TEXT NOT NULL DEFAULT 'UTC'")
    if cols and "locale" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN locale TEXT NOT NULL DEFAULT 'ko'")
    # 이메일 본인 인증 여부 — 기존 사용자는 미인증(0)으로 시작한다. 관리자가
    # 기능을 켜면 다음 로그인부터 인증을 요구받고, 개인 설정에서도 인증할 수 있다.
    if cols and "email_verified" not in cols:
        conn.execute(
            "ALTER TABLE users ADD COLUMN email_verified INTEGER NOT NULL DEFAULT 0"
        )
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
    # 로그인 자격증명으로 캡처된(로그인 뒤 콘텐츠) 스냅샷 표식 — 소유자/관리자만
    # 열람. authenticated_by 는 캡처에 쓰인 자격증명의 등록자(없으면 NULL=admin 전용).
    if cols and "authenticated" not in cols:
        conn.execute(
            "ALTER TABLE snapshots ADD COLUMN authenticated INTEGER NOT NULL DEFAULT 0"
        )
    if cols and "authenticated_by" not in cols:
        conn.execute(
            "ALTER TABLE snapshots ADD COLUMN authenticated_by INTEGER REFERENCES users(id)"
        )
    # 캡처 출처 — 'server'(서버 캡처, 기본) | 'extension'(브라우저 확장 클라이언트
    # 캡처). 확장 캡처는 실브라우저 렌더라 해상도·dpr 가 달라 스크린샷 비교를
    # 제공하지 않고 본문 diff 에 경고를 단다.
    if cols and "origin" not in cols:
        conn.execute(
            "ALTER TABLE snapshots ADD COLUMN origin TEXT NOT NULL DEFAULT 'server'"
        )
    # 불완전 캡처 표식 — 일부 자원·프레임·스크린샷 수집이 실패해도 저장하되 표시한다
    if cols and "incomplete" not in cols:
        conn.execute(
            "ALTER TABLE snapshots ADD COLUMN incomplete INTEGER NOT NULL DEFAULT 0"
        )
    # 스냅샷 디렉토리 용량 비정규화 — 용량 집계가 매번 파일시스템을 stat 하지 않게
    # 한다. 컬럼을 처음 추가하는 업그레이드에서만 기존 스냅샷을 파일시스템에서 1회
    # 백필한다 (신규 스냅샷은 캡처 시점에 기록, compact 가 형태를 바꾸면 갱신).
    if cols and "bytes" not in cols:
        conn.execute(
            "ALTER TABLE snapshots ADD COLUMN bytes INTEGER NOT NULL DEFAULT 0"
        )
        n = backfill_snapshot_bytes(conn)
        if n:
            logger.info("스냅샷 용량(bytes) 백필: %d개", n)
    # 페이지 제목 비정규화 — 목록·상세가 meta.json 을 반복 파싱하지 않게 한다.
    # 컬럼 최초 추가 시에만 기존 스냅샷의 meta.json 에서 1회 백필한다.
    if cols and "title" not in cols:
        conn.execute("ALTER TABLE snapshots ADD COLUMN title TEXT")
        n = backfill_snapshot_titles(conn)
        if n:
            logger.info("스냅샷 제목(title) 백필: %d개", n)
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
    # 확장(브라우저 클라이언트) 캡처 페이지 표식 — 1 이면 서버가 그 URL 을 다시
    # 가져오지 않는다(스케줄·크롤·재시도·재아카이빙 차단). 갱신은 확장 재캡처로만.
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(pages)")}
    if cols and "client_captured" not in cols:
        conn.execute(
            "ALTER TABLE pages ADD COLUMN client_captured INTEGER NOT NULL DEFAULT 0"
        )
    # API 키 소유자 — NULL=관리자 발급 시스템 키(공동관리), 값=그 사용자 귀속
    # 확장 토큰. 기존 키는 전부 시스템 키이므로 NULL 그대로가 정확한 의미(백필 없음).
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(api_keys)")}
    if cols and "owner_user_id" not in cols:
        conn.execute(
            "ALTER TABLE api_keys ADD COLUMN owner_user_id INTEGER REFERENCES users(id)"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_api_keys_owner ON api_keys(owner_user_id)"
    )
    # 확장 1회성 세션 자격증명의 만료 안전망 — site_credentials 는 SCHEMA 가 먼저 만든다.
    # NULL=영속(대시보드 등록), 값=확장이 만든 1회성(캡처 후 삭제, 만료 GC).
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(site_credentials)")}
    if cols and "expires_at" not in cols:
        conn.execute("ALTER TABLE site_credentials ADD COLUMN expires_at TEXT")
    # 단발 아카이빙 큐의 사람 보조(라이브 챌린지) 컬럼 — archive_jobs 는 SCHEMA 가 먼저 만든다
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(archive_jobs)")}
    if cols:
        for col, ddl in (
            ("needs_human_at", "TEXT"),
            ("live_token", "TEXT"),
            ("live_owner_id", "INTEGER REFERENCES users(id)"),
            ("live_cancel", "INTEGER NOT NULL DEFAULT 0"),
            ("live_force_solve", "INTEGER NOT NULL DEFAULT 0"),
            ("live_viewport_w", "INTEGER"),
            ("live_viewport_h", "INTEGER"),
            # 요청한 사용자 — 작업이 로그가 될 때 archive_logs.requested_by 로 이어진다
            ("requested_by", "INTEGER REFERENCES users(id)"),
        ):
            if col not in cols:
                conn.execute(f"ALTER TABLE archive_jobs ADD COLUMN {col} {ddl}")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_archive_jobs_needs_human "
            "ON archive_jobs(needs_human_at) WHERE needs_human_at IS NOT NULL"
        )
    # 아카이브 로그의 요청 사용자 — '내 아카이브' 화면의 필터 기준. 기존 로그는
    # 주체를 알 수 없으므로 NULL 그대로(백필 없음 — 누구의 것도 아닌 과거 기록).
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(archive_logs)")}
    if cols and "requested_by" not in cols:
        conn.execute(
            "ALTER TABLE archive_logs ADD COLUMN requested_by INTEGER REFERENCES users(id)"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_archive_logs_requester "
        "ON archive_logs(requested_by, started_at)"
    )
    # 결과 알림용 상관 키 — 확장이 요청한 작업의 결과(완료/실패)를 되찾게 작업 id 를
    # 로그까지 잇는다. 작업 행은 완료 시 삭제되므로 FK 는 걸지 않는다 (foreign_keys=ON
    # 이라 FK 면 작업 삭제가 막힌다). 기존 로그는 NULL (과거 작업 id 를 알 수 없음).
    if cols and "job_id" not in cols:
        conn.execute("ALTER TABLE archive_logs ADD COLUMN job_id INTEGER")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_archive_logs_job ON archive_logs(job_id)"
    )
    # 크롤 요청자 — 확장/웹이 시작한 사이트 전체 아카이브의 결과 알림 귀속. 기존 크롤은 NULL.
    crawl_cols = {r["name"] for r in conn.execute("PRAGMA table_info(crawls)")}
    if crawl_cols and "requested_by" not in crawl_cols:
        conn.execute(
            "ALTER TABLE crawls ADD COLUMN requested_by INTEGER REFERENCES users(id)"
        )
    # 핫패스·삭제·정리 경로의 인덱스 묶음 (멱등). crawl_pages·archive_logs 는
    # 데이터 증가에 가장 크게 자라는 테이블이라, 인덱스가 없으면 아카이빙마다
    # 전체 스캔하거나 삭제·로그 화면이 행 수에 비례해 느려진다. 대상 컬럼은
    # 모두 SCHEMA 또는 위 ALTER 로 이 시점에 존재가 보장된다.
    for ddl in (
        # 아카이빙 핫패스 (pipeline 이 아카이빙마다 url 로 조회)
        "CREATE INDEX IF NOT EXISTS idx_crawl_pages_url ON crawl_pages(url)",
        "CREATE INDEX IF NOT EXISTS idx_crawls_start_url ON crawls(start_url)",
        # 삭제 경로 (스냅샷 삭제 시 참조 해제 — 현재 전체 스캔)
        "CREATE INDEX IF NOT EXISTS idx_crawl_pages_snapshot ON crawl_pages(snapshot_id)",
        "CREATE INDEX IF NOT EXISTS idx_archive_logs_snapshot ON archive_logs(snapshot_id)",
        # 자격증명 삭제 시 참조 NULL 처리 / 조회
        "CREATE INDEX IF NOT EXISTS idx_pages_credential ON pages(credential_id)",
        "CREATE INDEX IF NOT EXISTS idx_crawls_credential ON crawls(credential_id)",
        "CREATE INDEX IF NOT EXISTS idx_crawl_schedules_credential "
        "ON crawl_schedules(credential_id)",
        # 로그 목록 무필터 정렬 / 만료 세션 정리
        "CREATE INDEX IF NOT EXISTS idx_archive_logs_started ON archive_logs(started_at)",
        "CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at)",
    ):
        conn.execute(ddl)
    _seed_permission_groups(conn)
    _migrate_api_key_permission(conn)
    _migrate_log_view_permissions(conn)
    _backfill_sites(conn)


def _seed_permission_groups(conn: sqlite3.Connection) -> None:
    """빌트인 권한 그룹(admin/archive_manager/archiver/viewer) 시드 — 멱등(INSERT OR IGNORE).

    permission_groups 테이블은 SCHEMA(executescript)가 먼저 만든다. 빌트인의
    permissions 는 시드 후 관리자가 편집할 수 있고(권한 묶음 조정), label·name 은
    잠긴다. pending/blocked/withdrawn 은 권한묶음이 아니라 접근 게이트 상태라
    이 테이블에 넣지 않는다 (코드 상수 STATE_ROLES).

    신규 설치 기본값: 아카이브(archiver)는 보기·아카이빙, 아카이브 관리
    (archive_manager)는 +삭제. 기존 설치는 INSERT OR IGNORE 로 archiver 가 그대로
    유지되고(이미 삭제 권한 보유), archive_manager 만 새로 추가된다. use_api_keys 는
    viewer 외 빌트인에 기본 부여 — 기존 설치 보강은 _migrate_api_key_permission.
    """
    for name, label, perms, order in (
        ("admin", "관리자", list(PERMISSIONS), 10),
        ("archive_manager", "아카이브 관리",
         ["view", "archive", "delete", "use_api_keys"], 15),
        ("archiver", "아카이브", ["view", "archive", "use_api_keys"], 20),
        ("viewer", "보기 전용", ["view"], 30),
    ):
        conn.execute(
            "INSERT OR IGNORE INTO permission_groups "
            "(name, label, permissions, is_builtin, sort_order, created_at) "
            "VALUES (?, ?, ?, 1, ?, ?)",
            (name, label, json.dumps(perms), order, _utcnow()),
        )


def _migrate_api_key_permission(conn: sqlite3.Connection) -> None:
    """기존 빌트인 그룹 admin·archiver 에 use_api_keys 권한 보강 — 멱등.

    use_api_keys 는 신규 추가 권한이라 기존 설치의 그룹 permissions JSON 에는
    없다. admin(전체 보유 불변)·archiver(아카이빙 그룹)에 추가하고 viewer 는
    제외한다(개인 API Key·크롬 확장 사용 불가가 기본). archive_manager 는
    _seed_permission_groups 가 use_api_keys 를 포함해 시드하므로 별도 보강이
    필요 없다. 신규 설치는 시드가 이미 넣어 이 함수는 no-op 이 된다.
    """
    changed = False
    for name in ("admin", "archiver"):
        row = conn.execute(
            "SELECT permissions FROM permission_groups "
            "WHERE name = ? AND is_builtin = 1",
            (name,),
        ).fetchone()
        if row is None:
            continue
        perms = _parse_permission_list(row["permissions"])
        if "use_api_keys" not in perms:
            perms.append("use_api_keys")
            conn.execute(
                "UPDATE permission_groups SET permissions = ? WHERE name = ?",
                (json.dumps(perms), name),
            )
            changed = True
    if changed:
        _bump_permission_groups_version(conn)


def _migrate_log_view_permissions(conn: sqlite3.Connection) -> None:
    """기존 빌트인 admin 그룹에 로그 열람 3종 권한을 보강 — 멱등.

    view_audit_logs·view_system_logs·view_archive_logs 는 신규 추가 권한이라
    기존 설치의 admin 그룹 permissions JSON 에는 없다. '관리자 권한 그룹만
    기본값'이라 admin 에만 보강하고 다른 빌트인에는 넣지 않는다. 신규 설치는
    _seed_permission_groups 가 list(PERMISSIONS) 로 이미 포함하므로 no-op.
    """
    row = conn.execute(
        "SELECT permissions FROM permission_groups "
        "WHERE name = 'admin' AND is_builtin = 1"
    ).fetchone()
    if row is None:
        return
    perms = _parse_permission_list(row["permissions"])
    added = [p for p in LOG_VIEW_PERMISSIONS if p not in perms]
    if added:
        perms.extend(added)
        conn.execute(
            "UPDATE permission_groups SET permissions = ? WHERE name = 'admin'",
            (json.dumps(perms),),
        )
        _bump_permission_groups_version(conn)


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
    # 런타임 PRAGMA — 커넥션마다 적용(영구 저장 아님), WAL+NORMAL 과 충돌 없음.
    # 커넥션을 매 요청·작업마다 새로 여는 구조라 모든 쿼리에 깔리는 고정 비용을 낮춘다.
    conn.execute("PRAGMA cache_size = -16000")    # ~16MB 페이지 캐시 (기본 ~2MB)
    conn.execute("PRAGMA mmap_size = 268435456")  # 256MB — read() 대신 메모리 매핑
    conn.execute("PRAGMA temp_store = MEMORY")    # ORDER BY/GROUP BY 임시정렬을 메모리에서
    _ensure_schema(conn, db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


@contextmanager
def _migration_lock(db_path) -> Iterator[None]:
    """스키마 생성·마이그레이션을 프로세스 간 직렬화하는 advisory 파일 락.

    serve·worker 가 같은 DB 를 동시에 처음 열 때 한 프로세스만 스키마를 준비하게
    한다. fcntl(POSIX)이 없는 환경(Windows)에서는 멀티프로세스 동시 마이그레이션이
    상정되지 않으므로 락 없이 진행한다.
    """
    if fcntl is None:  # pragma: no cover - Windows
        yield
        return
    lock_path = f"{db_path}.migrate.lock"
    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def _ensure_schema(conn: sqlite3.Connection, db_path) -> None:
    """프로세스에서 처음 보는 DB 파일이면 WAL 전환 + 스키마 생성·마이그레이션.

    db_path 는 conn 을 연 경로 — config.DB_PATH 를 다시 읽으면 다른 스레드가
    경로를 바꿨을 때(테스트 등) 엉뚱한 키가 '준비됨'으로 오염될 수 있다.
    """
    global _users_exist_latch
    key = str(db_path)
    with _schema_lock:
        if key in _schema_ready:
            return
        # serve·worker 가 같은 DB 를 동시에 처음 열면 스키마 준비가 프로세스 간
        # 겹쳐 깨진다 — WAL 전환·executescript 경합은 'database is locked',
        # _migrate 의 'cols 읽기 → 가드 → ALTER' 레이스는 'duplicate column' 을 낸다.
        # sqlite 쓰기 락(BEGIN IMMEDIATE)은 executescript 의 자동 커밋과 busy_timeout
        # 신뢰성 문제로 이 구간을 온전히 감싸지 못하므로, advisory 파일 락으로 스키마
        # 준비 전체(WAL 전환 포함)를 프로세스 간 직렬화한다. 한 프로세스가 끝낸 뒤
        # 다른 쪽이 들어오면 테이블·컬럼이 이미 있어 CREATE IF NOT EXISTS·가드가 스킵한다.
        with _migration_lock(db_path):
            # journal_mode 는 DB 파일에 영구 저장된다 — 이후 커넥션은 자동 WAL
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA)
            _migrate(conn)
            conn.commit()
        _schema_ready.add(key)
        # 새 DB 파일을 이 프로세스가 처음 쓰기 시작했다 — 최초 구동 판정 래치는
        # 이전 DB 기준이라 무효다. 풀어서 first_run_needed 가 새 DB 로 재평가하게
        # 한다 (복원·테스트의 DB 교체가 이 경로를 탄다).
        _users_exist_latch = False


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
        SELECT s.*, p.url AS page_url, p.domain, p.slug, p.site_id, p.network_tag_id
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


def list_sites_overview(
    conn: sqlite3.Connection, *, viewer: "tuple[int | None, bool] | None" = None
) -> list[sqlite3.Row]:
    """사이트 목록 + 페이지·스냅샷·크롤 회차·스케줄 집계 (아카이브 목록 화면용).

    last_activity_at 은 마지막 스냅샷과 마지막 크롤 활동(완료 또는 생성) 중
    더 최근 시각 — 목록 정렬 기준이다. viewer 를 주면 인증(로그인) 스냅샷은
    소유자/관리자만 스냅샷 수·마지막 활동에 반영한다 (메타데이터 누출 차단).
    """
    sv, params = _visible_snapshot_filter(viewer)
    return conn.execute(
        f"""
        SELECT st.*,
               (SELECT COUNT(*) FROM pages p WHERE p.site_id = st.id) AS page_count,
               (SELECT COUNT(*) FROM snapshots s JOIN pages p ON p.id = s.page_id
                 WHERE p.site_id = st.id{sv}) AS snapshot_count,
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
                             WHERE p.site_id = st.id{sv}), ''),
                   COALESCE((SELECT MAX(COALESCE(c.finished_at, c.created_at))
                             FROM crawls c WHERE c.site_id = st.id), '')
               ) AS last_activity_at
        FROM sites st
        ORDER BY last_activity_at DESC, st.site_key
        """,
        params,
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


def set_page_client_captured(conn: sqlite3.Connection, page_id: int) -> None:
    """페이지를 확장(브라우저 클라이언트) 캡처로 표시 — 서버 재요청을 막는다.

    한번 1 이 되면 스케줄·크롤·재시도·대시보드 재아카이빙이 그 URL 을 서버
    캡처하지 않는다(불변식). 갱신은 확장 재캡처로만. 멱등.
    """
    conn.execute("UPDATE pages SET client_captured = 1 WHERE id = ?", (page_id,))


def last_snapshot(conn: sqlite3.Connection, page_id: int) -> sqlite3.Row | None:
    """해당 페이지의 가장 최근 스냅샷 row (없으면 None)."""
    return conn.execute(
        "SELECT * FROM snapshots WHERE page_id = ? ORDER BY taken_at DESC, id DESC LIMIT 1",
        (page_id,),
    ).fetchone()


_SNAPSHOT_COLUMNS = frozenset(
    {"taken_at", "dir_name", "content_hash", "final_url", "http_status", "changed",
     "note", "resources_indexed", "css_externalized", "search_indexed", "bytes",
     "title", "authenticated", "authenticated_by", "origin", "incomplete"}
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


def update_snapshot_bytes(conn: sqlite3.Connection, snapshot_id: int, n: int) -> None:
    """스냅샷의 비정규화 용량(bytes)을 갱신 — compact 가 저장 형태를 바꾼 뒤 호출."""
    conn.execute("UPDATE snapshots SET bytes = ? WHERE id = ?", (n, snapshot_id))


def backfill_snapshot_bytes(
    conn: sqlite3.Connection, *, only_missing: bool = False
) -> int:
    """스냅샷 bytes 를 파일시스템에서 재계산해 갱신하고 갱신 건수 반환.

    only_missing=True 면 bytes=0 인 행만(마이그레이션 후 lazy 보정), False 면
    전체(컬럼 최초 추가·compact 후 형태 변경 반영). 디렉토리가 없는 스냅샷은
    0 으로 둔다 (로그만 남고 파일이 지워진 경우).
    """
    where = " WHERE s.bytes = 0" if only_missing else ""
    rows = conn.execute(
        f"SELECT s.id, p.domain, p.slug, s.dir_name "
        f"FROM snapshots s JOIN pages p ON p.id = s.page_id{where}"
    ).fetchall()
    n = 0
    for r in rows:
        snap_dir = storage.page_dir(r["domain"], r["slug"]) / r["dir_name"]
        conn.execute(
            "UPDATE snapshots SET bytes = ? WHERE id = ?",
            (storage.snapshot_dir_bytes(snap_dir), r["id"]),
        )
        n += 1
    return n


def backfill_snapshot_titles(conn: sqlite3.Connection) -> int:
    """모든 스냅샷의 title 을 meta.json 에서 채우고 갱신 건수 반환 (컬럼 최초 추가용).

    meta.json 이 없거나 title 키가 없으면 NULL 로 둔다 (오류 페이지·구형 등).
    """
    rows = conn.execute(
        "SELECT s.id, p.domain, p.slug, s.dir_name "
        "FROM snapshots s JOIN pages p ON p.id = s.page_id"
    ).fetchall()
    n = 0
    for r in rows:
        meta = storage.page_dir(r["domain"], r["slug"]) / r["dir_name"] / "meta.json"
        try:
            title = json.loads(meta.read_text(encoding="utf-8")).get("title") or None
        except (OSError, ValueError):
            title = None
        if title is not None:
            conn.execute("UPDATE snapshots SET title = ? WHERE id = ?", (title, r["id"]))
            n += 1
    return n


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
        "url", "domain", "page_id", "snapshot_id", "source", "requested_by",
        "status", "started_at", "duration_ms", "http_status", "content_hash",
        "error", "steps", "job_id",
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
    requested_by: int | None,
    date_from: str | None,
    date_to: str | None,
) -> tuple[str, list[object]]:
    """archive_logs 필터 WHERE 절 조립 (list/count 공용).

    date_from/date_to 는 YYYY-MM-DD. started_at 이 ISO 8601 이므로 하한은
    문자열 비교로, 상한은 다음날 0시 미만으로 비교한다 (해당 날짜 포함).
    requested_by 는 '내 아카이브' 화면이 본인 요청만 추리는 데 쓴다.
    """
    where: list[str] = []
    params: list[object] = []
    for cond, value in (
        ("al.domain = ?", domain),
        ("al.page_id = ?", page_id),
        ("al.snapshot_id = ?", snapshot_id),
        ("al.status = ?", status),
        ("al.requested_by = ?", requested_by),
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
    requested_by: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[sqlite3.Row]:
    """아카이브 실행 로그 (최신 순). 도메인/페이지/스냅샷/상태/요청자/기간으로 필터.

    스냅샷이 생긴 로그에는 디렉토리 위치(snap_domain, snap_slug, snap_dir_name)를
    함께 반환한다 — 대시보드가 저장된 파일 목록/용량을 조회하는 데 쓴다.
    사설 대역 페이지의 로그 구분용으로 로컬 네트워크 태그 이름·설명도 붙인다.
    """
    where_sql, params = _archive_log_where(
        domain=domain, page_id=page_id, snapshot_id=snapshot_id,
        status=status, requested_by=requested_by,
        date_from=date_from, date_to=date_to,
    )
    sql = """
        SELECT al.*, s.dir_name AS snap_dir_name,
               sp.domain AS snap_domain, sp.slug AS snap_slug,
               nt.name AS network_tag_name,
               nt.description AS network_tag_description,
               lp.network_tag_id, lp.site_id AS page_site_id
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
    requested_by: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> int:
    """필터 조건에 맞는 아카이브 로그 총 건수 (페이징용)."""
    where_sql, params = _archive_log_where(
        domain=domain, page_id=page_id, snapshot_id=snapshot_id,
        status=status, requested_by=requested_by,
        date_from=date_from, date_to=date_to,
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


# ---- 감사 로그 (audit_logs — 사용자 액션 기록, web.audit 가 적재) ----

# 감사 액션 종류 — 화면 필터의 단위. archive(아카이빙)·view(아카이브 열람)·
# download(문서 다운로드)·admin(설정·권한·자격증명 등 관리 작업).
AUDIT_ACTIONS = ("archive", "view", "download", "admin")
AUDIT_ACTION_LABELS = {
    "archive": "아카이빙",
    "view": "열람",
    "download": "문서 다운로드",
    "admin": "관리 작업",
}


def insert_audit_log(
    conn: sqlite3.Connection,
    *,
    created_at: str,
    actor: str,
    action: str,
    message: str,
    actor_user_id: int | None = None,
    target: str | None = None,
) -> int:
    """감사 로그 한 행 삽입 후 id 반환."""
    cur = conn.execute(
        "INSERT INTO audit_logs "
        "(created_at, actor, actor_user_id, action, target, message)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (created_at, actor, actor_user_id, action, target, message),
    )
    return cur.lastrowid


def _audit_log_where(
    *,
    action: str | None,
    actor: str | None,
    date_from: str | None,
    date_to: str | None,
) -> tuple[str, list[object]]:
    """audit_logs 필터 WHERE 절 조립 (list/count 공용). 날짜 의미는 system_logs 와 동일."""
    where: list[str] = []
    params: list[object] = []
    for cond, value in (
        ("action = ?", action),
        ("actor = ?", actor),
        ("created_at >= ?", date_from),
        ("created_at < DATE(?, '+1 day')", date_to),
    ):
        if value is not None:
            where.append(cond)
            params.append(value)
    return (" WHERE " + " AND ".join(where)) if where else "", params


def list_audit_logs(
    conn: sqlite3.Connection,
    *,
    action: str | None = None,
    actor: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[sqlite3.Row]:
    """감사 로그 (최신 순). 액션/요청자/기간으로 필터."""
    where_sql, params = _audit_log_where(
        action=action, actor=actor, date_from=date_from, date_to=date_to,
    )
    sql = (
        "SELECT * FROM audit_logs" + where_sql
        + " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
    )
    return conn.execute(sql, params + [limit, offset]).fetchall()


def count_audit_logs(
    conn: sqlite3.Connection,
    *,
    action: str | None = None,
    actor: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> int:
    """필터 조건에 맞는 감사 로그 총 건수 (페이징용)."""
    where_sql, params = _audit_log_where(
        action=action, actor=actor, date_from=date_from, date_to=date_to,
    )
    row = conn.execute("SELECT COUNT(*) FROM audit_logs" + where_sql, params).fetchone()
    return row[0]


def list_audit_actors(conn: sqlite3.Connection) -> list[str]:
    """감사 로그에 등장한 요청 주체 목록 (필터 드롭다운용, 가나다순)."""
    rows = conn.execute("SELECT DISTINCT actor FROM audit_logs ORDER BY actor").fetchall()
    return [r["actor"] for r in rows]


def prune_audit_logs(conn: sqlite3.Connection, keep: int) -> int:
    """최신 keep 건만 남기고 오래된 감사 로그 삭제. 삭제 건수 반환."""
    cur = conn.execute(
        "DELETE FROM audit_logs WHERE id < ("
        " SELECT COALESCE(MIN(id), 0) FROM ("
        "  SELECT id FROM audit_logs ORDER BY id DESC LIMIT ?))",
        (keep,),
    )
    return cur.rowcount


def _visible_snapshot_filter(
    viewer: "tuple[int | None, bool] | None", alias: str = "s"
) -> tuple[str, dict]:
    """집계 쿼리용 — 요청자가 볼 수 있는 스냅샷 조건절(과 named 파라미터).

    viewer=None 이면 전체(CLI·신뢰 호출, 빈 조건). (viewer_id, is_admin) 를 주면
    인증 스냅샷은 관리자이거나 등록자 본인일 때만 포함한다 (앞에 ' AND ' 붙음).
    """
    if viewer is None:
        return "", {}
    viewer_id, is_admin = viewer
    cond = (f" AND ({alias}.authenticated = 0 OR :sv_admin"
            f" OR {alias}.authenticated_by = :sv_uid)")
    return cond, {"sv_admin": 1 if is_admin else 0, "sv_uid": viewer_id}


def list_pages(
    conn: sqlite3.Connection, *, viewer: "tuple[int | None, bool] | None" = None
) -> list[sqlite3.Row]:
    """페이지 목록 + 스냅샷 수 + 마지막 캡처 시각 (대시보드/CLI list 용).

    viewer 를 주면 그 요청자가 볼 수 있는 스냅샷만 집계한다 — 인증(로그인)
    스냅샷은 소유자/관리자만 카운트·시각에 반영(메타데이터 누출 차단). None=전체.
    """
    cond, params = _visible_snapshot_filter(viewer)
    return conn.execute(
        f"""
        SELECT p.*, COUNT(s.id) AS snapshot_count, MAX(s.taken_at) AS last_taken_at
        FROM pages p
        LEFT JOIN snapshots s ON s.page_id = p.id{cond}
        GROUP BY p.id
        ORDER BY last_taken_at DESC NULLS LAST, p.url
        """,
        params,
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
    """모든 스냅샷의 시각·위치·용량·제목 (id, taken_at, site_id, domain, slug,
    dir_name, bytes, title).

    현황 대시보드의 기간별 집계와 아카이브 목록의 사이트별 용량 합산·제목
    표시에 쓴다 (bytes·title 비정규화로 파일시스템·meta.json 접근 없음).
    """
    return conn.execute(
        """
        SELECT s.id, s.taken_at, p.site_id, p.domain, p.slug, s.dir_name,
               s.bytes, s.title
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
               p.site_id,
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


# IN(?,?,…) 파라미터는 SQLite 변수 한도(구버전 999) 아래로 끊는다 — 사이트 전체
# 삭제처럼 스냅샷 수천 개의 GC 후보를 모을 때 'too many SQL variables' 를 막는다.
_SQL_VAR_CHUNK = 900


def _chunked(seq: list, n: int = _SQL_VAR_CHUNK):
    """seq 를 길이 n 이하 조각으로 끊어 순서대로 내준다."""
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def list_snapshot_resource_refs(
    conn: sqlite3.Connection, snapshot_ids: list[int]
) -> list[str]:
    """해당 스냅샷들이 참조하는 자원 CAS 이름 목록 (중복 제거) — 삭제 GC 용."""
    seen: dict[str, None] = {}  # 청크 간 전역 중복 제거 + 등장 순서 보존
    for chunk in _chunked(snapshot_ids):
        marks = ", ".join("?" for _ in chunk)
        for r in conn.execute(
            f"SELECT DISTINCT name FROM snapshot_resources "
            f"WHERE snapshot_id IN ({marks})",
            chunk,
        ):
            seen.setdefault(r["name"])
    return list(seen)


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


def mark_snapshot_search_stale(conn: sqlite3.Connection, snapshot_id: int) -> None:
    """스냅샷을 다시 색인 필요(search_indexed=0)로 되돌린다.

    compact 가 구형 files/ 문서를 CAS 로 이전해 첨부 문서가 새로 생긴 스냅샷,
    또는 정합성 교정이 색인 누락을 발견한 스냅샷을 백필 대상으로 표시한다.
    """
    conn.execute(
        "UPDATE snapshots SET search_indexed = 0 WHERE id = ?", (snapshot_id,)
    )


# ---- 검색 인덱스 정합성 점검 (searchindex.verify / repair) ----
# search_indexed 플래그와 실제 FTS 행이 어긋나는 경우를 찾는다 — 플래그가
# 1 인데 FTS 행이 없거나(백필 실패 등 '거짓말 플래그'), FTS 행이 있는데
# 스냅샷이 없는(외부 조작·손상) orphan. 정상 경로로는 생기지 않지만,
# 플래그만으로는 감지할 수 없는 부류라 별도 점검을 둔다.


def count_search_indexed(conn: sqlite3.Connection) -> int:
    """검색 인덱스에 반영됐다고 표시된(search_indexed=1) 스냅샷 수."""
    return conn.execute(
        "SELECT COUNT(*) AS c FROM snapshots WHERE search_indexed = 1"
    ).fetchone()["c"]


def count_fts_rows(conn: sqlite3.Connection) -> int:
    """실제 FTS 인덱스 행 수 (테이블 없으면 0)."""
    if not _table_exists(conn, "snapshot_fts"):
        return 0
    return conn.execute("SELECT COUNT(*) AS c FROM snapshot_fts").fetchone()["c"]


def list_missing_fts_snapshot_ids(conn: sqlite3.Connection) -> list[int]:
    """search_indexed=1 인데 FTS 행이 없는 스냅샷 id (과소 색인 — '거짓말 플래그')."""
    if not _table_exists(conn, "snapshot_fts"):
        return []
    return [
        r["id"]
        for r in conn.execute(
            "SELECT s.id FROM snapshots s WHERE s.search_indexed = 1 "
            "AND NOT EXISTS (SELECT 1 FROM snapshot_fts f WHERE f.rowid = s.id)"
        )
    ]


def list_orphan_fts_rowids(conn: sqlite3.Connection) -> list[int]:
    """대응하는 스냅샷이 없는 FTS 행의 rowid (orphan — 정상 경로로는 안 생김)."""
    if not _table_exists(conn, "snapshot_fts"):
        return []
    return [
        r["rowid"]
        for r in conn.execute(
            "SELECT f.rowid AS rowid FROM snapshot_fts f "
            "WHERE NOT EXISTS (SELECT 1 FROM snapshots s WHERE s.id = f.rowid)"
        )
    ]


def delete_fts_rows(conn: sqlite3.Connection, rowids: list[int]) -> None:
    """지정한 rowid 의 FTS 행 삭제 (정합성 교정의 orphan 정리)."""
    if not rowids or not _table_exists(conn, "snapshot_fts"):
        return
    conn.executemany(
        "DELETE FROM snapshot_fts WHERE rowid = ?", [(r,) for r in rowids]
    )


# 검색 결과 행의 공통 투영. url 은 pages.url 과 snapshot_fts.url 이 겹치므로
# 반드시 테이블로 한정한다.
#
# FTS 경로는 FTS5 내장 snippet() 으로 DB 가 매치 주변만 잘라 준다 — 첨부 문서
# 본문(최대 2MB/문서)을 행마다 통째로 Python 으로 가져오던 것을 없앤다(순위 7).
# 인자: (테이블, content 컬럼=0, 시작/끝 마커 없음 — 강조는 템플릿 highlight 필터가
# terms 로 한다, 생략기호 …, trigram 토큰 최대 64개 ≈ 매치 주변 ~60자).
_SEARCH_SELECT_FTS = """
    SELECT snapshot_fts.rowid AS snapshot_id, s.page_id, s.taken_at, s.changed,
           p.url AS page_url, p.domain, p.site_id,
           snippet(snapshot_fts, 0, '', '', '…', 64) AS snippet,
           snapshot_fts.title AS title
    FROM snapshot_fts
    JOIN snapshots s ON s.id = snapshot_fts.rowid
    JOIN pages p ON p.id = s.page_id
"""
# LIKE 폴백(1~2글자, 드물게)은 MATCH 가 없어 snippet() 을 못 쓴다 — content 를
# 가져와 searchindex 가 매치 위치를 찾아 스니펫을 만든다.
_SEARCH_SELECT_LIKE = """
    SELECT snapshot_fts.rowid AS snapshot_id, s.page_id, s.taken_at, s.changed,
           p.url AS page_url, p.domain, p.site_id,
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
    sql = _SEARCH_SELECT_FTS + " WHERE snapshot_fts MATCH ?"
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
    sql = _SEARCH_SELECT_LIKE + " WHERE " + clause
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
    seen: set[tuple] = set()
    out: list[sqlite3.Row] = []
    for chunk in _chunked(snapshot_ids):
        marks = ", ".join("?" for _ in chunk)
        for r in conn.execute(
            f"SELECT DISTINCT sha256, file FROM snapshot_documents "
            f"WHERE snapshot_id IN ({marks})",
            chunk,
        ):
            key = (r["sha256"], r["file"])
            if key not in seen:
                seen.add(key)
                out.append(r)
    return out


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


def list_site_document_groups(
    conn: sqlite3.Connection, site_id: int, limit: int = 100, offset: int = 0
) -> list[sqlite3.Row]:
    """사이트 상세용 — 해당 사이트 스냅샷이 참조하는 문서 목록 (sha256 그룹).

    list_document_groups 의 사이트 스코프 버전. 그룹 집계(참조 스냅샷·페이지
    수, 최근 저장)는 이 사이트에 속한 스냅샷만으로 계산하고, 표시용 파일명·
    출처는 그 안에서 가장 최근 참조 행의 값을 쓴다 (최근 저장 순)."""
    return conn.execute(
        """
        SELECT g.sha256, g.snapshot_count, g.page_count, g.first_seen, g.last_seen,
               d.file, d.url, d.bytes, d.content_type, d.snapshot_id,
               s.page_id, p.url AS page_url
        FROM (
            SELECT d2.sha256 AS sha256, MAX(d2.id) AS doc_id,
                   COUNT(*) AS snapshot_count,
                   COUNT(DISTINCT s2.page_id) AS page_count,
                   MIN(s2.taken_at) AS first_seen, MAX(s2.taken_at) AS last_seen
            FROM snapshot_documents d2
            JOIN snapshots s2 ON s2.id = d2.snapshot_id
            JOIN pages p2 ON p2.id = s2.page_id
            WHERE p2.site_id = ?
            GROUP BY d2.sha256
        ) g
        JOIN snapshot_documents d ON d.id = g.doc_id
        JOIN snapshots s ON s.id = d.snapshot_id
        JOIN pages p ON p.id = s.page_id
        ORDER BY g.last_seen DESC, g.sha256
        LIMIT ? OFFSET ?
        """,
        (site_id, limit, offset),
    ).fetchall()


def count_site_document_groups(conn: sqlite3.Connection, site_id: int) -> int:
    """사이트 소속 스냅샷이 참조하는 고유 문서(sha256) 수 — 사이트 상세 문서 페이징용."""
    return conn.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT d2.sha256
            FROM snapshot_documents d2
            JOIN snapshots s2 ON s2.id = d2.snapshot_id
            JOIN pages p2 ON p2.id = s2.page_id
            WHERE p2.site_id = ?
            GROUP BY d2.sha256
        )
        """,
        (site_id,),
    ).fetchone()[0]


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
        SELECT sc.*, p.url, p.network_tag_id, p.site_id,
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
    requested_by: int | None = None,
    network_tag_id: str | None = None,
    credential_id: int | None = None,
) -> int:
    """크롤 row 생성 후 id 반환. next_page_at 은 지금 — 즉시 시작 가능.

    소속 사이트(site_id)는 시작 URL 에서 계산해 자동 연결한다. requested_by 는
    요청한 사용자(web/확장 토큰) — 확장의 결과 알림 귀속에 쓴다.
    """
    now = _utcnow()
    site_id = get_or_create_site(conn, storage.site_key(start_url))
    cur = conn.execute(
        """
        INSERT INTO crawls
            (start_url, scope_host, scope_path, max_pages, max_depth,
             delay_seconds, source, requested_by, site_id, network_tag_id,
             credential_id, created_at, next_page_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (start_url, scope_host, scope_path, max_pages, max_depth,
         delay_seconds, source, requested_by, site_id, network_tag_id,
         credential_id, now, now),
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
        SELECT cp.*, s.page_id AS snapshot_page_id, sp.site_id AS snapshot_site_id
        FROM crawl_pages cp
        LEFT JOIN snapshots s ON s.id = cp.snapshot_id
        LEFT JOIN pages sp ON sp.id = s.page_id
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
    requested_by: int | None = None,
    network_tag_id: str | None = None,
    credential_id: int | None = None,
    interval_seconds: int | None = None,
    run_at: str | None = None,
) -> bool:
    """단발 아카이빙 작업을 큐에 추가. 같은 URL 의 활성 작업이 이미 있으면 무시(False).

    중복 차단은 부분 UNIQUE 인덱스(idx_archive_jobs_active)가 한다 — INSERT OR
    IGNORE 가 활성 중복이면 0행을 넣고 False 를 반환한다 (현재 _register_job 의
    중복-방지 역할 대체). requested_by 는 요청 사용자(web/확장 토큰) — 작업이
    실행돼 archive_logs 한 행이 될 때 그대로 이어진다('내 아카이브' 귀속).

    확장(브라우저) 캡처 페이지(pages.client_captured=1)는 서버가 다시 가져오지
    않는다(불변식) — 큐에 넣지 않고 False 를 반환한다. 갱신은 확장 재캡처로만.
    """
    page = conn.execute(
        "SELECT client_captured FROM pages WHERE url = ?", (url,)
    ).fetchone()
    if page is not None and page["client_captured"]:
        return False
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO archive_jobs
            (url, force, source, requested_by, network_tag_id, credential_id,
             interval_seconds, run_at, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
        """,
        (url, 1 if force else 0, source, requested_by, network_tag_id,
         credential_id, interval_seconds, run_at, _utcnow()),
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
    """클레임 후 오래 방치된 in_progress 를 pending 으로 복구 (프로세스 중단 대비).

    사람 보조 대기 중(needs_human_at)인 작업은 제외한다 — 사람이 분 단위로
    푸는 동안 claimed_at 이 오래돼도 가로채면 안 된다 (worker 재시작 복구는
    sweep_needs_human 이 따로 한다)."""
    cur = conn.execute(
        """
        UPDATE archive_jobs SET status = 'pending', claimed_at = NULL
        WHERE status = 'in_progress' AND claimed_at <= ? AND needs_human_at IS NULL
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


# ---- 확장 결과 알림 — 작업/크롤 상태 조회 (소유자 스코프) ----
#
# 확장이 요청한 작업의 결과(완료/실패/사람 확인)를 폴링으로 되찾는다. 한 사용자가
# 남의 작업 id 를 조회하지 못하게 토큰 소유자(requested_by)로 스코프한다.

def _owner_scope(owner_id: int | None, scoped: bool) -> tuple[str, list[object]]:
    """소유자 스코프 WHERE 조각(앞에 ' AND ' 포함)과 파라미터를 반환.

    scoped=False(인증 off) → 무필터(단일 로컬 사용자), owner_id 있음(확장 토큰)
    → requested_by = ?, 없음(시스템 키) → requested_by IS NULL.
    """
    if not scoped:
        return "", []
    if owner_id is None:
        return " AND requested_by IS NULL", []
    return " AND requested_by = ?", [owner_id]


def get_active_archive_job_id(conn: sqlite3.Connection, url: str) -> int | None:
    """url 의 활성(대기/진행) 작업 id (없으면 None) — 부분 UNIQUE 로 최대 하나.

    enqueue 직후 같은 트랜잭션에서 작업 id 를 되읽어 확장이 결과를 추적하게 한다.
    """
    row = conn.execute(
        "SELECT id FROM archive_jobs WHERE url = ? AND status IN ('pending', 'in_progress')",
        (url,),
    ).fetchone()
    return row["id"] if row else None


def archive_job_status(
    conn: sqlite3.Connection, job_id: int, *, owner_id: int | None, scoped: bool
) -> sqlite3.Row | None:
    """소유자 스코프 활성 작업 1건 (id·url·status·needs_human_at). 없으면 None."""
    clause, params = _owner_scope(owner_id, scoped)
    return conn.execute(
        f"SELECT id, url, status, needs_human_at FROM archive_jobs WHERE id = ?{clause}",
        [job_id, *params],
    ).fetchone()


def latest_archive_log_for_job(
    conn: sqlite3.Connection, job_id: int, *, owner_id: int | None, scoped: bool
) -> sqlite3.Row | None:
    """소유자 스코프, 이 작업의 최신 로그 1건. 없으면 None.

    재시도로 여러 행이면 최신(id DESC)이 종결 상태다 (과거 error 로그로 오판 방지).
    """
    clause, params = _owner_scope(owner_id, scoped)
    return conn.execute(
        f"""
        SELECT status, url, page_id, snapshot_id, http_status, error
        FROM archive_logs WHERE job_id = ?{clause}
        ORDER BY id DESC LIMIT 1
        """,
        [job_id, *params],
    ).fetchone()


def crawl_status_for_owner(
    conn: sqlite3.Connection, crawl_id: int, *, owner_id: int | None, scoped: bool
) -> sqlite3.Row | None:
    """소유자 스코프 크롤 1건 (id·start_url·status). 없으면 None.

    상태별 페이지 수는 호출부가 crawl_page_counts 로 따로 조회한다.
    """
    clause, params = _owner_scope(owner_id, scoped)
    return conn.execute(
        f"SELECT id, start_url, status FROM crawls WHERE id = ?{clause}",
        [crawl_id, *params],
    ).fetchone()


# ---- 사람 보조 챌린지 해결 (라이브 세션 — live_challenge.py / 대시보드) ----

def get_archive_job(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    """작업 한 건 (대시보드 라이브 뷰가 상태·토큰·소유자를 읽는다)."""
    return conn.execute(
        "SELECT * FROM archive_jobs WHERE id = ?", (job_id,)
    ).fetchone()


def list_needs_human_jobs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """사람 확인 필요 상태의 작업들 (대시보드 배지·목록용)."""
    return conn.execute(
        """
        SELECT id, url, needs_human_at, live_owner_id
        FROM archive_jobs WHERE needs_human_at IS NOT NULL
        ORDER BY needs_human_at
        """
    ).fetchall()


def mark_needs_human(
    conn: sqlite3.Connection, job_id: int, *,
    token: str, viewport_w: int, viewport_h: int,
) -> None:
    """작업을 '사람 확인 필요'로 표시 (worker 가 라이브 진입 시). status 는
    in_progress 유지 — 활성 불변식·클레임 배제를 보존한다."""
    conn.execute(
        """
        UPDATE archive_jobs
        SET needs_human_at = ?, live_token = ?, live_cancel = 0, live_force_solve = 0,
            live_owner_id = NULL, live_viewport_w = ?, live_viewport_h = ?
        WHERE id = ?
        """,
        (_utcnow(), token, viewport_w, viewport_h, job_id),
    )


def clear_needs_human(conn: sqlite3.Connection, job_id: int) -> None:
    """라이브 상태 해제 (통과·취소·실패·worker 재시작 정리). 명령 큐도 비운다."""
    row = conn.execute(
        "SELECT live_token FROM archive_jobs WHERE id = ?", (job_id,)
    ).fetchone()
    if row and row["live_token"]:
        conn.execute("DELETE FROM live_commands WHERE live_token = ?", (row["live_token"],))
    conn.execute(
        """
        UPDATE archive_jobs
        SET needs_human_at = NULL, live_token = NULL, live_owner_id = NULL,
            live_cancel = 0, live_force_solve = 0,
            live_viewport_w = NULL, live_viewport_h = NULL
        WHERE id = ?
        """,
        (job_id,),
    )


def claim_live_session(conn: sqlite3.Connection, job_id: int, owner_id: int) -> bool:
    """라이브 세션을 admin 이 클레임 (입력 권한자 = 처음 연 admin). 이미 다른
    소유자면 False. 소유자 정의가 'enqueue 사용자'가 아니라 '여는 admin'이라
    enqueue 권한(archiver)과 입력 권한(admin)의 충돌이 없다."""
    cur = conn.execute(
        """
        UPDATE archive_jobs SET live_owner_id = ?
        WHERE id = ? AND needs_human_at IS NOT NULL
          AND (live_owner_id IS NULL OR live_owner_id = ?)
        """,
        (owner_id, job_id, owner_id),
    )
    return cur.rowcount == 1


def set_live_cancel(conn: sqlite3.Connection, job_id: int) -> None:
    """사람이 취소 — worker 폴링 루프가 다음 반복에 중단한다."""
    conn.execute(
        "UPDATE archive_jobs SET live_cancel = 1 WHERE id = ?", (job_id,)
    )


def set_live_force_solve(conn: sqlite3.Connection, job_id: int) -> None:
    """사람이 '확인 완료' — worker 폴링 루프가 다음 반복에 챌린지 판정과 무관하게
    현재 페이지로 강제 진행한다 (잔여 마커로 자동 판정이 안 풀리는 경우 대비)."""
    conn.execute(
        "UPDATE archive_jobs SET live_force_solve = 1 WHERE id = ?", (job_id,)
    )


def enqueue_live_command(
    conn: sqlite3.Connection, token: str, *,
    kind: str, x: int | None = None, y: int | None = None,
    key: str | None = None, delay_ms: int = 0,
) -> None:
    """라이브 입력 명령을 큐에 추가 (대시보드 → worker). seq 는 토큰 내 증가."""
    seq = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 FROM live_commands WHERE live_token = ?",
        (token,),
    ).fetchone()[0]
    conn.execute(
        """
        INSERT INTO live_commands (live_token, seq, kind, x, y, key, delay_ms, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (token, seq, kind, x, y, key, delay_ms, _utcnow()),
    )


def claim_live_commands(conn: sqlite3.Connection, token: str) -> list[sqlite3.Row]:
    """미소비 명령을 seq 순으로 가져와 소비 표시 (worker 가 재생). 단일 worker
    소비라 SELECT→UPDATE 로 충분하다."""
    rows = conn.execute(
        """
        SELECT * FROM live_commands
        WHERE live_token = ? AND consumed_at IS NULL ORDER BY seq
        """,
        (token,),
    ).fetchall()
    if rows:
        conn.execute(
            "UPDATE live_commands SET consumed_at = ? WHERE live_token = ? AND consumed_at IS NULL",
            (_utcnow(), token),
        )
    return rows


def sweep_orphan_needs_human(conn: sqlite3.Connection) -> list[int]:
    """worker 재시작 시 호출 — 살아있던 라이브 page 가 사라졌으므로 모든
    needs_human 작업의 라이브 상태를 해제하고 pending 으로 떨군다. 떨군 작업
    id 목록 반환. 같은 URL 의 활성 중복(부분 UNIQUE)은 idx_archive_jobs_active
    위반을 피하려 충돌 시 그 작업을 삭제한다."""
    jobs = conn.execute(
        "SELECT id, url FROM archive_jobs WHERE needs_human_at IS NOT NULL"
    ).fetchall()
    reset: list[int] = []
    for job in jobs:
        clear_needs_human(conn, job["id"])
        # 같은 URL 의 다른 pending/in_progress 가 있으면 중복이 되므로 이 행은 삭제
        dup = conn.execute(
            """
            SELECT 1 FROM archive_jobs
            WHERE url = ? AND id != ? AND status IN ('pending', 'in_progress')
            """,
            (job["url"], job["id"]),
        ).fetchone()
        if dup:
            conn.execute("DELETE FROM archive_jobs WHERE id = ?", (job["id"],))
        else:
            conn.execute(
                "UPDATE archive_jobs SET status = 'pending', claimed_at = NULL WHERE id = ?",
                (job["id"],),
            )
            reset.append(job["id"])
    return reset


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


def retry_failed_crawl_pages_by_ids(
    conn: sqlite3.Connection, ids: Sequence[int]
) -> int:
    """주어진 failed 크롤 페이지들을 일괄 재시도 대상으로 되돌리고, 끝난 크롤을
    다시 연다 (사이트 상세 '모두 재시도'용). 반환은 되돌린 페이지 수.

    여러 크롤에 흩어진 페이지를 한 번에 처리한다 — retry_failed_crawl_pages
    의 페이지-id 버전. 영향받은 크롤 중 끝난(done/cancelled) 것만 다시 연다.
    """
    if not ids:
        return 0
    placeholders = ",".join("?" * len(ids))
    cur = conn.execute(
        f"""
        UPDATE crawl_pages
        SET status = 'pending', attempts = 0, next_attempt_at = NULL, error = NULL
        WHERE status = 'failed' AND id IN ({placeholders})
        """,
        tuple(ids),
    )
    conn.execute(
        f"""
        UPDATE crawls SET status = 'running', finished_at = NULL, next_page_at = ?
        WHERE status IN ('done', 'cancelled')
          AND id IN (SELECT DISTINCT crawl_id FROM crawl_pages WHERE id IN ({placeholders}))
          AND EXISTS (
              SELECT 1 FROM crawl_pages cp
              WHERE cp.crawl_id = crawls.id AND cp.status IN ('pending', 'in_progress')
          )
        """,
        (_utcnow(), *ids),
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
    """사이트 소속 스냅샷의 시각·위치·용량·제목 (page_id, taken_at, domain, slug,
    dir_name, bytes, title).

    사이트 상세가 페이지별·사이트 전체 저장 용량 합산과 현재 제목 표시에 쓴다
    (bytes·title 비정규화로 파일시스템·meta.json 접근 없음).
    """
    return conn.execute(
        """
        SELECT s.page_id, s.taken_at, p.domain, p.slug, s.dir_name, s.bytes, s.title
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

# 상태/게이트 역할 — 권한 묶음이 아니라 미들웨어(web.app)가 접근을 직접
# 차단하는 상태 키워드. 커스텀 권한 그룹 대상이 아니며 삭제·프리셋 편집 불가.
# pending=권한없음(가입 승인 대기 — 안내 페이지 외 접근 불가), blocked=차단,
# withdrawn=탈퇴(본인 탈퇴로만 진입 — 로그인 거부, 관리자가 계정 정보를
# 삭제해야 같은 이메일로 다시 가입/초대할 수 있다)
STATE_ROLES = ("pending", "blocked", "withdrawn")
STATE_ROLE_LABELS = {
    "pending": "권한없음",
    "blocked": "차단됨",
    "withdrawn": "탈퇴",
}
# 빌트인 권한 보유 역할 — permission_groups 에 is_builtin=1 로 시드된다.
# 삭제·개명 불가, permissions 묶음만 편집 가능. admin 은 founder 잠김과 엮인다.
# archive_manager(아카이브 관리)=아카이브 데이터 전권(보기·아카이빙·삭제),
# archiver(아카이브)=보기·아카이빙. 둘 다 개인 API Key 사용 가능(use_api_keys).
BUILTIN_PERMISSION_ROLES = ("admin", "archive_manager", "archiver", "viewer")
# 커스텀 권한 그룹 이름으로 쓸 수 없는 예약어 — 상태 역할 + 빌트인 역할.
RESERVED_ROLE_NAMES = frozenset(STATE_ROLES + BUILTIN_PERMISSION_ROLES)

# 세분 권한 — 역할(role)은 아래 권한들의 묶음(프리셋)이고, 사용자별
# permission_overrides 로 개별 가감한다. 실효 권한 = 프리셋 ± 오버라이드.
# 모든 라우트 가드는 web.permissions.has_permission(=실효 권한)으로 판정하므로,
# 오버라이드가 한 곳에서 전 경로에 반영된다.
PERMISSIONS = (
    "view",                    # 아카이브 열람 + 전문 검색 + 아카이빙 로그 (viewer 이상)
    "archive",                 # 아카이빙 추가·재아카이브·스케줄·크롤·재시도
    "delete",                  # 스냅샷·페이지·사이트 삭제
    "manage_credentials",      # 사이트 로그인 자격증명 관리 + 자격증명 연결 아카이빙
    "manage_system",           # 시스템 설정·백업·복원·네트워크 태그·시스템 로그
    "manage_users",            # 사용자·초대·시스템 API 키 관리
    "view_authenticated_all",  # 다른 사용자가 로그인 캡처한 인증 스냅샷 열람
    "use_api_keys",            # 개인 API Key(확장 토큰) 발급·사용 (크롬 확장 캡처)
    "view_audit_logs",         # 감사 로그(/log/audit) 열람 — 기본 admin 만
    "view_system_logs",        # 시스템 로그(/log/system) 열람 — 기본 admin 만
    "view_archive_logs",       # 아카이브 로그(/log/archive) 열람 — 기본 admin 만
)
PERMISSION_LABELS = {
    "view": "보기·검색",
    "archive": "아카이빙",
    "delete": "삭제",
    "manage_credentials": "자격증명 관리",
    "manage_system": "시스템 관리",
    "manage_users": "사용자 관리",
    "view_authenticated_all": "인증 스냅샷 전체 열람",
    "use_api_keys": "개인 API Key",
    "view_audit_logs": "감사 로그 보기",
    "view_system_logs": "시스템 로그 보기",
    "view_archive_logs": "아카이브 로그 보기",
}
# 로그 열람 3종 — 신규 추가 권한. 빌트인 중 admin 에만 기본 부여하고(관리자 권한
# 그룹만 기본값), 기존 설치는 _migrate_log_view_permissions 가 보강한다.
LOG_VIEW_PERMISSIONS = ("view_audit_logs", "view_system_logs", "view_archive_logs")
# 빌트인 역할의 기본 프리셋 — permission_groups 시드의 소스이자, 캐시가 아직
# 워밍되지 않은 프로세스의 conn 없는 fallback. 런타임 편집·커스텀 그룹은 DB
# permission_groups 가 정본이며 role_presets(conn) 가 버전 비교로 최신화한다.
_BUILTIN_PRESETS: dict[str, frozenset[str]] = {
    "admin": frozenset(PERMISSIONS),
    "archive_manager": frozenset({"view", "archive", "delete", "use_api_keys"}),
    "archiver": frozenset({"view", "archive", "use_api_keys"}),
    "viewer": frozenset({"view"}),
}
PERMISSION_GROUPS_VERSION_KEY = "permission_groups_version"

# 역할 프리셋 캐시 — DB permission_groups 가 정본. settings 의 단조 버전과 비교해
# 바뀌었을 때만 재로드(멀티프로세스 staleness 방지). conn 없는 호출처
# (web.permissions → 템플릿 _auth_context)는 이 캐시를 fallback 으로 읽으므로,
# 인증 미들웨어가 요청 앞단에서 role_presets(conn) 로 워밍한다.
_PRESETS_UNSET = object()
_presets_cache: dict[str, frozenset[str]] = dict(_BUILTIN_PRESETS)
_presets_version: object = _PRESETS_UNSET
_presets_cache_path: str | None = None
_presets_lock = threading.Lock()


def _parse_permission_list(raw: str | None) -> list[str]:
    """permission_groups.permissions JSON 배열을 PERMISSIONS 부분집합으로 파싱(순서·중복 제거)."""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    out: list[str] = []
    for p in data:
        if p in PERMISSIONS and p not in out:
            out.append(p)
    return out


def role_presets(conn: sqlite3.Connection) -> dict[str, frozenset[str]]:
    """역할명→권한 프리셋 dict. DB permission_groups 가 정본.

    settings 의 단조 버전(permission_groups_version)이 캐시와 다를 때만 재로드한다.
    같은 프로세스의 후속 conn 없는 호출(effective_permissions)이 이 캐시를 읽으므로,
    인증 미들웨어가 요청 앞단에서 1회 호출해 캐시를 최신화(워밍)한다.

    캐시 유효성은 (DB 경로, 버전)으로 판정한다 — 버전은 DB 별 단조 증가라
    복원(DB 파일 교체)·테스트(임시 DB)에서 다른 DB 의 같은 버전 번호와 겹칠
    수 있어, 경로가 다르면 무조건 재로드한다.
    """
    global _presets_cache, _presets_version, _presets_cache_path
    path = str(config.DB_PATH)
    version = get_setting(conn, PERMISSION_GROUPS_VERSION_KEY)
    if path == _presets_cache_path and version == _presets_version:
        return _presets_cache
    presets: dict[str, frozenset[str]] = {}
    for row in conn.execute("SELECT name, permissions FROM permission_groups"):
        presets[row["name"]] = frozenset(_parse_permission_list(row["permissions"]))
    # 시드 전 호출 등으로 빌트인이 비어 있으면 기본 프리셋으로 보강(fail-safe).
    for name, preset in _BUILTIN_PRESETS.items():
        presets.setdefault(name, preset)
    with _presets_lock:
        _presets_cache = presets
        _presets_version = version
        _presets_cache_path = path
    return presets


def _bump_permission_groups_version(conn: sqlite3.Connection) -> None:
    """프리셋 캐시 무효화용 단조 버전 +1 — 그룹 쓰기마다 호출, 멀티프로세스 재로드 신호."""
    current = get_setting(conn, PERMISSION_GROUPS_VERSION_KEY)
    try:
        nxt = int(current) + 1 if current else 1
    except (ValueError, TypeError):
        nxt = 1
    set_setting(conn, PERMISSION_GROUPS_VERSION_KEY, str(nxt))


def parse_permission_overrides(raw: str | None) -> dict[str, bool]:
    """permission_overrides JSON 을 {권한: 허용여부} dict 로 파싱 (오염·미지정 권한은 무시)."""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: bool(v) for k, v in data.items() if k in PERMISSIONS}


def effective_permissions(
    role: str,
    overrides_raw: str | None = None,
    *,
    presets: dict[str, frozenset[str]] | None = None,
) -> frozenset[str]:
    """역할 프리셋에 사용자 오버라이드를 적용한 실효 권한 집합.

    presets 가 주어지면 그 dict(conn 으로 막 로드한 최신, 또는 편집 시뮬레이션)를
    쓰고, 없으면 모듈 프리셋 캐시를 fallback 으로 쓴다(conn 없는 web.permissions
    경로). 캐시는 인증 미들웨어가 요청 앞단에서 role_presets(conn) 로 워밍한다.
    """
    source = presets if presets is not None else _presets_cache
    perms = set(source.get(role, frozenset()))
    for perm, granted in parse_permission_overrides(overrides_raw).items():
        if granted:
            perms.add(perm)
        else:
            perms.discard(perm)
    return frozenset(perms)


def set_permission_overrides(
    conn: sqlite3.Connection, user_id: int, overrides: dict[str, bool]
) -> None:
    """사용자 세분 권한 오버라이드 저장 (프리셋과 다른 항목만). 최초 관리자는 변경 불가."""
    clean = {k: bool(v) for k, v in overrides.items() if k in PERMISSIONS}
    conn.execute(
        "UPDATE users SET permission_overrides = ? WHERE id = ? AND is_founder = 0",
        (json.dumps(clean, sort_keys=True), user_id),
    )


def count_active_users_with_permission(
    conn: sqlite3.Connection,
    permission: str,
    *,
    exclude_user_id: int | None = None,
    presets: dict[str, frozenset[str]] | None = None,
) -> int:
    """해당 권한을 실효로 가진 활성 사용자 수 — 라스트-관리자 잠김 방지용.

    활성 = 권한 보유 역할(=permission_groups 의 그룹명). STATE_ROLES(pending/blocked/
    withdrawn)는 오버라이드가 있어도 접근이 막혀 있으므로 세지 않는다. presets 를
    넘기면(그룹 권한 편집 시뮬레이션 등) 그 프리셋으로 실효 권한을 계산한다.
    """
    if presets is None:
        presets = role_presets(conn)
    active_roles = set(presets) - set(STATE_ROLES)
    n = 0
    for u in conn.execute(
        "SELECT id, role, permission_overrides FROM users"
    ).fetchall():
        if exclude_user_id is not None and u["id"] == exclude_user_id:
            continue
        if u["role"] not in active_roles:
            continue
        if permission in effective_permissions(
            u["role"], u["permission_overrides"], presets=presets
        ):
            n += 1
    return n


# ---- 권한 그룹 동적 접근자 (코드 상수였던 역할 목록을 DB 기반으로) ----

def permission_group_names(conn: sqlite3.Connection) -> tuple[str, ...]:
    """권한 보유 역할(빌트인+커스텀 그룹) 이름 — sort_order 순.

    종전의 PERMISSION_ROLES 를 대체. 세분 권한 오버라이드 조정·확장 토큰이
    허용되는 역할 집합이다.
    """
    return tuple(
        r["name"]
        for r in conn.execute(
            "SELECT name FROM permission_groups ORDER BY sort_order, name"
        )
    )


def role_labels(conn: sqlite3.Connection) -> dict[str, str]:
    """역할명→표시 라벨 (그룹 label + STATE_ROLES 라벨). 종전 ROLE_LABELS 대체."""
    labels = dict(STATE_ROLE_LABELS)
    for r in conn.execute("SELECT name, label FROM permission_groups"):
        labels[r["name"]] = r["label"]
    return labels


def assignable_roles(conn: sqlite3.Connection) -> tuple[str, ...]:
    """관리자가 부여할 수 있는 역할 — 그룹 전체 + pending/blocked (withdrawn 제외)."""
    return permission_group_names(conn) + ("pending", "blocked")


def invitable_roles(conn: sqlite3.Connection) -> tuple[str, ...]:
    """초대로 부여할 수 있는 역할 — 권한 보유 그룹 전체."""
    return permission_group_names(conn)


def signup_roles(conn: sqlite3.Connection) -> tuple[str, ...]:
    """가입 초기 권한으로 쓸 수 있는 역할 — pending + admin 외 권한 보유 그룹.

    admin 자동 가입은 막는다(종전 SIGNUP_ROLES 가 admin 을 뺀 의도 유지).
    """
    groups = tuple(n for n in permission_group_names(conn) if n != "admin")
    return ("pending",) + groups


def all_valid_roles(conn: sqlite3.Connection) -> frozenset[str]:
    """users.role 에 저장 가능한 모든 역할 — 권한 보유 그룹 + 상태 역할."""
    return frozenset(permission_group_names(conn)) | frozenset(STATE_ROLES)


# ---- 권한 그룹 CRUD (쓰기는 버전 스탬프로 캐시 무효화) ----

_GROUP_NAME_RE = re.compile(r"^[a-z0-9_]{1,32}$")


def normalize_group_name(raw: str) -> str:
    """그룹 이름 정규화·검증 — 영문 소문자·숫자·밑줄 1~32자, 예약어 금지.

    공백·하이픈은 밑줄로, 대문자는 소문자로 접는다. 형식 위반·예약어면 ValueError.
    `.badge.role-<name>` CSS 합성에 안전한 문자만 허용한다.
    """
    name = (raw or "").strip().lower().replace(" ", "_").replace("-", "_")
    if not _GROUP_NAME_RE.match(name):
        raise ValueError("이름은 영문 소문자·숫자·밑줄 1~32자여야 합니다.")
    if name in RESERVED_ROLE_NAMES:
        raise ValueError(f"예약된 이름입니다: {name}")
    return name


def get_permission_group(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    """그룹 1행 (없으면 None)."""
    return conn.execute(
        "SELECT * FROM permission_groups WHERE name = ?", (name,)
    ).fetchone()


def list_permission_groups(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """모든 그룹 — sort_order 순(화면 목록용)."""
    return conn.execute(
        "SELECT * FROM permission_groups ORDER BY sort_order, name"
    ).fetchall()


def count_users_with_role(conn: sqlite3.Connection, role: str) -> int:
    """해당 역할(그룹)에 속한 사용자 수 — 그룹 삭제 가드."""
    return conn.execute(
        "SELECT COUNT(*) FROM users WHERE role = ?", (role,)
    ).fetchone()[0]


def create_permission_group(
    conn: sqlite3.Connection, name: str, label: str, permissions: Sequence[str]
) -> str:
    """커스텀 권한 그룹 생성 — name 정규화·중복 검사, permissions 는 PERMISSIONS 부분집합.

    반환: 저장된 정규화 name. 형식 위반·예약어·중복이면 ValueError.
    """
    name = normalize_group_name(name)
    if get_permission_group(conn, name) is not None:
        raise ValueError(f"이미 있는 그룹입니다: {name}")
    label = (label or "").strip() or name
    perms = [p for p in permissions if p in PERMISSIONS]
    order = conn.execute(
        "SELECT COALESCE(MAX(sort_order), 100) + 10 FROM permission_groups"
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO permission_groups "
        "(name, label, permissions, is_builtin, sort_order, created_at) "
        "VALUES (?, ?, ?, 0, ?, ?)",
        (name, label, json.dumps(perms), order, _utcnow()),
    )
    _bump_permission_groups_version(conn)
    return name


def update_permission_group(
    conn: sqlite3.Connection,
    name: str,
    *,
    label: str | None = None,
    permissions: Sequence[str] | None = None,
) -> bool:
    """그룹 권한·라벨 갱신. 빌트인은 permissions 만 바뀌고 label 은 잠긴다.

    반환: 갱신 성공 여부(그룹이 없으면 False).
    """
    group = get_permission_group(conn, name)
    if group is None:
        return False
    if permissions is None:
        new_perms = group["permissions"]
    else:
        new_perms = json.dumps([p for p in permissions if p in PERMISSIONS])
    if group["is_builtin"] or label is None:
        new_label = group["label"]  # 빌트인 라벨 잠금 / 미지정이면 유지
    else:
        new_label = (label or "").strip() or group["label"]
    conn.execute(
        "UPDATE permission_groups SET label = ?, permissions = ? WHERE name = ?",
        (new_label, new_perms, name),
    )
    _bump_permission_groups_version(conn)
    return True


def delete_permission_group(conn: sqlite3.Connection, name: str) -> bool:
    """커스텀 그룹 삭제. 빌트인이면 False. 호출부가 소속 사용자 0 을 먼저 보장한다.

    반환: 삭제 성공 여부.
    """
    group = get_permission_group(conn, name)
    if group is None or group["is_builtin"]:
        return False
    conn.execute(
        "DELETE FROM permission_groups WHERE name = ? AND is_builtin = 0", (name,)
    )
    _bump_permission_groups_version(conn)
    return True


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
    if role not in all_valid_roles(conn):
        raise ValueError(f"알 수 없는 역할: {role!r}")
    cur = conn.execute(
        "INSERT INTO users (email, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
        (email, password_hash, role, _utcnow()),
    )
    return cur.lastrowid


def count_users(conn: sqlite3.Connection) -> int:
    """전체 사용자 수 (0 이면 최초 구동으로 판단)."""
    return conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]


def first_run_needed(conn: sqlite3.Connection) -> bool:
    """최초 구동(사용자 0명) 여부 — 매 요청 COUNT(*) 를 피하려 프로세스 전역 래치.

    auth_gate 가 모든 요청에서 호출한다. 사용자가 한 명이라도 생기면 영원히
    0 이 아니므로, 한 번 확인하면 래치해 이후 COUNT(*) 를 건너뛴다. 복원으로
    DB 가 비는 경로는 invalidate_schema_cache() 가 래치를 함께 풀어 보존한다.
    """
    global _users_exist_latch
    if _users_exist_latch:
        return False
    if count_users(conn) > 0:
        _users_exist_latch = True
        return False
    return True


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
    if role not in all_valid_roles(conn):
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
    """사용자와 종속 데이터(세션·OIDC 연결·패스키·확장 토큰)를 일괄 삭제.

    FK(foreign_keys=ON)가 강제되므로 users 행을 지우기 전에 참조를 정리한다.
    본인 귀속 확장 토큰은 함께 폐기하고, 발급자(created_by)로만 참조되는
    시스템 키는 보존하되 발급자 표기만 끊는다(기록용 컬럼).
    """
    conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM identities WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM webauthn_credentials WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM email_verifications WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM api_keys WHERE owner_user_id = ?", (user_id,))
    conn.execute(
        "UPDATE api_keys SET created_by = NULL WHERE created_by = ?", (user_id,)
    )
    # 로그인 캡처 스냅샷의 소유자 표기는 NULL 로 끊는다 — 불변 기록은 보존하되
    # 끊으면 _may_view_authenticated 가 admin 전용으로 좁아져 접근이 넓어지지 않는다
    conn.execute(
        "UPDATE snapshots SET authenticated_by = NULL WHERE authenticated_by = ?",
        (user_id,),
    )
    # 아카이브 실행 이력(로그)·대기 작업의 요청자 표기는 NULL 로 끊는다 — 기록은
    # 보존하되 FK 만 해제한다 (지워진 사용자의 '내 아카이브'는 더는 의미 없음)
    conn.execute(
        "UPDATE archive_logs SET requested_by = NULL WHERE requested_by = ?",
        (user_id,),
    )
    conn.execute(
        "UPDATE archive_jobs SET requested_by = NULL WHERE requested_by = ?",
        (user_id,),
    )
    conn.execute(
        "UPDATE crawls SET requested_by = NULL WHERE requested_by = ?",
        (user_id,),
    )
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
# 초대로 부여할 수 있는 역할은 db.invitable_roles(conn) (권한 보유 그룹 전체).


def create_invite(
    conn: sqlite3.Connection,
    email: str,
    token_hash: str,
    role: str,
    invited_by: int | None,
    ttl_seconds: int,
) -> int:
    """초대 생성 후 id 반환. 같은 이메일의 기존 초대는 교체된다 (이전 링크 무효화)."""
    if role not in invitable_roles(conn):
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

# 이메일 본인 인증 — 켜면 패스워드 가입·로그인 시 메일로 받은 코드로 이메일을
# 검증한다 (SMTP 미설정이면 동작하지 않음, SSO 계정은 IdP 가 검증하므로 제외).
# 코드 만료 시간은 분 단위로 설정한다. 해석·클램핑은 email_verification_ttl_minutes.
EMAIL_VERIFICATION_ENABLED_KEY = "email_verification_enabled"        # 'on' | 'off' (기본 off)
EMAIL_VERIFICATION_TTL_MINUTES_KEY = "email_verification_ttl_minutes"  # 분 (기본·오염 시 config 기본값)

# 인증 무차별 대입 방어(rate limit). 한도·창은 시스템 설정에서 조정하며 값
# 해석·클램핑은 db.auth_throttle_settings 가 맡는다 (오염·범위 밖이면 config
# 기본값). 전체 토글이 off 면 throttle 검사를 건너뛴다. (CLAUDE.md 원칙 5·6 보강)
AUTH_THROTTLE_ENABLED_KEY = "auth_throttle_enabled"          # 'on' | 'off' (기본 on)
AUTH_LOGIN_LIMIT_KEY = "auth_login_limit"                    # 이메일별 로그인 시도 한도/창
AUTH_LOGIN_IP_LIMIT_KEY = "auth_login_ip_limit"             # IP별 로그인 시도 한도/창
AUTH_LOGIN_WINDOW_MINUTES_KEY = "auth_login_window_minutes"  # 로그인 카운트 창(분)
AUTH_TOTP_LIMIT_KEY = "auth_totp_limit"                      # 2단계(TOTP·패스키) 시도 한도/창
AUTH_EMAIL_VERIFY_LIMIT_KEY = "auth_email_verify_limit"      # 이메일 코드 오답 한도(초과 시 폐기)
AUTH_EMAIL_RESEND_LIMIT_KEY = "auth_email_resend_limit"      # 코드 재발송 시간당 한도

# 사이트 전체 아카이브 기본 옵션·실패 재시도 대기. 값 해석과 범위 검증은
# crawler.crawl_defaults / crawler.retry_backoff 가 맡는다 (오염 시 config 기본값).
CRAWL_DEFAULT_MAX_PAGES_KEY = "crawl_default_max_pages"
CRAWL_DEFAULT_MAX_DEPTH_KEY = "crawl_default_max_depth"
CRAWL_DEFAULT_DELAY_KEY = "crawl_default_delay_seconds"
CRAWL_RETRY_BACKOFF_KEY = "crawl_retry_backoff_seconds"  # 쉼표 구분 초 목록 (예: '300,900')

# 링크된 문서 파일 아카이브 한도. 값 해석·클램핑은 documents.limits 가 맡는다
# (오염·범위 밖이면 config 기본값). max_mb 는 MB 단위 정수로 저장한다.
DOCUMENT_MAX_COUNT_KEY = "document_max_count"
DOCUMENT_MAX_MB_KEY = "document_max_mb"
DOCUMENT_FETCH_TIMEOUT_KEY = "document_fetch_timeout_seconds"

# 초대 메일 발송 SMTP 설정. 값 해석·환경변수 폴백·비밀번호 복호화는
# mailer.resolve_config 가 맡는다 (여기 저장된 값이 WCCG_SMTP_* 환경변수보다
# 우선). 비밀번호는 대칭 암호화한 암호문만 저장한다 (CLAUDE.md 원칙 6 예외 —
# 외부 SMTP 서버에 replay 해야 하므로 복원 가능, crypto.encrypt).
SMTP_HOST_KEY = "smtp_host"
SMTP_PORT_KEY = "smtp_port"
SMTP_USER_KEY = "smtp_user"
SMTP_PASSWORD_KEY = "smtp_password"  # crypto.encrypt 암호문 (평문·해시 금지)
SMTP_FROM_KEY = "smtp_from"
SMTP_TLS_KEY = "smtp_tls"  # 'starttls' | 'ssl' | 'off'

# 춘추관 간 데이터 이전(마이그레이션). 소스(보내는 쪽)가 이전 모드를 켜면
# 인증 토큰을 발급하고, 그 동안 모든 스크래핑·스케줄·크롤이 중단된다.
# 받는 쪽은 토큰으로 소스의 /api/migration/* 에서 전체 데이터를 Pull 한다.
# 토큰은 세션·API 키와 같이 SHA-256 해시만 저장한다 (원칙 6 단방향).
MIGRATION_MODE_KEY = "migration_mode"               # 'on' | 'off' (기본 off)
MIGRATION_TOKEN_HASH_KEY = "migration_token_hash"   # 발급 토큰의 SHA-256 (모드 끄면 삭제)
MIGRATION_TOKEN_CREATED_AT_KEY = "migration_token_created_at"  # 발급 시각 (표시용)

# 회원 가입(셀프 가입·SSO 자동 생성)으로 만든 계정에 부여할 수 있는 초기 권한은
# db.signup_roles(conn) (pending + admin 외 권한 보유 그룹). 기본은 권한없음
# (pending) — 관리자가 사용자 관리에서 승인(권한 변경)해야 한다.


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


def delete_setting(conn: sqlite3.Connection, key: str) -> None:
    """설정 값 삭제 (없으면 무시) — 기본값/환경변수 폴백으로 되돌린다."""
    conn.execute("DELETE FROM settings WHERE key = ?", (key,))


def signup_enabled(conn: sqlite3.Connection) -> bool:
    """로그인 화면의 회원 가입 허용 여부 (기본 허용)."""
    return get_setting(conn, SIGNUP_ENABLED_KEY) != "off"


def signup_default_role(conn: sqlite3.Connection) -> str:
    """회원 가입으로 생성되는 계정의 초기 권한 (기본·값 오염·삭제된 그룹이면 pending)."""
    role = get_setting(conn, SIGNUP_DEFAULT_ROLE_KEY)
    return role if role in signup_roles(conn) else "pending"


def email_verification_enabled(conn: sqlite3.Connection) -> bool:
    """이메일 본인 인증 사용 여부 (기본 off — 옵트인). SMTP 미설정이면 호출부가 무시한다."""
    return get_setting(conn, EMAIL_VERIFICATION_ENABLED_KEY) == "on"


def migration_mode_enabled(conn: sqlite3.Connection) -> bool:
    """이전(마이그레이션) 모드 여부 (기본 off). 켜진 동안 스크래핑·스케줄·크롤 중단."""
    return get_setting(conn, MIGRATION_MODE_KEY) == "on"


def get_migration_token_hash(conn: sqlite3.Connection) -> str | None:
    """발급된 이전 토큰의 SHA-256 해시 (없으면 None)."""
    return get_setting(conn, MIGRATION_TOKEN_HASH_KEY)


def set_migration_mode(
    conn: sqlite3.Connection, on: bool, token_hash: str | None = None
) -> None:
    """이전 모드를 켜고/끈다. 켜면 토큰 해시를 저장하고, 끄면 토큰을 무효화(삭제)한다."""
    if on:
        set_setting(conn, MIGRATION_MODE_KEY, "on")
        if token_hash is not None:
            set_setting(conn, MIGRATION_TOKEN_HASH_KEY, token_hash)
            set_setting(conn, MIGRATION_TOKEN_CREATED_AT_KEY, _utcnow())
    else:
        set_setting(conn, MIGRATION_MODE_KEY, "off")
        delete_setting(conn, MIGRATION_TOKEN_HASH_KEY)
        delete_setting(conn, MIGRATION_TOKEN_CREATED_AT_KEY)


def email_verification_ttl_minutes(conn: sqlite3.Connection) -> int:
    """이메일 인증 코드 만료 시간(분) — 기본·오염·범위 밖이면 config 기본값으로 클램핑."""
    raw = get_setting(conn, EMAIL_VERIFICATION_TTL_MINUTES_KEY)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return config.EMAIL_VERIFICATION_TTL_MINUTES_DEFAULT
    return max(
        config.EMAIL_VERIFICATION_TTL_MINUTES_MIN,
        min(config.EMAIL_VERIFICATION_TTL_MINUTES_MAX, value),
    )


# ---- 인증 rate limit (무차별 대입 방어) ----


def auth_throttle_enabled(conn: sqlite3.Connection) -> bool:
    """인증 시도 rate limit 전체 사용 여부 (기본 on)."""
    return get_setting(conn, AUTH_THROTTLE_ENABLED_KEY) != "off"


def _clamped_int_setting(conn: sqlite3.Connection, key: str, default: int,
                         lo: int, hi: int) -> int:
    """정수 설정값을 [lo, hi] 로 클램핑 (없거나 오염이면 default)."""
    raw = get_setting(conn, key)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, value))


def auth_throttle_settings(conn: sqlite3.Connection) -> dict[str, int]:
    """인증 rate limit 한도·창 (시스템 설정, 오염·범위 밖이면 config 기본값 클램핑)."""
    lo, hi = config.AUTH_THROTTLE_LIMIT_MIN, config.AUTH_THROTTLE_LIMIT_MAX
    return {
        "login_limit": _clamped_int_setting(
            conn, AUTH_LOGIN_LIMIT_KEY, config.AUTH_LOGIN_LIMIT_DEFAULT, lo, hi),
        "login_ip_limit": _clamped_int_setting(
            conn, AUTH_LOGIN_IP_LIMIT_KEY, config.AUTH_LOGIN_IP_LIMIT_DEFAULT, lo, hi),
        "login_window_minutes": _clamped_int_setting(
            conn, AUTH_LOGIN_WINDOW_MINUTES_KEY,
            config.AUTH_LOGIN_WINDOW_MINUTES_DEFAULT,
            config.AUTH_THROTTLE_WINDOW_MIN, config.AUTH_THROTTLE_WINDOW_MAX),
        "totp_limit": _clamped_int_setting(
            conn, AUTH_TOTP_LIMIT_KEY, config.AUTH_TOTP_LIMIT_DEFAULT, lo, hi),
        "email_verify_limit": _clamped_int_setting(
            conn, AUTH_EMAIL_VERIFY_LIMIT_KEY,
            config.AUTH_EMAIL_VERIFY_LIMIT_DEFAULT, lo, hi),
        "email_resend_limit": _clamped_int_setting(
            conn, AUTH_EMAIL_RESEND_LIMIT_KEY,
            config.AUTH_EMAIL_RESEND_LIMIT_DEFAULT, lo, hi),
    }


def throttle_hit(
    conn: sqlite3.Connection, bucket: str, key: str, limit: int, window_seconds: int
) -> tuple[bool, int]:
    """고정 윈도우 카운터를 1 증가시키고 (허용 여부, 재시도까지 남은 초)를 반환.

    현재 윈도우가 만료됐으면 리셋(count=1)하고, 아니면 count+1 한다. 증가 후
    count 가 limit 을 초과하면 (False, 윈도우 종료까지 남은 초)로 차단을 알린다.
    UPSERT 로 원자적이며, 같은 (bucket, key) 동시 호출은 DB 락으로 직렬화된다.
    """
    now = datetime.now(timezone.utc)
    row = conn.execute(
        "SELECT window_start, count FROM auth_throttle WHERE bucket = ? AND key = ?",
        (bucket, key),
    ).fetchone()
    if row is not None:
        start = datetime.fromisoformat(row["window_start"])
        elapsed = (now - start).total_seconds()
        if elapsed < window_seconds:
            count = row["count"] + 1
            conn.execute(
                "UPDATE auth_throttle SET count = ? WHERE bucket = ? AND key = ?",
                (count, bucket, key),
            )
            if count > limit:
                return False, max(1, int(window_seconds - elapsed))
            return True, 0
    # 신규이거나 윈도우 만료 — 새 윈도우 시작
    conn.execute(
        """
        INSERT INTO auth_throttle (bucket, key, window_start, count)
        VALUES (?, ?, ?, 1)
        ON CONFLICT(bucket, key) DO UPDATE SET window_start = excluded.window_start,
                                               count = excluded.count
        """,
        (bucket, key, now.isoformat(timespec="seconds")),
    )
    if 1 > limit:  # limit 0 (사실상 차단) 방어
        return False, window_seconds
    return True, 0


def throttle_clear(conn: sqlite3.Connection, bucket: str, key: str) -> None:
    """카운터 삭제 — 인증 성공 시 호출해 정상 사용자가 누적 차단되지 않게 한다."""
    conn.execute(
        "DELETE FROM auth_throttle WHERE bucket = ? AND key = ?", (bucket, key)
    )


def delete_expired_throttle(conn: sqlite3.Connection, max_age_seconds: int) -> None:
    """오래된 throttle 행 정리 (기회적 GC) — window_start 가 max_age 보다 오래된 행."""
    cutoff = (
        datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)
    ).isoformat(timespec="seconds")
    conn.execute("DELETE FROM auth_throttle WHERE window_start <= ?", (cutoff,))


def set_email_verified(conn: sqlite3.Connection, user_id: int) -> None:
    """이메일 본인 인증 완료 표시."""
    conn.execute(
        "UPDATE users SET email_verified = 1 WHERE id = ?", (user_id,)
    )


def create_email_verification(
    conn: sqlite3.Connection, user_id: int, code_hash: str, ttl_seconds: int
) -> None:
    """이메일 인증 코드 발급 — 사용자당 1개, 재발송 시 교체. 해시만 저장한다."""
    conn.execute(
        """
        INSERT INTO email_verifications (user_id, code_hash, created_at, expires_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET code_hash = excluded.code_hash,
                                           created_at = excluded.created_at,
                                           expires_at = excluded.expires_at
        """,
        (user_id, code_hash, _utcnow(), _later(ttl_seconds)),
    )


def get_email_verification(
    conn: sqlite3.Connection, user_id: int
) -> sqlite3.Row | None:
    """만료되지 않은 이메일 인증 코드 행 조회 (없거나 만료면 None)."""
    return conn.execute(
        "SELECT * FROM email_verifications WHERE user_id = ? AND expires_at > ?",
        (user_id, _utcnow()),
    ).fetchone()


def delete_email_verification(conn: sqlite3.Connection, user_id: int) -> None:
    """이메일 인증 코드 삭제 (인증 완료·취소)."""
    conn.execute(
        "DELETE FROM email_verifications WHERE user_id = ?", (user_id,)
    )


MOBILE_SCREENSHOT_ENABLED_KEY = "mobile_screenshot_enabled"  # 'on' | 'off' (기본 off)


def mobile_screenshot_enabled(conn: sqlite3.Connection) -> bool:
    """모바일 해상도 스크린샷도 함께 저장할지 (기본 off — 옵트인).

    켜면 캡처가 데스크탑 스크린샷 외에, 안드로이드 크롬으로 위장한 모바일
    컨텍스트(config.MOBILE_SCREENSHOT_*)로 같은 URL 을 한 번 더 열어 스크린샷을
    찍는다 (capture._capture_mobile_screenshot).
    """
    return get_setting(conn, MOBILE_SCREENSHOT_ENABLED_KEY) == "on"


EXT_CREDENTIAL_TTL_HOURS_KEY = "ext_credential_ttl_hours"


def ext_credential_ttl_hours(conn: sqlite3.Connection) -> int:
    """확장 1회성 세션 자격증명의 만료 안전망 TTL(시간) — 기본·오염 시 config 기본값."""
    raw = get_setting(conn, EXT_CREDENTIAL_TTL_HOURS_KEY)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return config.EXT_CREDENTIAL_TTL_HOURS_DEFAULT
    return max(
        config.EXT_CREDENTIAL_TTL_HOURS_MIN,
        min(config.EXT_CREDENTIAL_TTL_HOURS_MAX, value),
    )


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
    owner_user_id: int | None = None,
) -> int:
    """API 키 row 생성 후 id 반환. ttl_seconds=None 이면 영구 키.

    owner_user_id=None 이면 관리자 발급 시스템 키, 값이 있으면 그 사용자에게
    귀속된 확장 토큰(권한은 _api_auth 가 소유자 현재 역할로 매 요청 재평가).
    """
    expires_at = _later(ttl_seconds) if ttl_seconds is not None else None
    cur = conn.execute(
        """
        INSERT INTO api_keys
            (name, token_hash, prefix, can_view, can_archive,
             created_by, created_at, expires_at, owner_user_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (name, token_hash, prefix, int(can_view), int(can_archive),
         created_by, _utcnow(), expires_at, owner_user_id),
    )
    return cur.lastrowid


def get_api_key(conn: sqlite3.Connection, key_id: int) -> sqlite3.Row | None:
    """API 키 단건 조회 (만료 여부 무관 — 폐기·소유 검증용). 없으면 None."""
    return conn.execute(
        "SELECT * FROM api_keys WHERE id = ?", (key_id,)
    ).fetchone()


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


def list_system_api_keys(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """관리자 발급 시스템 키만 (owner_user_id IS NULL) — /system/api-keys 화면용.

    사용자 귀속 확장 토큰(owner 값 보유)은 제외해 두 관리 영역을 분리한다.
    """
    return conn.execute(
        """
        SELECT k.*, u.email AS creator_email,
               (k.expires_at IS NOT NULL AND k.expires_at <= ?) AS expired
        FROM api_keys k LEFT JOIN users u ON u.id = k.created_by
        WHERE k.owner_user_id IS NULL
        ORDER BY k.created_at, k.id
        """,
        (_utcnow(),),
    ).fetchall()


def list_api_keys_for_owner(
    conn: sqlite3.Connection, user_id: int
) -> list[sqlite3.Row]:
    """특정 사용자에게 귀속된 확장 토큰 목록 — 계정 설정 화면용. 만료분 포함."""
    return conn.execute(
        """
        SELECT k.*,
               (k.expires_at IS NOT NULL AND k.expires_at <= ?) AS expired
        FROM api_keys k
        WHERE k.owner_user_id = ?
        ORDER BY k.created_at, k.id
        """,
        (_utcnow(), user_id),
    ).fetchall()


def touch_api_key(conn: sqlite3.Connection, key_id: int) -> None:
    """API 키 마지막 사용 시각 갱신 — 스로틀 창 이내면 생략(조건부 UPDATE).

    읽기 API 폴링(확장 버전 체크·상태 조회 등)이 매 요청 쓰기 트랜잭션을
    일으키지 않게, last_used_at 이 config.API_KEY_TOUCH_THROTTLE_SECONDS 이내면
    행을 건드리지 않는다. last_used_at 은 표시용 근사값이라 이 정도 지연은 무방.
    """
    cutoff = (
        datetime.now(timezone.utc)
        - timedelta(seconds=config.API_KEY_TOUCH_THROTTLE_SECONDS)
    ).isoformat(timespec="seconds")
    conn.execute(
        "UPDATE api_keys SET last_used_at = ? WHERE id = ? "
        "AND (last_used_at IS NULL OR last_used_at < ?)",
        (_utcnow(), key_id, cutoff),
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
    ttl_seconds: int | None = None,
) -> int:
    """사이트 자격증명 row 생성 후 id 반환. secret 은 암호문(평문 금지).

    같은 사이트 안에서 label 은 UNIQUE — 호출부가 중복을 먼저 검사한다.
    ttl_seconds 가 있으면 expires_at 이 설정된 1회성(확장) 자격증명이 된다 —
    캡처 직후 삭제되고, 누락분은 delete_expired_ext_credentials 가 정리한다.
    """
    now = _utcnow()
    expires_at = _later(ttl_seconds) if ttl_seconds is not None else None
    cur = conn.execute(
        """
        INSERT INTO site_credentials
            (site_id, label, kind, secret, created_by, created_at, updated_at,
             expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (site_id, label, kind, secret, created_by, now, now, expires_at),
    )
    return cur.lastrowid


def delete_ephemeral_credential(conn: sqlite3.Connection, cred_id: int) -> None:
    """1회성(expires_at 보유) 자격증명을 즉시 폐기 (캡처 소비 직후).

    영속 자격증명(expires_at IS NULL)은 건드리지 않는다. 참조(pages/crawls/
    crawl_schedules)는 1회성이라 보통 없지만, 방어적으로 NULL 로 끊는다.
    """
    row = conn.execute(
        "SELECT expires_at FROM site_credentials WHERE id = ?", (cred_id,)
    ).fetchone()
    if row is None or row["expires_at"] is None:
        return
    for table in ("pages", "crawls", "crawl_schedules"):
        conn.execute(
            f"UPDATE {table} SET credential_id = NULL WHERE credential_id = ?",
            (cred_id,),
        )
    conn.execute("DELETE FROM site_credentials WHERE id = ?", (cred_id,))


def delete_expired_ext_credentials(conn: sqlite3.Connection) -> int:
    """만료된 1회성(확장) 자격증명을 정리 (삭제 누락 안전망 GC). 삭제 행 수 반환.

    아직 처리되지 않은 작업이 참조 중인 행은 건드리지 않는다 — 큐에 남은
    단발 작업(archive_jobs)뿐 아니라 진행 중 크롤(crawls.status='running')도
    보호한다. 크롤은 여러 페이지를 시간차로 캡처하므로, 만료(기본 24h)가
    크롤 도중에 와도 인증이 끊기지 않게 한다. 참조 없는 만료분만 지운다.
    """
    rows = conn.execute(
        """
        SELECT id FROM site_credentials
        WHERE expires_at IS NOT NULL AND expires_at <= ?
          AND id NOT IN (
              SELECT credential_id FROM archive_jobs WHERE credential_id IS NOT NULL
          )
          AND id NOT IN (
              SELECT credential_id FROM crawls
              WHERE status = 'running' AND credential_id IS NOT NULL
          )
        """,
        (_utcnow(),),
    ).fetchall()
    for row in rows:
        for table in ("pages", "crawls", "crawl_schedules"):
            conn.execute(
                f"UPDATE {table} SET credential_id = NULL WHERE credential_id = ?",
                (row["id"],),
            )
        conn.execute("DELETE FROM site_credentials WHERE id = ?", (row["id"],))
    return len(rows)


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


def set_session_state(
    conn: sqlite3.Connection, token_hash: str, state: str, ttl_seconds: int
) -> None:
    """세션 상태를 바꾸고 만료를 갱신 (예: 2FA 통과 → pending_email_verify)."""
    conn.execute(
        "UPDATE sessions SET state = ?, expires_at = ? WHERE token_hash = ?",
        (state, _later(ttl_seconds), token_hash),
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
