const AUTH_KEY = "mindbridge.auth";
const ACTIVE_TURN_KEY = "mindbridge.activeTurn";
const BRAND = window.CAMPUSCOVE_BRAND || { product: "CampusCove", assistant: "Cove" };
const ASSISTANT_MARKDOWN_TAGS = [
  "p", "br", "strong", "em", "ul", "ol", "li", "blockquote",
  "h1", "h2", "h3", "h4", "pre", "code",
  "table", "thead", "tbody", "tr", "th", "td", "a", "hr"
];
const ASSISTANT_FORBIDDEN_TAGS = [
  "script", "style", "iframe", "object", "embed",
  "form", "input", "button", "textarea", "svg", "math"
];
const BLOCKED_LINK_PROTOCOLS = ["javascript:", "data:"];

const state = {
  sessionId: null,
  conversations: [],
  memories: [],
  loadingMemories: false,
  loadingConversations: false,
  loadingMessages: false,
  archivingSessionId: null,
  sending: false,
  profile: null,
  modelName: "mock",
  requestId: null,
  turnId: null
};

const els = {
  serviceState: document.querySelector("#serviceState"),
  modelState: document.querySelector("#modelState"),
  activeAccount: document.querySelector("#activeAccount"),
  openMemories: document.querySelector("#openMemories"),
  switchAccount: document.querySelector("#switchAccount"),
  messages: document.querySelector("#messages"),
  chatForm: document.querySelector("#chatForm"),
  messageInput: document.querySelector("#messageInput"),
  sendButton: document.querySelector("#sendButton"),
  newSession: document.querySelector("#newSession"),
  sessionBadge: document.querySelector("#sessionBadge"),
  conversationTitle: document.querySelector("#conversationTitle"),
  conversationList: document.querySelector("#conversationList"),
  conversationListState: document.querySelector("#conversationListState"),
  refreshConversations: document.querySelector("#refreshConversations"),
  conversationDrawer: document.querySelector("#conversationDrawer"),
  openHistory: document.querySelector("#openHistory"),
  closeHistory: document.querySelector("#closeHistory"),
  historyBackdrop: document.querySelector("#historyBackdrop"),
  archiveDialog: document.querySelector("#archiveDialog"),
  cancelArchive: document.querySelector("#cancelArchive"),
  confirmArchive: document.querySelector("#confirmArchive"),
  memoryDialog: document.querySelector("#memoryDialog"),
  closeMemories: document.querySelector("#closeMemories"),
  memoryList: document.querySelector("#memoryList"),
  memoryListState: document.querySelector("#memoryListState"),
  notice: document.querySelector("#notice")
};

let noticeTimer = null;

function readAuth() {
  try {
    return JSON.parse(sessionStorage.getItem(AUTH_KEY) || "null");
  } catch {
    return null;
  }
}

function clearAuth() {
  sessionStorage.removeItem(AUTH_KEY);
  clearActiveTurn();
}

function saveActiveTurn() {
  if (!state.requestId) return;
  sessionStorage.setItem(
    ACTIVE_TURN_KEY,
    JSON.stringify({ requestId: state.requestId, turnId: state.turnId, sessionId: state.sessionId })
  );
}

function readActiveTurn() {
  try {
    return JSON.parse(sessionStorage.getItem(ACTIVE_TURN_KEY) || "null");
  } catch {
    return null;
  }
}

function clearActiveTurn() {
  sessionStorage.removeItem(ACTIVE_TURN_KEY);
  state.requestId = null;
  state.turnId = null;
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
    const contentType = response.headers.get("content-type") || "";
    const body = contentType.includes("application/json") ? await response.json() : await response.text();
    const message = typeof body === "object" ? body.detail : body;
    throw new Error(message || `${response.status} ${response.statusText}`);
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

function friendlyTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const now = new Date();
  const seconds = Math.max(0, Math.floor((now - date) / 1000));
  if (seconds < 60) return "刚刚";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
  if (seconds < 604800) return `${Math.floor(seconds / 86400)} 天前`;
  return date.toLocaleDateString("zh-CN", { month: "numeric", day: "numeric" });
}

