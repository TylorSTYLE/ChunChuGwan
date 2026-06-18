// 춘추관 확장 — background service worker.
//
// popup 은 토큰을 모른다. 모든 /api/v1 호출은 여기서만 일어나고, 토큰은
// chrome.storage.local 에만 두며 fetch 헤더에 직접 주입한다. content/popup
// 컨텍스트로는 절대 전달하지 않는다 (토큰 노출면 최소화 + CORS 회피 —
// 서버에 CORS 가 없어도 host_permissions 를 받은 background fetch 는 cross-origin
// 호출이 된다).

const STORE_KEYS = ["base_url", "token", "token_prefix", "can_view", "can_archive"];

// 결과 알림 — 확장이 요청한 작업/크롤을 추적해 완료/실패/사람확인 시 데스크톱 알림.
const WATCH_KEY = "watch";            // [{kind:"job"|"crawl", id, url, last_state}]
const TARGETS_KEY = "notif_targets";  // {notificationId: 클릭 시 열 대시보드 URL}
const NOTIFY_PREF_KEY = "notify_enabled"; // 사용자 토글 (기본 on, 연결 해제에도 보존)
const POLL_ALARM = "wccg-poll";
const POLL_PERIOD_MIN = 1;            // chrome.alarms 최소 주기 (분)

const msg = (key, subs) => chrome.i18n.getMessage(key, subs) || key;

async function getConfig() {
  const c = await chrome.storage.local.get(STORE_KEYS);
  return c;
}

