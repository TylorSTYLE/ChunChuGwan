// 춘추관 확장 popup — UI 만 담당한다. 모든 API 호출·토큰 접근은 background 에
// 메시지로 위임한다 (토큰은 popup 으로 오지 않는다).

const $ = (sel) => document.querySelector(sel);
const send = (type, payload) => chrome.runtime.sendMessage({ type, payload });
const msg = (key, subs) => chrome.i18n.getMessage(key, subs) || key;

let currentUrl = "";

// ---- 호스트 권한 (user gesture 필요) ----
//
// chrome.permissions.request 는 사용자 제스처가 살아있는 컨텍스트에서만
// 동작한다. 팝업의 클릭 제스처는 sendMessage 로 background(service worker)
// 까지 전파되지 않으므로, 권한 요청은 반드시 여기 팝업에서 직접 한다.
// 받아두면 background 의 contains() 검사가 프롬프트 없이 통과한다.

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

// url 오리진에 대한 host 권한 확보. 이미 있으면 프롬프트 없이 통과.
async function ensureHostPermission(url) {
  const pattern = originPattern(normalizeBaseUrl(url));
  if (!pattern) return false;
  if (await chrome.permissions.contains({ origins: [pattern] })) return true;
  try {
    return await chrome.permissions.request({ origins: [pattern] });
  } catch (e) {
    return false;
  }
}

function applyI18n() {
  for (const el of document.querySelectorAll("[data-i18n]")) {
    el.textContent = msg(el.dataset.i18n);
  }
}

function showNote(id, key, cls) {
  const el = $(id);
  el.textContent = typeof key === "string" && key.startsWith("msg_") || key.startsWith("err_")
    ? msg(key) : key;
  el.className = "note" + (cls ? " " + cls : "");
  el.style.display = "block";
}

// API 응답({ok,status,data,error}) → 사용자 메시지 키
function apiError(res) {
  if (res.status === 401) return "err_invalid_token";
  if (res.status === 403) return "err_no_archive";
  if (res.status === 503) return "err_credential_disabled";
  if (res.error === "network" || res.status === 0) return "err_network";
  if (res.error === "permission_denied") return "err_permission";
  if (res.error === "no_cookies") return "err_no_cookies";
  if (res.status === 400 && res.data && res.data.detail) return res.data.detail;
  return "err_generic";
}

function displayUrl(u) {
  try {
    const x = new URL(u);
    return x.protocol + "//" + x.host.toLowerCase() + x.pathname + x.search;
  } catch (e) {
    return u;
  }
}

// 루프백/사설 IP 리터럴 선제 판정 (서버 netcheck 의 IP 리터럴 부분만 재현)
function hostKind(u) {
  let host;
  try {
    host = new URL(u).hostname.toLowerCase();
  } catch (e) {
    return "other";
  }
  if (host === "localhost" || host.endsWith(".localhost") ||
      host === "0.0.0.0" || host === "::1" || /^127\./.test(host)) return "loopback";
  if (/^10\./.test(host) || /^192\.168\./.test(host) || /^169\.254\./.test(host) ||
      /^172\.(1[6-9]|2\d|3[01])\./.test(host) ||
      /^(fc|fd|fe80)/.test(host)) return "private";
  return "other";
}

// ---- 탭 전환 ----

function activateTab(name) {
  document.querySelectorAll("nav button").forEach((b) =>
    b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab").forEach((t) =>
    t.classList.toggle("active", t.id === "tab-" + name));
  if (name === "history") loadHistory();
  if (name === "login") initLogin();
}

function initTabs() {
  for (const btn of document.querySelectorAll("nav button")) {
    btn.addEventListener("click", () => activateTab(btn.dataset.tab));
  }
}

// ---- 연결 ----

// 연결 상태를 UI 에 반영: 헤더 점·탭 노출·연결/해제 폼 전환.
// 미연결이면 연결 외 탭을 숨기고 연결 탭으로 되돌린다.
function applyConnState(st) {
  const on = !!st.connected;
  $("#conn-dot").classList.toggle("on", on);
  $("#conn-text").textContent = on
    ? msg("connect_status_connected") + " " + st.prefix + "…"
    : msg("header_not_connected");
  for (const b of document.querySelectorAll("nav button")) {
    if (b.dataset.tab !== "connect") b.style.display = on ? "" : "none";
  }
  if (!on) activateTab("connect");
  $("#connect-form").style.display = on ? "none" : "";
  $("#connected-info").style.display = on ? "" : "none";
  if (on) {
    $("#connected-detail").textContent =
      msg("connect_connected_to", [st.base_url, st.prefix]);
  }
}

async function refreshStatus() {
  const st = await send("status", {});
  $("#base-url").value = st.base_url || "";
  applyConnState(st);
  return st;
}

