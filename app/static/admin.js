const AUTH_KEY = "mindbridge.auth";
const BRAND = window.CAMPUSCARE_BRAND || { assistant: "CampusCare" };

const state = {
  profile: null,
  modelName: "mock",
  knowledgeJobId: null,
  selectedKnowledgeDocumentId: null
};

const els = {
  serviceState: document.querySelector("#serviceState"),
  modelState: document.querySelector("#modelState"),
  activeAccount: document.querySelector("#activeAccount"),
  switchAccount: document.querySelector("#switchAccount"),
  refreshAdmin: document.querySelector("#refreshAdmin"),
  metricReports: document.querySelector("#metricReports"),
  metricHigh: document.querySelector("#metricHigh"),
  metricCases: document.querySelector("#metricCases"),
  metricExcel: document.querySelector("#metricExcel"),
  metricAlerts: document.querySelector("#metricAlerts"),
  cases: document.querySelector("#cases"),
  reports: document.querySelector("#reports"),
  conversationState: document.querySelector("#conversationState"),
  conversationDetail: document.querySelector("#conversationDetail"),
  knowledgeState: document.querySelector("#knowledgeState"),
  knowledgeUploadForm: document.querySelector("#knowledgeUploadForm"),
  knowledgeFile: document.querySelector("#knowledgeFile"),
  knowledgeTitle: document.querySelector("#knowledgeTitle"),
  knowledgeCanonicalKey: document.querySelector("#knowledgeCanonicalKey"),
  knowledgeDomain: document.querySelector("#knowledgeDomain"),
  knowledgeTags: document.querySelector("#knowledgeTags"),
  knowledgeSite: document.querySelector("#knowledgeSite"),
  knowledgeVersion: document.querySelector("#knowledgeVersion"),
  knowledgeParserProfile: document.querySelector("#knowledgeParserProfile"),
  knowledgeChunkingProfile: document.querySelector("#knowledgeChunkingProfile"),
  knowledgeVerifiedAt: document.querySelector("#knowledgeVerifiedAt"),
  knowledgeExpiresAt: document.querySelector("#knowledgeExpiresAt"),
  knowledgeUploadState: document.querySelector("#knowledgeUploadState"),
  knowledgeRetryJob: document.querySelector("#knowledgeRetryJob"),
  refreshKnowledgeDocuments: document.querySelector("#refreshKnowledgeDocuments"),
  knowledgeDocuments: document.querySelector("#knowledgeDocuments"),
  knowledgePreviewState: document.querySelector("#knowledgePreviewState"),
  knowledgePreview: document.querySelector("#knowledgePreview")
};

function readAuth() {
  try {
    return JSON.parse(sessionStorage.getItem(AUTH_KEY) || "null");
  } catch {
    return null;
  }
}

function clearAuth() {
  sessionStorage.removeItem(AUTH_KEY);
}

function authHeader() {
  const auth = readAuth();
  if (!auth?.token) {
    window.location.replace("/");
    return "";
  }
  return `Basic ${auth.token}`;
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}), Authorization: authHeader() };
  const response = await fetch(path, { ...options, headers });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `${response.status} ${response.statusText}`);
  }
  return response;
}

function setPill(el, text, tone = "ok") {
  el.textContent = text;
  el.className = `pill ${tone}`;
}

function isAdmin(profile) {
  return profile.roles?.some((role) => role.authority === "ROLE_ADMIN");
}

function displayModel(model) {
  return (model || "").includes("mindbridge-qwen2.5-7b-ft") ? "微调 Qwen2.5-7B" : model;
}

function displayTime(value) {
  return value ? new Date(value).toLocaleString() : "";
}

function roleLabel(role) {
  const value = (role || "").toUpperCase();
  if (value === "USER") return "学生";
  if (value === "ASSISTANT") return BRAND.assistant;
  if (value === "SYSTEM") return "系统";
  return role || "未知角色";
}

async function checkHealth() {
  try {
    const response = await fetch("/actuator/health");
    const body = await response.json();
    setPill(els.serviceState, body.status === "UP" ? "服务正常" : `服务 ${body.status}`, body.status === "UP" ? "ok" : "danger");
  } catch {
    setPill(els.serviceState, "服务 DOWN", "danger");
  }
}

