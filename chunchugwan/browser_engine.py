"""캡처 엔진 선택 — 표준 Playwright 또는 patchright(스텔스) 드롭인.

`WCCG_CAPTURE_ENGINE` 로 고른다 (기본 'playwright'). 'patchright' 는 Playwright
sync API 와 시그니처 호환인 드롭인 패치로, Cloudflare 등이 쓰는 CDP
`Runtime.enable` 기반 봇 탐지를 우회한다. capture.py 는 Playwright 심볼을 이
모듈을 통해서만 가져와, 엔진을 환경변수로 갈아끼울 수 있게 한다.

예외 클래스(Error/TimeoutError)는 반드시 *활성 엔진* 것을 써야 한다 — patchright
가 던진 오류를 playwright 의 Error 로 잡으면 except 가 안 맞기 때문이다.
"""

from __future__ import annotations

import logging

from . import config

logger = logging.getLogger(__name__)

_warned = False


def get_engine():
    """(이름, sync_playwright, Error, TimeoutError) — 활성 캡처 엔진의 심볼.

    `WCCG_CAPTURE_ENGINE=patchright` 인데 patchright 가 설치돼 있지 않으면 한 번
    경고하고 표준 playwright 로 폴백한다 (기능은 유지, 스텔스만 없음).
    """
    global _warned
    if config.CAPTURE_ENGINE == "patchright":
        try:
            from patchright.sync_api import (  # type: ignore[import-not-found]
                Error,
                TimeoutError,
                sync_playwright,
            )

            return "patchright", sync_playwright, Error, TimeoutError
        except ImportError:
            if not _warned:
                logger.warning(
                    "WCCG_CAPTURE_ENGINE=patchright 이지만 patchright 가 설치돼 "
                    "있지 않습니다 — 표준 playwright 로 폴백합니다 "
                    "(설치: uv sync --extra stealth)"
                )
                _warned = True
    from playwright.sync_api import Error, TimeoutError, sync_playwright

    return "playwright", sync_playwright, Error, TimeoutError
