const state = {
  token: localStorage.getItem("prompt-lens-admin-token") || "",
  view: "users",
  users: [],
  jobs: [],
  tasks: [],
  feedback: [],
  taskTotal: 0,
  taskOffset: 0,
  taskLimit: 50,
};

const $ = (id) => document.getElementById(id);
const labels = { processing: ["处理中", "warn"], succeeded: ["已完成", "ok"], failed: ["已失败", "bad"] };
const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]);
const formatDate = (value) => value ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "-";
const formatDuration = (seconds) => {
  const value = Number(seconds) || 0;
  if (value < 60) return `${Math.round(value)} 秒`;
  if (value < 3600) return `${Math.round(value / 60)} 分钟`;
  return `${(value / 3600).toFixed(1)} 小时`;
};

function toast(message) {
  const node = $("toast");
  node.textContent = message;
  node.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => node.classList.remove("show"), 2600);
}

async function request(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", "X-Admin-Token": state.token, ...(options.headers || {}) },
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(data.detail || "请求失败");
    error.status = response.status;
    throw error;
  }
  return data;
}

function renderMetrics(overview, analytics) {
  const items = [
    ["用户总数", overview.users, `今日新增 ${analytics.users.new_today}`],
    ["今日活跃", analytics.users.active_today, `昨日新增 ${analytics.users.new_yesterday}`],
    ["今日任务", analytics.today.total, `处理中 ${analytics.today.processing}`],
    ["今日成功率", `${analytics.today.success_rate}%`, `失败 ${analytics.today.failed}`],
    ["平均处理耗时", formatDuration(analytics.today.avg_duration_seconds), "已结束任务"],
    ["今日算力消耗", analytics.credits.consumed, `退款 ${analytics.credits.refunded}`],
  ];
  $("metrics").innerHTML = items.map(([label, value, detail]) => `<div class="metric"><label>${escapeHtml(label)}</label><strong>${escapeHtml(value)}</strong><small>${escapeHtml(detail)}</small></div>`).join("");
}

function renderRateList(targetId, items) {
  const target = $(targetId);
  if (!items.length) {
    target.innerHTML = '<div class="empty-state">暂无数据</div>';
    return;
  }
  target.innerHTML = items.map((item) => {
    const rate = Math.max(0, Math.min(100, Number(item.success_rate) || 0));
    return `<div class="stat-row"><span>${escapeHtml(item.label)}</span><div class="progress ${rate < 70 ? "bad" : ""}"><span style="width:${rate}%"></span></div><strong>${rate}% · ${item.total}</strong></div>`;
  }).join("");
}

function renderAnalytics(data) {
  $("analyticsPanel").hidden = false;
  $("analyticsGenerated").textContent = `更新于 ${formatDate(data.generated_at)}`;
  const maxTotal = Math.max(1, ...data.trend.map((item) => item.total));
  $("trendChart").innerHTML = data.trend.map((item) => {
    const successHeight = Math.max(item.succeeded ? 4 : 2, item.succeeded * 100 / maxTotal);
    const failedHeight = Math.max(item.failed ? 4 : 2, item.failed * 100 / maxTotal);
    return `<div class="trend-day" title="${escapeHtml(item.day)}：成功 ${item.succeeded}，失败 ${item.failed}"><div class="trend-bars"><span class="trend-bar" style="height:${successHeight}%"></span><span class="trend-bar failed" style="height:${failedHeight}%"></span></div><span class="trend-label">${escapeHtml(item.day.slice(5))}<br>${item.total}</span></div>`;
  }).join("");
  renderRateList("taskTypeStats", data.task_types);
  renderRateList("platformStats", data.platforms);
  const maxFailure = Math.max(1, ...data.failures.map((item) => item.count));
  $("failureStats").innerHTML = data.failures.length
    ? data.failures.map((item) => `<div class="stat-row"><span>${escapeHtml(item.category)}</span><div class="progress bad"><span style="width:${item.count * 100 / maxFailure}%"></span></div><strong>${item.count}</strong></div>`).join("")
    : '<div class="empty-state">近 7 日无失败任务</div>';
  const alertCount = $("alertCount");
  alertCount.textContent = data.alerts.length ? `${data.alerts.length} 项异常` : "运行正常";
  alertCount.className = `status ${data.alerts.some((item) => item.severity === "critical") ? "bad" : data.alerts.length ? "warn" : "ok"}`;
  $("alerts").innerHTML = data.alerts.length
    ? data.alerts.map((item) => `<div class="alert-item ${item.severity === "critical" ? "critical" : ""}"><strong>${escapeHtml(item.title)}</strong><p>${escapeHtml(item.detail)}</p></div>`).join("")
    : '<div class="alert-clear">当前未检测到异常</div>';
}

