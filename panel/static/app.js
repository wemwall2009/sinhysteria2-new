"use strict";

const $ = (id) => document.getElementById(id);
const state = { clients: [], protocols: [], csrf: "", selectedLink: "" };

function toast(message) {
  $("toast").textContent = message;
  $("toast").classList.remove("hidden");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => $("toast").classList.add("hidden"), 2600);
}

async function request(path, options = {}) {
  const headers = Object.assign({"Content-Type": "application/json"}, options.headers || {});
  if (state.csrf && options.method && options.method !== "GET") headers["X-CSRF-Token"] = state.csrf;
  const response = await fetch(path, Object.assign({credentials: "same-origin", headers}, options));
  let data = {};
  if ((response.headers.get("content-type") || "").includes("json")) data = await response.json();
  if (!response.ok) {
    const error = new Error(data.error || `请求失败 (${response.status})`);
    error.status = response.status;
    throw error;
  }
  return data;
}

function bytes(value) {
  if (!value) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const level = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
  return `${(value / 1024 ** level).toFixed(level > 1 ? 2 : 0)} ${units[level]}`;
}

function remainingTime(expiresAt) {
  if (!expiresAt) return "∞";
  const seconds = expiresAt - Math.floor(Date.now() / 1000);
  if (seconds <= 0) return "已到期";
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  return days ? `${days}d ${hours}h` : `${hours}h`;
}

