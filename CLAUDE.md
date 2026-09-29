# CLAUDE.md

KDS「侃大山」——让多个 LLM 基于共享背景和各自 system prompt 进行无限制多人群聊的 Web 应用。产品功能与使用说明见 [README.md](README.md)；本文件面向在此仓库工作的 AI/开发者，只记录不易从代码直接看出的约定与结构。

## 技术栈

Flask（无 SQLAlchemy，直接用 sqlite3）+ 原生 JS/CSS 前端（无构建步骤）+ OpenAI 兼容 LLM 客户端。Python 3.10+，依赖见 [requirements.txt](requirements.txt)。

## 常用命令

```bash
python run.py            # 启动服务，默认 http://127.0.0.1:5000
python -m pytest -q      # 运行测试
```

- 默认测试使用 mock，不需要真实 API key；`KDS_TEST_DSH_RUNTIME=1` 启用的集成测试启动真实运行时，只连接本地模拟 API。`KDS_TEST_DEEPSEEK_LIVE=1` 则使用 `.env` 的真实密钥并消耗额度，仅在获准后运行。
- 无真实 LLM 时想手动体验界面，在 `.env` 设 `LLM_MOCK=true`。
- 配置来自环境变量（`.env`，见 [.env.example](.env.example)），在 [app/config.py](app/config.py) 集中读取。

## 架构要点

- `dsh` 分支新增 `app/harness.py`（官方 Python SDK 适配）和 `app/dsh/bridge.mjs`（Cordis overlay）。新建讨论默认 `AGENT_BACKEND=dsh`，`LLM_MOCK=true` 完全跳过 dsh。辅助评分、投票和总结仍走 `LLMClient`；历史 payload 未记录后端时恢复成 `direct`。
- dsh 角色按 conversation/agent ID 哈希分配独立 home 与 workspace。`harness_state` 和 `config.agent_backend` 必须随 SQLite payload 持久化。发送请求前持久化 `pending`；成功发言和游标在同一次提交中更新。SDK 发布版不能跨进程复用原 session ID；暂停会释放运行时，恢复时使用新 ID + 公开记录 + 有界的工具资料摘录，保留旧日志。不要宣称私有上下文无损恢复。
- dsh 用量通过通知实时计入引擎；成功返回时不能再加一次。失败/中断仍保留已报告的消耗，丢弃未完成的发言和白板操作。KDS 的 `_build_system` JSON 约束只适用于最终交付，不能禁止中间工具调用。`bridge.mjs` 的提示词变量值只展开一次，避免旧版 dsh 将角色或白板中的 `{{...}}` 当作模板。
- SDK 依赖固定在 `requirements-dsh.txt`，接入测试见 `tests/test_harness.py`、`tests/test_harness_integration.py` 和 `tests/test_dsh_bridge.mjs`。真实运行时测试须显式设置 `KDS_TEST_DSH_RUNTIME=1`，只连接本机模拟 API。
- `DSH_BASE_URL` 独立覆盖 Harness 子进程的地址，不能连带修改 `LLM_BASE_URL`。SDK 配套 0.1.5 使用普通 Chat Completions 根地址；本机 0.1.7 使用 `https://api.deepseek.com/anthropic`。模拟 API 必须校验请求路径，避免将真实服务会拒绝的 `/v1/messages` 当作成功请求。
- 最终输出解析使用 JSON decoder，不能用非贪婪代码围栏正则截取（白板 JSON 字符串也会包含代码围栏）。`FinalFormatError` 才能触发 `app/json_repair.py` 的 Chat Completions JSON mode 请求；`DSH_JSON_BASE_URL` 独立于 Messages 地址，密钥/模型复用 DSH 配置。最多两次修正（0 关闭），共用本轮步骤、时间、输出预算，所有已报告用量经 `on_usage` 结算一次。有效发言与白板操作在本地保留；原文没有可解析的白板则不接受修正模型新增操作，不能重跑工具或把无效回复直接发布。空正文、已知截断、取消和运行时错误不属于格式修正。回归覆盖见 `tests/test_parsing.py`、`tests/test_json_repair.py`。