function sanitizedAssistantDocument(markdown) {
  if (!window.marked?.parse || !window.DOMPurify?.sanitize) return null;
  const parsed = window.marked.parse(String(markdown || ""), {
    gfm: true,
    breaks: false
  });
  const sanitized = window.DOMPurify.sanitize(parsed, {
    ALLOWED_TAGS: ASSISTANT_MARKDOWN_TAGS,
    ALLOWED_ATTR: ["href", "title"],
    FORBID_TAGS: ASSISTANT_FORBIDDEN_TAGS,
    FORBID_ATTR: ["style"],
    ALLOW_DATA_ATTR: false
  });
  const documentFragment = new DOMParser().parseFromString(sanitized, "text/html");
  for (const link of documentFragment.body.querySelectorAll("a")) {
    const href = (link.getAttribute("href") || "").trim();
    if (!isAllowedAssistantHref(href)) {
      link.removeAttribute("href");
      link.removeAttribute("target");
      link.removeAttribute("rel");
      continue;
    }
    const target = new URL(href, window.location.href);
    if (target.protocol === "http:" || target.protocol === "https:") {
      if (target.origin !== window.location.origin) {
        link.setAttribute("target", "_blank");
        link.setAttribute("rel", "noopener noreferrer");
      }
    }
  }
  return documentFragment;
}

function isAllowedAssistantHref(href) {
  const lowered = href.trim().toLowerCase();
  if (!lowered || BLOCKED_LINK_PROTOCOLS.some((protocol) => lowered.startsWith(protocol))) {
    return false;
  }
  try {
    const parsed = new URL(href, window.location.href);
    return ["http:", "https:", "mailto:"].includes(parsed.protocol);
  } catch {
    return false;
  }
}

function renderAssistantMarkdown(element, markdown) {
  const parsed = sanitizedAssistantDocument(markdown);
  if (!parsed) {
    element.textContent = markdown || "";
    return;
  }
  const nodes = [...parsed.body.childNodes].map((node) => document.importNode(node, true));
  element.replaceChildren(...nodes);
}

function markdownToPlainText(markdown, limit = 160) {
  const parsed = sanitizedAssistantDocument(markdown);
  const text = parsed ? parsed.body.textContent : String(markdown || "");
  const compact = text.replace(/\s+/g, " ").trim();
  return compact.length <= limit ? compact : `${compact.slice(0, limit - 1)}…`;
}

function showNotice(message, tone = "ok") {
  window.clearTimeout(noticeTimer);
  els.notice.textContent = message;
  els.notice.className = `notice ${tone}`;
  els.notice.hidden = false;
  noticeTimer = window.setTimeout(() => {
    els.notice.hidden = true;
  }, 4200);
}

function isBusy() {
  return state.sending || state.loadingMessages || Boolean(state.archivingSessionId);
}

function syncControls() {
  const busy = isBusy();
  els.messageInput.disabled = state.loadingMessages || Boolean(state.archivingSessionId);
  els.sendButton.disabled = busy;
  els.newSession.disabled = busy;
  els.refreshConversations.disabled = state.loadingConversations || busy;
  els.openMemories.disabled = state.loadingMemories || busy;
  els.confirmArchive.disabled = !state.archivingSessionId;
  for (const button of els.conversationList.querySelectorAll("button")) {
    button.disabled = busy;
  }
}

const MEMORY_CATEGORY_LABELS = {
  PROFILE: "个人资料",
  LEARNING_PREFERENCE: "学习偏好",
  LONG_TERM_CONSTRAINT: "长期安排",
  EXPLICIT: "明确记忆"
};

function renderUserMemories() {
  els.memoryList.replaceChildren();
  if (state.loadingMemories) {
    els.memoryListState.textContent = "正在读取记忆...";
    els.memoryListState.hidden = false;
    return;
  }
  if (!state.memories.length) {
    els.memoryListState.textContent = "暂无已保存的长期记忆";
    els.memoryListState.hidden = false;
    return;
  }
  els.memoryListState.hidden = true;
  for (const memory of state.memories) {
    const item = document.createElement("article");
    item.className = "memory-item";

    const meta = document.createElement("div");
    meta.className = "memory-item-meta";
    const category = document.createElement("strong");
    category.textContent = MEMORY_CATEGORY_LABELS[memory.category] || memory.category || "记忆";
    const time = document.createElement("time");
    time.dateTime = memory.createdAt || "";
    time.textContent = friendlyTime(memory.createdAt);
    meta.append(category, time);

    const content = document.createElement("p");
    content.className = "memory-content";
    content.textContent = memory.content || "";

    const remove = document.createElement("button");
    remove.className = "danger-btn memory-delete";
    remove.type = "button";
    remove.textContent = "删除";
    remove.addEventListener("click", () => deleteUserMemory(memory.memoryId));
    item.append(meta, content, remove);
    els.memoryList.append(item);
  }
}