function statusTag(status) {
  const value = labels[status] || [status, "warn"];
  return `<span class="status ${value[1]}">${escapeHtml(value[0])}</span>`;
}

function renderTable() {
  $("dataTable").hidden = false;
  $("tableState").hidden = true;
  if (state.view === "users") {
    $("tableHead").innerHTML = "<tr><th>ID</th><th>OpenID</th><th>算力</th><th>状态</th><th>注册时间</th><th></th></tr>";
    $("tableBody").innerHTML = state.users.map((user) => `<tr><td>#${user.id}</td><td><code>${escapeHtml(user.openid)}</code></td><td><strong>${user.credits}</strong></td><td>${user.is_blocked ? '<span class="status bad">已封禁</span>' : '<span class="status ok">正常</span>'}</td><td>${formatDate(user.created_at)}</td><td><button class="action" data-user="${user.id}">查看</button></td></tr>`).join("") || '<tr><td colspan="6">暂无用户</td></tr>';
    return;
  }
  if (state.view === "operations") {
    $("tableHead").innerHTML = "<tr><th>ID</th><th>类型</th><th>用户</th><th>来源</th><th>状态</th><th>算力</th><th>时间</th><th></th></tr>";
    $("tableBody").innerHTML = state.tasks.map((task) => `<tr><td>#${task.id}</td><td><strong>${escapeHtml(task.label)}</strong><small class="table-sub">${escapeHtml(task.task_detail || "")}</small></td><td><code>${escapeHtml(task.openid)}</code></td><td>${escapeHtml(task.source_platform || task.source_type || "-")}</td><td>${statusTag(task.status)}</td><td>${task.cost}</td><td>${formatDate(task.created_at)}<small class="table-sub">${task.duration_seconds == null ? "处理中" : formatDuration(task.duration_seconds)}</small></td><td><button class="action" data-task-type="${escapeHtml(task.task_type)}" data-task-id="${task.id}">详情</button></td></tr>`).join("") || '<tr><td colspan="8">暂无任务</td></tr>';
    return;
  }
  if (state.view === "feedback") {
    $("tableHead").innerHTML = "<tr><th>ID</th><th>用户</th><th>关联任务</th><th>分类</th><th>内容</th><th>状态</th><th>时间</th><th></th></tr>";
    $("tableBody").innerHTML = state.feedback.map((ticket) => "<tr><td>#" + ticket.id + "</td><td><code>" + escapeHtml(ticket.openid) + "</code></td><td>" + escapeHtml(ticket.task_type) + " #" + ticket.task_id + "</td><td>" + escapeHtml(ticket.category) + "</td><td>" + escapeHtml(ticket.content) + "</td><td>" + feedbackStatusTag(ticket.status) + "</td><td>" + formatDate(ticket.created_at) + "</td><td><button class=\"action\" data-feedback=\"" + ticket.id + "\">处理</button></td></tr>").join("") || "<tr><td colspan=\"8\">暂无工单</td></tr>";
    return;
  }
  $("tableHead").innerHTML = "<tr><th>ID</th><th>用户</th><th>媒体</th><th>状态</th><th>算力</th><th>时间</th><th></th></tr>";
  $("tableBody").innerHTML = state.jobs.map((job) => `<tr><td>#${job.id}</td><td><code>${escapeHtml(job.openid)}</code></td><td>${job.mode === "video" ? "视频" : "图片"}</td><td>${statusTag(job.status)}</td><td>${job.cost}</td><td>${formatDate(job.created_at)}</td><td>${job.status === "succeeded" ? `<button class="action" data-refund="${job.id}">退款</button>` : ""}</td></tr>`).join("") || '<tr><td colspan="7">暂无任务</td></tr>';
}

