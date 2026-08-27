# CLAUDE.md

KDS「侃大山」——让多个 LLM 基于共享背景和各自 system prompt 进行无限制多人群聊的 Web 应用。产品功能与使用说明见 [README.md](README.md)；本文件面向在此仓库工作的 AI/开发者，只记录不易从代码直接看出的约定与结构。

## 技术栈

Flask（无 SQLAlchemy，直接用 sqlite3）+ 原生 JS/CSS 前端（无构建步骤）+ OpenAI 兼容 LLM 客户端。Python 3.10+，依赖见 [requirements.txt](requirements.txt)。

## 常用命令

```bash
python run.py            # 启动服务，默认 http://127.0.0.1:5000
python -m pytest -q      # 运行测试
```

- 测试全部用 `LLMClient(mock=True)` 离线运行，不需要真实 API key，也不联网。
- 无真实 LLM 时想手动体验界面，在 `.env` 设 `LLM_MOCK=true`。
- 配置来自环境变量（`.env`，见 [.env.example](.env.example)），在 [app/config.py](app/config.py) 集中读取。

## 架构要点

- [app/engine.py](app/engine.py) — 核心。`ConversationRunner` 每个对话在**独立后台线程**里跑 `_run()` 主循环。这是并发与状态的关键，改动前务必理解：
  - 所有可变状态用 `self._lock` 保护；对外快照统一走 `to_dict()`。
  - `RUNNERS: dict[str, ConversationRunner]` 是**内存注册表**（模块级全局）。API 优先查 `RUNNERS`，未命中再从 SQLite 用 `ConversationRunner.from_payload()` 复活。
  - 状态机：`running` / `paused`（`paused_reason` 为 `manual` 或 `limit`）/ `completed` / `error`。达到 token/时长上限会转 `paused(limit)`，无法 resume，只能 summarize。
  - 时长统计用「分段累计」：`_active_seconds` + `_segment_start`，暂停时结算，避免把暂停时间计入。
  - `_persist()` 每步写回 SQLite，且吞掉持久化异常——不能让持久化失败杀死对话线程。
  - 投票（`start_vote` / `_run_vote`）也在独立线程运行，只允许在非 `running` 状态发起。
- [app/scheduler.py](app/scheduler.py) — 纯函数调度算法。`willingness_select` 用数值稳定的 softmax：`s_eff = (score - lam*heat)/tau`，`forbid_consecutive` 禁止连续发言，`update_heat` 指数衰减（gamma）防垄断。**2 人对话在 engine 里被强制改为 round_robin。**
- [app/llm.py](app/llm.py) — 唯一的 LLM 出入口。每个用途（`speak`/`willingness_score`/`vote`/`summarize`/`assist`）都有独立方法且**各自带 mock 分支**。解析 LLM 输出很防御性（`_parse_score`/`_parse_vote`/`_extract_json` 处理各种畸形 JSON）。部分兼容服务不支持 `response_format`，`_call` 会去掉它重试。
- [app/db.py](app/db.py) — sqlite3 直连，两张表 `configs` / `conversations`，业务字段统一塞进 `payload` JSON 列。全局 `_lock` 串行化写。`mark_stale_running_conversations()` 在启动时把残留的 `running` 标为 `paused`（进程重启后线程已丢失）。
- [app/assistant.py](app/assistant.py) — 配置助手。多轮对话内存态存在 `sessions` dict，强制 LLM 只输出 `{"reply", "proposal"}` JSON；proposal 需前端人工确认后才应用。
- [app/routes/api.py](app/routes/api.py) — 全部 REST 端点。模块级单例 `_llm` / `_assistant`。`_clean_config` 做入参校验。
- 前端：[app/templates/index.html](app/templates/index.html) 单页 + [app/static/js/app.js](app/static/js/app.js)，通过轮询 `GET /api/conversations/<id>` 刷新聊天状态。

## 约定与不变量

- **关键业务约束：总输出 token 上限和总时长不能同时为无限**，至少设一个（在 `_clean_config` 和创建对话时都校验）。开始群聊至少需要 2 个角色。
- 所有面向用户的文案、LLM prompt、错误信息都用**中文**；`JSON_AS_ASCII=False` 保证中文不转义。
- 时间戳统一 UTC ISO 格式（`_now()`）。
- agent 的 `visibility` 可为 `"all"`、含 `"all"` 的列表、或具体 id/name 列表——决定该角色的设定对谁可见（见 `_build_persona`）。
- 修改 `to_dict()` 的输出结构时注意：它同时用于 API 响应、SQLite 持久化、以及 `from_payload()` 复活，三者必须保持字段兼容。
- 数据库文件在 `data/kds.db`（gitignore），首次运行自动建库。
