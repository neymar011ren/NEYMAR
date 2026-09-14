# KingSwitch

**KingSwitch** 是模型渠道切换助手：根据用户一句话，判断要把模型 API 配到哪个目标 Agent，并完成安装（如需要）→ 协议调研 → 协议转换 → 本地配置探测 → 索取凭证 → 写入渠道 → 测试验证。

底层仍直接调用你提供的模型 API；联网搜索、协议转换等能力保留。

## 流水线

1. **意图识别** `detect_target_agent` / `list_known_agents`
2. **协议调研**（档案内 `native_protocol` + 可选联网搜索）
3. **安装检测** `check_agent_installed` → 未安装则联网搜索并 `prepare_agent_install`（前端弹出安装按钮）
4. **协议转换** `protocol_adapt`（Responses / Anthropic → Chat Completions）
5. **本地探测** `probe_agent_config`
6. **向用户索取** Base URL、模型名、API Key（密钥由用户自行输入）
7. **写入配置** `write_agent_channel_config` 只生成预览（目标路径/是否新建/脱敏 Key），前端弹出「确认写入」卡片；真正落盘由用户点击确认后调用 `POST /api/agents/write-config` 完成（写入前 backup），模型本身无法跳过确认直接落盘
8. **测试验证** `test_agent_channel`（含协议转换步骤 + HTTP 探测）

## 支持的目标 Agent

| id | 名称 | 原生协议（档案默认） | 典型配置路径 |
|---|---|---|---|
| `claude_code` | Claude Code | anthropic_messages | `~/.claude/settings.json` |
| `codex` | OpenAI Codex CLI | responses | `~/.codex/config.toml` |
| `continue` | Continue | chat_completions | `~/.continue/config.yaml` |
| `cline` | Cline | chat_completions | `~/.cline/data/settings/...` |
| `workbuddy` | WorkBuddy | chat_completions | `~/.workbuddy/models.json` |
| `aider` | Aider | chat_completions | `~/.aider.conf.yml` |
| `opencode` | OpenCode | chat_completions | `~/.config/opencode/opencode.json` |
| `kingswitch` | KingSwitch（本助手上游） | chat_completions | `agent/data/config.json` |

## 功能

- 可视化配置页：KingSwitch 自身上游的 Base URL / Key / 模型 / 协议 / 工具 / **联网搜索** / Mem0
- KingSwitch 专用工具 + 原有文件工具 / `protocol_adapt`
- 联网搜索：`enable_web_search` 时在 Chat Completions 请求注入 `{"type":"web_search"}`
- 长期记忆（可选）：**本地 JSONL 优先**（不再因 Embed/Qdrant 锁导致失败）；Mem0 可用时自动叠加
- **本机 Agent 自动巡检**（`enable_auto_scan`，默认开启）：打开页面即自动跑一遍
  `GET /api/scan`，遍历 8 个已知档案，对每个档案：
  - 安装检测（`check_agent_installed`）
  - 尝试从本地已有配置文件里提取渠道凭证（只认识 KingSwitch 自己写过的文件形状）
  - 若提取成功：发起真实连通性探测 + **真实 function calling 探测**（往上游发一个带工具定义的请求，检查是否真的返回 `tool_calls`，而不只是能不能对话）
  - 若原生协议不是 Chat Completions：额外做一次**协议转换保真度**离线检查（不发网络请求，用一份带 system/多轮/工具调用的合成请求验证转换器不丢字段）
  结果汇总成表格展示在聊天区，同时生成一份 Markdown 报告存到 `data/reports/`，可通过
  `GET /api/scan/report/{filename}` 下载。这条链路完全不经过 LLM，是确定性代码逻辑，
  不依赖模型"愿不愿意"挨个调用工具。

## 快速开始

```bash
cd agent
pip install -r requirements.txt
python run.py
```

## 测试

`agent_config_tools.py` 会直接读写用户真实的第三方 Agent 配置文件（`~/.claude/settings.json`
等），改动这块代码风险较高，务必先跑一遍测试再改动写入器相关逻辑：

```bash
cd agent
pip install -r requirements-dev.txt
pytest
```

