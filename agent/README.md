# Judger Agent（配置判断器）

把「对话 Agent」收敛为**判断器**：根据用户一句话，判断要把模型 API 配到哪个目标 Agent，并完成协议调研 → 协议转换 → 本地配置探测 → 索取凭证 → 写入渠道 → 测试验证。

底层仍直接调用你提供的模型 API；联网搜索、协议转换等能力保留。

## 判断器流水线

1. **意图识别** `detect_target_agent` / `list_known_agents`
2. **协议调研**（档案内 `native_protocol` + 可选联网搜索）
3. **安装检测** `check_agent_installed` → 未安装则联网搜索并 `prepare_agent_install`（前端弹出安装按钮）
4. **协议转换** `protocol_adapt`（Responses / Anthropic → Chat Completions）
5. **本地探测** `probe_agent_config`
6. **向用户索取** Base URL、模型名、API Key（密钥由用户自行输入）
7. **写入配置** `write_agent_channel_config`（写入前 backup）
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
| `forge_agent` | 本判断器上游 | chat_completions | `agent/data/config.json` |

## 功能

- 可视化配置页：判断器自身上游的 Base URL / Key / 模型 / 协议 / 工具 / **联网搜索** / Mem0
- 判断器专用工具 + 原有文件工具 / `protocol_adapt`
- 联网搜索：`enable_web_search` 时在 Chat Completions 请求注入 `{"type":"web_search"}`
- 长期记忆（可选）：**本地 JSONL 优先**（不再因 Embed/Qdrant 锁导致失败）；Mem0 可用时自动叠加

## 快速开始

```bash
cd agent
pip install -r requirements.txt
python run.py
```

浏览器打开：<http://127.0.0.1:8787>

1. 在「模型配置」里填好**判断器自己的**上游 API（用于推理）
2. 建议开启「启用本地工具」与「启用联网搜索」
3. 对话示例：`我想把金山云模型配到 Claude Code`

## 配置说明

| 字段 | 说明 |
|---|---|
| `api_base_url` | 判断器上游 Base URL（通常到 `/v1`） |
| `api_key` | 判断器上游密钥；界面留空表示保持原值 |
| `model` | 判断器上游模型 ID |
| `protocol` | `chat_completions` / `responses` / `anthropic_messages` |
| `enable_tools` | 启用判断器工具集 |
| `enable_web_search` | 启用联网搜索（见[金山云文档](https://docs.ksyun.com/documents/45179)） |
| `system_prompt` | 默认已切换为判断器流水线提示词 |

## 单向协议适配器

| 方向 | 说明 |
|---|---|
| `responses` → `chat_completions` | 转换 `instructions` / `input` / … |
| `anthropic_messages` → `chat_completions` | 转换 `system` / `messages` / … |

`test_agent_channel` 会在目标原生协议非 Chat Completions 时自动走转换再探测。

## 安全提示

- `data/config.json` 与目标 Agent 配置可能含明文 Key，勿提交公开仓库
- 工作区文件工具仍限制在仓库内；**渠道写入工具**仅允许档案白名单路径
- 建议仅本机监听（默认 `127.0.0.1`）
- 不要把完整 API Key 贴进多轮总结

## 目录结构

```text
agent/
  main.py               # FastAPI 入口
  llm.py                # 多协议调用 + Agent 循环
  judger_prompt.py      # 判断器系统提示
  agent_profiles.py     # 目标 Agent 档案
  agent_config_tools.py # 探测 / 写入 / 测试
  protocol_adapter.py   # 单向协议适配
  memory_service.py
  config_store.py
  tools.py
  static/
  data/
  run.py
```