function initConnect() {
  $("#connect-btn").addEventListener("click", async () => {
    const baseUrl = $("#base-url").value;
    // host 권한은 클릭 제스처가 살아있는 지금(팝업) 받아야 한다.
    // background 에서 요청하면 제스처가 없어 거부된다 (→ err_permission).
    if (!(await ensureHostPermission(baseUrl))) {
      showNote("#connect-result", msg("err_permission"), "err");
      return;
    }
    const res = await send("connect", {
      base_url: baseUrl,
      token: $("#token").value,
    });
    if (res.ok) {
      $("#token").value = "";
      showNote("#connect-result", msg("msg_connected"), "");
      refreshStatus();
    } else {
      const key = { bad_base_url: "err_generic", bad_token: "err_invalid_token",
        invalid_token: "err_invalid_token", permission_denied: "err_permission" }[res.error]
        || "err_generic";
      showNote("#connect-result", msg(key), "err");
    }
  });
  $("#disconnect-btn").addEventListener("click", async () => {
    await send("disconnect", {});
    showNote("#connect-result", msg("msg_disconnected"), "");
    refreshStatus();
  });
  // 토큰 발급 화면 열기 — 아직 연결 전이라 저장된 base_url 이 없을 수 있으므로
  // 지금 입력칸에 적힌 주소를 함께 보낸다 (없으면 안내).
  $("#open-issue").addEventListener("click", async () => {
    const res = await send("openIssue", { base_url: $("#base-url").value });
    if (!res || !res.ok) showNote("#connect-result", msg("err_need_base_url"), "err");
  });
}

// ---- 아카이브 ----

function initArchive() {
  const kind = hostKind(currentUrl);
  const warn = $("#archive-result");
  if (kind === "loopback") {
    $("#archive-page").disabled = true;
    $("#archive-site").disabled = true;
    showNote("#archive-result", msg("err_loopback"), "warn");
  } else if (kind === "private") {
    showNote("#archive-result", msg("archive_private_warn"), "warn");
  }

  $("#archive-page").addEventListener("click", async () => {
    const res = await send("archivePage", { url: currentUrl, force: $("#force").checked });
    if (res.ok && res.data) {
      showNote("#archive-result",
        res.data.queued ? msg("msg_archived", res.data.url) : msg("msg_already"), "");
    } else showNote("#archive-result", msg(apiError(res)), "err");
  });

  $("#archive-site").addEventListener("click", async () => {
    const res = await send("archiveSite", {
      url: currentUrl,
      max_pages: $("#max-pages").value, max_depth: $("#max-depth").value,
      delay: $("#delay").value,
    });
    if (res.ok && res.data) {
      showNote("#archive-result",
        res.data.merged ? msg("msg_crawl_merged", String(res.data.crawl_id))
                        : msg("msg_crawl_started", String(res.data.crawl_id)), "");
    } else showNote("#archive-result", msg(apiError(res)), "err");
  });
}

// ---- 로그인 페이지 (1회성 인증 캡처) ----

async function initLogin() {
  let host = "";
  try { host = new URL(currentUrl).host; } catch (e) {}
  $("#login-domain").textContent = host;
  const cc = await send("cookieCount", { url: currentUrl });
  $("#login-cookie-count").textContent =
    cc.count == null ? "" : msg("login_cookie_count", String(cc.count));
  const consent = $("#consent");
  const runBtn = $("#auth-run");
  consent.addEventListener("change", () => { runBtn.disabled = !consent.checked; });
  runBtn.addEventListener("click", async () => {
    // 쿠키 수집에 필요한 host 권한도 제스처가 있는 팝업에서 먼저 받는다.
    if (!(await ensureHostPermission(currentUrl))) {
      showNote("#login-result", msg("err_permission"), "err");
      return;
    }
    const res = await send("authProfile", { url: currentUrl });
    if (res.ok && res.data) showNote("#login-result", msg("msg_auth_queued"), "");
    else showNote("#login-result", msg(apiError(res)), "err");
  });
}

// ---- 히스토리 ----

function badgeClass(changed, isFirst) {
  if (isFirst) return "new";
  return changed ? "changed" : "same";
}

async function loadHistory() {
  const list = $("#history-list");
  list.innerHTML = "";
  $("#history-result").style.display = "none";
  const res = await send("history", { url: currentUrl });
  if (!res.ok) {
    showNote("#history-result", msg(res.status === 401 ? "err_invalid_token" : "err_no_view"), "err");
    return;
  }
  if (!res.page) {
    showNote("#history-result", msg("history_none"), "");
    return;
  }
  const snaps = res.snapshots.slice().reverse(); // 최신 먼저
  snaps.forEach((s, idx) => {
    const isFirst = idx === snaps.length - 1; // 가장 오래된 = 신규
    const div = document.createElement("div");
    div.className = "snap";
    const bcls = badgeClass(s.changed, isFirst);
    div.innerHTML =
      `<div class="ts mono">${s.taken_at}</div>` +
      `<div><span class="badge ${bcls}">${msg("badge_" + bcls)}</span> ` +
      `<span class="hash mono">${(s.content_hash || "").slice(0, 12)}</span></div>`;
    div.addEventListener("click", () =>
      send("openDeepLink", { page_id: res.page.id }));
    list.appendChild(div);
  });
}

// ---- 부팅 ----

async function main() {
  applyI18n();
  initTabs();
  initConnect();
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  currentUrl = (tab && tab.url) || "";
  $("#cur-url").textContent = displayUrl(currentUrl);
  await refreshStatus();
  initArchive();
  $("#open-dashboard").addEventListener("click", () =>
    send("openDeepLink", { url: currentUrl }));
}

document.addEventListener("DOMContentLoaded", main);
