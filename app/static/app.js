const state = {
  user: null,
  status: null,
  account: null,
  accounts: [],
  activeAccountId: null,
  editingAccountId: null,
  overview: null,
  drafts: [],
  draftTotal: 0,
  draftHasMore: false,
  files: [],
  assets: [],
  selected: new Set(),
  uploading: false,
  deleting: false,
  loadingAssets: true,
  pairingCode: null,
  pairingExpiresAt: 0,
  pairingExchangeUrl: "",
  pairingWarning: "",
};

const views = {
  overview: ["CONTROL CENTER", "运行总览"],
  account: ["OFFICIAL ACCOUNT", "公众号设置"],
  assets: ["MATERIAL LIBRARY", "素材库"],
  drafts: ["DRAFT OPERATIONS", "草稿箱"],
  api: ["API ACCESS", "API 接入"],
  security: ["SECURITY", "安全设置"],
};

const $ = selector => document.querySelector(selector);
const els = {
  authView: $("#authView"), appShell: $("#appShell"), loginForm: $("#loginForm"),
  loginUsername: $("#loginUsername"), loginPassword: $("#loginPassword"),
  loginError: $("#loginError"), loginSubmit: $("#loginSubmit"), currentUsername: $("#currentUsername"),
  registerForm: $("#registerForm"), registerUsername: $("#registerUsername"), registerPassword: $("#registerPassword"),
  registerPasswordConfirm: $("#registerPasswordConfirm"), registerError: $("#registerError"), registerSubmit: $("#registerSubmit"),
  showRegister: $("#showRegister"), showLogin: $("#showLogin"),
  setupForm: $("#setupForm"), setupToken: $("#setupToken"), setupUsername: $("#setupUsername"), setupPassword: $("#setupPassword"),
  setupPasswordConfirm: $("#setupPasswordConfirm"), setupError: $("#setupError"), setupSubmit: $("#setupSubmit"),
  logoutButton: $("#logoutButton"), viewKicker: $("#viewKicker"), viewTitle: $("#viewTitle"),
  connection: $("#connectionState"), testConnection: $("#testConnection"),
  accountSwitcher: $("#accountSwitcher"),
  sidebarAccountMark: $("#sidebarAccountMark"), sidebarAccountName: $("#sidebarAccountName"), sidebarAccountId: $("#sidebarAccountId"),
  metricConnection: $("#metricConnection"), metricAccount: $("#metricAccount"), metricAssets: $("#metricAssets"),
  metricDrafts: $("#metricDrafts"), metricDraftFailures: $("#metricDraftFailures"), metricTemporary: $("#metricTemporary"),
  overviewApis: $("#overviewApis"), recentDrafts: $("#recentDrafts"),
  accountForm: $("#accountForm"), accountName: $("#accountName"), accountType: $("#accountType"),
  accountAppId: $("#accountAppId"), accountSecret: $("#accountSecret"), accountSource: $("#accountSource"),
  accountMeta: $("#accountMeta"), accountSave: $("#accountSave"), accountTest: $("#accountTest"),
  accountFormTitle: $("#accountFormTitle"), accountList: $("#accountList"), accountCount: $("#accountCount"),
  newAccount: $("#newAccount"), adminPairingSection: $("#adminPairingSection"),
  input: $("#fileInput"), dropZone: $("#dropZone"), queue: $("#queue"), queueSummary: $("#queueSummary"),
  clearQueue: $("#clearQueue"), startUpload: $("#startUpload"), assetTable: $("#assetTable"),
  assetCount: $("#assetCount"), copyUrls: $("#copyUrls"), copyJson: $("#copyJson"), deleteSelected: $("#deleteSelected"),
  draftTable: $("#draftTable"), draftCount: $("#draftCount"), refreshDrafts: $("#refreshDrafts"), loadMoreDrafts: $("#loadMoreDrafts"),
  apiEndpoints: $("#apiEndpoints"), runDiagnostics: $("#runDiagnostics"), diagnosticResults: $("#diagnosticResults"),
  generatePairingCode: $("#generatePairingCode"), copyPairingCode: $("#copyPairingCode"),
  pairingCode: $("#pairingCode"), pairingCountdown: $("#pairingCountdown"),
  pairingHint: $("#pairingHint"), pairingExchangeUrl: $("#pairingExchangeUrl"),
  confirmDialog: $("#confirmDialog"), confirmTitle: $("#confirmTitle"),
  confirmMessage: $("#confirmMessage"), confirmAction: $("#confirmAction"), toast: $("#toast"),
  passwordForm: $("#passwordForm"), currentPassword: $("#currentPassword"), newPassword: $("#newPassword"),
  newPasswordConfirm: $("#newPasswordConfirm"), passwordError: $("#passwordError"), passwordSubmit: $("#passwordSubmit"),
};

function escapeHtml(value) {
  return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
}

function formatBytes(bytes) {
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  const exponent = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return `${(bytes / 1024 ** exponent).toFixed(exponent ? 1 : 0)} ${units[exponent]}`;
}

function formatDate(value) {
  if (!value) return "-";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
  }).format(date);
}

let toastTimer;
let pairingTimer;
function toast(message) {
  els.toast.textContent = message;
  els.toast.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => els.toast.classList.remove("show"), 3200);
}

