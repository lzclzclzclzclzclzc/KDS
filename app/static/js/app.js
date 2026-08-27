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
  let chatConv = null;
  let chatSig = "";
  let wbOpen = false;
  let wbView = "render";

  function defaultDraft() {
    return {
      name: "新的群聊",
      shared_background: "",
      agents: [
        newAgent("a0", "小明"),
        newAgent("a1", "小红"),
      ],
      single_max_tokens: 300,
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
      end_vote_enabled: false,
      end_vote_proposers: [],
      end_vote_cooldown_turns: 3,
      whiteboard_enabled: false,
      whiteboard_format: "md",
      whiteboard_editors: [],
    };
  }

  function newAgent(id, name) {
    return {
      id: id,
      name: name,
      system_prompt: "",
      visibility: [],
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
            <button class="btn-ghost list-delete" data-del="${escapeHtml(c.id)}" title="删除对话">🗑 删除</button>
          </div>
        `
        )
        .join("");
      list.querySelectorAll(".list-item").forEach((el) => {
        el.addEventListener("click", (e) => {
          if (e.target.closest("[data-del]")) return;
          location.hash = "#/chat/" + el.dataset.id;
        });
      });
      list.querySelectorAll("[data-del]").forEach((btn) => {
        btn.addEventListener("click", (e) => {
          e.stopPropagation();
          deleteConversation(btn.dataset.del);
        });
      });
    } catch (e) {
      list.innerHTML = `<div class="empty">加载失败：${escapeHtml(e.message)}</div>`;
    }
  }

  async function deleteConversation(id) {
    if (!id) return;
    if (!window.confirm("确定删除这个对话吗？此操作不可恢复。")) return;
    try {
      await api("/api/conversations/" + id, { method: "DELETE" });
      toast("对话已删除");
      await loadConversations();
    } catch (e) {
      toast("删除失败：" + e.message);
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
      })),
      single_max_tokens:
        cfg.single_max_tokens ||
        ((cfg.agents || []).map((a) => Number(a.max_tokens) || 0).filter(Boolean)[0] ||
          300),
      total_max_tokens: cfg.total_max_tokens || null,
      total_duration_seconds: cfg.total_duration_seconds || null,
      first_speaker: cfg.first_speaker || "random",
      scheduling_mode: cfg.scheduling_mode || "round_robin",
      round_robin_order: cfg.round_robin_order || [],
      scheduler_params: Object.assign(
        { lam: 2.0, tau: 1.5, gamma: 0.7, forbid_consecutive: true },
        cfg.scheduler_params || {}
      ),
      end_vote_enabled: !!cfg.end_vote_enabled,
      end_vote_proposers: cfg.end_vote_proposers || [],
      end_vote_cooldown_turns: cfg.end_vote_cooldown_turns == null ? 3 : cfg.end_vote_cooldown_turns,
      whiteboard_enabled: !!cfg.whiteboard_enabled,
      whiteboard_format: cfg.whiteboard_format || "md",
      whiteboard_editors: cfg.whiteboard_editors || [],
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

    const proposers = Array.isArray(draft.end_vote_proposers) ? draft.end_vote_proposers : [];
    const proposerChips = draft.agents
      .map(
        (a) =>
          `<label class="vis-chip"><input type="checkbox" data-proposer="${escapeHtml(a.id)}" ${
            proposers.includes(a.id) || proposers.includes(a.name) ? "checked" : ""
          } /> ${escapeHtml(a.name)}</label>`
      )
      .join("");

    const wbEditors = Array.isArray(draft.whiteboard_editors) ? draft.whiteboard_editors : [];
    const wbEditorChips = draft.agents
      .map(
        (a) =>
          `<label class="vis-chip"><input type="checkbox" data-wbeditor="${escapeHtml(a.id)}" ${
            wbEditors.includes(a.id) || wbEditors.includes(a.name) ? "checked" : ""
          } /> ${escapeHtml(a.name)}</label>`
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
          <label>单人 max_token</label>
          <input id="cfg-single-tokens" type="number" value="${
            draft.single_max_tokens || 300
          }" />
          <div class="hint">每次发言的输出 token 上限，统一应用于所有角色。</div>
        </div>
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
          <div class="hint">总输出 max_token 和总时长不能同时留空（至少设置一个）。</div>
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

      <div class="field">
        <label><input id="cfg-endvote" type="checkbox" ${
          draft.end_vote_enabled ? "checked" : ""
        } style="width:auto;margin-right:6px" /> 允许角色主动发起「结束对话投票」</label>
        <div class="hint">被选定的角色可在发言中提议结束；全体一致同意才会结束（届时暂停，等你决定继续或总结）。不启用则到最大 token / 时长才停止。</div>
      </div>
      <div id="endvote-config" class="field ${draft.end_vote_enabled ? "" : "hidden"}">
        <label>可发起结束投票的角色（一个或多个）</label>
        <div class="vis-chips">${proposerChips}</div>
        <div class="field" style="margin-top:10px">
          <label>投票未通过后的冷却轮数</label>
          <input id="cfg-endvote-cooldown" type="number" min="0" value="${
            draft.end_vote_cooldown_turns == null ? 3 : draft.end_vote_cooldown_turns
          }" />
          <div class="hint">一次结束投票被否决后，多少轮内不再触发，避免反复打断。</div>
        </div>
      </div>

      <div class="field">
        <label><input id="cfg-whiteboard" type="checkbox" ${
          draft.whiteboard_enabled ? "checked" : ""
        } style="width:auto;margin-right:6px" /> 启用白板（最终产出物）</label>
        <div class="hint">启用后，人类可在对话界面右侧调出白板查看（只读，不中断对话）；有权限的角色会在自己回合增量编辑它。</div>
      </div>
      <div id="whiteboard-config" class="field ${draft.whiteboard_enabled ? "" : "hidden"}">
        <div class="field">
          <label>白板格式</label>
          <select id="cfg-wb-format">
            <option value="md" ${draft.whiteboard_format === "md" ? "selected" : ""}>Markdown</option>
            <option value="html" ${draft.whiteboard_format === "html" ? "selected" : ""}>HTML</option>
          </select>
        </div>
        <div class="field">
          <label>可编辑白板的角色（一个或多个）</label>
          <div class="vis-chips">${wbEditorChips}</div>
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
          <label>system prompt（角色设定） <button type="button" class="btn-ghost sync-prompt" data-sync-prompt>同步给所有人</button></label>
          <textarea class="agent-prompt">${escapeHtml(agent.system_prompt)}</textarea>
        </div>
        <div class="row" style="align-items:flex-start">
          <div class="field grow">
            <label>system prompt 对谁可见</label>
            <div class="vis-chips">${chips}</div>
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
    const singleTokens = $("cfg-single-tokens");
    const duration = $("cfg-duration");
    const firstSel = $("cfg-first");
    const modeSel = $("cfg-mode");

    nameInput.addEventListener("input", () => (draft.name = nameInput.value));
    bgInput.addEventListener("input", () => (draft.shared_background = bgInput.value));
    totalTokens.addEventListener("input", () => {
      draft.total_max_tokens = totalTokens.value ? Number(totalTokens.value) : null;
    });
    singleTokens.addEventListener("input", () => {
      draft.single_max_tokens = Number(singleTokens.value) || 300;
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

    const endvote = $("cfg-endvote");
    if (endvote)
      endvote.addEventListener("change", () => {
        draft.end_vote_enabled = endvote.checked;
        renderConfigForm();
      });
    const endvoteCooldown = $("cfg-endvote-cooldown");
    if (endvoteCooldown)
      endvoteCooldown.addEventListener("input", () => {
        const v = Number(endvoteCooldown.value);
        draft.end_vote_cooldown_turns = Number.isFinite(v) && v >= 0 ? v : 0;
      });
    document.querySelectorAll("input[data-proposer]").forEach((chip) => {
      chip.addEventListener("change", () => {
        const pid = chip.dataset.proposer;
        if (!Array.isArray(draft.end_vote_proposers)) draft.end_vote_proposers = [];
        const agent = draft.agents.find((a) => a.id === pid);
        if (chip.checked) {
          if (!draft.end_vote_proposers.includes(pid)) draft.end_vote_proposers.push(pid);
        } else {
          draft.end_vote_proposers = draft.end_vote_proposers.filter(
            (p) => p !== pid && !(agent && p === agent.name)
          );
        }
      });
    });

    const wbEnable = $("cfg-whiteboard");
    if (wbEnable)
      wbEnable.addEventListener("change", () => {
        draft.whiteboard_enabled = wbEnable.checked;
        renderConfigForm();
      });
    const wbFormat = $("cfg-wb-format");
    if (wbFormat)
      wbFormat.addEventListener("change", () => {
        draft.whiteboard_format = wbFormat.value;
      });
    document.querySelectorAll("input[data-wbeditor]").forEach((chip) => {
      chip.addEventListener("change", () => {
        const eid = chip.dataset.wbeditor;
        if (!Array.isArray(draft.whiteboard_editors)) draft.whiteboard_editors = [];
        const agent = draft.agents.find((a) => a.id === eid);
        if (chip.checked) {
          if (!draft.whiteboard_editors.includes(eid)) draft.whiteboard_editors.push(eid);
        } else {
          draft.whiteboard_editors = draft.whiteboard_editors.filter(
            (p) => p !== eid && !(agent && p === agent.name)
          );
        }
      });
    });

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
      card.querySelector("[data-sync-prompt]").addEventListener("click", () => {
        if (draft.agents.length < 2) return;
        const source = agent.system_prompt;
        draft.agents.forEach((other) => {
          if (other.id !== id) other.system_prompt = source;
        });
        renderConfigForm();
        toast("已将该角色的 system prompt 同步给所有人");
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

  function validateLimits() {
    const t = draft.total_max_tokens;
    const d = draft.total_duration_seconds;
    if (!t && !d) {
      return "总输出 max_token 和总对话时长不能同时为无限，至少设置一个";
    }
    return null;
  }

  async function saveConfig() {
    const limitError = validateLimits();
    if (limitError) {
      toast(limitError);
      return;
    }
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
    const limitError = validateLimits();
    if (limitError) {
      toast(limitError);
      return;
    }
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
    if (proposal.single_max_tokens != null) {
      draft.single_max_tokens = Number(proposal.single_max_tokens);
    }
    if (proposal.agents) {
      for (const name of Object.keys(proposal.agents)) {
        const agent = draft.agents.find((a) => a.name === name);
        if (!agent) continue;
        const patch = proposal.agents[name];
        if (patch.system_prompt != null) agent.system_prompt = patch.system_prompt;
        if (patch.visibility != null) agent.visibility = patch.visibility;
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
    wbOpen = false;
    wbView = "render";
    app.innerHTML = `
      <div class="chat-shell">
        <div class="chat-header">
          <button class="back" data-back>‹</button>
          <div class="title" id="chat-title">加载中…</div>
          <div class="token-info" id="chat-tokens"></div>
          <span id="chat-status" class="status-pill status-running">进行中</span>
          <div id="chat-countdown" class="countdown"></div>
          <div id="chat-actions"></div>
          <button id="chat-whiteboard-toggle" class="btn btn-ghost hidden">📋 白板</button>
        </div>
        <div id="chat-messages" class="chat-messages"></div>
        <div class="chat-footer">
          <select id="chat-target" class="chat-target"></select>
          <input id="chat-input" type="text" placeholder="预约下一轮发言（会跳过调度，直接发言）" />
          <button id="chat-send" class="btn btn-primary">发送</button>
        </div>
        <div id="whiteboard-layer" class="whiteboard-layer">
          <div class="whiteboard-head">
            <strong>📋 白板</strong>
            <span id="wb-meta" class="wb-meta"></span>
            <span class="grow"></span>
            <div class="wb-tabs">
              <button class="wb-tab active" data-wb-view="render">渲染</button>
              <button class="wb-tab" data-wb-view="raw">源码</button>
            </div>
            <button id="wb-close" class="btn-ghost" title="收起白板">×</button>
          </div>
          <div id="whiteboard-body" class="whiteboard-body"></div>
        </div>
      </div>
    `;

    app.querySelector("[data-back]").addEventListener("click", () => {
      location.hash = "#/";
    });
    app.querySelector("#chat-whiteboard-toggle").addEventListener("click", () => {
      wbOpen = !wbOpen;
      renderWhiteboard(chatConv);
    });
    app.querySelector("#wb-close").addEventListener("click", () => {
      wbOpen = false;
      renderWhiteboard(chatConv);
    });
    app.querySelector("#whiteboard-layer .wb-tabs").addEventListener("click", (e) => {
      const btn = e.target.closest("[data-wb-view]");
      if (!btn) return;
      wbView = btn.dataset.wbView;
      renderWhiteboard(chatConv);
    });
    app.querySelector("#chat-actions").addEventListener("click", (e) => {
      const id = e.target.id;
      if (id === "chat-stop") stopChat();
      else if (id === "chat-resume") resumeChat();
      else if (id === "chat-summarize") summarizeChat();
      else if (id === "chat-vote") showVoteModal();
      else if (id === "chat-extend") showExtendModal();
    });
    app.querySelector("#chat-send").addEventListener("click", sendHumanMessage);
    app.querySelector("#chat-input").addEventListener("keydown", (e) => {
      if (e.key === "Enter") sendHumanMessage();
    });

    pollChat();
    window.chatTimer = setInterval(pollChat, 1000);
    window.countdownTimer = setInterval(renderCountdown, 1000);
  }

  function clearChatTimer() {
    if (window.chatTimer) {
      clearInterval(window.chatTimer);
      window.chatTimer = null;
    }
    if (window.countdownTimer) {
      clearInterval(window.countdownTimer);
      window.countdownTimer = null;
    }
  }

  // Anchor the countdown to the last server value + a local timestamp, then let a
  // 1s client ticker interpolate — so it steps exactly once per second regardless
  // of polling/network jitter, and re-syncs to the server on every poll.
  let countdownAnchor = null;

  function updateCountdown(conv) {
    if (
      conv.status === "running" &&
      conv.total_duration_seconds != null &&
      conv.remaining_seconds != null
    ) {
      countdownAnchor = { remaining: conv.remaining_seconds, at: Date.now() };
    } else {
      countdownAnchor = null;
    }
    renderCountdown();
  }

  function renderCountdown() {
    const el = document.getElementById("chat-countdown");
    if (!el) return;
    if (!countdownAnchor) {
      el.textContent = "";
      el.classList.remove("countdown-warning");
      return;
    }
    const elapsed = (Date.now() - countdownAnchor.at) / 1000;
    const seconds = Math.max(0, Math.floor(countdownAnchor.remaining - elapsed));
    const mm = String(Math.floor(seconds / 60)).padStart(2, "0");
    const ss = String(seconds % 60).padStart(2, "0");
    el.textContent = `倒计时 ${mm}:${ss}`;
    el.classList.toggle("countdown-warning", seconds <= 60);
  }

  async function pollChat() {
    if (!chatConvId) return;
    try {
      const conv = await api("/api/conversations/" + chatConvId);
      chatConv = conv;
      updateCountdown(conv);
      const sig = JSON.stringify({
        s: conv.status,
        n: (conv.messages || []).length,
        last: conv.messages && conv.messages.length ? conv.messages[conv.messages.length - 1] : null,
        summary: conv.summary || "",
        er: conv.ended_reason || "",
        pr: conv.paused_reason || "",
        cr: !!conv.can_resume,
        v: conv.votes || [],
        wb: conv.whiteboard ? (conv.whiteboard.enabled ? 1 : 0) + ":" + (conv.whiteboard.rev || 0) : "",
      });
      if (sig !== chatSig) {
        chatSig = sig;
        renderChatState(conv);
      }
      const hasActiveVote = (conv.votes || []).some(
        (v) => v.status === "pending" || v.status === "running"
      );
      if ((conv.status === "completed" || conv.status === "error") && !hasActiveVote && window.chatTimer) {
        clearChatTimer();
      }
      if (hasActiveVote && !window.chatTimer) {
        window.chatTimer = setInterval(pollChat, 1000);
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
    const isLimitPause = isPaused && conv.paused_reason === "limit";
    const canResume = !!conv.can_resume;

    if (isRunning) {
      actions.innerHTML = '<button id="chat-stop" class="btn btn-danger">暂停</button>';
    } else if (isPaused) {
      actions.innerHTML =
        (canResume ? '<button id="chat-resume" class="btn btn-primary">继续对话</button>' : "") +
        '<button id="chat-extend" class="btn">延长上限</button>' +
        '<button id="chat-vote" class="btn">发起投票</button>' +
        '<button id="chat-summarize" class="btn">总结并完成</button>';
    } else {
      actions.innerHTML = "";
    }

    const input = document.getElementById("chat-input");
    const sendBtn = document.getElementById("chat-send");
    const canSpeak = isRunning || isPaused;
    input.disabled = !canSpeak;
    input.placeholder = isRunning
      ? "预约下一轮发言（会跳过调度，直接发言）"
      : isLimitPause
        ? "已达上限：可延长上限、发言、投票或总结"
        : isPaused
          ? "对话已暂停：可发言、继续、投票或总结"
          : "对话已结束";
    if (sendBtn) sendBtn.disabled = !canSpeak;

    const targetSel = document.getElementById("chat-target");
    if (targetSel) {
      const prev = targetSel.value;
      const opts = ['<option value="">指定回答：不指定</option>'].concat(
        (conv.agents || []).map(
          (a) => `<option value="${escapeHtml(a.id)}">指定「${escapeHtml(a.name)}」回答</option>`
        )
      );
      targetSel.innerHTML = opts.join("");
      if (prev && targetSel.querySelector(`option[value="${CSS.escape(prev)}"]`)) {
        targetSel.value = prev;
      }
      targetSel.disabled = !canSpeak;
    }

    const box = document.getElementById("chat-messages");
    const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 120;
    box.innerHTML = renderMessagesHTML(conv);
    if (nearBottom) box.scrollTop = box.scrollHeight;

    renderWhiteboard(conv);
  }

  function renderWhiteboard(conv) {
    const toggle = document.getElementById("chat-whiteboard-toggle");
    const layer = document.getElementById("whiteboard-layer");
    if (!toggle || !layer) return;
    const wb = conv && conv.whiteboard;
    if (!wb || !wb.enabled) {
      toggle.classList.add("hidden");
      layer.classList.remove("open");
      return;
    }
    toggle.classList.remove("hidden");
    toggle.classList.toggle("active", wbOpen);
    layer.classList.toggle("open", wbOpen);
    if (!wbOpen) return;

    const meta = document.getElementById("wb-meta");
    if (meta) {
      const fmt = wb.format === "html" ? "HTML" : "Markdown";
      const parts = [fmt];
      if (wb.last_editor) parts.push("最近编辑：" + wb.last_editor);
      if (wb.rev) parts.push("v" + wb.rev);
      meta.textContent = parts.join(" · ");
    }
    layer
      .querySelectorAll(".wb-tab")
      .forEach((t) => t.classList.toggle("active", t.dataset.wbView === wbView));

    const body = document.getElementById("whiteboard-body");
    const content = (wb.content || "").toString();
    if (!content.trim()) {
      body.innerHTML = '<div class="empty">白板还是空的，等有编辑权限的角色来填充。</div>';
      return;
    }
    if (wbView === "raw") {
      body.innerHTML = `<pre class="wb-raw">${escapeHtml(content)}</pre>`;
    } else if (wb.format === "html") {
      const iframe = document.createElement("iframe");
      iframe.className = "wb-frame";
      iframe.setAttribute("sandbox", "");
      iframe.srcdoc = content;
      body.innerHTML = "";
      body.appendChild(iframe);
    } else {
      body.innerHTML = `<div class="wb-md">${renderMarkdown(content)}</div>`;
    }
  }

  function renderMarkdown(src) {
    const esc = (s) =>
      String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    const codeBlocks = [];
    src = String(src).replace(/```(\w*)\n([\s\S]*?)```/g, (m, lang, code) => {
      codeBlocks.push(`<pre class="wb-code"><code>${esc(code)}</code></pre>`);
      return `@@@CB${codeBlocks.length - 1}@@@`;
    });
    const inline = (t) => {
      t = esc(t);
      t = t.replace(/`([^`]+)`/g, (m, c) => `<code>${c}</code>`);
      t = t.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
      t = t.replace(/\*([^*]+)\*/g, "<em>$1</em>");
      t = t.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (m, txt, url) => {
        const safe = /^(https?:|mailto:|#|\/|\.)/i.test(url) ? url : "#";
        return `<a href="${encodeURI(safe)}" target="_blank" rel="noopener">${txt}</a>`;
      });
      return t;
    };
    const lines = src.split(/\r?\n/);
    let html = "";
    let inUl = false;
    let inOl = false;
    const closeLists = () => {
      if (inUl) {
        html += "</ul>";
        inUl = false;
      }
      if (inOl) {
        html += "</ol>";
        inOl = false;
      }
    };
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      const cb = line.match(/^@@@CB(\d+)@@@$/);
      if (cb) {
        closeLists();
        html += codeBlocks[Number(cb[1])];
        continue;
      }
      if (/^\s*$/.test(line)) {
        closeLists();
        continue;
      }
      let m;
      if ((m = line.match(/^(#{1,6})\s+(.*)$/))) {
        closeLists();
        const lv = m[1].length;
        html += `<h${lv}>${inline(m[2])}</h${lv}>`;
      } else if (/^\s*([-*+])\s+/.test(line)) {
        if (!inUl) {
          closeLists();
          html += "<ul>";
          inUl = true;
        }
        html += `<li>${inline(line.replace(/^\s*[-*+]\s+/, ""))}</li>`;
      } else if (/^\s*\d+\.\s+/.test(line)) {
        if (!inOl) {
          closeLists();
          html += "<ol>";
          inOl = true;
        }
        html += `<li>${inline(line.replace(/^\s*\d+\.\s+/, ""))}</li>`;
      } else if (/^\s*>\s?/.test(line)) {
        closeLists();
        html += `<blockquote>${inline(line.replace(/^\s*>\s?/, ""))}</blockquote>`;
      } else if (/^\s*(---|\*\*\*|___)\s*$/.test(line)) {
        closeLists();
        html += "<hr>";
      } else {
        closeLists();
        html += `<p>${inline(line)}</p>`;
      }
    }
    closeLists();
    return html;
  }

  function renderMessagesHTML(conv) {
    const avatars = {};
    (conv.agents || []).forEach((a, i) => {
      avatars[a.name] = { color: avatarColor(a.name), initial: a.name.slice(0, 1) };
    });

    let html = `<div class="msg system"><div class="bubble">对话开始 · ${
      conv.scheduling_mode === "willingness" ? "按意愿调度" : "轮流发言"
    }</div></div>`;

    const renderMessage = (m) => {
      if (m.role === "human") {
        return `
          <div class="msg human">
            <div class="avatar" style="background:${escapeHtml(avatarColor("人类"))}">人</div>
            <div class="msg-body">
              <div class="speaker">人类 · 预约发言</div>
              <div class="msg-time">${escapeHtml(fmtTime(m.ts))}</div>
              <div class="bubble">${escapeHtml(m.content)}</div>
            </div>
          </div>
        `;
      }

      const a = avatars[m.speaker] || {
        color: avatarColor(m.speaker),
        initial: m.speaker.slice(0, 1),
      };
      let scoreLine = "";
      if (m.scores && m.scores.length) {
        const parts = m.scores.map((s) => `${escapeHtml(s.name)} ${s.score}`).join(" · ");
        scoreLine = `<div class="score-line">意愿分：${parts}</div>`;
      }
      const endTag = m.proposed_end
        ? '<div class="score-line">🔚 该角色提议结束对话，已发起全体投票</div>'
        : "";
      const wbTag = m.wb_edited
        ? '<div class="score-line">📝 更新了白板</div>'
        : "";
      return `
        <div class="msg agent">
          <div class="avatar" style="background:${escapeHtml(a.color)}">${escapeHtml(a.initial)}</div>
          <div class="msg-body">
            <div class="speaker">${escapeHtml(m.speaker)}</div>
            <div class="msg-time">${escapeHtml(fmtTime(m.ts))}</div>
            <div class="bubble">${escapeHtml(m.content)}</div>
            ${scoreLine}
            ${endTag}
            ${wbTag}
          </div>
        </div>
      `;
    };

    const events = [];
    (conv.messages || []).forEach((m) => {
      events.push({ ts: m.ts || "", kind: "message", data: m });
    });
    (conv.votes || []).forEach((v) => {
      events.push({ ts: v.created_at || "", kind: "vote", data: v });
    });
    events.sort((a, b) => String(a.ts || "").localeCompare(String(b.ts || "")));
    events.forEach((event) => {
      if (event.kind === "message") {
        html += renderMessage(event.data);
      } else {
        html += voteBlockHTML(event.data);
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
    const targetSel = document.getElementById("chat-target");
    const target = targetSel ? targetSel.value : "";
    input.value = "";
    try {
      await api("/api/conversations/" + chatConvId + "/reserve", {
        method: "POST",
        body: JSON.stringify({ content, target }),
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

  function closeExtendModal() {
    const el = document.getElementById("extend-modal");
    if (el) el.remove();
  }

  function showExtendModal() {
    const conv = chatConv || {};
    closeExtendModal();
    const overlay = document.createElement("div");
    overlay.id = "extend-modal";
    overlay.className = "modal-overlay";
    overlay.innerHTML = `
      <div class="modal">
        <div class="modal-head">
          <strong>延长上限</strong>
          <button type="button" class="btn-ghost" data-close>×</button>
        </div>
        <div class="modal-body">
          <div class="field">
            <label>总输出 max_token（已消耗 ${conv.total_output_tokens || 0}）</label>
            <input id="extend-tokens" type="number" value="${
              conv.total_max_tokens == null ? "" : conv.total_max_tokens
            }" placeholder="留空表示不限" />
          </div>
          <div class="field">
            <label>总对话时长（秒，已进行 ${Math.floor(conv.elapsed_seconds || 0)}）</label>
            <input id="extend-duration" type="number" value="${
              conv.total_duration_seconds == null ? "" : conv.total_duration_seconds
            }" placeholder="留空表示不限" />
          </div>
          <div class="hint">两者不能同时留空。改大后即可点「继续对话」。</div>
        </div>
        <div class="modal-foot">
          <button type="button" class="btn" data-cancel>取消</button>
          <button type="button" id="extend-submit" class="btn btn-primary">保存</button>
        </div>
      </div>
    `;
    document.body.appendChild(overlay);
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) closeExtendModal();
    });
    overlay.querySelector("[data-close]").addEventListener("click", closeExtendModal);
    overlay.querySelector("[data-cancel]").addEventListener("click", closeExtendModal);
    overlay.querySelector("#extend-submit").addEventListener("click", submitExtend);
  }

  async function submitExtend() {
    const modal = document.getElementById("extend-modal");
    if (!modal) return;
    const t = modal.querySelector("#extend-tokens").value;
    const d = modal.querySelector("#extend-duration").value;
    try {
      await api("/api/conversations/" + chatConvId + "/limits", {
        method: "POST",
        body: JSON.stringify({
          total_max_tokens: t ? Number(t) : null,
          total_duration_seconds: d ? Number(d) : null,
        }),
      });
      toast("上限已更新，可点「继续对话」");
      closeExtendModal();
      chatSig = "";
      pollChat();
    } catch (e) {
      toast("更新失败：" + e.message);
    }
  }

  function closeVoteModal() {
    const el = document.getElementById("vote-modal");
    if (el) el.remove();
  }

  function showVoteModal() {
    closeVoteModal();
    const overlay = document.createElement("div");
    overlay.id = "vote-modal";
    overlay.className = "modal-overlay";
    overlay.innerHTML = `
      <div class="modal">
        <div class="modal-head">
          <strong>发起投票</strong>
          <button type="button" class="btn-ghost" data-close>×</button>
        </div>
        <div class="modal-body">
          <div class="field">
            <label>投票题目</label>
            <input id="vote-question" type="text" placeholder="请输入题干" />
          </div>
          <div class="field">
            <label>选项（至少 2 个）</label>
            <div id="vote-options"></div>
            <button type="button" id="vote-add-option" class="btn btn-ghost" style="margin-top:6px">＋ 添加选项</button>
          </div>
          <div class="field">
            <label>每人票数</label>
            <input id="vote-tickets" type="number" value="1" min="1" max="20" />
          </div>
        </div>
        <div class="modal-foot">
          <button type="button" class="btn" data-cancel>取消</button>
          <button type="button" id="vote-submit" class="btn btn-primary">发送投票</button>
        </div>
      </div>
    `;
    document.body.appendChild(overlay);

    let optionCount = 2;
    function renderOptions() {
      const box = overlay.querySelector("#vote-options");
      box.innerHTML = "";
      for (let i = 1; i <= optionCount; i++) {
        const row = document.createElement("div");
        row.className = "option-row";
        row.innerHTML = `
          <span class="option-index">${i}</span>
          <input data-option="${i}" type="text" placeholder="选项 ${i}" />
          <button type="button" data-remove-option="${i}" class="btn-ghost">删除</button>
        `;
        box.appendChild(row);
      }
    }
    renderOptions();

    overlay.querySelector("#vote-add-option").addEventListener("click", () => {
      optionCount += 1;
      renderOptions();
    });
    overlay.querySelector("#vote-options").addEventListener("click", (e) => {
      const btn = e.target.closest("[data-remove-option]");
      if (btn && optionCount > 2) {
        optionCount -= 1;
        renderOptions();
      }
    });
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) closeVoteModal();
    });
    overlay.querySelector("[data-close]").addEventListener("click", closeVoteModal);
    overlay.querySelector("[data-cancel]").addEventListener("click", closeVoteModal);
    overlay.querySelector("#vote-submit").addEventListener("click", submitVote);
  }

  async function submitVote() {
    const modal = document.getElementById("vote-modal");
    if (!modal) return;
    const question = modal.querySelector("#vote-question").value.trim();
    const optionInputs = Array.from(modal.querySelectorAll("[data-option]")).sort(
      (a, b) => Number(a.dataset.option) - Number(b.dataset.option)
    );
    const options = optionInputs.map((i) => i.value.trim()).filter(Boolean);
    const votesPerPerson = Number(modal.querySelector("#vote-tickets").value) || 1;
    if (!question) {
      toast("请填写投票题目");
      return;
    }
    if (options.length < 2) {
      toast("至少需要 2 个有效选项");
      return;
    }
    const btn = modal.querySelector("#vote-submit");
    btn.disabled = true;
    btn.textContent = "发送中…";
    try {
      await api("/api/conversations/" + chatConvId + "/votes", {
        method: "POST",
        body: JSON.stringify({
          question,
          options,
          votes_per_person: votesPerPerson,
        }),
      });
      toast("投票已发起，等待各角色投票");
      closeVoteModal();
      pollChat();
    } catch (e) {
      toast("发起投票失败：" + e.message);
      btn.disabled = false;
      btn.textContent = "发送投票";
    }
  }

  function totalVotes(vote) {
    return Object.values(vote.results || {}).reduce(
      (sum, value) => sum + (Number(value) || 0),
      0
    );
  }

  function voteBlockHTML(vote) {
    const statusText =
      vote.status === "completed"
        ? "已完成"
        : vote.status === "error"
          ? "失败"
          : "投票中";
    let resultsHtml = "";
    const total = totalVotes(vote);
    if (vote.results && Object.keys(vote.results).length) {
      resultsHtml = (vote.options || [])
        .map((option, idx) => {
          const key = String(idx + 1);
          const count = Number(vote.results[key]) || 0;
          const pct = total > 0 ? Math.round((count / total) * 100) : 0;
          return `
            <div class="vote-result-row">
              <span class="vote-option-label">${idx + 1}. ${escapeHtml(option)}</span>
              <span class="vote-bar"><span style="width:${pct}%"></span></span>
              <span class="vote-count">${count} 票</span>
            </div>
          `;
        })
        .join("");
    } else {
      resultsHtml = '<div class="vote-pending">等待各角色提交…</div>';
    }
    const ballots = (vote.ballots || [])
      .map(
        (b) =>
          `<div class="vote-ballot"><strong>${escapeHtml(
            b.agent_name
          )}</strong> 投 ${(b.choices || [])
            .map((c) => `选项 ${escapeHtml(c)}`)
            .join("、")}</div>`
      )
      .join("");
    return `
      <div class="vote-block">
        <div class="vote-head">${
          vote.kind === "end" ? "🔚 结束投票" : "🗳️ 投票"
        } · ${statusText}${
          vote.kind === "end" && vote.status === "completed"
            ? vote.agreed
              ? " · 已通过，全体同意结束"
              : " · 未通过，继续对话"
            : ""
        }</div>
        <div class="vote-question">${escapeHtml(vote.question)}</div>
        <div class="vote-options">${resultsHtml}</div>
        ${ballots ? `<div class="vote-ballots">${ballots}</div>` : ""}
      </div>
    `;
  }

  // Handle leaving chat page.
  window.addEventListener("hashchange", () => {
    if (location.hash.indexOf("#/chat/") !== 0) {
      clearChatTimer();
    }
  });

  route();
})();
