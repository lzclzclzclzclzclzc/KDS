(function () {
  "use strict";

  const app = document.getElementById("team-app");
  const dialogs = document.getElementById("team-dialogs");
  const NODE_W = 178, NODE_H = 108;
  const STORAGE_KEY = "kds-team-draft-v1";
  const PANEL_STORAGE_KEY = "kds-team-panels-v1";
  const labels = { idle: "待命", queued: "排队", ready: "就绪", running: "执行中", active: "执行中", waiting: "等待子任务", waiting_children: "等待子任务", paused: "已暂停", completed: "已完成", succeeded: "已交付", failed: "失败", error: "出错", uncertain: "待核对", cancelled: "已取消", cancelling: "收尾中", pending: "待处理", consumed: "已读取" };
  const pauseLabels = { manual: "人工暂停", limit: "达到运行上限", budget: "达到预算上限", tasks_finished: "所有任务已交付", uncertain: "外部调用结果待核对", error: "执行出错", startup_recovery: "服务重启后等待继续", recovery: "服务重启后等待继续", activation_limit: "达到单任务激活上限" };
  const eventLabels = { run_created: "团队运行已启动", task_queued: "任务已排队", discussion_created: "有限群聊讨论已创建", agent_spawned: "动态子 Agent 已创建", command_accepted: "编排请求已接收", task_finished: "任务结束并回传结果", run_paused: "整场运行已暂停", run_resumed: "整场运行已继续", limits_updated: "运行上限已调整", parent_ready: "父任务已收到等待结果", activation_claimed: "Agent 开始处理任务", usage: "已知用量已更新", activity: "工具过程已更新", whiteboard_conflict: "内容白板版本冲突", activation_committed: "Agent 交付已保存", run_completed: "运行已人工完成", auxiliary_prepared: "系统辅助已开始", auxiliary_failed: "系统辅助失败", auxiliary_committed: "系统辅助结果已保存", message: "消息已投递" };
  const state = {
    roles: [], teams: [], runs: [], presets: { roles: [], teams: [], tools: [] }, presetsLoaded: false, panels: loadPanelPreferences(), draft: emptyDraft(), teamId: null, version: null,
    dirty: false, undo: [], redo: [], selection: new Set(), edgeId: null,
    mode: "select", linkFrom: null, view: { x: 80, y: 90, k: 1 },
    epoch: 0, previewEpoch: 0, run: null, runId: null, selectedView: "overview", selectedId: null,
    viewEpoch: 0, history: [], historyBefore: null, historyMore: false,
    pollTimer: null, polling: false, events: [], seenEvents: new Set(), cursor: 0,
    unread: new Map(), positions: new Map(), openLogs: new Set(), logDetails: new Map(), logRequests: new Set(), search: "", space: false,
  };

  function emptyDraft() { return { name: "新的 Agent 团队", shared_background: "", nodes: [], edges: [], whiteboard_enabled: false, whiteboard_format: "md", whiteboard_editors: [] }; }
  function uid(prefix = "n") { return prefix + "_" + (window.crypto?.randomUUID?.() || Math.random().toString(36).slice(2) + Date.now().toString(36)); }
  function copy(value) { return JSON.parse(JSON.stringify(value)); }
  function esc(value) { return String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
  function arr(value) { return Array.isArray(value) ? value : (value?.items || value?.versions || []); }
  function entity(value) { return value?.definition ? { ...value.definition, id: value.id, version: value.version } : value?.role ? { ...value.role, id: value.id, version: value.version } : value?.payload ? { ...value.payload, id: value.id, version: value.version } : value; }
  function status(value) { return `<span class="status status-${esc(value || "idle")}">${esc(labels[value] || value || "待命")}</span>`; }
  function time(value) { if (!value) return ""; const d = new Date(value); return Number.isNaN(+d) ? esc(value) : d.toLocaleTimeString("zh-CN", { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" }); }
  function num(value) { return Number(value || 0).toLocaleString("zh-CN"); }
  function text(value) { return typeof value === "string" ? value : JSON.stringify(value ?? "", null, 2); }
  function nodeRole(node) { return node.role && Number(node.role.version) === Number(node.role_version) ? node.role : state.roles.find(r => r.id === node.role_id); }
  function roleName(node) { return nodeRole(node)?.name || "角色"; }
  function nodePosition(node) { const p = node.position || node; return { x: Number(p.x) || 0, y: Number(p.y) || 0 }; }
  function normalizeDraft(raw) {
    const d = raw.definition || raw;
    if (!d || !Array.isArray(d.nodes) || !Array.isArray(d.edges)) throw new Error("文件必须包含 nodes 和 edges 数组。");
    if (d.nodes.length > 1000 || d.edges.length > 5000) throw new Error("配置过大：最多导入 1,000 个节点和 5,000 条连线。");
    return { name: d.name || "导入的团队", shared_background: d.shared_background || "", whiteboard_enabled: !!d.whiteboard_enabled, whiteboard_format: d.whiteboard_format === "html" ? "html" : "md", whiteboard_editors: Array.isArray(d.whiteboard_editors) ? d.whiteboard_editors : [], ...(d.viewport ? { viewport: d.viewport } : {}), ...(d.sources ? { sources: d.sources } : {}), ...(d.preset_key ? { preset_key: d.preset_key } : {}), ...(d.limits ? { limits: d.limits } : {}),
      nodes: d.nodes.map(n => ({ ...n, id: String(n.id), name: String(n.name || "Agent"), position: nodePosition(n), prompt_supplement: n.prompt_supplement || n.prompt_addendum || "" })),
      edges: d.edges.map(e => ({ id: e.id || uid("e"), source: String(e.source), target: String(e.target), type: e.type === "chat" ? "room" : e.type })) };
  }

  async function api(path, options = {}) {
    const response = await fetch(path, { ...options, headers: { ...(options.body ? { "Content-Type": "application/json" } : {}), ...options.headers } });
    let data; try { data = await response.json(); } catch (_) { data = null; }
    if (!response.ok) throw new Error(data?.error || data?.message || `请求失败（${response.status}）`);
    return data;
  }
  const post = (path, body) => api(path, { method: "POST", body: JSON.stringify(body) });
  function toast(message, error = false) {
    const el = document.getElementById("team-toast");
    el.textContent = message; el.className = "toast show" + (error ? " error" : "");
    clearTimeout(toast.timer); toast.timer = setTimeout(() => el.classList.remove("show"), 3800);
  }
  function persistDraft() { try { localStorage.setItem(STORAGE_KEY, JSON.stringify({ draft: state.draft, teamId: state.teamId, version: state.version })); } catch (_) {} }
  function loadPanelPreferences() {
    try { const saved = JSON.parse(localStorage.getItem(PANEL_STORAGE_KEY)); return saved && typeof saved === "object" && !Array.isArray(saved) ? saved : {}; } catch (_) { return {}; }
  }
  function rememberPanelPreferences() { try { localStorage.setItem(PANEL_STORAGE_KEY, JSON.stringify(state.panels)); } catch (_) {} }
  function panelBounds(grid, side) {
    const widths = state.panels[grid.dataset.panelMode], narrow = window.innerWidth <= 850;
    const min = side === "left" ? 160 : 200, center = grid.dataset.panelMode === "edit" ? 200 : 260;
    const other = side === "left" ? (narrow ? 0 : widths.right) : widths.left;
    return { min, max: Math.max(min, Math.min(side === "left" ? 480 : 520, grid.clientWidth - center - (narrow ? 8 : 16) - other)) };
  }
  function applyPanelWidths(grid) {
    const mode = grid.dataset.panelMode, small = window.innerWidth <= 1150;
    const defaults = mode === "edit" ? { left: small ? 200 : 236, right: small ? 240 : 280 } : { left: small ? 200 : 240, right: small ? 240 : 282 };
    if (!state.panels[mode] || typeof state.panels[mode] !== "object" || Array.isArray(state.panels[mode])) state.panels[mode] = { ...defaults };
    const widths = state.panels[mode];
    for (const side of ["left", "right"]) if (!Number.isFinite(Number(widths[side]))) widths[side] = defaults[side];
    for (const side of ["right", "left", "right"]) {
      const bounds = panelBounds(grid, side), raw = Number(widths[side]);
      widths[side] = Math.round(Math.min(bounds.max, Math.max(bounds.min, Number.isFinite(raw) ? raw : defaults[side])));
    }
    for (const side of ["left", "right"]) {
      grid.style.setProperty(`--${side}-panel-width`, widths[side] + "px");
      const handle = grid.querySelector(`[data-panel-resize="${side}"]`), bounds = panelBounds(grid, side);
      if (handle) { handle.setAttribute("aria-valuenow", widths[side]); handle.setAttribute("aria-valuemin", bounds.min); handle.setAttribute("aria-valuemax", bounds.max); handle.setAttribute("aria-valuetext", `${widths[side]} 像素`); }
    }
    if (state.runId && state.selectedView === "overview") requestAnimationFrame(renderRunTopology);
  }
  function bindPanelResize(mode) {
    const grid = document.querySelector(mode === "edit" ? ".editor-grid" : ".observer-grid");
    if (!grid) return; grid.dataset.panelMode = mode;
    const first = grid.firstElementChild, last = grid.lastElementChild;
    first.id ||= mode === "edit" ? "role-sidebar" : "run-navigation";
    const names = mode === "edit" ? { left: "角色库", right: "节点属性栏" } : { left: "运行导航", right: "运行详情栏" };
    for (const side of ["left", "right"]) {
      const handle = document.createElement("div");
      handle.className = "panel-resizer"; handle.dataset.panelResize = side; handle.tabIndex = 0;
      handle.setAttribute("role", "separator"); handle.setAttribute("aria-orientation", "vertical");
      handle.setAttribute("aria-label", `调整${names[side]}宽度`); handle.setAttribute("aria-controls", side === "left" ? first.id : last.id);
      handle.title = `拖动调整${names[side]}宽度；方向键微调，Shift 加速，Home / End 调到最小 / 最大`;
      if (side === "left") first.after(handle); else last.before(handle);
      let drag = null;
      handle.addEventListener("pointerdown", e => { if (e.button !== 0) return; e.preventDefault(); handle.focus(); drag = { x: e.clientX, width: state.panels[mode][side] }; handle.setPointerCapture(e.pointerId); document.body.classList.add("resizing-panels"); });
      handle.addEventListener("pointermove", e => { if (!drag) return; state.panels[mode][side] = drag.width + (e.clientX - drag.x) * (side === "left" ? 1 : -1); applyPanelWidths(grid); });
      const finish = () => { if (drag) rememberPanelPreferences(); drag = null; document.body.classList.remove("resizing-panels"); };
      handle.addEventListener("pointerup", finish); handle.addEventListener("pointercancel", finish);
      handle.addEventListener("keydown", e => {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return;
        e.preventDefault(); e.stopPropagation(); const bounds = panelBounds(grid, side), step = e.shiftKey ? 30 : 10;
        state.panels[mode][side] = e.key === "Home" ? bounds.min : e.key === "End" ? bounds.max : state.panels[mode][side] + (e.key === "ArrowRight" ? step : -step) * (side === "left" ? 1 : -1);
        applyPanelWidths(grid); rememberPanelPreferences();
      });
    }
    applyPanelWidths(grid);
  }
  function optionalOutputLimit(value) {
    const raw = String(value ?? "").trim(); if (!raw || Number(raw) === 0) return null;
    const amount = Number(raw); if (!Number.isSafeInteger(amount) || amount < 1) throw new Error("单次输出上限需为正整数，留空或 0 表示不限。");
    return amount;
  }
  async function loadPresets() {
    if (!state.presetsLoaded) { const data = await api("/api/team-presets"); state.presets = { roles: arr(data.roles), teams: arr(data.teams), tools: arr(data.tools) }; state.presetsLoaded = true; }
    return state.presets;
  }
  function sourcesMarkup(sources, heading = "论文来源") {
    const safe = arr(sources).filter(s => { try { return ["http:", "https:"].includes(new URL(s.url).protocol); } catch (_) { return false; } });
    return safe.length ? `<div class="preset-sources"><span class="small muted">${esc(heading)}</span>${safe.map(s => `<a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">${esc(s.title)} ↗</a>`).join("")}</div>` : "";
  }
  function markDirty() { state.dirty = true; persistDraft(); updateSaveStatus(); }
  function updateSaveStatus() {
    const el = document.getElementById("save-state"); if (el) el.textContent = state.dirty ? "草稿 · 尚未保存" : state.version ? `配置 v${state.version} · 已保存` : "本地草稿";
    const undo = document.querySelector('[data-action="undo"]'), redo = document.querySelector('[data-action="redo"]');
    if (undo) undo.disabled = !state.undo.length; if (redo) redo.disabled = !state.redo.length;
    const preview = document.querySelector('[data-action="team-preview"]'); if (preview) preview.disabled = !state.teamId;
  }
  function remember(before = copy(state.draft)) { state.undo.push(before); if (state.undo.length > 80) state.undo.shift(); state.redo = []; }
  function mutate(fn) { remember(); fn(); markDirty(); renderGraph(); renderInspector(); renderValidation(); }
  function historyMove(which) {
    const from = state[which], to = state[which === "undo" ? "redo" : "undo"];
    if (!from.length) return; to.push(copy(state.draft)); state.draft = from.pop(); state.selection.clear(); state.edgeId = null; markDirty();
    document.getElementById("team-name").value = state.draft.name; renderGraph(); renderInspector(); renderValidation();
  }

  // Task edges and room edges are separate relations. Mixed cycles are legal.
  function validateTopology(draft) {
    const errors = [], ids = new Set(), parent = new Map(), children = new Map(), seen = new Set();
    for (const node of draft.nodes) {
      if (!node.id || ids.has(node.id)) errors.push({ message: "节点 ID 重复或为空", node_ids: [node.id] });
      ids.add(node.id); children.set(node.id, []);
    }
    for (const edge of draft.edges) {
      if (!ids.has(edge.source) || !ids.has(edge.target)) { errors.push({ message: "连线包含不存在的节点", edge_ids: [edge.id] }); continue; }
      if (edge.source === edge.target) errors.push({ message: "节点不能连接自身", node_ids: [edge.source], edge_ids: [edge.id] });
      if (!["task", "room"].includes(edge.type)) errors.push({ message: "连线类型无效", edge_ids: [edge.id] });
      const pair = JSON.stringify([edge.source, edge.target].sort());
      if (seen.has(pair)) errors.push({ message: "同一对节点只能有一种连接，请先删除已有连线", edge_ids: [edge.id], node_ids: [edge.source, edge.target] }); seen.add(pair);
      if (edge.type === "task") {
        if (parent.has(edge.target)) errors.push({ message: `${draft.nodes.find(n => n.id === edge.target)?.name || "节点"} 只能有一个父节点`, node_ids: [edge.target], edge_ids: [edge.id] });
        parent.set(edge.target, edge.source); children.get(edge.source).push(edge.target);
      }
    }
    const visited = new Set(), visiting = new Set();
    function visit(id) { if (visiting.has(id)) { errors.push({ message: "父子任务关系不能形成有向环", node_ids: [...visiting] }); return; } if (visited.has(id)) return; visiting.add(id); for (const c of children.get(id) || []) visit(c); visiting.delete(id); visited.add(id); }
    for (const id of ids) visit(id);
    const groups = new Map([...ids].map(id => [id, id]));
    function find(id) { while (groups.get(id) !== id) { groups.set(id, groups.get(groups.get(id))); id = groups.get(id); } return id; }
    for (const e of draft.edges.filter(e => e.type === "room" && ids.has(e.source) && ids.has(e.target))) groups.set(find(e.source), find(e.target));
    const components = new Map(); for (const id of ids) { const root = find(id); if (!components.has(root)) components.set(root, []); components.get(root).push(id); }
    const rooms = [...components.values()].filter(members => members.length > 1).map((node_ids, i) => ({ id: `room_${i + 1}`, node_ids }));
    if (!draft.nodes.length) errors.push({ message: "请先从角色库添加至少一个节点" });
    return { valid: !errors.length, errors, rooms, roots: [...ids].filter(id => !parent.has(id)) };
  }
  function treeLayout(draft) {
    const v = validateTopology(draft); if (!v.valid) return false;
    const children = new Map(draft.nodes.map(n => [n.id, []]));
    for (const e of draft.edges.filter(e => e.type === "task")) children.get(e.source).push(e.target);
    const pos = new Map(); let leaf = 0;
    function place(id, depth) { const c = children.get(id); let x; if (!c.length) x = 40 + leaf++ * 230; else { const xs = c.map(ch => place(ch, depth + 1)); x = (xs[0] + xs[xs.length - 1]) / 2; } pos.set(id, { x, y: 40 + depth * 180 }); return x; }
    for (const id of v.roots) { place(id, 0); leaf += .5; }
    for (const n of draft.nodes) n.position = pos.get(n.id); return true;
  }

  function shell(mode) {
    app.innerHTML = `<div class="workspace">
      <header class="masthead"><a class="brand" href="/"><span class="brand-mark">侃</span><span><strong>KDS</strong><small>AGENT WORKSPACE</small></span></a><span class="divider"></span>
      ${mode === "edit" ? `<input id="team-name" class="document-name" aria-label="团队名称" value="${esc(state.draft.name)}" maxlength="120">` : `<strong class="grow">${esc(state.run?.name || state.run?.definition?.name || "运行观察台")}</strong>`}
      <div class="workspace-tabs"><button class="${mode === "edit" ? "active" : ""}" data-action="editor">拓扑画板</button><button class="${mode === "run" ? "active" : ""}" data-action="runs">运行观察台</button></div><div class="grow"></div>
      ${mode === "edit" ? '<span id="save-state" class="save-state"></span><button data-action="save">保存版本</button><button class="primary" data-action="start">启动团队 ↗</button>' : '<span id="run-status"></span><button data-action="pause">暂停</button><button data-action="resume">继续</button><button class="primary" data-action="finalize">总结并完成</button>'}
      <a class="small muted" href="/">返回群聊</a></header><div id="workspace-body" class="grow" style="display:flex;flex-direction:column;min-height:0"></div><footer class="footer"><span id="footer-state">团队配置按版本保存 · 运行时冻结角色与拓扑</span><span class="grow"></span><span>DSH / LangGraph</span></footer></div>`;
  }

  async function route() {
    const epoch = ++state.epoch; state.viewEpoch++; clearTimeout(state.pollTimer); state.polling = false;
    const parts = location.hash.replace(/^#\/?/, "").split("/");
    closeDialog();
    if (parts[0] === "run" && parts[1]) { await loadRun(parts[1], epoch); return; }
    state.runId = null; state.run = null; state.selection.clear(); state.edgeId = null; state.linkFrom = null;
    try {
      const results = await Promise.allSettled([api("/api/roles"), api("/api/teams"), api("/api/team-runs"), api("/api/team-presets")]);
      if (epoch !== state.epoch) return;
      if (results[0].status === "rejected") throw results[0].reason;
      state.roles = arr(results[0].value).map(entity).filter(r => !r.archived);
      state.teams = results[1].status === "fulfilled" ? arr(results[1].value).map(entity) : [];
      state.runs = results[2].status === "fulfilled" ? arr(results[2].value) : [];
      if (results[3].status === "fulfilled") { state.presets = { roles: arr(results[3].value.roles), teams: arr(results[3].value.teams), tools: arr(results[3].value.tools) }; state.presetsLoaded = true; }
      if (parts[0] === "edit" && parts[1]) {
        const raw = entity(await api(`/api/teams/${encodeURIComponent(parts[1])}`)); if (epoch !== state.epoch) return;
        state.draft = normalizeDraft(raw); state.teamId = raw.id || parts[1]; state.version = raw.version; state.dirty = false;
      } else if (!state.teamId && !state.draft.nodes.length) {
        try { const saved = JSON.parse(localStorage.getItem(STORAGE_KEY)); if (saved?.draft) { state.draft = normalizeDraft(saved.draft); state.teamId = saved.teamId; state.version = saved.version; state.dirty = true; } } catch (_) {}
      }
      state.undo = []; state.redo = []; renderEditor();
    } catch (error) { if (epoch !== state.epoch) return; shell("edit"); document.getElementById("workspace-body").innerHTML = `<div class="empty">${esc(error.message)}<p style="margin-top:14px"><button data-action="retry">重新连接</button></p></div>`; }
  }

  function renderEditor() {
    if (state.draft.viewport) state.view = { x: Number(state.draft.viewport.x) || 0, y: Number(state.draft.viewport.y) || 0, k: Math.max(.15, Math.min(2.5, Number(state.draft.viewport.k) || 1)) };
    shell("edit");
    document.getElementById("workspace-body").innerHTML = `<div class="toolbar">
      <select id="team-picker" class="team-select" aria-label="预览已保存团队"><option value="">预览已保存团队…</option>${state.teams.map(t => `<option value="${esc(t.id)}" ${t.id === state.teamId ? "selected" : ""}>${esc(t.name)} · v${esc(t.version || 1)}</option>`).join("")}</select><button class="tool" data-action="team-preview" ${state.teamId ? "" : "disabled"}>预览版本</button>
      <button class="tool" data-action="new">＋ 新团队</button><span class="divider"></span><button class="tool active" data-mode="select" title="选择与拖动（V）">↖ 选择</button><button class="tool" data-mode="task" title="依次点击父节点和子节点（T）">↓ 父子任务</button><button class="tool" data-mode="room" title="依次点击两个节点（C）">↔ 群聊连接</button><span class="divider"></span>
      <button class="tool" data-action="undo" title="撤销 Ctrl+Z">↶</button><button class="tool" data-action="redo" title="重做 Ctrl+Shift+Z">↷</button><button class="tool danger" data-action="delete" title="删除选中的节点或连线">删除</button><span class="grow"></span><button class="tool" data-action="layout">树形布局</button><button class="tool" data-action="import">导入</button><button class="tool" data-action="export">导出</button>
      </div><div class="editor-grid"><aside class="sidebar left"><div class="panel-heading"><h3>角色库</h3><button class="ghost small" data-action="role-new">＋ 新建</button></div><input id="role-search" placeholder="搜索角色…" aria-label="搜索角色"><div id="role-list" class="role-list"></div><div class="section"><div class="eyebrow">RELATION LEGEND</div><div class="legend"><span><i></i>任务</span><span><i class="room"></i>群聊</span></div><p class="hint" style="margin-top:12px">同一角色可放入多个节点，每个节点独立执行。群聊连接按传递关系组成房间。</p><button class="small" style="width:100%;margin-top:15px" data-action="example">载入示例团队</button><button class="ghost small" style="width:100%;margin-top:4px" data-action="legacy-import">从旧配置建立团队</button></div></aside>
      <section class="canvas-shell"><div id="team-canvas" class="canvas" tabindex="0" aria-label="团队拓扑画板"><svg id="graph-edges" class="graph-svg" aria-label="节点关系"></svg><div id="graph-nodes" class="node-plane"></div><div id="selection-box" class="selection-box hidden"></div><div class="canvas-guide"><span id="mode-guide">拖动节点 · 空白拖动框选</span><span>空格拖动平移 · 滚轮缩放</span></div><div class="canvas-controls"><button data-action="zoom-out" aria-label="缩小">−</button><span id="zoom-level">100%</span><button data-action="zoom-in" aria-label="放大">＋</button><button data-action="fit" title="适配全部节点（F）">适配</button></div></div><div id="validation" class="validation"></div></section>
      <aside id="inspector" class="sidebar right"></aside></div>`;
    const exampleButton = app.querySelector('[data-action="example"]'); exampleButton.dataset.action = "team-presets"; exampleButton.textContent = "选择预设团队";
    bindPanelResize("edit"); renderRoles(); renderGraph(); renderInspector(); renderValidation(); updateSaveStatus(); bindCanvas();
    document.getElementById("team-name").addEventListener("change", e => mutate(() => { state.draft.name = e.target.value.trim() || "新的 Agent 团队"; }));
    document.getElementById("team-picker").addEventListener("change", async e => { const id = e.target.value; e.target.value = state.teamId || ""; if (id) try { await teamPreview({ type: "saved", id }); } catch (error) { toast(error.message, true); } });
    document.getElementById("role-search").addEventListener("input", renderRoles);
    requestAnimationFrame(() => { if (state.draft.nodes.length && !state.draft.viewport) fitView(); });
  }
  function renderRoles() {
    const el = document.getElementById("role-list"); if (!el) return;
    const query = document.getElementById("role-search").value.toLowerCase();
    const roles = roleLibrary().filter(r => `${r.name} ${r.description || ""}`.toLowerCase().includes(query));
    el.innerHTML = roles.map(r => `<article class="role-card" data-role-equivalence="${esc(roleKey(r))}" ${r.preset ? `data-preset-role="${esc(r.preset.key)}"` : ""}><div class="row"><span class="role-avatar">${esc(r.name.slice(0, 1))}</span><div class="grow"><strong>${esc(r.name)}</strong><div class="small muted">${r.preset ? "论文预设 · " : ""}${r.id ? `版本 ${esc(r.version || r.latest_version || 1)}` : "按需使用"}</div></div><button class="ghost role-edit" data-role-edit="${esc(r.libraryId)}" aria-label="编辑${esc(r.name)}">⋯</button></div><p title="${esc(r.system_prompt || "")}">${esc(r.description || r.system_prompt || "尚未填写角色设定")}</p>${r.preset ? sourcesMarkup(r.sources) : ""}<button class="add-role" data-role-add="${esc(r.libraryId)}">＋ 放到画板</button></article>`).join("") || '<div class="empty">没有匹配的角色</div>';
  }
  function roleLibrary() {
    const library = [], seen = new Set();
    for (const preset of state.presets.roles) {
      const role = state.roles.find(r => equivalentRoles(r, preset.role));
      const item = { ...preset.role, ...(role || {}), preset, libraryId: role?.id || `preset:${preset.key}` };
      library.push(item); seen.add(roleKey(item));
    }
    for (const role of state.roles) { const key = roleKey(role); if (!seen.has(key)) { library.push({ ...role, libraryId: role.id }); seen.add(key); } }
    return library;
  }
  function roleKey(role, useServerKey = true) {
    if (useServerKey && role.equivalence_key) return role.equivalence_key;
    const canonical = value => Array.isArray(value) ? value.map(canonical) : value && typeof value === "object" ? Object.fromEntries(Object.keys(value).sort().map(key => [key, canonical(value[key])])) : value;
    return JSON.stringify(canonical({ name: role.name, description: role.description || "", system_prompt: role.system_prompt || "", model_config_id: role.model_config_id || "default", tools: [...(role.tools || [])].sort(), default_budget: role.default_budget || {}, visibility: Array.isArray(role.visibility) ? [...role.visibility].sort() : role.visibility || [], sources: role.sources || [], preset_key: role.preset_key || "" }));
  }
  function equivalentRoles(a, b) { return a.equivalence_key && b.equivalence_key ? a.equivalence_key === b.equivalence_key : roleKey(a, false) === roleKey(b, false); }
  async function persistentRole(libraryId) {
    const item = roleLibrary().find(r => r.libraryId === libraryId); if (!item) return null;
    if (item.id) return state.roles.find(r => r.id === item.id);
    const record = entity(await post(`/api/team-presets/roles/${encodeURIComponent(item.preset.key)}`, {}));
    const index = state.roles.findIndex(r => r.id === record.id); if (index < 0) state.roles.push(record); else state.roles[index] = record;
    renderRoles(); return record;
  }
  function addRole(id) {
    const role = state.roles.find(r => r.id === id); if (!role) return;
    const board = document.getElementById("team-canvas").getBoundingClientRect();
    const node = { id: uid(), name: role.name, role_id: role.id, role_version: role.version || role.latest_version || 1, prompt_supplement: "", position: { x: (board.width / 2 - state.view.x) / state.view.k - NODE_W / 2 + Math.random() * 50, y: (board.height / 2 - state.view.y) / state.view.k - NODE_H / 2 + Math.random() * 50 } };
    state.selection = new Set([node.id]); state.edgeId = null; mutate(() => state.draft.nodes.push(node));
  }
  function renderGraph() {
    const plane = document.getElementById("graph-nodes"), svg = document.getElementById("graph-edges"); if (!plane || !svg) return;
    const v = validateTopology(state.draft);
    const roomOf = new Map(); v.rooms.forEach((r, i) => r.node_ids.forEach(id => roomOf.set(id, i + 1)));
    plane.innerHTML = state.draft.nodes.map(n => { const p = nodePosition(n); return `<article class="graph-node ${state.selection.has(n.id) ? "selected" : ""} ${state.linkFrom === n.id ? "source" : ""}" data-node="${esc(n.id)}" style="left:${p.x}px;top:${p.y}px" tabindex="0" role="button" aria-label="${esc(n.name)}，${esc(roleName(n))}，版本 ${esc(n.role_version)}"><button class="node-port" data-port="${esc(n.id)}" aria-label="连接到${esc(n.name)}"></button><div class="node-top"><span class="role-avatar">${esc(n.name.slice(0, 1))}</span><div><div class="node-name">${esc(n.name)}</div><div class="node-role">${esc(roleName(n))} · v${esc(n.role_version || 1)}</div></div></div><div class="node-footer"><span class="small muted mono">${esc(n.id.slice(-6))}</span>${roomOf.has(n.id) ? `<span class="badge">群聊 ${roomOf.get(n.id)}</span>` : '<span class="small muted">独立实例</span>'}</div><button class="node-port out" data-port="${esc(n.id)}" aria-label="从${esc(n.name)}发起连线"></button></article>`; }).join("");
    svg.innerHTML = edgeMarkup(state.draft.nodes, state.draft.edges, state.edgeId); transformGraph();
  }
  function edgeMarkup(nodes, edges, selected) {
    const index = new Map(nodes.map(n => [n.id, n]));
    return `<defs><marker id="task-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#8c9e8b"/></marker><marker id="room-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#ad7431"/></marker></defs>${edges.map(e => {
      const a = index.get(e.source), b = index.get(e.target); if (!a || !b) return "";
      const p = nodePosition(a), q = nodePosition(b); let x1, y1, x2, y2, path;
      if (e.type === "room") { const right = q.x > p.x; x1 = p.x + (right ? NODE_W + 3 : -3); y1 = p.y + NODE_H / 2; x2 = q.x + (right ? -3 : NODE_W + 3); y2 = q.y + NODE_H / 2; const off = Math.max(45, Math.abs(x2 - x1) / 2) * (right ? 1 : -1); path = `M${x1},${y1} C${x1 + off},${y1} ${x2 - off},${y2} ${x2},${y2}`; }
      else { x1 = p.x + NODE_W / 2; y1 = p.y + NODE_H + 3; x2 = q.x + NODE_W / 2; y2 = q.y - 4; const off = Math.max(45, Math.abs(y2 - y1) / 2); path = `M${x1},${y1} C${x1},${y1 + off} ${x2},${y2 - off} ${x2},${y2}`; }
      return `<path class="edge-hit" data-edge="${esc(e.id)}" d="${path}"/><path class="edge-path ${e.type} ${e.id === selected ? "selected" : ""}" data-edge="${esc(e.id)}" d="${path}" marker-end="url(#${e.type === "room" ? "room" : "task"}-arrow)" ${e.type === "room" ? 'marker-start="url(#room-arrow)"' : ""}/>`;
    }).join("")}`;
  }
  function transformGraph() {
    const value = `translate(${state.view.x}px,${state.view.y}px) scale(${state.view.k})`;
    document.getElementById("graph-nodes").style.transform = value;
    document.getElementById("graph-edges").style.transform = value;
    document.getElementById("zoom-level").textContent = Math.round(state.view.k * 100) + "%";
  }
  function fitView() {
    if (!state.draft.nodes.length) { state.view = { x: 80, y: 90, k: 1 }; transformGraph(); return; }
    const el = document.getElementById("team-canvas"); if (!el) return;
    const ps = state.draft.nodes.map(nodePosition), minX = Math.min(...ps.map(p => p.x)), minY = Math.min(...ps.map(p => p.y));
    const w = Math.max(...ps.map(p => p.x)) - minX + NODE_W, h = Math.max(...ps.map(p => p.y)) - minY + NODE_H;
    const k = Math.max(.15, Math.min(1.15, (el.clientWidth - 100) / w, (el.clientHeight - 110) / h));
    state.view = { k, x: (el.clientWidth - w * k) / 2 - minX * k, y: (el.clientHeight - h * k) / 2 - minY * k }; transformGraph();
  }
  function zoom(factor, cx, cy) {
    const board = document.getElementById("team-canvas"); if (!board) return;
    cx ??= board.clientWidth / 2; cy ??= board.clientHeight / 2;
    const next = Math.max(.15, Math.min(2.5, state.view.k * factor)), ratio = next / state.view.k;
    state.view.x = cx - (cx - state.view.x) * ratio; state.view.y = cy - (cy - state.view.y) * ratio; state.view.k = next; transformGraph();
  }
  function setMode(mode) {
    state.mode = mode; state.linkFrom = null;
    document.querySelectorAll("[data-mode]").forEach(el => el.classList.toggle("active", el.dataset.mode === mode));
    const board = document.getElementById("team-canvas"); board.className = "canvas mode-" + mode;
    document.getElementById("mode-guide").textContent = mode === "select" ? "拖动节点 · 空白拖动框选" : mode === "task" ? "依次点击父节点和子节点" : "依次点击两个群聊成员"; renderGraph();
  }
  function connectTo(id) {
    if (!state.linkFrom) { state.linkFrom = id; renderGraph(); return; }
    const edge = { id: uid("e"), source: state.linkFrom, target: id, type: state.mode === "room" ? "room" : "task" };
    const proposed = copy(state.draft); proposed.edges.push(edge); const v = validateTopology(proposed);
    if (!v.valid) { toast(v.errors[0].message, true); return; }
    state.linkFrom = null; state.selection.clear(); state.edgeId = edge.id; mutate(() => state.draft.edges.push(edge));
  }
  function deleteSelection() {
    if (!state.selection.size && !state.edgeId) return;
    mutate(() => { state.draft.nodes = state.draft.nodes.filter(n => !state.selection.has(n.id)); state.draft.edges = state.draft.edges.filter(e => e.id !== state.edgeId && !state.selection.has(e.source) && !state.selection.has(e.target)); state.selection.clear(); state.edgeId = null; state.linkFrom = null; });
  }
  function bindCanvas() {
    const board = document.getElementById("team-canvas"); let drag = null;
    const local = e => { const r = board.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; };
    board.addEventListener("pointerdown", e => {
      if (e.target.closest(".canvas-controls")) return;
      const node = e.target.closest("[data-node]"), edge = e.target.closest("[data-edge]"), p = local(e);
      if (e.button === 1 || state.space || e.altKey) { e.preventDefault(); drag = { type: "pan", start: p, view: { ...state.view } }; board.classList.add("panning"); }
      else if (e.button !== 0) return;
      else if (node) {
        const id = node.dataset.node;
        if (state.mode !== "select" || e.target.closest("[data-port]")) { if (state.mode === "select") setMode("task"); connectTo(id); return; }
        if (e.shiftKey) { state.selection.has(id) ? state.selection.delete(id) : state.selection.add(id); }
        else if (!state.selection.has(id)) state.selection = new Set([id]);
        state.edgeId = null; renderGraph(); renderInspector();
        drag = { type: "node", start: p, before: copy(state.draft), nodes: new Map(state.draft.nodes.filter(n => state.selection.has(n.id)).map(n => [n.id, nodePosition(n)])), moved: false };
      } else if (edge) { state.edgeId = edge.dataset.edge; state.selection.clear(); renderGraph(); renderInspector(); return; }
      else { if (!e.shiftKey) state.selection.clear(); state.edgeId = null; renderGraph(); renderInspector(); drag = { type: "box", start: p, initial: new Set(state.selection) }; }
      if (drag) { board.setPointerCapture(e.pointerId); e.preventDefault(); }
    });
    board.addEventListener("pointermove", e => {
      if (!drag) return; const p = local(e), dx = p.x - drag.start.x, dy = p.y - drag.start.y;
      if (drag.type === "pan") { state.view.x = drag.view.x + dx; state.view.y = drag.view.y + dy; transformGraph(); }
      if (drag.type === "node") {
        drag.moved ||= Math.abs(dx) + Math.abs(dy) > 3;
        for (const n of state.draft.nodes) if (drag.nodes.has(n.id)) { const old = drag.nodes.get(n.id); n.position = { x: old.x + dx / state.view.k, y: old.y + dy / state.view.k }; }
        renderGraph();
      }
      if (drag.type === "box") {
        const x = Math.min(p.x, drag.start.x), y = Math.min(p.y, drag.start.y), w = Math.abs(dx), h = Math.abs(dy);
        const el = document.getElementById("selection-box"); el.classList.remove("hidden"); Object.assign(el.style, { left: x + "px", top: y + "px", width: w + "px", height: h + "px" });
        state.selection = new Set(drag.initial);
        for (const n of state.draft.nodes) { const q = nodePosition(n), nx = q.x * state.view.k + state.view.x, ny = q.y * state.view.k + state.view.y; if (nx >= x && ny >= y && nx + NODE_W * state.view.k <= x + w && ny + NODE_H * state.view.k <= y + h) state.selection.add(n.id); }
        renderGraph();
      }
    });
    const finish = () => { if (drag?.type === "node" && drag.moved) { remember(drag.before); markDirty(); } drag = null; board.classList.remove("panning"); document.getElementById("selection-box").classList.add("hidden"); renderInspector(); };
    board.addEventListener("pointerup", finish); board.addEventListener("pointercancel", finish);
    board.addEventListener("wheel", e => { e.preventDefault(); const p = local(e); zoom(Math.exp(-e.deltaY * .0015), p.x, p.y); }, { passive: false });
    board.addEventListener("keydown", e => { const node = e.target.closest("[data-node]"); if (node && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); if (state.mode !== "select") connectTo(node.dataset.node); else { state.selection = new Set([node.dataset.node]); state.edgeId = null; renderGraph(); renderInspector(); } } });
  }
  function renderValidation() {
    const el = document.getElementById("validation"); if (!el) return; const v = validateTopology(state.draft);
    el.className = "validation " + (v.valid ? "ok" : "bad");
    el.innerHTML = v.valid ? `✓ 拓扑有效 · ${state.draft.nodes.length} 个独立节点 · ${v.roots.length} 个根节点 · ${v.rooms.length} 个群聊` : v.errors.map((e, i) => `<button data-error="${i}">! ${esc(e.message)}</button>`).join("");
  }

  function renderInspector() {
    const panel = document.getElementById("inspector"); if (!panel) return;
    panel.classList.toggle("inspector-open", !!state.selection.size || !!state.edgeId);
    if (state.selection.size === 1) {
      const n = state.draft.nodes.find(n => state.selection.has(n.id)); if (!n) return;
      const role = nodeRole(n);
      const parent = state.draft.edges.find(e => e.type === "task" && e.target === n.id);
      panel.innerHTML = `<div class="panel-heading"><h3>节点属性</h3><span class="badge">实例</span></div><label class="field"><span>实例名称</span><input id="node-name" value="${esc(n.name)}" maxlength="120"></label><label class="field"><span>角色版本</span><select id="node-version"><option value="${esc(n.role_version)}">${esc(role?.name || n.role_id)} · v${esc(n.role_version)}</option></select><div class="hint">固定引用版本；更新角色不会改写该节点。</div></label><label class="field"><span>本实例的提示词补充</span><textarea id="node-prompt" placeholder="补充工作范围、验收要求…">${esc(n.prompt_supplement || "")}</textarea></label><div class="section"><div class="eyebrow">RELATIONS</div><div class="property-line"><span>父节点</span><span>${esc(parent ? state.draft.nodes.find(x => x.id === parent.source)?.name : "无 · 根节点")}</span></div><div class="property-line"><span>直接子节点</span><span>${state.draft.edges.filter(e => e.type === "task" && e.source === n.id).length}</span></div>${roomMarkup(n.id)}</div><button class="danger small" data-action="delete">删除节点</button><p class="hint" style="margin-top:14px">删除配置节点仅更新草稿，已启动的运行保持原有快照。</p>`;
      for (const [id, key] of [["node-name", "name"], ["node-prompt", "prompt_supplement"]]) document.getElementById(id).addEventListener("change", e => mutate(() => { n[key] = e.target.value; }));
      document.getElementById("node-version").addEventListener("change", e => mutate(() => { n.role_version = Number(e.target.value); }));
      const selectionId = n.id;
      api(`/api/roles/${encodeURIComponent(n.role_id)}/versions`).then(data => {
        if (state.runId || state.selection.size !== 1 || !state.selection.has(selectionId)) return;
        const el = document.getElementById("node-version"); if (!el) return;
        const versions = arr(data).map(entity); if (!versions.some(v => Number(v.version) === Number(n.role_version))) versions.unshift({ version: n.role_version });
        el.innerHTML = versions.map(v => `<option value="${esc(v.version)}" ${Number(v.version) === Number(n.role_version) ? "selected" : ""}>${esc(role?.name || n.role_id)} · v${esc(v.version)}</option>`).join("");
      }).catch(() => {});
    } else if (state.selection.size > 1) panel.innerHTML = `<h3>已选 ${state.selection.size} 个节点</h3><p class="hint" style="margin-top:12px">拖动任意选中节点，可一同移动。Shift + 点击可以增减选择。</p><button class="danger small" style="margin-top:18px" data-action="delete">删除所选节点</button>`;
    else if (state.edgeId) {
      const e = state.draft.edges.find(x => x.id === state.edgeId); if (!e) return;
      const a = state.draft.nodes.find(n => n.id === e.source), b = state.draft.nodes.find(n => n.id === e.target);
      panel.innerHTML = `<h3>连线属性</h3><label class="field"><span>关系类型</span><select id="edge-type"><option value="task" ${e.type === "task" ? "selected" : ""}>父子任务 →</option><option value="room" ${e.type === "room" ? "selected" : ""}>群聊连接 ↔</option></select></label><div class="room-chip">${esc(a?.name)} ${e.type === "room" ? "↔" : "→"} ${esc(b?.name)}</div><p class="hint">${e.type === "task" ? "父节点可向直接子节点委派任务，子任务结果回传给实际父任务。" : "双向连线的连通分量组成群聊。不会授予派发任务的权限。"}</p><button class="danger small" style="margin-top:20px" data-action="delete">删除连线</button>`;
      document.getElementById("edge-type").addEventListener("change", event => { const test = copy(state.draft); test.edges.find(x => x.id === e.id).type = event.target.value; const v = validateTopology(test); if (!v.valid) { toast(v.errors[0].message, true); renderInspector(); return; } mutate(() => { e.type = event.target.value; }); });
    } else {
      const v = validateTopology(state.draft);
      panel.innerHTML = `<div class="inspector-placeholder"><div class="symbol">⌘</div><h3>构建你的 Agent 团队</h3><p class="hint" style="margin-top:12px">从角色库添加节点，再选择两种关系连线。点击节点或连线编辑属性。</p></div><label class="field"><span>团队共享背景</span><textarea id="shared-background" placeholder="所有节点都可以读取的任务背景…">${esc(state.draft.shared_background)}</textarea></label><div class="section"><div class="panel-heading"><h3>群聊预览</h3><span class="badge">${v.rooms.length}</span></div>${v.rooms.map((r, i) => `<div class="room-chip"><strong class="small">群聊 ${i + 1}</strong><p class="small muted" style="margin-top:4px">${r.node_ids.map(id => esc(state.draft.nodes.find(n => n.id === id)?.name)).join(" · ")}</p></div>`).join("") || '<p class="hint">连接两个或更多节点后显示群聊。</p>'}</div>`;
      document.getElementById("shared-background").addEventListener("change", e => mutate(() => { state.draft.shared_background = e.target.value; }));
      panel.insertAdjacentHTML("beforeend", whiteboardConfigMarkup());
      if (state.draft.sources) panel.insertAdjacentHTML("beforeend", sourcesMarkup(state.draft.sources));
      document.getElementById("whiteboard-enabled").addEventListener("change", e => mutate(() => { state.draft.whiteboard_enabled = e.target.checked; }));
      document.getElementById("whiteboard-format").addEventListener("change", e => mutate(() => { state.draft.whiteboard_format = e.target.value; }));
      panel.querySelectorAll("[data-whiteboard-editor]").forEach(el => el.addEventListener("change", () => { const editors = [...panel.querySelectorAll("[data-whiteboard-editor]:checked")].map(input => input.dataset.whiteboardEditor); mutate(() => { state.draft.whiteboard_editors = editors; }); }));
    }
  }
  function roomMarkup(id) { const room = validateTopology(state.draft).rooms.find(r => r.node_ids.includes(id)); return room ? `<div class="room-chip"><span class="small muted">同群成员</span><p style="margin-top:5px">${room.node_ids.filter(x => x !== id).map(x => esc(state.draft.nodes.find(n => n.id === x)?.name)).join(" · ")}</p></div>` : '<p class="hint">没有群聊连接</p>'; }
  function whiteboardConfigMarkup() {
    const editors = state.draft.whiteboard_editors || [];
    return `<div class="section"><h3>内容白板</h3><p class="hint" style="margin:7px 0">用于保存共同产出物，与拓扑画板分别管理。</p><label class="root-choice"><input id="whiteboard-enabled" type="checkbox" ${state.draft.whiteboard_enabled ? "checked" : ""}><span>启用内容白板</span></label><label class="field"><span>内容格式</span><select id="whiteboard-format"><option value="md" ${state.draft.whiteboard_format !== "html" ? "selected" : ""}>Markdown</option><option value="html" ${state.draft.whiteboard_format === "html" ? "selected" : ""}>HTML</option></select></label><div class="field"><span>允许编辑的节点</span>${state.draft.nodes.map(n => `<label class="root-choice"><input type="checkbox" data-whiteboard-editor="${esc(n.id)}" ${editors.includes(n.id) || editors.includes(n.role_id) ? "checked" : ""}><span>${esc(n.name)}</span></label>`).join("") || '<p class="hint">添加节点后配置编辑权限。</p>'}</div><p class="hint">修改按白板版本校验，冲突会读取新版本并重新规划。</p></div>`;
  }

  function dialog(title, html, submit, wide = false) {
    state.previewEpoch++;
    dialogs.innerHTML = `<div class="dialog-overlay"><section class="dialog ${wide ? "wide" : ""}" role="dialog" aria-modal="true" aria-label="${esc(title)}"><div class="dialog-head"><div><div class="eyebrow">KDS / TEAM WORKSPACE</div><h2>${esc(title)}</h2></div><button class="ghost" data-dialog-close aria-label="关闭">×</button></div><form id="dialog-form">${html}<p id="dialog-error" class="dialog-error" role="alert"></p>${submit ? `<div class="dialog-actions"><button type="button" data-dialog-close>取消</button><button type="submit" class="primary" id="dialog-submit">${esc(submit.label || "保存")}</button></div>` : ""}</form></section></div>`;
    const previouslyFocused = document.activeElement;
    dialogs.querySelectorAll("[data-dialog-close]").forEach(el => el.addEventListener("click", closeDialog));
    dialogs.querySelector(".dialog-overlay").addEventListener("click", e => { if (e.target === e.currentTarget) closeDialog(); });
    dialogs.onkeydown = e => {
      if (e.key === "Escape") { e.stopPropagation(); closeDialog(); }
      if (e.key === "Tab") { const focusable = [...dialogs.querySelectorAll("button,input,textarea,select,a[href]")].filter(el => !el.disabled); const first = focusable[0], last = focusable[focusable.length - 1]; if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last?.focus(); } else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first?.focus(); } }
    };
    dialog.lastFocus = previouslyFocused;
    if (submit) document.getElementById("dialog-form").addEventListener("submit", async e => { e.preventDefault(); const button = document.getElementById("dialog-submit"); button.disabled = true; document.getElementById("dialog-error").textContent = ""; try { await submit.action(new FormData(e.target)); } catch (error) { const el = document.getElementById("dialog-error"); if (el) el.textContent = error.message; } finally { if (button.isConnected) button.disabled = false; } });
    dialogs.querySelector("input,textarea,select,button")?.focus();
  }
  function closeDialog() { state.previewEpoch++; dialogs.innerHTML = ""; dialogs.onkeydown = null; dialog.lastFocus?.focus?.(); }
  async function roleDialog(id) {
    await loadPresets();
    const role = state.roles.find(r => r.id === id) || { name: "", system_prompt: "", model_config_id: "default", tools: [], default_budget: { single_max_tokens: null } };
    const tools = state.presets.tools, unavailable = (role.tools || []).filter(name => !tools.some(t => t.name === name));
    const value = role.default_budget?.single_max_tokens;
    const toolChoices = `<fieldset class="tool-picker"><legend>工具权限</legend><label class="tool-select-all"><input id="tools-all" type="checkbox" ${tools.length ? "" : "disabled"}><span>全选可用工具</span><span id="tools-count" class="small muted"></span></label><div class="tool-choice-grid">${tools.map(t => `<label class="tool-choice"><input type="checkbox" name="tools" value="${esc(t.name)}" ${(role.tools || []).includes(t.name) ? "checked" : ""}><span><strong>${esc(t.label || t.name)}</strong><small>${esc(t.description || t.name)}</small></span></label>`).join("") || '<p class="hint">服务器当前没有启用可选工具。</p>'}</div><p class="hint">仅列出服务器启用的工具；团队编排工具由系统按任务权限自动提供。</p>${unavailable.length ? `<p class="hint" style="color:var(--ochre)">已停用的工具将在保存时移除：${esc(unavailable.join("、"))}</p>` : ""}</fieldset>`;
    dialog(id ? `编辑角色 · v${role.version || 1}` : "新建角色模板", `<label class="field"><span>角色名称</span><input name="name" value="${esc(role.name)}" required maxlength="120" placeholder="例如：边界案例校验"></label><label class="field"><span>角色设定 / System prompt</span><textarea name="system_prompt" required style="min-height:160px" placeholder="职责、工作方式与交付标准…">${esc(role.system_prompt)}</textarea></label><div class="field-grid"><label class="field"><span>模型配置引用</span><input name="model_config_id" value="${esc(role.model_config_id || "default")}" required></label><label class="field"><span>单次输出上限（可留空）</span><input name="single_max_tokens" type="number" min="0" step="1" value="${value == null || value === 0 ? "" : Number(value)}" placeholder="不限"><div class="hint">留空或 0 表示不限，仍受父任务与运行额度约束。</div></label></div>${toolChoices}${sourcesMarkup(role.sources)}${id ? '<p class="hint">保存会创建新版本。现有节点固定引用的旧版本继续保留。</p><button type="button" class="ghost danger small" id="archive-role">归档此角色</button>' : ""}`, { label: id ? "保存新版本" : "创建角色", action: async fd => {
      const body = { name: fd.get("name").trim(), system_prompt: fd.get("system_prompt"), model_config_id: fd.get("model_config_id").trim(), tools: fd.getAll("tools"), default_budget: { ...role.default_budget, single_max_tokens: optionalOutputLimit(fd.get("single_max_tokens")) }, ...(role.description != null ? { description: role.description } : {}), ...(role.sources ? { sources: role.sources } : {}), ...(role.preset_key ? { preset_key: role.preset_key } : {}) };
      const visibilityMode = fd.get("visibility_mode"); body.visibility = visibilityMode === "all" ? "all" : visibilityMode === "selected" ? fd.get("visible_to").split(/[,，]/).map(id => id.trim()).filter(Boolean) : [];
      const result = entity(await post(id ? `/api/roles/${encodeURIComponent(id)}/versions` : "/api/roles", { ...body, ...(id ? { base_version: role.version } : {}) }));
      if (id) state.roles = state.roles.filter(r => r.id !== id); state.roles.push({ ...body, ...result }); closeDialog(); renderRoles(); renderInspector(); toast("角色版本已保存");
    } });
    const updateToolSelection = () => { const checked = dialogs.querySelectorAll('input[name="tools"]:checked').length, all = document.getElementById("tools-all"); all.checked = tools.length > 0 && checked === tools.length; all.indeterminate = checked > 0 && checked < tools.length; document.getElementById("tools-count").textContent = `${checked} / ${tools.length}`; };
    document.getElementById("tools-all").addEventListener("change", e => { dialogs.querySelectorAll('input[name="tools"]').forEach(input => { input.checked = e.target.checked; }); updateToolSelection(); });
    dialogs.querySelectorAll('input[name="tools"]').forEach(input => input.addEventListener("change", updateToolSelection)); updateToolSelection();
    const visibility = role.visibility || [], visibilityMode = visibility === "all" || visibility.includes?.("all") ? "all" : visibility.length ? "selected" : "private";
    document.querySelector("#dialog-form .dialog-actions").insertAdjacentHTML("beforebegin", `<div class="field"><span>角色设定可见范围</span><select name="visibility_mode"><option value="private" ${visibilityMode === "private" ? "selected" : ""}>仅本 Agent</option><option value="all" ${visibilityMode === "all" ? "selected" : ""}>全部成员</option><option value="selected" ${visibilityMode === "selected" ? "selected" : ""}>指定实例 / 节点</option></select><input name="visible_to" style="margin-top:7px" value="${esc(Array.isArray(visibility) ? visibility.join(", ") : "")}" placeholder="指定接收方 ID，逗号分隔"><p class="hint" style="margin-top:5px">只决定其他成员能否读取角色设定，不授予工具或任务权限。</p></div>`);
    if (id) document.getElementById("archive-role").addEventListener("click", async () => { try { await api(`/api/roles/${encodeURIComponent(id)}`, { method: "DELETE" }); state.roles = state.roles.filter(r => r.id !== id); closeDialog(); renderRoles(); toast("角色已归档，现有版本引用仍保留"); } catch (error) { document.getElementById("dialog-error").textContent = error.message; } });
  }
  async function saveTeam() {
    state.draft.viewport = { ...state.view };
    const v = validateTopology(state.draft); if (!v.valid) throw new Error(v.errors.map(e => e.message).join("；"));
    const validation = await post("/api/teams/validate", state.draft);
    if (!validation.valid) throw new Error((validation.errors || []).map(e => e.message || e).join("；"));
    const raw = entity(await post(state.teamId ? `/api/teams/${encodeURIComponent(state.teamId)}/versions` : "/api/teams", { ...state.draft, ...(state.teamId ? { base_version: state.version } : {}) }));
    state.teamId = raw.id || state.teamId; state.version = raw.version || (state.version || 0) + 1; state.dirty = false; persistDraft(); updateSaveStatus();
    const index = state.teams.findIndex(t => t.id === state.teamId), record = { ...state.draft, id: state.teamId, version: state.version };
    if (index < 0) state.teams.push(record); else state.teams[index] = record;
    const picker = document.getElementById("team-picker"); if (picker) picker.innerHTML = '<option value="">预览已保存团队…</option>' + state.teams.map(t => `<option value="${esc(t.id)}" ${t.id === state.teamId ? "selected" : ""}>${esc(t.name)} · v${esc(t.version || 1)}</option>`).join("");
    toast(`已保存配置 v${state.version}，将在下次运行使用`); return record;
  }
  function limitsFields(limits = {}) {
    const fields = [["total_max_tokens", "总输出 token 上限（0 不限）", 20000, 0], ["total_duration_seconds", "运行时长上限（秒，0 不限）", 600, 0], ["single_max_tokens", "单次激活输出上限（可留空）", null, 0], ["summary_max_tokens", "预留总结输出预算", 1024, 1], ["max_concurrency", "并发 Agent 上限", 2, 1], ["max_depth", "最大任务深度", 4, 1], ["max_instances", "实例数量上限", 20, 1], ["max_tasks", "任务数量上限", 100, 1]];
    return `<div class="field-grid">${fields.map(([key, name, fallback, min]) => { const single = key === "single_max_tokens", summary = key === "summary_max_tokens", value = summary ? limits[key] ?? fallback : limits[key] === null ? (single ? "" : 0) : limits[key] ?? fallback; return `<label class="field"><span>${name}</span><input name="${key}" type="number" min="${min}" step="1" value="${single && (value == null || value === "" || value === 0) ? "" : Number(value)}" ${single ? 'placeholder="不限"' : "required"}>${summary ? '<div class="hint">包含模型思考消耗；总输出有限时必须低于总额度，给任务执行留下预算。</div>' : ""}</label>`; }).join("")}</div><p class="hint">单次输出留空或 0 表示不限，仍受角色、父任务与运行总额度约束。总 token 或时长至少一项有限。暂停期间不计入运行时长。</p>`;
  }
  function readLimits(fd, base = {}) {
    const result = { ...base };
    for (const key of ["total_max_tokens", "total_duration_seconds", "summary_max_tokens", "max_concurrency", "max_depth", "max_instances", "max_tasks"]) result[key] = Number(fd.get(key));
    result.single_max_tokens = optionalOutputLimit(fd.get("single_max_tokens"));
    if (!Number.isSafeInteger(result.summary_max_tokens) || result.summary_max_tokens <= 0) throw new Error("预留总结输出预算必须是正整数。");
    if (!(result.total_max_tokens > 0 || result.total_duration_seconds > 0)) throw new Error("总输出 token 和运行时长上限至少设置一项。");
    if (result.total_max_tokens > 0 && result.summary_max_tokens >= result.total_max_tokens) throw new Error("预留总结输出预算必须低于总输出 token 上限，请为任务执行保留预算。");
    for (const key of ["total_max_tokens", "total_duration_seconds"]) if (result[key] === 0) result[key] = null;
    return result;
  }
  function startDialog() {
    const v = validateTopology(state.draft); if (!v.valid) { toast(v.errors[0].message, true); renderValidation(); return; }
    const requestId = uid("start");
    dialog("启动团队", `<p class="hint">启动后冻结当前配置与角色版本。未选择的根节点保持待命。</p><label class="field"><span>工作目标</span><textarea name="goal" required placeholder="描述希望团队完成的工作，以及怎样判断完成…"></textarea></label><div class="field"><span>入口根节点</span>${v.roots.map((id, i) => `<label class="root-choice"><input type="checkbox" name="entry" value="${esc(id)}" ${i === 0 ? "checked" : ""}><span>${esc(state.draft.nodes.find(n => n.id === id)?.name)}</span></label>`).join("")}</div>${limitsFields(state.draft.limits)}`, { label: "保存并启动", action: async fd => { const entries = fd.getAll("entry"); if (!entries.length) throw new Error("请选择至少一个入口根节点。"); const limits = readLimits(fd, state.draft.limits); if (state.dirty || !state.teamId) await saveTeam(); const run = await post("/api/team-runs", { team_id: state.teamId, team_version: state.version, entry_node_ids: entries, goal: fd.get("goal"), limits, request_id: requestId }); closeDialog(); location.hash = `#/run/${run.id || run.run_id}`; } }, true);
  }
  function importDialog() {
    dialog("导入拓扑配置", '<p class="hint">导入完整团队文件会建立独立的角色模板与团队版本。仅包含拓扑的 JSON 会生成本地草稿，需引用现有角色。</p><label class="field"><span>选择 JSON 文件</span><input id="import-file" type="file" accept=".json,application/json"></label><label class="field"><span>或粘贴配置 JSON</span><textarea id="import-text" style="min-height:180px" placeholder="{ &quot;nodes&quot;: [...], &quot;edges&quot;: [...] }"></textarea></label>', { label: "导入配置", action: async () => {
      const file = document.getElementById("import-file").files[0];
      if (file && file.size > 5 * 1024 * 1024) throw new Error("文件不能大于 5 MB。");
      const raw = file ? await file.text() : document.getElementById("import-text").value;
      let parsed, d; try { parsed = JSON.parse(raw); d = normalizeDraft(parsed); } catch (error) { throw new Error(`无法导入：${error.message}`); }
      if (Array.isArray(parsed.roles) && parsed.roles.length) { const imported = entity(await post("/api/teams/import", { ...parsed, definition: d })); closeDialog(); location.hash = `#/edit/${imported.id || imported.team_id}`; toast("团队与角色版本已独立导入"); return; }
      remember(); state.draft = d; state.teamId = null; state.version = null; state.selection.clear(); state.edgeId = null; markDirty(); closeDialog(); renderEditor(); toast("配置已导入，请检查角色引用与拓扑");
    } });
  }
  async function exportTeam() {
    const refs = new Map(state.draft.nodes.map(n => [`${n.role_id}:${n.role_version}`, n])), roles = [];
    for (const n of refs.values()) { const versions = arr(await api(`/api/roles/${encodeURIComponent(n.role_id)}/versions`)).map(entity); const role = versions.find(r => Number(r.version) === Number(n.role_version)); if (!role) throw new Error(`无法导出：角色版本 ${n.role_id} v${n.role_version} 不存在。`); roles.push(role); }
    download(JSON.stringify({ format: "kds-team-v1", schema_version: 1, definition: state.draft, roles }, null, 2), `${state.draft.name}.json`, "application/json");
  }
  function download(content, name, type) { const url = URL.createObjectURL(new Blob([content], { type })), a = document.createElement("a"); a.href = url; a.download = name; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); }
  async function legacyImportDialog() {
    const configs = arr(await api("/api/configs"));
    dialog("从旧配置建立团队", `<p class="hint">为旧角色建立独立模板与节点，以群聊连接保留讨论关系。旧数据继续保留；任务关系可在画板上补充。</p>${configs.map(c => `<button type="button" class="list-choice" data-legacy="${esc(c.id)}"><span class="role-avatar">侃</span><span class="grow"><strong>${esc(c.name || c.config?.name || "旧配置")}</strong><small>${esc(c.updated_at || "")}</small></span><span>导入 ↗</span></button>`).join("") || '<div class="empty">没有旧配置</div>'}`, null);
    dialogs.querySelectorAll("[data-legacy]").forEach(el => el.addEventListener("click", async () => { el.disabled = true; try { const result = entity(await post("/api/teams/import", { config_id: el.dataset.legacy, legacy_config_id: el.dataset.legacy })); closeDialog(); location.hash = `#/edit/${result.id || result.team_id}`; } catch (error) { document.getElementById("dialog-error").textContent = error.message; el.disabled = false; } }));
  }
  function basicExamplePreview() {
    const definitions = [{ name: "方案负责人", system_prompt: "负责拆解目标，为直接子节点派发任务；等待结果后汇总交付。" }, { name: "方案扩展", system_prompt: "扩展方案细节，按需委派校验或创建临时子角色，确保结果满足验收条件。" }, { name: "方案校验", system_prompt: "独立校验方案与边界情况，通过正式子任务结果回传发现。" }];
    const roles = definitions.map((role, i) => ({ ...role, id: `basic-role-${i}`, version: 1, tools: ["read"], model_config_id: "default", default_budget: { single_max_tokens: 2048 } }));
    const d = emptyDraft(); d.name = "方案协作团队";
    d.nodes = [0, 1, 2, 2].map((r, i) => ({ id: `basic-node-${i}`, name: ["方案负责人", "方案扩展", "总体校验", "细节校验"][i], role_id: roles[r].id, role_version: 1, position: { x: 0, y: 0 }, prompt_supplement: "" }));
    for (const [a, b, type] of [[0, 1, "task"], [0, 2, "task"], [1, 3, "task"], [1, 2, "room"], [2, 3, "room"]]) d.edges.push({ id: uid("e"), source: d.nodes[a].id, target: d.nodes[b].id, type });
    treeLayout(d); return { definition: d, roles };
  }
  async function exampleTeam() {
    const preview = basicExamplePreview(), refs = new Map();
    for (const definition of preview.roles) {
      let role = state.roles.find(r => equivalentRoles(r, definition));
      if (!role) { const { id, version, ...body } = definition; role = entity(await post("/api/roles", body)); state.roles.push(role); }
      refs.set(definition.id, role);
    }
    const d = preview.definition;
    for (const node of d.nodes) { const role = refs.get(node.role_id); node.role_id = role.id; node.role_version = role.version; }
    remember(); state.draft = d; state.teamId = null; state.version = null; state.selection.clear(); markDirty(); renderEditor(); toast("示例已建立：两个校验节点复用同一角色，分别独立执行");
  }
  async function presetsDialog() {
    const catalog = await loadPresets();
    dialog("选择预设团队", `<p class="hint">先查看分工、关系和角色设定；预览保留当前草稿。明确使用后才建立团队。</p><div class="preset-list">${catalog.teams.map(p => `<article class="preset-card"><div class="row"><h3 class="grow">${esc(p.name)}</h3><span class="badge">论文预设</span></div><p>${esc(p.description || "")}</p>${sourcesMarkup(p.sources)}<button type="button" class="small" data-preset-preview="${esc(p.key)}">只读预览 ↗</button></article>`).join("") || '<div class="empty">当前没有可用的论文预设。</div>'}</div><div class="section"><button type="button" class="small" id="basic-example">预览基础协作示例</button><p class="hint" style="margin-top:7px">负责人、扩展者与两个复用角色的校验节点，不绑定论文来源。</p></div>`, null, true);
    dialogs.querySelectorAll("[data-preset-preview]").forEach(button => button.addEventListener("click", async () => { button.disabled = true; try { await teamPreview({ type: "preset", key: button.dataset.presetPreview }); } catch (error) { const el = document.getElementById("dialog-error"); if (el) el.textContent = error.message; button.disabled = false; } }));
    document.getElementById("basic-example").addEventListener("click", () => teamPreview({ type: "basic" }));
  }
  async function teamPreview(source) {
    let token = ++state.previewEpoch; const epoch = state.epoch;
    const path = source.type === "preset" ? `/api/team-presets/teams/${encodeURIComponent(source.key)}/preview` : source.type === "run" ? `/api/team-runs/${encodeURIComponent(source.id)}/preview` : `/api/teams/${encodeURIComponent(source.id)}/preview${source.version ? `?version=${encodeURIComponent(source.version)}` : ""}`;
    const results = await Promise.all(source.type === "basic" ? [Promise.resolve(basicExamplePreview())] : [api(path), ...(source.type === "saved" ? [api(`/api/teams/${encodeURIComponent(source.id)}/versions`)] : [])]);
    if (token !== state.previewEpoch || epoch !== state.epoch) return;
    const preview = results[0], definition = normalizeDraft(preview.definition), roles = arr(preview.roles).map(entity), versions = results[1] ? arr(results[1]).map(entity) : [];
    const version = preview.version || preview.definition.version || source.version, oldVersion = source.type === "saved" && version && Number(version) !== Number(preview.head_version || state.teams.find(t => t.id === source.id)?.version);
    const useLabel = source.type === "preset" ? "使用此预设团队" : source.type === "basic" ? "使用基础协作示例" : source.type === "run" || oldVersion ? "载入为独立草稿" : "载入此版本";
    dialog(`${definition.name} · 只读预览`, `<div class="preview-notice"><span class="badge">只读预览${version ? ` · v${esc(version)}` : ""}</span><p class="hint">当前草稿保持不变。点击「${useLabel}」后才进入编辑，预览不会创建角色、团队或调用模型。</p></div>${source.type === "saved" && versions.length ? `<label class="field preview-version"><span>查看保存版本</span><select id="preview-version">${versions.map(v => `<option value="${esc(v.version)}" ${Number(v.version) === Number(version) ? "selected" : ""}>版本 ${esc(v.version)}${Number(v.version) === Number(preview.head_version) ? " · 当前" : ""}</option>`).join("")}</select></label>` : ""}<div class="preview-grid"><div><div class="panel-heading"><h3>团队分工与关系</h3><span class="small muted">${definition.nodes.length} 节点 · ${definition.edges.length} 连线</span></div><div id="preview-topology" class="preview-topology"></div><div class="legend"><span><i></i>父子任务</span><span><i class="room"></i>群聊连接</span><span>点击节点查看设定</span></div></div><aside id="preview-node-details" class="preview-node-details"></aside></div><details class="preview-background"><summary>团队共享背景</summary><pre>${esc(definition.shared_background || "未设置共享背景")}</pre></details>${sourcesMarkup(definition.sources)}<div class="dialog-actions"><button type="button" id="preview-back">返回${source.type === "saved" ? "编辑画板" : source.type === "run" ? "运行列表" : "团队列表"}</button><button type="button" class="primary" id="preview-use">${useLabel} ↗</button></div>`, null, true);
    token = state.previewEpoch; dialogs.querySelector(".dialog").classList.add("team-preview-dialog");
    document.querySelector(".team-preview-dialog .preview-background").insertAdjacentHTML("afterend", previewSettingsMarkup(definition));
    const selectNode = id => {
      const node = definition.nodes.find(n => n.id === id), role = roles.find(r => r.id === node?.role_id && Number(r.version) === Number(node?.role_version)) || node?.role || {};
      dialogs.querySelectorAll("[data-preview-node]").forEach(el => el.classList.toggle("selected", el.dataset.previewNode === id));
      const details = document.getElementById("preview-node-details");
      details.innerHTML = node ? `<h3>${esc(node.name)}</h3><p class="small muted" style="margin-top:6px">${esc(role.name || node.role_id)} · v${esc(node.role_version)}</p><p class="hint" style="margin-top:12px">${esc(role.description || "")}</p><div class="property-line"><span>模型</span><span>${esc(role.model_config_id || "default")}</span></div><div class="field"><span>工具权限</span><p class="small">${esc((role.tools || []).map(name => state.presets.tools.find(t => t.name === name)?.label || name).join("、") || "无自选工具")}</p></div><details class="preview-prompt" open><summary>完整角色设定</summary><pre>${esc(role.system_prompt || "未设置")}</pre></details>${node.prompt_supplement ? `<details class="preview-prompt"><summary>节点补充设定</summary><pre>${esc(node.prompt_supplement)}</pre></details>` : ""}${sourcesMarkup(role.sources)}` : '<p class="hint">选择节点查看角色设定。</p>';
    };
    requestAnimationFrame(() => { if (token !== state.previewEpoch) return; renderPreviewTopology(definition); document.getElementById("preview-topology").querySelectorAll("[data-preview-node]").forEach(el => el.addEventListener("click", () => selectNode(el.dataset.previewNode))); selectNode(definition.nodes[0]?.id); });
    document.getElementById("preview-version")?.addEventListener("change", e => { teamPreview({ ...source, version: Number(e.target.value) }).catch(error => toast(error.message, true)); });
    document.getElementById("preview-back").addEventListener("click", () => { if (source.type === "saved") closeDialog(); else if (source.type === "run") runsDialog(); else presetsDialog(); });
    const useRequestId = uid("preset-team");
    document.getElementById("preview-use").addEventListener("click", async e => {
      e.target.disabled = true;
      try {
        if (source.type === "preset") { const record = entity(await post(`/api/team-presets/teams/${encodeURIComponent(source.key)}`, { request_id: useRequestId })); closeDialog(); location.hash = `#/edit/${record.id}`; }
        else if (source.type === "basic") { await exampleTeam(); closeDialog(); }
        else {
          definition.nodes.forEach(node => { const role = roles.find(r => r.id === node.role_id && Number(r.version) === Number(node.role_version)); if (role) node.role = role; });
          state.draft = definition; state.teamId = source.type === "saved" && !oldVersion ? source.id : null; state.version = state.teamId ? Number(version) : null; state.dirty = !state.teamId;
          state.selection.clear(); state.edgeId = null; state.undo = []; state.redo = []; persistDraft(); closeDialog();
          clearTimeout(state.pollTimer); state.runId = null; state.run = null; history.replaceState(null, "", state.teamId ? `#/edit/${encodeURIComponent(state.teamId)}` : "#/edit"); renderEditor();
          toast(state.teamId ? `已载入配置 v${state.version}` : "已载入独立草稿，保存后建立新团队");
        }
      } catch (error) { const el = document.getElementById("dialog-error"); if (el) el.textContent = error.message; e.target.disabled = false; }
    });
  }
  function renderPreviewTopology(definition) {
    const el = document.getElementById("preview-topology"), nodes = definition.nodes; if (!el) return;
    if (!nodes.length) { el.innerHTML = '<div class="empty">没有节点</div>'; return; }
    const ps = nodes.map(nodePosition), minX = Math.min(...ps.map(p => p.x)), minY = Math.min(...ps.map(p => p.y)), w = Math.max(...ps.map(p => p.x)) - minX + NODE_W, h = Math.max(...ps.map(p => p.y)) - minY + NODE_H;
    const k = Math.max(.05, Math.min(1, (el.clientWidth - 50) / w, (el.clientHeight - 64) / h)), transform = `translate(${(el.clientWidth - w * k) / 2 - minX * k}px,${(el.clientHeight - h * k) / 2 - minY * k}px) scale(${k})`;
    el.innerHTML = `<svg class="graph-svg" style="transform:${transform}">${edgeMarkup(nodes, definition.edges)}</svg><div class="node-plane" style="transform:${transform}">${nodes.map(n => { const p = nodePosition(n); return `<button type="button" class="graph-node" data-preview-node="${esc(n.id)}" style="left:${p.x}px;top:${p.y}px;text-align:left"><div class="node-top"><span class="role-avatar">${esc(n.name.slice(0, 1))}</span><span class="node-name">${esc(n.name)}</span></div><div class="node-footer"><span class="small muted">角色版本 ${esc(n.role_version)}</span><span class="badge">只读</span></div></button>`; }).join("")}</div>`;
  }
  function previewSettingsMarkup(definition) {
    const names = { total_max_tokens: "总输出 token", total_duration_seconds: "运行时长（秒）", single_max_tokens: "单次输出", summary_max_tokens: "总结预留", max_concurrency: "并发 Agent", max_depth: "任务深度", max_instances: "实例数量", max_tasks: "任务数量", max_processes: "运行时进程", max_discussion_turns: "讨论轮次" };
    const editors = (definition.whiteboard_editors || []).map(id => definition.nodes.find(n => n.id === id)?.name || id);
    return `<details class="preview-background"><summary>建议运行额度与内容白板</summary>${definition.limits ? `<div class="detail-grid">${Object.entries(definition.limits).filter(([key]) => names[key]).map(([key, value]) => `<div><span>${names[key]}</span><strong>${value == null || value === 0 ? "不限" : esc(value)}</strong></div>`).join("")}</div>` : '<p class="hint" style="margin-top:8px">未设置建议运行额度，启动时填写。</p>'}<div class="property-line"><span>内容白板</span><span>${definition.whiteboard_enabled ? `启用 · ${definition.whiteboard_format === "html" ? "HTML" : "Markdown"}` : "未启用"}</span></div>${definition.whiteboard_enabled ? `<p class="hint">允许编辑：${esc(editors.join("、") || "没有配置编辑者")}</p>` : ""}</details>`;
  }

  async function runsDialog() {
    state.runs = arr(await api("/api/team-runs"));
    dialog("团队运行", `<p class="hint">可先只读预览本次运行冻结的团队配置。</p>${state.runs.map(r => `<article class="list-choice"><span class="role-avatar">◈</span><a class="grow" href="#/run/${esc(r.id)}"><strong>${esc(r.name || r.definition?.name || r.goal?.slice(0, 50) || r.id)}</strong><small>${esc(r.created_at || "")} · 配置 v${esc(r.team_version || "—")}</small></a>${status(r.status)}<button type="button" class="small" data-run-preview="${esc(r.id)}">预览配置</button></article>`).join("") || '<div class="empty">尚无团队运行。在画板上配置后启动。</div>'}`, null, true);
    dialogs.querySelectorAll("[data-run-preview]").forEach(button => button.addEventListener("click", async () => { button.disabled = true; try { await teamPreview({ type: "run", id: button.dataset.runPreview }); } catch (error) { const el = document.getElementById("dialog-error"); if (el) el.textContent = error.message; button.disabled = false; } }));
  }
  async function loadRun(id, epoch) {
    state.runId = id; state.run = null; state.selectedView = "overview"; state.selectedId = null; state.events = []; state.seenEvents = new Set(); state.unread.clear(); state.openLogs.clear(); state.logDetails.clear(); state.logRequests.clear(); state.history = [];
    app.innerHTML = '<div class="empty">正在连接团队运行…</div>';
    try { const run = await api(`/api/team-runs/${encodeURIComponent(id)}`); if (epoch !== state.epoch) return; state.run = run; state.cursor = Math.max(0, (run.event_seq || 0) - 200); const recent = await api(`/api/team-runs/${encodeURIComponent(id)}/events?after=${state.cursor}&limit=200`); if (epoch !== state.epoch) return; appendEvents(arr(recent.events || recent)); appendEvents(run.events || []); renderObserver(); await selectView("overview"); schedulePoll(); }
    catch (error) { if (epoch !== state.epoch) return; app.innerHTML = `<div class="empty">${esc(error.message)}<p><a href="/teams">返回拓扑画板</a></p><button data-action="retry">重新连接</button></div>`; }
  }
  function renderObserver() {
    shell("run"); document.getElementById("workspace-body").innerHTML = `<div class="toolbar"><span class="small muted">运行快照 <span class="mono">${esc(state.runId.slice(-10))}</span></span><span class="divider"></span><span class="small muted" id="run-version">配置 v${esc(state.run.team_version || state.run.definition_version || 1)}</span><span class="grow"></span><span id="connection-state" class="small muted">已连接</span><button class="tool" data-action="edit-running">编辑下次运行配置</button><button class="tool" data-action="limits">调整运行上限</button><button class="tool" data-action="runs">全部运行</button></div><div class="observer-grid"><aside class="observer-nav"><input id="observer-search" placeholder="搜索 Agent / 群聊…" aria-label="搜索运行视图"><div id="observer-navigation"></div></aside><section class="view-shell"><div id="view-header" class="view-header"></div><div id="view-content" class="view-content"></div><div id="view-composer"></div></section><aside id="run-inspector" class="sidebar right"></aside></div>`;
    document.getElementById("observer-search").value = state.search;
    document.getElementById("observer-search").addEventListener("input", e => { state.search = e.target.value; renderNav(); });
    bindPanelResize("run"); renderNav(); updateRunControls();
  }
  function agents() { return arr(state.run?.agents || state.run?.instances); }
  function tasks() { return arr(state.run?.tasks); }
  function rooms() { return arr(state.run?.rooms); }
  function agent(id) { return agents().find(a => (a.id || a.instance_id) === id); }
  function agentId(a) { return a.id || a.instance_id; }
  function roomMembers(r) { return r.member_ids || r.instance_ids || r.members?.map(m => typeof m === "string" ? m : m.instance_id || m.id) || []; }
  function entityName(id) { return agent(id)?.name || rooms().find(r => r.id === id)?.name || id || "系统"; }
  function renderNav() {
    const el = document.getElementById("observer-navigation"); if (!el) return; const query = state.search.toLowerCase();
    function nav(type, id, name, extra = "") { const key = `${type}:${id || ""}`, unread = state.unread.get(key) || 0; return `<button class="nav-item ${state.selectedView === type && state.selectedId === (id || null) ? "active" : ""}" data-view="${type}" ${id ? `data-view-id="${esc(id)}"` : ""}><span class="grow">${esc(name)}</span>${extra}${unread ? `<span class="counter">${unread}</span>` : ""}</button>`; }
    el.innerHTML = `<div class="nav-group">${nav("overview", null, "◈ 运行总览")}${nav("tasks", null, "⌘ 任务树", `<span class="counter">${tasks().length}</span>`)}${nav("artifacts", null, "▤ 产出物")}${nav("system", null, "○ 系统辅助")}</div><div class="nav-group"><div class="eyebrow">ROOMS / 群聊</div>${rooms().filter(r => (r.name || r.id).toLowerCase().includes(query)).map((r, i) => nav("room", r.id, r.name || `群聊 ${i + 1}`, `<span class="small muted">${roomMembers(r).length}</span>`)).join("") || '<div class="small muted" style="padding:8px">没有匹配的群聊</div>'}</div><div class="nav-group"><div class="eyebrow">AGENTS / 独立实例</div>${agents().filter(a => (a.name || "").toLowerCase().includes(query)).map(a => nav("agent", agentId(a), a.name || agentId(a), ["failed", "error", "uncertain"].includes(a.status) ? '<span class="failure">!</span>' : `${a.dynamic ? '<span class="badge dynamic">临时</span>' : ""}<span class="status status-${esc(a.status)}" title="${esc(labels[a.status] || a.status)}"></span>`)).join("")}</div>`;
  }
  function updateRunControls() {
    const run = state.run, completed = run.status === "completed";
    document.getElementById("run-status").innerHTML = status(run.status);
    document.querySelector('[data-action="pause"]').disabled = run.status !== "running";
    document.querySelector('[data-action="resume"]').disabled = run.status !== "paused" || (run.can_resume === false && !uncertainTasks().length);
    document.querySelector('[data-action="finalize"]').disabled = completed;
    document.querySelector('[data-action="limits"]').disabled = completed;
    document.getElementById("footer-state").textContent = completed ? "运行已人工完成 · 所有视图只读" : `事件 #${state.cursor} · 页面切换不改变后台执行`;
  }
  function schedulePoll() { clearTimeout(state.pollTimer); state.pollTimer = setTimeout(pollRun, state.run?.status === "completed" ? 6000 : 1500); }
  function appendEvents(events) {
    for (const event of events) { const key = event.id || event.seq; if (state.seenEvents.has(key)) continue; state.seenEvents.add(key); state.events.push(event); state.cursor = Math.max(state.cursor, Number(event.seq || 0)); const targetType = event.room_id ? "room" : "agent", targetId = event.room_id || event.instance_id; if (targetId && !(state.selectedView === targetType && state.selectedId === targetId)) { const k = `${targetType}:${targetId}`; state.unread.set(k, (state.unread.get(k) || 0) + 1); } }
    if (state.events.length > 500) state.events = state.events.slice(-500);
  }
  async function pollRun() {
    if (!state.runId || state.polling) return; const runId = state.runId, epoch = state.epoch; state.polling = true;
    try {
      const data = await api(`/api/team-runs/${encodeURIComponent(runId)}/events?after=${state.cursor}&limit=200`);
      if (epoch !== state.epoch || runId !== state.runId) return;
      if (data?.reset_required || data?.cursor_expired) state.cursor = 0;
      appendEvents(arr(data.events || data));
      const snapshot = await api(`/api/team-runs/${encodeURIComponent(runId)}`);
      if (epoch !== state.epoch || runId !== state.runId) return;
      state.run = snapshot; appendEvents(snapshot.events || []);
      document.getElementById("connection-state").textContent = "已连接"; document.getElementById("connection-state").classList.remove("disconnected");
      renderNav(); updateRunControls(); await refreshView();
    } catch (error) { if (epoch === state.epoch) { const el = document.getElementById("connection-state"); if (el) { el.textContent = "连接中断 · 自动重试"; el.classList.add("disconnected"); el.title = error.message; } } }
    finally { if (epoch === state.epoch) { state.polling = false; schedulePoll(); } }
  }
  async function selectView(type, id = null) {
    state.viewEpoch++; state.selectedView = type; state.selectedId = id; state.history = []; state.historyBefore = null; state.historyMore = false; state.unread.delete(`${type}:${id || ""}`);
    renderNav(); renderView(); document.getElementById("view-content").scrollTop = state.positions.get(`${type}:${id || ""}`) || 0;
    await refreshView(true);
  }
  async function refreshView(loadHistory = false) {
    const token = state.viewEpoch, epoch = state.epoch, type = state.selectedView, id = state.selectedId;
    if ((type === "agent" || type === "room") && loadHistory) {
      try {
        const data = await api(`/api/team-runs/${encodeURIComponent(state.runId)}/${type === "agent" ? "agents" : "rooms"}/${encodeURIComponent(id)}/messages?limit=100`);
        if (token !== state.viewEpoch || epoch !== state.epoch) return;
        state.history = arr(data.messages || data); state.historyBefore = data.next_before ?? data.before ?? state.history[0]?.seq ?? null; state.historyMore = !!(data.has_more || data.next_before || (Array.isArray(data) && data.length >= 100));
      } catch (error) { if (token !== state.viewEpoch || epoch !== state.epoch) return; state.history = scopedMessages(type, id); toast(error.message, true); }
    } else if (type === "agent" || type === "room") {
      const known = new Set(state.history.map(m => m.id || m.seq));
      for (const m of scopedMessages(type, id)) if (!known.has(m.id || m.seq)) { state.history.push(m); known.add(m.id || m.seq); } else Object.assign(state.history.find(old => (old.id || old.seq) === (m.id || m.seq)), m);
    }
    renderView(); renderRunInspector();
  }
  function scopedMessages(type, id) { return arr(state.run.messages).filter(m => type === "room" ? m.room_id === id || (m.target_type === "room" && m.target_id === id) : !m.room_id && (m.instance_id === id || m.agent_id === id || m.recipient_id === id || m.target_instance_id === id || (m.target_type === "agent" && m.target_id === id))); }
  function renderView() {
    const el = document.getElementById("view-content"), header = document.getElementById("view-header"); if (!el) return;
    const scroll = el.scrollTop, nearBottom = el.scrollHeight - el.clientHeight - scroll < 55, firstRender = !el.dataset.view || el.dataset.view !== `${state.selectedView}:${state.selectedId || ""}`;
    el.querySelectorAll("details[data-log]").forEach(d => d.open ? state.openLogs.add(d.dataset.log) : state.openLogs.delete(d.dataset.log));
    const active = document.activeElement, focusLog = active?.closest("details[data-log]")?.dataset.log;
    const type = state.selectedView, id = state.selectedId;
    let title = "运行总览", subtitle = "独立任务并行推进 · 有限讨论按需触发", body = "";
    if (type === "overview") body = overviewMarkup();
    else if (type === "tasks") { title = "任务树"; subtitle = "正式委派与结果回传 · 只读观察"; body = taskTreeMarkup(); }
    else if (type === "agent") {
      const a = agent(id); title = a?.name || "Agent"; subtitle = `${a?.dynamic ? "运行时创建 · " : "独立实例 · "}${labels[a?.status] || a?.status || "待命"}`;
      const own = tasks().filter(t => (t.instance_id || t.assignee_instance_id || t.assignee_id) === id);
      body = `${a?.error ? `<div class="run-alert">${esc(text(a.error))}</div>` : ""}${a?.dynamic ? `<div class="run-alert">由 <button class="ghost small" data-view="agent" data-view-id="${esc(a.creator_instance_id || a.parent_instance_id)}">${esc(entityName(a.creator_instance_id || a.parent_instance_id))}</button> 创建 · ${esc(a.role?.name || "临时角色")}${a.role?.temporary ? `<button class="ghost small" data-save-role="${esc(id)}">保存角色到角色库 ↗</button>` : '<span class="small muted"> · 角色库版本</span>'}</div>` : ""}<h3>当前任务与交付</h3>${own.map(taskMarkup).join("") || '<p class="empty">尚未收到任务。补充消息将保留至下次合法激活。</p>'}${toolLogsMarkup(id)}<div class="section"><h3>输入与公开记录</h3>${historyMarkup()}</div><div class="section"><h3>子 Agent</h3>${agents().filter(x => x.parent_instance_id === id || x.creator_instance_id === id).map(x => `<button class="list-choice" data-view="agent" data-view-id="${esc(agentId(x))}"><span class="grow">${esc(x.name)}</span>${status(x.status)}</button>`).join("") || '<p class="hint">没有子 Agent</p>'}</div>`;
    } else if (type === "room") { const r = rooms().find(x => x.id === id); title = r?.name || "群聊"; subtitle = roomMembers(r || {}).map(entityName).join(" · "); body = historyMarkup(); }
    else if (type === "artifacts") { title = "产出物"; subtitle = "内容白板与交付文件 · 与拓扑画板分别保存"; body = artifactsMarkup(); }
    else if (type === "system") { title = "系统辅助"; subtitle = "配置、评分、投票、格式修正与总结的用量和错误"; body = `${state.run.status !== "completed" ? '<div class="row wrap"><button class="small" data-auxiliary="assist">配置建议</button><button class="small" data-auxiliary="score">意愿评分</button><button class="small" data-auxiliary="vote">发起投票</button></div><p class="hint" style="margin-top:9px">系统辅助与 Agent 共用运行预算和并发名额。</p>' : ""}${toolLogsMarkup(null, true)}${eventsMarkup(state.events.filter(e => e.scope === "system" || e.type?.startsWith("summary") || e.type?.startsWith("auxiliary")))}`; }
    if (type === "overview" && state.run.summary_error) body = `<div class="run-alert">运行已人工完成；总结失败：${esc(text(state.run.summary_error))}</div>` + body;
    header.innerHTML = `<span class="role-avatar">${type === "room" ? "↔" : type === "agent" ? esc(title.slice(0, 1)) : "◈"}</span><div class="grow"><h2>${esc(title)}</h2><p class="small muted">${esc(subtitle)}</p></div>${type === "agent" ? status(agent(id)?.status) : ""}`;
    el.innerHTML = body; el.dataset.view = `${type}:${id || ""}`;
    el.querySelectorAll("details[data-log]").forEach(d => { d.open = state.openLogs.has(d.dataset.log); d.addEventListener("toggle", () => { if (d.open) { state.openLogs.add(d.dataset.log); ensureLogDetails(d); } else state.openLogs.delete(d.dataset.log); }); });
    if (focusLog) el.querySelector(`details[data-log="${CSS.escape(focusLog)}"] summary`)?.focus({ preventScroll: true });
    if (type === "overview") renderRunTopology();
    if (firstRender) el.scrollTop = state.positions.get(`${type}:${id || ""}`) || 0; else el.scrollTop = nearBottom && type === "room" ? el.scrollHeight : scroll;
    renderComposer();
  }
  function overviewMarkup() {
    const as = agents(), ts = tasks(), total = state.run.limits?.total_max_tokens, used = usageTokens();
    const problems = ts.filter(t => t.error && (["failed", "uncertain"].includes(t.status) || t.uncertain)).slice(0, 5).map(t => `<div class="pause-task-error"><strong>${esc(entityName(t.instance_id || t.assignee_instance_id || t.assignee_id))}</strong> · ${esc(text(t.error).slice(0, 260))}<button class="ghost small" data-task-jump="${esc(t.id || t.task_id)}">查看任务 ↗</button></div>`).join("");
    return `${state.run.paused_reason ? `<div class="run-alert">暂停原因：${esc(pauseLabels[state.run.paused_reason] || state.run.paused_reason)}。任务结束不会自动完成人工会话。${problems}</div>` : ""}<div class="metric-grid"><div class="metric"><div class="eyebrow">AGENTS / 实例</div><strong>${as.length}</strong><span class="small muted">${as.filter(a => ["running", "active"].includes(a.status)).length} 执行中 · ${as.filter(a => a.dynamic).length} 动态创建</span></div><div class="metric"><div class="eyebrow">TASKS / 已交付</div><strong>${ts.filter(t => ["completed", "succeeded"].includes(t.status)).length}<small class="muted" style="font-size:15px"> / ${ts.length}</small></strong><span class="small muted">${ts.filter(t => ["waiting", "waiting_children"].includes(t.status)).length} 等待子结果</span></div><div class="metric"><div class="eyebrow">OUTPUT / 已知消耗</div><strong>${num(used)}</strong><span class="small muted">${total ? `总上限 ${num(total)} tokens` : "按运行时长限制"}</span></div></div><div class="panel-heading"><h3>本次运行拓扑</h3><span class="badge">冻结配置 + 动态增量</span></div><div id="run-topology" class="run-topology"></div><div class="panel-heading"><h3>最近活动</h3><span class="small muted">点击派发记录查看子任务</span></div>${eventsMarkup(state.events.filter(e => e.type !== "usage").slice(-40).reverse())}${state.run.summary ? `<div class="section"><h3>运行总结</h3><pre class="artifact-text">${esc(text(state.run.summary))}</pre></div>` : ""}`;
  }
  function renderRunTopology() {
    const el = document.getElementById("run-topology"); if (!el) return;
    const frozen = state.run.definition || {}, original = new Map((frozen.nodes || []).map(n => [n.id, n]));
    const nodes = agents().map((a, i) => { const n = original.get(a.node_id); return { ...a, id: agentId(a), position: n ? nodePosition(n) : { x: i * 210, y: 240 } }; });
    const edges = []; for (const a of agents()) if (a.parent_instance_id) edges.push({ id: `parent_${agentId(a)}`, source: a.parent_instance_id, target: agentId(a), type: "task" });
    const byNode = new Map(agents().map(a => [a.node_id, agentId(a)]));
    for (const e of frozen.edges || []) if (byNode.has(e.source) && byNode.has(e.target) && (e.type === "room" || !edges.some(x => x.source === byNode.get(e.source) && x.target === byNode.get(e.target)))) edges.push({ ...e, source: byNode.get(e.source), target: byNode.get(e.target) });
    for (const r of rooms()) { const members = roomMembers(r); for (let i = 1; i < members.length; i++) if (!edges.some(e => e.type === "room" && [e.source, e.target].includes(members[i]))) edges.push({ id: `dynamic_room_${r.id}_${i}`, source: members[i - 1], target: members[i], type: "room" }); }
    if (!nodes.length) { el.innerHTML = '<div class="empty">没有运行节点</div>'; return; }
    const ps = nodes.map(nodePosition), minX = Math.min(...ps.map(p => p.x)), minY = Math.min(...ps.map(p => p.y)), w = Math.max(...ps.map(p => p.x)) - minX + NODE_W, h = Math.max(...ps.map(p => p.y)) - minY + NODE_H;
    const k = Math.min(.85, (el.clientWidth - 48) / w, (el.clientHeight - 38) / h), x = (el.clientWidth - w * k) / 2 - minX * k, y = (el.clientHeight - h * k) / 2 - minY * k;
    const transform = `translate(${x}px,${y}px) scale(${k})`;
    el.innerHTML = `<div class="canvas"><svg class="graph-svg" style="transform:${transform}">${edgeMarkup(nodes, edges)}</svg><div class="node-plane" style="transform:${transform}">${nodes.map(n => { const p = nodePosition(n); return `<button class="graph-node ${n.dynamic ? "dynamic" : ""}" data-view="agent" data-view-id="${esc(n.id)}" style="left:${p.x}px;top:${p.y}px;text-align:left"><div class="node-top"><span class="role-avatar">${esc(n.name?.slice(0, 1) || "A")}</span><span class="node-name">${esc(n.name)}</span></div><div class="node-footer">${status(n.status)}${n.dynamic ? '<span class="badge dynamic">动态</span>' : ""}</div></button>`; }).join("")}</div></div>`;
  }
  function taskMarkup(t) {
    const id = t.id || t.task_id, assignee = t.instance_id || t.assignee_instance_id || t.assignee_id;
    const waiting = t.wait?.task_ids || t.wait_for || t.waiting_task_ids || t.waiting?.task_ids || [];
    return `<article class="task-card" id="task-${esc(id)}"><div class="row"><strong class="task-title grow">${esc(t.title || t.name || t.goal || (typeof t.input === "string" ? t.input.slice(0, 80) : "任务"))}</strong>${status(t.status)}</div>${t.input && (typeof t.input !== "object" || Object.keys(t.input).length) ? `<p>${esc(text(t.input))}</p>` : ""}${t.result ? `<div class="small muted" style="margin-top:12px">正式结果</div><p>${esc(text(t.result))}</p>` : ""}${t.error ? `<p style="color:var(--danger)">${esc(text(t.error))}</p>` : ""}<div class="task-meta"><span class="mono">${esc((id || "").slice(-10))}</span><button class="ghost small" data-view="agent" data-view-id="${esc(assignee)}">${esc(entityName(assignee))} ↗</button>${t.parent_task_id ? `<button class="ghost small" data-task-jump="${esc(t.parent_task_id)}">父任务 ↑</button>` : ""}${waiting.length ? `<span>等待 ${waiting.length} 个子结果 · ${esc(t.wait?.mode || t.wait_mode || t.waiting?.mode || "all")}</span>` : ""}</div></article>`;
  }
  function taskTreeMarkup() {
    const all = tasks(), children = new Map(); for (const t of all) { const key = t.parent_task_id || "root"; if (!children.has(key)) children.set(key, []); children.get(key).push(t); }
    const seen = new Set(); function render(key) { return (children.get(key) || []).filter(t => !seen.has(t.id || t.task_id)).map(t => { const id = t.id || t.task_id; seen.add(id); return `<div class="task-tree-entry">${taskMarkup(t)}${render(id)}</div>`; }).join(""); }
    return render("root") || '<div class="empty">尚无任务</div>';
  }
  function historyMarkup() { return `${state.historyMore ? '<div class="pagination"><button class="small" data-action="history-more">加载更早记录</button></div>' : ""}${state.history.map(messageMarkup).join("") || '<div class="empty">暂无消息。群聊按任务需要或人工消息触发有限讨论。</div>'}`; }
  function messageMarkup(m) {
    const human = m.role === "human" || m.sender_type === "human" || m.kind === "human" || m.kind === "supplement", name = m.speaker || m.sender_name || (human ? "你" : entityName(m.instance_id || m.agent_id || m.sender_id));
    const queued = m.status === "queued" || m.delivery_status === "pending" || (human && (m.target_type === "agent" || m.target_instance_id) && !m.consumed_at);
    return `<article class="message ${human ? "human" : ""}" data-message="${esc(m.id || m.seq)}"><span class="message-avatar">${esc(name.slice(0, 1))}</span><div class="message-body"><div class="message-meta"><strong>${esc(name)}</strong><time>${time(m.ts || m.created_at || m.timestamp)}</time>${m.task_id ? `<button class="ghost small" data-task-jump="${esc(m.task_id)}">关联任务 ↗</button>` : ""}</div><div class="message-text">${esc(text(m.content || m.message || m.result || ""))}</div>${queued && state.selectedView === "agent" ? '<div class="queued-label">已排队 · 待下次激活读取</div>' : ""}</div></article>`;
  }
  function toolLogsMarkup(id, system = false) {
    const logs = arr(state.run.tool_logs || state.run.harness_logs || state.run.logs).filter(l => system ? l.scope === "system" || l.system || !l.instance_id || l.instance_id.startsWith("system:") : l.instance_id === id || l.agent_id === id).map(l => ({ ...state.logDetails.get(l.id), ...l }));
    if (!logs.length) return system ? '<div class="empty">尚无系统辅助记录</div>' : '<div class="section"><h3>工具过程</h3><p class="hint" style="margin-top:9px">尚无工具调用记录。</p></div>';
    return `<div class="section"><h3>工具过程</h3><p class="hint" style="margin:7px 0">仅展示操作及结果。调用按实例、任务和激活归属。</p>${logs.map(l => `<details class="tool-log" data-log="${esc(l.id || [l.instance_id, l.activation_id, l.call_id, l.seq].join(":"))}"><summary><span class="mono grow">${esc(l.tool || l.name || "工具")}</span>${status(l.status)}<time class="muted small">${time(l.started_at || l.created_at)}</time></summary><pre>${esc(text(l.arguments || l.input || ""))}</pre>${l.result || l.error ? `<pre>${esc(text(l.result || l.error))}</pre>` : ""}<div class="small muted" style="padding:7px 12px">任务 ${esc(l.task_id || "—")} · 激活 ${esc(l.activation_id || "—")} · ${esc(l.call_id || "")}</div></details>`).join("")}</div>`;
  }
  async function ensureLogDetails(details) {
    const id = details.dataset.log, original = arr(state.run.tool_logs || state.run.harness_logs || state.run.logs).find(l => l.id === id);
    const cached = state.logDetails.get(id);
    if (!original || original.arguments !== undefined || original.result !== undefined || (cached && cached.status === original.status && cached.finished_at === original.finished_at) || state.logRequests.has(id)) return;
    const instanceId = original.instance_id || original.agent_id, runId = state.runId, token = state.viewEpoch;
    if (!instanceId) return;
    state.logRequests.add(id);
    try { const response = await api(`/api/team-runs/${encodeURIComponent(runId)}/agents/${encodeURIComponent(instanceId)}/tool-logs`); if (runId !== state.runId) return; for (const log of arr(response.logs || response.tool_logs || response)) state.logDetails.set(log.id, log); if (token === state.viewEpoch) renderView(); }
    catch (error) { if (runId === state.runId && token === state.viewEpoch) toast(error.message, true); }
    finally { if (runId === state.runId) state.logRequests.delete(id); }
  }
  function eventsMarkup(events) {
    return events.map(e => `<div class="event"><time>${time(e.ts || e.created_at || e.timestamp)}</time><i class="event-dot"></i><div class="grow"><span>${esc(e.message || e.description || eventLabels[e.type] || "运行状态更新")}</span>${e.instance_id ? `<button class="ghost small" data-view="agent" data-view-id="${esc(e.instance_id)}">${esc(entityName(e.instance_id))} ↗</button>` : ""}${e.task_id ? `<button class="ghost small" data-task-jump="${esc(e.task_id)}">查看任务 ↗</button>` : ""}${e.result !== undefined ? `<pre class="artifact-text" style="margin-top:8px">${esc(text(e.result))}</pre>` : ""}${e.error ? `<p style="color:var(--danger);margin-top:5px">${esc(text(e.error))}</p>` : ""}</div></div>`).join("") || '<div class="empty">等待新的运行事件</div>';
  }
  function artifactsMarkup() {
    const wb = state.run.whiteboard ? { format: state.run.definition?.whiteboard_format || "md", ...state.run.whiteboard } : null, artifacts = arr(state.run.artifacts);
    return `${wb ? `<div class="artifact-card"><div class="row"><h3 class="grow">内容白板</h3><span class="badge">版本 ${esc(wb.rev || wb.version || 0)}</span><button class="small" data-action="download-whiteboard">下载</button></div><p class="hint" style="margin:9px 0">${esc(wb.format === "html" ? "HTML 预览在隔离环境中展示" : "Markdown 源文")}</p>${wb.format === "html" ? `<iframe class="artifact-frame" sandbox="" referrerpolicy="no-referrer" title="内容白板预览" srcdoc="${esc(wb.content || "")}"></iframe>` : `<pre class="artifact-text">${esc(wb.content || "尚无内容")}</pre>`}</div>` : ""}${artifacts.map(a => `<div class="artifact-card"><div class="row"><h3 class="grow">${esc(a.name || a.title || "产出物")}</h3><span class="badge">v${esc(a.version || 1)}</span></div><p class="hint">来源：${esc(entityName(a.instance_id))} · ${esc(a.task_id || "")}</p>${a.content ? `<pre class="artifact-text" style="margin-top:10px">${esc(text(a.content))}</pre>` : '<p class="hint">暂无可预览内容</p>'}</div>`).join("")}${!wb && !artifacts.length ? '<div class="empty">暂无产出物</div>' : ""}`;
  }
  function usageTokens() { const u = state.run.usage || {}; return u.output_tokens ?? u.completion_tokens ?? u.total_output_tokens ?? state.run.total_output_tokens ?? 0; }
  function renderRunInspector() {
    const el = document.getElementById("run-inspector"); if (!el) return; const a = state.selectedView === "agent" ? agent(state.selectedId) : null, current = a ? tasks().find(t => (t.id || t.task_id) === a.current_task_id) : null;
    const used = usageTokens(), limit = state.run.limits?.total_max_tokens;
    el.innerHTML = `<div class="panel-heading"><h3>${a ? "Agent 详情" : "运行详情"}</h3>${a?.dynamic ? '<span class="badge dynamic">动态实例</span>' : ""}</div>${a ? `<div class="property-line"><span>角色来源</span><span>${esc(a.role?.name || a.role_name || "角色快照")}</span></div><div class="property-line"><span>角色版本</span><span>${esc(a.role?.version || a.role_version || "临时")}</span></div><div class="property-line"><span>运行状态</span>${status(a.status)}</div><div class="property-line"><span>DSH 阶段</span><span>${esc(a.dsh_phase || a.phase || "—")}</span></div>${a.parent_instance_id ? `<div class="property-line"><span>父 Agent</span><button class="ghost small" data-view="agent" data-view-id="${esc(a.parent_instance_id)}">${esc(entityName(a.parent_instance_id))} ↗</button></div>` : ""}${current ? `<div class="section"><h3>当前任务</h3><p class="hint" style="margin-top:10px">${esc(current.title || current.goal || text(current.input).slice(0, 120))}</p><span class="mono small muted">${esc(current.id || current.task_id)}</span></div>` : '<p class="hint" style="margin-top:12px">当前没有激活任务。</p>'}${a.role?.system_prompt ? `<details class="tool-log" style="margin-top:15px"><summary>角色提示词</summary><pre>${esc(a.role.system_prompt)}</pre></details>` : ""}` : `<label class="field"><span>工作目标</span><p>${esc(state.run.goal || "—")}</p></label><div class="property-line"><span>会话状态</span>${status(state.run.status)}</div><div class="property-line"><span>已知运行时长</span><span>${num(Math.round(state.run.elapsed_seconds ?? state.run.active_seconds ?? state.run.usage?.active_seconds ?? 0))} 秒</span></div>`}<div class="section"><div class="eyebrow">BUDGET / 运行预算</div><div class="property-line"><span>已知输出</span><span>${num(used)} tokens</span></div><div class="property-line"><span>总输出上限</span><span>${limit ? num(limit) : "按时长限制"}</span></div>${limit ? `<div class="budget-track"><i style="width:${Math.max(0, Math.min(100, used / limit * 100))}%"></i></div>` : ""}<div class="property-line"><span>并发上限</span><span>${esc(state.run.limits?.max_concurrency || 1)}</span></div>${state.run.usage?.uncertain || state.run.uncertain_usage ? '<p class="hint" style="color:var(--ochre)">部分外部调用用量尚未确定。</p>' : ""}<p class="hint" style="margin-top:12px">预算到限或全部任务交付后暂停。由你决定何时完成本次运行。</p></div>`;
    const waitingIds = current?.wait?.task_ids || current?.wait_for || [];
    el.insertAdjacentHTML("beforeend", sourcesMarkup(a?.role?.sources || state.run.definition?.sources));
    if (waitingIds.length) el.insertAdjacentHTML("beforeend", `<div class="section"><h3>等待对象</h3><p class="hint">${current.wait?.mode === "any_success" ? "任一子任务成功后恢复" : "全部子任务结束后恢复"}</p>${waitingIds.map(id => { const t = tasks().find(t => t.id === id); return `<button class="list-choice" data-task-jump="${esc(id)}"><span class="grow small">${esc(t?.goal || t?.title || id)}</span>${status(t?.status)}</button>`; }).join("")}</div>`);
  }
  function renderComposer() {
    const el = document.getElementById("view-composer"), type = state.selectedView, id = state.selectedId;
    if (!["agent", "room"].includes(type)) { el.innerHTML = ""; return; }
    if (state.run.status === "completed") { el.innerHTML = '<div class="read-only">此运行已人工完成，消息与控制保持只读。</div>'; return; }
    if (el.dataset.scope === `${type}:${id}` && el.querySelector("textarea")) return;
    el.dataset.scope = `${type}:${id}`;
    el.innerHTML = `<form id="message-form" class="composer"><textarea name="content" aria-label="${type === "agent" ? "补充信息" : "群聊消息"}" placeholder="${type === "agent" ? "为此 Agent 补充信息，待下次激活读取…" : "向此群聊发送消息，触发有限讨论…"}" required maxlength="30000"></textarea><div class="row"><p class="hint">${type === "agent" ? "仅排队补充信息，不创建任务，不解除子任务等待。" : "消息仅送达此房间，按任务需要触发有限讨论。"}</p><button class="primary" type="submit">${type === "agent" ? "排队补充" : "发送到群聊"} ↑</button></div></form>`;
    document.getElementById("message-form").addEventListener("submit", async e => { e.preventDefault(); const form = e.target, textarea = form.querySelector("textarea"), content = textarea.value.trim(); if (!content) return; if (form.lastContent !== content) { form.lastContent = content; form.requestId = uid("human"); } const runId = state.runId, viewEpoch = state.viewEpoch, submit = form.querySelector("button"); submit.disabled = true; try { const receipt = await post(`/api/team-runs/${encodeURIComponent(runId)}/messages`, { target_type: type, target_id: id, content, request_id: form.requestId }); if (runId !== state.runId || viewEpoch !== state.viewEpoch) return; textarea.value = ""; form.lastContent = null; toast(type === "agent" ? "已排队 · 待下次激活读取" : "群聊消息已接收"); if (receipt.message) state.history.push(receipt.message); await refreshSnapshot(); } catch (error) { toast(error.message, true); } finally { if (submit.isConnected) submit.disabled = false; } });
  }
  async function refreshSnapshot() { const id = state.runId, epoch = state.epoch; const snapshot = await api(`/api/team-runs/${encodeURIComponent(id)}`); if (id !== state.runId || epoch !== state.epoch) return; state.run = snapshot; renderNav(); updateRunControls(); await refreshView(); }
  async function loadEarlier() {
    const type = state.selectedView, id = state.selectedId, token = state.viewEpoch, runId = state.runId, old = state.history, before = state.historyBefore || old[0]?.seq;
    const data = await api(`/api/team-runs/${encodeURIComponent(runId)}/${type === "agent" ? "agents" : "rooms"}/${encodeURIComponent(id)}/messages?limit=100&before=${encodeURIComponent(before || "")}`);
    if (token !== state.viewEpoch || runId !== state.runId) return;
    const earlier = arr(data.messages || data), all = new Map(); for (const m of [...earlier, ...old]) all.set(m.id || m.seq, m); state.history = [...all.values()]; state.historyBefore = data.next_before ?? state.history[0]?.seq ?? null; state.historyMore = !!(data.has_more || (Array.isArray(data) && earlier.length >= 100)); const el = document.getElementById("view-content"), height = el.scrollHeight, scroll = el.scrollTop; renderView(); el.scrollTop = scroll + el.scrollHeight - height;
  }
  function finalizeDialog() {
    dialog("完成本次团队运行", '<p class="hint">先停止新工作并收拢正在执行的任务，再冻结总结输入。人工完成后此运行只读。总结失败也会保留完成状态与错误记录。</p><label class="root-choice"><input type="checkbox" name="skip_summary"><span>只保存现有结果完成，不请求总结</span></label>', { label: "确认完成", action: async fd => { await post(`/api/team-runs/${encodeURIComponent(state.runId)}/finalize`, { summarize: !fd.has("skip_summary"), request_id: uid("finalize") }); closeDialog(); await refreshSnapshot(); toast("运行已人工完成"); } });
  }
  function limitsDialog() { dialog("调整运行上限", limitsFields(state.run.limits), { label: "保存运行上限", action: async fd => { await post(`/api/team-runs/${encodeURIComponent(state.runId)}/limits`, readLimits(fd, state.run.limits)); closeDialog(); await refreshSnapshot(); toast("运行上限已更新"); } }, true); }
  function saveDynamicRole(id) {
    const a = agent(id); dialog("保存临时角色", `<p class="hint">显式保存为可复用角色模板，保留动态创建来源。</p><label class="field"><span>模板名称</span><input name="name" value="${esc(a?.role?.name || a?.name || "临时角色")}" required></label>`, { label: "保存到角色库", action: async fd => { await post(`/api/team-runs/${encodeURIComponent(state.runId)}/agents/${encodeURIComponent(id)}/save-role`, { name: fd.get("name") }); closeDialog(); toast("临时角色已保存到角色库"); } });
  }
  function uncertainTasks() { return tasks().filter(t => t.status === "uncertain" || t.uncertain); }
  function resumeUncertainDialog() {
    dialog("核对不确定调用后继续", `<p class="hint">以下任务的外部调用没有确定结果。重试可能重复已经发生的文件或工具副作用。</p>${uncertainTasks().map(t => `<div class="room-chip">${esc(t.title || t.id || t.task_id)} · ${esc(entityName(t.instance_id))}</div>`).join("")}<label class="root-choice"><input type="checkbox" name="verified" required><span>我已核对外部副作用，明确允许重试这些不确定任务。</span></label>`, { label: "重试并继续整场运行", action: async fd => { if (!fd.has("verified")) throw new Error("请先核对外部副作用。"); await post(`/api/team-runs/${encodeURIComponent(state.runId)}/resume`, { retry_uncertain: true }); closeDialog(); await refreshSnapshot(); } });
  }
  function auxiliaryDialog(kind) {
    const title = { assist: "配置建议", score: "意愿评分", vote: "发起投票" }[kind], requestId = uid("auxiliary");
    dialog(title, `<p class="hint">使用公开记录和正式交付进行系统辅助调用；结果保存为运行事件。</p><label class="field"><span>${kind === "vote" ? "投票题目" : "关注的问题"}</span><textarea name="topic" required placeholder="${kind === "assist" ? "希望怎样改进团队分工…" : "描述需要分析的问题…"}"></textarea></label>${kind === "vote" ? '<label class="field"><span>选项（每行一个）</span><textarea name="options" required placeholder="方案 A\n方案 B"></textarea></label>' : ""}`, { label: "执行系统辅助", action: async fd => { const args = { topic: fd.get("topic"), ...(kind === "vote" ? { options: fd.get("options").split("\n").map(o => o.trim()).filter(Boolean) } : {}) }; if (kind === "vote" && args.options.length < 2) throw new Error("投票至少需要两个选项。"); await post(`/api/team-runs/${encodeURIComponent(state.runId)}/auxiliary`, { kind, arguments: args, request_id: requestId }); closeDialog(); await refreshSnapshot(); toast("系统辅助已完成，结果将显示在运行事件中"); } });
  }

  app.addEventListener("click", async e => {
    const b = e.target.closest("button,a[data-view]"); if (!b || b.disabled) return;
    try {
      if (b.dataset.mode) { setMode(b.dataset.mode); return; }
      if (b.dataset.roleAdd) { b.disabled = true; const role = await persistentRole(b.dataset.roleAdd); if (role) addRole(role.id); return; }
      if (b.dataset.roleEdit) { b.disabled = true; const role = await persistentRole(b.dataset.roleEdit); if (role) await roleDialog(role.id); return; }
      if (b.dataset.view) { const el = document.getElementById("view-content"); state.positions.set(`${state.selectedView}:${state.selectedId || ""}`, el.scrollTop); await selectView(b.dataset.view, b.dataset.viewId || null); return; }
      if (b.dataset.saveRole) { saveDynamicRole(b.dataset.saveRole); return; }
      if (b.dataset.auxiliary) { auxiliaryDialog(b.dataset.auxiliary); return; }
      if (b.dataset.taskJump) { await selectView("tasks"); requestAnimationFrame(() => document.getElementById(`task-${b.dataset.taskJump}`)?.scrollIntoView({ block: "center" })); return; }
      if (b.dataset.error !== undefined) { const error = validateTopology(state.draft).errors[Number(b.dataset.error)]; state.selection = new Set(error.node_ids || []); state.edgeId = error.edge_ids?.[0] || null; renderGraph(); renderInspector(); return; }
      const action = b.dataset.action;
      if (action === "save") { b.disabled = true; await saveTeam(); }
      else if (action === "start") startDialog();
      else if (action === "new") { state.draft = emptyDraft(); state.teamId = null; state.version = null; state.undo = []; state.redo = []; state.selection.clear(); state.dirty = false; persistDraft(); if (location.hash !== "#/edit") location.hash = "#/edit"; else renderEditor(); }
      else if (action === "role-new") await roleDialog();
      else if (action === "team-presets") await presetsDialog("teams");
      else if (action === "team-preview") await teamPreview({ type: "saved", id: state.teamId });
      else if (action === "delete") deleteSelection();
      else if (action === "undo" || action === "redo") historyMove(action);
      else if (action === "layout") { const test = copy(state.draft); if (!treeLayout(test)) throw new Error("请先修正拓扑错误，再使用树形布局。"); mutate(() => { state.draft = test; }); fitView(); }
      else if (action === "fit") fitView();
      else if (action === "zoom-in") zoom(1.2);
      else if (action === "zoom-out") zoom(1 / 1.2);
      else if (action === "import") importDialog();
      else if (action === "export") await exportTeam();
      else if (action === "legacy-import") await legacyImportDialog();
      else if (action === "example") { b.disabled = true; await exampleTeam(); }
      else if (action === "runs") await runsDialog();
      else if (action === "editor") location.hash = state.teamId ? `#/edit/${state.teamId}` : "#/edit";
      else if (action === "edit-running") { const teamId = state.run.team_id; if (teamId) location.hash = `#/edit/${teamId}`; else { state.draft = normalizeDraft(state.run.definition); state.teamId = null; state.version = null; state.dirty = true; persistDraft(); location.hash = "#/edit"; } toast("编辑只生成新配置版本，下次运行生效"); }
      else if (action === "resume" && uncertainTasks().length) resumeUncertainDialog();
      else if (action === "pause" || action === "resume") { b.disabled = true; await post(`/api/team-runs/${encodeURIComponent(state.runId)}/${action}`, { request_id: uid(action) }); await refreshSnapshot(); toast(action === "pause" ? "整场运行已暂停" : "整场运行已继续"); }
      else if (action === "limits") limitsDialog();
      else if (action === "finalize") finalizeDialog();
      else if (action === "history-more") { b.disabled = true; await loadEarlier(); }
      else if (action === "download-whiteboard") { const w = state.run.whiteboard, format = w.format || state.run.definition?.whiteboard_format || "md"; download(w.content || "", `内容白板.${format === "html" ? "html" : "md"}`, format === "html" ? "text/html" : "text/markdown"); }
      else if (action === "retry") await route();
    } catch (error) { toast(error.message, true); }
    finally { if (b.isConnected && (b.dataset.roleAdd || b.dataset.roleEdit || ["save", "example", "history-more"].includes(b.dataset.action))) b.disabled = false; if (!state.runId) updateSaveStatus(); else if (document.getElementById("run-status")) updateRunControls(); }
  });
  window.addEventListener("keydown", e => {
    if (dialogs.children.length || /INPUT|TEXTAREA|SELECT/.test(e.target.tagName) || e.target.isContentEditable) return;
    if (state.runId) return;
    if (e.code === "Space") { state.space = true; e.preventDefault(); }
    if (e.key === "Escape") { state.linkFrom = null; state.selection.clear(); state.edgeId = null; setMode("select"); renderInspector(); }
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z") { e.preventDefault(); historyMove(e.shiftKey ? "redo" : "undo"); }
    else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "y") { e.preventDefault(); historyMove("redo"); }
    else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") { e.preventDefault(); saveTeam().catch(error => toast(error.message, true)); }
    else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "a") { e.preventDefault(); state.selection = new Set(state.draft.nodes.map(n => n.id)); renderGraph(); renderInspector(); }
    else if (e.key === "Delete" || e.key === "Backspace") { e.preventDefault(); deleteSelection(); }
    else if (!e.ctrlKey && !e.metaKey) { if (e.key.toLowerCase() === "v") setMode("select"); if (e.key.toLowerCase() === "t") setMode("task"); if (e.key.toLowerCase() === "c") setMode("room"); if (e.key.toLowerCase() === "f") fitView(); }
  });
  window.addEventListener("keyup", e => { if (e.code === "Space") state.space = false; });
  window.addEventListener("blur", () => { state.space = false; });
  window.addEventListener("resize", () => { const grid = document.querySelector(".editor-grid,.observer-grid"); if (grid) applyPanelWidths(grid); });
  window.addEventListener("hashchange", route);
  window.addEventListener("beforeunload", e => { if (state.dirty && !state.runId) { e.preventDefault(); e.returnValue = ""; } });
  window.KDSTeam = Object.freeze({ validateTopology, treeLayout, normalizeDraft });
  route();
})();
