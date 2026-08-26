# KDS 侃大山

让多个 LLM 根据预设背景和各自的 system prompt 进行无限制的多人聊天。前端为浅色简洁界面，聊天页类似微信，按时间纵向罗列消息。

## 功能

- 开始页：进入配置页、查看历史对话
- 配置页：配置人数、每个角色的 system prompt（可设置对谁可见）、共享背景、单次发言 token 上限、总输出 token 上限、总时长、首个发言人、发言调度模式（轮流 / 按意愿）
- 配置助手：在配置页右侧与助手多轮对话，自动生成或修改 system prompt 与共享背景，应用前需人工确认
- 聊天页：类微信消息流，支持人类预约下一轮发言（跳过调度）与随时暂停对话
- 手动暂停后可选择「继续」继续群聊，或「总结」让独立的总结 agent 读取日志生成总结
- 发言调度：轮流发言（人数为 2 时强制轮流），或基于「意愿分 + 指数衰减热度 + softmax 温度采样」的按意愿调度；按意愿调度时每次发言后会展示各角色意愿分
- 对话结束（达 token/时长上限、或手动总结）后，由独立的总结 agent 读取日志生成总结
- 配置与对话持久化到本地 SQLite

## 快速开始

1. 安装依赖（Python 3.10+）：

   ```powershell
   pip install -r requirements.txt
   ```

2. 配置 LLM。复制 `.env.example` 为 `.env`，填入：

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

3. 启动服务：

   ```powershell
   python run.py
   ```

4. 打开浏览器访问 <http://127.0.0.1:5000>。

## 目录结构

```text
KDS/
├─ design.md            # 需求说明
├─ run.py               # 启动入口
├─ app/
│  ├─ config.py         # 环境变量与路径
│  ├─ db.py             # SQLite 持久化
│  ├─ llm.py            # OpenAI 兼容客户端（含 mock）
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