async function loadProfile() {
  try {
    const response = await api("/api/profile");
    const profile = await response.json();
    if (!isAdmin(profile)) {
      window.location.replace("/student.html");
      return null;
    }
    state.profile = profile;
    els.activeAccount.textContent = profile.displayName || profile.username;
    return profile;
  } catch {
    clearAuth();
    window.location.replace("/");
    return null;
  }
}

async function loadAgentStatus() {
  const response = await api("/api/agent/status");
  const status = await response.json();
  state.modelName = status.model || "mock";
  if (status.realModelEnabled) {
    setPill(els.modelState, `${status.provider} / ${displayModel(state.modelName)}`, "ok");
  } else {
    setPill(els.modelState, "mock 演示", "warn");
  }
}

async function loadAdminDashboard() {
  const [reportsRes, casesRes, excelRes, alertsRes] = await Promise.all([
    api("/api/admin/reports"),
    api("/api/admin/cases"),
    api("/api/admin/excel-records"),
    api("/api/admin/alerts")
  ]);
  const reports = await reportsRes.json();
  const cases = await casesRes.json();
  const excel = await excelRes.json();
  const alerts = await alertsRes.json();
  els.metricReports.textContent = reports.length;
  els.metricHigh.textContent = reports.filter((item) => item.riskLevel === "HIGH").length;
  els.metricCases.textContent = cases.length;
  els.metricExcel.textContent = excel.length;
  els.metricAlerts.textContent = alerts.length;
  renderCases(cases);
  renderReports(reports);
}

async function loadKnowledgeStatus() {
  try {
    const response = await api("/api/admin/knowledge/status");
    const status = await response.json();
    const vector = status.vectorAvailable
      ? `Hybrid ${status.vectorChunks ?? 0}`
      : `${status.retrievalMode || "BM25-only"}`;
    const index = status.indexState || "EMPTY";
    els.knowledgeState.textContent = `DB ${status.databaseChunks} 片段 · ${vector} · 索引 ${index}`;
  } catch (error) {
    els.knowledgeState.textContent = `读取失败：${error.message}`;
  }
}

function renderCases(cases) {
  els.cases.innerHTML = "";
  if (!cases.length) {
    els.cases.innerHTML = `<div class="empty small"><strong>暂无个案</strong><p>中高风险报告会自动创建风险个案。</p></div>`;
    return;
  }
  for (const item of cases) {
    const card = document.createElement("article");
    card.className = `case-card risk-${item.riskLevel.toLowerCase()}`;

    const head = document.createElement("div");
    head.className = "case-head";
    const title = document.createElement("strong");
    title.textContent = `个案 #${item.id} · ${item.status}`;
    const time = document.createElement("span");
    time.textContent = displayTime(item.updatedAt);
    head.append(title, time);

    const meta = document.createElement("small");
    meta.textContent = `报告 #${item.reportId} · ${item.riskLevel} · 负责人 ${item.owner || "未分配"}`;
    const summary = document.createElement("p");
    summary.textContent = item.summary || "";
    const handoff = document.createElement("pre");
    handoff.textContent = item.handoffSummary || "";

    card.append(head, meta, summary, handoff);
    els.cases.append(card);
  }
}

function renderReports(reports) {
  els.reports.innerHTML = "";
  if (!reports.length) {
    els.reports.innerHTML = `<div class="empty small"><strong>暂无报告</strong><p>学生咨询或风险场景会在这里沉淀记录。</p></div>`;
    return;
  }
  for (const item of reports) {
    const card = document.createElement("button");
    card.type = "button";
    card.className = `report risk-${item.riskLevel.toLowerCase()}`;
    card.dataset.sessionId = item.sessionId;

    const head = document.createElement("div");
    head.className = "report-head";
    const title = document.createElement("strong");
    title.textContent = `${item.displayName} · ${item.riskLevel}`;
    const time = document.createElement("span");
    time.textContent = displayTime(item.createdAt);
    head.append(title, time);

    const summary = document.createElement("p");
    summary.textContent = item.summary;
    const content = document.createElement("small");
    content.textContent = item.content;
    const action = document.createElement("span");
    action.className = "report-action";
    action.textContent = "查看会话档案";

    card.append(head, summary, content, action);
    card.addEventListener("click", () => loadConversation(item.sessionId));
    els.reports.append(card);
  }
}

