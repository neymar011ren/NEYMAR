# Forge Agent

本地可配置 Agent：底层直接调用你提供的模型 API，并提供可视化配置界面，随时修改 Base URL、API Key、模型 ID、协议等。

## 功能

- 可视化配置页（侧滑面板）：API Base URL / Key / 模型 / 协议 / 温度 / Token / 系统提示词 / 工具开关 / **Mem0 记忆**
- 配置持久化到 `data/config.json`，保存后立即生效
- 支持三种协议：
  - `chat_completions`（OpenAI Chat Completions）
  - `responses`（OpenAI Responses）
  - `anthropic_messages`（Anthropic Messages）
- **长期记忆**：接入开源 [Mem0](https://github.com/mem0ai/mem0)，本地 Qdrant 落盘；对话前检索、对话后写入
- Agent 对话流式事件：状态、工具调用、记忆召回、最终回复
- 本地工具：列出目录、读文件、写文件（限制在仓库工作区内，且屏蔽 `agent/data`）
- **单向协议适配工具** `protocol_adapt`：把 Responses / Anthropic Messages 请求转换为 Chat Completions，并可调用当前配置的上游；由模型在对话中自主决定是否调用
- 一键「测试连接」/「查看记忆」/「清空记忆」

## 快速开始

```bash
cd agent
python3 -m venv .venv && source .venv/bin/activate   # 若系统无 venv，可直接: pip install -r requirements.txt
pip install -r requirements.txt
python run.py
```

浏览器打开：<http://127.0.0.1:8787>

1. 点击右上角「模型配置」
2. 填入你的 API Base URL、Key、模型 ID，选择协议
3. 点「测试连接」确认可用
4. 「保存配置」后即可对话

## 配置说明

| 字段 | 说明 |
|---|---|
| `api_base_url` | 通常填到 `/v1`；也可填完整路径（如 `.../v1/chat/completions`） |
| `api_key` | 上游密钥；界面留空表示保持原值 |
| `model` | 上游模型 ID |
| `protocol` | `chat_completions` / `responses` / `anthropic_messages` |
| `enable_tools` | 是否允许 Agent 调用本地工具（含文件工具与 `protocol_adapt`） |
| `enable_web_search` | 是否启用联网搜索（请求 `tools` 注入 `{"type":"web_search"}`，见[金山云文档](https://docs.ksyun.com/documents/45179)） |
| `enable_memory` | 是否启用 Mem0 长期记忆 |
| `memory_user_id` | 记忆命名空间（多用户隔离） |
| `memory_embed_base_url` | **向量模型独立 Base URL**（与文本模型分开） |
| `memory_embed_api_key` | **向量模型独立 API Key** |
| `memory_embed_model` | 向量模型 ID |
| `memory_llm_model` | 记忆抽取模型，空则复用主模型（仍用文本模型地址/密钥） |
| `memory_top_k` | 每轮检索注入的记忆条数 |

示例文件：`data/config.example.json`。首次启动会自动复制为 `data/config.json`。

记忆数据目录：`data/memory/`（本地 Qdrant + history.db，已随 `data/` 敏感路径屏蔽，工具无法读取）。

## 单向协议适配器

工具名：`protocol_adapt`（需开启「启用本地工具」）。

| 方向 | 说明 |
|---|---|
| `responses` → `chat_completions` | 转换 `instructions` / `input` / `max_output_tokens` / function tools |
| `anthropic_messages` → `chat_completions` | 转换 `system` / `messages`（含 tool_use / tool_result）/ `input_schema` tools |

- **单向**：不支持 Chat Completions 转回另外两种协议
- `execute=true`：转换后用当前配置的 Base URL / Key 调用 `/v1/chat/completions`
- `execute=false`：只返回转换后的请求体，便于检查

模型会根据工具描述自主决定是否调用；也可在系统提示词中进一步约束调用规则。

> 文本模型与向量模型的地址/密钥完全独立配置。Mem0 抽取走文本模型兼容 Chat；向量化走你单独填写的 Embeddings 端点。

## 安全提示

- `data/config.json` 含明文 API Key，请勿提交到公开仓库
- 工具只能访问当前仓库工作区，不执行任意 Shell
- 建议仅本机监听（默认 `127.0.0.1`）

## 目录结构

```text
agent/
  main.py           # FastAPI 入口
  llm.py              # 多协议调用 + Agent 循环
  protocol_adapter.py # 单向协议适配（→ Chat Completions）
  memory_service.py   # Mem0 长期记忆封装
  config_store.py     # 配置读写
  tools.py            # 本地工具（含 protocol_adapt）
  static/           # 可视化前端
  data/             # 配置与记忆落盘
  run.py            # 启动脚本
```