async function loadUserMemories() {
  state.loadingMemories = true;
  renderUserMemories();
  syncControls();
  try {
    const response = await api("/api/user/memories");
    const body = await response.json();
    state.memories = body.items || [];
  } catch (error) {
    state.memories = [];
    els.memoryListState.textContent = `记忆读取失败：${error.message}`;
    els.memoryListState.hidden = false;
    return;
  } finally {
    state.loadingMemories = false;
    syncControls();
  }
  renderUserMemories();
}

async function deleteUserMemory(memoryId) {
  const memory = state.memories.find((item) => item.memoryId === memoryId);
  if (!memory || !window.confirm("确认删除这条长期记忆吗？删除后将不再用于后续对话。")) return;
  try {
    await api(`/api/user/memories/${encodeURIComponent(memoryId)}`, { method: "DELETE" });
    state.memories = state.memories.filter((item) => item.memoryId !== memoryId);
    renderUserMemories();
    showNotice("记忆已删除。");
  } catch (error) {
    showNotice(`删除记忆失败：${error.message}`, "danger");
  }
}

async function openMemoryDialog() {
  els.memoryDialog.showModal();
  await loadUserMemories();
  els.closeMemories.focus();
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
    if (isAdmin(profile)) {
      window.location.replace("/admin.html");
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
  try {
    const response = await api("/api/agent/status");
    const status = await response.json();
    state.modelName = status.model || "mock";
    if (status.realModelEnabled) {
      setPill(els.modelState, `${status.provider} / ${displayModel(state.modelName)}`, "ok");
    } else {
      setPill(els.modelState, "mock 演示", "warn");
    }
  } catch {
    setPill(els.modelState, "状态读取失败", "danger");
  }
}

function closeConversationMenus() {
  for (const menu of els.conversationList.querySelectorAll(".conversation-menu")) {
    menu.hidden = true;
  }
  for (const button of els.conversationList.querySelectorAll(".conversation-menu-btn")) {
    button.setAttribute("aria-expanded", "false");
  }
}

function renderConversationList() {
  els.conversationList.replaceChildren();
  if (state.loadingConversations && !state.conversations.length) {
    els.conversationListState.textContent = "正在读取历史会话...";
    els.conversationListState.hidden = false;
    return;
  }
  if (!state.conversations.length) {
    els.conversationListState.textContent = "暂无历史会话";
    els.conversationListState.hidden = false;
    return;
  }
  els.conversationListState.hidden = true;

  for (const conversation of state.conversations) {
    const item = document.createElement("article");
    item.className = "conversation-item";
    item.classList.toggle("active", conversation.sessionId === state.sessionId);

    const openButton = document.createElement("button");
    openButton.type = "button";
    openButton.className = "conversation-open";
    openButton.setAttribute("aria-label", `打开会话：${conversation.title}`);

    const title = document.createElement("strong");
    title.className = "conversation-item-title";
    title.textContent = conversation.title || "新会话";
    const preview = document.createElement("span");
    preview.className = "conversation-preview";
    preview.textContent = markdownToPlainText(conversation.preview || "暂无消息预览");
    const time = document.createElement("time");
    time.className = "conversation-time";
    time.dateTime = conversation.updatedAt || "";
    time.textContent = friendlyTime(conversation.updatedAt);
    openButton.append(title, preview, time);
    openButton.addEventListener("click", () => openConversation(conversation.sessionId));

    const menuButton = document.createElement("button");
    menuButton.type = "button";
    menuButton.className = "icon-btn conversation-menu-btn";
    menuButton.textContent = "⋯";
    menuButton.title = "会话操作";
    menuButton.setAttribute("aria-label", "会话操作");
    menuButton.setAttribute("aria-expanded", "false");

    const menu = document.createElement("div");
    menu.className = "conversation-menu";
    menu.hidden = true;
    const archiveButton = document.createElement("button");
    archiveButton.type = "button";
    archiveButton.className = "archive-action";
    archiveButton.textContent = "归档";
    archiveButton.addEventListener("click", () => requestArchive(conversation.sessionId));
    menu.append(archiveButton);

    menuButton.addEventListener("click", (event) => {
      event.stopPropagation();
      const shouldOpen = menu.hidden;
      closeConversationMenus();
      menu.hidden = !shouldOpen;
      menuButton.setAttribute("aria-expanded", String(shouldOpen));
    });

    item.append(openButton, menuButton, menu);
    els.conversationList.append(item);
  }
  syncControls();
}

async function loadConversations({ quiet = false } = {}) {
  if (state.loadingConversations) return;
  state.loadingConversations = true;
  if (!quiet) renderConversationList();
  syncControls();
  try {
    const response = await api("/api/conversations?limit=30");
    const body = await response.json();
    state.conversations = body.items || [];
    if (state.sessionId) {
      const current = state.conversations.find((item) => item.sessionId === state.sessionId);
      if (current) els.conversationTitle.textContent = current.title || "当前会话";
    }
    renderConversationList();
  } catch (error) {
    els.conversationList.replaceChildren();
    els.conversationListState.textContent = `历史会话读取失败：${error.message}`;
    els.conversationListState.hidden = false;
  } finally {
    state.loadingConversations = false;
    syncControls();
  }
}

function createQuickButton(label, message) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "quick-prompt";
  button.textContent = label;
  button.addEventListener("click", () => {
    els.messageInput.value = message;
    els.messageInput.focus();
  });
  return button;
}