async function loadConversation(sessionId) {
  if (!sessionId) {
    els.conversationState.textContent = "该报告缺少会话 ID";
    return;
  }
  els.conversationState.textContent = "正在读取...";
  els.conversationDetail.innerHTML = `<div class="empty small"><strong>加载中</strong><p>正在读取历史消息。</p></div>`;
  for (const card of els.reports.querySelectorAll(".report")) {
    card.classList.toggle("active", card.dataset.sessionId === sessionId);
  }
  try {
    const response = await api(`/api/admin/conversations/${encodeURIComponent(sessionId)}`);
    const conversation = await response.json();
    renderConversation(conversation);
  } catch (error) {
    els.conversationState.textContent = "读取失败";
    els.conversationDetail.innerHTML = "";
    const empty = document.createElement("div");
    empty.className = "empty small";
    const title = document.createElement("strong");
    title.textContent = "无法查看档案";
    const detail = document.createElement("p");
    detail.textContent = error.message;
    empty.append(title, detail);
    els.conversationDetail.append(empty);
  }
}

function renderConversation(conversation) {
  const messages = conversation.messages || [];
  els.conversationState.replaceChildren();
  const summary = document.createElement("span");
  summary.textContent = `${conversation.title || conversation.sessionId} · ${messages.length} 条消息`;
  els.conversationState.append(summary);
  if (conversation.archived) {
    const archived = document.createElement("span");
    archived.className = "archive-status";
    archived.textContent = "已归档";
    archived.title = conversation.archivedAt ? `归档于 ${displayTime(conversation.archivedAt)}` : "已归档";
    els.conversationState.append(archived);
  }
  els.conversationDetail.innerHTML = "";
  if (!messages.length) {
    els.conversationDetail.innerHTML = `<div class="empty small"><strong>暂无消息</strong><p>这个会话还没有写入消息记录。</p></div>`;
    return;
  }
  for (const message of messages) {
    const role = (message.role || "").toLowerCase();
    const row = document.createElement("article");
    row.className = `conversation-message ${role}`;

    const meta = document.createElement("div");
    meta.className = "conversation-meta";
    const label = document.createElement("strong");
    label.textContent = roleLabel(message.role);
    const time = document.createElement("span");
    time.textContent = displayTime(message.createdAt);
    meta.append(label, time);

    const bubble = document.createElement("div");
    bubble.className = "conversation-bubble";
    bubble.textContent = message.content || "";

    row.append(meta, bubble);
    els.conversationDetail.append(row);
  }
}

async function uploadKnowledgeFile(event) {
  event.preventDefault();
  const file = els.knowledgeFile.files?.[0];
  if (!file) {
    els.knowledgeUploadState.textContent = "请先选择文件";
    return;
  }
  const data = new FormData();
  data.append("file", file);
  data.append("title", els.knowledgeTitle.value || file.name.replace(/\.[^.]+$/, ""));
  data.append("canonical_key", els.knowledgeCanonicalKey.value);
  data.append("domain", els.knowledgeDomain.value);
  data.append("tags", els.knowledgeTags.value);
  data.append("site", els.knowledgeSite.value || "ALL");
  data.append("version", els.knowledgeVersion.value || "1");
  data.append("parser_profile", els.knowledgeParserProfile.value);
  data.append("chunking_profile", els.knowledgeChunkingProfile.value);
  data.append("verified_at", localDateTimeToIso(els.knowledgeVerifiedAt.value));
  data.append("expires_at", localDateTimeToIso(els.knowledgeExpiresAt.value));
  els.knowledgeUploadState.textContent = "任务提交中...";
  els.knowledgeRetryJob.hidden = true;
  try {
    const response = await api("/api/admin/knowledge/documents", { method: "POST", body: data });
    const result = await response.json();
    state.knowledgeJobId = result.id;
    els.knowledgeUploadState.textContent = `任务 #${result.id} 已提交`;
    els.knowledgeFile.value = "";
    pollKnowledgeJob(result.id);
    loadKnowledgeDocuments();
  } catch (error) {
    els.knowledgeUploadState.textContent = `上传失败：${error.message}`;
  }
}

function localDateTimeToIso(value) {
  return value ? new Date(value).toISOString() : "";
}

