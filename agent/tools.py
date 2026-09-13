from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from protocol_adapter import protocol_adapt
from agent_config_tools import (
    detect_target_agent,
    list_channel_history,
    list_known_agents,
    probe_agent_config,
    test_agent_channel,
    write_agent_channel_config,
)
from agent_install import tool_check_agent_installed, tool_prepare_agent_install

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
LOCAL_TOOL_NAMES = {
    "list_files",
    "read_file",
    "write_file",
    "protocol_adapt",
    "list_known_agents",
    "detect_target_agent",
    "probe_agent_config",
    "write_agent_channel_config",
    "test_agent_channel",
    "check_agent_installed",
    "prepare_agent_install",
    "list_channel_history",
}
AGENT_DIR = Path(__file__).resolve().parent
SENSITIVE_DIRS = (
    (AGENT_DIR / "data").resolve(),
)
SENSITIVE_FILES = {
    (AGENT_DIR / "data" / "config.json").resolve(),
}
MAX_READ_CHARS = 80_000


def _is_within(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _is_sensitive(path: Path) -> bool:
    resolved = path.resolve()
    if resolved in SENSITIVE_FILES:
        return True
    return any(_is_within(sensitive_dir, resolved) for sensitive_dir in SENSITIVE_DIRS)


def _safe_path(relative: str) -> Path:
    raw = (relative or "").strip() or "."
    candidate = (WORKSPACE_ROOT / raw).resolve()
    if not _is_within(WORKSPACE_ROOT, candidate):
        raise ValueError("路径越界：只能访问当前工作区")
    if _is_sensitive(candidate):
        raise ValueError("拒绝访问敏感配置文件")
    return candidate


def list_files(path: str = ".", max_entries: int = 200) -> str:
    target = _safe_path(path)
    if not target.exists():
        return f"路径不存在: {path}"
    if target.is_file():
        return f"这是文件: {target.relative_to(WORKSPACE_ROOT)}"
    entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    lines: list[str] = []
    shown = 0
    skipped_sensitive = 0
    for item in entries:
        if _is_sensitive(item):
            skipped_sensitive += 1
            continue
        if shown >= max_entries:
            continue
        rel = item.relative_to(WORKSPACE_ROOT).as_posix()
        kind = "dir" if item.is_dir() else "file"
        size = "" if item.is_dir() else f" ({item.stat().st_size} B)"
        lines.append(f"[{kind}] {rel}{size}")
        shown += 1
    remaining = max(0, len(entries) - shown - skipped_sensitive)
    if remaining:
        lines.append(f"... 还有 {remaining} 项未显示")
    if skipped_sensitive:
        lines.append(f"... 已隐藏 {skipped_sensitive} 个敏感配置项")
    return "\n".join(lines) or "(空目录)"


def read_file(path: str) -> str:
    target = _safe_path(path)
    if not target.exists() or not target.is_file():
        return f"文件不存在: {path}"
    text = target.read_text(encoding="utf-8", errors="replace")
    if len(text) > MAX_READ_CHARS:
        return text[:MAX_READ_CHARS] + f"\n\n...[截断，共 {len(text)} 字符]"
    return text


def write_file(path: str, content: str) -> str:
    target = _safe_path(path)
    if target.exists() and target.is_dir():
        return f"不能写入目录: {path}"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"已写入 {target.relative_to(WORKSPACE_ROOT).as_posix()}（{len(content)} 字符）"


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "列出工作区内某个相对路径下的文件和目录。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "相对工作区的路径，默认当前根目录。",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取工作区内的文本文件内容。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对工作区的文件路径"}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "向工作区写入或覆盖文本文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对工作区的文件路径"},
                    "content": {"type": "string", "description": "要写入的完整文本内容"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "protocol_adapt",
            "description": (
                "单向协议适配器：把 OpenAI Responses 或 Anthropic Messages 格式的请求"
                "转换为 Chat Completions，并可调用当前配置的上游 /v1/chat/completions。"
                "适用场景：用户给了其他协议的请求体、或需要把非 Chat Completions 语义"
                "桥接到只支持 Chat Completions 的网关。不要用于本身已是 Chat Completions 的请求；"
                "也不支持反向转换。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "source_protocol": {
                        "type": "string",
                        "enum": ["responses", "anthropic_messages"],
                        "description": "源协议。仅支持 responses 或 anthropic_messages。",
                    },
                    "request": {
                        "type": "object",
                        "description": (
                            "源协议的完整请求体对象。"
                            "也可把整段 JSON 字符串放进字段 raw_json（若你更方便）；"
                            "优先传对象。Anthropic 常用：model/system/messages/max_tokens/tools；"
                            "Responses 常用：model/instructions/input/max_output_tokens/tools。"
                        ),
                        "additionalProperties": True,
                    },
                    "raw_json": {
                        "type": "string",
                        "description": "可选。若不便构造对象，可传源请求的 JSON 字符串；与 request 二选一，优先 request。",
                    },
                    "execute": {
                        "type": "boolean",
                        "description": (
                            "true（默认）：转换后立刻用当前配置的 API Key/Base URL 调用 Chat Completions；"
                            "false：只返回转换后的 Chat Completions 请求体，不发网络请求。"
                        ),
                        "default": True,
                    },
                },
                "required": ["source_protocol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_known_agents",
            "description": "列出 KingSwitch 支持自动探测/写入的目标 Agent 档案（名称、别名、原生协议、配置路径）。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "detect_target_agent",
            "description": "根据用户原话判断要把模型 API 配置到哪个 Agent。返回候选 id、分数与原生协议。",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_text": {
                        "type": "string",
                        "description": "用户输入的原话",
                    }
                },
                "required": ["user_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "probe_agent_config",
            "description": "探测本地是否存在该 Agent 的配置文件，并返回脱敏预览。",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "如 claude_code / workbuddy / continue / codex / cline / aider / opencode / kingswitch",
                    }
                },
                "required": ["agent_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_agent_channel_config",
            "description": (
                "校验参数并生成「写入预览」（目标路径、是否新建、脱敏后的 Key），"
                "不会真正落盘。真正写入只会在用户于界面点击预览卡片上的「确认写入」"
                "按钮后才会发生。调用后直接把 message 里的提示转述给用户、"
                "提醒其点击确认即可，不要假设已经写完、也不要重复调用来'确认'。"
                "必须等用户明确提供密钥后再调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string"},
                    "base_url": {"type": "string", "description": "模型渠道 Base URL，通常到 /v1"},
                    "model": {"type": "string", "description": "模型名称/ID"},
                    "api_key": {"type": "string", "description": "用户自行提供的密钥"},
                    "protocol": {
                        "type": "string",
                        "enum": ["chat_completions", "responses", "anthropic_messages"],
                        "description": "仅 kingswitch 可用来设置协议；其他 Agent 可忽略",
                    },
                    "create_if_missing": {
                        "type": "boolean",
                        "description": "本地无配置文件时是否创建，默认 true",
                        "default": True,
                    },
                },
                "required": ["agent_id", "base_url", "model", "api_key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "test_agent_channel",
            "description": (
                "验证协议转换与渠道是否可用：按 Agent 原生协议构造探测请求，"
                "必要时转换为 Chat Completions，再用用户提供的 Base URL/Key/模型发起真实 HTTP 探测。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string"},
                    "base_url": {"type": "string"},
                    "model": {"type": "string"},
                    "api_key": {"type": "string"},
                    "source_protocol": {
                        "type": "string",
                        "enum": ["chat_completions", "responses", "anthropic_messages"],
                        "description": "覆盖档案中的原生协议；默认用档案值",
                    },
                },
                "required": ["agent_id", "base_url", "model", "api_key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_agent_installed",
            "description": (
                "检测本地是否已安装目标 Agent（可执行文件 / 配置目录）。"
                "应在 probe_agent_config 之前调用；若未安装则继续 prepare_agent_install。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "如 claude_code / workbuddy / aider / codex 等",
                    }
                },
                "required": ["agent_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "prepare_agent_install",
            "description": (
                "在确认本地未安装后调用：按当前操作系统生成安装方案，并让前端弹出「安装」按钮。"
                "调用前应先联网搜索该 Agent 在本 OS 上的官方安装方式，把摘要写入 search_notes。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string"},
                    "search_notes": {
                        "type": "string",
                        "description": "联网搜索到的安装要点摘要（官网、包名、系统差异）",
                    },
                },
                "required": ["agent_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_channel_history",
            "description": (
                "查此前 KingSwitch 成功写入过的渠道配置历史（结构化记录：agent_id/base_url/"
                "模型/协议/写入路径/脱敏后的 Key，按时间倒序）。"
                "适合回答'上次给这个 Agent 配的是什么/什么时候配的'，"
                "比翻聊天记录里的自由文本记忆更可靠。不传 agent_id 则返回所有 Agent 的最近记录。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "可选，指定只看某个 Agent 的历史，如 claude_code",
                    }
                },
            },
        },
    },
]


