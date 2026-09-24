const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");

// 执行页面实际使用的 SSE 解析器和渲染处理函数，不依赖浏览器或后端。
const source = readFileSync(path.join(__dirname, "../app/static/student.js"), "utf8");
const functions = source.slice(source.indexOf("function parseSse("),
  source.indexOf("async function reconnectActiveTurn("));

async function consume(events) {
  const notices = [];
  const rendered = [];
  const context = vm.createContext({
    TextDecoder, state: {}, els: { messages: {}, conversationTitle: {} },
    saveActiveTurn() {},
    showNotice(message) { notices.push(message); },
    renderAssistantMarkdown(element, text) { rendered.push(text); },
  });
  vm.runInContext(functions, context);
  const bytes = new TextEncoder().encode(events.map(event =>
    `data: ${JSON.stringify(event)}\n\n`).join(""));
  let offset = 0;
  const response = { body: { getReader: () => ({ async read() {
    if (offset === bytes.length) return { done: true };
    const value = bytes.slice(offset, offset + 7);
    offset += value.length;
    return { done: false, value };
  } }) } };
  const bubble = { textContent: "" };
  const status = await context.consumeChatStream(response, bubble);
  return { bubble, notices, rendered, status };
}

test("内部错误显示在空气泡中，且不会作为成功回答渲染", async () => {
  const result = await consume([
    { type: "snapshot", content: "" },
    { type: "error", status: "FAILED", message: "处理消息时发生内部错误，本轮未完成。" },
    { type: "done", status: "FAILED", completionVerified: false },
  ]);
  assert.equal(result.bubble.textContent, "处理消息时发生内部错误，本轮未完成。");
  assert.equal(result.notices.length, 1);
  assert.equal(result.rendered.length, 0);
  assert.equal(result.status.status, "FAILED");
});

test("失败时保留已有正文并显示错误提示", async () => {
  const result = await consume([
    { type: "snapshot", content: "已有部分内容" },
    { type: "error", message: "本轮未完成" },
    { type: "done", status: "FAILED" },
  ]);
  assert.equal(result.bubble.textContent, "已有部分内容");
  assert.deepEqual(result.notices, ["本轮未完成"]);
  assert.equal(result.rendered.length, 0);
});

test("只有失败终态事件时也不留下空气泡", async () => {
  const result = await consume([{ type: "done", status: "INTERRUPTED" }]);
  assert.equal(result.bubble.textContent, "本轮回答未完成，请重试。");
});

test("成功的澄清回复仍正常显示", async () => {
  const result = await consume([
    { type: "snapshot", content: "请补充目标日期。" },
    { type: "done", status: "COMPLETED", completionVerified: true },
  ]);
  assert.equal(result.bubble.textContent, "请补充目标日期。");
  assert.deepEqual(result.rendered, ["请补充目标日期。"]);
  assert.equal(result.notices.length, 0);
});
