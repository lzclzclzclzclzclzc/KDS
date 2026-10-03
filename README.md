# KDS 侃大山

让多个 LLM 根据预设背景和各自的 system prompt 进行无限制的多人聊天。前端为浅色简洁界面，聊天页类似微信，按时间纵向罗列消息。

## 功能

- 开始页：进入配置页、查看历史对话
- 配置页：配置人数、每个角色的 system prompt（可设置对谁可见）、共享背景、单次发言 token 上限、总输出 token 上限、总时长（两者可单个设为无限，但不能同时无限）、首个发言人、发言调度模式（轮流 / 按意愿）
- 配置助手：在配置页右侧与助手多轮对话，自动生成或修改 system prompt 与共享背景，应用前需人工确认
- 主动结束投票（可选）：在配置页勾选后，指定一个或多个角色可在发言中提议结束对话；提议会发起一次全体投票，只有所有角色都同意才会结束（结束后仍是暂停，等你决定）。不启用时则到最大 token / 时长才停止
- 白板（可选，最终产出物）：在配置页勾选并选择格式（Markdown / HTML）与「可编辑白板的角色」。启用后聊天页右上角出现「白板」按钮，可随时调出一个覆盖对话的图层（不中断对话进行）；白板支持「渲染」与「源码」两种视图，人类只读，有权限的角色会在自己回合增量编辑白板内容
- 聊天页：类微信消息流；有总时长限制时状态栏实时显示倒计时；支持人类随时发言（进行中预约下一轮、暂停时直接插话）与随时暂停对话
- **对话「完成」有且仅有一个触发条件：人类手动点「总结并完成」。** 无论是达到 token/时长上限、还是投票决定结束，都只是暂停——你随时可以：延长 token/时长上限、继续对话（仅针对投票/手动暂停，上限已到需先延长）、发起投票、人类发言，或最终手动总结完成
- 发言调度：轮流发言（人数为 2 时强制轮流），或基于「意愿分 + 指数衰减热度 + softmax 温度采样」的按意愿调度；按意愿调度时每次发言后会展示各角色意愿分
- 手动「总结并完成」后，由独立的总结 agent 读取日志生成总结并展示在对话末尾
- 配置与对话持久化到本地 SQLite
- DSH 工具日志：每个角色每轮发言前显示折叠的「工具调用」，展开查看工具、参数、返回结果、时间和成功／失败状态；运行中自动更新，暂停或刷新后仍可查看。

## 快速开始

1. 安装依赖（Python 3.10+）：

   ```powershell
   pip install -r requirements.txt
   ```

2. 配置 LLM。复制 `.env.example` 为 `.env`，填入（已有 `.env` 时按示例补充，保留现有配置）：

   ```env
   LLM_BASE_URL=https://api.openai.com/v1
   LLM_API_KEY=sk-your-key-here
   LLM_MODEL=gpt-4o-mini
   ```

   - `LLM_BASE_URL`：任意 OpenAI 兼容接口地址（如 DeepSeek、Ollama、本地 vLLM 等）。
   - `LLM_API_KEY`：接口密钥。
   - `LLM_MODEL`：模型名称。

   如需在没有真实 LLM 的情况下先体验界面，设置：

   ```env
   LLM_MOCK=true
   ```

   此分支新建讨论默认使用 DeepSeek Harness。请继续完成下面的 SDK 配置；如果暂时使用原来的单次模型调用，在 `.env` 设置 `AGENT_BACKEND=direct`。`LLM_MOCK=true` 始终完全离线，不启动 dsh。

3. 启动服务：

   ```powershell
   python run.py
   ```

4. 打开浏览器访问 <http://127.0.0.1:5000>。

## DeepSeek Harness 讨论角色

旧群聊的讨论角色通过官方 Python SDK 调用 dsh；其发言意愿评分、投票、配置助手和最终总结通过 `LLM_*` 配置直连 DeepSeek。团队模式的 Agent 与辅助调用均使用 DSH，详见下方「图形化多 Agent 团队」。示例配置统一使用 DeepSeek API。

### 安装与配置