async function api(url, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.method && options.method !== "GET") headers.set("X-Requested-With", "WechatUploader");
  const response = await fetch(url, { ...options, headers });
  let payload = {};
  try { payload = await response.json(); } catch { /* empty response */ }
  if (!response.ok) {
    const detail = Array.isArray(payload.detail)
      ? payload.detail.map(item => item.msg || String(item)).join("；")
      : payload.detail;
    const error = new Error(detail || `请求失败（${response.status}）`);
    error.status = response.status;
    if (response.status === 401 && !url.startsWith("/api/auth/")) showLogin();
    throw error;
  }
  return payload;
}

function showView(name, updateHash = true) {
  const next = views[name] ? name : "overview";
  document.querySelectorAll(".nav-item").forEach(button => button.classList.toggle("active", button.dataset.view === next));
  document.querySelectorAll(".view").forEach(panel => panel.classList.toggle("active", panel.dataset.viewPanel === next));
  [els.viewKicker.textContent, els.viewTitle.textContent] = views[next];
  if (updateHash) history.replaceState(null, "", `#${next}`);
  window.scrollTo({ top: 0, behavior: "instant" });
}

function resetWorkspaceState() {
  state.user = null;
  state.status = null;
  state.account = null;
  state.accounts = [];
  state.activeAccountId = null;
  state.editingAccountId = null;
  state.overview = null;
  state.drafts = [];
  state.draftTotal = 0;
  state.draftHasMore = false;
  state.pairingCode = null;
  state.pairingExpiresAt = 0;
  state.pairingExchangeUrl = "";
  state.pairingWarning = "";
  clearInterval(pairingTimer);
  state.files = [];
  state.assets = [];
  state.selected = new Set();
  state.uploading = false;
  state.deleting = false;
  state.loadingAssets = false;
  els.currentUsername.textContent = "";
  els.connection.textContent = "等待登录";
  els.connection.className = "status-badge";
  els.metricConnection.textContent = "未加载";
  els.metricAccount.textContent = "未配置账号";
  els.metricAssets.textContent = "0";
  els.metricDrafts.textContent = "0";
  els.metricDraftFailures.textContent = "无异常任务";
  els.metricTemporary.textContent = "0";
  els.overviewApis.innerHTML = '<p class="empty-state">登录后加载接口状态</p>';
  els.recentDrafts.innerHTML = '<p class="empty-state">暂无草稿记录</p>';
  els.apiEndpoints.innerHTML = "";
  els.diagnosticResults.innerHTML = '<p class="empty-state">尚未运行诊断</p>';
  updateAccountIdentity(null);
  renderAccountList();
  renderAccount();
  renderDrafts();
  renderQueue();
  renderAssets();
  renderPairingCode();
}

function showLogin() {
  resetWorkspaceState();
  els.appShell.hidden = true;
  els.authView.hidden = false;
  els.authView.setAttribute("aria-labelledby", "loginTitle");
  els.setupForm.hidden = true;
  els.registerForm.hidden = true;
  els.loginForm.hidden = false;
  setTimeout(() => els.loginUsername.focus(), 0);
}

function showSetup() {
  resetWorkspaceState();
  els.appShell.hidden = true;
  els.authView.hidden = false;
  els.authView.setAttribute("aria-labelledby", "setupTitle");
  els.loginForm.hidden = true;
  els.registerForm.hidden = true;
  els.setupForm.hidden = false;
  els.setupError.textContent = "";
  setTimeout(() => els.setupUsername.focus(), 0);
}

function showRegister() {
  els.appShell.hidden = true;
  els.authView.hidden = false;
  els.authView.setAttribute("aria-labelledby", "registerTitle");
  els.loginForm.hidden = true;
  els.setupForm.hidden = true;
  els.registerForm.hidden = false;
  els.registerError.textContent = "";
  setTimeout(() => els.registerUsername.focus(), 0);
}

function showWorkspace(user) {
  state.user = user;
  els.currentUsername.textContent = user.username;
  els.authView.hidden = true;
  els.appShell.hidden = true;
  els.loginError.textContent = "";
  els.registerError.textContent = "";
  els.setupError.textContent = "";
  els.loginForm.reset();
  els.registerForm.reset();
  els.setupForm.reset();
  els.adminPairingSection.hidden = user.role !== "admin";
  showView(location.hash.slice(1) || "overview", false);
}

async function enterWorkspace(user) {
  showWorkspace(user);
  await loadWorkspace();
  els.appShell.hidden = false;
}

function updateAccountIdentity(account) {
  const configured = account && account.app_id;
  const name = configured ? account.display_name : "未配置公众号";
  els.sidebarAccountName.textContent = name;
  els.sidebarAccountId.textContent = configured ? account.app_id : "等待配置";
  els.sidebarAccountMark.textContent = configured ? name.slice(0, 1) : "未";
}

async function checkStatus() {
  try {
    state.status = await api("/api/status");
    const ready = state.status.ready;
    const wechatInputs = document.querySelectorAll('input[name="mode"]:not([value="temporary"])');
    wechatInputs.forEach(input => { input.disabled = !ready; });
    if (!ready) document.querySelector('input[name="mode"][value="temporary"]').checked = true;
    els.connection.textContent = ready ? `微信已配置 · ${state.status.app_id_suffix}` : "微信未配置";
    els.connection.className = `status-badge ${ready ? "ok" : "warning"}`;
    els.testConnection.disabled = !ready;
    updateAccountIdentity(state.status.account);
    renderApiEndpoints();
  } catch (error) {
    els.connection.textContent = "状态异常";
    els.connection.className = "status-badge error";
    toast(error.message);
  }
}

