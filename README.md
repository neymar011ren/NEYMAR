# Forge Agent

本仓库现包含一个本地可配置 Agent（`agent/`）：

- 底层 API 使用你自己的模型服务
- 提供可视化配置界面，随时修改 Base URL、Key、模型、协议
- 支持 Chat Completions / Responses / Anthropic Messages

详见 [`agent/README.md`](agent/README.md)。

```bash
cd agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python run.py
```

打开 http://127.0.0.1:8787