推荐在项目虚拟环境中安装并启动：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dsh.txt
.\.venv\Scripts\python.exe run.py
```

运行前在 `.env` 补充：

```env
AGENT_BACKEND=dsh
DEEPSEEK_API_KEY=你的DeepSeek密钥
DEEPSEEK_BASE_URL=https://api.deepseek.com
DSH_MODEL=deepseek-v4-flash
```

`DEEPSEEK_API_KEY` 与 `LLM_API_KEY` 独立。不会从个人 dsh 的凭据文件自动复制密钥。模型请填写你的 API 账户实际可用、并支持工具调用的型号。

已固定 `deepseek-harness-sdk==0.1.5rc1`，其安装会带上同版本原生运行时。默认使用配套运行时，无需系统 Node.js。要复用本机已安装的 dsh，可在 `.env` 指定其可执行入口的**绝对路径**，例如 Windows npm 安装的 `C:/Users/你的用户名/AppData/Roaming/npm/dsh.cmd`：

```env
DSH_BIN=C:/完整路径/dsh.cmd
DSH_BASE_URL=https://api.deepseek.com/anthropic
```

`DSH_BIN` 和 `DSH_BASE_URL` 同时留空即使用 SDK 配套运行时及 `DEEPSEEK_BASE_URL`。兼容验证覆盖配套 `0.1.5rc1` 与本机 npm `0.1.7-rc.2`；dsh 仍处于预览阶段，升级后应运行集成测试。较新的 dsh 使用 DeepSeek Messages 协议；自建代理需支持所选 dsh 版本的协议，不能仅因为支持 Chat Completions 就假定兼容。

**两个协议使用不同地址。** 本机 0.1.7 DSH 需要 `DSH_BASE_URL=https://api.deepseek.com/anthropic`，实际请求 `/anthropic/v1/messages`；旧群聊的辅助调用使用 `LLM_BASE_URL=https://api.deepseek.com`。如果把普通根地址直接给新版 DSH，会请求不存在的 `/v1/messages` 并返回 404。`DSH_BASE_URL` 优先于 `DEEPSEEK_BASE_URL`，只覆盖 DSH 子进程，不影响旧群聊的直连辅助调用。完整示例见 [.env.dsh.example](.env.dsh.example)。端点依据：[DeepSeek 官方协议地址](https://api-docs.deepseek.com/quick_start/pricing/)。

### 回合与恢复行为

- KDS 决定谁发言；该角色可以在本轮内部查询网页、读取文件、执行计算，最终交付 `speech`、`propose_end`、`whiteboard.ops` JSON。
- 角色收到自己的设定、按原可见性规则开放的其他角色设定，以及共享聊天记录。白板编辑权限和结束投票仍由 KDS 校验、应用。
- 每个「讨论 × 角色」有独立的 dsh home、工作目录和活动会话，数据保存在 `data/dsh/`，不会修改个人 `~/.dsh` 的 profile。运行期间同一角色复用进程和会话，只增量添加新群聊消息。
- 暂停、出错、达到上限后会关闭运行时、释放进程。当前发布版 SDK 无法在新进程里直接重开原 session ID，所以恢复时生成新 ID，补回完整公开聊天和该角色最近的工具资料摘录（最多 8 条，每条 2,000 字符）。这不是完整私有上下文的无损恢复；原始 dsh 日志与工作目录文件仍保留。
- 中断或进程意外退出留下的未完成回合不会作为发言发布。继续时重新组织当前角色的回合，不回放已记录的工具调用；模型重新规划时仍可能再次调用工具，已经完成的文件操作不会回滚。
- `data/` 已被 Git 忽略；dsh 原始事件和角色私有工具过程不会作为其他角色的群聊消息。独立工作目录不等于操作系统级读取隔离，工具实际访问仍受 dsh 的权限实现约束。
- 默认工具列表为 `web_search,web_fetch,read,glob,grep,write,edit,pwsh,bash`，按平台和运行时实际注册结果开放。默认不提供子代理、自动目标循环或交互式提问工具，群聊流程由 KDS 控制。额外工具的服务凭据需按 dsh 自身要求配置。
- 保留 dsh 的 `workspace-write` 权限模式及审批策略。需要批准而 SDK 无可用审批界面的操作会失败，不会自动放开权限。

### 限制与消耗

| 配置 | 默认值 | 含义 |
| --- | --- | --- |
| `DSH_REQUEST_MAX_TOKENS` | 8192 | 单次讨论模型请求输出上限 |
| `DSH_TURN_MAX_TOKENS` | 24000 | 一个工具回合的输出预算 |
| `DSH_MAX_STEPS` | 8 | 本轮最多模型步骤，包含格式修正请求 |
| `DSH_MAX_TOOL_CALLS` | 12 | 本轮最多工具执行次数 |
| `DSH_TURN_TIMEOUT` | 180 | 本轮最长秒数，包含启动时间 |
| `DSH_REASONING_EFFORT` | 空 | 可选的模型推理档位；空值沿用模型默认 |
| `DSH_TOOLS` | 见上文 | 允许使用的工具名，以逗号分隔 |
| `DSH_JSON_BASE_URL` | `DEEPSEEK_BASE_URL` | 格式修正使用的 Chat Completions 地址，不能带 `/anthropic` |
| `DSH_JSON_REPAIR_ATTEMPTS` | 2 | 格式失败后最多修正次数；可设 0 关闭，最大为 2 |
| `DSH_JSON_REPAIR_MAX_TOKENS` | 8192 | 每次格式修正的输出上限，还受请求上限与剩余预算约束 |

