// Browser check using real application assets and synthetic, offline API data.
// Requires Playwright; KDS_TEST_CHROMIUM may point to an existing Chromium binary.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");

async function main() {
  const browser = await chromium.launch({ headless: true,
    ...(process.env.KDS_TEST_CHROMIUM ? { executablePath: process.env.KDS_TEST_CHROMIUM } : {}) });
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const calls = [];
    const conv = {
      id: "A", name: "水印方案讨论", agent_backend: "dsh", status: "running", can_resume: false,
      agents: [{ id: "a0", name: "小明" }, { id: "a1", name: "小红" }],
      messages: [
        { role: "agent", agent_id: "a0", speaker: "小明", round: 0, content: "已查阅资料，可以从检测准确率开始比较。", ts: "2026-09-29T03:00:05Z" },
        { role: "agent", speaker: "小红", round: 1, content: "我补充了搜索结果，也要考虑改写后的稳定性。", ts: "2026-09-29T03:00:15Z" },
      ],
      votes: [], total_output_tokens: 128, total_max_tokens: 12000, scheduling_mode: "round_robin",
      harness_log_rev: 1, harness_log_dropped: 0, harness_log_count: 3,
      harness_logs: [
        { id: "one", tool: "read", agent_id: "a0", agent_name: "小明", turn: 1, step: 1, status: "completed",
          started_at: "2026-09-29T03:00:00Z", arguments: '{"file_path":"notes/evidence.md"}', result: "第一轮资料。" },
        { id: "two", tool: "web_search", agent_id: "a1", agent_name: "小红", turn: 2, step: 1, status: "completed",
          started_at: "2026-09-29T03:00:10Z", arguments: '{"query":"LLM watermark evaluation"}',
          result: '<img src=x onerror="window.injected=true">\n这段工具输出应按原文显示。' },
        { id: "three", tool: "read", agent_id: "a0", agent_name: "小明", turn: 3, step: 1, status: "running",
          started_at: "2026-09-29T03:00:20Z", arguments: '{"file_path":"notes/comparison.md"}' },
      ],
    };
    const second = { ...conv, id: "B", name: "新的讨论", messages: [], harness_log_count: 0,
      harness_log_rev: 0, harness_logs: [], status: "paused", can_resume: true };
    await page.route("http://kds.test/**", async (route) => {
      const url = new URL(route.request().url());
      if (url.pathname.startsWith("/api/conversations/")) {
        calls.push(url.pathname + url.search);
        const snapshot = JSON.parse(JSON.stringify(url.pathname.endsWith("/B") ? second : conv));
        snapshot.harness_log_index = snapshot.harness_logs.map(({ arguments, result, ...entry }) => entry);
        if (!url.searchParams.has("tool_logs")) delete snapshot.harness_logs;
        return route.fulfill({ json: snapshot });
      }
      const assets = {
        "/": ["app/templates/index.html", "text/html"],
        "/static/js/app.js": ["app/static/js/app.js", "text/javascript"],
        "/static/css/style.css": ["app/static/css/style.css", "text/css"],
      };
      const asset = assets[url.pathname];
      return route.fulfill(asset ? { body: fs.readFileSync(path.join(__dirname, "..", asset[0])), contentType: asset[1] } : { status: 404 });
    });
    await page.goto("http://kds.test/#/chat/A");
    await page.waitForLoadState("networkidle");
    const first = page.locator('.msg[data-agent-id="a0"][data-turn="1"]');
    const secondTurn = page.locator('.msg[data-agent-id="a1"][data-turn="2"]');
    const active = page.locator('.msg[data-agent-id="a0"][data-turn="3"]');
    await active.waitFor();
    assert.equal(await page.locator("#chat-tools-toggle, #tool-log-panel").count(), 0);
    assert.equal(await page.locator(".tool-log-group[open]").count(), 0);
    assert(calls.length > 0 && calls.every((url) => !url.includes("tool_logs")));
    assert.equal(await first.locator('[data-log-id="one"]').count(), 1);
    assert.equal(await secondTurn.locator('[data-log-id="two"]').count(), 1);
    assert.equal(await active.locator('[data-log-id="three"]').count(), 1);
    assert(await first.evaluate((el) => !!(el.querySelector(".tool-log-group").compareDocumentPosition(el.querySelector(".bubble")) & Node.DOCUMENT_POSITION_FOLLOWING)));
    await active.locator(".tool-log-group > summary").click();
    await active.locator('[data-log-id="three"] summary').click();
    await page.waitForFunction(() => document.querySelector('[data-log-id="three"] pre').textContent.includes("comparison.md"));
    await secondTurn.locator(".tool-log-group > summary").click();
    await secondTurn.locator('[data-log-id="two"] summary').click();
    assert((await page.locator('[data-log-id="two"] pre').last().textContent()).includes("<img"));
    assert.equal(await page.locator("#chat-messages img").count(), 0);
    assert.equal(await page.evaluate(() => window.injected), undefined);
    conv.harness_logs[2].status = "completed";
    conv.harness_logs[2].result = "已读取对照表：共 12 页，包含检测方法与评估指标。";
    conv.harness_logs.push({ id: "four", tool: "web_fetch", agent_id: "a0", agent_name: "小明", turn: 3, step: 2,
      status: "error", arguments: '{"url":"https://example.test/paper"}', result: "请求超时，请稍后重试。",
      started_at: "2026-09-29T03:00:22Z" });
    conv.harness_log_rev++;
    conv.harness_log_count++;
    await page.locator('[data-log-id="four"]').waitFor({ state: "attached" });
    assert(await page.locator('[data-log-id="three"]').evaluate((el) => el.open));
    assert.equal(await page.locator('[data-log-id="three"] .tool-log-status').textContent(), "成功");
    conv.messages.push({ role: "agent", agent_id: "a0", speaker: "小明", round: 2,
      content: "对照表已核对，一篇补充资料暂时无法访问。", ts: "2026-09-29T03:00:25Z" });
    conv.status = "paused";
    conv.can_resume = true;
    await active.locator(".bubble").waitFor();
    assert.equal(await page.locator('[data-log-id="three"]').count(), 1, "final speech must reuse the live tool group");
    assert(await active.locator(".tool-log-group").evaluate((el) => el.open));
    assert(await active.evaluate((el) => !!(el.querySelector(".tool-log-group").compareDocumentPosition(el.querySelector(".bubble")) & Node.DOCUMENT_POSITION_FOLLOWING)));
    // Repeated turns by one role and an intervening human message stay separate.
    conv.messages.push({ role: "human", speaker: "人类", round: 3, content: "请继续核验。", ts: "2026-09-29T03:00:30Z" });
    conv.harness_logs.push({ ...conv.harness_logs[0], id: "five", turn: 5,
      started_at: "2026-09-29T03:00:35Z", result: "第三次发言的独立资料。" });
    conv.harness_log_count++;
    conv.harness_log_rev++;
    const fifth = page.locator('.msg[data-agent-id="a0"][data-turn="5"]');
    await fifth.waitFor();
    assert.equal(await fifth.locator('[data-log-id="five"]').count(), 1);
    assert.equal(await active.locator('[data-log-id="five"]').count(), 0);
    assert.equal(await page.locator(".msg.human .tool-log-group").count(), 0);
    // Keyboard toggling and refresh preserve native collapsed controls.
    await secondTurn.locator(".tool-log-group > summary").focus();
    await page.keyboard.press("Enter");
    assert.equal(await secondTurn.locator(".tool-log-group").evaluate((el) => el.open), false);
    const output = process.env.KDS_UI_SCREENSHOTS;
    if (output) {
      fs.mkdirSync(output, { recursive: true });
      await page.locator("#chat-messages").evaluate((el) => { el.scrollTop = 0; });
      await page.screenshot({ path: path.join(output, "tool-logs-inline-desktop.png"), fullPage: true });
    }
    // A live update must preserve both chat and inner result reading positions.
    conv.harness_logs[2].result = Array.from({ length: 100 }, (_, i) => `记录 ${i}`).join("\n");
    conv.harness_log_rev++;
    await page.waitForFunction(() => document.querySelector('[data-log-id="three"] pre:last-child').textContent.includes("记录 99"));
    await page.locator('[data-log-id="three"] pre').last().evaluate((el) => { el.scrollTop = 120; });
    await page.locator("#chat-messages").evaluate((el) => { el.scrollTop = 0; });
    conv.harness_logs[2].result += "\n更新已完成。";
    conv.harness_log_rev++;
    await page.waitForFunction(() => document.querySelector('[data-log-id="three"] pre:last-child').textContent.includes("更新已完成"));
    assert.equal(await page.locator("#chat-messages").evaluate((el) => el.scrollTop), 0);
    assert.equal(await page.locator('[data-log-id="three"] pre').last().evaluate((el) => el.scrollTop), 120);
    await page.reload();
    await fifth.waitFor();
    assert.equal(await page.locator(".tool-log-group[open]").count(), 0);
    await page.evaluate(() => { location.hash = "#/chat/B"; });
    await page.waitForFunction(() => document.querySelector("#chat-title").textContent === "新的讨论");
    assert.equal(await page.locator(".tool-log-group, .tool-log-item").count(), 0);
    conv.harness_logs[2].result = "对照表已核对。";
    conv.harness_log_rev++;
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto("http://kds.test/#/chat/A");
    await active.waitFor();
    await active.locator(".tool-log-group > summary").click();
    await active.locator('[data-log-id="three"] summary').click();
    await page.waitForFunction(() => document.querySelector('[data-log-id="three"] pre').textContent.includes("comparison.md"));
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    assert(await page.locator("#chat-messages").evaluate((el) => el.scrollWidth <= el.clientWidth));
    assert(await page.locator("#chat-send").evaluate((el) => el.getBoundingClientRect().right <= innerWidth));
    if (output) {
      await page.locator("#chat-messages").evaluate((el) => { el.scrollTop = 0; });
      await page.screenshot({ path: path.join(output, "tool-logs-inline-mobile.png"), fullPage: true });
    }
    assert.deepEqual(errors, []);
    console.log("inline tool logs: per-role turns, live-to-final placement, folding, scrolling, escaping and mobile passed");
  } finally {
    await browser.close();
  }
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