function capability(name, ready, detail) {
  return `<div class="capability"><span class="signal ${ready ? "ok" : "off"}"></span><div><strong>${escapeHtml(name)}</strong><small>${escapeHtml(detail)}</small></div><b>${ready ? "可用" : "未配置"}</b></div>`;
}

function draftStatus(status) {
  return ({ created: ["已写入", "ok"], failed: ["失败", "error"], unknown: ["结果待核实", "warning"], pending: ["处理中", "pending"], deleted: ["已删除", "muted"] })[status] || [status, "muted"];
}

function draftOwner(draft) {
  if (state.user?.role !== "admin") return "";
  const owner = draft.owner_username || "未知用户";
  const account = draft.account_display_name || "未知公众号";
  return `<span class="draft-owner">${escapeHtml(owner)} / ${escapeHtml(account)}</span>`;
}

function renderOverview() {
  const data = state.overview;
  if (!data) return;
  const account = data.account;
  els.metricConnection.textContent = data.apis.wechat ? "已配置" : "未配置";
  els.metricAccount.textContent = account.app_id ? `${account.display_name} · ${account.app_id_suffix}` : "未配置账号";
  els.metricAssets.textContent = data.counts.assets;
  els.metricDrafts.textContent = data.counts.drafts;
  els.metricTemporary.textContent = data.counts.temporary_assets;
  const draftIssues = data.counts.failed_drafts + (data.counts.unknown_drafts || 0);
  els.metricDraftFailures.textContent = draftIssues ? `${draftIssues} 个异常任务` : "无异常任务";
  els.overviewApis.innerHTML = [
    capability("微信平台", data.apis.wechat, data.apis.wechat ? "凭据已加载" : "等待公众号配置"),
    capability("AI 客户端", data.apis.client, "验证码配对后统一访问图片与草稿接口"),
  ].join("");
  els.recentDrafts.innerHTML = data.recent_drafts.length ? data.recent_drafts.map(draft => {
    const [label, className] = draftStatus(draft.status);
    return `<div class="compact-row"><div>${draftOwner(draft)}<strong>${escapeHtml(draft.title)}</strong><small>${formatDate(draft.updated_at)} · ${escapeHtml(draft.request_id)}</small></div><span class="status-pill ${className}">${label}</span></div>`;
  }).join("") : '<p class="empty-state">暂无草稿记录</p>';
}

async function loadOverview() {
  try {
    state.overview = await api("/api/overview");
    renderOverview();
  } catch (error) { toast(`加载总览失败：${error.message}`); }
}

function renderAccount() {
  const account = state.accounts.find(item => item.id === state.editingAccountId) || null;
  els.accountFormTitle.textContent = account ? "编辑公众号" : "添加公众号";
  els.accountName.value = account?.display_name || "";
  els.accountType.value = account?.account_type || "subscription";
  els.accountAppId.value = account?.app_id || "";
  els.accountSecret.value = "";
  els.accountSecret.required = !account;
  els.accountSecret.placeholder = account ? "留空则保持不变" : "请输入 AppSecret";
  els.accountSource.textContent = account ? (account.id === state.activeAccountId ? "当前公众号" : "已保存") : "新公众号";
  if (account) {
    const secret = account.secret_configured ? "AppSecret 已保存" : "AppSecret 未配置";
    const encryption = account.encryption === "environment" ? "环境主密钥" : "本机密钥文件";
    els.accountMeta.textContent = `${secret} · ${encryption}${account.updated_at ? ` · ${formatDate(account.updated_at)}` : ""}`;
  } else {
    els.accountMeta.textContent = state.accounts.length ? "保存后可在上方切换公众号" : "完成首个公众号配置后即可开始上传素材";
  }
  els.accountSave.textContent = account ? "保存修改" : "添加并启用";
  els.accountTest.disabled = !account || account.id !== state.activeAccountId || !account.secret_configured || !account.app_id;
}

function renderAccountList() {
  els.accountCount.textContent = `${state.accounts.length} 个`;
  els.accountSwitcher.disabled = !state.accounts.length;
  els.accountSwitcher.innerHTML = state.accounts.length
    ? state.accounts.map(account => `<option value="${account.id}" ${account.id === state.activeAccountId ? "selected" : ""}>${escapeHtml(account.display_name)}</option>`).join("")
    : "<option>暂无公众号</option>";
  if (!state.accounts.length) {
    els.accountList.innerHTML = '<p class="empty-state">尚未添加公众号</p>';
    return;
  }
  els.accountList.innerHTML = state.accounts.map(account => {
    const active = account.id === state.activeAccountId;
    const type = account.account_type === "service" ? "服务号" : "订阅号";
    return `<article class="account-item ${active ? "active" : ""}" data-id="${account.id}"><span class="account-list-mark">${escapeHtml(account.display_name.slice(0, 1))}</span><div class="account-list-copy"><strong>${escapeHtml(account.display_name)}</strong><small>${type} · ${escapeHtml(account.app_id)}</small></div><span class="status-pill ${active ? "ok" : "muted"}">${active ? "当前" : "未启用"}</span><div class="account-actions">${active ? "" : `<button class="button secondary activate-account" type="button" data-id="${account.id}">切换</button>`}<button class="button secondary edit-account" type="button" data-id="${account.id}">编辑</button><button class="button danger-outline delete-account" type="button" data-id="${account.id}">删除</button></div></article>`;
  }).join("");
}