function escapeHtml(value) {
  return String(value).replace(/[&<>'"]/g, (character) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"})[character]);
}

function usageCell(client) {
  const percent = client.quota_bytes ? Math.min(client.used_bytes / client.quota_bytes * 100, 100) : 58;
  const total = client.quota_bytes ? bytes(client.quota_bytes) : "∞";
  return `<div class="usage"><span class="usage-text">${bytes(client.used_bytes)}</span><div class="progress"><span style="width:${percent}%"></span></div><span>${total}</span></div>`;
}

function statusLabel(client) {
  if (client.status === "expired") return '<span class="badge badge-red">已到期</span>';
  if (client.status === "exhausted") return '<span class="badge badge-red">已耗尽</span>';
  if (client.status === "disabled") return '<span class="badge">已关闭</span>';
  return '<span class="badge badge-green">可用</span>';
}

function render() {
  let clients = [...state.clients];
  const query = $("searchInput").value.toLowerCase().trim();
  const filter = $("statusFilter").value;
  if (query) clients = clients.filter((client) => `${client.name} ${client.protocol} ${client.port}`.toLowerCase().includes(query));
  if (filter) clients = clients.filter((client) => client.status === filter);
  const sort = $("sortSelect").value;
  if (sort === "usage") clients.sort((a, b) => b.used_bytes - a.used_bytes);
  if (sort === "expiry") clients.sort((a, b) => (a.expires_at || Number.MAX_SAFE_INTEGER) - (b.expires_at || Number.MAX_SAFE_INTEGER));
  if (sort === "newest") clients.sort((a, b) => b.id - a.id);

  $("clientRows").innerHTML = clients.map((client) => `<tr>
    <td><div class="actions"><button class="icon-btn" data-action="qr" data-id="${client.id}" title="二维码">▦</button><button class="icon-btn" data-action="link" data-id="${client.id}" title="复制链接">⧉</button><button class="icon-btn" data-action="reset" data-id="${client.id}" title="重置流量">↺</button><button class="icon-btn" data-action="edit" data-id="${client.id}" title="编辑">✎</button><button class="icon-btn danger" data-action="delete" data-id="${client.id}" title="删除">♲</button></div></td>
    <td><label class="switch"><input type="checkbox" data-action="toggle" data-id="${client.id}" ${client.enabled ? "checked" : ""}><span class="slider"></span></label></td>
    <td>${client.online ? '<span class="badge badge-blue">在线</span>' : '<span class="badge">离线</span>'}</td>
    <td><div class="client-name">${escapeHtml(client.name)}</div><div class="client-id">ID ${client.id}</div></td>
    <td><span class="badge badge-blue">${client.protocol === "reality" ? "VLESS Reality" : "Hysteria2"}</span> <span class="subtle">:${client.port}</span></td>
    <td>${usageCell(client)}</td><td>${client.speed ? bytes(client.speed) + "/s" : "—"}</td>
    <td>${client.quota_bytes ? `<span class="badge badge-green">${bytes(Math.max(client.quota_bytes-client.used_bytes,0))}</span>` : '<span class="badge badge-purple">∞</span>'}</td>
    <td>${statusLabel(client)} <span class="badge badge-blue">${remainingTime(client.expires_at)}</span></td>
  </tr>`).join("") || '<tr><td colspan="9" style="text-align:center;color:var(--muted);padding:34px">暂无客户，点击“添加客户”创建第一个节点</td></tr>';

  const total = state.clients.length;
  $("statTotal").textContent = total;
  $("statOnline").textContent = state.clients.filter((item) => item.online).length;
  $("statExhausted").textContent = state.clients.filter((item) => ["exhausted", "expired"].includes(item.status)).length;
  $("statLow").textContent = state.clients.filter((item) => item.quota_bytes && item.used_bytes / item.quota_bytes >= .8 && item.status === "enabled").length;
  $("statDisabled").textContent = state.clients.filter((item) => item.status === "disabled").length;
  $("statEnabled").textContent = state.clients.filter((item) => item.status === "enabled").length;
}

async function loadState(showError = true) {
  try {
    const data = await request("/api/state");
    Object.assign(state, data);
    $("loginView").classList.add("hidden");
    $("appView").classList.remove("hidden");
    $("accountingNotice").textContent = data.accounting_error ? `流量统计异常：${data.accounting_error}` : "";
    $("accountingNotice").classList.toggle("hidden", !data.accounting_error);
    render();
  } catch (error) {
    if (error.status === 401) {
      $("appView").classList.add("hidden");
      $("loginView").classList.remove("hidden");
    } else if (showError) toast(error.message);
  }
}

function openCreate() {
  $("clientModalTitle").textContent = "添加客户";
  $("clientId").value = "";
  $("clientName").value = "";
  $("clientQuota").value = "100";
  $("clientDuration").value = "30";
  $("protocolField").classList.remove("hidden");
  $("durationField").classList.remove("hidden");
  $("expiryField").classList.add("hidden");
  $("clientProtocol").innerHTML = state.protocols.map((protocol) => `<option value="${protocol}">${protocol === "reality" ? "VLESS Reality" : "Hysteria2"}</option>`).join("");
  $("clientModal").classList.remove("hidden");
  $("clientName").focus();
}

function openEdit(client) {
  $("clientModalTitle").textContent = "编辑客户";
  $("clientId").value = client.id;
  $("clientName").value = client.name;
  $("clientQuota").value = client.quota_bytes ? (client.quota_bytes / 1024 ** 3).toFixed(2) : "0";
  $("clientExpiry").value = client.expires_at ? new Date(client.expires_at * 1000 - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 16) : "";
  $("protocolField").classList.add("hidden");
  $("durationField").classList.add("hidden");
  $("expiryField").classList.remove("hidden");
  $("clientModal").classList.remove("hidden");
}

async function showQr(clientId) {
  const data = await request(`/api/clients/${clientId}/link`);
  state.selectedLink = data.link;
  $("qrImage").src = `/api/clients/${clientId}/qr?t=${Date.now()}`;
  $("qrLink").textContent = data.link;
  $("qrModal").classList.remove("hidden");
}

async function act(action, clientId, target) {
  const client = state.clients.find((item) => item.id === clientId);
  if (!client) return;
  try {
    if (action === "qr") return showQr(clientId);
    if (action === "link") {
      const data = await request(`/api/clients/${clientId}/link`);
      await navigator.clipboard.writeText(data.link);
      return toast("节点链接已复制");
    }
    if (action === "edit") return openEdit(client);
    if (action === "toggle") await request(`/api/clients/${clientId}`, {method:"PUT", body:JSON.stringify({enabled:target.checked})});
    if (action === "reset" && confirm(`确定重置 ${client.name} 的流量吗？`)) await request(`/api/clients/${clientId}/reset`, {method:"POST", body:"{}"});
    if (action === "delete" && confirm(`确定删除 ${client.name} 及其节点吗？`)) await request(`/api/clients/${clientId}`, {method:"DELETE"});
    await loadState();
  } catch (error) { toast(error.message); await loadState(false); }
}

$("loginForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await request("/api/login", {method:"POST", body:JSON.stringify({username:$("loginUsername").value,password:$("loginPassword").value})});
    $("loginPassword").value = "";
    await loadState();
  } catch (error) { toast(error.message); }
});

