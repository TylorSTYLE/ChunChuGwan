"""Playwright 기반 페이지 캡처.

산출물 4종을 스냅샷 디렉토리에 저장한다:
- raw.html        렌더링 완료 후 DOM 소스 (page.content())
- page.html       이미지/CSS를 base64로 인라인한 단일 HTML
- screenshot.png  전체 페이지 스크린샷 (full_page=True)
- content.md      extract.py 가 생성 (이 모듈 밖)
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence
from urllib.parse import urldefrag, urljoin

from . import config, documents, storage, trackers

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
async ({ timeoutMs, concurrency }) => {
  const failed = [];
  const inlined = [];
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
  const toDataUrl = async (url) => {
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
  const FONT_URL_RE = /url\\((['"]?)([^)'"]+?\\.(?:woff2?|ttf|otf|eot)(?:[?#][^)'"]*)?)\\1\\)/gi;
  const inlineFonts = async (cssText, baseUrl) => {
    const matches = Array.from(cssText.matchAll(FONT_URL_RE));
    if (!matches.length) return cssText;
    // 한 CSS 안의 폰트는 한꺼번에 받는다 (보통 소수라 바운드 불필요)
    const repls = await Promise.all(matches.map(async (m) => {
      let abs = null;
      try { abs = new URL(m[2], baseUrl).href; } catch (e) {}
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
      style.textContent = await inlineFonts(style.textContent, document.baseURI);
    });
  }
  for (const img of Array.from(document.querySelectorAll("img[src]"))) {
    const src = img.currentSrc || img.src;
    if (!src || src.startsWith("data:")) continue;
    tasks.push(async () => {
      try {
        const dataUrl = await toDataUrl(src);
        img.removeAttribute("srcset");
        img.src = dataUrl;
      } catch (e) {
        failed.push({ kind: "img", url: src });
      }
    });
  }
  const queue = tasks.slice();
  const worker = async () => {
    while (queue.length) await queue.shift()();
  };
  await Promise.all(
    Array.from({ length: Math.min(concurrency, tasks.length) }, worker)
  );
  return { failed, inlined };
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


# 앵커 절대 URL 목록 → {원본 href: 재작성 href}. 비거나 None 이면 재작성 없음.
LinkRewriter = Callable[[Sequence[str]], dict[str, str]]

# 자원 인라인 실패 시 과거 캡처본 조회 — URL 을 받아 (content-type, body) 또는
# None 을 반환한다 (pipeline 이 snapshot_resources + 자원 CAS 로 구현).
ResourceFallback = Callable[[str], "tuple[str, bytes] | None"]


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
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=True)
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
        "user_agent": config.USER_AGENT,
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
) -> CaptureResult:
    """URL을 렌더링해 raw.html / page.html / screenshot.png 를 out_dir에 저장.

    remove_selectors 가 있으면 저장 산출물에는 손대지 않고, 본문 추출용
    content_html 에서만 해당 노드를 제거한다.
    link_rewriter 가 있으면(사이트 전체 아카이브) page.html 의 앵커를
    반환된 매핑대로 재작성한다 — raw.html/content_html 은 원본 유지.
    session 이 있으면 그 브라우저를 재사용한다 (없으면 1회용 기동).

    일부 서버(구형 IIS 등)는 HTTP/2 구현이 깨져 chromium 만
    net::ERR_HTTP2_* 로 실패한다 (curl 등은 정상) — 이 경우 HTTP/2 를
    끄고(HTTP/1.1) 한 번 더 시도한다. 이 폴백은 브라우저 인자가 달라
    session 과 무관하게 항상 1회용으로 띄운다.
    """
    try:
        return _capture_once(url, out_dir, remove_selectors, link_rewriter,
                             session=session, resource_fallback=resource_fallback,
                             insecure_tls=insecure_tls, credential=credential)
    except CaptureError as e:
        if "ERR_HTTP2" not in str(e):
            raise
        logger.warning("HTTP/2 프로토콜 오류 — HTTP/1.1 로 재시도: %s", url)
        return _capture_once(
            url, out_dir, remove_selectors, link_rewriter,
            browser_args=("--disable-http2",),
            resource_fallback=resource_fallback,
            insecure_tls=insecure_tls, credential=credential,
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
) -> CaptureResult:
    """캡처 1회 시도 — 폴백 판단은 capture() 가 한다."""
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    try:
        if session is not None and not browser_args:
            return _capture_in_browser(
                session.browser(), url, out_dir, remove_selectors, link_rewriter,
                resource_fallback, insecure_tls, credential,
            )
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=list(browser_args))
            try:
                return _capture_in_browser(
                    browser, url, out_dir, remove_selectors, link_rewriter,
                    resource_fallback, insecure_tls, credential,
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
) -> CaptureResult:
    """브라우저 하나 안에서 캡처 — 컨텍스트를 만들고 끝나면 닫는다.

    insecure_tls 는 인증서 검증을 무시한다 — 자체 서명 인증서로 https 만
    서빙하는 사이트(사설 NAS 등)의 재시도 경로(pipeline)에서만 켠다.
    컨텍스트 옵션이라 페이지의 하위 자원 요청·context.request 폴백에도
    적용된다.
    credential 이 있으면 종류별로 컨텍스트에 주입한다 (http_credentials/
    storage_state) — jwt 는 대상 origin 요청에만 Authorization 헤더를 붙인다.
    """
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    context_kwargs, jwt_authorization = _context_options(credential, insecure_tls)
    context = browser.new_context(**context_kwargs)
    if jwt_authorization and credential is not None:
        _install_origin_scoped_header(context, credential.origin, jwt_authorization)
    try:
        page = context.new_page()
        page.set_default_timeout(config.PAGE_LOAD_TIMEOUT_MS)
        _expose_sha256_binding(page)
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

        raw_html = page.content()
        (out_dir / "raw.html").write_text(raw_html, encoding="utf-8")
        document_links = _collect_document_links(page)
        page_links = _collect_page_links(page)

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

        if link_rewriter is not None:
            _rewrite_links(page, link_rewriter, page_links)

        page.screenshot(path=str(out_dir / "screenshot.png"), full_page=True)
        page_html, resource_urls = _inline_resources(
            page, raw_html, resource_fallback
        )
        (out_dir / "page.html").write_text(page_html, encoding="utf-8")

        return CaptureResult(
            final_url=page.url,
            http_status=response.status if response else None,
            title=page.title() or None,
            raw_html=raw_html,
            content_html=content_html,
            document_links=document_links,
            page_links=page_links,
            resource_urls=resource_urls,
        )
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
    page, raw_html: str, resource_fallback: ResourceFallback | None = None
) -> tuple[str, dict[str, str]]:
    """<img src>, <link rel=stylesheet>, 폰트를 data URI로 치환한 단일 HTML 생성.

    페이지 컨텍스트 fetch() 가 CORS 로 막힌 자원은 context.request 폴백으로
    재시도하고, 그래도 실패한 자원은 resource_fallback(과거 캡처본 재사용)을
    시도한다. 끝내 실패한 자원만 원본 URL을 유지하며 경고 로그를 남긴다.
    (HTML, 인라인 자원의 sha256 → 원본 URL) 반환.
    """
    resource_urls: dict[str, str] = {}
    try:
        result: dict = page.evaluate(_INLINE_JS, {
            "timeoutMs": config.RESOURCE_FETCH_TIMEOUT_MS,
            "concurrency": config.RESOURCE_FETCH_CONCURRENCY,
        })
        failed: list[dict] = result["failed"]
        resource_urls = {
            i["sha256"]: i["url"] for i in result["inlined"] if i.get("sha256")
        }
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

    pipeline 이 문서 아카이빙(documents.download_direct)으로 전환하는 신호다.
    """
