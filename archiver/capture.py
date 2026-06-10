"""Playwright 기반 페이지 캡처.

산출물 4종을 스냅샷 디렉토리에 저장한다:
- raw.html        렌더링 완료 후 DOM 소스 (page.content())
- page.html       이미지/CSS를 base64로 인라인한 단일 HTML
- screenshot.png  전체 페이지 스크린샷 (full_page=True)
- content.md      extract.py 가 생성 (이 모듈 밖)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from . import config

logger = logging.getLogger(__name__)

# 페이지 컨텍스트에서 스타일시트/이미지를 data URI로 치환. 실패한 자원 URL 목록 반환.
_INLINE_JS = """
async () => {
  const failed = [];
  const toDataUrl = async (url) => {
    const res = await fetch(url, { credentials: "omit" });
    if (!res.ok) throw new Error("HTTP " + res.status);
    const blob = await res.blob();
    return await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = () => reject(reader.error);
      reader.readAsDataURL(blob);
    });
  };
  for (const link of Array.from(document.querySelectorAll('link[rel="stylesheet"][href]'))) {
    try {
      const res = await fetch(link.href, { credentials: "omit" });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const style = document.createElement("style");
      style.textContent = await res.text();
      link.replaceWith(style);
    } catch (e) {
      failed.push(link.href);
    }
  }
  for (const img of Array.from(document.querySelectorAll("img[src]"))) {
    const src = img.currentSrc || img.src;
    if (!src || src.startsWith("data:")) continue;
    try {
      const dataUrl = await toDataUrl(src);
      img.removeAttribute("srcset");
      img.src = dataUrl;
    } catch (e) {
      failed.push(src);
    }
  }
  return failed;
}
"""


@dataclass
class CaptureResult:
    final_url: str
    http_status: int | None
    title: str | None
    raw_html: str


def capture(url: str, out_dir: Path) -> CaptureResult:
    """URL을 렌더링해 raw.html / page.html / screenshot.png 를 out_dir에 저장."""
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                context = browser.new_context(user_agent=config.USER_AGENT)
                page = context.new_page()
                page.set_default_timeout(config.PAGE_LOAD_TIMEOUT_MS)
                try:
                    response = page.goto(
                        url, wait_until="networkidle", timeout=config.PAGE_LOAD_TIMEOUT_MS
                    )
                except PlaywrightTimeoutError:
                    logger.warning("networkidle 미도달, load 기준으로 재시도: %s", url)
                    response = page.goto(
                        url, wait_until="load", timeout=config.PAGE_LOAD_TIMEOUT_MS
                    )

                raw_html = page.content()
                (out_dir / "raw.html").write_text(raw_html, encoding="utf-8")
                page.screenshot(path=str(out_dir / "screenshot.png"), full_page=True)
                (out_dir / "page.html").write_text(
                    _inline_resources(page, raw_html), encoding="utf-8"
                )
                return CaptureResult(
                    final_url=page.url,
                    http_status=response.status if response else None,
                    title=page.title() or None,
                    raw_html=raw_html,
                )
            finally:
                browser.close()
    except PlaywrightError as e:
        raise CaptureError(f"{url} 캡처 실패: {e}") from e


def _inline_resources(page, raw_html: str) -> str:
    """<img src>, <link rel=stylesheet> 를 data URI로 치환한 단일 HTML 생성.

    실패한 자원은 원본 URL을 유지하고 경고 로그만 남긴다. 폰트 인라인은 M5.
    """
    try:
        failed: list[str] = page.evaluate(_INLINE_JS)
        for res_url in failed:
            logger.warning("자원 인라인 실패(원본 URL 유지): %s", res_url)
        return page.content()
    except Exception as e:  # 인라인이 실패해도 캡처 자체는 유효
        logger.warning("자원 인라인 단계 실패, raw HTML로 대체: %s", e)
        return raw_html


class CaptureError(RuntimeError):
    pass