async function loadAccounts() {
  try {
    const result = await api("/api/accounts");
    state.accounts = result.items;
    state.activeAccountId = result.active_account_id;
    state.account = state.accounts.find(item => item.id === state.activeAccountId) || null;
    if (state.editingAccountId !== null && !state.accounts.some(item => item.id === state.editingAccountId)) {
      state.editingAccountId = null;
    }
    if (state.editingAccountId === null && state.account) state.editingAccountId = state.account.id;
    updateAccountIdentity(state.account);
    renderAccountList();
    renderAccount();
  } catch (error) { toast(`加载公众号配置失败：${error.message}`); }
}

async function saveAccount(event) {
  event.preventDefault();
  els.accountSave.disabled = true;
  els.accountSave.textContent = "保存中";
  const payload = {
    display_name: els.accountName.value.trim(),
    account_type: els.accountType.value,
    app_id: els.accountAppId.value.trim(),
  };
  if (els.accountSecret.value.trim()) payload.app_secret = els.accountSecret.value.trim();
  try {
    const accountId = state.editingAccountId;
    const url = accountId === null ? "/api/accounts" : `/api/accounts/${accountId}`;
    const method = accountId === null ? "POST" : "PUT";
    const result = await api(url, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    state.editingAccountId = result.account.id;
    await loadAccounts();
    await Promise.all([checkStatus(), loadOverview(), loadAssets(), loadDrafts()]);
    toast(accountId === null ? "公众号已添加并启用" : "公众号配置已保存");
  } catch (error) { toast(error.message); }
  finally { els.accountSave.disabled = false; renderAccount(); }
}

async function activateAccount(accountId) {
  if (accountId === state.activeAccountId) return;
  try {
    const result = await api(`/api/accounts/${accountId}/activate`, { method: "POST" });
    state.activeAccountId = result.account.id;
    state.editingAccountId = result.account.id;
    state.selected = new Set();
    state.files = [];
    renderQueue();
    await loadAccounts();
    await Promise.all([checkStatus(), loadOverview(), loadAssets(), loadDrafts()]);
    toast(`已切换到 ${result.account.display_name}`);
  } catch (error) { toast(error.message); }
}

async function deleteAccount(account) {
  const confirmed = await askConfirmation("删除公众号", `将删除“${account.display_name}”及其本地素材和草稿记录。`, "确认删除");
  if (!confirmed) return;
  try {
    await api(`/api/accounts/${account.id}`, { method: "DELETE" });
    state.editingAccountId = null;
    state.selected = new Set();
    await loadAccounts();
    await Promise.all([checkStatus(), loadOverview(), loadAssets(), loadDrafts()]);
    toast("公众号已删除");
  } catch (error) { toast(error.message); }
}

async function testAccount() {
  const buttons = [els.accountTest, els.testConnection];
  buttons.forEach(button => { button.disabled = true; });
  try {
    await api("/api/account/test", { method: "POST" });
    toast("微信接口连接成功");
    await checkStatus();
  } catch (error) { toast(`连接失败：${error.message}`); }
  finally { buttons.forEach(button => { button.disabled = !(state.status && state.status.ready); }); }
}

async function loadDrafts(options = {}) {
  const append = options?.append === true;
  if (!append) els.draftTable.innerHTML = '<p class="empty-state">正在加载草稿记录...</p>';
  els.loadMoreDrafts.disabled = true;
  try {
    const offset = append ? state.drafts.length : 0;
    const result = await api(`/api/drafts?limit=100&offset=${offset}`);
    state.drafts = append ? [...state.drafts, ...result.items] : result.items;
    state.draftTotal = result.count;
    state.draftHasMore = result.has_more;
    renderDrafts();
  } catch (error) {
    if (!append) els.draftTable.innerHTML = `<p class="empty-state error-text">${escapeHtml(error.message)}</p>`;
    else toast(`加载更多失败：${error.message}`);
  } finally { els.loadMoreDrafts.disabled = false; }
}

function renderDrafts() {
  els.draftCount.textContent = `${state.draftTotal} 条`;
  els.loadMoreDrafts.hidden = !state.draftHasMore;
  if (!state.drafts.length) {
    els.draftTable.innerHTML = '<p class="empty-state">暂无草稿任务</p>';
    return;
  }
  els.draftTable.innerHTML = `<div class="draft-row table-head"><span>文章</span><span>请求标识</span><span>状态</span><span>更新时间</span><span>操作</span></div>${state.drafts.map(draft => {
    const [label, className] = draftStatus(draft.status);
    const error = draft.last_error ? `<small class="error-text">${escapeHtml(draft.last_error)}</small>` : `<small>${draft.content_characters} 字符</small>`;
    return `<div class="draft-row"><div>${draftOwner(draft)}<strong>${escapeHtml(draft.title)}</strong>${error}</div><code>${escapeHtml(draft.request_id)}</code><span><b class="status-pill ${className}">${label}</b></span><span>${formatDate(draft.updated_at)}</span><div>${draft.status === "created" && draft.can_delete ? `<button class="button danger-outline delete-draft" data-id="${draft.id}" type="button">删除</button>` : "-"}</div></div>`;
  }).join("")}`;
}

async function deleteDraftRecord(draft) {
  const confirmed = await askConfirmation("删除微信草稿", `将从微信公众号草稿箱删除《${draft.title}》。`, "确认删除");
  if (!confirmed) return;
  try {
    await api(`/api/drafts/${draft.id}/delete`, { method: "POST" });
    toast("草稿已删除");
    await Promise.all([loadDrafts(), loadOverview()]);
  } catch (error) { toast(error.message); }
}

function renderApiEndpoints() {
  if (!state.status) return;
  const endpoints = [
    ["POST", "/api/v1/wechat-images", "图片上传", state.status.client_api_ready, "配对令牌"],
    ["POST", "/api/v1/wechat-drafts", "写入草稿", state.status.client_api_ready, "配对令牌"],
    ["GET", "/api/v1/wechat-drafts", "查询草稿", state.status.client_api_ready, "配对令牌"],
    ["GET", "/api/v1/wechat-drafts/{id}", "核对草稿", state.status.client_api_ready, "配对令牌"],
    ["PUT", "/api/v1/wechat-drafts/{id}", "修改草稿", state.status.client_api_ready, "配对令牌"],
    ["DELETE", "/api/v1/wechat-drafts/{id}", "删除草稿", state.status.client_api_ready, "配对令牌"],
    ["GET", "/api/v1/wechat-drafts/wechat-box", "微信草稿箱", state.status.client_api_ready, "配对令牌"],
    ["GET", "/api/v1/wechat-drafts/wechat-box/{media_id}", "读取微信草稿", state.status.client_api_ready, "配对令牌"],
    ["PUT", "/api/v1/wechat-drafts/wechat-box/{media_id}", "修改微信草稿", state.status.client_api_ready, "配对令牌"],
    ["DELETE", "/api/v1/wechat-drafts/wechat-box/{media_id}", "删除微信草稿", state.status.client_api_ready, "配对令牌"],
    ["POST", "/api/v1/pairing/exchange", "兑换客户端令牌", state.status.client_api_ready, "一次性验证码"],
    ["POST", "/api/v1/temp-images", "临时图片", state.status.client_api_ready, "配对令牌"],
  ];
  els.apiEndpoints.innerHTML = endpoints.map(([method, path, name, ready, key]) => `<div class="endpoint"><span class="method">${method}</span><code>${path}</code><strong>${name}</strong><span class="endpoint-key">${key}</span><b class="status-pill ${ready ? "ok" : "muted"}">${ready ? "已启用" : "未配置"}</b></div>`).join("");
}

function renderPairingCode() {
  const remaining = state.pairingExpiresAt ? Math.max(0, Math.ceil((state.pairingExpiresAt - Date.now()) / 1000)) : 0;
  const active = Boolean(state.pairingCode && remaining > 0);
  els.pairingCode.textContent = active ? state.pairingCode : (state.pairingCode ? "已过期" : "---- ----");
  els.pairingCountdown.textContent = active ? `${remaining} 秒后失效` : (state.pairingCode ? "已失效" : "尚未生成");
  els.pairingCountdown.className = `pairing-countdown ${active ? "active" : state.pairingCode ? "expired" : ""}`;
  els.copyPairingCode.disabled = !active;
  els.pairingExchangeUrl.textContent = state.pairingExchangeUrl || "POST /api/v1/pairing/exchange";
  const defaultWarning = location.protocol === "https:"
    ? "生成后手动交给 AI，验证码仅可兑换一次。"
    : "当前为 HTTP 连接，可以使用，但验证码和令牌会明文传输，建议配置 HTTPS。";
  els.pairingHint.textContent = state.pairingWarning || defaultWarning;
  els.pairingHint.classList.toggle("warning", Boolean(state.pairingWarning || location.protocol !== "https:"));
  if (state.pairingCode && !active) clearInterval(pairingTimer);
}

async function generatePairingCode() {
  els.generatePairingCode.disabled = true;
  els.generatePairingCode.textContent = "生成中";
  try {
    const result = await api("/api/pairing-code", { method: "POST" });
    state.pairingCode = result.code;
    state.pairingExpiresAt = Date.now() + result.expires_in * 1000;
    state.pairingExchangeUrl = result.exchange_url;
    state.pairingWarning = result.warning || "";
    clearInterval(pairingTimer);
    pairingTimer = setInterval(renderPairingCode, 250);
    renderPairingCode();
    toast("验证码已生成，60 秒内有效");
  } catch (error) {
    toast(`生成失败：${error.message}`);
  } finally {
    els.generatePairingCode.disabled = false;
    els.generatePairingCode.textContent = "重新生成 1 分钟验证码";
  }
}

async function runDiagnostics() {
  els.runDiagnostics.disabled = true;
  els.runDiagnostics.textContent = "诊断中";
  els.diagnosticResults.innerHTML = '<p class="empty-state">正在检查数据库、微信连接和 API 配置...</p>';
  try {
    const result = await api("/api/diagnostics/run", { method: "POST" });
    els.diagnosticResults.innerHTML = result.checks.map(check => {
      const ready = check.status === "ok";
      return `<div class="capability"><span class="signal ${ready ? "ok" : "off"}"></span><div><strong>${escapeHtml(check.label)}</strong><small>${escapeHtml(check.detail)}</small></div><b>${ready ? "正常" : check.status === "warning" ? "待配置" : "异常"}</b></div>`;
    }).join("");
    toast(result.ready ? `v${result.version} 诊断通过` : `v${result.version} 仍有项目需要处理`);
  } catch (error) {
    els.diagnosticResults.innerHTML = `<p class="empty-state error-text">${escapeHtml(error.message)}</p>`;
  } finally {
    els.runDiagnostics.disabled = false;
    els.runDiagnostics.textContent = "重新诊断";
  }
}

function assetKey(asset) { return `${asset.kind || "wechat"}:${asset.id}`; }
function preferredUrl(asset) { return asset.url || asset.article_url || asset.material_url || ""; }
function normalizeAsset(asset) { return { ...asset, _sessionKey: assetKey(asset) }; }

function assetKind(asset) {
  if (asset.kind === "temporary") return ["临时托管", "temporary"];
  if (asset.article_url && asset.media_id) return ["两种位置", "article"];
  if (asset.article_url) return ["正文图片", "article"];
  if (asset.media_id) return ["永久素材", "material"];
  return ["上传失败", "error"];
}

function assetJson(asset) {
  return { filename: asset.filename ?? null, url: preferredUrl(asset) || null, kind: asset.kind ?? "wechat", media_id: asset.media_id ?? null,
    width: asset.width ?? null, height: asset.height ?? null, processed_bytes: asset.processed_bytes ?? asset.original_bytes ?? null,
    sha256: asset.sha256 ?? null, created_at: asset.created_at ?? null, expires_at: asset.expires_at ?? null };
}

function upsertAsset(asset) {
  if (!asset?.id) return;
  const next = normalizeAsset(asset);
  const index = state.assets.findIndex(item => item._sessionKey === next._sessionKey);
  if (index >= 0) state.assets.splice(index, 1);
  state.assets.unshift(next);
}

async function loadAssets({ silent = false } = {}) {
  if (!silent) { state.loadingAssets = true; renderAssets(); }
  try {
    const result = await api("/api/assets?limit=2000");
    state.assets = result.items.map(normalizeAsset);
    const available = new Set(state.assets.map(item => item._sessionKey));
    state.selected = new Set([...state.selected].filter(key => available.has(key)));
  } catch (error) { toast(`加载素材失败：${error.message}`); }
  finally { state.loadingAssets = false; renderAssets(); }
}

function addFiles(fileList) {
  if (state.uploading) return;
  const known = new Set(state.files.map(item => `${item.file.name}:${item.file.size}:${item.file.lastModified}`));
  for (const file of fileList) {
    const key = `${file.name}:${file.size}:${file.lastModified}`;
    if (!known.has(key)) { state.files.push({ file, state: "waiting", message: "等待上传" }); known.add(key); }
  }
  renderQueue();
}

function renderQueue() {
  const finished = state.files.filter(item => ["complete", "partial", "failed"].includes(item.state)).length;
  els.queueSummary.textContent = state.files.length ? `${state.files.length} 张 · ${formatBytes(state.files.reduce((sum, item) => sum + item.file.size, 0))}${finished ? ` · 已处理 ${finished}` : ""}` : "尚未选择图片";
  els.clearQueue.disabled = !state.files.length || state.uploading;
  els.startUpload.disabled = !state.files.length || state.uploading;
  els.startUpload.textContent = state.uploading ? "上传中" : "开始上传";
  els.queue.innerHTML = state.files.map(item => `<div class="queue-item"><div><strong>${escapeHtml(item.file.name)}</strong><small>${formatBytes(item.file.size)} · ${escapeHtml(item.message)}</small></div><span class="queue-state ${item.state}">${({ waiting: "等待", working: "上传中", complete: "成功", partial: "部分成功", failed: "失败" })[item.state]}</span></div>`).join("");
}

function renderAssets() {
  const busy = state.loadingAssets || state.deleting;
  const urls = state.assets.map(preferredUrl).filter(Boolean);
  els.assetCount.textContent = state.loadingAssets ? "加载中" : `${state.assets.length} 条`;
  els.copyUrls.disabled = !urls.length || busy;
  els.copyJson.disabled = !state.assets.length || busy;
  els.deleteSelected.disabled = !state.selected.size || busy;
  els.deleteSelected.textContent = state.deleting ? "删除中" : `删除选中${state.selected.size ? ` (${state.selected.size})` : ""}`;
  if (state.loadingAssets) { els.assetTable.innerHTML = '<p class="empty-state">正在加载素材...</p>'; return; }
  if (!state.assets.length) { els.assetTable.innerHTML = '<p class="empty-state">暂无素材记录</p>'; return; }
  const allSelected = state.assets.every(asset => state.selected.has(asset._sessionKey));
  els.assetTable.innerHTML = `<div class="asset-row table-head"><label><input class="select-all" type="checkbox" ${allSelected ? "checked" : ""}></label><span>图片</span><span>位置</span><span>URL</span><span>操作</span></div>${state.assets.map(asset => {
    const [label, className] = assetKind(asset);
    const url = preferredUrl(asset);
    const preview = url ? `<img src="${escapeHtml(url)}" alt="" loading="lazy" referrerpolicy="no-referrer">` : '<span>无预览</span>';
    return `<div class="asset-row" data-key="${escapeHtml(asset._sessionKey)}"><label><input class="asset-select" type="checkbox" data-key="${escapeHtml(asset._sessionKey)}" ${state.selected.has(asset._sessionKey) ? "checked" : ""}></label><div class="asset-photo"><div class="asset-thumb">${preview}</div><div><strong>${escapeHtml(asset.filename)}</strong><small>${asset.width || "-"} × ${asset.height || "-"} · ${formatBytes(asset.processed_bytes || asset.original_bytes)}</small></div></div><span><b class="kind-badge ${className}">${label}</b></span>${url ? `<a class="url-cell" href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(url)}</a>` : '<span class="muted-cell">暂无 URL</span>'}<div class="row-actions"><button class="button secondary copy-url" data-key="${escapeHtml(asset._sessionKey)}" type="button" ${url ? "" : "disabled"}>复制</button><button class="button danger-outline delete-one" data-key="${escapeHtml(asset._sessionKey)}" type="button">删除</button></div></div>`;
  }).join("")}`;
}

async function uploadOne(item, mode) {
  item.state = "working"; item.message = "正在传输"; renderQueue();
  const body = new FormData(); body.append("mode", mode); body.append("image", item.file, item.file.name);
  try {
    const result = await api("/api/upload", { method: "POST", body });
    item.state = result.status;
    item.message = result.cached ? "已存在，直接复用" : (result.errors?.join("；") || "上传完成");
    upsertAsset(result.asset); renderAssets();
  } catch (error) { item.state = "failed"; item.message = error.message; }
  renderQueue();
}

async function startUpload() {
  state.uploading = true; renderQueue();
  const mode = document.querySelector('input[name="mode"]:checked').value;
  const pending = state.files.filter(item => item.state === "waiting" || item.state === "failed");
  const workers = Array.from({ length: Math.min(3, pending.length) }, async () => { while (pending.length) await uploadOne(pending.shift(), mode); });
  await Promise.all(workers);
  state.uploading = false; renderQueue();
  await Promise.all([loadAssets({ silent: true }), loadOverview()]);
  toast("批次处理完成");
}

async function copyText(value, successMessage) {
  if (!value) return toast("没有可复制的内容");
  if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(value);
  else { const area = document.createElement("textarea"); area.value = value; area.style.position = "fixed"; area.style.opacity = "0"; document.body.append(area); area.select(); document.execCommand("copy"); area.remove(); }
  toast(successMessage);
}

function askConfirmation(title, message, confirmText = "确认") {
  els.confirmTitle.textContent = title; els.confirmMessage.textContent = message; els.confirmAction.textContent = confirmText;
  els.confirmDialog.returnValue = "cancel"; els.confirmDialog.showModal();
  return new Promise(resolve => els.confirmDialog.addEventListener("close", () => resolve(els.confirmDialog.returnValue === "confirm"), { once: true }));
}

async function deleteRecords(assets) {
  if (!assets.length) return;
  const permanentCount = assets.filter(asset => asset.media_id).length;
  const articleUrlCount = assets.filter(asset => asset.article_url).length;
  const details = [`将删除 ${assets.length} 条控制台记录。`];
  if (permanentCount) details.push(`其中 ${permanentCount} 条永久素材会同步从微信素材库删除。`);
  if (articleUrlCount) details.push(`其中 ${articleUrlCount} 个正文图片 URL 没有微信删除接口，原 URL 可能仍可访问。`);
  const confirmed = await askConfirmation("删除素材", details.join(""), "确认删除");
  if (!confirmed) return;
  state.deleting = true; renderAssets();
  try {
    const result = await api("/api/assets/delete", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ items: assets.map(asset => ({ kind: asset.kind || "wechat", id: asset.id })) }) });
    const warningCount = result.deleted.filter(item => item.warning).length;
    if (result.error_count) toast(`已删除 ${result.deleted_count} 条，${result.error_count} 条失败`);
    else if (warningCount) toast(`已删除 ${result.deleted_count} 条记录；${warningCount} 个正文图片 URL 无法从微信撤销`);
    else toast(`已删除 ${result.deleted_count} 条素材`);
    state.selected = new Set();
    await Promise.all([loadAssets(), loadOverview()]);
  } catch (error) { toast(error.message); }
  finally { state.deleting = false; renderAssets(); }
}