测试通过 `conftest.py` 里的 `tmp_home` / `isolated_config_store` / `isolated_memory_store`
fixture 把 `$HOME` 和配置文件路径重定向到临时目录，不会碰到本机真实的配置文件。

浏览器打开：<http://127.0.0.1:8787>

1. 在「模型配置」里填好 **KingSwitch 自己的**上游 API（用于推理）
2. 建议开启「启用本地工具」与「启用联网搜索」
3. 对话示例：`我想把金山云模型配到 Claude Code`

## 配置说明

| 字段 | 说明 |
|---|---|
| `api_base_url` | KingSwitch 上游 Base URL（通常到 `/v1`） |
| `api_key` | KingSwitch 上游密钥；界面留空表示保持原值 |
| `model` | KingSwitch 上游模型 ID |
| `protocol` | `chat_completions` / `responses` / `anthropic_messages` |
| `enable_tools` | 启用 KingSwitch 工具集 |
| `enable_web_search` | 启用联网搜索（见[金山云文档](https://docs.ksyun.com/documents/45179)） |
| `enable_auto_scan` | 打开页面时自动巡检本机 Agent（会对已发现凭证的渠道发起真实请求，见下方安全提示） |
| `system_prompt` | 默认已切换为 KingSwitch 流水线提示词 |

## 单向协议适配器

| 方向 | 说明 |
|---|---|
| `responses` → `chat_completions` | 转换 `instructions` / `input` / … |
| `anthropic_messages` → `chat_completions` | 转换 `system` / `messages` / … |

`test_agent_channel` 会在目标原生协议非 Chat Completions 时自动走转换再探测。

## 安全提示

- `data/config.json` 与目标 Agent 配置可能含明文 Key，勿提交公开仓库
- 工作区文件工具仍限制在仓库内；**渠道写入工具**仅允许档案白名单路径
- **写入渠道配置需要用户在前端手动点击「确认写入」**：`write_agent_channel_config` 这个 LLM 工具只做校验和生成预览，真正落盘只能通过 `/api/agents/write-config` 接口触发，模型自身无法绕过确认直接改动本机其它 Agent 的真实配置文件
- `probe_agent_config` 对 JSON / YAML / TOML 等各种格式的已有配置都会脱敏后再展示给模型，避免已保存的旧密钥被完整回显
- `test_agent_channel` 仍然会用当轮参数里的 `base_url`/`api_key` 发起一次真实探测请求，且不做内网地址限制（因为很多用户的合法场景就是探测本机/内网自建网关）；如果担心模型被提示词注入误导去测试一个不受信任的地址，建议在拿到密钥后先人工确认 Base URL 再让流程继续
- **`enable_auto_scan` 默认开启，会在每次打开页面时自动对本机已发现凭证的渠道发起真实网络请求**（连通性 + 工具调用探测）。这不经过 LLM、纯代码触发，但如果你不希望每次开页面都自动打一遍所有已配置渠道，可以在设置里关掉这个开关。巡检报告只包含脱敏后的 Key（`data/reports/` 已加入 `.gitignore`）
- 聊天内容里出现的疑似密钥（`Bearer xxx`、`api_key=xxx`、`sk-xxx` 等模式）在写入长期记忆前后都会做脱敏扫描，但**这只覆盖 KingSwitch 自己的记忆存储**——如果你把真实 Key 贴进了聊天框，视为该 Key 已经暴露给了上游模型服务，请评估是否需要轮换
- 建议仅本机监听（默认 `127.0.0.1`）
- 不要把完整 API Key 贴进多轮总结

## 目录结构

```text
agent/
  main.py               # FastAPI 入口
  llm.py                # 多协议调用 + Agent 循环
  judger_prompt.py      # KingSwitch 系统提示
  agent_profiles.py     # 目标 Agent 档案
  agent_config_tools.py # 探测 / 写入 / 测试
  agent_install.py      # 安装检测与一键安装
  protocol_adapter.py   # 单向协议适配 + 保真度离线检查
  scan_service.py       # 本机 Agent 自动巡检 + 报告生成
  memory_service.py
  config_store.py
  tools.py
  static/
  data/
  tests/                # pytest 用例
  conftest.py           # 测试用的隔离 fixture
  run.py
```
