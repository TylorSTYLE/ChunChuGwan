"""Playwright 기반 페이지 캡처.

산출물 4종을 스냅샷 디렉토리에 저장한다:
- raw.html        렌더링 완료 후 DOM 소스 (page.content())
- page.html       이미지/CSS를 base64로 인라인한 단일 HTML
- screenshot.png  전체 페이지(데스크탑 뷰포트) 스크린샷 (full_page=True)
- content.md      extract.py 가 생성 (이 모듈 밖)

mobile_screenshot 가 켜져 있으면 같은 URL 을 안드로이드 크롬으로 위장한
모바일 컨텍스트(config.MOBILE_SCREENSHOT_*)로 한 번 더 열어
screenshot-mobile.png 한 장을 더 찍는다.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence
from urllib.parse import quote, urldefrag, urljoin, urlsplit

from . import (
    browser_engine, config, consent_overlays, documents, netcheck, storage,
    trackers,
)

logger = logging.getLogger(__name__)

# 페이지 컨텍스트에서 스타일시트/이미지/폰트를 data URI로 치환.
# {failed, inlined} 반환 — failed 는 실패 자원 {kind, url, raw?} 목록으로,
# 페이지 컨텍스트 fetch() 는 CORS 에 막힐 수 있으므로(<img> 렌더링과 달리)
# Python 쪽 폴백이 재시도한다. fetch 에는 자원별 타임아웃(AbortSignal)을
# 걸어 응답 없는 호스트가 인라인 단계 전체를 매달지 못하게 하고,
# 자원 단위 작업은 concurrency 개수의 워커로 병렬 실행한다 — 캐시를 못
# 타는 자원이 많은 페이지에서 직렬 왕복이 쌓이는 것을 막는다 (각 작업이
# 만지는 노드는 서로 다르므로 동시 실행해도 안전).
# inlined 는 성공 자원의 {url, sha256} 목록 —
# CAS 추출 후 snapshot_resources 에 원본 URL 을 기록하는 근거다. sha256 은
# crypto.subtle(보안 컨텍스트)로 계산하고, http 페이지처럼 없는 환경에서는
# expose_function 으로 노출된 Python 바인딩(_sha256_of_base64)으로 폴백한다.
_INLINE_JS = """
async ({ timeoutMs, concurrency, baseUrl, overallMs, maxCount }) => {
  const failed = [];
  const inlined = [];
  // 인라인 총량 예산 — unicode-range 웹폰트는 @font-face 서브셋이 수천 개라
  // 전부 인라인하면 page.html 이 수십 MB 로 부풀고 메모리를 폭증시킨다. 예산을
  // 넘는 자원은 인라인하지 않고 원본 url() 을 유지한다(뷰어가 라이브로 받음).
  let budget = (maxCount && maxCount > 0) ? maxCount : Infinity;
  const takeBudget = () => (budget > 0 ? (budget--, true) : false);
  // 상대경로 해석 기준 — 링크 재작성이 <base> 를 떼기 전에 Python 이 떠 둔 값.
  // 없으면(직접 호출 등) 현재 baseURI 로 폴백.
  const base = baseUrl || document.baseURI;
  const shaHex = async (blob, dataUrl) => {
    if (crypto.subtle) {
      const buf = await blob.arrayBuffer();
      const digest = await crypto.subtle.digest("SHA-256", buf);
      return Array.from(new Uint8Array(digest))
        .map((b) => b.toString(16).padStart(2, "0")).join("");
    }
    // 비보안 컨텍스트(http) — Python 바인딩으로 폴백 (hashlib, capture.py)
    if (window._wccgSha256) {
      return await window._wccgSha256(dataUrl.slice(dataUrl.indexOf(",") + 1));
    }
    return null;
  };
  const dataUrlCache = new Map();
  const fetchDataUrl = async (url) => {
    const res = await fetch(url, {
      credentials: "omit", signal: AbortSignal.timeout(timeoutMs),
    });
    if (!res.ok) throw new Error("HTTP " + res.status);
    const blob = await res.blob();
    const dataUrl = await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = () => reject(reader.error);
      reader.readAsDataURL(blob);
    });
    try {
      const sha = await shaHex(blob, dataUrl);
      if (sha) inlined.push({ url, sha256: sha });
    } catch (e) { /* 해시 실패는 인라인을 막지 않는다 */ }
    return dataUrl;
  };
  // URL 단위로 in-flight 프로미스를 캐시 — 같은 자원을 여러 요소가 써도
  // fetch·인코딩·inlined 기록은 1회. 실패(reject)도 공유해 재시도를 막는다.
  const toDataUrl = (url) => {
    let cached = dataUrlCache.get(url);
    if (!cached) { cached = fetchDataUrl(url); dataUrlCache.set(url, cached); }
    return cached;
  };
  const FONT_URL_RE = /url\\((['"]?)([^)'"]+?\\.(?:woff2?|ttf|otf|eot)(?:[?#][^)'"]*)?)\\1\\)/gi;
  const inlineFonts = async (cssText, baseUrl) => {
    const matches = Array.from(cssText.matchAll(FONT_URL_RE));
    if (!matches.length) return cssText;
    // 한 CSS 안의 폰트는 한꺼번에 받는다 (보통 소수라 바운드 불필요)
    const repls = await Promise.all(matches.map(async (m) => {
      let abs = null;
      try { abs = new URL(m[2], baseUrl).href; } catch (e) {}
      // 예산 초과분(웹폰트 서브셋 폭발)은 받지 않고 원본 url() 유지 — failed 에도
      // 안 넣어 재시도 폭발을 막는다.
      if (!takeBudget()) return m[0];
      try {
        if (!abs) throw new Error("URL 해석 실패");
        return "url(" + (await toDataUrl(abs)) + ")";
      } catch (e) {
        failed.push({ kind: "font", url: abs, raw: m[0] });
        return m[0];
      }
    }));
    const out = [];
    let last = 0;
    matches.forEach((m, i) => {
      out.push(cssText.slice(last, m.index), repls[i]);
      last = m.index + m[0].length;
    });
    out.push(cssText.slice(last));
    return out.join("");
  };
  // 인라인 style="...url(...)..." 의 이미지 참조 — <img>/<style>/<link> 어디에도
  // 안 걸려 상대경로면 뷰어(/snapshot/{id}/file/)에서 깨진다. <img> 와 동일하게
  // data URI 로 인라인한다 (실패분은 failed 로 — 컨텍스트/과거캡처 폴백이 받는다).
  const STYLE_URL_RE = /url\\((['"]?)([^)'"]+?)\\1\\)/gi;
  const inlineStyleUrls = async (styleText, baseUrl) => {
    const matches = Array.from(styleText.matchAll(STYLE_URL_RE));
    if (!matches.length) return styleText;
    const repls = await Promise.all(matches.map(async (m) => {
      const ref = m[2].trim();
      if (!ref || ref.startsWith("data:") || ref.startsWith("#")) return m[0];
      let abs = null;
      try { abs = new URL(ref, baseUrl).href; } catch (e) {}
      if (!takeBudget()) return m[0];
      try {
        if (!abs) throw new Error("URL 해석 실패");
        return "url(" + (await toDataUrl(abs)) + ")";
      } catch (e) {
        failed.push({ kind: "bgstyle", url: abs, raw: m[0] });
        return m[0];
      }
    }));
    const out = [];
    let last = 0;
    matches.forEach((m, i) => {
      out.push(styleText.slice(last, m.index), repls[i]);
      last = m.index + m[0].length;
    });
    out.push(styleText.slice(last));
    return out.join("");
  };
  const tasks = [];
  // <style> 스냅샷은 작업 생성 시점에 뜬다 — 링크 치환으로 새로 생기는
  // <style> 은 이미 inlineFonts 를 거쳤으므로 다시 처리하지 않는다
  for (const link of Array.from(document.querySelectorAll('link[rel="stylesheet"][href]'))) {
    tasks.push(async () => {
      try {
        const res = await fetch(link.href, {
          credentials: "omit", signal: AbortSignal.timeout(timeoutMs),
        });
        if (!res.ok) throw new Error("HTTP " + res.status);
        const style = document.createElement("style");
        style.textContent = await inlineFonts(await res.text(), link.href);
        link.replaceWith(style);
      } catch (e) {
        failed.push({ kind: "css", url: link.href });
      }
    });
  }
  for (const style of Array.from(document.querySelectorAll("style"))) {
    tasks.push(async () => {
      style.textContent = await inlineFonts(style.textContent, base);
    });
  }
  for (const img of Array.from(document.querySelectorAll("img[src]"))) {
    const src = img.currentSrc || img.src;
    if (!src || src.startsWith("data:")) continue;
    tasks.push(async () => {
      if (!takeBudget()) return;  // 예산 초과 — 원본 src 유지
      try {
        const dataUrl = await toDataUrl(src);
        img.removeAttribute("srcset");
        img.src = dataUrl;
      } catch (e) {
        failed.push({ kind: "img", url: src });
      }
    });
  }
  for (const el of Array.from(document.querySelectorAll('[style*="url(" i]'))) {
    const styleText = el.getAttribute("style");
    if (!styleText) continue;
    tasks.push(async () => {
      const replaced = await inlineStyleUrls(styleText, base);
      if (replaced !== styleText) el.setAttribute("style", replaced);
    });
  }
  const queue = tasks.slice();
  const worker = async () => {
    while (queue.length) await queue.shift()();
  };
  const allDone = Promise.all(
    Array.from({ length: Math.min(concurrency, tasks.length) }, worker)
  );
  // 전체 데드라인 — 자원별 AbortSignal.timeout 만으로는 headful 실제 Chrome 에서
  // 일부 fetch 가 끝내 안 끊겨 Promise.all 이 영영 안 끝날 수 있다. overallMs 가
  // 지나면 그때까지 인라인된 것만 들고 부분결과로 resolve 한다 (남은 자원은 원본
  // URL 유지 — 끝나지 않은 task 는 DOM 을 안 건드린 채로 남는다).
  let timedOut = false;
  await Promise.race([
    allDone,
    new Promise((resolve) => setTimeout(() => { timedOut = true; resolve(); }, overallMs)),
  ]);
  return { failed, inlined, timedOut };
}
"""

# _INLINE_JS 를 띄우고 결과를 window 에 적재하는 런처 — page.evaluate 는
# set_default_timeout 을 무시하고 반환 Promise 를 무한 대기하므로, 작업을 백그라운드로
# 시작만 시키고(즉시 반환) Python 이 wait_for_function(타임아웃 적용됨)으로 완료를
# 기다린다. JS 데드라인이 1차, wait_for_function 이 2차(백스톱) 강제 상한이다.
_INLINE_LAUNCH_JS = """
(args) => {
  window.__wccgInlineDone = false;
  window.__wccgInlineResult = null;
  Promise.resolve((%s)(args))
    .then((r) => { window.__wccgInlineResult = r; window.__wccgInlineDone = true; })
    .catch((e) => {
      window.__wccgInlineResult = { failed: [], inlined: [], error: String(e) };
      window.__wccgInlineDone = true;
    });
}
""" % _INLINE_JS

# 폴백으로 받아온 자원을 DOM 에 반영. img 는 data URI 치환,
# css 는 <style> 로 교체(url 은 Python 에서 절대화됨), font 는 인라인된
# <style> 텍스트 안의 원본 url(...) 토큰을 data URI 로 치환, bgstyle 은
# 인라인 style="" 속성 안의 원본 url(...) 토큰을 data URI 로 치환.
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
    } else if (r.kind === "bgstyle") {
      for (const el of Array.from(document.querySelectorAll("[style]"))) {
        const s = el.getAttribute("style");
        if (s && s.includes(r.raw)) {
          el.setAttribute("style", s.split(r.raw).join("url(" + r.dataUrl + ")"));
        }
      }
    }
  }
}
"""

# 문서 링크 후보 수집 — href/src/data 프로퍼티는 브라우저가 절대 URL 로
# 해석해 주므로 상대 경로 걱정이 없다. 확장자 필터는 Python 쪽에서 한다.
_DOC_LINK_JS = """
() => {
  const urls = [];
  const push = (u) => { if (u) urls.push(u); };
  document.querySelectorAll("a[href]").forEach((a) => push(a.href));
  document.querySelectorAll("embed[src]").forEach((e) => push(e.src));
  document.querySelectorAll("object[data]").forEach((o) => push(o.data));
  document.querySelectorAll("iframe[src]").forEach((f) => push(f.src));
  return urls;
}
"""

# 페이지 링크 수집 — a.href 프로퍼티는 브라우저가 절대 URL 로 해석해 준다.
_PAGE_LINK_JS = """
() => Array.from(document.querySelectorAll("a[href]"), (a) => a.href)
"""

# 크롤 캡처용 링크 재작성 — map(원본 절대 URL → 리졸버 URL)에 있는 앵커를
# 치환하고 target="_top" 을 붙인다 (샌드박스 iframe 안에서 사용자 클릭 시
# 뷰어 전체가 다음 스냅샷으로 이동 — allow-top-navigation-by-user-activation).
# 문서 내 앵커(#...)는 그대로 둔다. 치환이 있었으면 <base> 를 제거한다 —
# 루트 상대(/crawl/...) 링크가 원본 사이트 기준으로 해석되는 것을 막기
# 위해서이며, 원본 DOM 은 raw.html 이 보존한다.
_REWRITE_LINKS_JS = """
(map) => {
  let rewritten = 0;
  for (const a of Array.from(document.querySelectorAll("a[href]"))) {
    const attr = a.getAttribute("href") || "";
    if (attr.startsWith("#")) continue;
    const to = map[a.href];
    if (!to) continue;
    a.setAttribute("href", to);
    a.setAttribute("target", "_top");
    rewritten += 1;
  }
  if (rewritten) {
    document.querySelectorAll("base").forEach((b) => b.remove());
  }
  return rewritten;
}
"""

# live DOM에서 CSS 셀렉터에 걸리는 노드를 제거. 잘못된 셀렉터는 무시.
_REMOVE_FROM_LIVE_DOM_JS = """
(selectors) => {
  let removed = 0;
  for (const sel of selectors) {
    try {
      document.querySelectorAll(sel).forEach(el => { el.remove(); removed += 1; });
    } catch (e) { /* 잘못된 셀렉터 무시 */ }
  }
  return removed;
}
"""

# live DOM에서 쿠키 동의(CMP) 오버레이를 제거하고, 그것이 걸어둔 스크롤 잠금
# (html/body 의 인라인 overflow:hidden)을 푼다 — 무언가 실제로 제거했을 때만
# 잠금을 풀어 정상 페이지의 overflow 를 건드리지 않는다.
_REMOVE_CONSENT_JS = """
(selectors) => {
  let removed = 0;
  for (const sel of selectors) {
    try {
      document.querySelectorAll(sel).forEach(el => { el.remove(); removed += 1; });
    } catch (e) { /* 잘못된 셀렉터 무시 */ }
  }
  if (removed > 0) {
    for (const el of [document.documentElement, document.body]) {
      if (el && el.style && el.style.overflow === 'hidden') el.style.overflow = '';
    }
  }
  return removed;
}
"""

# live DOM에서 src 없는 <script>와 <noscript> 중 텍스트가 패턴에 일치하는 것을 제거.
_REMOVE_INLINE_TRACKERS_JS = """
(patterns) => {
  const regexps = patterns.map(p => new RegExp(p));
  let removed = 0;
  document.querySelectorAll('script:not([src]), noscript').forEach(el => {
    if (regexps.some(re => re.test(el.textContent))) {
      el.remove();
      removed += 1;
    }
  });
  return removed;
}
"""

# 도메인 룰의 셀렉터에 걸리는 노드를 제거한 HTML 생성. 잘못된 셀렉터는 무시.
# 라이브 DOM 대신 raw_html 문자열을 DOMParser 로 파싱해 작업한다 —
# 저장 산출물(page.html)을 오염시키지 않고, _inline_resources 이후의
# base64 데이터가 추출용 HTML 에 섞이는 것도 막는다. DOMParser 는 스크립트
# 실행/자원 로드를 하지 않으며, 셀렉터 엔진은 live querySelectorAll 과 동일.
_REMOVE_JS = """
([html, selectors]) => {
  const doc = new DOMParser().parseFromString(html, "text/html");
  let removed = 0;
  for (const sel of selectors) {
    try {
      doc.querySelectorAll(sel).forEach((el) => { el.remove(); removed += 1; });
    } catch (e) { /* 잘못된 셀렉터 무시 */ }
  }
  const doctype = doc.doctype ? "<!DOCTYPE " + doc.doctype.name + ">" : "";
  return [doctype + doc.documentElement.outerHTML, removed];
}
"""


@dataclass
class CaptureResult:
    final_url: str
    http_status: int | None
    title: str | None
    raw_html: str
    content_html: str  # raw_html 에서 도메인 룰 셀렉터를 제거한 추출용 HTML (룰 없으면 raw와 동일)
    # 페이지가 링크한 문서 파일 URL (절대 URL, fragment 제거·중복 제거)
    document_links: list[str] = field(default_factory=list)
    # 페이지의 모든 앵커 href (절대 URL, 중복 제거) — 크롤러의 링크 추적용
    page_links: list[str] = field(default_factory=list)
    # 인라인된 자원의 sha256 → 원본 URL — CAS 추출 후 snapshot_resources 의
    # url 컬럼을 채우는 근거 (sha 를 못 구한 자원은 빠진다)
    resource_urls: dict[str, str] = field(default_factory=dict)
    # 일부 산출물(스크린샷 등) 수집이 실패한 불완전 캡처 표식 — HTML·자원은
    # 수집됐으므로 스냅샷은 저장하되 snapshots.incomplete=1 로 남긴다.
    incomplete: bool = False


# 앵커 절대 URL 목록 → {원본 href: 재작성 href}. 비거나 None 이면 재작성 없음.
LinkRewriter = Callable[[Sequence[str]], dict[str, str]]


def generic_link_rewriter() -> LinkRewriter:
    """단일 페이지(비크롤) 캡처용 앵커 재작성 — http(s) 링크를 /goto 리졸버로.

    크롤은 crawl_id 종속 /crawl/{id}/goto 로 보내지만(crawler.link_rewriter),
    단일 페이지는 대상 스냅샷 id 를 캡처 시점에 알 수 없어 url 리졸버
    /goto?url=... 로 보낸다. http(s) 가 아닌 mailto:/javascript:/# 등은 그대로
    둔다. crawler._normalize_http 와 같은 정규화(명시적 스킴 요구).
    """

    def rewrite(hrefs: Sequence[str]) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for href in hrefs:
            if not href.startswith(("http://", "https://")):
                continue
            try:
                norm = storage.normalize_url(href)
            except ValueError:
                continue
            mapping[href] = f"/goto?url={quote(norm, safe='')}"
        return mapping

    return rewrite

# 자원 인라인 실패 시 과거 캡처본 조회 — URL 을 받아 (content-type, body) 또는
# None 을 반환한다 (pipeline 이 snapshot_resources + 자원 CAS 로 구현).
ResourceFallback = Callable[[str], "tuple[str, bytes] | None"]


# 채널(real Chrome 등) 기동이 한 번 실패하면(예: arm64 에 google-chrome
# 미설치) 이후로는 번들 chromium 으로 폴백을 유지한다 — 매 캡처마다 실패-재시도
# 비용을 한 번만 치르게 한다.
_channel_fallback = False
_mode_logged = False


def capture_mode_str() -> str:
    """현재 캡처가 도는 모드 — 엔진/headful/channel 한 줄. 진단용.

    스텔스 설정(WCCG_CAPTURE_*)이 실제로 적용됐는지 로그·archive_logs 로
    확인할 수 있게 한다. 예: 'playwright · headless · channel=-' 이 찍히면
    스텔스가 안 켜진 것(이미지 미빌드 또는 환경변수 미설정).
    """
    engine = browser_engine.get_engine()[0]
    head = "headful" if config.CAPTURE_HEADFUL else "headless"
    channel = config.CAPTURE_CHANNEL or "-"
    if _channel_fallback:
        channel += " (폴백: 번들 chromium)"
    return f"{engine} · {head} · channel={channel}"


def _launch(p, browser_args: tuple[str, ...] = ()):
    """설정에 따라 chromium 을 기동 — headless/channel 을 한 곳에서 결정.

    기본은 headless=True (기존 동작). `WCCG_CAPTURE_HEADFUL=on` 이면 헤드풀로
    띄우고(서버에선 Xvfb 가상 디스플레이 전제), `WCCG_CAPTURE_CHANNEL` 이 있으면
    그 채널(예: 'chrome' — 번들 chromium 대신 시스템 real Chrome)을 쓴다.
    두 launch 지점(BrowserSession.browser, _capture_once)이 이 헬퍼만 호출해
    옵션이 어긋나지 않게 한다.

    채널을 지정했는데 그 브라우저가 없으면(amd64 전용 google-chrome 을 arm64 에서
    요청하는 등) 번들 chromium 으로 폴백한다 — 같은 설정을 양쪽 아키텍처에서
    안전하게 쓸 수 있게 한다 (arm64 는 real Chrome 이 없으므로 stealth 가 다소
    약하지만 동작은 한다).
    """
    global _channel_fallback, _mode_logged
    if not _mode_logged:
        # 프로세스당 한 번 — 캡처가 실제로 어떤 모드로 도는지 남긴다(진단)
        logger.info("캡처 모드: %s", capture_mode_str())
        _mode_logged = True
    kwargs: dict = {
        "headless": not config.CAPTURE_HEADFUL,
        "args": list(browser_args),
    }
    channel = config.CAPTURE_CHANNEL
    if channel and not _channel_fallback:
        try:
            return p.chromium.launch(channel=channel, **kwargs)
        except Exception as e:
            logger.warning(
                "캡처 채널 %r 기동 실패 — 번들 chromium 으로 폴백합니다 "
                "(예: arm64 에 real Chrome 미설치). 이후 폴백을 유지합니다: %s",
                channel, str(e).splitlines()[0],
            )
            _channel_fallback = True
    return p.chromium.launch(**kwargs)


def _context_user_agent() -> str | None:
    """new_context 에 넘길 User-Agent — 헤드풀 스텔스에선 real Chrome 기본 UA.

    고정 UA(config.USER_AGENT, Chrome 136 Windows)를 real Chrome 위에 강제하면
    UA/Client Hints/JA4 가 불일치해 오히려 봇 신호가 된다. 따라서 헤드풀일 때는
    기본적으로 오버라이드를 해제(None → 브라우저 실제 UA 사용)하고,
    `WCCG_CAPTURE_FORCE_UA=on` 이면 강제한다. 헤드리스 기본 경로는 영향 없음.
    """
    if config.CAPTURE_HEADFUL and not config.CAPTURE_FORCE_USER_AGENT:
        return None
    return config.USER_AGENT


class BrowserSession:
    """여러 캡처가 재사용하는 Chromium 세션 (크롤러 스레드용).

    캡처마다 브라우저를 새로 띄우는 기동 비용(수 초 + CPU 스파이크)을
    없앤다. 캡처는 컨텍스트 단위로 격리되고(쿠키·캐시 공유 없음) 브라우저만
    유지된다. sync Playwright 제약상 스레드 간 공유 금지 — 스레드당 1개.
    브라우저가 죽었으면 다음 browser() 호출이 재기동하고, close() 후에도
    다시 쓸 수 있다 (큐가 빌 때 내려서 메모리 점유를 피하는 용도).
    """

    def __init__(self) -> None:
        self._playwright = None
        self._browser = None

    def browser(self):
        """살아 있는 브라우저 반환 — 없거나 죽었으면 (재)기동."""
        if self._browser is not None and self._browser.is_connected():
            return self._browser
        self.close()
        _, sync_playwright, _, _ = browser_engine.get_engine()

        self._playwright = sync_playwright().start()
        self._browser = _launch(self._playwright)
        return self._browser

    def close(self) -> None:
        """브라우저·드라이버 종료 (이미 닫혔으면 무해). 이후 재사용 가능."""
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                pass  # 이미 죽은 브라우저 — 드라이버 정리만 하면 된다
            self._browser = None
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

    def __enter__(self) -> "BrowserSession":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


@dataclass
class CaptureCredential:
    """캡처 시 주입할 로그인 자격증명 — pipeline 이 복호화해 만든다.

    origin 은 대상 사이트의 origin(`scheme://host[:port]`) — 자격증명을 이
    origin 의 요청에만 적용해 페이지의 서드파티 하위 자원(CDN 등)으로
    Basic 인증·Bearer 토큰이 새는 것을 막는다.
    """
    kind: str
    payload: dict
    origin: str


def _context_options(
    credential: CaptureCredential | None, insecure_tls: bool
) -> tuple[dict, str | None]:
    """new_context() 옵션과, jwt 면 origin-스코프로 붙일 Authorization 값을 만든다.

    - http_basic : http_credentials 를 대상 origin 으로 스코프 (context.request 에도 상속)
    - session    : storage_state (쿠키는 도메인 스코프라 자체 안전)
    - jwt        : 컨텍스트 옵션으로는 못 붙인다(extra_http_headers 는 모든 origin
      으로 새므로). Authorization 헤더 문자열만 돌려주고, 호출부가 context.route 로
      대상 origin 요청에만 추가한다.
    반환: (new_context kwargs, jwt Authorization 헤더 또는 None)
    """
    kwargs: dict = {
        "user_agent": _context_user_agent(),
        "ignore_https_errors": insecure_tls,
    }
    if credential is None:
        return kwargs, None
    if credential.kind == "http_basic":
        kwargs["http_credentials"] = {
            "username": credential.payload.get("username", ""),
            "password": credential.payload.get("password", ""),
            "origin": credential.origin,   # 이 origin 에만 Basic 전송 (누수 차단)
            "send": "always",
        }
        return kwargs, None
    if credential.kind == "session":
        state = credential.payload.get("storage_state")
        if state:
            kwargs["storage_state"] = state
        return kwargs, None
    if credential.kind == "jwt":
        token = credential.payload.get("token", "")
        return kwargs, (f"Bearer {token}" if token else None)
    return kwargs, None


def _install_origin_scoped_header(context, origin: str, authorization: str) -> None:
    """대상 origin 요청에만 Authorization 헤더를 붙이는 라우트를 단다 (jwt 용).

    context.route 는 페이지 네비게이션·하위 자원·페이지 내 fetch 를 가로채므로
    같은 origin 의 요청에만 토큰을 실어 보낸다 — 서드파티(CDN 등)로는 새지
    않는다. context.request(자원 인라인 폴백)에는 route 가 안 걸린다 — 같은
    origin 자원은 보통 페이지 fetch 단계에서 이 헤더로 인라인되지만, 그 fetch
    가 실패해 폴백으로 떨어지면 그 자원은 인증 없이 시도된다 (인라인 실패 가능,
    토큰 누수는 없음). http_basic 은 http_credentials 가 context.request 에도
    상속돼 이 한계가 없다.
    """
    def handler(route) -> None:
        request = route.request
        if storage.url_origin(request.url) == origin:
            headers = {
                k: v for k, v in request.headers.items()
                if k.lower() != "authorization"
            }
            headers["Authorization"] = authorization
            route.continue_(headers=headers)
        else:
            route.continue_()

    context.route("**/*", handler)


def capture(
    url: str,
    out_dir: Path,
    remove_selectors: tuple[str, ...] = (),
    link_rewriter: LinkRewriter | None = None,
    session: BrowserSession | None = None,
    resource_fallback: ResourceFallback | None = None,
    insecure_tls: bool = False,
    credential: CaptureCredential | None = None,
    live_session: "object | None" = None,
    ai_session: "object | None" = None,
    mobile_screenshot: bool = False,
) -> CaptureResult:
    """URL을 렌더링해 raw.html / page.html / screenshot.png 를 out_dir에 저장.

    remove_selectors 가 있으면 저장 산출물에는 손대지 않고, 본문 추출용
    content_html 에서만 해당 노드를 제거한다.
    link_rewriter 가 있으면(사이트 전체 아카이브) page.html 의 앵커를
    반환된 매핑대로 재작성한다 — raw.html/content_html 은 원본 유지.
    session 이 있으면 그 브라우저를 재사용한다 (없으면 1회용 기동).
    mobile_screenshot 가 켜져 있으면 안드로이드 크롬 모바일 컨텍스트로 같은
    URL 을 한 번 더 열어 screenshot-mobile.png 도 함께 저장한다.

    일부 서버(구형 IIS 등)는 HTTP/2 구현이 깨져 chromium 만
    net::ERR_HTTP2_* 로 실패한다 (curl 등은 정상) — 이 경우 HTTP/2 를
    끄고(HTTP/1.1) 한 번 더 시도한다. 이 폴백은 브라우저 인자가 달라
    session 과 무관하게 항상 1회용으로 띄운다.
    """
    try:
        return _capture_once(url, out_dir, remove_selectors, link_rewriter,
                             session=session, resource_fallback=resource_fallback,
                             insecure_tls=insecure_tls, credential=credential,
                             live_session=live_session, ai_session=ai_session,
                             mobile_screenshot=mobile_screenshot)
    except CaptureError as e:
        if "ERR_HTTP2" not in str(e):
            raise
        logger.warning("HTTP/2 프로토콜 오류 — HTTP/1.1 로 재시도: %s", url)
        return _capture_once(
            url, out_dir, remove_selectors, link_rewriter,
            browser_args=("--disable-http2",),
            resource_fallback=resource_fallback,
            insecure_tls=insecure_tls, credential=credential,
            live_session=live_session, ai_session=ai_session,
            mobile_screenshot=mobile_screenshot,
        )


def _capture_once(
    url: str,
    out_dir: Path,
    remove_selectors: tuple[str, ...] = (),
    link_rewriter: LinkRewriter | None = None,
    browser_args: tuple[str, ...] = (),
    session: BrowserSession | None = None,
    resource_fallback: ResourceFallback | None = None,
    insecure_tls: bool = False,
    credential: CaptureCredential | None = None,
    live_session: "object | None" = None,
    ai_session: "object | None" = None,
    mobile_screenshot: bool = False,
) -> CaptureResult:
    """캡처 1회 시도 — 폴백 판단은 capture() 가 한다."""
    _, sync_playwright, PlaywrightError, _ = browser_engine.get_engine()

    try:
        if session is not None and not browser_args:
            return _capture_in_browser(
                session.browser(), url, out_dir, remove_selectors, link_rewriter,
                resource_fallback, insecure_tls, credential, live_session,
                ai_session, mobile_screenshot,
            )
        with sync_playwright() as p:
            browser = _launch(p, browser_args)
            try:
                return _capture_in_browser(
                    browser, url, out_dir, remove_selectors, link_rewriter,
                    resource_fallback, insecure_tls, credential, live_session,
                    ai_session, mobile_screenshot,
                )
            finally:
                browser.close()
    except PlaywrightError as e:
        if _DOWNLOAD_MARKER in str(e):
            raise CaptureDownloadError(f"{url} 은 파일 다운로드 URL: {e}") from e
        if any(marker in str(e) for marker in _CONNECT_ERROR_MARKERS):
            raise CaptureConnectError(f"{url} 캡처 실패: {e}") from e
        raise CaptureError(f"{url} 캡처 실패: {e}") from e


def _capture_in_browser(
    browser,
    url: str,
    out_dir: Path,
    remove_selectors: tuple[str, ...],
    link_rewriter: LinkRewriter | None,
    resource_fallback: ResourceFallback | None = None,
    insecure_tls: bool = False,
    credential: CaptureCredential | None = None,
    live_session: "object | None" = None,
    ai_session: "object | None" = None,
    mobile_screenshot: bool = False,
) -> CaptureResult:
    """브라우저 하나 안에서 캡처 — 컨텍스트를 만들고 끝나면 닫는다.

    insecure_tls 는 인증서 검증을 무시한다 — 자체 서명 인증서로 https 만
    서빙하는 사이트(사설 NAS 등)의 재시도 경로(pipeline)에서만 켠다.
    컨텍스트 옵션이라 페이지의 하위 자원 요청·context.request 폴백에도
    적용된다.
    credential 이 있으면 종류별로 컨텍스트에 주입한다 (http_credentials/
    storage_state) — jwt 는 대상 origin 요청에만 Authorization 헤더를 붙인다.
    """
    _, _, PlaywrightError, PlaywrightTimeoutError = browser_engine.get_engine()

    context_kwargs, jwt_authorization = _context_options(credential, insecure_tls)
    context = browser.new_context(**context_kwargs)
    if jwt_authorization and credential is not None:
        _install_origin_scoped_header(context, credential.origin, jwt_authorization)
    try:
        page = context.new_page()
        page.set_default_timeout(config.PAGE_LOAD_TIMEOUT_MS)
        _expose_sha256_binding(page)
        # 탐색이 다운로드로 전환되면 goto 가 "Download is starting" 으로 실패하지만,
        # 이 시점에 브라우저는 이미 WAF/안티봇을 통과해 파일을 받았다. 이미 발사된
        # download 이벤트를 놓치지 않도록 goto 전에 리스너를 단다.
        downloads: list = []
        page.on("download", lambda d: downloads.append(d))
        try:
            response = page.goto(
                url, wait_until="load", timeout=config.PAGE_LOAD_TIMEOUT_MS
            )
            # 네트워크가 잠잠해질 때까지 짧게만 더 기다린다 — 분석 스크립트·
            # 롱폴링이 있는 페이지는 networkidle 에 영영 도달하지 않으므로
            # 상한을 두고, 미도달이면 현재 상태로 진행한다 (load 는 이미 도달)
            try:
                page.wait_for_load_state(
                    "networkidle", timeout=config.NETWORK_IDLE_TIMEOUT_MS
                )
            except PlaywrightTimeoutError:
                logger.info("networkidle 미도달 — 현재 상태로 진행: %s", url)
        except PlaywrightTimeoutError as e:
            if page.url == "about:blank":
                # 네비게이션이 시작조차 못함(연결 불가) — 재시도 무의미
                raise CaptureConnectError(f"{url} 캡처 실패: {e}") from e
            # 응답 없는 하위 자원(죽은 외부 이미지 등)은 load 를 영영
            # 막는다 — DOM 은 이미 파싱됐으므로 매달린 로드를 끊고
            # 현재 상태로 진행한다 (http_status 는 알 수 없으므로 None).
            # window.stop() 없이는 스크린샷의 fonts.ready 대기도 매달린다
            logger.warning("load 미도달, 현재 DOM 으로 진행: %s", url)
            try:
                page.evaluate("window.stop()")
            except Exception as stop_err:
                logger.warning("window.stop() 실패, 그대로 진행: %s", stop_err)
            response = None
        except PlaywrightError as e:
            # 탐색이 파일 다운로드로 전환 — URL 이 페이지가 아니라 파일이다.
            # 컨텍스트가 살아 있는 지금 브라우저가 받은 파일을 out_dir 에 빼내고
            # (httpx 재요청은 WAF 의 TLS 핑거프린팅에 막힌다) enriched 오류로
            # 전파한다. 그 외 PlaywrightError 는 _capture_once 가 분류하도록 재전파.
            if _DOWNLOAD_MARKER not in str(e):
                raise
            raise _capture_download(page, url, out_dir, downloads) from e

        raw_html = page.content()
        # 봇 차단/사람 확인 챌린지 페이지면 정상 콘텐츠가 아니므로 저장하지
        # 않는다 — 그대로 해시하면 차단 페이지가 스냅샷으로 둔갑하거나 직전과
        # 같은 해시로 묻혀 아카이브를 오염시킨다 (아키텍처 원칙 3). raw.html 을
        # 쓰기 전에 판정해 out_dir 을 깨끗이 둔다.
        title = page.title()
        reason = challenge_reason(
            raw_html, response.status if response else None, page.url, title
        )
        challenged = bool(reason)
        if reason:
            # 스텔스 캡처면 비상호작용 챌린지가 자동 통과하도록 잠시 기다린다.
            # 풀리면 갱신된 raw_html 로 진행, 끝내 안 풀리면 차단으로 실패.
            raw_html, reason = _await_challenge_clear(page, raw_html, reason)
        if reason and ai_session is not None:
            # 자동으로 안 풀린 양성 게이트(동의·연령 확인 등) — 비전 LLM 으로 입력을
            # 대신 수행해 통과를 시도한다(B). 못 풀면 reason 이 유지돼 아래 사람
            # 보조(C)로 캐스케이드한다. 세션은 설정 완비 + enabled 일 때만 주입된다.
            raw_html, reason = ai_session.solve(page, reason)
        if reason and live_session is not None and config.LIVE_CHALLENGE:
            # 자동으로 안 풀린 인터랙티브 챌린지 — 최후 수단으로 사람이 대시보드
            # 에서 직접 풀게 한다 (page 가 살아있는 이 지점에서 그대로 이어간다).
            raw_html, reason = live_session.solve(page, reason)
        if reason:
            raise CaptureChallengeError(f"{url}: {reason}")
        if challenged:
            title = page.title()  # 챌린지 통과로 DOM 이 바뀌었으니 최종 제목 재독
        (out_dir / "raw.html").write_text(raw_html, encoding="utf-8")
        document_links = _collect_document_links(page)
        page_links = _collect_page_links(page)
        # 상대경로 자원 인라인 기준 base — 링크 재작성(_rewrite_links)이 <base> 를
        # 떼기 전에 떠 둔다. 떼인 뒤 document.baseURI 로 풀면 <base href> 가 가리키던
        # 곳이 아니라 문서 URL 로 잘못 절대화된다.
        base_uri = page.evaluate("document.baseURI")

        # 추출용 content_html 은 인라인 전의 raw_html 기준으로 만든다 —
        # 인라인 후 DOM 에는 base64 데이터가 섞여 extract 가 느려진다.
        content_html = raw_html
        if remove_selectors:
            content_html, removed = page.evaluate(
                _REMOVE_JS, [raw_html, list(remove_selectors)]
            )
            logger.info("도메인 룰로 노드 %d개 제거: %s", removed, url)

        n_ext, n_inline = _remove_trackers(page)
        if n_ext + n_inline:
            logger.info("추적기 제거 (page.html): 외부 %d · 인라인/noscript %d: %s",
                        n_ext, n_inline, page.url)

        n_consent = _remove_consent_overlays(page)
        if n_consent:
            logger.info("쿠키 동의 오버레이 제거 (page.html): %d개: %s",
                        n_consent, page.url)

        if link_rewriter is not None:
            _rewrite_links(page, link_rewriter, page_links)

        incomplete = False
        try:
            page.screenshot(path=str(out_dir / "screenshot.png"), full_page=True)
        except PlaywrightError as e:
            # 무거운 full-page DOM 은 기본 타임아웃(30s) 안에 래스터화를 못
            # 끝내기도 한다. HTML·자원은 이미 수집했으므로 스크린샷 한 장
            # 때문에 캡처 전체를 버리지 않고, 불완전 표식만 남겨 진행한다.
            logger.warning("스크린샷 실패, 불완전 스냅샷으로 진행: %s — %s", url, e)
            (out_dir / "screenshot.png").unlink(missing_ok=True)  # 부분 파일 제거
            incomplete = True
        if mobile_screenshot:
            # 데스크탑 캡처가 끝난 뒤, 같은 브라우저에 안드로이드 크롬 모바일
            # 컨텍스트를 새로 띄워 같은 URL 을 한 번 더 열고 스크린샷을 찍는다
            # (UA 가 컨텍스트 옵션이라 기존 page 의 뷰포트만 바꿔서는 불가).
            # 데스크탑 컨텍스트/page 는 건드리지 않는다.
            _capture_mobile_screenshot(browser, url, out_dir, credential, insecure_tls)
        page_html, resource_urls = _inline_resources(
            page, raw_html, resource_fallback, base_uri
        )
        (out_dir / "page.html").write_text(page_html, encoding="utf-8")

        return CaptureResult(
            final_url=page.url,
            http_status=response.status if response else None,
            title=title or None,
            raw_html=raw_html,
            content_html=content_html,
            document_links=document_links,
            page_links=page_links,
            resource_urls=resource_urls,
            incomplete=incomplete,
        )
    finally:
        context.close()


def _capture_download(page, url: str, out_dir: Path, downloads: list) -> CaptureDownloadError:
    """탐색이 다운로드로 전환됐을 때, 브라우저가 받은 파일을 out_dir 에 저장한다.

    goto 가 "Download is starting" 으로 실패한 시점에 브라우저는 이미 WAF 를
    통과해 파일을 받았다. 이미 발사된 download 이벤트(downloads)나 곧 도달할
    이벤트를 받아 out_dir/files 에 임시 이름으로 저장하고(최종 파일명은 pipeline
    이 documents 로 정한다), 파일·제안명·최종 URL 을 실은 CaptureDownloadError 를
    돌려준다. 다운로드를 못 잡으면 정보 없이 돌려줘 pipeline 이 httpx 폴백하게 한다.
    """
    download = downloads[0] if downloads else None
    if download is None:
        try:
            download = page.wait_for_event(
                "download", timeout=config.RESOURCE_FETCH_TIMEOUT_MS
            )
        except Exception:  # noqa: BLE001 — 못 잡으면 httpx 폴백
            download = None
    if download is None:
        return CaptureDownloadError(f"{url} 은 파일 다운로드 URL")
    try:
        files_dir = out_dir / "files"
        files_dir.mkdir(parents=True, exist_ok=True)
        # 임시 이름으로 저장 (확장자·정제는 documents.entry_from_local_file 이 처리)
        saved = files_dir / f"download-{hashlib.sha256(url.encode()).hexdigest()[:16]}"
        download.save_as(str(saved))
    except Exception as e:  # noqa: BLE001 — 저장 실패 시 httpx 폴백
        logger.warning("브라우저 다운로드 저장 실패(httpx 폴백): %s — %s", url, e)
        return CaptureDownloadError(f"{url} 은 파일 다운로드 URL: {e}")
    suggested = None
    final_url = url
    try:
        suggested = download.suggested_filename
        final_url = download.url or url
    except Exception:  # noqa: BLE001 — 메타 조회 실패는 무해 (파일은 이미 저장됨)
        pass
    return CaptureDownloadError(
        f"{url} 은 파일 다운로드 URL",
        download_path=saved,
        suggested_filename=suggested,
        final_url=final_url,
    )


def _capture_mobile_screenshot(
    browser,
    url: str,
    out_dir: Path,
    credential: CaptureCredential | None,
    insecure_tls: bool,
) -> None:
    """안드로이드 크롬으로 위장한 모바일 컨텍스트로 같은 URL 을 다시 열어
    전체 페이지 스크린샷을 screenshot-mobile.png 로 저장.

    User-Agent 는 컨텍스트 생성 옵션이라 데스크탑 page 의 뷰포트만 바꿔서는
    바꿀 수 없다 — 모바일 UA(config.MOBILE_SCREENSHOT_USER_AGENT)·뷰포트
    (390×844)·isMobile/hasTouch 를 가진 컨텍스트를 같은 브라우저에 새로 띄워
    url 을 한 번 더 캡처한다. 자격증명·인증서 무시 설정은 데스크탑 캡처와
    동일하게 적용한다 (origin 스코프 유지).

    부가 산출물이라 실패해도 캡처 전체를 막지 않는다(best-effort) — 모바일
    스크린샷만 빠지고 나머지 산출물은 그대로 저장된다. 모바일 UA 로만 다른
    호스트의 루프백으로 리다이렉트되면(모바일 전용 우회) 저장하지 않는다 —
    대시보드 자신이 모바일 스크린샷으로 새는 것을 막는다(아키텍처 원칙 7·
    netcheck). 네트워크 게이트는 본래 pipeline 의 몫이라 capture 는 호출자가
    검증한 url 을 신뢰하지만, 모바일 재캡처의 리다이렉트는 pipeline 이 보지
    못하므로 이 루프백 한 가지만 여기서 막는다.
    """
    _, _, _, PlaywrightTimeoutError = browser_engine.get_engine()
    context_kwargs, jwt_authorization = _context_options(credential, insecure_tls)
    context_kwargs.update(
        user_agent=config.MOBILE_SCREENSHOT_USER_AGENT,
        viewport={"width": config.MOBILE_SCREENSHOT_WIDTH,
                  "height": config.MOBILE_SCREENSHOT_HEIGHT},
        is_mobile=True,
        has_touch=True,
    )
    try:
        context = browser.new_context(**context_kwargs)
    except Exception as e:  # noqa: BLE001 — 엔진이 isMobile 미지원 등
        logger.warning("모바일 컨텍스트 생성 실패, 모바일 스크린샷 건너뜀: %s", e)
        return
    try:
        if jwt_authorization and credential is not None:
            _install_origin_scoped_header(
                context, credential.origin, jwt_authorization
            )
        page = context.new_page()
        page.set_default_timeout(config.PAGE_LOAD_TIMEOUT_MS)
        try:
            page.goto(url, wait_until="load", timeout=config.PAGE_LOAD_TIMEOUT_MS)
            try:
                page.wait_for_load_state(
                    "networkidle", timeout=config.NETWORK_IDLE_TIMEOUT_MS
                )
            except PlaywrightTimeoutError:
                pass  # 분석 스크립트·롱폴링 — 현재 상태로 진행
        except PlaywrightTimeoutError:
            # 응답 없는 하위 자원이 load 를 막으면 매달린 로드를 끊고 진행
            try:
                page.evaluate("window.stop()")
            except Exception:  # noqa: BLE001
                pass
        # 요청한 호스트와 다른 호스트의 루프백으로 리다이렉트됐으면 생략한다.
        # (요청 url 자체가 루프백인 경우는 호출자가 검증한 것이므로 신뢰 —
        #  데스크탑 캡처와 같은 신뢰 모델. pipeline 은 운영에서 루프백 url 을
        #  애초에 캡처로 넘기지 않는다.)
        final_host = urlsplit(page.url).hostname or ""
        if (final_host != (urlsplit(url).hostname or "")
                and netcheck.classify_host(final_host) == netcheck.LOOPBACK):
            logger.warning(
                "모바일 캡처가 루프백으로 리다이렉트됨 — 모바일 스크린샷 생략: %s",
                page.url,
            )
            return
        page.wait_for_timeout(config.MOBILE_SCREENSHOT_SETTLE_MS)
        page.screenshot(
            path=str(out_dir / "screenshot-mobile.png"), full_page=True
        )
    except Exception as e:  # noqa: BLE001 — 부가 산출물 실패가 캡처를 깨지 않게
        logger.warning("모바일 스크린샷 실패, 건너뜀: %s", e)
        (out_dir / "screenshot-mobile.png").unlink(missing_ok=True)
    finally:
        context.close()


def _collect_document_links(page) -> list[str]:
    """DOM 에서 문서 파일(PDF·워드·한글 등) 링크를 수집 (중복 제거, 순서 보존).

    수집 실패가 캡처 자체를 막아서는 안 되므로 실패 시 빈 목록을 반환한다.
    """
    try:
        urls: list[str] = page.evaluate(_DOC_LINK_JS)
    except Exception as e:
        logger.warning("문서 링크 수집 실패, 건너뜀: %s", e)
        return []
    # fragment(#page=2 등)만 다른 링크는 같은 문서 — 제거 후 중복 제거
    return list(dict.fromkeys(
        urldefrag(u).url for u in urls if documents.is_document_url(u)
    ))


def _collect_page_links(page) -> list[str]:
    """DOM 의 모든 앵커 href 를 절대 URL 로 수집 (중복 제거, 순서 보존).

    수집 실패가 캡처 자체를 막아서는 안 되므로 실패 시 빈 목록을 반환한다.
    """
    try:
        urls: list[str] = page.evaluate(_PAGE_LINK_JS)
    except Exception as e:
        logger.warning("페이지 링크 수집 실패, 건너뜀: %s", e)
        return []
    return list(dict.fromkeys(u for u in urls if u))


def _rewrite_links(page, link_rewriter: LinkRewriter, page_links: list[str]) -> None:
    """링크 재작성 적용 — 실패해도 캡처 자체는 유효 (원본 링크 유지)."""
    try:
        mapping = link_rewriter(page_links)
        if mapping:
            rewritten = page.evaluate(_REWRITE_LINKS_JS, mapping)
            logger.info("링크 %d개 재작성: %s", rewritten, page.url)
    except Exception as e:
        logger.warning("링크 재작성 실패, 원본 링크 유지: %s", e)


def _sha256_of_base64(b64: str) -> str:
    """base64 문자열을 디코딩한 바이트의 sha256 hex.

    _INLINE_JS 의 자원 해시 폴백 — crypto.subtle 이 없는 비보안 컨텍스트
    (http 페이지)에서 페이지 바인딩(window._wccgSha256)으로 호출된다.
    데이터는 FileReader 의 data URL 에서 잘라낸 base64 부분이라 항상 유효하다.
    """
    return hashlib.sha256(base64.b64decode(b64)).hexdigest()


def _expose_sha256_binding(page) -> None:
    """sha256 페이지 바인딩 등록 — 실패해도 캡처는 계속된다.

    바인딩이 없으면 http 페이지의 자원 URL 매핑만 빠진다 (저장은 정상).
    """
    try:
        page.expose_function("_wccgSha256", _sha256_of_base64)
    except Exception as e:
        logger.warning("sha256 바인딩 등록 실패 — http 페이지의 자원 URL 매핑 생략: %s", e)


def _inline_resources(
    page,
    raw_html: str,
    resource_fallback: ResourceFallback | None = None,
    base_uri: str | None = None,
) -> tuple[str, dict[str, str]]:
    """<img src>, <link rel=stylesheet>, 폰트를 data URI로 치환한 단일 HTML 생성.

    페이지 컨텍스트 fetch() 가 CORS 로 막힌 자원은 context.request 폴백으로
    재시도하고, 그래도 실패한 자원은 resource_fallback(과거 캡처본 재사용)을
    시도한다. 끝내 실패한 자원만 원본 URL을 유지하며 경고 로그를 남긴다.
    (HTML, 인라인 자원의 sha256 → 원본 URL) 반환.
    """
    resource_urls: dict[str, str] = {}
    try:
        # 작업을 백그라운드로 시작만 시키고(즉시 반환) 완료를 wait_for_function 으로
        # 기다린다 — page.evaluate 는 타임아웃을 무시해 무한 hang 위험이 있어서다.
        # JS 전체 데드라인(overallMs)이 1차, 이 대기(+여유)가 2차 강제 상한.
        page.evaluate(_INLINE_LAUNCH_JS, {
            "timeoutMs": config.RESOURCE_FETCH_TIMEOUT_MS,
            "concurrency": config.RESOURCE_FETCH_CONCURRENCY,
            "baseUrl": base_uri,
            "overallMs": config.INLINE_OVERALL_TIMEOUT_MS,
            "maxCount": config.RESOURCE_INLINE_MAX_COUNT,
        })
        page.wait_for_function(
            "() => window.__wccgInlineDone === true",
            timeout=config.INLINE_OVERALL_TIMEOUT_MS + 15_000,
        )
        result: dict = page.evaluate("() => window.__wccgInlineResult") or {}
        if result.get("timedOut"):
            logger.warning("자원 인라인 전체 데드라인 초과 — 부분결과로 진행")
        if result.get("error"):
            logger.warning("자원 인라인 작업 오류: %s", result["error"])
        failed: list[dict] = result.get("failed", [])
        resource_urls = {
            i["sha256"]: i["url"] for i in result.get("inlined", []) if i.get("sha256")
        }
        # 같은 자원이 CSS 에 여러 번 나오면 failed 도 그만큼 중복된다(같은 url()
        # 토큰 수백~수천 회) — 중복을 접고 개수를 제한해야 재시도·적용(_APPLY_INLINE_JS
        # page.evaluate) 페이로드 폭발과 그로 인한 메모리 폭증·hang 을 막는다. 적용은
        # 항목당 DOM 의 모든 매칭 노드를 갱신하므로 고유 항목 1개면 충분하다.
        failed = _dedupe_failed(failed)
        if len(failed) > config.RESOURCE_INLINE_MAX_COUNT:
            logger.warning(
                "인라인 실패 자원 %d개 — 상한 %d 초과분은 원본 URL 유지",
                len(failed), config.RESOURCE_INLINE_MAX_COUNT,
            )
            failed = failed[:config.RESOURCE_INLINE_MAX_COUNT]
        if failed:
            failed = _retry_inline_via_context(page, failed, resource_urls)
        if failed and resource_fallback is not None:
            failed = _apply_resource_fallback(
                page, failed, resource_fallback, resource_urls
            )
        for item in failed:
            logger.warning(
                "자원 인라인 실패(원본 URL 유지): %s", item.get("url") or item.get("raw")
            )
        return page.content(), resource_urls
    except Exception as e:  # 인라인이 실패해도 캡처 자체는 유효
        logger.warning("자원 인라인 단계 실패, raw HTML로 대체: %s", e)
        return raw_html, resource_urls


def _dedupe_failed(failed: list[dict]) -> list[dict]:
    """인라인 실패 목록의 중복 제거 — 같은 (kind, url, raw)는 한 번만 둔다.

    같은 @font-face url() 이 CSS 에 반복되면 failed 가 동일 항목으로 부풀어
    재시도·적용 페이로드가 폭발한다. 적용(_APPLY_INLINE_JS)은 항목당 DOM 의
    모든 매칭 노드를 갱신하므로 고유 항목만 남겨도 결과는 같다.
    """
    seen: set[tuple] = set()
    out: list[dict] = []
    for item in failed:
        key = (item.get("kind"), item.get("url"), item.get("raw"))
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _retry_inline_via_context(
    page, failed: list[dict], resource_urls: dict[str, str]
) -> list[dict]:
    """CORS 로 막힌 자원을 브라우저 밖 API 요청(context.request)으로 재시도.

    context.request 는 CORS 제약이 없고 컨텍스트의 쿠키/UA 를 공유한다.
    핫링크 보호 대비 Referer 를 현재 페이지로 보낸다. 성공분은 DOM 에 반영하고
    (sha256 → URL 을 resource_urls 에 기록), 끝내 실패한 항목만 돌려준다.
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
        replacements.append(_replacement_for(item, url, content_type, body))
        resource_urls[hashlib.sha256(body).hexdigest()] = url
    if replacements:
        page.evaluate(_APPLY_INLINE_JS, replacements)
    return still_failed


def _replacement_for(item: dict, url: str, content_type: str, body: bytes) -> dict:
    """받아온 자원 바이트 → _APPLY_INLINE_JS 치환 항목."""
    if item["kind"] == "css":
        css_text = _absolutize_css_urls(body.decode("utf-8", errors="replace"), url)
        return {"kind": "css", "url": url, "cssText": css_text}
    encoded = base64.b64encode(body).decode("ascii")
    return {**item, "dataUrl": f"data:{content_type};base64,{encoded}"}


def _apply_resource_fallback(
    page,
    failed: list[dict],
    resource_fallback: ResourceFallback,
    resource_urls: dict[str, str],
) -> list[dict]:
    """끝내 받지 못한 자원을 과거 캡처본(자원 CAS)으로 메운다.

    같은 URL 로 저장된 콘텐츠가 있으면 재사용한다 — 그 사이 원본이 바뀌었을
    수 있지만, 라이브 URL 을 남겨 뷰어가 원본 사이트로 요청을 흘리는 것보다
    낫다 (정적 자원 전제). 성공분은 DOM 에 반영하고 남은 실패만 돌려준다.
    """
    replacements: list[dict] = []
    still_failed: list[dict] = []
    for item in failed:
        url = item.get("url")
        result = resource_fallback(url) if url else None
        if result is None:
            still_failed.append(item)
            continue
        content_type, body = result
        replacements.append(_replacement_for(item, url, content_type, body))
        resource_urls[hashlib.sha256(body).hexdigest()] = url
        logger.info("자원 인라인 실패 — 이전 캡처본 재사용: %s", url)
    if replacements:
        page.evaluate(_APPLY_INLINE_JS, replacements)
    return still_failed


def fetch_documents_via_browser(
    session: "BrowserSession | None",
    links: list[str],
    dest_dir: Path,
    *,
    referer: str | None = None,
    credential: CaptureCredential | None = None,
    insecure_tls: bool = False,
    limits: "documents.DocumentLimits | None" = None,
) -> tuple[list[dict[str, object]], list[str]]:
    """링크 문서들을 브라우저 네트워크 스택(context.request)으로 받아 (manifest, 실패 URL).

    httpx 와 달리 Chromium 네트워크 스택을 경유하므로 WAF 의 TLS 핑거프린팅을
    통과한다(루트 원인 — `[Errno 104] Connection reset by peer`). 컨텍스트가
    credential(http_basic/storage_state)·insecure_tls 를 반영하지만 jwt 토큰은
    context.request 에 안 붙으므로(_install_origin_scoped_header 한계), 그런 문서는
    실패로 돌아가 호출부의 httpx 폴백이 인증을 싣는다. session 이 있으면 그 브라우저를
    재사용하고(새 컨텍스트만), 없으면 일회용으로 띄운다.
    """
    limits = limits or documents.DEFAULT_LIMITS
    links = list(dict.fromkeys(links))
    if len(links) > limits.max_count:
        logger.warning(
            "문서 링크 %d개 중 앞 %d개만 시도 (개수 한도)", len(links), limits.max_count
        )
        links = links[: limits.max_count]
    if not links:
        return [], []

    manifest: list[dict[str, object]] = []
    failed: list[str] = []

    def _run(browser) -> None:
        context_kwargs, jwt_authorization = _context_options(credential, insecure_tls)
        context = browser.new_context(**context_kwargs)
        if jwt_authorization and credential is not None:
            _install_origin_scoped_header(context, credential.origin, jwt_authorization)
        try:
            for url in links:
                try:
                    manifest.append(
                        _fetch_document_via_context(context, url, dest_dir, referer, limits)
                    )
                except Exception as e:  # noqa: BLE001 — 실패분은 httpx 폴백 대상
                    logger.warning("브라우저 문서 다운로드 실패(폴백 대상): %s — %s", url, e)
                    failed.append(url)
        finally:
            context.close()

    try:
        if session is not None:
            _run(session.browser())
        else:
            _, sync_playwright, _, _ = browser_engine.get_engine()
            with sync_playwright() as p:
                browser = _launch(p)
                try:
                    _run(browser)
                finally:
                    browser.close()
    except Exception as e:  # noqa: BLE001 — 브라우저 기동·컨텍스트 실패는 전부 httpx 폴백
        logger.warning("브라우저 문서 다운로드 불가(전부 httpx 폴백): %s", e)
        done = {str(m["url"]) for m in manifest}
        failed = [u for u in links if u not in done]
    return manifest, failed


def _fetch_document_via_context(
    context, url: str, dest_dir: Path, referer: str | None,
    limits: "documents.DocumentLimits",
) -> dict[str, object]:
    """문서 1개를 context.request 로 받아 dest_dir 에 저장하고 manifest 항목 반환.

    HTML 응답(로그인/오류 페이지)·크기 한도 초과는 ValueError 로 거부한다
    (httpx 경로와 동일 정책). 파일명은 documents.document_filename(URL 경로 기반).
    """
    headers = {"User-Agent": config.USER_AGENT}
    if referer:
        headers["Referer"] = referer
    resp = context.request.get(
        url, headers=headers, timeout=limits.timeout_seconds * 1000
    )
    if not resp.ok:
        raise ValueError(f"http {resp.status}")
    content_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type in documents._HTML_TYPES:
        raise ValueError(f"HTML 응답 — 문서 아님 ({content_type})")
    body = resp.body()
    if len(body) > limits.max_bytes:
        raise ValueError(f"크기 한도 초과 (> {limits.max_bytes} bytes)")
    name = documents.document_filename(url)
    dest_dir.mkdir(parents=True, exist_ok=True)
    (dest_dir / name).write_bytes(body)
    return {
        "url": url,
        "file": name,
        "bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "content_type": content_type or "application/octet-stream",
    }


def _fetch_via_context(page, url: str) -> tuple[str, bytes] | None:
    """자원 1개를 context.request 로 받아 (content-type, body) 반환. 실패 시 None."""
    try:
        resp = page.context.request.get(
            url,
            headers={"Referer": page.url},
            timeout=config.RESOURCE_FETCH_TIMEOUT_MS,
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


# TLS 인증서/SSL 단계의 실패 — 검증 무시(insecure_tls) 재시도 판단용
_CERT_ERROR_MARKERS = ("net::ERR_CERT_", "net::ERR_SSL_")


def is_cert_error(exc: Exception) -> bool:
    """캡처 실패가 인증서/SSL 단계인지 — 자체 서명 인증서 등.

    pipeline 이 검증 무시로 https 를 한 번 더 시도할지 판단하는 데 쓴다.
    """
    return any(marker in str(exc) for marker in _CERT_ERROR_MARKERS)


# 탐색이 페이지 로드 대신 파일 다운로드로 전환됐을 때의 Playwright 오류 문구 —
# URL 이 문서 파일 직접 링크(download.php 등)라는 뜻이다
_DOWNLOAD_MARKER = "Download is starting"


# 서버 연결 단계에서 나는 chromium 네트워크 오류 (DNS 실패는 스킴과 무관하므로 제외)
_CONNECT_ERROR_MARKERS = (
    "net::ERR_CONNECTION_",
    "net::ERR_SSL_",
    "net::ERR_CERT_",
    "net::ERR_TIMED_OUT",
    "net::ERR_ADDRESS_UNREACHABLE",
)


# 봇 차단/사람 확인 챌린지 페이지의 마커 (raw_html·title·final_url 에서 소문자
# 비교로 탐지). 잡히면 정상 콘텐츠가 아니므로 스냅샷으로 저장하지 않는다 —
# 차단 페이지를 해시하면 아카이브가 오염된다 (아키텍처 원칙 3).
#
# 두 단계로 나눈다. 폼에 Turnstile 위젯을 박아둔 정상 페이지(그누보드 계열
# 커뮤니티 등)는 본문이 멀쩡한 채로 위젯 마커를 갖기 때문이다.
#
# _INTERSTITIAL_MARKERS — 페이지 자체가 차단/챌린지 인터스티셜이라는 강한 신호.
#   관리형 챌린지의 오케스트레이트 스크립트·토큰, 인터스티셜 제목, 전면 차단
#   문구 등. 있으면 HTTP 상태와 무관하게 차단으로 본다.
_INTERSTITIAL_MARKERS = (
    "/cdn-cgi/challenge-platform/",        # Cloudflare 챌린지 오케스트레이트 스크립트
    "__cf_chl_",                           # 챌린지 토큰/오케스트레이트
    "cf_chl_opt",
    "just a moment...",                    # Cloudflare 인터스티셜 제목
    "attention required! | cloudflare",    # Cloudflare 차단 제목
    "enable javascript and cookies to continue",
    "there was a problem providing the content you requested",  # Elsevier/ScienceDirect 전면 차단
)

# _WIDGET_MARKERS — Turnstile 위젯이 '박혀 있다'는 약한 신호. 스팸 방지용으로
#   로그인·글쓰기·검색 폼에 Turnstile 을 넣은 정상 페이지도 본문이 멀쩡한 채
#   이 마커를 가진다. 따라서 단독으로는 차단으로 보지 않고, 응답 자체가 차단을
#   가리킬 때(http_status >= 400)만 차단으로 처리한다 — 정상 200 페이지에 폼
#   위젯으로 박힌 Turnstile 을 오탐하지 않게 한다.
_WIDGET_MARKERS = (
    "challenges.cloudflare.com",           # Turnstile iframe / api.js src
    "cf-turnstile",                        # Turnstile 위젯 컨테이너
    "verify you are human",                # 사람 확인 문구 (영문)
    "사람인지 확인",                         # 사람 확인 문구 (한글)
)


def challenge_reason(
    raw_html: str,
    http_status: int | None,
    final_url: str,
    title: str | None,
) -> str | None:
    """봇 차단/사람 확인 챌린지 페이지면 사유 문자열, 아니면 None.

    순수 함수 — Playwright 없이 단위 테스트할 수 있다. raw_html·title·final_url
    을 소문자로 합쳐 마커를 찾는다.

    _INTERSTITIAL_MARKERS(강한 신호)는 HTTP 상태와 무관하게 차단으로 본다.
    _WIDGET_MARKERS(약한 신호 — 폼에 박힌 Turnstile 위젯 등 정상 페이지에도
    나타남)는 응답이 명시적 성공(2xx/3xx)이 아닐 때만 차단으로 본다 — 즉
    정상 200 페이지의 임베드 위젯은 통과시키되(오탐 방지), 4xx/5xx 차단 응답과
    상태 미상(http_status=None — 챌린지 통과 대기/라이브 재검사 폴링 경로)에서는
    보수적으로 차단으로 둔다 (마커 없는 4xx/5xx 는 정상 콘텐츠일 수 있어
    상태만으로는 차단으로 보지 않는다).
    """
    haystack = "\n".join(s for s in (raw_html, title or "", final_url) if s).lower()
    status = f"http {http_status} · " if http_status else ""
    for marker in _INTERSTITIAL_MARKERS:
        if marker in haystack:
            return f"봇 차단/사람 확인 챌린지 감지 ({status}마커: {marker!r})"
    if http_status is None or http_status >= 400:
        for marker in _WIDGET_MARKERS:
            if marker in haystack:
                return f"봇 차단/사람 확인 챌린지 감지 ({status}마커: {marker!r})"
    return None


def _stealth_active() -> bool:
    """스텔스 캡처가 켜졌는지 — patchright 엔진 또는 헤드풀.

    켜져 있으면 비상호작용 챌린지가 자동 통과할 가망이 있어 대기한다. 헤드리스
    기본(playwright)에선 가망이 없어 챌린지를 즉시 실패시킨다(기존 동작 유지).
    """
    return config.CAPTURE_ENGINE == "patchright" or config.CAPTURE_HEADFUL


def _await_challenge_clear(page, raw_html: str, reason: str) -> tuple[str, str | None]:
    """챌린지가 감지되면 자동 통과(비상호작용)를 기다린다.

    스텔스 캡처에서만 대기한다. config.CHALLENGE_WAIT_SECONDS 동안 폴링하며
    챌린지 마커가 사라지면 (갱신된 raw_html, None) 을, 시간 초과면 (raw_html,
    reason) 을 반환한다. 하드 블록(403 인터스티셜·인터랙티브 Turnstile)은
    풀리지 않아 시간 초과로 실패하고, 사람 개입이 필요하다.
    """
    if not _stealth_active():
        return raw_html, reason
    logger.info(
        "챌린지 감지 — 자동 통과 대기 (최대 %ds): %s",
        config.CHALLENGE_WAIT_SECONDS, page.url,
    )
    deadline = time.monotonic() + config.CHALLENGE_WAIT_SECONDS
    while time.monotonic() < deadline:
        try:
            page.wait_for_timeout(config.CHALLENGE_WAIT_POLL_MS)
            raw_html = page.content()
            title = page.title()
        except Exception:
            break  # 페이지가 닫혔거나 네비게이션 중 — 현재 상태로 판정
        if challenge_reason(raw_html, None, page.url, title) is None:
            logger.info("챌린지 통과 — 캡처 진행: %s", page.url)
            return raw_html, None
    logger.warning("챌린지 대기 시간 초과 — 차단으로 처리: %s", page.url)
    return raw_html, reason


def _remove_consent_overlays(page) -> int:
    """page.html 저장 전 live DOM 에서 쿠키 동의(CMP) 오버레이 제거.

    스냅샷 렌더는 iframe sandbox 라 스크립트가 안 돌아 배너를 닫을 수 없어 본문을
    가린다 — 알려진 CMP 컨테이너(consent_overlays.SELECTORS)를 미리 제거하고
    스크롤 잠금을 푼다. raw.html·content_html(해시/diff)에는 영향 없다. 실패해도
    캡처는 계속된다. 제거한 노드 수 반환.
    """
    try:
        return page.evaluate(_REMOVE_CONSENT_JS, list(consent_overlays.SELECTORS))
    except Exception as e:
        logger.warning("쿠키 동의 오버레이 제거 실패: %s", e)
        return 0


def _remove_trackers(page) -> tuple[int, int]:
    """page.html 저장 전 live DOM 에서 추적기 스크립트·픽셀 제거.

    실패해도 캡처 자체는 계속된다. (외부 제거 수, 인라인/noscript 제거 수) 반환.
    """
    n_ext = n_inline = 0
    try:
        n_ext = page.evaluate(_REMOVE_FROM_LIVE_DOM_JS, list(trackers.EXTERNAL_SELECTORS))
    except Exception as e:
        logger.warning("외부 추적기 제거 실패: %s", e)
    try:
        n_inline = page.evaluate(_REMOVE_INLINE_TRACKERS_JS, list(trackers.INLINE_PATTERNS))
    except Exception as e:
        logger.warning("인라인 추적기 제거 실패: %s", e)
    return n_ext, n_inline


class CaptureError(RuntimeError):
    pass


class CaptureConnectError(CaptureError):
    """서버 연결 자체가 안 된 실패 (443 닫힘·SSL 오류 등) — https→http 폴백 판단용."""


class CaptureDownloadError(CaptureError):
    """탐색이 파일 다운로드로 전환된 실패 — URL 이 페이지가 아니라 파일.

    pipeline 이 문서 아카이빙으로 전환하는 신호다. 브라우저가 WAF/안티봇을
    통과해 이미 받은 다운로드 파일을 들고 올 수 있다(download_path) — 그러면
    pipeline 은 httpx 재요청(WAF 에 TLS 단계에서 막힘) 없이 그 파일을 그대로
    문서 스냅샷으로 만든다. 정보 없이 raise 되면 종전처럼 httpx 폴백한다.
    """

    def __init__(
        self,
        message: str,
        *,
        download_path: "Path | None" = None,
        suggested_filename: str | None = None,
        final_url: str | None = None,
    ) -> None:
        super().__init__(message)
        self.download_path = download_path
        self.suggested_filename = suggested_filename
        self.final_url = final_url


class CaptureChallengeError(CaptureError):
    """봇 차단/사람 확인 챌린지 페이지가 감지된 실패 — 정상 콘텐츠가 아님.

    pipeline 이 http 폴백으로 새지 않고(폴백해도 못 푼다) 저장·해시를 생략한 채
    실패로만 기록하게 하는 신호다 (아키텍처 원칙 3 — 해시 오염 방지).
    """
