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
      ? `${base_url}/page/${m.payload.page_id}`
      : `${base_url}/go?url=${encodeURIComponent(m.payload.url)}`;
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
    await chrome.tabs.create({ url: base + "/settings/api-keys#ext-token-form" });
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
  if (kind === "crawl") return `${base}/crawls/${w.id}`;
  if (st.state === "needs_human") return `${base}/archive/needs-human`;
  if (st.state === "failed") return `${base}/settings/archives`; // 내 아카이브에서 실패 확인
  if (st.page_id) return `${base}/page/${st.page_id}`;
  return `${base}/go?url=${encodeURIComponent(st.url || w.url || "")}`;
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
