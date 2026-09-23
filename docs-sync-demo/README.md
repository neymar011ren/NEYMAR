# docs-sync-demo

一个带 **可视化 Web UI** 的 `llms.txt` 增量同步 Demo：自动拉取 Claude / OpenAI / Gemini 官方文档索引，下载 Markdown 正文，并用 **SHA-256 + ETag/304** 检测更新。

## 它做什么

1. 读取源的 `llms.txt`
2. 按正则过滤出文档 Markdown URL
3. 并发下载页面（带礼貌延迟）
4. 对比本地 `state.json` 里的 content hash
5. 只写入 **新增 / 变更** 页面；索引中消失的页面标记为 **deleted**

第二次运行同一源时，未变更页面会显示为 `unchanged`（若服务端支持还会收到 HTTP 304）。

## 快速开始（Web UI，推荐）

```bash
cd docs-sync-demo
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 打开可视化界面
uvicorn app:app --host 0.0.0.0 --port 8765 --reload
```

浏览器访问 http://127.0.0.1:8765

界面可勾选源、设置页数/并发/Watch、看实时日志、浏览已同步 Markdown。

## CLI（可选）

```bash
python sync_docs.py --list-sources
python sync_docs.py --source claude --max-pages 5
python sync_docs.py --source all --max-pages 3 --interval 60
```

## 输出结构

```text
data/
  claude/
    llms.txt          # 最近一次索引快照
    state.json        # URL → hash / etag / path
    pages/
      docs/en/...md   # 正文 Markdown
  openai/
  gemini/
```

## 配置

编辑 `config.yaml` 可增删源、调整并发与过滤规则。默认源：

| key | index |
|-----|--------|
| `claude` | https://platform.claude.com/llms.txt |
| `openai` | https://developers.openai.com/api/llms.txt |
| `gemini` | https://ai.google.dev/gemini-api/docs/llms.txt |

## 说明

- 这是 Demo，不是生产爬虫：无代理池、无复杂重试队列、不写向量库。
- 「实时更新」通过 `--interval` 轮询实现；文档站一般没有公开推送 webhook。
- 请控制 `--concurrency` / `--delay`，遵守站点合理使用约定。