async function loadWorkspace() {
  state.loadingAssets = true; renderAssets();
  await loadAccounts();
  await Promise.all([checkStatus(), loadOverview(), loadAssets(), loadDrafts()]);
  if (!state.accounts.length) showView("account");
}

async function bootstrapAuth() {
  try { const result = await api("/api/auth/me"); await enterWorkspace(result.user); }
  catch (error) {
    if (error.status !== 401) toast(`登录状态检查失败：${error.message}`);
    try {
      const setup = await api("/api/setup/status");
      if (setup.configured) showLogin(); else showSetup();
    } catch (setupError) {
      toast(`初始化状态检查失败：${setupError.message}`);
      showLogin();
    }
  }
}

document.querySelectorAll(".nav-item").forEach(button => button.addEventListener("click", () => showView(button.dataset.view)));
document.querySelectorAll(".goto-view").forEach(button => button.addEventListener("click", () => showView(button.dataset.target)));
window.addEventListener("hashchange", () => showView(location.hash.slice(1), false));

els.loginForm.addEventListener("submit", async event => {
  event.preventDefault(); els.loginError.textContent = ""; els.loginSubmit.disabled = true; els.loginSubmit.textContent = "登录中";
  try {
    const result = await api("/api/auth/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username: els.loginUsername.value, password: els.loginPassword.value }) });
    await enterWorkspace(result.user);
  } catch (error) { els.loginError.textContent = error.message; }
  finally { els.loginSubmit.disabled = false; els.loginSubmit.textContent = "登录"; }
});

els.showRegister.addEventListener("click", showRegister);
els.showLogin.addEventListener("click", showLogin);
els.registerForm.addEventListener("submit", async event => {
  event.preventDefault();
  els.registerError.textContent = "";
  if (els.registerPassword.value !== els.registerPasswordConfirm.value) {
    els.registerError.textContent = "两次输入的密码不一致";
    return;
  }
  els.registerSubmit.disabled = true;
  els.registerSubmit.textContent = "正在注册";
  try {
    const result = await api("/api/auth/register", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: els.registerUsername.value, password: els.registerPassword.value }),
    });
    await enterWorkspace(result.user);
  } catch (error) { els.registerError.textContent = error.message; }
  finally { els.registerSubmit.disabled = false; els.registerSubmit.textContent = "注册并登录"; }
});

