// 춘추관 확장 popup — UI 만 담당한다. 모든 API 호출·토큰 접근은 background 에
// 메시지로 위임한다 (토큰은 popup 으로 오지 않는다).

const $ = (sel) => document.querySelector(sel);
const send = (type, payload) => chrome.runtime.sendMessage({ type, payload });
const msg = (key, subs) => chrome.i18n.getMessage(key, subs) || key;

let currentUrl = "";
let currentTabId = null;
let selectedNetworkTag = null; // 사설 호스트 캡처용 사전 선택 태그 (기능 3)

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
  for (const el of document.querySelectorAll("[data-i18n-ph]")) {
    el.placeholder = msg(el.dataset.i18nPh);
  }
}

// 페이지 메모(선택) — 캡처/아카이브 성공 후 이 URL 의 페이지 메모로 등록한다.
// 캡처 메커니즘과 독립이라 성공 결과 메시지에 등록 여부만 덧붙인다.
async function maybeSaveMemo() {
  const box = $("#capture-memo");
  const memo = (box?.value || "").trim();
  if (!memo) return "";
  const res = await send("addNote", { url: currentUrl, content: memo });
  if (res && res.ok) {
    box.value = "";
    return " · " + msg("memo_saved");
  }
  return " · " + msg("memo_failed");
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
  if (res.status === 429) return "err_rate_limited";
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
  chrome.storage.local.set({ last_tab: name }); // 마지막 선택 탭 (연결 시 복원)
  if (name === "history") loadHistory();
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

// 인증 캡처에 실제로 쓰인 로그인 방식 표시 (background 가 res.auth_kind 로 알려준다)
function authSuffix(res) {
  if (res.auth_kind === "jwt") return " · " + msg("auth_kind_jwt");
  if (res.auth_kind === "session") return " · " + msg("auth_kind_session");
  return "";
}

// 토글 미리보기 — detectAuth 응답을 사람이 읽는 문구로
function authPreview(det) {
  if (!det) return "";
  if (det.kind === "jwt") return msg("login_detected_jwt");
  if (det.kind === "session") return msg("login_cookie_count", String(det.count));
  if (det.kind === "none") return msg("login_detected_none");
  return ""; // unknown — 권한 미부여 (실행 시 요청)
}

function initArchive() {
  const kind = hostKind(currentUrl);
  if (kind === "loopback") {
    $("#archive-page").disabled = true;
    $("#archive-site").disabled = true;
    showNote("#archive-result", msg("err_loopback"), "warn");
  } else if (kind === "private") {
    // 사설 호스트 — 캡처 전에 태그를 고르게 해 2회 캡처(422 후 재캡처)를 피한다.
    showTagPicker();
    showNote("#archive-result", msg("tag_pick_hint"), "warn");
  }

  initLoginOption();

  // 로그인 세션을 포함하면 인증 경로(쿠키 수집 → auth-profiles/crawl)로 보낸다.
  // host 권한은 제스처가 살아있는 팝업에서 먼저 받아야 한다(background 는 거부됨).
  const wantsAuth = () => $("#with-login").checked && !$("#with-login").disabled;

  $("#archive-page").addEventListener("click", async () => {
    const auth = wantsAuth();
    if (auth && !(await ensureHostPermission(currentUrl))) {
      showNote("#archive-result", msg("err_permission"), "err");
      return;
    }
    const res = await send(auth ? "archivePageAuth" : "archivePage",
      { url: currentUrl, force: $("#force").checked, tabId: currentTabId, network_tag: selectedNetworkTag });
    if (res.ok && res.data) {
      const base = res.data.queued ? msg("msg_archived", res.data.url) : msg("msg_already");
      const hint = res.data.queued && $("#notify-toggle").checked
        ? " " + msg("archive_notify_hint") : "";
      const memoSuffix = await maybeSaveMemo();
      showNote("#archive-result", base + (auth ? authSuffix(res) : "") + hint + memoSuffix, "");
    } else showNote("#archive-result", msg(apiError(res)), "err");
  });

  $("#archive-site").addEventListener("click", async () => {
    const auth = wantsAuth();
    if (auth && !(await ensureHostPermission(currentUrl))) {
      showNote("#archive-result", msg("err_permission"), "err");
      return;
    }
    const res = await send(auth ? "archiveSiteAuth" : "archiveSite", {
      url: currentUrl, tabId: currentTabId, network_tag: selectedNetworkTag,
      max_pages: $("#max-pages").value, max_depth: $("#max-depth").value,
      delay: $("#delay").value,
    });
    if (res.ok && res.data) {
      const base = res.data.merged ? msg("msg_crawl_merged", String(res.data.crawl_id))
                                   : msg("msg_crawl_started", String(res.data.crawl_id));
      const hint = $("#notify-toggle").checked ? " " + msg("archive_notify_hint") : "";
      const memoSuffix = await maybeSaveMemo();
      showNote("#archive-result", base + (auth ? authSuffix(res) : "") + hint + memoSuffix, "");
    } else showNote("#archive-result", msg(apiError(res)), "err");
  });
}

// 작업 완료 알림 토글 — background 의 notify_enabled 와 동기 (기본 on).
async function initNotifyToggle() {
  const cb = $("#notify-toggle");
  if (!cb) return;
  const pref = await send("getNotifyPref", {});
  cb.checked = !(pref && pref.on === false);
  cb.addEventListener("change", () => send("setNotifyPref", { on: cb.checked }));
}

// ---- 로그인 세션 포함 옵션 (인증 캡처는 서버 가드와 일치해 https 대상만) ----

// 체크하면 host 권한을 받아(제스처) 현재 도메인 쿠키 개수를 보여준다. ID/PW 는
// 절대 수집하지 않고 이미 로그인된 세션 쿠키만 전송한다 (background.collectCapsule).
function initLoginOption() {
  const cb = $("#with-login");
  const info = $("#login-info");
  const hint = $("#login-hint");
  if (!/^https:\/\//i.test(currentUrl) || hostKind(currentUrl) === "loopback") {
    cb.disabled = true;
    hint.textContent = msg("archive_login_https_only");
    hint.style.display = "block";
    return;
  }
  cb.addEventListener("change", async () => {
    if (!cb.checked) { info.style.display = "none"; return; }
    if (!(await ensureHostPermission(currentUrl))) {
      cb.checked = false;
      showNote("#archive-result", msg("err_permission"), "err");
      return;
    }
    const det = await send("detectAuth", { url: currentUrl, tabId: currentTabId });
    $("#login-cookie-count").textContent = authPreview(det);
    info.style.display = "block";
  });
}

// ---- 브라우저 캡처 (서버 무요청 — 지금 보는 화면을 그대로) ----

const CAP_UNSUPPORTED = /^(chrome|edge|about|view-source|chrome-extension|moz-extension|devtools|file):/i;

function capturablePopup(u) {
  return !!u && /^https?:\/\//i.test(u) && !CAP_UNSUPPORTED.test(u);
}

// 자원 재요청에 광범위 host 권한 필요 — 제스처가 살아있는 팝업에서 받아야 한다.
async function ensureAllUrls() {
  if (await chrome.permissions.contains({ origins: ["*://*/*"] })) return true;
  try { return await chrome.permissions.request({ origins: ["*://*/*"] }); }
  catch (e) { return false; }
}

function captureError(res) {
  if (res.error === "unsupported_page") return "err_unsupported_page";
  if (res.status === 401) return "err_invalid_token";
  if (res.status === 429) return "err_rate_limited";
  if (res.status === 403) return "err_no_archive";
  if (res.status === 413) return "err_too_large";
  if (res.error === "capture_failed") return "err_capture_failed";
  if (res.error === "network" || res.status === 0) return "err_network";
  if (res.status === 400 && res.data && res.data.detail) return res.data.detail;
  return "err_generic";
}

async function runBrowserCapture(networkTag) {
  if (!(await ensureAllUrls())) {
    showNote("#archive-result", msg("err_permission_all"), "err");
    return;
  }
  const btn = $("#capture-browser");
  btn.disabled = true;
  showNote("#archive-result", msg("capture_running"), "");
  try {
    const res = await send("captureBrowser", {
      url: currentUrl, tabId: currentTabId,
      force: $("#force").checked, network_tag: networkTag || null,
    });
    if (res.ok && res.data) {
      $("#tag-picker").style.display = "none";
      const inc = res.data.incomplete ? " · " + msg("capture_incomplete") : "";
      const memoSuffix = await maybeSaveMemo();
      showNote("#archive-result", msg("msg_captured", [msg("status_" + res.data.status)]) + inc + memoSuffix, "");
    } else if (res.needs_network_tag) {
      await showTagPicker();
      showNote("#archive-result", msg("capture_needs_tag", [res.host || ""]), "warn");
    } else {
      showNote("#archive-result", msg(captureError(res)), "err");
    }
  } finally {
    btn.disabled = false;
  }
}

// 사설 호스트 — 등록된 네트워크 태그 목록을 채워 사전 선택하게 한다 (선택값은
// selectedNetworkTag 에 보관되고, 캡처/아카이브 버튼이 읽는다 — 2회 캡처 방지).
async function showTagPicker() {
  const res = await send("listNetworkTags", {});
  const sel = $("#tag-select");
  sel.innerHTML = "";
  const none = document.createElement("option");
  none.value = "";
  none.textContent = msg("tag_none_option");
  sel.appendChild(none);
  const tags = (res && res.data && res.data.tags) || [];
  for (const t of tags) {
    const o = document.createElement("option");
    o.value = String(t.id);
    o.textContent = t.name;
    sel.appendChild(o);
  }
  sel.value = selectedNetworkTag || "";
  $("#tag-picker").style.display = "block";
}

function initBrowserCapture() {
  const btn = $("#capture-browser");
  if (!capturablePopup(currentUrl) || hostKind(currentUrl) === "loopback") {
    btn.disabled = true;
    if (!capturablePopup(currentUrl)) showNote("#archive-result", msg("err_unsupported_page"), "warn");
  } else {
    btn.addEventListener("click", () => runBrowserCapture(selectedNetworkTag));
  }
  // 사전 선택 — 선택값만 보관하고 캡처/아카이브 버튼이 읽는다.
  $("#tag-select").addEventListener("change", (e) => {
    selectedNetworkTag = e.target.value || null;
  });
  $("#tag-add").addEventListener("click", async () => {
    const name = $("#tag-new").value.trim();
    if (!name) return;
    const res = await send("createNetworkTag", { name });
    if (res.ok && res.data) {
      $("#tag-new").value = "";
      selectedNetworkTag = String(res.data.id); // 새로 만든 태그 자동 선택
      await showTagPicker();
    } else showNote("#archive-result", msg(res.status === 403 ? "err_tag_perm" : "err_generic"), "err");
  });
}

// ---- 히스토리 ----

// 서버의 ISO 8601(UTC 등) 시각을 브라우저(시스템)의 시간대로 표시. 파싱 실패 시 원문.
function formatLocalTime(iso) {
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso || "";
  return d.toLocaleString();
}

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
      `<div class="ts mono">${formatLocalTime(s.taken_at)}</div>` +
      `<div><span class="badge ${bcls}">${msg("badge_" + bcls)}</span> ` +
      `<span class="hash mono">${(s.content_hash || "").slice(0, 12)}</span></div>`;
    div.addEventListener("click", () =>
      send("openDeepLink", { page_id: res.page.id }));
    list.appendChild(div);
  });
}