配置页的单次发言 token 数仍用于指导 `speech` 的长度，与工具循环预算独立。群聊总输出累计 SDK 已上报的每步输出（包括已上报的失败尝试和压缩总结）及 JSON 修正请求的输出，不再只统计最终发言。输入统计包含缓存输入，推理 token 不重复计数。服务商未上报的消耗（例如请求取消前没有返回用量）无法精确补算；上下文压缩由 dsh 独立执行，可能使总预算在结算时超过阈值。预算不是精确的人民币费用限制。

暂停按钮会取消活动 Agent，允许正在执行的工具收尾；如果运行时未响应，3 秒后进入进程关闭兜底。总时长到达也会中断活动回合。回合自身的步骤、工具次数、时间或输出预算到达会暂停并提示，可以调整 `.env`、重启服务后继续。总输出/总时长到达则按原流程先延长上限。只有人类点击「总结并完成」才会完成讨论。

### JSON 格式修正

最终回复先在本地解析、校验。允许外层 Markdown 代码围栏，字符串里的代码块、括号和转义字符会完整保留；重复字段、多份 JSON 混合或不合法的白板操作会判为格式错误。

只有非空、且服务商未报告截断的回复发生格式错误时，才用同一 `DEEPSEEK_API_KEY` 与 `DSH_MODEL` 额外请求 DeepSeek Chat Completions，启用 `response_format={"type":"json_object"}`，关闭推理和工具。此请求只接收原始最终回复、校验错误及上次修正结果，不重跑 DSH 的调查或工具。页面显示「修正输出格式」。有效回复不会增加这次 API 调用。

已通过校验的原发言、白板操作由本地代码保留；已有但畸形的白板结构可交给修正模型处理。无法解析原 JSON 或原回复未包含白板时，修正结果不能新增白板操作；结束提议仅保留原 JSON 明确给出的 `true`。引擎仍检查角色的白板编辑和结束提议权限。

JSON mode 约束语法，修正结果仍须通过本地字段校验。修正最多两次，共享本轮剩余步骤、时间、token 及讨论总预算；成功立即结束，失败尝试的已报告用量也会累计。请求失败、预算耗尽、空原回复、已知截断或连续校验失败仍会暂停，未通过校验的发言和白板操作不会发布。设置 `DSH_JSON_REPAIR_ATTEMPTS=0` 可关闭修正；不添加新环境变量时默认开启。修改代码或配置后需重启服务。

