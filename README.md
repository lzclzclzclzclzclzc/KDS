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

所有讨论角色通过官方 Python SDK 调用 dsh；发言意愿评分、投票、配置助手和最终总结仍使用 `LLM_*` 配置，可继续用本地 Qwen，也可以另外切到 DeepSeek。

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

**两个协议使用不同地址。** 本机 0.1.7 DSH 需要 `DSH_BASE_URL=https://api.deepseek.com/anthropic`，实际请求 `/anthropic/v1/messages`；辅助调用使用 `LLM_BASE_URL=https://api.deepseek.com`。如果把普通根地址直接给新版 DSH，会请求不存在的 `/v1/messages` 并返回 404。`DSH_BASE_URL` 优先于 `DEEPSEEK_BASE_URL`，只覆盖 DSH 子进程，不影响辅助调用。完整示例见 [.env.dsh.example](.env.dsh.example)。端点依据：[DeepSeek 官方协议地址](https://api-docs.deepseek.com/quick_start/pricing/)。

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
| `DSH_MAX_STEPS` | 8 | 本轮最多模型步骤 |
| `DSH_MAX_TOOL_CALLS` | 12 | 本轮最多工具执行次数 |
| `DSH_TURN_TIMEOUT` | 180 | 本轮最长秒数，包含启动时间 |
| `DSH_REASONING_EFFORT` | 空 | 可选的模型推理档位；空值沿用模型默认 |
| `DSH_TOOLS` | 见上文 | 允许使用的工具名，以逗号分隔 |

配置页的单次发言 token 数仍用于指导 `speech` 的长度，与工具循环预算独立。群聊总输出累计 SDK 已上报的每步输出（包括已上报的失败尝试和压缩总结），不再只统计最终发言。输入统计包含缓存输入，推理 token 不重复计数。服务商未上报的消耗无法精确补算；上下文压缩由 dsh 独立执行，可能使总预算在结算时超过阈值。预算不是精确的人民币费用限制。

暂停按钮会取消活动 Agent，允许正在执行的工具收尾；如果运行时未响应，3 秒后进入进程关闭兜底。总时长到达也会中断活动回合。回合自身的步骤、工具次数、时间或输出预算到达会暂停并提示，可以调整 `.env`、重启服务后继续。总输出/总时长到达则按原流程先延长上限。只有人类点击「总结并完成」才会完成讨论。

### 验证

```powershell
.\.venv\Scripts\python.exe -m pip install pytest
.\.venv\Scripts\python.exe -m pytest -q
node tests/test_frontend_timers.js
node tests/test_dsh_bridge.mjs
```

真实 SDK 集成测试连接本地模拟 API，不使用真实密钥、不消耗 DeepSeek 额度，验证文件工具执行、双花括号提示词、用量、预算、取消和跨进程恢复：

```powershell
$env:KDS_TEST_DSH_RUNTIME = "1"
.\.venv\Scripts\python.exe -m pytest tests/test_harness_integration.py -q
# 可选：测试本机已有 dsh
$env:KDS_TEST_DSH_BIN = "C:/完整路径/dsh.cmd"
.\.venv\Scripts\python.exe -m pytest tests/test_harness_integration.py -q
```

填好 `.env` 后，也可显式启用**会消耗 DeepSeek 额度**的真实 API 测试。它使用临时工作目录，验证两个角色分别读取真实文件、发言、更新白板，以及辅助 JSON 调用；不修改已有讨论：

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
│  ├─ dsh/bridge.mjs    # dsh 提示词、工具限制、回合预算与取消
│  ├─ scheduler.py      # 发言调度算法
│  ├─ engine.py         # 对话引擎
│  ├─ assistant.py      # 配置助手
│  ├─ routes/api.py     # REST API
│  ├─ templates/        # 页面
│  └─ static/           # CSS / JS
└─ tests/               # 单元测试
```

## 运行测试

```powershell
python -m pytest -q
```

未安装 pytest 时，也可以直接运行测试脚本或使用应用自带的离线 mock 模式手动验证。