async function load() {
  try {
    $("refreshButton").disabled = true;
    const listRequest = state.view === "users"
      ? request(`/api/admin/users?query=${encodeURIComponent($("searchInput").value)}`)
      : state.view === "operations"
        ? request(`/api/admin/operations/tasks?status=${encodeURIComponent($("statusSelect").value)}&task_type=${encodeURIComponent($("taskTypeSelect").value)}&query=${encodeURIComponent($("searchInput").value)}&created_after=${encodeURIComponent(toUtcIso($("createdAfter").value))}&created_before=${encodeURIComponent(toUtcIso($("createdBefore").value))}&failure=${encodeURIComponent($("failureInput").value)}&limit=${state.taskLimit}&offset=${state.taskOffset}`)
        : state.view === "feedback"
          ? request("/api/admin/feedback?status=" + encodeURIComponent($("statusSelect").value) + "&query=" + encodeURIComponent($("searchInput").value) + "&limit=" + state.taskLimit + "&offset=" + state.taskOffset)
        : request(`/api/admin/jobs?status=${encodeURIComponent($("statusSelect").value)}`);
    const [overview, analytics, list] = await Promise.all([request("/api/admin/overview"), request("/api/admin/analytics"), listRequest]);
    renderMetrics(overview, analytics);
    renderAnalytics(analytics);
    if (state.view === "users") state.users = list;
    else if (state.view === "operations") { state.tasks = list.items || []; state.taskTotal = list.total || 0; renderPagination(); }
    else if (state.view === "feedback") { state.feedback = list.items || []; state.taskTotal = list.total || 0; renderPagination(); }
    else state.jobs = list;
    renderTable();
  } catch (error) {
    if (error.status === 401) return showLogin();
    toast(error.message);
  } finally {
    $("refreshButton").disabled = false;
  }
}

function renderPagination() {
  const visible = state.view === "operations" || state.view === "feedback";
  $("pagination").hidden = !visible;
  if (!visible) return;
  const start = state.taskTotal ? state.taskOffset + 1 : 0;
  const end = Math.min(state.taskOffset + state.taskLimit, state.taskTotal);
  $("pageInfo").textContent = `第 ${start}-${end} 条，共 ${state.taskTotal} 条`;
  $("prevPage").disabled = state.taskOffset === 0;
  $("nextPage").disabled = state.taskOffset + state.taskLimit >= state.taskTotal;
}

function toUtcIso(value) {
  return value ? new Date(value).toISOString() : "";
}

async function loadConfigStatus() {
  try {
    const config = await request("/api/admin/config");
    const missing = [];
    if (!config.openai_configured) missing.push("OPENAI_API_KEY");
    if (!config.wechat_configured) missing.push("WX_APP_ID / WX_APP_SECRET");
    if (!config.ad_configured) missing.push("WX_AD_UNIT_ID");
    const notice = $("configNotice");
    if (config.environment === "development" || missing.length) {
      notice.hidden = false;
      notice.innerHTML = `<strong>部署状态：</strong>${config.environment === "development" ? "当前仍是开发环境；" : "生产环境；"}${missing.length ? `待配置 ${escapeHtml(missing.join("、"))}。` : "核心配置已就绪。"}`;
    } else {
      notice.hidden = true;
    }
  } catch (error) {
    if (error.status === 401) showLogin();
  }
}

function showLogin() {
  $("loginView").hidden = false;
  $("appView").hidden = true;
  localStorage.removeItem("prompt-lens-admin-token");
  state.token = "";
}

function showApp() {
  $("loginView").hidden = true;
  $("appView").hidden = false;
  loadConfigStatus();
  load();
}