function normalizeBaseUrl(raw) {
  let url = (raw || "").trim();
  if (!url) return "";
  if (!/^https?:\/\//i.test(url)) url = "https://" + url;
  return url.replace(/\/+$/, ""); // 후행 슬래시 제거
}

function originPattern(url) {
  try {
    const u = new URL(url);
    return `${u.protocol}//${u.host}/*`;
  } catch (e) {
    return null;
  }
}

// base_url 오리진에 대한 host 권한 확보 (background fetch 의 cross-origin 허용).
async function ensureHostPermission(url) {
  const pattern = originPattern(url);
  if (!pattern) return false;
  if (await chrome.permissions.contains({ origins: [pattern] })) return true;
  try {
    return await chrome.permissions.request({ origins: [pattern] });
  } catch (e) {
    return false;
  }
}

// /api/v1 호출 — {ok, status, data, error} 반환. 토큰은 헤더로만.
async function apiFetch(path, { method = "GET", body = null } = {}) {
  const { base_url, token } = await getConfig();
  if (!base_url) return { ok: false, status: 0, error: "not_connected" };
  const headers = { Accept: "application/json" };
  if (token) headers["Authorization"] = "Bearer " + token;
  const init = { method, headers };
  if (body != null) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  let resp;
  try {
    resp = await fetch(base_url + path, init);
  } catch (e) {
    return { ok: false, status: 0, error: "network" };
  }
  let data = null;
  try {
    data = await resp.json();
  } catch (e) {
    /* 비 JSON 응답(예: 평문 에러) — data 는 null */
  }
  return { ok: resp.ok, status: resp.status, data };
}

// ---- 연결 (토큰 검증 + 권한 추론) ----

async function connect({ base_url, token }) {
  const normalized = normalizeBaseUrl(base_url);
  if (!normalized) return { ok: false, error: "bad_base_url" };
  const tok = (token || "").trim();
  if (!tok.startsWith("wccg_")) return { ok: false, error: "bad_token" };
  if (!(await ensureHostPermission(normalized))) {
    return { ok: false, error: "permission_denied" };
  }
  await chrome.storage.local.set({
    base_url: normalized,
    token: tok,
    token_prefix: tok.slice(0, 10),
  });
  // 가벼운 조회로 토큰 검증 + 권한 추론
  const view = await apiFetch("/api/v1/pages?url=https://example.invalid/");
  if (view.status === 401) {
    await disconnect();
    return { ok: false, error: "invalid_token" };
  }
  const canView = view.status === 200;
  // 아카이브 권한은 실제 호출 전엔 단정할 수 없다 — 보기 가능하면 일단 표시,
  // 실제 차단은 서버 403 으로 안내한다.
  await chrome.storage.local.set({ can_view: canView, can_archive: true });
  return { ok: true, prefix: tok.slice(0, 10), can_view: canView };
}

async function disconnect() {
  // 토큰·연결 정보와 추적 상태를 비운다 (notify_enabled 사용자 설정은 보존).
  await chrome.storage.local.remove([...STORE_KEYS, WATCH_KEY, TARGETS_KEY]);
  await chrome.alarms.clear(POLL_ALARM);
  await updateBadge([]);
  return { ok: true };
}

async function status() {
  const c = await getConfig();
  return {
    connected: !!c.token,
    base_url: c.base_url || "",
    prefix: c.token_prefix || "",
  };
}

// ---- 쿠키 캡슐 (로그인 페이지 1회성 인증 캡처) ----

// chrome.cookies.SameSite → Playwright storage_state SameSite
function mapSameSite(s) {
  if (s === "strict") return "Strict";
  if (s === "lax") return "Lax";
  return "None"; // no_restriction / unspecified
}

// 현재 탭 도메인의 쿠키를 Playwright storage_state 형식으로 모은다.
// httpOnly 쿠키도 chrome.cookies 로는 읽힌다(인증 세션 누락 방지). ID/PW 는
// 절대 수집하지 않는다 — 이미 로그인된 브라우저 세션의 쿠키만 캡슐화한다.
async function collectCapsule(pageUrl) {
  const pattern = originPattern(pageUrl);
  if (pattern && !(await chrome.permissions.contains({ origins: [pattern] }))) {
    const granted = await chrome.permissions.request({ origins: [pattern] });
    if (!granted) return null;
  }
  const raw = await chrome.cookies.getAll({ url: pageUrl });
  const cookies = raw.map((c) => ({
    name: c.name,
    value: c.value,
    domain: c.domain,
    path: c.path,
    expires: c.session ? -1 : c.expirationDate || -1,
    httpOnly: !!c.httpOnly,
    secure: !!c.secure,
    sameSite: mapSameSite(c.sameSite),
  }));
  return { cookies, origins: [] };
}

// ---- 로그인 방식 감지 (세션 쿠키 vs JWT) ----

// executeScript 로 페이지 컨텍스트에서 실행 — localStorage/sessionStorage 에서
// 인증용 JWT 를 찾는다. 인증 힌트(token/jwt/auth/…) 키·필드에 달린 JWT 만 채택해
// 무관한 토큰을 잘못 잡지 않는다. 자체 완결 함수여야 한다(외부 변수 참조 금지).
function scanPageForJwt() {
  const looksJwt = (v) => {
    if (typeof v !== "string" || v.length < 20 || v.length > 8192) return false;
    if (/\s/.test(v)) return false;
    const parts = v.split(".");
    if (parts.length !== 3 || !parts[0] || !parts[1] || !parts[2]) return false;
    try {
      const b64 = parts[0].replace(/-/g, "+").replace(/_/g, "/");
      const padded = b64 + "=".repeat((4 - (b64.length % 4)) % 4);
      const header = JSON.parse(atob(padded));
      return !!header && typeof header === "object" && typeof header.alg === "string";
    } catch (e) {
      return false;
    }
  };
  const HINT = /token|jwt|auth|bearer|access|credential|session|id_token/i;
  const walk = (obj, parentHinted, depth) => {
    if (obj == null || typeof obj !== "object" || depth < 0) return null;
    for (const k of Object.keys(obj)) {
      const v = obj[k];
      const hinted = parentHinted || HINT.test(k);
      if (typeof v === "string") {
        if (hinted && looksJwt(v)) return v;
      } else if (typeof v === "object" && depth > 0) {
        const r = walk(v, hinted, depth - 1);
        if (r) return r;
      }
    }
    return null;
  };
  const fromValue = (val, hinted) => {
    if (looksJwt(val)) return hinted ? val : null; // 인증 힌트 없는 JWT 는 무시(보수적)
    let obj;
    try { obj = JSON.parse(val); } catch (e) { return null; } // JSON 래핑 풀어 재귀
    return walk(obj, hinted, 3);
  };
  const scan = (store) => {
    try {
      for (let i = 0; i < store.length; i++) {
        const key = store.key(i);
        const r = fromValue(store.getItem(key), HINT.test(key));
        if (r) return r;
      }
    } catch (e) {
      /* 스토리지 접근 불가 */
    }
    return null;
  };
  return scan(window.localStorage) || scan(window.sessionStorage) || null;
}

// 활성 탭의 페이지에서 JWT 를 찾는다 (없거나 권한·제약 페이지면 null).
async function detectJwt(tabId) {
  if (tabId == null) return null;
  try {
    const results = await chrome.scripting.executeScript({
      target: { tabId },
      func: scanPageForJwt,
    });
    const r = results && results[0];
    return r && typeof r.result === "string" ? r.result : null;
  } catch (e) {
    return null; // scripting 권한 없음·제약 페이지 — 세션 쿠키로 폴백
  }
}

// 로그인 방식 판단: JWT(localStorage/sessionStorage) 우선, 없으면 세션 쿠키.
// 반환: {kind:"jwt", token} | {kind:"session", storage_state} | {kind:"none"} | null(권한 거부)
async function collectAuth(pageUrl, tabId) {
  const token = await detectJwt(tabId);
  if (token) return { kind: "jwt", token };
  const capsule = await collectCapsule(pageUrl);
  if (capsule == null) return null;
  if (capsule.cookies.length === 0) return { kind: "none" };
  return { kind: "session", storage_state: capsule };
}

// ---- 브라우저 클라이언트 캡처 (CDP 풀페이지 + 자원/문서 재요청 + ingest 업로드) ----
//
// 서버를 통하지 않고 브라우저 내부에서 현재 페이지를 캡처해 POST /api/v1/ingest 로
// 올린다. 자원은 background fetch(host 권한 → CORS 우회, cache:'force-cache' 로 이미
// 로드된 캐시 우선)로 받아 page 컨텍스트에서 인라인하고, 스크린샷은 chrome.debugger
// (CDP captureBeyondViewport 풀페이지)로 찍는다. 서버는 대상 URL 을 다시 가져오지 않는다.

const MAX_RESOURCE_BYTES = 5 * 1024 * 1024;        // 자원 1개 상한
const MAX_TOTAL_RESOURCE_BYTES = 30 * 1024 * 1024; // 인라인 합계 상한 (업로드 캡 여유)
const MAX_DOC_BYTES = 50 * 1024 * 1024;            // 문서 1개 상한
const DOC_LIMIT = 20;                              // 페이지당 문서 수 상한

// 캡처할 수 없는 페이지 (debugger 부착 불가 — chrome://, 웹스토어, 확장 페이지 등)
const UNSUPPORTED_RE =
  /^(chrome|edge|about|view-source|chrome-extension|moz-extension|devtools|file):/i;

function isCapturable(url) {
  if (!url || !/^https?:\/\//i.test(url)) return false;
  if (UNSUPPORTED_RE.test(url)) return false;
  if (/^https?:\/\/(chrome\.google\.com\/webstore|chromewebstore\.google\.com)/i.test(url))
    return false;
  return true;
}

function bufToBase64(buf) {
  const bytes = new Uint8Array(buf);
  let bin = "";
  const CHUNK = 0x8000;
  for (let i = 0; i < bytes.length; i += CHUNK) {
    bin += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
  }
  return btoa(bin);
}

function b64ToBlob(b64, type) {
  const bin = atob(b64);
  const arr = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
  return new Blob([arr], { type });
}

// CSS url()/@import 의 상대 참조를 css 파일 URL 기준 절대 URL 로 (resources._absolutize_css_refs 와 동일 정책)
function absolutizeCss(css, baseUrl) {
  const SAFE = /^(data:|https?:|\/\/|#|\/resource\/)/i;
  return css
    .replace(/url\(\s*(['"]?)([^)'"]+)\1\s*\)/gi, (mm, q, ref) => {
      const r = ref.trim();
      if (SAFE.test(r)) return mm;
      try { return `url(${q}${new URL(r, baseUrl).href}${q})`; } catch (e) { return mm; }
    })
    .replace(/@import\s+(['"])([^'"]+)\1/gi, (mm, q, ref) => {
      if (SAFE.test(ref)) return mm;
      try { return `@import ${q}${new URL(ref, baseUrl).href}${q}`; } catch (e) { return mm; }
    });
}

// 페이지 컨텍스트에서 실행 — DOM 직렬화 + 자원/문서 URL 수집 + 뷰포트. 자체 완결 함수.
function grabPageInfo() {
  const abs = (u) => { try { return new URL(u, location.href).href; } catch (e) { return null; } };
  const http = (u) => u && /^https?:\/\//i.test(u);
  const res = new Set();
  document.querySelectorAll("img[src]").forEach((el) => {
    const u = abs(el.getAttribute("src")); if (http(u)) res.add(u);
  });
  document.querySelectorAll("img[srcset], source[srcset]").forEach((el) => {
    (el.getAttribute("srcset") || "").split(",").forEach((p) => {
      const u = abs((p.trim().split(/\s+/)[0]) || ""); if (http(u)) res.add(u);
    });
  });
  document.querySelectorAll('link[rel~="stylesheet"][href]').forEach((el) => {
    const u = abs(el.getAttribute("href")); if (http(u)) res.add(u);
  });
  document.querySelectorAll("video[poster]").forEach((el) => {
    const u = abs(el.getAttribute("poster")); if (http(u)) res.add(u);
  });
  const DOC_EXT = /\.(pdf|docx?|pptx?|xlsx?|hwpx?|odt|odp|ods|rtf|epub)(\?|#|$)/i;
  const docs = new Set();
  document.querySelectorAll("a[href]").forEach((el) => {
    const u = abs(el.getAttribute("href")); if (http(u) && DOC_EXT.test(u)) docs.add(u);
  });
  // 교차출처 iframe 은 페이지 컨텍스트에서 DOM 직렬화 불가 — 있으면 불완전 표시
  // (CDP 풀페이지 스크린샷은 시각적으로 담는다). 동일출처 iframe 은 outerHTML 에 포함.
  let crossFrames = 0;
  document.querySelectorAll("iframe").forEach((f) => {
    try { void f.contentDocument; if (!f.contentDocument && f.src) crossFrames++; }
    catch (e) { crossFrames++; }
  });
  const doctype = document.doctype ? "<!DOCTYPE " + document.doctype.name + ">" : "";
  return {
    raw_html: doctype + document.documentElement.outerHTML,
    final_url: location.href,
    title: document.title || "",
    resources: [...res],
    doc_links: [...docs],
    cross_frames: crossFrames,
    viewport_w: window.innerWidth,
    viewport_h: window.innerHeight,
    dpr: window.devicePixelRatio || 1,
  };
}

// 페이지 컨텍스트에서 실행 — 자원 맵으로 단일 파일 HTML 생성 (실 DOM 기준). 자체 완결.
// map: { absUrl: {data:"data:…"} | {css:"<absolutized css text>"} }
function inlinePageWithMap(map) {
  const abs = (u) => { try { return new URL(u, location.href).href; } catch (e) { return null; } };
  const root = document.documentElement.cloneNode(true);
  root.querySelectorAll("script").forEach((el) => el.remove()); // 샌드박스서 미실행 — 용량·노이즈 제거
  root.querySelectorAll("img[src]").forEach((el) => {
    const e = map[abs(el.getAttribute("src"))];
    if (e && e.data) el.setAttribute("src", e.data);
    el.removeAttribute("srcset"); el.removeAttribute("loading");
  });
  root.querySelectorAll("source[srcset]").forEach((el) => el.removeAttribute("srcset"));
  root.querySelectorAll("video[poster]").forEach((el) => {
    const e = map[abs(el.getAttribute("poster"))]; if (e && e.data) el.setAttribute("poster", e.data);
  });
  root.querySelectorAll('link[rel~="stylesheet"][href]').forEach((el) => {
    const e = map[abs(el.getAttribute("href"))];
    if (e && e.css != null) {
      const style = document.createElement("style");
      style.textContent = e.css;
      el.replaceWith(style);
    }
  });
  const doctype = document.doctype ? "<!DOCTYPE " + document.doctype.name + ">" : "";
  return doctype + root.outerHTML;
}

// 자원 재요청 — host 권한으로 CORS 우회, 캐시 우선. CSS 는 url() 절대화한 텍스트로.
async function fetchResources(urls) {
  const map = {};
  let total = 0;
  let incomplete = false;
  for (const url of urls) {
    if (total >= MAX_TOTAL_RESOURCE_BYTES) { incomplete = true; break; }
    try {
      const resp = await fetch(url, { credentials: "include", cache: "force-cache" });
      if (!resp.ok) { incomplete = true; continue; }
      const ct = (resp.headers.get("content-type") || "").split(";")[0].trim().toLowerCase();
      const buf = await resp.arrayBuffer();
      if (buf.byteLength > MAX_RESOURCE_BYTES) { incomplete = true; continue; }
      total += buf.byteLength;
      if (ct === "text/css" || /\.css(\?|#|$)/i.test(url)) {
        map[url] = { css: absolutizeCss(new TextDecoder("utf-8").decode(buf), url) };
      } else {
        map[url] = { data: "data:" + (ct || "application/octet-stream") + ";base64," + bufToBase64(buf) };
      }
    } catch (e) { incomplete = true; }
  }
  return { map, incomplete };
}

// 페이지가 링크한 문서 재요청 (한도 내). 반환: [{url, filename, content_type, blob}]
async function fetchDocuments(urls) {
  const out = [];
  for (const url of (urls || []).slice(0, DOC_LIMIT)) {
    try {
      const resp = await fetch(url, { credentials: "include", cache: "force-cache" });
      if (!resp.ok) continue;
      const ct = (resp.headers.get("content-type") || "application/octet-stream").split(";")[0].trim();
      const buf = await resp.arrayBuffer();
      if (buf.byteLength > MAX_DOC_BYTES) continue;
      let name = "document";
      try { name = decodeURIComponent(new URL(url).pathname.split("/").pop() || "") || "document"; }
      catch (e) { /* keep default */ }
      out.push({ url, filename: name, content_type: ct, blob: new Blob([buf], { type: ct }) });
    } catch (e) { /* 개별 문서 실패는 건너뛴다 */ }
  }
  return out;
}

// 분할 캡처가 합성할 수 있는 전체 높이 상한 (지나치게 긴 페이지 보호).
const MAX_SCREENSHOT_HEIGHT = 16384;
const CAPTURE_SEGMENT_PAUSE_MS = 120; // 캡처 직전 스크롤 후 페인트 반영 대기
const CAPTURE_SETTLE_PAUSE_MS = 150;  // 사전 스크롤(지연 로딩 유발) 각 단계 대기
const MAX_CAPTURE_SEGMENTS = 240;     // 무한 루프 방지 (구간 수 상한)

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// 현재 레이아웃의 콘텐츠 크기 + 뷰포트 크기(CSS px)를 읽는다 — 또는 null.
async function getLayout(target) {
  try {
    const m = await chrome.debugger.sendCommand(target, "Page.getLayoutMetrics");
    const cs = m && (m.cssContentSize || m.contentSize);
    const vp = m && (m.cssLayoutViewport || m.layoutViewport);
    if (cs && cs.width > 0 && cs.height > 0 && vp && vp.clientWidth > 0 && vp.clientHeight > 0) {
      return {
        width: Math.ceil(cs.width),
        height: Math.ceil(cs.height),
        viewW: Math.ceil(vp.clientWidth),
        viewH: Math.ceil(vp.clientHeight),
      };
    }
  } catch (e) { /* 메트릭 실패 */ }
  return null;
}

// 페이지 컨텍스트 — y 로 스크롤하고 실제 적용된 scrollY(끝 클램프 반영)를 반환. 자체 완결.
function scrollToY(y) {
  window.scrollTo(0, y);
  return window.scrollY;
}

// 페이지 컨텍스트 — 분할 캡처 준비. ① 고정/스티키 요소를 일반 흐름으로 바꿔 구간마다
// 중복 합성되는 걸 막고(첫 구간 위치에만 남도록), ② 부드러운 스크롤·애니메이션·전환을
// 꺼 구간 간 어긋남을 줄인다. 변경분은 isolated world 의 window 에 보관해
// restoreAfterCapture 가 복원한다. 원래 scroll 위치도 반환. 자체 완결.
function prepFixedForCapture() {
  const changed = [];
  for (const el of document.querySelectorAll("*")) {
    const pos = getComputedStyle(el).position;
    if (pos === "fixed" || pos === "sticky") {
      changed.push({ el, prev: el.style.position });
      el.style.setProperty("position", pos === "fixed" ? "absolute" : "relative", "important");
    }
  }
  window.__wccgFixed = changed;
  const style = document.createElement("style");
  style.id = "__wccgCaptureStyle";
  style.textContent =
    "html{scroll-behavior:auto !important;}" +
    "*,*::before,*::after{animation:none !important;transition:none !important;}";
  (document.head || document.documentElement).appendChild(style);
  return { scrollY: window.scrollY };
}

// 페이지 컨텍스트 — prepFixedForCapture 가 바꾼 요소·스타일·스크롤 위치를 복원. 자체 완결.
function restoreAfterCapture(scrollY) {
  for (const c of window.__wccgFixed || []) c.el.style.position = c.prev;
  delete window.__wccgFixed;
  const style = document.getElementById("__wccgCaptureStyle");
  if (style) style.remove();
  window.scrollTo(0, scrollY || 0);
}

async function runInPage(tabId, func, args) {
  const r = await chrome.scripting.executeScript({ target: { tabId }, func, args: args || [] });
  return r && r[0] ? r[0].result : undefined;
}

// 전체를 한 번 훑어 지연 로딩(이미지·무한스크롤 외 콘텐츠)을 미리 유발한다. 캡처 도중
// 콘텐츠가 새로 로드돼 레이아웃이 밀리면 이음새가 어긋나므로, 캡처 전에 끝까지
// 내려가 로드시킨 뒤 맨 위로 돌아온다.
async function settleLazyLoad(tabId, target, viewH) {
  const l = await getLayout(target);
  const h = l ? l.height : 0;
  for (let yy = 0, n = 0; yy < h && n < MAX_CAPTURE_SEGMENTS; yy += viewH, n++) {
    await runInPage(tabId, scrollToY, [yy]);
    await sleep(CAPTURE_SETTLE_PAUSE_MS);
  }
  await runInPage(tabId, scrollToY, [h]); // 바닥까지
  await sleep(CAPTURE_SETTLE_PAUSE_MS);
  await runInPage(tabId, scrollToY, [0]); // 맨 위로 복귀
  await sleep(CAPTURE_SETTLE_PAUSE_MS);
}

// CDP 풀페이지 스크린샷 — base64 PNG 또는 null.
// captureBeyondViewport(+clip·디바이스메트릭 오버라이드)는 헤드풀 크롬에서 물리
// 윈도우 표면이 뷰포트 크기에 묶여 있어, 요청한 전체 높이를 보이는 첫 뷰포트의
// 반복 타일로 채우는 Chromium 버그가 있다. 그래서 뷰포트 단위로 스크롤하며 보이는
// 표면만 찍어(captureBeyondViewport:false) OffscreenCanvas 에 이어 붙인다
// (scroll-and-stitch). 이음새는 디바이스 픽셀로 누적하고 새로 드러난 부분만 소스
// 크롭해 그려 틈/겹침을 없앤다(분수 dpr·끝 클램프 안전). 고정/스티키 요소는 첫
// 구간에만 남도록 일반 흐름으로 바꾸고, 캡처 전 전체를 한 번 훑어 지연 로딩을 끝낸다.
async function captureFullPage(tabId) {
  const target = { tabId };
  try {
    await chrome.debugger.attach(target, "1.3");
  } catch (e) {
    return null; // 부착 실패 (제약 페이지·이미 부착됨)
  }
  let prepped = null;
  try {
    const l0 = await getLayout(target);
    if (!l0) return null;
    try { await chrome.debugger.sendCommand(target, "Emulation.setScrollbarsHidden", { hidden: true }); }
    catch (e) { /* 스크롤바 숨김 미지원 — 무시 */ }

    await settleLazyLoad(tabId, target, l0.viewH);
    prepped = (await runInPage(tabId, prepFixedForCapture)) || { scrollY: 0 };
    // 지연 로딩·고정 요소 흐름 변경으로 콘텐츠 높이가 바뀌므로 다시 측정.
    const layout = await getLayout(target);
    if (!layout) return null;
    const fullH = Math.min(layout.height, MAX_SCREENSHOT_HEIGHT);
    const viewH = layout.viewH;

    let canvas = null;
    let ctx = null;
    let dpr = 1;
    let destY = 0;     // 캔버스에 채워진 높이(디바이스 px) — 다음 그릴 위치
    let prevY = 0;     // 직전 구간의 실제 scrollY(CSS px)
    let y = 0;
    for (let i = 0; i < MAX_CAPTURE_SEGMENTS; i++) {
      const actualY = (await runInPage(tabId, scrollToY, [y])) || 0;
      await sleep(CAPTURE_SEGMENT_PAUSE_MS);
      const shot = await chrome.debugger.sendCommand(target, "Page.captureScreenshot", {
        format: "png", fromSurface: true, captureBeyondViewport: false,
      });
      if (!shot || !shot.data) return null;
      const bmp = await createImageBitmap(b64ToBlob(shot.data, "image/png"));
      if (!canvas) {
        dpr = bmp.width / layout.viewW || 1;
        // 높이가 자라날 여지를 한 뷰포트만큼 더 둔다(끝에서 정확히 잘라낸다).
        canvas = new OffscreenCanvas(bmp.width, Math.round(fullH * dpr) + bmp.height);
        ctx = canvas.getContext("2d");
        ctx.drawImage(bmp, 0, 0);
        destY = bmp.height;
      } else {
        // 직전 구간 대비 새로 드러난 높이(디바이스 px)만큼만 아래쪽을 잘라 붙인다.
        const newDev = Math.min(Math.round((actualY - prevY) * dpr), bmp.height);
        if (newDev > 0) {
          const srcY = bmp.height - newDev;
          ctx.drawImage(bmp, 0, srcY, bmp.width, newDev, 0, destY, bmp.width, newDev);
          destY += newDev;
        }
      }
      prevY = actualY;
      bmp.close();
      if (actualY + viewH >= fullH) break; // 바닥 도달
      const next = actualY + viewH;
      if (next <= y) break; // 더 못 내려감 — 종료
      y = next;
    }
    if (!canvas) return null;
    // 실제로 채운 높이만큼(상한 클램프) 정확히 잘라 내보낸다.
    const outH = Math.min(destY, Math.round(MAX_SCREENSHOT_HEIGHT * dpr));
    const out = new OffscreenCanvas(canvas.width, outH);
    out.getContext("2d").drawImage(canvas, 0, 0);
    const blob = await out.convertToBlob({ type: "image/png" });
    return bufToBase64(await blob.arrayBuffer());
  } catch (e) {
    return null;
  } finally {
    try { await runInPage(tabId, restoreAfterCapture, [prepped ? prepped.scrollY : 0]); } catch (e) { /* 무시 */ }
    try { await chrome.debugger.sendCommand(target, "Emulation.setScrollbarsHidden", { hidden: false }); } catch (e) { /* 무시 */ }
    try { await chrome.debugger.detach(target); } catch (e) { /* 무시 */ }
  }
}

// 멀티파트 ingest 업로드 — 토큰은 헤더로만 (apiFetch 는 JSON 전용이라 별도).
async function uploadIngest(form) {
  const { base_url, token } = await getConfig();
  if (!base_url) return { ok: false, status: 0, error: "not_connected" };
  const headers = { Accept: "application/json" };
  if (token) headers["Authorization"] = "Bearer " + token;
  let resp;
  try {
    resp = await fetch(base_url + "/api/v1/ingest", { method: "POST", headers, body: form });
  } catch (e) {
    return { ok: false, status: 0, error: "network" };
  }
  let data = null;
  try { data = await resp.json(); } catch (e) { /* 비 JSON */ }
  return { ok: resp.ok, status: resp.status, data };
}

// 현재 페이지를 브라우저에서 캡처해 ingest 로 올린다.
async function captureAndIngest(payload) {
  const { url, tabId, force, network_tag } = payload || {};
  if (tabId == null || !isCapturable(url)) {
    return { ok: false, status: 0, error: "unsupported_page" };
  }
  let grab;
  try {
    const r = await chrome.scripting.executeScript({ target: { tabId }, func: grabPageInfo });
    grab = r && r[0] && r[0].result;
  } catch (e) {
    return { ok: false, status: 0, error: "capture_failed" };
  }
  if (!grab) return { ok: false, status: 0, error: "capture_failed" };

  const { map, incomplete: resIncomplete } = await fetchResources(grab.resources || []);

  let pageHtml = grab.raw_html;
  let inlineFailed = false;
  try {
    const r = await chrome.scripting.executeScript({
      target: { tabId }, func: inlinePageWithMap, args: [map],
    });
    if (r && r[0] && typeof r[0].result === "string") pageHtml = r[0].result;
    else inlineFailed = true;
  } catch (e) { inlineFailed = true; /* 인라인 실패 — raw_html 그대로 */ }

  const shotB64 = await captureFullPage(tabId);
  const docs = await fetchDocuments(grab.doc_links);
  const incomplete =
    !!resIncomplete || inlineFailed || !shotB64 || (grab.cross_frames || 0) > 0;

  const form = new FormData();
  form.set("url", url);
  form.set("final_url", grab.final_url || url);
  form.set("title", grab.title || "");
  form.set("force", force ? "true" : "false");
  form.set("incomplete", incomplete ? "true" : "false");
  form.set("capture_env", JSON.stringify({
    viewport_w: grab.viewport_w, viewport_h: grab.viewport_h,
    dpr: grab.dpr, ua: navigator.userAgent,
  }));
  if (network_tag) form.set("network_tag", network_tag);
  form.set("page_html", new Blob([pageHtml], { type: "text/html" }), "page.html");
  form.set("raw_html", new Blob([grab.raw_html], { type: "text/html" }), "raw.html");
  if (shotB64) form.set("screenshot", b64ToBlob(shotB64, "image/png"), "screenshot.png");
  const docUrls = [];
  for (const d of docs) { form.append("documents", d.blob, d.filename); docUrls.push(d.url); }
  form.set("document_urls", JSON.stringify(docUrls));

  const res = await uploadIngest(form);
  if (res.status === 422 && res.data && res.data.detail && res.data.detail.needs_network_tag) {
    return { ok: false, status: 422, needs_network_tag: true, host: res.data.detail.host };
  }
  if (res.data) res.incomplete = incomplete;
  await trackIngestResult(res, url);
  return res;
}

// ingest 는 동기 응답이라 폴링이 불필요하나, 결과를 즉시 데스크톱 알림으로 알린다(옵션 on).
async function trackIngestResult(res, url) {
  if (!(await notifyEnabled())) return;
  if (!res || !res.ok || !res.data) return;
  const { base_url } = await getConfig();
  if (!base_url) return;
  const st = res.data;
  const notifId = `wccg:ingest:${st.snapshot_id || Date.now()}`;
  await setTarget(notifId, st.page_id ? `${base_url}/extension/page/${st.page_id}` : base_url);
  await showNotification(notifId, msg("notif_capture_done"),
    msg(OUTCOME_KEY[st.status] || "notif_outcome_done") + " — " + shortUrl(st.url || url));
}

// ---- 버전 체크 (서버 버전 vs 설치된 확장 버전) ----
//
// 확장은 웹스토어 미등록 unpacked 로드라 자동 업데이트가 없다. 연결된 서버의
// 현재 버전을 조회해 자기 manifest 버전과 비교하고, 서버가 더 최신이면 팝업이
// 재설치 안내 배너를 띄운다.

// a > b 면 1, a < b 면 -1, 같으면 0. 점으로 분해해 숫자만 비교한다 (1.2.3).
// 숫자가 아닌 토큰(예: 0.0.0+unknown 의 '0+unknown')은 0 으로 취급(보수적).
function compareVersions(a, b) {
  const pa = String(a || "").split(".");
  const pb = String(b || "").split(".");
  const n = Math.max(pa.length, pb.length);
  for (let i = 0; i < n; i++) {
    const x = parseInt(pa[i], 10) || 0;
    const y = parseInt(pb[i], 10) || 0;
    if (x > y) return 1;
    if (x < y) return -1;
  }
  return 0;
}

// 서버 버전을 조회해 업데이트 필요 여부 반환. 미연결·조회 실패·파싱 불가면
// update_available:false 로 조용히 무시한다(오탐 방지).
async function checkVersion() {
  const current = chrome.runtime.getManifest().version;
  const res = await apiFetch("/api/v1/version");
  const latest = res && res.ok && res.data && res.data.version;
  if (!latest) return { ok: false, update_available: false, current, latest: null };
  return {
    ok: true,
    update_available: compareVersions(latest, current) > 0,
    current,
    latest,
  };
}

// ---- 메시지 라우터 ----

const HANDLERS = {
  status: () => status(),
  connect: (m) => connect(m.payload),
  disconnect: () => disconnect(),

  archivePage: async (m) => {
    const res = await apiFetch("/api/v1/archive", {
      method: "POST",
      body: { url: m.payload.url, force: !!m.payload.force },
    });
    await trackJob(res, m.payload.url);
    return res;
  },

  // 브라우저에서 직접 캡처해 ingest 로 업로드 (서버 무요청). 사설 호스트 무태그면
  // {needs_network_tag, host} 로 응답해 팝업이 태그를 고르게 한다.
  captureBrowser: (m) => captureAndIngest(m.payload),

  // 로컬 네트워크 태그 목록·생성 (사설 호스트 캡처 시 팝업이 사용).
  listNetworkTags: () => apiFetch("/api/v1/network-tags"),
  createNetworkTag: (m) =>
    apiFetch("/api/v1/network-tags", {
      method: "POST",
      body: { name: m.payload.name, description: m.payload.description || "" },
    }),

  archiveSite: async (m) => {
    const body = { url: m.payload.url };
    for (const k of ["max_pages", "max_depth", "delay"]) {
      if (m.payload[k] != null && m.payload[k] !== "") body[k] = Number(m.payload[k]);
    }
    if (m.payload.network_tag) body.network_tag = m.payload.network_tag;
    const res = await apiFetch("/api/v1/crawl", { method: "POST", body });
    await trackCrawl(res, m.payload.url);
    return res;
  },

  // 로그인 정보 포함 단일 페이지 — JWT/세션 쿠키를 판단해 1회성 인증 캡처로.
  archivePageAuth: async (m) => {
    const auth = await collectAuth(m.payload.url, m.payload.tabId);
    if (auth == null) return { ok: false, status: 0, error: "permission_denied" };
    if (auth.kind === "none") return { ok: false, status: 0, error: "no_cookies" };
    const body = { url: m.payload.url, force: !!m.payload.force };
    if (auth.kind === "jwt") body.jwt = auth.token;
    else body.storage_state = auth.storage_state;
    const res = await apiFetch("/api/v1/auth-profiles", { method: "POST", body });
    res.auth_kind = auth.kind; // 팝업이 사용된 로그인 방식을 표시
    await trackJob(res, m.payload.url);
    return res;
  },

  // 로그인 정보 포함 사이트 전체 — JWT/세션 쿠키를 크롤 전 페이지에 적용.
  archiveSiteAuth: async (m) => {
    const auth = await collectAuth(m.payload.url, m.payload.tabId);
    if (auth == null) return { ok: false, status: 0, error: "permission_denied" };
    if (auth.kind === "none") return { ok: false, status: 0, error: "no_cookies" };
    const body = { url: m.payload.url };
    if (auth.kind === "jwt") body.jwt = auth.token;
    else body.storage_state = auth.storage_state;
    for (const k of ["max_pages", "max_depth", "delay"]) {
      if (m.payload[k] != null && m.payload[k] !== "") body[k] = Number(m.payload[k]);
    }
    if (m.payload.network_tag) body.network_tag = m.payload.network_tag;
    const res = await apiFetch("/api/v1/crawl", { method: "POST", body });
    res.auth_kind = auth.kind;
    await trackCrawl(res, m.payload.url);
    return res;
  },

  // popup 의 알림 토글 — 끄면 추적 목록·알람·배지를 즉시 정리한다.
  getNotifyPref: async () => ({ on: await notifyEnabled() }),
  setNotifyPref: async (m) => {
    await setNotifyPref(!!(m.payload && m.payload.on));
    return { ok: true };
  },

  // 토글 미리보기 — 감지된 로그인 방식(JWT/세션 쿠키)과 쿠키 수.
  detectAuth: async (m) => {
    const token = await detectJwt(m.payload.tabId);
    if (token) return { kind: "jwt" };
    const pattern = originPattern(m.payload.url);
    if (pattern && !(await chrome.permissions.contains({ origins: [pattern] }))) {
      return { kind: "unknown", count: null }; // 권한 미부여 — 실행 시 요청
    }
    const raw = await chrome.cookies.getAll({ url: m.payload.url });
    return { kind: raw.length ? "session" : "none", count: raw.length };
  },

  history: async (m) => {
    const lookups = [m.payload.url];
    const swapped = swapScheme(m.payload.url);
    if (swapped) lookups.push(swapped);
    for (const u of lookups) {
      const res = await apiFetch("/api/v1/pages?url=" + encodeURIComponent(u));
      if (res.status === 401) return { ok: false, status: 401 };
      if (res.status === 403) return { ok: false, status: 403 };
      const pages = (res.data && res.data.pages) || [];
      if (pages.length > 0) {
        const detail = await apiFetch("/api/v1/pages/" + pages[0].id);
        return {
          ok: true,
          page: pages[0],
          snapshots: (detail.data && detail.data.snapshots) || [],
          matched_url: u,
        };
      }
    }
    return { ok: true, page: null, snapshots: [] };
  },

  openDeepLink: async (m) => {
    const { base_url } = await getConfig();
    if (!base_url) return { ok: false };
    const url = m.payload.page_id
      ? `${base_url}/extension/page/${m.payload.page_id}`
      : `${base_url}/extension/go?url=${encodeURIComponent(m.payload.url)}`;
    await chrome.tabs.create({ url });
    return { ok: true };
  },

  openIssue: async (m) => {
    // 연결 전에는 저장된 base_url 이 없으므로 팝업이 입력칸 주소를 함께 보낸다.
    // 입력값을 우선 쓰고(정규화), 없으면 저장된 값으로 폴백한다.
    const typed = normalizeBaseUrl(m && m.payload ? m.payload.base_url : "");
    let base = typed;
    if (!base) {
      const { base_url } = await getConfig();
      base = base_url || "";
    }
    if (!base) return { ok: false, error: "no_base_url" };
    // 입력한 주소를 기억해 둔다 (팝업이 닫혀도 다음에 채워지도록). 토큰이 없으면
    // status 의 connected 는 여전히 false 라 연결로 오인되지 않는다.
    if (typed) await chrome.storage.local.set({ base_url: typed });
    await chrome.tabs.create({ url: base + "/extension/token" });
    return { ok: true };
  },

  // 서버 버전 조회 + 설치 버전 비교 (팝업이 업데이트 배너 표시 여부 판단).
  checkVersion: () => checkVersion(),

  // 업데이트 재설치 — 연결된 서버의 확장 다운로드(zip) 를 새 탭으로 연다.
  // /extension/download 는 세션 인증이라 로그인된 브라우저에서만 바로 받아진다.
  openDownload: async () => {
    const { base_url } = await getConfig();
    if (!base_url) return { ok: false };
    await chrome.tabs.create({ url: base_url + "/extension/download" });
    return { ok: true };
  },
};

function swapScheme(url) {
  if (/^https:\/\//i.test(url)) return url.replace(/^https:/i, "http:");
  if (/^http:\/\//i.test(url)) return url.replace(/^http:/i, "https:");
  return null;
}

// ---- 결과 알림 (작업/크롤 추적 → 주기 폴링 → 데스크톱 알림) ----
//
// MV3 서비스 워커는 수명이 짧아 푸시를 유지할 수 없다. 제출한 작업을 추적 목록
// (chrome.storage.local)에 담고 chrome.alarms 로 주기 폴링하며, 상태 엔드포인트의
// 전이를 감지해 chrome.notifications 로 알린다. 추적이 비면 알람을 끈다.

async function notifyEnabled() {
  const c = await chrome.storage.local.get(NOTIFY_PREF_KEY);
  return c[NOTIFY_PREF_KEY] !== false; // 미설정 기본 on
}

async function setNotifyPref(on) {
  await chrome.storage.local.set({ [NOTIFY_PREF_KEY]: on });
  if (!on) {
    // 끄면 추적을 중단하고 흔적을 정리한다.
    await chrome.storage.local.remove([WATCH_KEY, TARGETS_KEY]);
    await chrome.alarms.clear(POLL_ALARM);
    await updateBadge([]);
  }
}

async function getWatch() {
  const c = await chrome.storage.local.get(WATCH_KEY);
  return Array.isArray(c[WATCH_KEY]) ? c[WATCH_KEY] : [];
}

async function setWatch(watch) {
  await chrome.storage.local.set({ [WATCH_KEY]: watch });
}

// 제출 성공 응답을 추적 목록에 추가 (알림이 켜져 있을 때만).
async function addWatch(entry) {
  if (!(await notifyEnabled())) return;
  const watch = await getWatch();
  if (watch.some((w) => w.kind === entry.kind && w.id === entry.id)) return;
  watch.push(entry);
  await setWatch(watch);
  await ensureAlarm();
  await updateBadge(watch);
}

async function trackJob(res, url) {
  if (res && res.ok && res.data && res.data.job_id) {
    await addWatch({ kind: "job", id: res.data.job_id, url, last_state: null });
  }
}

async function trackCrawl(res, url) {
  if (res && res.ok && res.data && res.data.crawl_id) {
    await addWatch({ kind: "crawl", id: res.data.crawl_id, url, last_state: null });
  }
}

async function ensureAlarm() {
  const existing = await chrome.alarms.get(POLL_ALARM);
  if (!existing) chrome.alarms.create(POLL_ALARM, { periodInMinutes: POLL_PERIOD_MIN });
}

async function updateBadge(watch) {
  const n = (watch || []).length;
  try {
    await chrome.action.setBadgeBackgroundColor({ color: "#1d6e56" });
    await chrome.action.setBadgeText({ text: n ? String(n) : "" });
  } catch (e) {
    /* 배지 미지원 — 무시 */
  }
}

function shortUrl(u) {
  try {
    const x = new URL(u);
    const s = x.host + (x.pathname === "/" ? "" : x.pathname);
    return s.length > 64 ? s.slice(0, 63) + "…" : s;
  } catch (e) {
    return u || "";
  }
}

// 알림 클릭 시 열 대시보드 딥링크.
function clickTarget(base, kind, st, w) {
  if (kind === "crawl") return `${base}/extension/crawl/${w.id}`;
  if (st.state === "needs_human") return `${base}/extension/needs-human`;
  if (st.state === "failed") return `${base}/extension/archives`; // 내 아카이브에서 실패 확인
  if (st.page_id) return `${base}/extension/page/${st.page_id}`;
  return `${base}/extension/go?url=${encodeURIComponent(st.url || w.url || "")}`;
}

async function setTarget(notifId, url) {
  const c = await chrome.storage.local.get(TARGETS_KEY);
  const map = c[TARGETS_KEY] || {};
  map[notifId] = url;
  await chrome.storage.local.set({ [TARGETS_KEY]: map });
}

async function showNotification(notifId, title, message) {
  try {
    await chrome.notifications.create(notifId, {
      type: "basic",
      iconUrl: chrome.runtime.getURL("icons/icon128.png"),
      title,
      message,
      priority: 1,
    });
  } catch (e) {
    /* 알림 미허용/미지원 — 무시 (추적은 계속) */
  }
}

const OUTCOME_KEY = {
  new: "notif_outcome_new",
  changed: "notif_outcome_changed",
  unchanged: "notif_outcome_unchanged",
  forced_same: "notif_outcome_unchanged",
};

async function notifyJob(base, w, st) {
  let title, message;
  if (st.state === "failed") {
    title = msg("notif_archive_failed");
    message = (st.error ? st.error + " — " : "") + shortUrl(st.url || w.url);
  } else if (st.state === "needs_human") {
    title = msg("notif_needs_human");
    message = shortUrl(st.url || w.url);
  } else {
    title = msg("notif_archive_done");
    message =
      msg(OUTCOME_KEY[st.outcome] || "notif_outcome_done") +
      " — " + shortUrl(st.url || w.url);
  }
  const notifId = `wccg:job:${w.id}`;
  await setTarget(notifId, clickTarget(base, "job", st, w));
  await showNotification(notifId, title, message);
}

async function notifyCrawl(base, w, st) {
  const counts = st.counts || {};
  const done = counts.done || 0;
  const failed = counts.failed || 0;
  const total = counts.total || 0;
  let title;
  if (st.status === "cancelled") title = msg("notif_crawl_cancelled");
  else if (total > 0 && failed >= total) title = msg("notif_crawl_failed");
  else title = msg("notif_crawl_done");
  let message =
    shortUrl(st.url || w.url) + " — " + msg("notif_crawl_done_count") + " " + done;
  if (failed) message += " · " + msg("notif_crawl_failed_count") + " " + failed;
  const notifId = `wccg:crawl:${w.id}`;
  await setTarget(notifId, clickTarget(base, "crawl", st, w));
  await showNotification(notifId, title, message);
}

// 추적 항목 하나의 상태 전이 처리 → {done, state}. done 이면 추적 목록에서 제거.
async function handleTransition(base, w, st, enabled) {
  if (w.kind === "job") {
    const s = st.state;
    if (s === "succeeded" || s === "failed") {
      if (enabled) await notifyJob(base, w, st);
      return { done: true };
    }
    if (s === "needs_human") {
      // 진입 시 1회만 알림 (이미 needs_human 이었으면 재알림 안 함). 계속 추적.
      if (enabled && w.last_state !== "needs_human") await notifyJob(base, w, st);
      return { done: false, state: s };
    }
    if (s === "unknown") return { done: true }; // 작업·로그 모두 없음 — 추적 종료
    return { done: false, state: s }; // pending | in_progress
  }
  const s = st.status;
  if (s === "done" || s === "cancelled") {
    if (enabled) await notifyCrawl(base, w, st);
    return { done: true };
  }
  if (s === "unknown") return { done: true };
  return { done: false, state: s }; // running
}

// 알람 1회 — 추적 중인 작업/크롤 상태를 조회해 전이 시 알림.
async function pollStatus() {
  const watch = await getWatch();
  if (watch.length === 0) {
    await chrome.alarms.clear(POLL_ALARM);
    await updateBadge([]);
    return;
  }
  const { base_url, token } = await getConfig();
  if (!base_url || !token) {
    await chrome.alarms.clear(POLL_ALARM); // 연결 해제 — 폴링 중단
    return;
  }
  const jobIds = watch.filter((w) => w.kind === "job").map((w) => w.id);
  const crawlIds = watch.filter((w) => w.kind === "crawl").map((w) => w.id);
  const qs = [];
  if (jobIds.length) qs.push("jobs=" + jobIds.join(","));
  if (crawlIds.length) qs.push("crawls=" + crawlIds.join(","));
  const res = await apiFetch("/api/v1/archive/status?" + qs.join("&"));
  if (res.status === 401) {
    await chrome.alarms.clear(POLL_ALARM); // 토큰 무효 — 폴링 중단 (재연결 시 재개)
    return;
  }
  if (!res.ok || !res.data) return; // 일시 오류 — 다음 폴링에서 재시도
  const jobMap = new Map((res.data.jobs || []).map((j) => [j.id, j]));
  const crawlMap = new Map((res.data.crawls || []).map((c) => [c.id, c]));
  const enabled = await notifyEnabled();
  const remaining = [];
  for (const w of watch) {
    const st = w.kind === "job" ? jobMap.get(w.id) : crawlMap.get(w.id);
    if (!st) {
      remaining.push(w); // 응답에 없음(이론상 안 생김) — 유지
      continue;
    }
    const r = await handleTransition(base_url, w, st, enabled);
    if (!r.done) remaining.push({ ...w, last_state: r.state });
  }
  await setWatch(remaining);
  await updateBadge(remaining);
  if (remaining.length === 0) await chrome.alarms.clear(POLL_ALARM);
}

// SW 재기동 대비 — 추적 중이면 알람·배지 복원.
async function rearm() {
  const watch = await getWatch();
  await updateBadge(watch);
  if (watch.length) await ensureAlarm();
}

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === POLL_ALARM) pollStatus();
});

chrome.notifications.onClicked.addListener(async (notifId) => {
  const c = await chrome.storage.local.get(TARGETS_KEY);
  const map = c[TARGETS_KEY] || {};
  const url = map[notifId];
  if (url) {
    await chrome.tabs.create({ url });
    delete map[notifId];
    await chrome.storage.local.set({ [TARGETS_KEY]: map });
  }
  chrome.notifications.clear(notifId);
});

chrome.runtime.onStartup.addListener(rearm);
chrome.runtime.onInstalled.addListener(rearm);

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  const handler = HANDLERS[msg && msg.type];
  if (!handler) {
    sendResponse({ ok: false, error: "unknown_message" });
    return false;
  }
  Promise.resolve(handler(msg))
    .then((res) => sendResponse(res))
    .catch((e) => sendResponse({ ok: false, status: 0, error: String(e) }));
  return true; // 비동기 응답
});