function showWelcome(title = "从一段真实表达开始") {
  els.messages.replaceChildren();
  const empty = document.createElement("div");
  empty.className = "empty student-welcome";
  const heading = document.createElement("strong");
  heading.textContent = title;
  const copy = document.createElement("p");
  copy.textContent = "无论是学习规划、校园办事、发展选择，还是情绪与关系困扰，都可以从现在最需要解决的一件事说起。";
  const prompts = document.createElement("div");
  prompts.className = "quick-prompts";
  prompts.append(
    createQuickButton("学习计划", "帮我根据这学期的课程和截止时间制定一份学习计划。"),
    createQuickButton("校园办事", "我想了解调宿申请要怎么办理，需要准备什么？"),
    createQuickButton("升学就业", "我在考研、就业和实习之间拿不定主意，帮我梳理一下。"),
    createQuickButton("压力倾诉", "我最近压力很大，晚上总是睡不着，想和你聊聊。")
  );
  empty.append(heading, copy, prompts);
  els.messages.append(empty);
}

function addMessage(role, content, { renderMarkdown = false } = {}) {
  const welcome = els.messages.querySelector(".student-welcome");
  if (welcome) welcome.remove();
  const row = document.createElement("article");
  row.className = `message ${role}`;
  const label = document.createElement("div");
  label.className = "message-role";
  label.textContent = role === "user" ? "我" : BRAND.assistant;
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.textContent = content;
  if (role === "assistant" && renderMarkdown) {
    renderAssistantMarkdown(bubble, content);
  }
  row.append(label, bubble);
  els.messages.append(row);
  els.messages.scrollTop = els.messages.scrollHeight;
  return bubble;
}

async function openConversation(sessionId) {
  if (isBusy() || sessionId === state.sessionId) return;
  closeConversationMenus();
  state.loadingMessages = true;
  setPill(els.sessionBadge, "LOADING", "warn");
  syncControls();
  try {
    const response = await api(`/api/conversations/${encodeURIComponent(sessionId)}`);
    const conversation = await response.json();
    els.messages.replaceChildren();
    for (const message of conversation.messages || []) {
      const role = (message.role || "").toUpperCase() === "USER" ? "user" : "assistant";
      addMessage(role, message.content || "", { renderMarkdown: role === "assistant" });
    }
    if (!(conversation.messages || []).length) showWelcome("继续这段会话");
    state.sessionId = conversation.sessionId;
    els.conversationTitle.textContent = conversation.title || "当前会话";
    setPill(els.sessionBadge, "READY");
    renderConversationList();
    closeHistoryDrawer();
  } catch (error) {
    setPill(els.sessionBadge, "ERROR", "danger");
    showNotice(`会话读取失败：${error.message}`, "danger");
    await loadConversations({ quiet: true });
  } finally {
    state.loadingMessages = false;
    syncControls();
  }
}