els.setupForm.addEventListener("submit", async event => {
  event.preventDefault();
  els.setupError.textContent = "";
  if (els.setupPassword.value !== els.setupPasswordConfirm.value) {
    els.setupError.textContent = "两次输入的密码不一致";
    return;
  }
  els.setupSubmit.disabled = true;
  els.setupSubmit.textContent = "正在创建";
  try {
    const result = await api("/api/setup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ setup_token: els.setupToken.value, username: els.setupUsername.value, password: els.setupPassword.value }),
    });
    await enterWorkspace(result.user);
    showView("account");
  } catch (error) { els.setupError.textContent = error.message; }
  finally { els.setupSubmit.disabled = false; els.setupSubmit.textContent = "创建管理员"; }
});

els.logoutButton.addEventListener("click", async () => { try { await api("/api/auth/logout", { method: "POST" }); } catch { /* clear locally */ } showLogin(); });
els.passwordForm.addEventListener("submit", async event => {
  event.preventDefault();
  els.passwordError.textContent = "";
  if (els.newPassword.value !== els.newPasswordConfirm.value) {
    els.passwordError.textContent = "两次输入的新密码不一致";
    return;
  }
  const username = state.user?.username || "";
  els.passwordSubmit.disabled = true;
  els.passwordSubmit.textContent = "正在修改";
  try {
    await api("/api/auth/password", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ current_password: els.currentPassword.value, new_password: els.newPassword.value }),
    });
    els.passwordForm.reset();
    showLogin();
    els.loginUsername.value = username;
    toast("密码已修改，请重新登录");
  } catch (error) { els.passwordError.textContent = error.message; }
  finally { els.passwordSubmit.disabled = false; els.passwordSubmit.textContent = "修改并重新登录"; }
});
els.accountForm.addEventListener("submit", saveAccount);
els.newAccount.addEventListener("click", () => {
  state.editingAccountId = null;
  renderAccount();
  els.accountName.focus();
});
els.accountSwitcher.addEventListener("change", event => activateAccount(Number(event.target.value)));
els.accountList.addEventListener("click", event => {
  const button = event.target.closest("button");
  if (!button) return;
  const account = state.accounts.find(item => item.id === Number(button.dataset.id));
  if (!account) return;
  if (button.classList.contains("activate-account")) activateAccount(account.id);
  if (button.classList.contains("edit-account")) {
    state.editingAccountId = account.id;
    renderAccount();
    els.accountName.focus();
  }
  if (button.classList.contains("delete-account")) deleteAccount(account);
});
els.accountTest.addEventListener("click", testAccount);
els.testConnection.addEventListener("click", testAccount);
els.refreshDrafts.addEventListener("click", loadDrafts);
els.loadMoreDrafts.addEventListener("click", () => loadDrafts({ append: true }));
els.runDiagnostics.addEventListener("click", runDiagnostics);
els.generatePairingCode.addEventListener("click", generatePairingCode);
els.copyPairingCode.addEventListener("click", () => copyText(state.pairingCode, "验证码已复制"));
els.draftTable.addEventListener("click", event => { const button = event.target.closest(".delete-draft"); if (button) { const draft = state.drafts.find(item => item.id === Number(button.dataset.id)); if (draft) deleteDraftRecord(draft); } });

