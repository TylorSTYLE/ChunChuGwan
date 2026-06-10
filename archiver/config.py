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