async function pollKnowledgeJob(jobId) {
  if (state.knowledgeJobId !== jobId) return;
  try {
    const response = await api(`/api/admin/knowledge/jobs/${jobId}`);
    const job = await response.json();
    els.knowledgeUploadState.textContent = `任务 #${job.id} · ${job.status} · 尝试 ${job.attempts}/${job.maxAttempts}`;
    if (job.status === "PENDING" || job.status === "RUNNING") {
      window.setTimeout(() => pollKnowledgeJob(jobId), 1000);
      return;
    }
    if (job.status === "FAILED") {
      els.knowledgeUploadState.textContent = `${job.failure?.message || "处理失败"}（${job.failure?.code || "ingestion_failed"}）`;
      els.knowledgeRetryJob.hidden = false;
      return;
    }
    els.knowledgeRetryJob.hidden = true;
    await Promise.all([loadKnowledgeStatus(), loadKnowledgeDocuments()]);
    if (job.documentId) loadKnowledgePreview(job.documentId);
  } catch (error) {
    els.knowledgeUploadState.textContent = `任务状态读取失败：${error.message}`;
  }
}

async function retryKnowledgeJob() {
  if (!state.knowledgeJobId) return;
  try {
    const response = await api(`/api/admin/knowledge/jobs/${state.knowledgeJobId}/retry`, { method: "POST" });
    const job = await response.json();
    els.knowledgeRetryJob.hidden = true;
    pollKnowledgeJob(job.id);
  } catch (error) {
    els.knowledgeUploadState.textContent = `重试失败：${error.message}`;
  }
}

async function loadKnowledgeDocuments() {
  try {
    const response = await api("/api/admin/knowledge/documents");
    const body = await response.json();
    renderKnowledgeDocuments(body.items || []);
  } catch (error) {
    els.knowledgeDocuments.textContent = `读取失败：${error.message}`;
  }
}

function renderKnowledgeDocuments(items) {
  els.knowledgeDocuments.innerHTML = "";
  if (!items.length) {
    els.knowledgeDocuments.textContent = "暂无知识文档";
    return;
  }
  for (const item of items) {
    const row = document.createElement("article");
    row.className = "knowledge-document-row";
    const head = document.createElement("div");
    head.className = "knowledge-document-head";
    const title = document.createElement("strong");
    title.textContent = item.title;
    const status = document.createElement("span");
    status.className = "hint";
    status.textContent = `${item.status} / ${item.ingestionStatus}`;
    head.append(title, status);
    const meta = document.createElement("p");
    meta.textContent = `${item.domain} · ${item.version} · ${item.chunkCount} Chunk · ${item.parserProfile} / ${item.chunkingProfile}`;
    const actions = document.createElement("div");
    actions.className = "knowledge-document-actions";
    actions.append(
      knowledgeAction("预览", () => loadKnowledgePreview(item.id)),
      knowledgeAction("重处理", () => reprocessKnowledgeDocument(item)),
      knowledgeAction("发布", () => publishKnowledgeDocument(item.id), item.ingestionStatus !== "READY"),
      knowledgeAction("下线", () => deactivateKnowledgeDocument(item.id), item.status !== "ACTIVE")
    );
    row.append(head, meta, actions);
    els.knowledgeDocuments.append(row);
  }
}

function knowledgeAction(label, handler, disabled = false) {
  const button = document.createElement("button");
  button.className = "ghost compact";
  button.type = "button";
  button.textContent = label;
  button.disabled = disabled;
  button.addEventListener("click", handler);
  return button;
}

async function loadKnowledgePreview(documentId) {
  state.selectedKnowledgeDocumentId = documentId;
  els.knowledgePreviewState.textContent = "读取中...";
  try {
    const response = await api(`/api/admin/knowledge/documents/${documentId}/preview`);
    const preview = await response.json();
    renderKnowledgePreview(preview);
    els.knowledgePreviewState.textContent = `${preview.counts.elements} 元素 · ${preview.counts.pages} 页 · ${preview.counts.tables} 表 · ${preview.counts.chunks} Chunk`;
  } catch (error) {
    els.knowledgePreviewState.textContent = "读取失败";
    els.knowledgePreview.textContent = error.message;
  }
}

