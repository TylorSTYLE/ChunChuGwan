"""전역 설정. 환경변수 ARCHIVER_ROOT 로 아카이브 위치 변경 가능."""

from __future__ import annotations

import os
from pathlib import Path

ARCHIVE_ROOT = Path(os.environ.get("ARCHIVER_ROOT", "archive")).resolve()
SITES_DIR = ARCHIVE_ROOT / "sites"
DB_PATH = ARCHIVE_ROOT / "index.db"

PAGE_LOAD_TIMEOUT_MS = 30_000
USER_AGENT = "Mozilla/5.0 (compatible; PersonalArchiver/0.1)"

# URL 정규화 시 제거할 트래킹 파라미터 prefix
TRACKING_PARAM_PREFIXES = ("utm_", "fbclid", "gclid", "igshid", "ref_src")

DASHBOARD_HOST = "127.0.0.1"
DASHBOARD_PORT = 8765


def ensure_dirs() -> None:
    """아카이브 루트 디렉토리 생성."""
    SITES_DIR.mkdir(parents=True, exist_ok=True)