// ---- 업데이트 안내 ----

// 연결된 서버 버전과 설치된 확장 버전을 비교해, 서버가 더 최신이면 배너를 띄운다.
// 확장은 웹스토어 미등록 unpacked 라 자동 업데이트가 없어 사용자가 직접 재설치해야 한다.
async function checkUpdate() {
  const res = await send("checkVersion", {});
  if (!res || !res.update_available) return;
  $("#update-text").textContent = msg("update_available", [res.latest]);
  $("#update-banner").style.display = "block";
  $("#update-reinstall").addEventListener("click", () => send("openDownload", {}));
}

// 아카이브 상태 배지 토글 — background 의 status_badge_enabled 와 동기 (기본 on).
async function initStatusBadgeToggle() {
  const cb = $("#status-badge-toggle");
  if (!cb) return;
  const pref = await send("getStatusBadgePref", {});
  cb.checked = !(pref && pref.on === false);
  cb.addEventListener("change", () => send("setStatusBadgePref", { on: cb.checked }));
}

// background 푸시 수신 — 캡처 진행률 표시 + 인증 만료 시 미연결 UI 전환.
function initBackgroundListener() {
  chrome.runtime.onMessage.addListener((m) => {
    if (!m) return;
    if (m.type === "capture_progress") {
      const label = msg("cap_phase_" + m.phase) + (m.total ? ` (${m.done}/${m.total})` : "");
      showNote("#archive-result", label, "");
    } else if (m.type === "auth_lost") {
      refreshStatus();
      showNote("#connect-result", msg("msg_auth_lost"), "err");
    }
  });
}

// 연결 시 마지막으로 보던 탭 복원 (connect 는 미연결 때 강제됐던 값이라 archive 로 치환).
async function restoreLastTab() {
  const c = await chrome.storage.local.get("last_tab");
  const name = c.last_tab;
  activateTab(name === "archive" || name === "history" ? name : "archive");
}

// ---- 부팅 ----

async function main() {
  applyI18n();
  initTabs();
  initConnect();
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  currentUrl = (tab && tab.url) || "";
  currentTabId = tab && tab.id != null ? tab.id : null;
  $("#cur-url").textContent = displayUrl(currentUrl);
  const st = await refreshStatus();
  initArchive();
  initBrowserCapture();
  initNotifyToggle();
  initStatusBadgeToggle();
  initBackgroundListener();
  $("#open-dashboard").addEventListener("click", () =>
    send("openDeepLink", { url: currentUrl }));
  if (st && st.connected) {
    checkUpdate();
    await restoreLastTab(); // 연결 시 마지막 선택 탭 복원
  }
}

document.addEventListener("DOMContentLoaded", main);