async function login(event) {
  event.preventDefault();
  $("loginError").textContent = "";
  try {
    const data = await request("/api/admin/login", { method: "POST", body: JSON.stringify({ username: $("username").value, password: $("password").value }), headers: { "X-Admin-Token": "" } });
    state.token = data.token;
    localStorage.setItem("prompt-lens-admin-token", state.token);
    showApp();
  } catch (error) {
    $("loginError").textContent = error.message;
  }
}

async function openUser(id) {
  const data = await request(`/api/admin/users/${id}`);
  const user = data.user;
  $("drawerTitle").textContent = `用户 #${user.id}`;
  $("drawerBody").innerHTML = `<div class="detail-card"><div class="detail-row"><span>OpenID</span><strong>${escapeHtml(user.openid)}</strong></div><div class="detail-row"><span>当前算力</span><strong>${user.credits}</strong></div><div class="detail-row"><span>状态</span><strong>${user.is_blocked ? "已封禁" : "正常"}</strong></div><div class="detail-row"><span>注册时间</span><strong>${formatDate(user.created_at)}</strong></div></div><div class="drawer-actions"><button class="action" data-adjust="${user.id}">调整算力</button><button class="action" data-block="${user.id}" data-blocked="${user.is_blocked}">${user.is_blocked ? "解除封禁" : "封禁用户"}</button></div><div class="detail-card"><p class="kicker">最近流水</p>${data.ledger.slice(0, 10).map((item) => `<div class="detail-row"><span>${escapeHtml(item.reason)}</span><strong>${item.amount > 0 ? "+" : ""}${item.amount}</strong></div>`).join("") || '<div class="empty-state">暂无流水</div>'}</div>`;
  $("drawer").hidden = false;
}

function openCredit(id) {
  $("creditUserId").value = id;
  $("creditAmount").value = "";
  $("creditReason").value = "";
  $("creditError").textContent = "";
  $("modal").hidden = false;
}

async function adjustCredit(event) {
  event.preventDefault();
  try {
    await request(`/api/admin/users/${$("creditUserId").value}/credits`, { method: "POST", body: JSON.stringify({ amount: Number($("creditAmount").value), reason: $("creditReason").value }) });
    $("modal").hidden = true;
    toast("算力已调整");
    await load();
  } catch (error) {
    $("creditError").textContent = error.message;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  $("loginForm").addEventListener("submit", login);
  $("logoutButton").addEventListener("click", async () => { try { await request("/api/admin/logout", { method: "POST" }); } finally { showLogin(); } });
  $("refreshButton").addEventListener("click", load);
  $("closeDrawer").addEventListener("click", () => { $("drawer").hidden = true; });
  $("closeModal").addEventListener("click", () => { $("modal").hidden = true; });
  $("creditForm").addEventListener("submit", adjustCredit);
  document.querySelectorAll(".tab").forEach((button) => button.addEventListener("click", () => {
    state.view = button.dataset.view;
    state.taskOffset = 0;
    $("taskTypeSelect").hidden = state.view !== "operations";
    $("statusSelect").hidden = state.view === "users";
    $("createdAfter").hidden = state.view !== "operations";
    $("createdBefore").hidden = state.view !== "operations";
    $("failureInput").hidden = state.view !== "operations";
    $("pagination").hidden = state.view !== "operations" && state.view !== "feedback";
    $("searchInput").placeholder = state.view === "operations" ? "搜索任务 ID / 用户 / 文件名" : state.view === "feedback" ? "搜索工单 / 用户 / 内容" : "搜索用户 ID / openid";
    document.querySelectorAll(".tab").forEach((item) => item.classList.toggle("active", item === button));
    load();
  }));
  $("searchButton").addEventListener("click", () => { state.taskOffset = 0; load(); });
  $("prevPage").addEventListener("click", () => { state.taskOffset = Math.max(0, state.taskOffset - state.taskLimit); load(); });
  $("nextPage").addEventListener("click", () => { state.taskOffset += state.taskLimit; load(); });
  document.addEventListener("click", async (event) => {
    const user = event.target.closest("[data-user]");
    if (user) await openUser(user.dataset.user);
    const adjust = event.target.closest("[data-adjust]");
    if (adjust) openCredit(adjust.dataset.adjust);
    const block = event.target.closest("[data-block]");
    if (block) {
      const endpoint = block.dataset.blocked === "1" ? "unblock" : "block";
      await request(`/api/admin/users/${block.dataset.block}/${endpoint}`, { method: "POST" });
      toast("用户状态已更新");
      await openUser(block.dataset.block);
      await load();
    }
    const refund = event.target.closest("[data-refund]");
    if (refund && window.confirm("确认给该任务退款？")) {
      await request(`/api/admin/jobs/${refund.dataset.refund}/refund`, { method: "POST" });
      toast("已退款");
      await load();
    }
    const task = event.target.closest("[data-task-type]");
    if (task) await openTask(task.dataset.taskType, task.dataset.taskId);
    const feedback = event.target.closest("[data-feedback]");
    if (feedback) await openFeedback(feedback.dataset.feedback);
  });
  if (state.token) showApp();
});

