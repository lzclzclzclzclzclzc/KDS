(function () {
  "use strict";

  const app = document.getElementById("app");
  const PALETTE = [
    "#5b6ee1",
    "#e8793f",
    "#2ea886",
    "#c65bd1",
    "#e5488a",
    "#3f8fcf",
    "#b0852a",
    "#6c5ce7",
  ];

  let draft = defaultDraft();
  let configId = null;
  let assistantSessionId = null;
  let chatConvId = null;
  let chatSig = "";

  function defaultDraft() {
    return {
      name: "新的群聊",
      shared_background: "",
      agents: [
        newAgent("a0", "小明"),
        newAgent("a1", "小红"),
      ],
      total_max_tokens: 20000,
      total_duration_seconds: 600,
      first_speaker: "random",
      scheduling_mode: "round_robin",
      round_robin_order: [],
      scheduler_params: {
        lam: 2.0,
        tau: 1.5,
        gamma: 0.7,
        forbid_consecutive: true,
      },
    };
  }

  function newAgent(id, name) {
    return {
      id: id,
      name: name,
      system_prompt: "",
      visibility: [],
      max_tokens: 300,
    };
  }

  function uid() {
    return "a" + Math.random().toString(36).slice(2, 9);
  }

  function escapeHtml(str) {
    return String(str == null ? "" : str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function avatarColor(name) {
    let sum = 0;
    for (const ch of String(name)) sum += ch.codePointAt(0);
    return PALETTE[sum % PALETTE.length];
  }

  async function api(path, options) {
    const opts = options || {};
    const headers = Object.assign({}, opts.headers || {});
    if (opts.body != null) headers["Content-Type"] = "application/json";
    const resp = await fetch(path, Object.assign({}, opts, { headers }));
    let data = null;
    try {
      data = await resp.json();
    } catch (e) {
      data = null;
    }
    if (!resp.ok) {
      throw new Error((data && data.error) || "请求失败");
    }
    return data;
  }

  let toastTimer = null;
  function toast(msg) {
    let el = document.querySelector(".toast");
    if (!el) {
      el = document.createElement("div");
      el.className = "toast";
      document.body.appendChild(el);
    }
    el.textContent = msg;
    el.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove("show"), 2200);
  }

  function statusLabel(status) {
    const map = {
      running: "进行中",
      paused: "已暂停",
      completed: "已完成",
      error: "出错",
    };
    return map[status] || status;
  }

  function statusClass(status) {
    return "status-" + status;
  }

  function fmtTime(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    return d.toLocaleString("zh-CN", { hour12: false });
  }

  // ---------------- Router ----------------
  function route() {
    const hash = location.hash || "#/";
    const parts = hash.replace(/^#\//, "").split("/");
    if (parts[0] === "config") {
      renderConfig(parts[1] || null);
    } else if (parts[0] === "chat") {
      renderChat(parts[1] || null);
    } else {
      renderStart();
    }
  }

  window.addEventListener("hashchange", route);

  // ---------------- Start page ----------------
  async function renderStart() {
    app.innerHTML = `
      <div class="container">
        <div class="start-hero">
          <div class="logo">侃</div>
          <h1>KDS 侃大山</h1>
          <p>让多个 LLM 按预设背景和 system prompt 无限畅聊</p>
          <div class="start-actions">
            <button class="btn btn-primary" data-go="config">＋ 新建配置</button>
          </div>
        </div>
        <div class="section-title">历史对话</div>
        <div id="conversation-list" class="list"></div>
      </div>
    `;

    app.querySelector("[data-go='config']").addEventListener("click", () => {
      location.hash = "#/config";
    });

    await loadConversations();
  }

  async function loadConversations() {
    const list = document.getElementById("conversation-list");
    try {
      const items = await api("/api/conversations");
      if (!items.length) {
        list.innerHTML = '<div class="empty">还没有历史对话，先新建一个配置开始吧。</div>';
        return;
      }
      list.innerHTML = items
        .map(
          (c) => `
          <div class="list-item" data-id="${escapeHtml(c.id)}">
            <div class="thumb">💬</div>
            <div class="meta">
              <div class="title">${escapeHtml(c.name)}</div>
              <div class="sub">${escapeHtml(fmtTime(c.updated_at || c.created_at))} · ${c.turn || 0} 轮</div>
            </div>
            <span class="status-pill ${statusClass(c.status)}">${statusLabel(c.status)}</span>
          </div>
        `
        )
        .join("");
      list.querySelectorAll(".list-item").forEach((el) => {
        el.addEventListener("click", () => {
          location.hash = "#/chat/" + el.dataset.id;
        });
      });
    } catch (e) {
      list.innerHTML = `<div class="empty">加载失败：${escapeHtml(e.message)}</div>`;
    }
  }

  // ---------------- Config page ----------------
  async function renderConfig(id) {
    configId = id || null;
    assistantSessionId = null;
    if (id) {
      try {
        const cfg = await api("/api/configs/" + id);
        draft = normalizeDraft(cfg);
      } catch (e) {
        toast(e.message);
        draft = defaultDraft();
      }
    } else {
      draft = defaultDraft();
    }

    app.innerHTML = `
      <div class="container">
        <div class="config-header">
          <button class="btn btn-ghost" data-back>← 返回</button>
          <h2>${id ? "编辑配置" : "新建配置"}</h2>
          <span class="grow"></span>
          <select id="import-config" class="hidden">
            <option value="">导入历史配置…</option>
          </select>
        </div>
        <div class="config-shell">
          <div class="config-main">
            <div class="card" style="padding:20px">
              <div id="config-form"></div>
            </div>
          </div>
          <aside class="config-aside">
            <div class="card assistant-card">
              <div class="assistant-head">✨ 配置助手</div>
              <div id="assistant-messages" class="assistant-messages">
                <div class="assistant-msg assistant">
                  <div class="bubble">告诉我你想让角色扮演什么身份、聊什么话题，我可以帮你自动填写 system prompt 和共享背景。</div>
                </div>
              </div>
              <div class="assistant-input">
                <input id="assistant-input" type="text" placeholder="例如：让小红扮演挑剔的投资人" />
                <button id="assistant-send" class="btn btn-primary">发送</button>
              </div>
            </div>
          </aside>
        </div>
      </div>
    `;

    app.querySelector("[data-back]").addEventListener("click", () => {
      location.hash = "#/";
    });
    app.querySelector("#assistant-send").addEventListener("click", sendAssistant);
    app
      .querySelector("#assistant-input")
      .addEventListener("keydown", (e) => {
        if (e.key === "Enter") sendAssistant();
      });

    await loadConfigOptions();
    renderConfigForm();
  }

  function normalizeDraft(cfg) {
    return {
      name: cfg.name || "未命名配置",
      shared_background: cfg.shared_background || "",
      agents: (cfg.agents || []).map((a) => ({
        id: a.id || uid(),
        name: a.name || "",
        system_prompt: a.system_prompt || "",
        visibility: a.visibility || [],
        max_tokens: a.max_tokens || 300,
      })),
      total_max_tokens: cfg.total_max_tokens || null,
      total_duration_seconds: cfg.total_duration_seconds || null,
      first_speaker: cfg.first_speaker || "random",
      scheduling_mode: cfg.scheduling_mode || "round_robin",
      round_robin_order: cfg.round_robin_order || [],
      scheduler_params: Object.assign(
        { lam: 2.0, tau: 1.5, gamma: 0.7, forbid_consecutive: true },
        cfg.scheduler_params || {}
      ),
    };
  }

  async function loadConfigOptions() {
    const select = document.getElementById("import-config");
    try {
      const items = await api("/api/configs");
      select.classList.remove("hidden");
      select.innerHTML =
        '<option value="">导入历史配置…</option>' +
        items
          .map(
            (c) =>
              `<option value="${escapeHtml(c.id)}">${escapeHtml(c.name)}</option>`
          )
          .join("");
      select.addEventListener("change", () => {
        if (select.value) location.hash = "#/config/" + select.value;
      });
    } catch (e) {
      // Ignore; the import list is optional.
    }
  }

  function renderConfigForm() {
    if (draft.agents.length === 2 && draft.scheduling_mode !== "round_robin") {
      draft.scheduling_mode = "round_robin";
    }
    const form = document.getElementById("config-form");
    const n = draft.agents.length;
    const isWilling = draft.scheduling_mode === "willingness";
    const showRR = draft.scheduling_mode === "round_robin" && n > 2;

    const firstOptions = ["<option value='random'>随机</option>"]
      .concat(
        draft.agents.map(
          (a) =>
            `<option value="${escapeHtml(a.name)}" ${
              draft.first_speaker === a.name ? "selected" : ""
            }>${escapeHtml(a.name)}</option>`
        )
      )
      .join("");

    form.innerHTML = `
      <div class="field">
        <label>配置名称</label>
        <input id="cfg-name" type="text" value="${escapeHtml(draft.name)}" />
      </div>
      <div class="field">
        <label>共享背景（所有角色都能看到）</label>
        <textarea id="cfg-bg">${escapeHtml(draft.shared_background)}</textarea>
        <div class="hint">描述本次群聊的共同话题、场景或目标。</div>
      </div>

      <div class="field">
        <label>角色（人数：${n}）</label>
        <div id="agents-list">${draft.agents.map(agentCardHTML).join("")}</div>
        <button id="add-agent" class="btn btn-ghost" style="width:100%">＋ 添加角色</button>
      </div>

      <div class="grid-2">
        <div class="field">
          <label>总输出 max_token</label>
          <input id="cfg-total-tokens" type="number" value="${
            draft.total_max_tokens == null ? "" : draft.total_max_tokens
          }" placeholder="留空表示不限" />
        </div>
        <div class="field">
          <label>总对话时长（秒）</label>
          <input id="cfg-duration" type="number" value="${
            draft.total_duration_seconds == null ? "" : draft.total_duration_seconds
          }" placeholder="留空表示不限" />
        </div>
        <div class="field">
          <label>首个发言人</label>
          <select id="cfg-first">${firstOptions}</select>
        </div>
        <div class="field">
          <label>发言调度模式</label>
          <select id="cfg-mode">
            <option value="round_robin" ${
              draft.scheduling_mode === "round_robin" ? "selected" : ""
            }>轮流发言</option>
            <option value="willingness" ${
              draft.scheduling_mode === "willingness" ? "selected" : ""
            }>按意愿</option>
          </select>
          <div class="hint">人数为 2 时始终为轮流发言。</div>
        </div>
      </div>

      <div id="rr-order-field" class="field ${showRR ? "" : "hidden"}">
        <label>轮流发言顺序（手动）</label>
        <input id="cfg-rr-order" type="text" value="${escapeHtml(
          draft.round_robin_order.join(", ")
        )}" placeholder="留空表示随机顺序，例如：小明, 小红, 小刚" />
      </div>

      <div id="scheduler-params" class="field ${isWilling ? "" : "hidden"}">
        <label>按意愿调度参数</label>
        <div class="grid-2" style="gap:10px">
          <div class="field">
            <label>λ 抗垄断</label>
            <input id="cfg-lam" type="number" step="0.1" value="${draft.scheduler_params.lam}" />
          </div>
          <div class="field">
            <label>τ 温度</label>
            <input id="cfg-tau" type="number" step="0.1" value="${draft.scheduler_params.tau}" />
          </div>
          <div class="field">
            <label>γ 衰减</label>
            <input id="cfg-gamma" type="number" step="0.1" value="${draft.scheduler_params.gamma}" />
          </div>
          <div class="field">
            <label>禁止连续发言</label>
            <input id="cfg-forbid" type="checkbox" ${
              draft.scheduler_params.forbid_consecutive ? "checked" : ""
            } style="width:auto;margin-top:8px" />
          </div>
        </div>
      </div>

      <div class="row" style="justify-content:flex-end">
        <button id="save-config" class="btn">保存配置</button>
        <button id="start-chat" class="btn btn-primary">开始对话</button>
      </div>
    `;

    attachConfigEvents();
  }

  function agentCardHTML(agent) {
    const others = draft.agents.filter((a) => a.id !== agent.id);
    const vis = Array.isArray(agent.visibility) ? agent.visibility : [];
    const allChecked = vis.includes("all");
    const chips = [
      `<label class="vis-chip"><input type="checkbox" data-vis="all" ${
        allChecked ? "checked" : ""
      } /> 全体</label>`,
    ]
      .concat(
        others.map(
          (o) =>
            `<label class="vis-chip"><input type="checkbox" data-vis="${escapeHtml(
              o.id
            )}" ${!allChecked && vis.includes(o.id) ? "checked" : ""} ${
              allChecked ? "disabled" : ""
            } /> ${escapeHtml(o.name)}</label>`
        )
      )
      .join("");

    return `
      <div class="agent-card" data-agent-id="${escapeHtml(agent.id)}">
        <div class="agent-head">
          <input class="agent-name" type="text" value="${escapeHtml(agent.name)}" placeholder="角色名" />
          <button class="remove-agent btn-ghost">删除</button>
        </div>
        <div class="field">
          <label>system prompt（角色设定）</label>
          <textarea class="agent-prompt">${escapeHtml(agent.system_prompt)}</textarea>
        </div>
        <div class="row" style="align-items:flex-start">
          <div class="field grow">
            <label>system prompt 对谁可见</label>
            <div class="vis-chips">${chips}</div>
          </div>
          <div class="field" style="width:120px">
            <label>单次 max_token</label>
            <input class="agent-max" type="number" value="${agent.max_tokens || 300}" />
          </div>
        </div>
      </div>
    `;
  }

  function attachConfigEvents() {
    const $ = (id) => document.getElementById(id);

    const nameInput = $("cfg-name");
    const bgInput = $("cfg-bg");
    const totalTokens = $("cfg-total-tokens");
    const duration = $("cfg-duration");
    const firstSel = $("cfg-first");
    const modeSel = $("cfg-mode");

    nameInput.addEventListener("input", () => (draft.name = nameInput.value));
    bgInput.addEventListener("input", () => (draft.shared_background = bgInput.value));
    totalTokens.addEventListener("input", () => {
      draft.total_max_tokens = totalTokens.value ? Number(totalTokens.value) : null;
    });
    duration.addEventListener("input", () => {
      draft.total_duration_seconds = duration.value ? Number(duration.value) : null;
    });
    firstSel.addEventListener("change", () => (draft.first_speaker = firstSel.value));
    modeSel.addEventListener("change", () => {
      draft.scheduling_mode = modeSel.value;
      renderConfigForm();
    });

    const rrOrder = $("cfg-rr-order");
    if (rrOrder) {
      rrOrder.addEventListener("input", () => {
        draft.round_robin_order = rrOrder.value
          .split(/[,，]/)
          .map((s) => s.trim())
          .filter(Boolean);
      });
    }

    const lam = $("cfg-lam");
    const tau = $("cfg-tau");
    const gamma = $("cfg-gamma");
    const forbid = $("cfg-forbid");
    if (lam) lam.addEventListener("input", () => (draft.scheduler_params.lam = Number(lam.value)));
    if (tau) tau.addEventListener("input", () => (draft.scheduler_params.tau = Number(tau.value)));
    if (gamma) gamma.addEventListener("input", () => (draft.scheduler_params.gamma = Number(gamma.value)));
    if (forbid)
      forbid.addEventListener("change", () => (draft.scheduler_params.forbid_consecutive = forbid.checked));

    $("add-agent").addEventListener("click", () => {
      const id = uid();
      const name = "角色" + (draft.agents.length + 1);
      draft.agents.push(newAgent(id, name));
      renderConfigForm();
    });

    document.querySelectorAll(".agent-card").forEach((card) => {
      const id = card.dataset.agentId;
      const agent = draft.agents.find((a) => a.id === id);
      card.querySelector(".agent-name").addEventListener("input", (e) => {
        agent.name = e.target.value;
      });
      card.querySelector(".agent-prompt").addEventListener("input", (e) => {
        agent.system_prompt = e.target.value;
      });
      card.querySelector(".agent-max").addEventListener("input", (e) => {
        agent.max_tokens = Number(e.target.value) || 300;
      });
      card.querySelector(".remove-agent").addEventListener("click", () => {
        draft.agents = draft.agents.filter((a) => a.id !== id);
        renderConfigForm();
      });

      card.querySelectorAll("input[data-vis]").forEach((chip) => {
        chip.addEventListener("change", () => {
          const val = chip.dataset.vis;
          if (val === "all") {
            if (chip.checked) {
              agent.visibility = ["all"];
              card.querySelectorAll("input[data-vis]:not([data-vis='all'])").forEach((c) => {
                c.checked = false;
                c.disabled = true;
              });
            } else {
              agent.visibility = [];
              card.querySelectorAll("input[data-vis]:not([data-vis='all'])").forEach((c) => {
                c.disabled = false;
              });
            }
          } else {
            if (!Array.isArray(agent.visibility)) agent.visibility = [];
            agent.visibility = agent.visibility.filter((v) => v !== "all");
            if (chip.checked) agent.visibility.push(val);
            else agent.visibility = agent.visibility.filter((v) => v !== val);
          }
        });
      });
    });

    $("save-config").addEventListener("click", saveConfig);
    $("start-chat").addEventListener("click", startChat);
  }

  function serializeDraft() {
    return JSON.parse(JSON.stringify(draft));
  }

  async function saveConfig() {
    try {
      const payload = serializeDraft();
      if (configId) {
        await api("/api/configs/" + configId, {
          method: "PUT",
          body: JSON.stringify(payload),
        });
      } else {
        const created = await api("/api/configs", {
          method: "POST",
          body: JSON.stringify(payload),
        });
        configId = created.id;
      }
      toast("配置已保存");
    } catch (e) {
      toast("保存失败：" + e.message);
    }
  }

  async function startChat() {
    try {
      const payload = serializeDraft();
      let id = configId;
      if (!id) {
        const created = await api("/api/configs", {
          method: "POST",
          body: JSON.stringify(payload),
        });
        id = created.id;
        configId = id;
      } else {
        await api("/api/configs/" + id, {
          method: "PUT",
          body: JSON.stringify(payload),
        });
      }
      const conv = await api("/api/conversations", {
        method: "POST",
        body: JSON.stringify({ config_id: id, name: payload.name }),
      });
      location.hash = "#/chat/" + conv.id;
    } catch (e) {
      toast("开始失败：" + e.message);
    }
  }

  // ---------------- Assistant ----------------
  async function sendAssistant() {
    const input = document.getElementById("assistant-input");
    const msg = input.value.trim();
    if (!msg) return;
    input.value = "";
    appendAssistantMessage("user", msg, null);

    const pending = appendAssistantMessage("assistant", "", null, true);
    try {
      const res = await api("/api/assistant/chat", {
        method: "POST",
        body: JSON.stringify({
          session_id: assistantSessionId,
          draft: serializeDraft(),
          message: msg,
        }),
      });
      assistantSessionId = res.session_id;
      const assistantEl = buildAssistantMessage(res.reply, res.proposal);
      pending.replaceWith(assistantEl);
      if (res.proposal) {
        attachProposalButton(assistantEl);
      }
    } catch (e) {
      pending.replaceWith(buildAssistantMessage("出错了：" + e.message, null));
    }
  }

  function appendAssistantMessage(kind, text, proposal, pending) {
    const box = document.getElementById("assistant-messages");
    const el = document.createElement("div");
    el.className = "assistant-msg " + kind;
    el.innerHTML = buildAssistantMessageHTML(text, proposal);
    box.appendChild(el);
    box.scrollTop = box.scrollHeight;
    return el;
  }

  function buildAssistantMessage(text, proposal) {
    const el = document.createElement("div");
    el.className = "assistant-msg assistant";
    el.innerHTML = buildAssistantMessageHTML(text, proposal);
    return el;
  }

  function buildAssistantMessageHTML(text, proposal) {
    let html = `<div class="bubble">${text ? escapeHtml(text) : '<span class="spinner"></span>'}</div>`;
    if (proposal) {
      html += `
        <div class="proposal-card" data-proposal='${escapeHtml(JSON.stringify(proposal))}'>
          <div class="proposal-title">建议修改（需你确认）</div>
          ${proposalSummary(proposal)}
          <button class="btn btn-primary" data-apply-proposal style="margin-top:8px;width:100%">应用修改</button>
        </div>
      `;
    }
    return html;
  }

  function proposalSummary(proposal) {
    const parts = [];
    if (proposal && proposal.shared_background != null) {
      parts.push(
        `<div class="proposal-item">共享背景：${escapeHtml(proposal.shared_background)}</div>`
      );
    }
    if (proposal && proposal.agents) {
      for (const name of Object.keys(proposal.agents)) {
        parts.push(`<div class="proposal-item">角色 ${escapeHtml(name)} 的设定</div>`);
      }
    }
    return parts.join("");
  }

  function attachProposalButton(container) {
    const btn = container.querySelector("[data-apply-proposal]");
    if (!btn) return;
    btn.addEventListener("click", () => {
      const raw = container.querySelector("[data-proposal]").dataset.proposal;
      let proposal;
      try {
        proposal = JSON.parse(raw);
      } catch (e) {
        return;
      }
      applyProposal(proposal);
    });
  }

  function applyProposal(proposal) {
    if (proposal.shared_background != null) {
      draft.shared_background = proposal.shared_background;
    }
    if (proposal.agents) {
      for (const name of Object.keys(proposal.agents)) {
        const agent = draft.agents.find((a) => a.name === name);
        if (!agent) continue;
        const patch = proposal.agents[name];
        if (patch.system_prompt != null) agent.system_prompt = patch.system_prompt;
        if (patch.visibility != null) agent.visibility = patch.visibility;
        if (patch.max_tokens != null) agent.max_tokens = Number(patch.max_tokens);
      }
    }
    renderConfigForm();
    toast("已应用助手建议，请检查后再保存");
  }

  // ---------------- Chat page ----------------
  function renderChat(id) {
    if (!id) {
      location.hash = "#/";
      return;
    }
    chatConvId = id;
    chatSig = "";
    app.innerHTML = `
      <div class="chat-shell">
        <div class="chat-header">
          <button class="back" data-back>‹</button>
          <div class="title" id="chat-title">加载中…</div>
          <div class="token-info" id="chat-tokens"></div>
          <span id="chat-status" class="status-pill status-running">进行中</span>
          <div id="chat-actions"></div>
        </div>
        <div id="chat-messages" class="chat-messages"></div>
        <div class="chat-footer">
          <input id="chat-input" type="text" placeholder="预约下一轮发言（会跳过调度，直接发言）" />
          <button id="chat-send" class="btn btn-primary">发送</button>
        </div>
      </div>
    `;

    app.querySelector("[data-back]").addEventListener("click", () => {
      location.hash = "#/";
    });
    app.querySelector("#chat-actions").addEventListener("click", (e) => {
      const id = e.target.id;
      if (id === "chat-stop") stopChat();
      else if (id === "chat-resume") resumeChat();
      else if (id === "chat-summarize") summarizeChat();
    });
    app.querySelector("#chat-send").addEventListener("click", sendHumanMessage);
    app.querySelector("#chat-input").addEventListener("keydown", (e) => {
      if (e.key === "Enter") sendHumanMessage();
    });

    pollChat();
    window.chatTimer = setInterval(pollChat, 1000);
  }

  function clearChatTimer() {
    if (window.chatTimer) {
      clearInterval(window.chatTimer);
      window.chatTimer = null;
    }
  }

  async function pollChat() {
    if (!chatConvId) return;
    try {
      const conv = await api("/api/conversations/" + chatConvId);
      const sig = JSON.stringify({
        s: conv.status,
        n: (conv.messages || []).length,
        last: conv.messages && conv.messages.length ? conv.messages[conv.messages.length - 1] : null,
        summary: conv.summary || "",
      });
      if (sig !== chatSig) {
        chatSig = sig;
        renderChatState(conv);
      }
      if ((conv.status === "completed" || conv.status === "error") && window.chatTimer) {
        clearChatTimer();
      }
    } catch (e) {
      // Transient polling error; keep trying until navigated away.
    }
  }

  function renderChatState(conv) {
    document.getElementById("chat-title").textContent = conv.name || "群聊";
    const tokenInfo = document.getElementById("chat-tokens");
    const maxTokens = conv.total_max_tokens != null ? conv.total_max_tokens : "∞";
    tokenInfo.textContent = `输出 ${conv.total_output_tokens || 0} / ${maxTokens} tokens`;
    const statusEl = document.getElementById("chat-status");
    statusEl.textContent = statusLabel(conv.status);
    statusEl.className = "status-pill " + statusClass(conv.status);

    const actions = document.getElementById("chat-actions");
    const isRunning = conv.status === "running";
    const isPaused = conv.status === "paused";

    if (isRunning) {
      actions.innerHTML = '<button id="chat-stop" class="btn btn-danger">暂停</button>';
    } else if (isPaused) {
      actions.innerHTML =
        '<button id="chat-resume" class="btn btn-primary">继续</button>' +
        '<button id="chat-summarize" class="btn">总结</button>';
    } else {
      actions.innerHTML = "";
    }

    const input = document.getElementById("chat-input");
    const sendBtn = document.getElementById("chat-send");
    input.disabled = !isRunning;
    input.placeholder = isRunning
      ? "预约下一轮发言（会跳过调度，直接发言）"
      : isPaused
        ? "对话已暂停，可继续或总结"
        : "对话已结束";
    if (sendBtn) sendBtn.disabled = !isRunning;

    const box = document.getElementById("chat-messages");
    const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 120;
    box.innerHTML = renderMessagesHTML(conv);
    if (nearBottom) box.scrollTop = box.scrollHeight;
  }

  function renderMessagesHTML(conv) {
    const avatars = {};
    (conv.agents || []).forEach((a, i) => {
      avatars[a.name] = { color: avatarColor(a.name), initial: a.name.slice(0, 1) };
    });

    let html = `<div class="msg system"><div class="bubble">对话开始 · ${
      conv.scheduling_mode === "willingness" ? "按意愿调度" : "轮流发言"
    }</div></div>`;

    (conv.messages || []).forEach((m) => {
      if (m.role === "human") {
        html += `
          <div class="msg human">
            <div class="avatar" style="background:${escapeHtml(avatarColor("人类"))}">人</div>
            <div class="msg-body">
              <div class="speaker">人类 · 预约发言</div>
              <div class="bubble">${escapeHtml(m.content)}</div>
            </div>
          </div>
        `;
      } else {
        const a = avatars[m.speaker] || { color: avatarColor(m.speaker), initial: m.speaker.slice(0, 1) };
        let scoreLine = "";
        if (m.scores && m.scores.length) {
          const parts = m.scores.map((s) => `${escapeHtml(s.name)} ${s.score}`).join(" · ");
          scoreLine = `<div class="score-line">意愿分：${parts}</div>`;
        }
        html += `
          <div class="msg agent">
            <div class="avatar" style="background:${escapeHtml(a.color)}">${escapeHtml(a.initial)}</div>
            <div class="msg-body">
              <div class="speaker">${escapeHtml(m.speaker)}</div>
              <div class="bubble">${escapeHtml(m.content)}</div>
              ${scoreLine}
            </div>
          </div>
        `;
      }
    });

    if (conv.summary) {
      html += `
        <div class="msg system"><div class="bubble">对话结束 · 总结如下</div></div>
        <div class="summary-block">
          <h4>📋 总结</h4>
          <p>${escapeHtml(conv.summary)}</p>
        </div>
      `;
    } else if (conv.error) {
      html += `
        <div class="summary-block">
          <h4>⚠️ 运行出错</h4>
          <p>${escapeHtml(conv.error)}</p>
        </div>
      `;
    } else if (conv.status === "running") {
      html += `<div class="msg system"><div class="bubble"><span class="spinner" style="border-color:rgba(0,0,0,.15);border-top-color:#4f6ef7"></span> 正在思考…</div></div>`;
    }

    return html;
  }

  async function sendHumanMessage() {
    const input = document.getElementById("chat-input");
    const content = input.value.trim();
    if (!content) return;
    input.value = "";
    try {
      await api("/api/conversations/" + chatConvId + "/reserve", {
        method: "POST",
        body: JSON.stringify({ content }),
      });
    } catch (e) {
      toast("发送失败：" + e.message);
    }
  }

  async function stopChat() {
    try {
      await api("/api/conversations/" + chatConvId + "/interrupt", {
        method: "POST",
        body: JSON.stringify({}),
      });
      toast("已请求暂停对话");
    } catch (e) {
      toast("停止失败：" + e.message);
    }
  }

  async function resumeChat() {
    try {
      await api("/api/conversations/" + chatConvId + "/resume", {
        method: "POST",
        body: JSON.stringify({}),
      });
      toast("已恢复对话");
      pollChat();
    } catch (e) {
      toast("恢复失败：" + e.message);
    }
  }

  async function summarizeChat() {
    try {
      await api("/api/conversations/" + chatConvId + "/summarize", {
        method: "POST",
        body: JSON.stringify({}),
      });
      toast("已生成总结");
      pollChat();
    } catch (e) {
      toast("总结失败：" + e.message);
    }
  }

  // Handle leaving chat page.
  window.addEventListener("hashchange", () => {
    if (location.hash.indexOf("#/chat/") !== 0) {
      clearChatTimer();
    }
  });

  route();
})();
