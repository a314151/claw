# 个人助手（LLM + MCP，Python）

这是一个从零搭建的、可迭代的个人助手项目骨架，目标是：

- 模块化：配置、LLM、MCP、编排、CLI 分层
- 精简：最少必要依赖与清晰代码结构
- 稳健：参数校验、超时控制、错误隔离
- 易扩展：可替换模型、可增加 MCP 服务、可继续拆分业务模块

当前已内置支持：

- OpenAI
- DeepSeek
- 智谱清言（GLM）
- 通义千问（Qwen）
- Gemini

新增（2026-03）：

- 邮箱验证码认证（Web，区分注册/登录）
- Cloudflare R2 持久化（用户、会话历史、用户画像、MCP 用户配置）
- 用户画像注入（每次请求自动带入系统上下文）
- MCP 可视化配置（在设置弹窗中选择并配置）
- 回答末尾展示本次 Token 用量与人民币费用估算
- 内置文档导出 MCP（CSV/PDF）
- 价格表可在设置页编辑并持久化
- 资料库支持画像/行为习惯/Skills/价格表查看与编辑
- 资料修改自动存档，支持历史回滚
- 导出按钮默认折叠显示（减小占位），支持后端导出与前端本地导出兜底
- 即使模型未返回 usage，也会显示明确提示（Token 未返回）

## 1. 项目结构

```text
.
├─ .env.example
├─ mcp_servers.example.json
├─ pyproject.toml
├─ README.md
└─ src/
   └─ personal_assistant/
      ├─ __init__.py
      ├─ assistant.py
      ├─ cli.py
      ├─ config.py
      ├─ llm_client.py
      └─ mcp_client.py
```

## 2. 环境准备（Windows，使用 uv 统一管理）

首次安装 uv（已完成可跳过）：

```powershell
pip install uv
```

使用 uv 创建并管理虚拟环境 + 安装依赖：

```powershell
uv venv .venv
uv pip install -e .
```

说明：

- 后续统一使用 `uv pip` 管理依赖
- 如需安装新包，使用 `uv pip install <package>`

## 3. 配置

1. 复制 `.env.example` 为 `.env` 并填写：

- `LLM_PROVIDER`：默认提供商（`openai/deepseek/zhipu/qwen/gemini`）
- 至少配置一个 API Key：
  - `OPENAI_API_KEY`
  - `DEEPSEEK_API_KEY`
  - `ZHIPU_API_KEY`
  - `QWEN_API_KEY`
  - `GEMINI_API_KEY`
- 可选覆盖每家模型与地址：`*_MODEL` / `*_BASE_URL`

2. MCP 配置：

- 参考 `mcp_servers.example.json` 新建 `mcp_servers.json`
- 默认会读取 `.env` 中的 `MCP_CONFIG`（默认值 `./mcp_servers.json`）

示例（filesystem + 文档导出 MCP）：

```json
[
  {
    "name": "filesystem",
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-filesystem", "D:/Desktop/1"],
    "env": {}
  },
  {
    "name": "doc_export",
    "command": "uv",
    "args": ["run", "python", "-m", "personal_assistant.mcp_document_server"],
    "env": {}
  }
]
```

## 4. 运行

### 4.1 CLI

单次提问：

```powershell
uv run -m personal_assistant.cli --once "帮我看看当前目录结构"
```

交互模式：

```powershell
uv run -m personal_assistant.cli
```

指定提供商和模型：

```powershell
uv run -m personal_assistant.cli --provider deepseek --model deepseek-chat --once "你好"
```

或安装脚本后：

```powershell
pa --once "你好"
```

### 4.2 本地可视化网页

启动 Web：

```powershell
uv run pa-web --host 127.0.0.1 --port 8090
```

或：

```powershell
uv run python -m personal_assistant.web --port 8090
```

浏览器访问：

```text
http://127.0.0.1:8090
```

提示：请优先使用 `8090` 端口访问新版服务；若同时存在旧服务端口（如 `18090`），可能出现价格/资料库/导出接口 404。

页面支持：

- 提供商下拉固定多选项（OpenAI/DeepSeek/智谱/通义/Gemini）
- 预设模型快速选择 + 自定义模型输入
- 若后端未配置某个 provider 密钥，可直接在网页输入该 provider 的 API Key（仅浏览器本地保存）
- 显示响应耗时
- 本地对话可视化
- 左侧头像入口统一承载注册/登录（首次使用先注册）
- 邮箱验证码认证（必须登录后才可聊天）
- MCP 目录浏览与用户级 MCP 配置保存
- MCP 调用过程可折叠展示，刷新后可从历史恢复
- 每条助手回答末尾显示 Token 与费用估算（人民币）
- 每条助手回答支持折叠式导出（CSV/PDF）
- 若后端导出接口缺失（旧版服务），前端会自动使用本地导出兜底
- 头像菜单支持打开“我的资料库”，可查看和编辑持久化信息

登录与持久化说明：