async function openTask(taskType, taskId) {
  try {
    const data = await request(`/api/admin/operations/tasks/${taskType}/${taskId}`);
    const task = data.task;
    const actions = task.status === "processing" ? `<button class="action" data-task-close="${taskType}:${taskId}">关闭并退款</button>` : task.status !== "failed" ? `<button class="action" data-task-refund="${taskType}:${taskId}">退款</button>` : "";
    $("drawerTitle").textContent = `${task.label} #${task.id}`;
    $("drawerBody").innerHTML = `<div class="detail-card"><div class="detail-row"><span>用户</span><strong>${escapeHtml(data.user.openid || data.user.id)}</strong></div><div class="detail-row"><span>状态</span>${statusTag(task.status)}</div><div class="detail-row"><span>文件</span><strong>${escapeHtml(task.filename || "-")}</strong></div><div class="detail-row"><span>来源</span><strong>${escapeHtml(task.source_url || task.source_platform || task.source_type || "-")}</strong></div><div class="detail-row"><span>创建时间</span><strong>${formatDate(task.created_at)}</strong></div><div class="detail-row"><span>更新时间</span><strong>${formatDate(task.updated_at)}</strong></div></div><div class="drawer-actions">${actions}</div><div class="detail-card"><p class="kicker">错误信息</p><pre class="json-preview">${escapeHtml(task.error_message || "无")}</pre></div><div class="detail-card"><p class="kicker">结果摘要</p><pre class="json-preview">${escapeHtml(JSON.stringify(task.result || {}, null, 2))}</pre></div><div class="detail-card"><p class="kicker">算力流水</p>${data.ledger.map((item) => `<div class="detail-row"><span>${escapeHtml(item.reason)}</span><strong>${item.amount > 0 ? "+" : ""}${item.amount}</strong></div>`).join("") || '<div class="empty-state">暂无流水</div>'}</div><div class="detail-card"><p class="kicker">管理员审计</p>${data.audits.map((item) => `<div class="detail-row"><span>${escapeHtml(item.action)} · ${escapeHtml(item.admin_username)}</span><strong>${formatDate(item.created_at)}</strong></div>`).join("") || '<div class="empty-state">暂无操作</div>'}</div>`;
    $("drawer").hidden = false;
    $("drawerBody").querySelectorAll("[data-task-refund],[data-task-close]").forEach((button) => button.addEventListener("click", async () => {
      const [type, id] = (button.dataset.taskRefund || button.dataset.taskClose).split(":");
      const reason = window.prompt(button.dataset.taskClose ? "请输入关闭原因" : "请输入退款原因", button.dataset.taskClose ? "任务超时" : "运营补偿");
      if (!reason) return;
      await request(`/api/admin/operations/tasks/${type}/${id}/${button.dataset.taskClose ? "close" : "refund"}`, { method: "POST", body: JSON.stringify({ reason }) });
      toast("操作已完成");
      await openTask(type, id);
      await load();
    }));
  } catch (error) { toast(error.message); }
}

