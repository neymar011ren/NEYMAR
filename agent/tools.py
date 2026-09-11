from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from protocol_adapter import protocol_adapt

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
LOCAL_TOOL_NAMES = {"list_files", "read_file", "write_file", "protocol_adapt"}
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