function renderKnowledgePreview(preview) {
  els.knowledgePreview.innerHTML = "";
  const summary = document.createElement("div");
  summary.className = "knowledge-preview-summary";
  const parser = document.createElement("p");
  parser.textContent = `解析 ${preview.parser.profile} / ${preview.parser.version} · 分割 ${preview.chunkingProfile} · 索引 ${preview.index.state}`;
  const quality = document.createElement("p");
  quality.textContent = `质量 ${JSON.stringify(preview.quality)} · 警告 ${(preview.warnings || []).join("、") || "无"}`;
  summary.append(parser, quality, createMetadataEditor(preview.document));
  const chunks = document.createElement("div");
  chunks.className = "knowledge-chunks";
  for (const item of preview.chunks) {
    const row = document.createElement("article");
    row.className = "knowledge-chunk";
    const title = document.createElement("strong");
    title.textContent = `#${item.index} ${item.type} · 第 ${item.pageNumber ?? "?"} 页 · 父块 ${item.parentChunkId ?? "无"}`;
    const path = document.createElement("p");
    path.textContent = item.headingPath.join(" / ") || "无标题路径";
    const content = document.createElement("pre");
    content.textContent = item.content;
    row.append(title, path, content);
    chunks.append(row);
  }
  els.knowledgePreview.append(summary, chunks);
}

function createMetadataEditor(documentItem) {
  const form = document.createElement("form");
  form.className = "knowledge-metadata-grid";
  const values = [
    ["title", "标题", documentItem.title],
    ["domain", "领域", documentItem.domain],
    ["tags", "标签", (documentItem.tags || []).join(",")],
    ["site", "校区", documentItem.site],
    ["version", "版本", documentItem.version]
  ];
  for (const [name, labelText, value] of values) {
    const label = document.createElement("label");
    label.textContent = labelText;
    const input = document.createElement("input");
    input.name = name;
    input.value = value || "";
    label.append(input);
    form.append(label);
  }
  const save = document.createElement("button");
  save.className = "primary compact";
  save.type = "submit";
  save.textContent = "保存元数据";
  form.append(save);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(form);
    await api(`/api/admin/knowledge/documents/${documentItem.id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        title: data.get("title"),
        domain: data.get("domain"),
        tags: String(data.get("tags") || "").split(/[,，]/).map((item) => item.trim()).filter(Boolean),
        site: data.get("site"),
        version: data.get("version"),
        verifiedAt: documentItem.verifiedAt,
        expiresAt: documentItem.expiresAt
      })
    });
    await Promise.all([loadKnowledgeDocuments(), loadKnowledgePreview(documentItem.id)]);
  });
  return form;
}

async function reprocessKnowledgeDocument(documentItem) {
  try {
    const response = await api(`/api/admin/knowledge/documents/${documentItem.id}/reprocess`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ parserProfile: documentItem.parserProfile, chunkingProfile: documentItem.chunkingProfile })
    });
    const job = await response.json();
    state.knowledgeJobId = job.id;
    pollKnowledgeJob(job.id);
  } catch (error) {
    els.knowledgeUploadState.textContent = `重处理提交失败：${error.message}`;
  }
}

async function publishKnowledgeDocument(documentId) {
  try {
    await api(`/api/admin/knowledge/documents/${documentId}/publish`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ verifiedAt: new Date().toISOString() })
    });
    await Promise.all([loadKnowledgeDocuments(), loadKnowledgePreview(documentId), loadKnowledgeStatus()]);
  } catch (error) {
    els.knowledgeUploadState.textContent = `发布失败：${error.message}`;
  }
}

async function deactivateKnowledgeDocument(documentId) {
  try {
    await api(`/api/admin/knowledge/documents/${documentId}/deactivate`, { method: "POST" });
    await Promise.all([loadKnowledgeDocuments(), loadKnowledgePreview(documentId), loadKnowledgeStatus()]);
  } catch (error) {
    els.knowledgeUploadState.textContent = `下线失败：${error.message}`;
  }
}

function logout() {
  clearAuth();
  window.location.assign("/");
}

els.switchAccount.addEventListener("click", logout);
els.refreshAdmin.addEventListener("click", () => {
  loadAdminDashboard();
  loadKnowledgeStatus();
});
els.knowledgeUploadForm.addEventListener("submit", uploadKnowledgeFile);
els.knowledgeRetryJob.addEventListener("click", retryKnowledgeJob);
els.refreshKnowledgeDocuments.addEventListener("click", loadKnowledgeDocuments);
checkHealth();
loadProfile().then((profile) => {
  if (!profile) return;
  loadAgentStatus();
  loadAdminDashboard();
  loadKnowledgeStatus();
  loadKnowledgeDocuments();
});
