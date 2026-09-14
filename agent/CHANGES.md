# 本次改动说明

基于 `kingswitch-code-review.zip` 的代码审查与改造成果。

## 运行方式

```bash
cd agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp data/config.example.json data/config.json   # 然后填入你自己的上游 API
python run.py
```

跑测试：

```bash
pip install -r requirements-dev.txt
pytest          # 127 个用例
```

## 安全修复

- **`probe_agent_config` 对 yaml/toml 配置补了脱敏**。原先只有 JSON 分支走 `_redact_obj`，
  aider / continue / codex 这类非 JSON 配置里的明文 Key 会原样回显给模型和前端。
- **写渠道配置改为两段式，代码层面强制用户确认**。`write_agent_channel_config`（LLM 工具）
  只做校验和生成预览，真正落盘的 `execute_write_agent_channel_config` 只能经
  `POST /api/agents/write-config` 调用，而该接口只由前端「确认写入」按钮触发。
  模型没有代码路径可以绕过确认直接改本机其它 Agent 的配置文件。
- **聊天内容里的密钥不再原样落盘**。`memory_service.redact_secrets` 会在写入和读取两侧
  都过一遍正则脱敏，覆盖 `Authorization: Bearer xxx`、裸 `sk-xxx`、`api_key="xxx"` 等形态。
  展示路径也兜底脱敏，修复上线前已存在的明文记录同样会被遮蔽。
- 目标文件 JSON 解析失败时不再静默清空重建，改为返回 `warning` 并保留备份。

## 逻辑修复

- 流式请求补上 `stream_options.include_usage`，否则多数 OpenAI 兼容网关不会回传 usage。
- `write_agent_channel_config` 补 `model` 非空校验（原先漏校验会把字面量 `custom` 写进配置）。
- `check_agent_installed` 的可执行文件探测纳入 `~/.npm-global/bin`、`~/.local/bin`，
  修复"装完立刻检测却报未安装"（安装时用的是子进程改过的 PATH，检测时读的是没更新的）。
- `check_agent_installed` 对未知 `agent_id` 返回 `ok:false`，与其它工具契约一致
  （顺带激活了 main.py 里一段一直没触发过的 404 分支）。
- 工具调用改为 `asyncio.to_thread` 执行，不再阻塞事件循环。

## 新增能力

- **自动巡检**（`scan_service.py`）：探测本机已知 Agent 的安装 / 渠道配置 / 连通性 /
  工具调用 / 协议保真，输出 Markdown 报告到 `data/reports/`。
- **渠道配置历史**：结构化记录每次成功写入（`list_channel_history` 工具可查）。
- **记忆库面板**：长期 / 短期记忆分离，token 消耗统计（输入 / 输出 / 缓存），
  点击记忆可跳回来源会话回放。

## 前端重构

- 设置面板从单一长表单拆成 4 层卡片：模型服务 / 能力开关 / 记忆细节 / 系统提示词，
  记忆细节跟随开关联动禁用。
- 配色与字体改为暖色调（参照 Anthropic/Claude 的界面基调）。

## 已知遗留

- `test_agent_channel` 仍会用当轮参数发真实探测请求，且不做内网地址限制——
  因为探测本机 / 内网自建网关是合法用法，加黑名单会误伤。
- 模型若幻觉出未注册的工具名，该 `tool_call` 不会有对应应答，可能导致下一轮被上游拒绝。
- 未加 CSRF / Origin 校验；写入的配置文件未设 `chmod 600`。

## 流水线状态机（代码强制）

- 新增 `pipeline.py`：`ChannelSetupState` + 工具门禁 + 进度文案
- `run_tool` / `write_agent_channel_config` 在 `target_agent` 未锁定时直接拒绝写入预览
- `run_agent` 每轮注入进度 system 消息；写入确认 / 安装完成会推进 `written` / `install_status`
- `judger_prompt.py` 精简为角色与决策原则，8 步细节不再只靠提示词约束

## 前端体验优化（流水线可见化）

- 常驻「渠道配置进度」8 步指示条，消费 `pipeline` SSE / 写入·安装回写
- 写入确认卡展示 Base URL / 模型 / 协议 / 路径，支持取消
- 安装成功、写入成功后提供「继续配置 / 继续测试」快捷发送
- 发送中可「停止」（AbortController）；输入框不再整框锁死
- 未配置 API Key 时硬引导；示例 chips 降低冷启动成本
- 巡检结果行可点击发起对应配置意图
- Esc 关闭设置/记忆抽屉；清空对话提示新会话 + 进度重置
