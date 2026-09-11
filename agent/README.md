# Forge Agent

本地可配置 Agent：底层直接调用你提供的模型 API，并提供可视化配置界面，随时修改 Base URL、API Key、模型 ID、协议等。

## 功能

- 可视化配置页（侧滑面板）：API Base URL / Key / 模型 / 协议 / 温度 / Token / 系统提示词 / 工具开关
- 配置持久化到 `data/config.json`，保存后立即生效
- 支持三种协议：
  - `chat_completions`（OpenAI Chat Completions）
  - `responses`（OpenAI Responses）
  - `anthropic_messages`（Anthropic Messages）
- Agent 对话流式事件：状态、工具调用、最终回复
- 本地工具：列出目录、读文件、写文件（限制在仓库工作区内）
- 一键「测试连接」

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
| `enable_tools` | 是否允许 Agent 调用本地文件工具 |

示例文件：`data/config.example.json`。首次启动会自动复制为 `data/config.json`。

## 安全提示

- `data/config.json` 含明文 API Key，请勿提交到公开仓库
- 工具只能访问当前仓库工作区，不执行任意 Shell
- 建议仅本机监听（默认 `127.0.0.1`）

## 目录结构

```text
agent/
  main.py           # FastAPI 入口
  llm.py            # 多协议调用 + Agent 循环
  config_store.py   # 配置读写
  tools.py          # 本地工具
  static/           # 可视化前端
  data/             # 配置文件
  run.py            # 启动脚本
```