HANDLERS: dict[str, Callable[..., str]] = {
    "list_files": lambda path=".": list_files(path),
    "read_file": lambda path: read_file(path),
    "write_file": lambda path, content: write_file(path, content),
    "protocol_adapt": lambda source_protocol, request=None, raw_json=None, execute=True: protocol_adapt(
        source_protocol,
        request if request is not None else raw_json,
        execute=execute,
    ),
    "list_known_agents": lambda: list_known_agents(),
    "detect_target_agent": lambda user_text: detect_target_agent(user_text),
    "probe_agent_config": lambda agent_id: probe_agent_config(agent_id),
    "write_agent_channel_config": lambda agent_id, base_url, model, api_key, protocol=None, create_if_missing=True: write_agent_channel_config(
        agent_id,
        base_url,
        model,
        api_key,
        protocol=protocol,
        create_if_missing=create_if_missing,
    ),
    "test_agent_channel": lambda agent_id, base_url, model, api_key, source_protocol=None: test_agent_channel(
        agent_id,
        base_url,
        model,
        api_key,
        source_protocol=source_protocol,
    ),
    "check_agent_installed": lambda agent_id: tool_check_agent_installed(agent_id),
    "prepare_agent_install": lambda agent_id, search_notes="": tool_prepare_agent_install(
        agent_id, search_notes=search_notes
    ),
    "list_channel_history": lambda agent_id="": list_channel_history(agent_id),
}


def run_tool(name: str, arguments: dict[str, Any] | str | None) -> str:
    if name not in HANDLERS:
        return f"未知工具: {name}"
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments) if arguments else {}
        except json.JSONDecodeError:
            return f"工具参数不是合法 JSON: {arguments}"
    arguments = arguments or {}
    try:
        return HANDLERS[name](**arguments)
    except TypeError as exc:
        return f"工具参数错误: {exc}"
    except Exception as exc:  # noqa: BLE001
        return f"工具执行失败: {exc}"
