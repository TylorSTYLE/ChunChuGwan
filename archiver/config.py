"""전역 설정. 환경변수 ARCHIVER_ROOT(아카이브 위치), ARCHIVER_HOST(대시보드 바인딩) 오버라이드 가능."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

ARCHIVE_ROOT = Path(os.environ.get("ARCHIVER_ROOT", "archive")).resolve()
SITES_DIR = ARCHIVE_ROOT / "sites"
DB_PATH = ARCHIVE_ROOT / "index.db"
CACHE_DIR = ARCHIVE_ROOT / "cache"          # 파생 산출물(픽셀 diff 등), 재생성 가능
RULES_PATH = ARCHIVE_ROOT / "rules.json"    # 도메인별 정규화 룰

PAGE_LOAD_TIMEOUT_MS = 30_000
USER_AGENT = "Mozilla/5.0 (compatible; PersonalArchiver/0.1)"

# URL 정규화 시 제거할 트래킹 파라미터 prefix
TRACKING_PARAM_PREFIXES = ("utm_", "fbclid", "gclid", "igshid", "ref_src")

# 기본 127.0.0.1 (localhost 전용). 컨테이너 등에서만 ARCHIVER_HOST=0.0.0.0 으로 오버라이드.
DASHBOARD_HOST = os.environ.get("ARCHIVER_HOST", "127.0.0.1")
DASHBOARD_PORT = 8765

# ---- 인증 ----
# ARCHIVER_AUTH=off 는 loopback 바인딩일 때만 허용 (cli.serve 에서 강제)
AUTH_ENABLED = os.environ.get("ARCHIVER_AUTH", "on") != "off"

# 최초 구동(사용자 0명) 시 자동 등록할 관리자. 미설정이면 /setup 페이지로 유도.
ADMIN_EMAIL = os.environ.get("ARCHIVER_ADMIN_EMAIL", "").strip()
ADMIN_PASSWORD = os.environ.get("ARCHIVER_ADMIN_PASSWORD", "")
SESSION_TTL_DAYS = int(os.environ.get("ARCHIVER_SESSION_TTL_DAYS", "14"))
SESSION_COOKIE = "archiver_session"
TOTP_ISSUER = "Web Archiver"
PENDING_TOTP_TTL_SECONDS = 600          # 패스워드 통과 후 OTP 입력 제한 시간
MIN_PASSWORD_LENGTH = 8

# 외부 노출 시 공개 URL (OIDC redirect_uri 조립, https 면 Secure 쿠키)
PUBLIC_URL = os.environ.get("ARCHIVER_PUBLIC_URL", "").rstrip("/")
COOKIE_SECURE = PUBLIC_URL.startswith("https://")

# ---- OIDC (Authentik) ----
OIDC_PROVIDER = "authentik"
OIDC_ISSUER = os.environ.get("ARCHIVER_OIDC_ISSUER", "").rstrip("/")
OIDC_CLIENT_ID = os.environ.get("ARCHIVER_OIDC_CLIENT_ID", "")
OIDC_CLIENT_SECRET = os.environ.get("ARCHIVER_OIDC_CLIENT_SECRET", "")
OIDC_STATE_TTL_SECONDS = 600


def oidc_enabled() -> bool:
    """OIDC 설정이 모두 채워졌는지 (테스트에서 monkeypatch 가능하도록 함수)."""
    return bool(OIDC_ISSUER and OIDC_CLIENT_ID and OIDC_CLIENT_SECRET)


def ensure_dirs() -> None:
    """아카이브 루트 디렉토리 생성."""
    SITES_DIR.mkdir(parents=True, exist_ok=True)


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