els.dropZone.addEventListener("click", () => els.input.click());
els.input.addEventListener("change", event => { addFiles(event.target.files); event.target.value = ""; });
for (const name of ["dragenter", "dragover"]) els.dropZone.addEventListener(name, event => { event.preventDefault(); els.dropZone.classList.add("dragging"); });
for (const name of ["dragleave", "drop"]) els.dropZone.addEventListener(name, event => { event.preventDefault(); els.dropZone.classList.remove("dragging"); });
els.dropZone.addEventListener("drop", event => addFiles(event.dataTransfer.files));
els.clearQueue.addEventListener("click", () => { state.files = []; renderQueue(); });
els.startUpload.addEventListener("click", startUpload);
els.copyUrls.addEventListener("click", () => copyText(state.assets.map(preferredUrl).filter(Boolean).join("\n"), "URL 已复制"));
els.copyJson.addEventListener("click", () => copyText(JSON.stringify(state.assets.map(assetJson), null, 2), "JSON 已复制"));
els.deleteSelected.addEventListener("click", () => deleteRecords(state.assets.filter(asset => state.selected.has(asset._sessionKey))));

els.assetTable.addEventListener("change", event => {
  if (event.target.classList.contains("select-all")) state.selected = event.target.checked ? new Set(state.assets.map(asset => asset._sessionKey)) : new Set();
  if (event.target.classList.contains("asset-select")) { if (event.target.checked) state.selected.add(event.target.dataset.key); else state.selected.delete(event.target.dataset.key); }
  renderAssets();
});

els.assetTable.addEventListener("click", event => {
  const button = event.target.closest("button"); if (!button) return;
  const asset = state.assets.find(item => item._sessionKey === button.dataset.key); if (!asset) return;
  if (button.classList.contains("copy-url")) copyText(preferredUrl(asset), "URL 已复制");
  if (button.classList.contains("delete-one")) deleteRecords([asset]);
});

els.assetTable.addEventListener("error", event => { if (event.target.tagName === "IMG") event.target.closest(".asset-thumb")?.classList.add("broken"); }, true);
renderPairingCode();
bootstrapAuth();
