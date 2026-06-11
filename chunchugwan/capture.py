"""Playwright 기반 페이지 캡처.

산출물 4종을 스냅샷 디렉토리에 저장한다:
- raw.html        렌더링 완료 후 DOM 소스 (page.content())
- page.html       이미지/CSS를 base64로 인라인한 단일 HTML
- screenshot.png  전체 페이지 스크린샷 (full_page=True)
- content.md      extract.py 가 생성 (이 모듈 밖)
"""

from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

from . import config

logger = logging.getLogger(__name__)

# 페이지 컨텍스트에서 스타일시트/이미지/폰트를 data URI로 치환.
# 실패한 자원 목록 {kind, url, raw?} 반환 — 페이지 컨텍스트 fetch() 는 CORS 에
# 막힐 수 있으므로(<img> 렌더링과 달리), 실패분은 Python 쪽 폴백이 재시도한다.
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
  const FONT_URL_RE = /url\\((['"]?)([^)'"]+?\\.(?:woff2?|ttf|otf|eot)(?:[?#][^)'"]*)?)\\1\\)/gi;
  const inlineFonts = async (cssText, baseUrl) => {
    const out = [];
    let last = 0;
    for (const m of cssText.matchAll(FONT_URL_RE)) {
      out.push(cssText.slice(last, m.index));
      let repl = m[0];
      let abs = null;
      try { abs = new URL(m[2], baseUrl).href; } catch (e) {}
      try {
        if (!abs) throw new Error("URL 해석 실패");
        repl = "url(" + (await toDataUrl(abs)) + ")";
      } catch (e) {
        failed.push({ kind: "font", url: abs, raw: m[0] });
      }
      out.push(repl);
      last = m.index + m[0].length;
    }
    out.push(cssText.slice(last));
    return out.join("");
  };
  for (const link of Array.from(document.querySelectorAll('link[rel="stylesheet"][href]'))) {
    try {
      const res = await fetch(link.href, { credentials: "omit" });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const style = document.createElement("style");
      style.textContent = await inlineFonts(await res.text(), link.href);
      link.replaceWith(style);
    } catch (e) {
      failed.push({ kind: "css", url: link.href });
    }
  }
  for (const style of Array.from(document.querySelectorAll("style"))) {
    style.textContent = await inlineFonts(style.textContent, document.baseURI);
  }
  for (const img of Array.from(document.querySelectorAll("img[src]"))) {
    const src = img.currentSrc || img.src;
    if (!src || src.startsWith("data:")) continue;
    try {
      const dataUrl = await toDataUrl(src);
      img.removeAttribute("srcset");
      img.src = dataUrl;
    } catch (e) {
      failed.push({ kind: "img", url: src });
    }
  }
  return failed;
}
"""

# 폴백으로 받아온 자원을 DOM 에 반영. img 는 data URI 치환,
# css 는 <style> 로 교체(url 은 Python 에서 절대화됨), font 는 인라인된
# <style> 텍스트 안의 원본 url(...) 토큰을 data URI 로 치환.
_APPLY_INLINE_JS = """
(repls) => {
  for (const r of repls) {
    if (r.kind === "img") {
      for (const img of Array.from(document.querySelectorAll("img[src]"))) {
        if ((img.currentSrc || img.src) === r.url) {
          img.removeAttribute("srcset");
          img.src = r.dataUrl;
        }
      }
    } else if (r.kind === "css") {
      for (const link of Array.from(document.querySelectorAll('link[rel="stylesheet"][href]'))) {
        if (link.href === r.url) {
          const style = document.createElement("style");
          style.textContent = r.cssText;
          link.replaceWith(style);
        }
      }
    } else if (r.kind === "font") {
      for (const style of Array.from(document.querySelectorAll("style"))) {
        if (style.textContent.includes(r.raw)) {
          style.textContent = style.textContent.split(r.raw).join("url(" + r.dataUrl + ")");
        }
      }
    }
  }
}
"""

# 도메인 룰의 셀렉터에 걸리는 노드 제거. 잘못된 셀렉터는 무시.
_REMOVE_JS = """
(selectors) => {
  let removed = 0;
  for (const sel of selectors) {
    try {
      document.querySelectorAll(sel).forEach((el) => { el.remove(); removed += 1; });
    } catch (e) { /* 잘못된 셀렉터 무시 */ }
  }
  return removed;
}
"""


@dataclass
class CaptureResult:
    final_url: str
    http_status: int | None
    title: str | None
    raw_html: str
    content_html: str  # 도메인 룰 셀렉터 제거 후 DOM (추출용, 룰 없으면 raw와 동일)


def capture(
    url: str, out_dir: Path, remove_selectors: tuple[str, ...] = ()
) -> CaptureResult:
    """URL을 렌더링해 raw.html / page.html / screenshot.png 를 out_dir에 저장.

    remove_selectors 가 있으면 저장 산출물에는 손대지 않고, 본문 추출용
    content_html 에서만 해당 노드를 제거한다.
    """
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
                except PlaywrightTimeoutError as e:
                    if page.url == "about:blank":
                        # 네비게이션이 시작조차 못함(연결 불가) — load 재시도 무의미
                        raise CaptureConnectError(f"{url} 캡처 실패: {e}") from e
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

                content_html = raw_html
                if remove_selectors:
                    removed = page.evaluate(_REMOVE_JS, list(remove_selectors))
                    logger.info("도메인 룰로 노드 %d개 제거: %s", removed, url)
                    content_html = page.content()

                return CaptureResult(
                    final_url=page.url,
                    http_status=response.status if response else None,
                    title=page.title() or None,
                    raw_html=raw_html,
                    content_html=content_html,
                )
            finally:
                browser.close()
    except PlaywrightError as e:
        if any(marker in str(e) for marker in _CONNECT_ERROR_MARKERS):
            raise CaptureConnectError(f"{url} 캡처 실패: {e}") from e
        raise CaptureError(f"{url} 캡처 실패: {e}") from e


def _inline_resources(page, raw_html: str) -> str:
    """<img src>, <link rel=stylesheet>, 폰트를 data URI로 치환한 단일 HTML 생성.

    페이지 컨텍스트 fetch() 가 CORS 로 막힌 자원은 context.request 폴백으로
    재시도하고, 끝내 실패한 자원만 원본 URL을 유지하며 경고 로그를 남긴다.
    """
    try:
        failed: list[dict] = page.evaluate(_INLINE_JS)
        if failed:
            failed = _retry_inline_via_context(page, failed)
        for item in failed:
            logger.warning(
                "자원 인라인 실패(원본 URL 유지): %s", item.get("url") or item.get("raw")
            )
        return page.content()
    except Exception as e:  # 인라인이 실패해도 캡처 자체는 유효
        logger.warning("자원 인라인 단계 실패, raw HTML로 대체: %s", e)
        return raw_html


def _retry_inline_via_context(page, failed: list[dict]) -> list[dict]:
    """CORS 로 막힌 자원을 브라우저 밖 API 요청(context.request)으로 재시도.

    context.request 는 CORS 제약이 없고 컨텍스트의 쿠키/UA 를 공유한다.
    핫링크 보호 대비 Referer 를 현재 페이지로 보낸다. 성공분은 DOM 에 반영하고
    끝내 실패한 항목만 돌려준다.
    """
    replacements: list[dict] = []
    still_failed: list[dict] = []
    fetched: dict[str, tuple[str, bytes] | None] = {}
    for item in failed:
        url = item.get("url")
        if not url:
            still_failed.append(item)
            continue
        if url not in fetched:
            fetched[url] = _fetch_via_context(page, url)
        result = fetched[url]
        if result is None:
            still_failed.append(item)
            continue
        content_type, body = result
        if item["kind"] == "css":
            css_text = _absolutize_css_urls(
                body.decode("utf-8", errors="replace"), url
            )
            replacements.append({"kind": "css", "url": url, "cssText": css_text})
        else:
            encoded = base64.b64encode(body).decode("ascii")
            data_url = f"data:{content_type};base64,{encoded}"
            replacements.append({**item, "dataUrl": data_url})
    if replacements:
        page.evaluate(_APPLY_INLINE_JS, replacements)
    return still_failed


def _fetch_via_context(page, url: str) -> tuple[str, bytes] | None:
    """자원 1개를 context.request 로 받아 (content-type, body) 반환. 실패 시 None."""
    try:
        resp = page.context.request.get(
            url,
            headers={"Referer": page.url},
            timeout=config.PAGE_LOAD_TIMEOUT_MS,
        )
        if not resp.ok:
            return None
        content_type = resp.headers.get("content-type", "")
        content_type = content_type.split(";")[0].strip() or "application/octet-stream"
        return content_type, resp.body()
    except Exception:
        return None


_CSS_URL_RE = re.compile(r"url\(\s*(['\"]?)([^)'\"]+)\1\s*\)")


def _absolutize_css_urls(css_text: str, base_url: str) -> str:
    """CSS 를 <style> 로 옮기면 상대 경로 기준이 문서로 바뀌므로 url(...) 절대화."""

    def repl(m: re.Match[str]) -> str:
        ref = m.group(2).strip()
        if ref.startswith(("data:", "http://", "https://", "//", "#")):
            return m.group(0)
        return f"url({urljoin(base_url, ref)})"

    return _CSS_URL_RE.sub(repl, css_text)


# 서버 연결 단계에서 나는 chromium 네트워크 오류 (DNS 실패는 스킴과 무관하므로 제외)
_CONNECT_ERROR_MARKERS = (
    "net::ERR_CONNECTION_",
    "net::ERR_SSL_",
    "net::ERR_CERT_",
    "net::ERR_TIMED_OUT",
    "net::ERR_ADDRESS_UNREACHABLE",
)


class CaptureError(RuntimeError):
    pass


class CaptureConnectError(CaptureError):
    """서버 연결 자체가 안 된 실패 (443 닫힘·SSL 오류 등) — https→http 폴백 판단용."""
