"""전역 설정. 환경변수 WCCG_ROOT(아카이브 위치), WCCG_HOST(대시보드 바인딩) 오버라이드 가능."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

ARCHIVE_ROOT = Path(os.environ.get("WCCG_ROOT", "archive")).resolve()
SITES_DIR = ARCHIVE_ROOT / "sites"
DB_PATH = ARCHIVE_ROOT / "index.db"
CACHE_DIR = ARCHIVE_ROOT / "cache"          # 파생 산출물(픽셀 diff 등), 재생성 가능
RULES_PATH = ARCHIVE_ROOT / "rules.json"    # 도메인별 정규화 룰
RESOURCES_DIR = ARCHIVE_ROOT / "resources"  # 스냅샷 간 공유 자원 CAS (resources.py)
DOCUMENTS_DIR = ARCHIVE_ROOT / "documents"  # 문서 파일 CAS (documents.py — 인증 라우트 전용)

PAGE_LOAD_TIMEOUT_MS = 30_000
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"

# ---- 저장 압축 (resources.py) ----
SCREENSHOT_WEBP_QUALITY = 85    # 스크린샷 PNG → WebP 변환 품질 (손실 압축)
RESOURCE_MIN_BYTES = 4096       # 이보다 작은 data URI 자원은 추출하지 않고 인라인 유지

# ---- 링크된 문서 파일 아카이빙 (documents.py) ----
# 페이지가 링크한 문서(PDF·워드·한글 등)를 문서 CAS(documents/)에 저장하고
# 스냅샷은 snapshot_documents 행과 meta.json 의 documents 목록으로 참조한다.
DOCUMENT_EXTENSIONS = (
    ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx",
    ".hwp", ".hwpx", ".odt", ".odp", ".ods", ".rtf",
    ".pages", ".key", ".numbers", ".epub",
)
DOCUMENT_MAX_COUNT = 20                     # 스냅샷당 문서 수 한도
DOCUMENT_MAX_BYTES = 50 * 1024 * 1024       # 문서 1개 크기 한도 (50MB)
DOCUMENT_FETCH_TIMEOUT_SECONDS = 30

# URL 정규화 시 제거할 트래킹 파라미터 prefix
TRACKING_PARAM_PREFIXES = ("utm_", "fbclid", "gclid", "igshid", "ref_src")

# ---- 사이트 전체 아카이브 (crawler.py) ----
# CRAWL_DEFAULT_* 와 CRAWL_RETRY_BACKOFF_SECONDS 는 시스템 설정(settings 테이블,
# 대시보드 시스템 화면)으로 오버라이드된다 — crawler.crawl_defaults / retry_backoff 참조.
CRAWL_DEFAULT_MAX_PAGES = 100
CRAWL_MAX_PAGES_LIMIT = 2000
CRAWL_DEFAULT_MAX_DEPTH = 5
CRAWL_MAX_DEPTH_LIMIT = 20
CRAWL_DEFAULT_DELAY_SECONDS = 5      # 페이지 간 최소 간격 (대상 서버 부담 방지)
CRAWL_MIN_DELAY_SECONDS = 1
CRAWL_MAX_DELAY_SECONDS = 3600
CRAWL_RETRY_BACKOFF_SECONDS = (300, 900)   # n차 실패 후 재시도 대기 — 최대 시도 = 길이 + 1
CRAWL_RETRY_BACKOFF_MIN_SECONDS = 10       # 재시도 대기 항목별 허용 범위 (설정 검증용)
CRAWL_RETRY_BACKOFF_MAX_SECONDS = 86400
CRAWL_RETRY_BACKOFF_MAX_STEPS = 5          # 대기 항목 수 한도 — 최대 시도 6회
CRAWL_STALE_CLAIM_SECONDS = 600      # 이보다 오래된 in_progress 는 중단으로 보고 복구
CRAWLER_POLL_SECONDS = 2             # serve·워커 크롤러 폴링 간격

# ---- 아카이빙 워커 (`wccg worker`, worker.py) ----
# 크롤 스레드 수 = 동시에 진행되는 크롤(사이트) 수. 같은 크롤은 스레드가
# 몇 개든 한 번에 한 페이지만 처리된다 (db.claim_due_crawl_page).
CRAWL_WORKERS = int(os.environ.get("WCCG_CRAWL_WORKERS", "2"))
CRAWL_WORKERS_LIMIT = 8

# 기본 127.0.0.1 (localhost 전용). 컨테이너 등에서만 WCCG_HOST=0.0.0.0 으로 오버라이드.
DASHBOARD_HOST = os.environ.get("WCCG_HOST", "127.0.0.1")
DASHBOARD_PORT = 8765

# ---- 스케줄러 (주기적 재아카이빙) ----
# 대시보드(serve) 프로세스 안에서 폴링 스레드로 동작한다.
# off 면 serve 는 스케줄을 실행하지 않는다 — cron 의 `wccg schedule run` 으로 대체 가능.
SCHEDULER_ENABLED = os.environ.get("WCCG_SCHEDULER", "on") != "off"
SCHEDULER_POLL_SECONDS = 60

# ---- 인증 ----
# WCCG_AUTH=off 는 loopback 바인딩일 때만 허용 (cli.serve 에서 강제)
AUTH_ENABLED = os.environ.get("WCCG_AUTH", "on") != "off"

# 최초 구동(사용자 0명) 시 자동 등록할 관리자. 미설정이면 /setup 페이지로 유도.
ADMIN_EMAIL = os.environ.get("WCCG_ADMIN_EMAIL", "").strip()
ADMIN_PASSWORD = os.environ.get("WCCG_ADMIN_PASSWORD", "")
SESSION_TTL_DAYS = int(os.environ.get("WCCG_SESSION_TTL_DAYS", "14"))
SESSION_COOKIE = "wccg_session"
TOTP_ISSUER = "ChunChuGwan"
PENDING_TOTP_TTL_SECONDS = 600          # 패스워드 통과 후 OTP 입력 제한 시간
MIN_PASSWORD_LENGTH = 8

# 외부 노출 시 공개 URL (OIDC redirect_uri 조립, https 면 Secure 쿠키)
PUBLIC_URL = os.environ.get("WCCG_PUBLIC_URL", "").rstrip("/")
COOKIE_SECURE = PUBLIC_URL.startswith("https://")

# ---- 패스키 (WebAuthn) ----
# RP ID 는 도메인이어야 한다. PUBLIC_URL 미설정 시 localhost 로 동작 —
# 이 경우 http://localhost:8765 접속에서만 패스키를 쓸 수 있다 (127.0.0.1 불가).
WEBAUTHN_RP_ID = (urlsplit(PUBLIC_URL).hostname or "localhost") if PUBLIC_URL else "localhost"
WEBAUTHN_RP_NAME = "ChunChuGwan"
WEBAUTHN_ORIGINS = [PUBLIC_URL] if PUBLIC_URL else [f"http://localhost:{DASHBOARD_PORT}"]

# ---- 메일 (초대 발송) ----
# WCCG_SMTP_HOST 가 설정되면 초대 메일을 발송한다. 미설정 시 초대 링크를
# 화면에 표시해 관리자가 직접 전달한다.
SMTP_HOST = os.environ.get("WCCG_SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("WCCG_SMTP_PORT", "587"))
SMTP_USER = os.environ.get("WCCG_SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("WCCG_SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("WCCG_SMTP_FROM", "") or SMTP_USER
SMTP_TLS = os.environ.get("WCCG_SMTP_TLS", "starttls")  # starttls | ssl | off
SMTP_TIMEOUT_SECONDS = 10
INVITE_TTL_DAYS = int(os.environ.get("WCCG_INVITE_TTL_DAYS", "7"))


def mail_enabled() -> bool:
    """메일 발송 설정이 채워졌는지 (테스트에서 monkeypatch 가능하도록 함수)."""
    return bool(SMTP_HOST)


# ---- OIDC (Authentik) ----
OIDC_PROVIDER = "authentik"
OIDC_ISSUER = os.environ.get("WCCG_OIDC_ISSUER", "").rstrip("/")
OIDC_CLIENT_ID = os.environ.get("WCCG_OIDC_CLIENT_ID", "")
OIDC_CLIENT_SECRET = os.environ.get("WCCG_OIDC_CLIENT_SECRET", "")
OIDC_STATE_TTL_SECONDS = 600


def oidc_enabled() -> bool:
    """OIDC 설정이 모두 채워졌는지 (테스트에서 monkeypatch 가능하도록 함수)."""
    return bool(OIDC_ISSUER and OIDC_CLIENT_ID and OIDC_CLIENT_SECRET)


def ensure_dirs() -> None:
    """아카이브 루트 디렉토리 생성."""
    try:
        SITES_DIR.mkdir(parents=True, exist_ok=True)
    except PermissionError as e:
        raise PermissionError(
            f"아카이브 디렉토리를 만들 수 없습니다: {ARCHIVE_ROOT} — 쓰기 권한을 "
            "확인하세요 (도커 바인드 마운트라면 호스트 디렉토리 소유자가 "
            "컨테이너 사용자 uid 1000 과 다른 경우)"
        ) from e


def load_domain_rules(domain: str) -> dict:
    """rules.json 에서 도메인별 정규화 룰 로드. 없거나 깨졌으면 빈 dict.

    형식:
        {"example.com": {"remove_selectors": [".ads"],
                         "remove_line_patterns": ["^관련 기사"]}}
    `www.` 접두사가 빠진 키로도 조회한다. 룰 파일 오류가 아카이빙을
    막아서는 안 되므로 경고만 남기고 무시한다.
    """
    if not RULES_PATH.is_file():
        return {}
    try:
        rules = json.loads(RULES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("rules.json 로드 실패, 무시: %s", e)
        return {}
    entry = rules.get(domain) or rules.get(domain.removeprefix("www.")) or {}
    return entry if isinstance(entry, dict) else {}