function feedbackStatusTag(status) {
  const labels = { open: ["待处理", "warn"], in_progress: ["处理中", "warn"], resolved: ["已解决", "ok"], closed: ["已关闭", "bad"] };
  const item = labels[status] || [status, "warn"];
  return '<span class="status ' + item[1] + '">' + escapeHtml(item[0]) + '</span>';
}

async function openFeedback(id) {
  try {
    const data = await request("/api/admin/feedback/" + id);
    const ticket = data.ticket;
    $("drawerTitle").textContent = "工单 #" + ticket.id;
    const audits = data.audits.map((item) => '<div class="detail-row"><span>' + escapeHtml(item.action + " · " + item.reason) + '</span><strong>' + formatDate(item.created_at) + '</strong></div>').join("") || '<div class="empty-state">暂无操作</div>';
    $("drawerBody").innerHTML = '<div class="detail-card"><div class="detail-row"><span>用户</span><strong>' + escapeHtml(ticket.openid) + '</strong></div><div class="detail-row"><span>关联任务</span><strong>' + escapeHtml(ticket.task_type) + ' #' + ticket.task_id + '</strong></div><div class="detail-row"><span>分类</span><strong>' + escapeHtml(ticket.category) + '</strong></div><div class="detail-row"><span>状态</span>' + feedbackStatusTag(ticket.status) + '</div><div class="detail-row"><span>反馈内容</span><strong>' + escapeHtml(ticket.content) + '</strong></div><div class="detail-row"><span>标签</span><strong>' + escapeHtml(ticket.admin_tags || "-") + '</strong></div><div class="detail-row"><span>回复</span><strong>' + escapeHtml(ticket.reply || "-") + '</strong></div></div><div class="drawer-actions"><button class="action" id="editFeedback">处理工单</button></div><div class="detail-card"><p class="kicker">操作审计</p>' + audits + '</div>';
    $("drawer").hidden = false;
    $("editFeedback").addEventListener("click", async () => {
      const status = window.prompt("状态：open / in_progress / resolved / closed", ticket.status);
      if (!["open", "in_progress", "resolved", "closed"].includes(status)) return toast("状态不正确");
      const admin_tags = window.prompt("标签，使用逗号分隔", ticket.admin_tags || "");
      if (admin_tags === null) return;
      const reply = window.prompt("用户可见回复", ticket.reply || "");
      if (reply === null) return;
      const admin_note = window.prompt("仅管理员可见备注", ticket.admin_note || "");
      if (admin_note === null) return;
      const reason = window.prompt("处理说明", "已处理用户反馈");
      if (!reason) return;
      await request("/api/admin/feedback/" + ticket.id, { method: "PATCH", body: JSON.stringify({ status, admin_tags, reply, admin_note, reason }) });
      toast("工单已更新");
      await openFeedback(ticket.id);
      await load();
    });
  } catch (error) { toast(error.message); }
}

function riskTag(level) {
  const labels = { normal: ["正常", "ok"], watch: ["关注", "warn"], high: ["高风险", "bad"], banned: ["已封禁", "bad"] };
  const item = labels[level] || [level || "正常", "warn"];
  return '<span class="status ' + item[1] + '">' + escapeHtml(item[0]) + "</span>";
}

function detailRows(items, makeLabel, makeValue) {
  if (!items.length) return '<div class="empty-state">暂无记录</div>';
  return items.slice(0, 20).map((item) => '<div class="detail-row"><span>' + escapeHtml(makeLabel(item)) + '</span><strong>' + makeValue(item) + "</strong></div>").join("");
}

