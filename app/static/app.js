const state = {
  user: null,
  status: null,
  account: null,
  overview: null,
  drafts: [],
  files: [],
  assets: [],
  selected: new Set(),
  uploading: false,
  deleting: false,
  loadingAssets: true,
};

const views = {
  overview: ["CONTROL CENTER", "运行总览"],
  account: ["OFFICIAL ACCOUNT", "公众号设置"],
  assets: ["MATERIAL LIBRARY", "素材库"],
  drafts: ["DRAFT OPERATIONS", "草稿箱"],
  api: ["API ACCESS", "API 接入"],
};

const $ = selector => document.querySelector(selector);
const els = {
  authView: $("#authView"), appShell: $("#appShell"), loginForm: $("#loginForm"),
  loginUsername: $("#loginUsername"), loginPassword: $("#loginPassword"),
  loginError: $("#loginError"), loginSubmit: $("#loginSubmit"), currentUsername: $("#currentUsername"),
  logoutButton: $("#logoutButton"), viewKicker: $("#viewKicker"), viewTitle: $("#viewTitle"),
  connection: $("#connectionState"), testConnection: $("#testConnection"),
  sidebarAccountMark: $("#sidebarAccountMark"), sidebarAccountName: $("#sidebarAccountName"), sidebarAccountId: $("#sidebarAccountId"),
  metricConnection: $("#metricConnection"), metricAccount: $("#metricAccount"), metricAssets: $("#metricAssets"),
  metricDrafts: $("#metricDrafts"), metricDraftFailures: $("#metricDraftFailures"), metricTemporary: $("#metricTemporary"),
  overviewApis: $("#overviewApis"), recentDrafts: $("#recentDrafts"),
  accountForm: $("#accountForm"), accountName: $("#accountName"), accountType: $("#accountType"),
  accountAppId: $("#accountAppId"), accountSecret: $("#accountSecret"), accountSource: $("#accountSource"),
  accountMeta: $("#accountMeta"), accountSave: $("#accountSave"), accountTest: $("#accountTest"),
  input: $("#fileInput"), dropZone: $("#dropZone"), queue: $("#queue"), queueSummary: $("#queueSummary"),
  clearQueue: $("#clearQueue"), startUpload: $("#startUpload"), assetTable: $("#assetTable"),
  assetCount: $("#assetCount"), copyUrls: $("#copyUrls"), copyJson: $("#copyJson"), deleteSelected: $("#deleteSelected"),
  draftTable: $("#draftTable"), draftCount: $("#draftCount"), refreshDrafts: $("#refreshDrafts"),
  apiEndpoints: $("#apiEndpoints"), runDiagnostics: $("#runDiagnostics"), diagnosticResults: $("#diagnosticResults"),
  confirmDialog: $("#confirmDialog"), confirmTitle: $("#confirmTitle"),
  confirmMessage: $("#confirmMessage"), confirmAction: $("#confirmAction"), toast: $("#toast"),
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

function showLogin() {
  state.user = null;
  state.files = [];
  state.assets = [];
  state.selected = new Set();
  els.appShell.hidden = true;
  els.authView.hidden = false;
  renderQueue();
  renderAssets();
  setTimeout(() => els.loginUsername.focus(), 0);
}

function showWorkspace(user) {
  state.user = user;
  els.currentUsername.textContent = user.username;
  els.authView.hidden = true;
  els.appShell.hidden = false;
  els.loginError.textContent = "";
  els.loginForm.reset();
  showView(location.hash.slice(1) || "overview", false);
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
    capability("图片接口", data.apis.images, data.apis.images ? "AI_API_KEY 已启用" : "AI_API_KEY 未配置"),
    capability("草稿接口", data.apis.drafts, data.apis.drafts ? "PUBLISH_API_KEY 已启用" : "PUBLISH_API_KEY 未配置"),
    capability("临时图片", data.apis.temporary, data.apis.temporary ? "TEMP_API_KEY 已启用" : "TEMP_API_KEY 未配置"),
  ].join("");
  els.recentDrafts.innerHTML = data.recent_drafts.length ? data.recent_drafts.map(draft => {
    const [label, className] = draftStatus(draft.status);
    return `<div class="compact-row"><div><strong>${escapeHtml(draft.title)}</strong><small>${formatDate(draft.updated_at)} · ${escapeHtml(draft.request_id)}</small></div><span class="status-pill ${className}">${label}</span></div>`;
  }).join("") : '<p class="empty-state">暂无草稿记录</p>';
}

async function loadOverview() {
  try {
    state.overview = await api("/api/overview");
    renderOverview();
  } catch (error) { toast(`加载总览失败：${error.message}`); }
}

function renderAccount() {
  const account = state.account;
  if (!account) return;
  els.accountName.value = account.display_name || "";
  els.accountType.value = account.account_type || "subscription";
  els.accountAppId.value = account.app_id || "";
  els.accountSecret.value = "";
  els.accountSource.textContent = ({ console: "控制台配置", environment: "环境变量", none: "未配置" })[account.source] || account.source;
  const secret = account.secret_configured ? "AppSecret 已保存" : "AppSecret 未配置";
  const encryption = account.encryption === "environment" ? "环境主密钥" : "本机密钥文件";
  els.accountMeta.textContent = `${secret} · ${encryption}${account.updated_at ? ` · ${formatDate(account.updated_at)}` : ""}`;
  els.accountTest.disabled = !account.secret_configured || !account.app_id;
}