function parseSse(buffer, onEvent) {
  const normalized = buffer.replace(/\r\n/g, "\n");
  const parts = normalized.split("\n\n");
  const rest = parts.pop();
  for (const part of parts) {
    const dataLine = part.split("\n").find((line) => line.startsWith("data: "));
    if (!dataLine) continue;
    try {
      onEvent(JSON.parse(dataLine.slice(6)));
    } catch {
      // Ignore malformed partial events; the stream can continue.
    }
  }
  return rest;
}

function wait(milliseconds) {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

async function consumeChatStream(response, assistant) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let terminalStatus = null;
  let completionVerified = false;
  const handleEvent = (eventData) => {
    if (eventData.requestId) state.requestId = eventData.requestId;
    if (eventData.turnId) state.turnId = eventData.turnId;
    if (eventData.sessionId) {
      state.sessionId = eventData.sessionId;
      els.conversationTitle.textContent = "当前会话";
    }
    if (eventData.type === "meta") saveActiveTurn();
    if (eventData.type === "snapshot") {
      assistant.textContent = eventData.content || "";
      els.messages.scrollTop = els.messages.scrollHeight;
    }
    if (eventData.type === "error") {
      terminalStatus = eventData.status || "FAILED";
      const message = eventData.message || "回答未完整生成，以上内容可能不完整，请重试。";
      if (!assistant.textContent.trim()) assistant.textContent = message;
      showNotice(message, "danger");
    }
    if (eventData.type === "done") {
      terminalStatus = eventData.status || "FAILED";
      completionVerified = eventData.completionVerified === true;
      if (terminalStatus === "COMPLETED" && completionVerified) {
        renderAssistantMarkdown(assistant, assistant.textContent);
      } else if (!assistant.textContent.trim()) {
        assistant.textContent = "本轮回答未完成，请重试。";
      }
    }
  };
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    buffer = parseSse(buffer, handleEvent);
  }
  buffer += decoder.decode();
  if (buffer.trim()) parseSse(`${buffer}\n\n`, handleEvent);
  if (!terminalStatus) throw new Error("连接提前中断");
  return { status: terminalStatus, completionVerified };
}

async function reconnectActiveTurn(assistant) {
  const delays = [500, 1000, 2000];
  let lastError = null;
  for (const delay of delays) {
    await wait(delay);
    try {
      const response = await api(`/api/chat/turns/${encodeURIComponent(state.requestId)}/stream`);
      return await consumeChatStream(response, assistant);
    } catch (error) {
      lastError = error;
    }
  }
  throw lastError || new Error("连接中断");
}

async function sendMessage(event) {
  event.preventDefault();
  if (isBusy()) return;
  const message = els.messageInput.value.trim();
  if (!message) return;
  state.sending = true;
  syncControls();
  setPill(els.sessionBadge, "THINKING", "warn");
  els.messageInput.value = "";
  addMessage("user", message);
  const assistant = addMessage("assistant", "");
  state.requestId = crypto.randomUUID();
  state.turnId = null;
  saveActiveTurn();

  try {
    let status;
    try {
      const response = await api("/api/chat/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ requestId: state.requestId, sessionId: state.sessionId, message })
      });
      status = await consumeChatStream(response, assistant);
    } catch {
      status = await reconnectActiveTurn(assistant);
    }
    if (status.status === "COMPLETED" && status.completionVerified) {
      clearActiveTurn();
      setPill(els.sessionBadge, "DONE", "ok");
      await loadConversations({ quiet: true });
    } else {
      if (status.status === "FAILED" || status.status === "INTERRUPTED") clearActiveTurn();
      setPill(els.sessionBadge, "ERROR", "danger");
    }
  } catch (error) {
    if (!assistant.textContent) assistant.textContent = "连接中断，可重新连接";
    setPill(els.sessionBadge, "ERROR", "danger");
  } finally {
    state.sending = false;
    syncControls();
  }
}