async function openUser(id) {
  const data = await request("/api/admin/users/" + id);
  const user = data.user;
  const taskCards = [
    ["图片/视频反推", data.jobs, (item) => item.mode + " · " + item.filename],
    ["深度转换", data.depth_jobs, (item) => item.preset + " · " + item.filename],
    ["提示词优化", data.optimizations, (item) => item.strategy + " · " + item.platform],
    ["视频复刻诊断", data.diagnostics, (item) => (item.original_filename || "-") + " / " + (item.generated_filename || "-")],
  ].map((group) => '<div class="detail-card"><p class="kicker">' + group[0] + "</p>" + detailRows(group[1], group[2], (item) => statusTag(item.status === "completed" ? "succeeded" : item.status)) + "</div>").join("");
  $("drawerTitle").textContent = "用户 #" + user.id;
  $("drawerBody").innerHTML = '<div class="detail-card"><div class="detail-row"><span>OpenID</span><strong>' + escapeHtml(user.openid) + '</strong></div><div class="detail-row"><span>当前算力</span><strong>' + user.credits + '</strong></div><div class="detail-row"><span>风险等级</span>' + riskTag(user.risk_level) + '</div><div class="detail-row"><span>账号状态</span><strong>' + (user.is_blocked ? "已封禁" : "正常") + '</strong></div><div class="detail-row"><span>封禁原因</span><strong>' + escapeHtml(user.block_reason || "-") + '</strong></div><div class="detail-row"><span>运营备注</span><strong>' + escapeHtml(user.admin_note || "-") + '</strong></div><div class="detail-row"><span>注册时间</span><strong>' + formatDate(user.created_at) + '</strong></div></div><div class="drawer-actions"><button class="action" data-adjust="' + user.id + '">调整算力</button><button class="action" data-profile="' + user.id + '">编辑运营信息</button><button class="action" data-block="' + user.id + '" data-blocked="' + user.is_blocked + '">' + (user.is_blocked ? "解除封禁" : "封禁用户") + '</button></div><div class="detail-card"><p class="kicker">算力流水</p>' + detailRows(data.ledger, (item) => item.reason, (item) => (item.amount > 0 ? "+" : "") + item.amount + " · " + formatDate(item.created_at)) + '</div><div class="detail-card"><p class="kicker">激励广告</p>' + detailRows(data.ads, (item) => item.status, (item) => formatDate(item.claimed_at || item.created_at)) + '</div><div class="detail-card"><p class="kicker">创作项目</p>' + detailRows(data.projects, (item) => item.title, (item) => escapeHtml(item.recent_platform)) + '</div>' + taskCards + '<div class="detail-card"><p class="kicker">管理员审计</p>' + detailRows(data.audits, (item) => item.action + " · " + item.reason, (item) => formatDate(item.created_at)) + "</div>";
  $("drawer").hidden = false;
}

document.addEventListener("DOMContentLoaded", () => {
  document.addEventListener("click", async (event) => {
    const profile = event.target.closest("[data-profile]");
    const block = event.target.closest("[data-block]");
    if (!profile && !block) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    try {
      const userId = (profile || block).dataset.profile || block.dataset.block;
      if (profile) {
        const data = await request("/api/admin/users/" + userId);
        const note = window.prompt("运营备注", data.user.admin_note || "");
        if (note === null) return;
        const riskLevel = window.prompt("风险等级：normal / watch / high", data.user.risk_level || "normal");
        if (!["normal", "watch", "high"].includes(riskLevel)) return toast("风险等级不正确");
        const reason = window.prompt("请输入修改原因", "运营标记更新");
        if (!reason) return;
        await request("/api/admin/users/" + userId + "/profile", { method: "PATCH", body: JSON.stringify({ admin_note: note, risk_level: riskLevel, reason }) });
        toast("运营信息已保存");
      } else {
        const endpoint = block.dataset.blocked === "1" ? "unblock" : "block";
        const reason = window.prompt(endpoint === "block" ? "请输入封禁原因" : "请输入解除封禁原因", endpoint === "block" ? "异常使用行为" : "人工复核通过");
        if (!reason) return;
        await request("/api/admin/users/" + userId + "/" + endpoint, { method: "POST", body: JSON.stringify({ reason }) });
        toast("用户状态已更新");
      }
      await openUser(userId);
      await load();
    } catch (error) {
      toast(error.message);
    }
  }, true);
});
