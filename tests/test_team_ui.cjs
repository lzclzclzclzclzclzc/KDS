// Offline Chromium acceptance checks using the actual team page and assets.
// NODE_PATH may point to bundled Playwright; KDS_TEST_CHROMIUM selects a browser.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");

async function main() {
  const browser = await chromium.launch({ headless: true,
    ...(process.env.KDS_TEST_CHROMIUM ? { executablePath: process.env.KDS_TEST_CHROMIUM } : {}) });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 960 } });
    const errors = [], calls = [];
    page.on("pageerror", e => errors.push(e.message));
    const roles = [
      { id: "planner", name: "方案负责人", version: 1, description: "拆解工作并整合正式结果", system_prompt: "拆解任务并汇总子结果", tools: ["read"], default_budget: { single_max_tokens: 1024 } },
      { id: "verifier", name: "方案校验", version: 2, system_prompt: "独立校验方案", tools: ["read"], default_budget: { single_max_tokens: 1024 } },
    ];
    let savedTeam, run, delayedHistory;
    const teamRecords = [];
    const paperSources = [{ title: "预设论文", url: "https://example.test/paper" }];
    const catalog = { tools: [
      { name: "read", label: "读取文件", description: "读取工作区资料" },
      { name: "write", label: "写入文件", description: "保存工作区产出" },
      { name: "web_search", label: "网页搜索", description: "查询公开资料" },
    ], roles: [{ key: "academic", name: "论文规划者", description: "来自论文的规划职责", sources: paperSources,
      role: { name: "论文规划者", description: "来自论文的规划职责", system_prompt: "按论文方法规划工作", tools: ["read"], default_budget: { single_max_tokens: null }, sources: paperSources, preset_key: "academic", equivalence_key: "academic-original" } }],
      teams: [{ key: "paper-team", name: "论文团队", description: "规划与独立审查", sources: paperSources }] };
    for (let i = 1; i < 9; i++) catalog.roles.push({ key: "paper-role-" + i, name: "论文职责" + i, description: "独立职责" + i, sources: paperSources, role: { name: "论文职责" + i, system_prompt: "独立工作" + i, sources: paperSources, equivalence_key: "paper-role-" + i } });
    roles.push({ ...roles[0], id: "planner-equivalent-copy" });
    roles.push({ ...catalog.roles[0].role, id: "academic-modified", version: 3, system_prompt: "同名角色的不同实际职责", equivalence_key: "academic-modified" });
    const historyTeam = { id: "saved-history", version: 2, name: "已保存团队", shared_background: "保存版本的背景", nodes: [{ id: "history-node", name: "历史节点", role_id: "planner", role_version: 1, position: { x: 20, y: 20 } }], edges: [], sources: paperSources };
    teamRecords.push(historyTeam);
    const presetDefinition = { name: "论文团队", sources: paperSources, preset_key: "paper-team", limits: { single_max_tokens: null, summary_max_tokens: 1024, total_max_tokens: 30000, total_duration_seconds: 600, max_concurrency: 2, max_processes: 2, max_discussion_turns: 6 }, nodes: [{ id: "preset-node", name: "论文规划者", role_id: "academic", role_version: 1, position: { x: 40, y: 40 } }], edges: [] };
    const fullLogs = [{ id: "tool-1", instance_id: "inst-0", task_id: "task-0", activation_id: "act-0", tool: "read", status: "running", arguments: "notes.md", result: '<img src=x onerror="window.injected=true">工具原文' }, { id: "tool-2", instance_id: "inst-0", task_id: "task-0", activation_id: "act-0", tool: "read", status: "completed", arguments: "notes.md", result: "同参数的独立合法调用" }];
    await page.route("http://kds.test/**", async route => {
      const request = route.request(), url = new URL(request.url()), p = url.pathname;
      const body = request.postDataJSON(); calls.push({ path: p, method: request.method(), body });
      if (p === "/api/team-presets") return route.fulfill({ json: catalog });
      if (p === "/api/team-presets/teams/paper-team/preview") return route.fulfill({ json: { definition: presetDefinition, roles: [{ ...catalog.roles[0].role, id: "academic", version: 1 }] } });
      if (p === "/api/team-presets/roles/academic") { const role = { id: "academic-role", version: 1, ...catalog.roles[0].role }; roles.push(role); return route.fulfill({ json: role }); }
      if (p === "/api/team-presets/teams/paper-team") {
        const team = { ...presetDefinition, id: "preset-team", version: 1, nodes: presetDefinition.nodes.map(n => ({ ...n, role_id: "academic-role" })) };
        teamRecords.push(team); return route.fulfill({ json: team });
      }
      if (p === "/api/roles") {
        if (request.method() === "POST") { const role = { id: "created-role", version: 1, ...body }; roles.push(role); return route.fulfill({ json: role }); }
        return route.fulfill({ json: roles });
      }
      if (p.endsWith("/versions") && p.startsWith("/api/roles/")) {
        if (request.method() === "POST") { const role = roles.find(r => r.id === p.split("/")[3]); Object.assign(role, body, { version: role.version + 1 }); return route.fulfill({ json: role }); }
        return route.fulfill({ json: [{ version: 1 }, { version: 2 }] });
      }
      if (p === "/api/teams") {
        if (request.method() === "POST") { savedTeam = { id: "team-1", version: 1, ...body }; teamRecords.push(savedTeam); return route.fulfill({ json: savedTeam }); }
        return route.fulfill({ json: teamRecords });
      }
      if (p === "/api/teams/validate") return route.fulfill({ json: { valid: true, errors: [] } });
      if (p === "/api/teams/saved-history/versions") return route.fulfill({ json: [historyTeam, { ...historyTeam, version: 1 }] });
      if (p === "/api/teams/saved-history/preview") return route.fulfill({ json: { definition: { ...historyTeam, version: Number(url.searchParams.get("version") || 2) }, head_version: 2, roles: [{ ...roles[0], name: "历史负责人", system_prompt: "冻结的完整角色设定" }] } });
      if (p === "/api/team-runs/run-1/preview") return route.fulfill({ json: { definition: run.definition, roles: run.agents.map(a => a.role), run_id: run.id, team_id: run.team_id, team_version: run.team_version } });
      if (p.startsWith("/api/teams/")) return route.fulfill({ json: teamRecords.find(t => t.id === p.split("/")[3]) });
      if (p === "/api/team-runs") {
        if (request.method() === "POST") {
          run = { id: "run-1", name: savedTeam.name, team_id: "team-1", team_version: 1, definition: savedTeam, status: "running", event_seq: 0,
            goal: body.goal, limits: body.limits, usage: { output_tokens: 123 },
            agents: savedTeam.nodes.map((n, i) => ({ id: "inst-" + i, node_id: n.id, name: n.name, role: roles.find(r => r.id === n.role_id), status: i === 0 ? "waiting" : "idle", current_task_id: i === 0 ? "task-0" : null })),
            tasks: [{ id: "task-0", instance_id: "inst-0", status: "waiting", input: "校验设计", wait_for: ["task-1"] }, { id: "task-1", instance_id: "inst-1", parent_task_id: "task-0", status: "queued", input: "审查边界" }],
            rooms: [{ id: "room-1", name: "设计群聊", members: ["inst-0", "inst-1", "inst-2"] }], messages: [],
            tool_logs: fullLogs.map(({ arguments, result, ...log }) => log),
            whiteboard: { format: "html", content: '<script>parent.injected=true</script><h1>设计交付</h1>', rev: 1 } };
          return route.fulfill({ json: run });
        }
        return route.fulfill({ json: run ? [run] : [] });
      }
      if (p === "/api/team-runs/run-1") return route.fulfill({ json: run });
      if (p.endsWith("/tool-logs")) return route.fulfill({ json: fullLogs });
      if (p.endsWith("/events")) return route.fulfill({ json: [] });
      if (p.endsWith("/messages") && request.method() === "GET") {
        if (p.includes("/agents/inst-0/") && delayedHistory) return delayedHistory(route);
        return route.fulfill({ json: { messages: run.messages.filter(m => p.includes("/rooms/") ? m.room_id === "room-1" : m.target_id === p.split("/")[5]), has_more: false } });
      }
      if (p.endsWith("/messages") && request.method() === "POST") {
        const message = { id: "human-" + run.messages.length, role: "human", ...body, status: "queued" }; run.messages.push(message);
        return route.fulfill({ json: { queued: true, message } });
      }
      if (p.endsWith("/pause")) { run.status = "paused"; return route.fulfill({ json: run }); }
      if (p.endsWith("/limits")) { Object.assign(run.limits, body); return route.fulfill({ json: run }); }
      if (p.endsWith("/resume")) { run.status = "running"; return route.fulfill({ json: run }); }
      if (p.endsWith("/finalize")) { run.status = "completed"; return route.fulfill({ json: run }); }
      const assets = { "/teams": ["app/templates/team.html", "text/html"], "/static/js/team.js": ["app/static/js/team.js", "text/javascript"], "/static/css/team.css": ["app/static/css/team.css", "text/css"] };
      const asset = assets[p]; return route.fulfill(asset ? { body: fs.readFileSync(path.join(__dirname, "..", asset[0])), contentType: asset[1] } : { status: 404 });
    });
    await page.goto("http://kds.test/teams"); await page.waitForLoadState("networkidle");
    assert.equal(await page.locator(".role-card[data-preset-role]").count(), 9, "all nine paper roles appear directly");
    assert.equal(await page.locator(".role-card").count(), 12, "equivalent persisted copies merge, changed same-name roles stay separate");
    assert.equal(await page.locator('[data-action="role-presets"]').count(), 0);
    assert.equal(calls.filter(c => c.method === "POST").length, 0, "opening the library does not seed roles or teams");
    const plannerCard = page.locator('.role-card').filter({ has: page.locator('[data-role-edit="planner"]') });
    assert.equal(await plannerCard.locator("p").textContent(), roles[0].description);
    assert.equal(await plannerCard.locator("p").getAttribute("title"), roles[0].system_prompt);
    assert.equal(await page.locator('.role-card').filter({ has: page.locator('[data-role-edit="verifier"]') }).locator("p").textContent(), roles[1].system_prompt);
    await page.locator('[data-action="role-new"]').click();
    assert.equal(await page.locator('input[name="tools"][type="text"]').count(), 0);
    assert.equal(await page.locator('input[name="tools"][type="checkbox"]').count(), catalog.tools.length);
    await page.locator('input[name="name"]').fill("无限工具角色");
    await page.locator('textarea[name="system_prompt"]').fill("可用有限工具，单次输出不限。");
    await page.locator("#tools-all").check();
    await page.locator('input[name="tools"][value="write"]').uncheck();
    assert(await page.locator("#tools-all").evaluate(el => el.indeterminate));
    await page.locator("#tools-all").check();
    assert.equal(await page.locator('input[name="single_max_tokens"]').inputValue(), "");
    await page.locator("#dialog-submit").click();
    await page.locator('[data-role-edit="created-role"]').waitFor();
    const createdBody = calls.find(c => c.path === "/api/roles" && c.method === "POST").body;
    assert.equal(createdBody.default_budget.single_max_tokens, null);
    assert.deepEqual(createdBody.tools.sort(), catalog.tools.map(t => t.name).sort());
    await page.locator('[data-role-edit="created-role"]').click();
    await page.locator('input[name="single_max_tokens"]').fill("0");
    await page.locator("#dialog-submit").click();
    await page.waitForFunction(() => !document.querySelector("#dialog-form"));
    assert.equal(calls.filter(c => c.path === "/api/roles/created-role/versions").at(-1).body.default_budget.single_max_tokens, null);
    await page.locator('[data-role-edit="created-role"]').click();
    await page.locator('input[name="single_max_tokens"]').fill("256"); await page.locator("#dialog-submit").click();
    await page.waitForFunction(() => !document.querySelector("#dialog-form"));
    assert.equal(calls.filter(c => c.path === "/api/roles/created-role/versions").at(-1).body.default_budget.single_max_tokens, 256);
    assert.equal(await page.locator('[data-preset-role="academic"] .preset-sources a').getAttribute("href"), paperSources[0].url);
    await page.locator('[data-role-edit="preset:academic"]').click();
    await page.locator('textarea[name="system_prompt"]').waitFor();
    await page.locator("#dialog-submit").click(); await page.waitForFunction(() => !document.querySelector("#dialog-form"));
    assert.deepEqual(calls.find(c => c.path === "/api/roles/academic-role/versions").body.sources, paperSources);
    assert.equal(calls.find(c => c.path === "/api/roles/academic-role/versions").body.description, catalog.roles[0].role.description);
    roles.push({ ...roles.find(r => r.id === "academic-role"), id: "academic-equivalent-copy", version: 4 });
    await page.reload(); await page.waitForLoadState("networkidle");
    assert.equal(await page.locator('[data-preset-role="academic"]').count(), 1);
    await page.locator('[data-role-add="academic-role"]').click();
    await page.locator('[data-role-add="academic-role"]').click();
    assert.equal(calls.filter(c => c.path === "/api/team-presets/roles/academic").length, 1, "equivalent persisted roles are reused for each instance");
    assert.equal(await page.locator('#graph-nodes .graph-node').count(), 2);
    const draftBeforePreview = await page.evaluate(() => localStorage.getItem("kds-team-draft-v1")), writesBeforePreview = calls.filter(c => c.method === "POST").length;
    await page.locator('[data-action="team-presets"]').click();
    await page.locator('[data-preset-preview="paper-team"]').click();
    await page.locator('#preview-topology [data-preview-node]').waitFor();
    assert((await page.locator('#preview-node-details').textContent()).includes("按论文方法规划工作"));
    assert.equal(await page.locator('#graph-nodes .graph-node').count(), 2);
    assert.equal(await page.evaluate(() => localStorage.getItem("kds-team-draft-v1")), draftBeforePreview);
    assert.equal(calls.filter(c => c.method === "POST").length, writesBeforePreview);
    await page.locator('[data-dialog-close]').first().click();
    await page.locator('#team-picker').selectOption("saved-history");
    await page.locator('#preview-version').waitFor();
    await page.locator('#preview-version').selectOption("1");
    await page.waitForFunction(() => document.querySelector('.preview-notice').textContent.includes("v1"));
    assert((await page.locator('#preview-node-details').textContent()).includes("冻结的完整角色设定"));
    assert.equal(await page.evaluate(() => localStorage.getItem("kds-team-draft-v1")), draftBeforePreview);
    assert.equal(calls.filter(c => c.method === "POST").length, writesBeforePreview);
    await page.locator('#preview-use').click();
    assert.equal(await page.locator('#graph-nodes .graph-node').count(), 1);
    assert.equal(await page.evaluate(() => JSON.parse(localStorage.getItem("kds-team-draft-v1")).teamId), null, "loading a historical version makes an independent draft");
    await page.locator('[data-action="team-presets"]').click();
    await page.locator('[data-preset-preview="paper-team"]').click();
    await page.locator('#preview-use').click();
    await page.locator('[data-node="preset-node"]').waitFor();
    assert.equal(await page.locator('#inspector .preset-sources a').getAttribute("href"), paperSources[0].url);
    await page.locator('[data-action="start"]').click();
    assert.equal(await page.locator('input[name="total_max_tokens"]').inputValue(), "30000");
    assert.equal(await page.locator('input[name="single_max_tokens"]').inputValue(), "");
    assert.equal(await page.locator('input[name="summary_max_tokens"]').inputValue(), "1024");
    await page.locator('[data-dialog-close]').first().click();
    await page.locator('[data-action="new"]').click();
    await page.locator('#team-canvas').waitFor();
    const leftHandle = page.locator('[data-panel-resize="left"]'), rightHandle = page.locator('[data-panel-resize="right"]');
    const handleBox = await leftHandle.boundingBox(), oldLeftWidth = await page.locator("#role-sidebar").evaluate(el => el.clientWidth);
    await page.mouse.move(handleBox.x + 4, handleBox.y + 100); await page.mouse.down(); await page.mouse.move(handleBox.x + 64, handleBox.y + 100); await page.mouse.up();
    assert.equal(await page.locator("#role-sidebar").evaluate(el => el.clientWidth), oldLeftWidth + 60);
    const oldRightWidth = Number(await rightHandle.getAttribute("aria-valuenow"));
    await rightHandle.focus(); await page.keyboard.press("ArrowLeft");
    assert.equal(Number(await rightHandle.getAttribute("aria-valuenow")), oldRightWidth + 10);
    await page.reload(); await page.locator('#team-canvas').waitFor();
    assert.equal(await page.locator("#role-sidebar").evaluate(el => el.clientWidth), oldLeftWidth + 60, "panel preferences survive reloading");
    await page.locator('[data-role-add="planner"]').click();
    await page.locator('[data-role-add="verifier"]').click();
    await page.locator('[data-role-add="verifier"]').click();
    assert.equal(await page.locator("#graph-nodes .graph-node").count(), 3, "roles must be reusable as separate nodes");
    const ids = await page.locator("#graph-nodes [data-node]").evaluateAll(els => els.map(e => e.dataset.node));
    await page.locator('[data-mode="task"]').click();
    // Node positions can overlap when added; connect through keyboard for accessibility.
    for (const target of [ids[0], ids[1]]) { await page.locator(`[data-node="${target}"]`).focus(); await page.keyboard.press("Enter"); }
    await page.locator(`[data-node="${ids[0]}"]`).focus(); await page.keyboard.press("Enter");
    await page.locator(`[data-node="${ids[2]}"]`).focus(); await page.keyboard.press("Enter");
    assert.equal(await page.locator(".edge-path.task").count(), 2);
    await page.locator('[data-mode="room"]').click();
    for (const target of [ids[0], ids[1]]) { await page.locator(`[data-node="${target}"]`).focus(); await page.keyboard.press("Enter"); }
    assert.equal(await page.locator(".edge-path.room").count(), 0, "a task-connected node pair cannot also have a room edge");
    await page.keyboard.press("Escape"); await page.locator('[data-mode="room"]').click();
    for (const target of [ids[1], ids[2]]) { await page.locator(`[data-node="${target}"]`).focus(); await page.keyboard.press("Enter"); }
    assert((await page.locator("#validation").textContent()).includes("1 个群聊"));
    await page.locator('[data-action="layout"]').click();
    await page.locator('[data-mode="select"]').click();
    const before = await page.locator(`[data-node="${ids[0]}"]`).evaluate(el => [el.style.left, el.style.top]);
    const box = await page.locator(`[data-node="${ids[0]}"]`).boundingBox();
    await page.mouse.move(box.x + 30, box.y + 30); await page.mouse.down(); await page.mouse.move(box.x + 80, box.y + 60, { steps: 4 }); await page.mouse.up();
    const moved = await page.locator(`[data-node="${ids[0]}"]`).evaluate(el => [el.style.left, el.style.top]);
    assert.notDeepEqual(moved, before, "node dragging must persist graph coordinates");
    await page.locator('[data-action="undo"]').click();
    assert.deepEqual(await page.locator(`[data-node="${ids[0]}"]`).evaluate(el => [el.style.left, el.style.top]), before);
    await page.locator('[data-action="redo"]').click();
    assert.deepEqual(await page.locator(`[data-node="${ids[0]}"]`).evaluate(el => [el.style.left, el.style.top]), moved);
    const validation = await page.evaluate(() => {
      const d = { nodes: [{ id: "a" }, { id: "b" }, { id: "c" }], edges: [{ id: "one", source: "a", target: "b", type: "task" }, { id: "two", source: "b", target: "c", type: "room" }, { id: "three", source: "c", target: "a", type: "room" }] };
      const mixed = window.KDSTeam.validateTopology(d); d.edges.push({ id: "cycle", source: "b", target: "a", type: "task" });
      const duplicate = window.KDSTeam.validateTopology({ nodes: [{ id: "a" }, { id: "b" }], edges: [{ source: "a", target: "b", type: "task" }, { source: "b", target: "a", type: "room" }] });
      return { mixed, cycle: window.KDSTeam.validateTopology(d), duplicate };
    });
    assert(validation.mixed.valid && validation.mixed.rooms.length === 1, "mixed task/room cycles are legal");
    assert(!validation.cycle.valid, "directed task cycles must be rejected");
    assert(!validation.duplicate.valid, "reversed endpoints still identify the same node pair");
    await page.locator('[data-action="start"]').click(); await page.locator('textarea[name="goal"]').fill("离线浏览器验收");
    assert.equal(await page.locator('input[name="summary_max_tokens"]').inputValue(), "1024");
    assert((await page.locator('input[name="summary_max_tokens"]').locator('..').textContent()).includes("模型思考消耗"));
    await page.locator('input[name="total_max_tokens"]').fill("1024");
    await page.locator("#dialog-submit").click();
    await page.waitForFunction(() => document.querySelector("#dialog-error").textContent.includes("必须低于总输出"));
    assert.equal(calls.filter(c => c.path === "/api/team-runs" && c.method === "POST").length, 0);
    await page.locator('input[name="total_max_tokens"]').fill("20000");
    await page.locator('input[name="summary_max_tokens"]').fill("1536");
    await page.locator("#dialog-submit").click(); await page.locator("#run-topology").waitFor();
    const runLeftHandle = page.locator('[data-panel-resize="left"]'), runRightHandle = page.locator('[data-panel-resize="right"]');
    await runLeftHandle.focus(); await page.keyboard.press("Shift+ArrowRight");
    await runRightHandle.focus(); await page.keyboard.press("ArrowLeft");
    const panels = await page.evaluate(() => JSON.parse(localStorage.getItem("kds-team-panels-v1")));
    assert(panels.edit.left !== panels.run.left && panels.edit.right !== panels.run.right, "editor and observer retain separate widths");
    assert.equal(savedTeam.nodes.filter(n => n.role_id === "verifier").length, 2);
    assert.equal(savedTeam.nodes[1].role_version, 2);
    const runPayload = calls.find(c => c.path === "/api/team-runs" && c.method === "POST").body;
    assert(runPayload.entry_node_ids.length === 1 && runPayload.request_id);
    assert.equal(runPayload.limits.single_max_tokens, null);
    assert.equal(runPayload.limits.summary_max_tokens, 1536);
    await page.locator('[data-action="limits"]').click();
    assert.equal(await page.locator('input[name="summary_max_tokens"]').inputValue(), "1536");
    await page.locator('input[name="total_max_tokens"]').fill("1536");
    await page.locator("#dialog-submit").click();
    await page.waitForFunction(() => document.querySelector("#dialog-error").textContent.includes("必须低于总输出"));
    assert.equal(calls.filter(c => c.path.endsWith("/limits")).length, 0);
    await page.locator('input[name="total_max_tokens"]').fill("0");
    await page.locator('input[name="summary_max_tokens"]').fill("2048");
    await page.locator("#dialog-submit").click();
    await page.waitForFunction(() => !document.querySelector("#dialog-form"));
    const changedLimits = calls.find(c => c.path.endsWith("/limits")).body;
    assert.equal(changedLimits.total_max_tokens, null, "duration-only limits allow a summary reserve");
    assert.equal(changedLimits.summary_max_tokens, 2048);
    const writesBeforeRunPreview = calls.filter(c => c.method === "POST").length;
    await page.locator('[data-action="runs"]').first().click();
    await page.locator('[data-run-preview="run-1"]').click();
    await page.locator('#preview-topology [data-preview-node]').first().waitFor();
    assert.equal(await page.locator('#preview-topology [data-preview-node]').count(), savedTeam.nodes.length);
    assert.equal(calls.filter(c => c.method === "POST").length, writesBeforeRunPreview, "history preview does not create or activate a run");
    await page.locator('[data-dialog-close]').first().click();
    await page.locator('.observer-nav [data-view="agent"][data-view-id="inst-0"]').click();
    await page.locator('[data-log="tool-1"]').waitFor();
    assert.equal(await page.locator('[data-log="tool-2"]').count(), 1, "identical arguments with distinct call IDs remain separate tool calls");
    assert.equal(calls.filter(c => c.path.endsWith("/tool-logs")).length, 0, "tool bodies should only load when expanded");
    await page.locator('[data-log="tool-1"] summary').click();
    await page.waitForFunction(() => document.querySelector('[data-log="tool-1"] pre').textContent.includes("notes.md"));
    assert((await page.locator('[data-log="tool-1"] pre').last().textContent()).includes("<img"));
    assert.equal(await page.locator("#view-content img").count(), 0);
    assert.equal(await page.evaluate(() => window.injected), undefined);
    await page.locator('#message-form textarea').fill("一条补充信息"); await page.locator('#message-form button').click();
    await page.waitForFunction(() => [...document.querySelectorAll(".queued-label")].some(e => e.textContent.includes("待下次激活")));
    const input = calls.find(c => c.path.endsWith("/messages") && c.method === "POST").body;
    assert.equal(input.target_type, "agent"); assert.equal(input.target_id, "inst-0"); assert(input.request_id);
    assert.equal(run.tasks.length, 2, "human supplemental messages cannot create tasks");
    await page.waitForTimeout(1800);
    assert(await page.locator('[data-log="tool-1"]').evaluate(el => el.open), "polling must preserve expanded logs");
    fullLogs[0].status = "completed"; fullLogs[0].finished_at = "2026-10-02T10:00:00Z";
    fullLogs[0].result += "已完成更新";
    run.tool_logs[0].status = "completed"; run.tool_logs[0].finished_at = fullLogs[0].finished_at;
    await page.waitForFunction(() => document.querySelector('[data-log="tool-1"]').textContent.includes("已完成更新"));
    assert.equal(await page.getByRole("button", { name: /取消任务|派发任务|手动派/ }).count(), 0);
    // A late response from another agent must not replace the current room view.
    let release;
    delayedHistory = route => new Promise(resolve => { release = () => route.fulfill({ json: [{ id: "late", content: "迟到的私有记录" }] }).then(resolve); });
    await page.locator('.observer-nav [data-view="agent"][data-view-id="inst-1"]').click();
    await page.locator('.observer-nav [data-view="agent"][data-view-id="inst-0"]').click();
    await page.waitForTimeout(100);
    await page.locator('.observer-nav [data-view="room"]').click(); await page.waitForFunction(() => document.querySelector("#view-header h2").textContent === "设计群聊");
    assert(release); release(); await page.waitForTimeout(150);
    assert.equal(await page.locator("#view-header h2").textContent(), "设计群聊");
    assert(!(await page.locator("#view-content").textContent()).includes("迟到的私有记录"));
    delayedHistory = null;
    await page.locator('[data-view="artifacts"]').click();
    assert.equal(await page.locator("iframe.artifact-frame").getAttribute("sandbox"), "");
    await page.waitForTimeout(150); assert.equal(await page.evaluate(() => window.injected), undefined);
    await page.locator('[data-action="pause"]').click();
    await page.locator('#run-status .status-paused').waitFor();
    run.paused_reason = "error"; run.tasks[1].status = "failed"; run.tasks[1].error = "明确拒绝：工具权限不足";
    await page.locator('.observer-nav [data-view="overview"]').click();
    await page.waitForFunction(() => document.querySelector('.pause-task-error')?.textContent.includes("工具权限不足"));
    assert((await page.locator('.pause-task-error').textContent()).includes("方案校验"));
    assert(await page.locator('[data-action="pause"]').isDisabled());
    assert(!(await page.locator('[data-action="resume"]').isDisabled()));
    await page.locator('[data-action="resume"]').click();
    await page.locator('#run-status .status-running').waitFor();
    await page.locator('[data-action="finalize"]').click(); await page.locator('input[name="skip_summary"]').check(); await page.locator("#dialog-submit").click();
    await page.locator('.observer-nav [data-view="agent"][data-view-id="inst-0"]').click();
    await page.locator(".read-only").waitFor(); assert.equal(await page.locator("#message-form").count(), 0);
    assert.equal(calls.find(c => c.path.endsWith("/finalize")).body.summarize, false);
    assert.deepEqual(errors, []);
    console.log("Team UI acceptance passed: canvas, reusable versions, topology validation, scoped views, queued input, safe logs, stale response, controls, read-only completion.");
  } finally { await browser.close(); }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