function resetSession() {
  if (isBusy()) return;
  state.sessionId = null;
  clearActiveTurn();
  els.conversationTitle.textContent = "新会话";
  showWelcome();
  setPill(els.sessionBadge, "READY");
  closeConversationMenus();
  renderConversationList();
  closeHistoryDrawer();
  els.messageInput.focus();
}

function requestArchive(sessionId) {
  if (isBusy()) return;
  closeConversationMenus();
  state.archivingSessionId = sessionId;
  syncControls();
  els.archiveDialog.showModal();
}

function cancelArchive() {
  els.archiveDialog.close();
  state.archivingSessionId = null;
  syncControls();
}

async function confirmArchive() {
  const sessionId = state.archivingSessionId;
  if (!sessionId) return;
  els.confirmArchive.disabled = true;
  els.confirmArchive.textContent = "正在归档...";
  try {
    await api(`/api/conversations/${encodeURIComponent(sessionId)}/archive`, { method: "POST" });
    state.conversations = state.conversations.filter((item) => item.sessionId !== sessionId);
    if (state.sessionId === sessionId) {
      clearActiveTurn();
      state.sessionId = null;
      els.conversationTitle.textContent = "新会话";
      showWelcome("会话已归档");
      setPill(els.sessionBadge, "READY");
    }
    els.archiveDialog.close();
    showNotice("会话已归档，并从学生端历史记录中移除。");
    renderConversationList();
  } catch (error) {
    showNotice(`归档失败：${error.message}`, "danger");
  } finally {
    state.archivingSessionId = null;
    els.confirmArchive.textContent = "确认归档";
    syncControls();
  }
}

function openHistoryDrawer() {
  els.conversationDrawer.classList.add("open");
  els.historyBackdrop.hidden = false;
  document.body.classList.add("drawer-open");
  els.closeHistory.focus();
}

function closeHistoryDrawer() {
  els.conversationDrawer.classList.remove("open");
  els.historyBackdrop.hidden = true;
  document.body.classList.remove("drawer-open");
}

function logout() {
  clearAuth();
  window.location.assign("/");
}

els.chatForm.addEventListener("submit", sendMessage);
els.messageInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    els.chatForm.requestSubmit();
  }
});
els.newSession.addEventListener("click", resetSession);
els.refreshConversations.addEventListener("click", () => loadConversations());
els.openMemories.addEventListener("click", openMemoryDialog);
els.closeMemories.addEventListener("click", () => els.memoryDialog.close());
els.switchAccount.addEventListener("click", logout);
els.openHistory.addEventListener("click", openHistoryDrawer);
els.closeHistory.addEventListener("click", closeHistoryDrawer);
els.historyBackdrop.addEventListener("click", closeHistoryDrawer);
els.cancelArchive.addEventListener("click", cancelArchive);
els.confirmArchive.addEventListener("click", confirmArchive);
els.archiveDialog.addEventListener("cancel", (event) => {
  event.preventDefault();
  cancelArchive();
});
els.memoryDialog.addEventListener("cancel", (event) => {
  event.preventDefault();
  els.memoryDialog.close();
});
document.addEventListener("click", (event) => {
  if (!event.target.closest(".conversation-item")) closeConversationMenus();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && els.conversationDrawer.classList.contains("open")) {
    closeHistoryDrawer();
  }
});

showWelcome();
syncControls();
checkHealth();
loadProfile().then(async (profile) => {
  if (!profile) return;
  await Promise.all([loadAgentStatus(), loadConversations()]);
  const active = readActiveTurn();
  if (active?.requestId) {
    state.requestId = active.requestId;
    state.turnId = active.turnId || null;
    state.sessionId = active.sessionId || state.sessionId;
    state.sending = true;
    syncControls();
    const assistant = addMessage("assistant", "");
    try {
      const status = await reconnectActiveTurn(assistant);
      if (["COMPLETED", "FAILED", "INTERRUPTED"].includes(status.status)) clearActiveTurn();
      setPill(
        els.sessionBadge,
        status.status === "COMPLETED" ? "DONE" : "ERROR",
        status.status === "COMPLETED" ? "ok" : "danger"
      );
      await loadConversations({ quiet: true });
    } catch {
      if (!assistant.textContent) assistant.textContent = "连接中断，可重新连接";
      setPill(els.sessionBadge, "ERROR", "danger");
    } finally {
      state.sending = false;
      syncControls();
    }
  }
});
