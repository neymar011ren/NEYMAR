from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
MAX_READ_CHARS = 80_000


def _safe_path(relative: str) -> Path:
    raw = (relative or "").strip() or "."
    candidate = (WORKSPACE_ROOT / raw).resolve()
    if not str(candidate).startswith(str(WORKSPACE_ROOT)):
        raise ValueError("路径越界：只能访问当前工作区")
    return candidate


def list_files(path: str = ".", max_entries: int = 200) -> str:
    target = _safe_path(path)
    if not target.exists():
        return f"路径不存在: {path}"
    if target.is_file():
        return f"这是文件: {target.relative_to(WORKSPACE_ROOT)}"
    entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    lines: list[str] = []
    for item in entries[:max_entries]:
        rel = item.relative_to(WORKSPACE_ROOT).as_posix()
        kind = "dir" if item.is_dir() else "file"
        size = "" if item.is_dir() else f" ({item.stat().st_size} B)"
        lines.append(f"[{kind}] {rel}{size}")
    if len(entries) > max_entries:
        lines.append(f"... 还有 {len(entries) - max_entries} 项未显示")
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
]


HANDLERS: dict[str, Callable[..., str]] = {
    "list_files": lambda path=".": list_files(path),
    "read_file": lambda path: read_file(path),
    "write_file": lambda path, content: write_file(path, content),
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