- [app/engine.py](app/engine.py) — 核心。`ConversationRunner` 每个对话在**独立后台线程**里跑 `_run()` 主循环。这是并发与状态的关键，改动前务必理解：
  - 所有可变状态用 `self._lock` 保护；对外快照统一走 `to_dict()`。
  - `RUNNERS: dict[str, ConversationRunner]` 是**内存注册表**（模块级全局）。API 优先查 `RUNNERS`，未命中再从 SQLite 用 `ConversationRunner.from_payload()` 复活。
  - 状态机：`running` / `paused`（`paused_reason` 为 `manual` / `limit` / `vote_end` / `error`）/ `completed`。**唯一进入 `completed` 的路径是人类手动 `summarize_now()`**；达到 token/时长上限只转 `paused(limit)`，角色一致投票结束只转 `paused(vote_end)`，都不自动完成。
  - `resume()` 的放行条件是 `paused 且 not _limit_reached()`——所以 `vote_end`/`manual` 可直接继续；`limit` 必须先经 `POST /limits`（`update_limits`）把上限改大到超过已消耗才能续跑。`to_dict()` 的 `can_resume` 字段即前端「继续对话」按钮的开关。`_limit_reached` 有持锁/免锁两版（`_limit_reached_nolock` 供 `to_dict`/`resume` 在已持锁时调用，避免 `threading.Lock` 不可重入导致死锁）。
  - **结构化回合输出**：dsh 后端走 `harness.run_turn()`，direct/mock 走 `llm.agent_turn()`，均返回结构化 dict `{speech, propose_end, whiteboard_ops}`。dsh 严格校验最终 JSON，不接受非 JSON 发言；direct 保留 `_parse_turn` 的文本兜底。能力字段由 `_build_system` 按角色能力注入到 system 里——**新功能就加一个新字段，不要再用文本标记**。
  - 主动结束投票（item 3）：`end_vote_enabled` 时，`end_vote_proposers` 里的角色 system 里声明 `propose_end` 字段；该角色某回合 `propose_end=true` 时，`_run` **内联**（非独立线程，保持锁/持久化一致）跑 `_run_end_vote_inline()`——全体一致「同意结束」才转 `vote_end`，否则设 `_end_vote_block_until_turn` 冷却若干轮。该投票以 `kind:"end"` 记入 `votes`。
  - 白板（最终产出物）：`whiteboard_enabled` 时，`whiteboard_editors` 里的角色 system 里声明 `whiteboard` 字段并附上当前白板全文；其回合返回的 `whiteboard_ops`（增量 op：`append`/`replace`/`prepend`/`set`）由 `_apply_whiteboard_ops` **在同一回合内联应用**（发言→改白板→再继续，避免与下个发言者读写重叠）。白板状态 `whiteboard_content`/`whiteboard_rev`/`whiteboard_last_editor` 在 `to_dict` 的 `whiteboard` 块里；人类只读。`whiteboard_format` 为 `md`/`html`。
  - 时长统计用「分段累计」：`_active_seconds` + `_segment_start`，暂停时结算，避免把暂停时间计入。
  - `_persist()` 每步写回 SQLite，且吞掉持久化异常——不能让持久化失败杀死对话线程。
  - 人类发言二态：`running` 时 `human_say` 走 `pending_human_message` 下一轮注入；`paused` 时直接 append 到 messages 并持久化（立即可见）。可选 `target`（agent id/name）指定下一个发言者：running 时目标随 `pending_human_target` 一起入队，在**人类消息被追加的同一时刻**才置 `_forced_next_idx`（不能在 reserve 时就置，否则可能被正在进行的一次 agent 回合抢先消费，导致指定失效）；`_forced_next_idx` 在下一个 agent 回合被消费一次即清空，覆盖任何调度模式与 `forbid_consecutive`。
  - 投票（`start_vote` / `_run_vote`，人类发起）也在独立线程运行，只允许在非 `running` 状态发起。
- [app/scheduler.py](app/scheduler.py) — 纯函数调度算法。`willingness_select` 用数值稳定的 softmax：`s_eff = (score - lam*heat)/tau`，`forbid_consecutive` 禁止连续发言，`update_heat` 指数衰减（gamma）防垄断。**2 人对话在 engine 里被强制改为 round_robin。**
- [app/llm.py](app/llm.py) — direct 发言及辅助 LLM 调用的出入口。每个用途（`agent_turn`（结构化发言回合）/`willingness_score`/`vote`/`summarize`/`assist`）都有独立方法且**各自带 mock 分支**。解析 LLM 输出很防御性（`_parse_turn`/`_parse_score`/`_parse_vote`/`_extract_json` 处理各种畸形 JSON）。部分兼容服务不支持 `response_format`，`_call` 会去掉它重试。（旧的纯文本 `speak` 仍保留但引擎已不用。）
- [app/db.py](app/db.py) — sqlite3 直连，两张表 `configs` / `conversations`，业务字段统一塞进 `payload` JSON 列。全局 `_lock` 串行化写。`mark_stale_running_conversations()` 在启动时把残留的 `running` 标为 `paused`（进程重启后线程已丢失）。
- [app/assistant.py](app/assistant.py) — 配置助手。多轮对话内存态存在 `sessions` dict，强制 LLM 只输出 `{"reply", "proposal"}` JSON；proposal 需前端人工确认后才应用。
- [app/routes/api.py](app/routes/api.py) — 全部 REST 端点。模块级单例 `_llm` / `_assistant`。`_clean_config` 做入参校验。
- 前端：[app/templates/index.html](app/templates/index.html) 单页 + [app/static/js/app.js](app/static/js/app.js)，通过轮询 `GET /api/conversations/<id>` 刷新聊天状态。

## 约定与不变量

- **关键业务约束：总输出 token 上限和总时长不能同时为无限**，至少设一个（在 `_clean_config`、创建对话、以及 `POST /limits` 延长时都校验）。开始群聊至少需要 2 个角色。启用 `end_vote_enabled` 时至少要选定一个 `end_vote_proposers`。
- 所有面向用户的文案、LLM prompt、错误信息都用**中文**；`JSON_AS_ASCII=False` 保证中文不转义。
- 时间戳统一 UTC ISO 格式（`_now()`）。
- agent 的 `visibility` 可为 `"all"`、含 `"all"` 的列表、或具体 id/name 列表——决定该角色的设定对谁可见（见 `_build_persona`）。
- 修改 `to_dict()` 的输出结构时注意：它同时用于 API 响应、SQLite 持久化、以及 `from_payload()` 复活，三者必须保持字段兼容。
- 数据库文件在 `data/kds.db`（gitignore），首次运行自动建库。