- 认证方式：邮箱 + 验证码（后端通过 SMTP 发送）
- 首次账号：先走注册流程（注册发码 -> 注册验证）
- 已有账号：走登录流程（登录发码 -> 登录验证）
- 历史与画像：优先写入 Cloudflare R2；若未配置 R2，则降级到本地 `data/` 目录
- 除画像外还持久化：会话历史、MCP 配置、行为习惯统计、Skills 配置、模型价格表、资料修改存档
- 用户画像策略：
  - 单次写入不超过 200 词
  - 总量不超过 5000 词
  - 仅在“用户明确要求”或“每 10 轮对话”时写入
  - 每次 LLM 请求都注入画像文本

Cloudflare R2 推荐配置（`.env`）：

- `CLOUDFLARE_R2_BUCKET`
- `CLOUDFLARE_R2_ENDPOINT`
- `CLOUDFLARE_R2_ACCESS_KEY_ID`
- `CLOUDFLARE_R2_SECRET_ACCESS_KEY`

说明：

- 当前实现优先使用 R2 S3 凭据（SigV4）访问对象存储。
- `CLOUDFLARE_API_TOKEN` 仅用于 Cloudflare Account API 路径，不是 S3 endpoint 对象操作的必需项。

SMTP 必填配置（`.env`）：

- `SMTP_HOST`
- `SMTP_PORT`
- `SMTP_USERNAME`
- `SMTP_PASSWORD`
- `SMTP_FROM`
- `SMTP_USE_TLS`

联调模式（仅本地开发）：

- `SMTP_DEV_ECHO_CODE=true` 时，若 SMTP 未配置，接口会返回 `debug_code` 便于联调。
- 正式环境请保持 `SMTP_DEV_ECHO_CODE=false`，并配置真实 SMTP 后再开启验证码发信。

当前内置预设模型（可自行覆盖）：

- OpenAI: `gpt-5`, `gpt-5-mini`, `gpt-4.1`, `gpt-4o`
- DeepSeek: `deepseek-v3`, `deepseek-r1`, `deepseek-chat`, `deepseek-reasoner`
- 智谱: `glm-4.7`, `glm-4-plus`, `glm-4-air`, `glm-4-flash`
- 通义: `qwen3-max`, `qwen3-plus`, `qwen-max`, `qwen-plus`
- Gemini: `gemini-3-pro`, `gemini-3-flash`, `gemini-2.5-pro`, `gemini-2.5-flash`

## 5. 设计说明

### 5.1 模块职责

- `config.py`
  - 读取多提供商环境变量和 MCP JSON 配置
  - 做边界校验（温度、token、超时、轮次）
- `llm_client.py`
  - 封装多提供商路由（统一 OpenAI 兼容接口）
  - 统一解析文本与 tool calls
- `mcp_client.py`
  - 管理多个 MCP 服务连接
  - 聚合工具列表并做命名空间隔离（`server.tool`）
  - 工具调用统一超时和异常处理
- `assistant.py`
  - 主编排循环：模型决定是否调用工具，工具结果回灌模型
  - 限制最大轮次，避免无限循环
  - 新增 Skill 匹配注入：调用工具前按题目/关键词匹配 skill 指令，减少无效探索
- `cli.py`
  - 命令行入口，支持单次与 REPL 模式
- `web_app.py`
  - FastAPI 本地服务，提供可视化页面与 `/api/chat`
  - 新增 `/api/export/answer`、`/api/pricing`、`/api/user/memory*` 接口

### 5.2 稳健性策略

- 所有关键参数均做校验，避免非法配置直接进入运行期
- MCP 启动失败不会导致整个进程退出（降级可继续对话）
- 工具调用具备超时保护
- 工具调用与模型回复采用受控循环（`ASSISTANT_MAX_TURNS`）

## 6. 常见扩展

1. 增加记忆模块
- 新增 `memory.py`，在 `assistant.py` 中拼接短期上下文

2. 增加策略层
- 新增 `planner.py`，对工具调用顺序和重试策略进行细化

3. 增加安全策略
- 对高风险工具增加白名单和参数过滤
- 对敏感路径、外部命令增加策略检查

## 7. 故障排查

- 报错“请设置 LLM_API_KEY”
  - 新版请检查是否至少设置一个 provider 的 API Key
- MCP 工具不可用
  - 检查 `mcp_servers.json` 的 command/args 是否可执行
  - Windows 下确认 Node.js、npx 可用（如使用 npx 启动 MCP）
- 频繁超时
  - 增大 `LLM_TIMEOUT_S`
  - 检查网络和模型服务可用性
- 资料库/价格/导出接口 404（常见于旧服务端口）
  - 检查端口进程：

```powershell
Get-NetTCPConnection -State Listen -LocalPort 8090,18090 | Select-Object LocalPort,OwningProcess
```

  - 检查进程命令行：

```powershell
Get-CimInstance Win32_Process -Filter "ProcessId=<PID>" | Select-Object ProcessId,Name,CommandLine
```

  - 仅保留一个新版服务实例并使用 8090 访问：

```powershell
uv run pa-web --host 127.0.0.1 --port 8090
```

## 8. 许可建议

你可以按需添加 MIT License，便于后续开源和复用。
