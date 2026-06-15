// 춘추관 확장 — background service worker.
//
// popup 은 토큰을 모른다. 모든 /api/v1 호출은 여기서만 일어나고, 토큰은
// chrome.storage.local 에만 두며 fetch 헤더에 직접 주입한다. content/popup
// 컨텍스트로는 절대 전달하지 않는다 (토큰 노출면 최소화 + CORS 회피 —
// 서버에 CORS 가 없어도 host_permissions 를 받은 background fetch 는 cross-origin
// 호출이 된다).

const STORE_KEYS = ["base_url", "token", "token_prefix", "can_view", "can_archive"];

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
  await chrome.storage.local.remove(STORE_KEYS);
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

// ---- 메시지 라우터 ----

const HANDLERS = {
  status: () => status(),
  connect: (m) => connect(m.payload),
  disconnect: () => disconnect(),

  archivePage: (m) =>
    apiFetch("/api/v1/archive", {
      method: "POST",
      body: { url: m.payload.url, force: !!m.payload.force },
    }),

  archiveSite: (m) => {
    const body = { url: m.payload.url };
    for (const k of ["max_pages", "max_depth", "delay"]) {
      if (m.payload[k] != null && m.payload[k] !== "") body[k] = Number(m.payload[k]);
    }
    if (m.payload.network_tag) body.network_tag = m.payload.network_tag;
    return apiFetch("/api/v1/crawl", { method: "POST", body });
  },

  authProfile: async (m) => {
    const capsule = await collectCapsule(m.payload.url);
    if (capsule == null) return { ok: false, status: 0, error: "permission_denied" };
    if (capsule.cookies.length === 0) return { ok: false, status: 0, error: "no_cookies" };
    return apiFetch("/api/v1/auth-profiles", {
      method: "POST",
      body: { url: m.payload.url, storage_state: capsule, force: !!m.payload.force },
    });
  },

  cookieCount: async (m) => {
    const pattern = originPattern(m.payload.url);
    if (pattern && !(await chrome.permissions.contains({ origins: [pattern] }))) {
      return { count: null }; // 권한 미부여 — 실행 시 요청
    }
    const raw = await chrome.cookies.getAll({ url: m.payload.url });
    return { count: raw.length };
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

  openIssue: async () => {
    const { base_url } = await getConfig();
    const target = base_url ? base_url + "/settings/account" : null;
    if (target) await chrome.tabs.create({ url: target });
    return { ok: !!target };
  },
};

function swapScheme(url) {
  if (/^https:\/\//i.test(url)) return url.replace(/^https:/i, "http:");
  if (/^http:\/\//i.test(url)) return url.replace(/^http:/i, "https:");
  return null;
}

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