接口依据：[DeepSeek JSON Output](https://api-docs.deepseek.com/guides/json_mode/)。

### 在聊天页查看工具日志

工具日志按角色和回合放在对应发言正文前，默认折叠为「工具调用 · N 次」。展开该回合后，可以继续展开每条调用，查看参数和结果；同一调用由「执行中」更新为「成功」或「失败」，未收到结果就结束的调用标为「未完成」。正在进行、尚未发言的回合会先显示该角色的工具记录，发言完成后保留在正文前，不重复展示。没有工具调用的发言不显示折叠条。

每秒随聊天状态更新，保留已展开的回合、调用和阅读位置。全部折叠时只下载调用摘要；展开后才下载参数和结果正文。同一角色多次发言、人类插话或暂停重试时，日志仍按角色与回合关联。

每个对话保存最近 200 条调用，参数最多 2,000 字符、结果最多 6,000 字符；超出部分明确标注截取或省略。日志随对话保存到 SQLite，刷新页面、暂停和服务重启后仍可查看。此功能从更新后的新调用开始记录，旧版本产生的磁盘原始日志不会自动导入聊天页。

这些记录只供人类查看，不会加入群聊发言或其他角色的模型上下文，也不展示模型思考内容。工具内容以纯文本显示，配置中的 API 密钥及常见凭据字段会做遮盖；这不保证识别工具返回的任意敏感内容。完整 DSH 原始记录仍由运行时保存在 `data/dsh/`。

### 验证

```powershell
.\.venv\Scripts\python.exe -m pip install pytest
.\.venv\Scripts\python.exe -m pytest -q
node tests/test_frontend_timers.js
node tests/test_dsh_bridge.mjs
```

真实 SDK 集成测试连接本地模拟 API，不使用真实密钥、不消耗 DeepSeek 额度，验证文件工具执行、双花括号提示词、格式修正、用量、预算、取消和跨进程恢复：

```powershell
$env:KDS_TEST_DSH_RUNTIME = "1"
.\.venv\Scripts\python.exe -m pytest tests/test_harness_integration.py -q
# 可选：测试本机已有 dsh
$env:KDS_TEST_DSH_BIN = "C:/完整路径/dsh.cmd"
.\.venv\Scripts\python.exe -m pytest tests/test_harness_integration.py -q
```

填好 `.env` 后，也可显式启用**会消耗 DeepSeek 额度**的真实 API 测试。它使用临时工作目录，验证两个角色分别读取真实文件、发言、更新白板，以及辅助 JSON 调用和故意注入格式错误后的 JSON mode 修正；不修改已有讨论：

```powershell
$env:KDS_TEST_DEEPSEEK_LIVE = "1"
.\.venv\Scripts\python.exe -m pytest tests/test_harness_live.py -q -s
Remove-Item Env:KDS_TEST_DEEPSEEK_LIVE
```

官方接口参考：[Python SDK](https://github.com/deepseek-ai/deepseek-harness/tree/master/python/sdk)、[SDK 服务边界](https://github.com/deepseek-ai/deepseek-harness/tree/master/packages/sdk/server)。

## 目录结构

```text
KDS/
├─ design.md            # 需求说明
├─ run.py               # 启动入口
├─ app/
│  ├─ config.py         # 环境变量与路径
│  ├─ db.py             # SQLite 持久化
│  ├─ llm.py            # OpenAI 兼容客户端（含 mock）
│  ├─ harness.py        # 官方 dsh Python SDK、角色会话和恢复检查点
│  ├─ json_repair.py    # 可取消的 DeepSeek JSON mode 格式修正请求
│  ├─ tool_logs.py      # 工具事件归一化、展示摘要及凭据遮盖
│  ├─ dsh/bridge.mjs    # dsh 提示词、工具限制、回合预算与取消
│  ├─ scheduler.py      # 发言调度算法
│  ├─ engine.py         # 对话引擎
│  ├─ assistant.py      # 配置助手
│  ├─ routes/api.py     # REST API
│  ├─ templates/        # 页面
│  └─ static/           # CSS / JS
└─ tests/               # 单元测试
```

## LangGraph 编排与历史兼容

新建对话默认通过 LangGraph 的有限回合图推进，意愿评分、投票和人工总结有独立工作流。角色发言仍然顺序执行，DSH 内部工具循环、权限与格式修正规则沿用原实现。只有人工「总结并完成」会把对话标为完成。

`requirements.txt` 固定 `langgraph==1.2.12` 与 `langgraph-checkpoint-sqlite==3.1.1`，两者声明支持 Python 3.10+。本次本地执行验证使用 Python 3.13；没有实测所有 Python 次版本。LangSmith 服务与账户不是运行前提。

```env
ORCHESTRATION_BACKEND=langgraph
GRAPH_MAX_CONCURRENCY=1
AUXILIARY_MAX_CONCURRENCY=4
```

`GRAPH_MAX_CONCURRENCY` 控制一个评分或投票批次同时执行的角色数，可改为 2 或 3；`AUXILIARY_MAX_CONCURRENCY` 控制所有讨论共享的辅助调用上限。默认批次并发为 1。评分和投票固定输入历史，按原角色顺序输出结果；单个角色失败不会把缺失票据当成同意。

业务记录仍保存在 `data/kds.db`，检查点另存在 `data/langgraph_checkpoints.db`（可通过 `LANGGRAPH_CHECKPOINT_PATH` 指定）。操作、尝试和用量事件用于结果复用、消息与白板的原子提交及消耗去重。关键写入失败会暂停推进并显示错误，恢复时优先提交已保存的有效结果。两份数据库没有跨库原子事务，检查点不能保证外部工具副作用恰好执行一次。

启动时先暂停遗留的运行状态，普通投票与结束投票标记中断，确定的总结结果离线对账；不会自动请求模型。运行中人工预约保存为一个槽位，新预约覆盖旧预约；暂停不会丢弃预约。有兼容检查点且用户明确继续时，不确定的 DSH 回合会通过新操作和新会话重新组织原角色，可能再次执行工具；缺失检查点、图版本不兼容或关键用量保存失败时先对账，也可人工插话开始新回合。

已有记录缺少 `orchestration_backend` 时使用 legacy；缺少 `config.agent_backend` 时使用 direct，这两个默认互相独立。设置 `ORCHESTRATION_BACKEND=legacy` 只影响新建对话。可通过 `POST /api/conversations/<id>/orchestration` 和 `{"orchestration_backend":"langgraph"}` 或 `legacy` 显式切换已暂停的对话；须等工作线程、投票和总结退出，且没有未对账操作。完整消息、白板、调度游标、用量及预约一起迁移，不能直接回退到不支持这些字段的旧版本。

迁移或回退前先暂停所有讨论、等后台操作结束并关闭服务，再备份整个 `data/`，同时包含业务库、检查点库及 DSH 文件。仅复制运行中的一个 SQLite 文件无法形成一致备份。此次代码重构的测试使用临时库，不批量迁移已有真实讨论。

设计与验证细节见 [langgraph_plan.md](docs/langgraph_plan.md)。

## 图形化多 Agent 团队

首页新增「图形化 Agent 团队」入口，也可访问 `http://127.0.0.1:5000/teams`。
在角色库保存版本，把角色放到画板，分别绘制父子任务有向边和群聊双向边。
同一模板可以建立多个独立实例；单节点团队也能启动。运行冻结配置版本，后续编辑下次生效。

角色库提供 9 个通用角色预设，团队选择提供 CAMEL、AutoGen、MetaGPT 与多 Agent 辩论启发的 4 个配置。
预设展示论文链接并保存来源，添加后可按普通版本编辑；具体职责、拓扑及改编差异见 [team-presets.md](docs/team-presets.md)。
工具权限从服务器启用的有限目录勾选，支持全选；角色及运行单次输出留空表示无限，仍受总额度、时长和父任务显式限制约束。
一对节点最多选择一种连线。编辑和观察界面的左右侧栏均可拖动分隔条调整宽度，并分别记住设置。

团队模式通过独立 LangGraph 激活、持久化邮箱和 DSH 编排工具执行委派、临时角色创建、等待、结果回传和有限讨论。
配置建议、评分、投票、总结及格式修正也走 DSH；`LLM_MOCK=true` 明确启用离线模拟，不请求真实模型。
向单个 Agent 发送消息只补充信息，不派发任务、不解除等待；只有人工「总结并完成」才完成整场运行。

默认使用已有 `DSH_*` 配置，角色的 `model_config_id=default` 指向该配置。
`TEAM_DSH_MAX_CONCURRENCY`（默认 4）限制整个应用的团队 DSH 调用；每次运行另有限制执行并发、进程、层级、实例、任务与讨论回合。
可在服务器的 `TEAM_MODEL_CONFIGS` JSON 环境变量中注册其他 DSH 配置引用；角色定义和浏览器不会收到 API 密钥。
应用使用数据库旁的进程锁，团队调度只支持单个应用进程，使用多个 Flask worker 会被拒绝。

首次升级已有业务库前自动建立 `data/kds.before-team-v1.db` 一致备份，迁移保持幂等。
检查点继续使用既有独立数据库。启动后遗留团队运行只暂停、对账，不自动调用模型。
出现不确定外部调用时需核对副作用并明确重试；暂停和停机不计入运行时长，重启时间恢复精度为最近持久进度（至多约一秒残余）。

使用说明见 [teams-guide.md](docs/teams-guide.md)，设计核对与实际验证见 [graphical-multi-agent-validation.md](docs/graphical-multi-agent-validation.md)。

启动方式仍为 `.\.venv\Scripts\python.exe run.py`。修改代码或切换分支后需要先结束旧 KDS 服务再启动；默认关闭自动热更新。
若 `/teams` 返回 404，确认地址端口与启动输出一致、当前目录和分支正确，并确认旧服务已退出。

离线端到端浏览器预览使用临时数据库，不改变已有讨论：

```powershell
.\.venv\Scripts\python.exe tests/team_preview_server.py
# 另一个终端；需要可用的 Playwright 和 Chromium
$env:KDS_TEAM_UI_URL = 'http://127.0.0.1:5017'
node tests/test_team_service_ui.cjs
```

团队容量样本：

```powershell
.\.venv\Scripts\python.exe scripts/benchmark_teams.py --instances 8 --concurrency 1 3
```

## 运行测试

```powershell
python -m pytest -q
```

未安装 pytest 时，也可以直接运行测试脚本或使用应用自带的离线 mock 模式手动验证。