$("clientForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const clientId = Number($("clientId").value || 0);
  try {
    if (clientId) {
      const expiryValue = $("clientExpiry").value;
      await request(`/api/clients/${clientId}`, {method:"PUT", body:JSON.stringify({name:$("clientName").value,quota_gb:Number($("clientQuota").value),expires_at:expiryValue ? Math.floor(new Date(expiryValue).getTime()/1000) : 0})});
    } else {
      await request("/api/clients", {method:"POST", body:JSON.stringify({name:$("clientName").value,protocol:$("clientProtocol").value,quota_gb:Number($("clientQuota").value),duration_days:Number($("clientDuration").value)})});
    }
    $("clientModal").classList.add("hidden");
    toast(clientId ? "客户已更新" : "客户与节点已创建");
    await loadState();
  } catch (error) { toast(error.message); }
});

$("clientRows").addEventListener("click", (event) => {
  const element = event.target.closest("[data-action]");
  if (element && element.dataset.action !== "toggle") act(element.dataset.action, Number(element.dataset.id), element);
});
$("clientRows").addEventListener("change", (event) => {
  if (event.target.dataset.action === "toggle") act("toggle", Number(event.target.dataset.id), event.target);
});
$("addBtn").addEventListener("click", openCreate);
$("refreshBtn").addEventListener("click", () => loadState());
$("clientCancel").addEventListener("click", () => $("clientModal").classList.add("hidden"));
$("qrClose").addEventListener("click", () => $("qrModal").classList.add("hidden"));
$("copyLinkBtn").addEventListener("click", async () => { await navigator.clipboard.writeText(state.selectedLink); toast("节点链接已复制"); });
$("passwordBtn").addEventListener("click", () => { $("passwordForm").reset(); $("passwordModal").classList.remove("hidden"); $("currentPassword").focus(); });
$("passwordCancel").addEventListener("click", () => $("passwordModal").classList.add("hidden"));
$("passwordForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  if ($("newPassword").value !== $("confirmPassword").value) return toast("两次新密码不一致");
  try {
    await request("/api/change-password", {method:"POST", body:JSON.stringify({current_password:$("currentPassword").value,new_password:$("newPassword").value})});
    $("passwordModal").classList.add("hidden"); state.csrf = ""; toast("密码已修改，请使用新密码重新登录"); await loadState(false);
  } catch (error) { toast(error.message); }
});
$("searchInput").addEventListener("input", render);
$("statusFilter").addEventListener("change", render);
$("sortSelect").addEventListener("change", render);
$("logoutBtn").addEventListener("click", async () => { await request("/api/logout", {method:"POST", body:"{}"}); state.csrf=""; await loadState(false); });
window.addEventListener("keydown", (event) => { if (event.key === "Escape") { $("clientModal").classList.add("hidden"); $("qrModal").classList.add("hidden"); $("passwordModal").classList.add("hidden"); } });

loadState(false);
setInterval(() => { if (!$("appView").classList.contains("hidden")) loadState(false); }, 10000);
