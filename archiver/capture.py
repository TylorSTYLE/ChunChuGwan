"""Playwright 기반 페이지 캡처.

산출물 4종을 스냅샷 디렉토리에 저장한다:
- raw.html        렌더링 완료 후 DOM 소스 (page.content())
- page.html       이미지/CSS를 base64로 인라인한 단일 HTML
- screenshot.png  전체 페이지 스크린샷 (full_page=True)
- content.md      extract.py 가 생성 (이 모듈 밖)
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import config


@dataclass
class CaptureResult:
    final_url: str
    http_status: int | None
    title: str | None
    raw_html: str


def capture(url: str, out_dir: Path) -> CaptureResult:
    """URL을 렌더링해 raw.html / page.html / screenshot.png 저장.

    TODO(M2):
    - sync_playwright, chromium headless, config.USER_AGENT 적용
    - page.goto(url, wait_until="networkidle", timeout=config.PAGE_LOAD_TIMEOUT_MS)
      networkidle 미도달 시 "load" 로 폴백
    - response.status 기록, page.title() 기록
    - raw.html: page.content()
    - screenshot.png: page.screenshot(full_page=True)
    - page.html: _inline_resources() 로 자원 인라인
    - 캡처 실패는 CaptureError 로 래핑해 던질 것 (CLI에서 메시지 처리)
    """
    raise NotImplementedError


def _inline_resources(page, raw_html: str) -> str:
    """<img src>, <link rel=stylesheet> 를 data URI로 치환한 단일 HTML 생성.

    TODO(M2): page.evaluate 로 같은 브라우저 컨텍스트에서 fetch → base64.
    실패한 자원은 원본 URL 유지하고 경고 로그. 폰트 인라인은 M5.
    보안 노트: 인라인 과정에서 외부 자원을 받아올 때도 페이지와 동일한
    컨텍스트(쿠키/세션 비공유, 새 브라우저 컨텍스트)를 유지할 것.
    """
    raise NotImplementedError


class CaptureError(RuntimeError):
    pass
