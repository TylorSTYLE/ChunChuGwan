// 춘추관 확장 popup — UI 만 담당한다. 모든 API 호출·토큰 접근은 background 에
// 메시지로 위임한다 (토큰은 popup 으로 오지 않는다).

const $ = (sel) => document.querySelector(sel);
const send = (type, payload) => chrome.runtime.sendMessage({ type, payload });
const msg = (key, subs) => chrome.i18n.getMessage(key, subs) || key;

let currentUrl = "";

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

function initTabs() {
  for (const btn of document.querySelectorAll("nav button")) {
    btn.addEventListener("click", () => {
      document.querySelectorAll("nav button").forEach((b) => b.classList.remove("active"));
      document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
      btn.classList.add("active");
      $("#tab-" + btn.dataset.tab).classList.add("active");
      if (btn.dataset.tab === "history") loadHistory();
      if (btn.dataset.tab === "login") initLogin();
    });
  }
}

// ---- 연결 ----

async function refreshStatus() {
  const st = await send("status", {});
  $("#conn-dot").classList.toggle("on", st.connected);
  $("#conn-text").textContent = st.connected
    ? msg("connect_status_connected") + " " + st.prefix + "…"
    : msg("header_not_connected");
  $("#base-url").value = st.base_url || "";
  return st;
}

function initConnect() {
  $("#connect-btn").addEventListener("click", async () => {
    const res = await send("connect", {
      base_url: $("#base-url").value,
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
  $("#open-issue").addEventListener("click", () => send("openIssue", {}));
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