async function loadAccount() {
  try {
    const result = await api("/api/account");
    state.account = result.account;
    updateAccountIdentity(state.account);
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
    const result = await api("/api/account", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    state.account = result.account;
    renderAccount();
    updateAccountIdentity(state.account);
    await Promise.all([checkStatus(), loadOverview()]);
    toast("公众号配置已保存");
  } catch (error) { toast(error.message); }
  finally { els.accountSave.disabled = false; els.accountSave.textContent = "保存配置"; }
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

async function loadDrafts() {
  els.draftTable.innerHTML = '<p class="empty-state">正在加载草稿记录...</p>';
  try {
    const result = await api("/api/drafts?limit=500");
    state.drafts = result.items;
    renderDrafts();
  } catch (error) { els.draftTable.innerHTML = `<p class="empty-state error-text">${escapeHtml(error.message)}</p>`; }
}

function renderDrafts() {
  els.draftCount.textContent = `${state.drafts.length} 条`;
  if (!state.drafts.length) {
    els.draftTable.innerHTML = '<p class="empty-state">暂无草稿任务</p>';
    return;
  }
  els.draftTable.innerHTML = `<div class="draft-row table-head"><span>文章</span><span>请求标识</span><span>状态</span><span>更新时间</span><span>操作</span></div>${state.drafts.map(draft => {
    const [label, className] = draftStatus(draft.status);
    const error = draft.last_error ? `<small class="error-text">${escapeHtml(draft.last_error)}</small>` : `<small>${draft.content_characters} 字符</small>`;
    return `<div class="draft-row"><div><strong>${escapeHtml(draft.title)}</strong>${error}</div><code>${escapeHtml(draft.request_id)}</code><span><b class="status-pill ${className}">${label}</b></span><span>${formatDate(draft.updated_at)}</span><div>${draft.status === "created" ? `<button class="button danger-outline delete-draft" data-id="${draft.id}" type="button">删除</button>` : "-"}</div></div>`;
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
    ["POST", "/api/v1/wechat-images", "图片上传", state.status.image_api_ready, "AI_API_KEY"],
    ["POST", "/api/v1/wechat-drafts", "写入草稿", state.status.publish_api_ready, "PUBLISH_API_KEY"],
    ["POST", "/api/v1/temp-images", "临时图片", state.status.temporary_api_ready, "TEMP_API_KEY"],
  ];
  els.apiEndpoints.innerHTML = endpoints.map(([method, path, name, ready, key]) => `<div class="endpoint"><span class="method">${method}</span><code>${path}</code><strong>${name}</strong><span class="endpoint-key">${key}</span><b class="status-pill ${ready ? "ok" : "muted"}">${ready ? "已启用" : "未配置"}</b></div>`).join("");
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
  const confirmed = await askConfirmation("删除素材", `将删除 ${assets.length} 条记录，永久素材会同步从微信素材库删除。`, "确认删除");
  if (!confirmed) return;
  state.deleting = true; renderAssets();
  try {
    const result = await api("/api/assets/delete", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ items: assets.map(asset => ({ kind: asset.kind || "wechat", id: asset.id })) }) });
    if (result.error_count) toast(`已删除 ${result.deleted_count} 条，${result.error_count} 条失败`); else toast(`已删除 ${result.deleted_count} 条素材`);
    state.selected = new Set();
    await Promise.all([loadAssets(), loadOverview()]);
  } catch (error) { toast(error.message); }
  finally { state.deleting = false; renderAssets(); }
}

async function loadWorkspace() {
  state.loadingAssets = true; renderAssets();
  await Promise.all([checkStatus(), loadOverview(), loadAccount(), loadAssets(), loadDrafts()]);
}

async function bootstrapAuth() {
  try { const result = await api("/api/auth/me"); showWorkspace(result.user); await loadWorkspace(); }
  catch (error) { if (error.status !== 401) toast(`登录状态检查失败：${error.message}`); showLogin(); }
}

document.querySelectorAll(".nav-item").forEach(button => button.addEventListener("click", () => showView(button.dataset.view)));
document.querySelectorAll(".goto-view").forEach(button => button.addEventListener("click", () => showView(button.dataset.target)));
window.addEventListener("hashchange", () => showView(location.hash.slice(1), false));

els.loginForm.addEventListener("submit", async event => {
  event.preventDefault(); els.loginError.textContent = ""; els.loginSubmit.disabled = true; els.loginSubmit.textContent = "登录中";
  try {
    const result = await api("/api/auth/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username: els.loginUsername.value, password: els.loginPassword.value }) });
    showWorkspace(result.user); await loadWorkspace();
  } catch (error) { els.loginError.textContent = error.message; }
  finally { els.loginSubmit.disabled = false; els.loginSubmit.textContent = "登录"; }
});

els.logoutButton.addEventListener("click", async () => { try { await api("/api/auth/logout", { method: "POST" }); } catch { /* clear locally */ } showLogin(); });
els.accountForm.addEventListener("submit", saveAccount);
els.accountTest.addEventListener("click", testAccount);
els.testConnection.addEventListener("click", testAccount);
els.refreshDrafts.addEventListener("click", loadDrafts);
els.runDiagnostics.addEventListener("click", runDiagnostics);
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
bootstrapAuth();
