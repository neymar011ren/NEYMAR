"""单向协议适配器：Responses / Anthropic Messages → Chat Completions。

许多上游（如兼容 WorkBuddy / 部分国产网关）只接受 Chat Completions。
本模块把另外两种协议的请求体转换成 Chat Completions，并可选择直接发起调用。
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from config_store import load_config

SOURCE_PROTOCOLS = ("responses", "anthropic_messages")
TARGET_PROTOCOL = "chat_completions"
MAX_RESULT_CHARS = 12_000


class ProtocolAdaptError(ValueError):
    pass


def _as_dict(request: Any) -> dict[str, Any]:
    if isinstance(request, str):
        text = request.strip()
        if not text:
            raise ProtocolAdaptError("request 为空")
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProtocolAdaptError(f"request 不是合法 JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ProtocolAdaptError("request JSON 必须是对象")
        return parsed
    if isinstance(request, dict):
        return request
    raise ProtocolAdaptError("request 必须是 JSON 对象或 JSON 字符串")


def _chat_url(base: str) -> str:
    base = (base or "").rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


def _parse_args(raw: Any) -> Any:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"raw": raw}
    return raw if raw is not None else {}


def _anthropic_content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            elif block.get("type") == "tool_result":
                inner = block.get("content")
                if isinstance(inner, list):
                    parts.append(_anthropic_content_to_text(inner))
                else:
                    parts.append(str(inner or ""))
        return "".join(parts)
    return str(content)


def convert_anthropic_to_chat(payload: dict[str, Any]) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    system = payload.get("system")
    if isinstance(system, str) and system.strip():
        messages.append({"role": "system", "content": system})
    elif isinstance(system, list):
        text = _anthropic_content_to_text(system)
        if text.strip():
            messages.append({"role": "system", "content": text})

    for msg in payload.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content")
        if role == "user":
            if isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in content
            ):
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_result":
                        continue
                    inner = block.get("content")
                    if isinstance(inner, list):
                        tool_text = _anthropic_content_to_text(inner)
                    else:
                        tool_text = str(inner or "")
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": block.get("tool_use_id") or block.get("id") or "tool",
                            "content": tool_text,
                        }
                    )
                # 同条 user 里若还有纯文本，一并保留
                text_only = [
                    b
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "text" and b.get("text")
                ]
                if text_only:
                    messages.append(
                        {
                            "role": "user",
                            "content": "".join(str(b.get("text") or "") for b in text_only),
                        }
                    )
                continue
            messages.append({"role": "user", "content": _anthropic_content_to_text(content)})
        elif role == "assistant":
            out: dict[str, Any] = {"role": "assistant", "content": None}
            texts: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            if isinstance(content, str):
                texts.append(content)
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        texts.append(str(block.get("text") or ""))
                    elif block.get("type") == "tool_use":
                        tool_calls.append(
                            {
                                "id": block.get("id") or f"call_{len(tool_calls)}",
                                "type": "function",
                                "function": {
                                    "name": block.get("name") or "",
                                    "arguments": json.dumps(
                                        block.get("input") or {}, ensure_ascii=False
                                    ),
                                },
                            }
                        )
            if texts:
                out["content"] = "".join(texts)
            if tool_calls:
                out["tool_calls"] = tool_calls
            messages.append(out)

    chat: dict[str, Any] = {
        "model": payload.get("model"),
        "messages": messages,
    }
    if "temperature" in payload:
        chat["temperature"] = payload["temperature"]
    if "max_tokens" in payload:
        chat["max_tokens"] = payload["max_tokens"]
    if payload.get("tools"):
        chat["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": item.get("name"),
                    "description": item.get("description") or "",
                    "parameters": item.get("input_schema")
                    or item.get("parameters")
                    or {"type": "object", "properties": {}},
                },
            }
            for item in payload["tools"]
            if isinstance(item, dict) and item.get("name")
        ]
        chat["tool_choice"] = "auto"
    return chat


def convert_responses_to_chat(payload: dict[str, Any]) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    instructions = payload.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        messages.append({"role": "system", "content": instructions})

    raw_input = payload.get("input")
    items: list[Any]
    if isinstance(raw_input, str):
        items = [{"type": "message", "role": "user", "content": raw_input}]
    elif isinstance(raw_input, list):
        items = raw_input
    else:
        items = []

    pending_assistant_tools: list[dict[str, Any]] = []

    def flush_assistant_tools() -> None:
        nonlocal pending_assistant_tools
        if pending_assistant_tools:
            messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": pending_assistant_tools,
                }
            )
            pending_assistant_tools = []

    for item in items:
        if isinstance(item, str):
            flush_assistant_tools()
            messages.append({"role": "user", "content": item})
            continue
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type in {None, "message"}:
            flush_assistant_tools()
            role = item.get("role") or "user"
            content = item.get("content")
            if isinstance(content, list):
                texts: list[str] = []
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") in {
                        "input_text",
                        "output_text",
                        "text",
                    } and part.get("text"):
                        texts.append(str(part["text"]))
                text = "".join(texts)
            else:
                text = str(content or "")
            if role in {"user", "assistant", "system"}:
                messages.append({"role": role, "content": text})
        elif item_type in {"function_call", "tool_call"}:
            pending_assistant_tools.append(
                {
                    "id": item.get("call_id") or item.get("id") or f"call_{len(pending_assistant_tools)}",
                    "type": "function",
                    "function": {
                        "name": item.get("name") or "",
                        "arguments": item.get("arguments")
                        if isinstance(item.get("arguments"), str)
                        else json.dumps(item.get("arguments") or {}, ensure_ascii=False),
                    },
                }
            )
        elif item_type == "function_call_output":
            flush_assistant_tools()
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": item.get("call_id") or item.get("id") or "tool",
                    "content": str(item.get("output") or ""),
                }
            )
    flush_assistant_tools()

    chat: dict[str, Any] = {
        "model": payload.get("model"),
        "messages": messages,
    }
    if "temperature" in payload:
        chat["temperature"] = payload["temperature"]
    if "max_output_tokens" in payload:
        chat["max_tokens"] = payload["max_output_tokens"]
    elif "max_tokens" in payload:
        chat["max_tokens"] = payload["max_tokens"]

    tools_out: list[dict[str, Any]] = []
    for item in payload.get("tools") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "function" or "name" in item:
            name = item.get("name") or (item.get("function") or {}).get("name")
            if not name:
                continue
            fn = item.get("function") if isinstance(item.get("function"), dict) else {}
            tools_out.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": item.get("description") or fn.get("description") or "",
                        "parameters": item.get("parameters")
                        or fn.get("parameters")
                        or {"type": "object", "properties": {}},
                    },
                }
            )
    if tools_out:
        chat["tools"] = tools_out
        chat["tool_choice"] = "auto"
    return chat


def convert_to_chat_completions(source_protocol: str, request: Any) -> dict[str, Any]:
    source = (source_protocol or "").strip()
    if source not in SOURCE_PROTOCOLS:
        raise ProtocolAdaptError(
            f"仅支持单向转换：{list(SOURCE_PROTOCOLS)} → {TARGET_PROTOCOL}，收到: {source_protocol!r}"
        )
    payload = _as_dict(request)
    if source == "anthropic_messages":
        chat = convert_anthropic_to_chat(payload)
    else:
        chat = convert_responses_to_chat(payload)
    if not chat.get("messages"):
        raise ProtocolAdaptError("转换后 messages 为空，请检查源请求体")
    return chat


def execute_chat_completions(chat_payload: dict[str, Any]) -> dict[str, Any]:
    config = load_config()
    if not config.api_key:
        raise ProtocolAdaptError("尚未配置 API Key，无法执行转换后的请求")
    if not config.api_base_url:
        raise ProtocolAdaptError("尚未配置 API Base URL")

    body = dict(chat_payload)
    if not body.get("model"):
        body["model"] = config.model
    if "temperature" not in body:
        body["temperature"] = config.temperature
    if "max_tokens" not in body:
        body["max_tokens"] = config.max_tokens

    url = _chat_url(config.api_base_url)
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {config.api_key}",
    }
    timeout = httpx.Timeout(config.request_timeout_seconds)
    with httpx.Client(timeout=timeout) as client:
        response = client.post(url, headers=headers, json=body)
    if response.status_code >= 400:
        raise ProtocolAdaptError(
            f"上游 Chat Completions {response.status_code}: {response.text[:1200]}"
        )
    try:
        return response.json()
    except json.JSONDecodeError as exc:
        raise ProtocolAdaptError(f"上游返回非 JSON: {response.text[:800]}") from exc


def protocol_adapt(
    source_protocol: str,
    request: Any = None,
    execute: bool = True,
) -> str:
    """供 Agent 工具调用的入口。

    - source_protocol: responses | anthropic_messages
    - request: 源协议请求体（对象或 JSON 字符串）
    - execute: true 时转换后向当前配置的 Chat Completions 端点发起调用；
      false 时只返回转换后的 Chat Completions 请求体
    """
    try:
        if request is None:
            raise ProtocolAdaptError("请提供 request 对象或 raw_json 字符串")
        chat_payload = convert_to_chat_completions(source_protocol, request)
        if not execute:
            return json.dumps(
                {
                    "ok": True,
                    "direction": f"{source_protocol} → {TARGET_PROTOCOL}",
                    "converted_request": chat_payload,
                    "executed": False,
                },
                ensure_ascii=False,
                indent=2,
            )[:MAX_RESULT_CHARS]

        upstream = execute_chat_completions(chat_payload)
        message = ((upstream.get("choices") or [{}])[0].get("message")) or {}
        content = message.get("content")
        tool_calls = message.get("tool_calls")
        result = {
            "ok": True,
            "direction": f"{source_protocol} → {TARGET_PROTOCOL}",
            "executed": True,
            "model": upstream.get("model") or chat_payload.get("model"),
            "content": content,
            "tool_calls": tool_calls,
            "usage": upstream.get("usage"),
            "converted_request_preview": {
                "model": chat_payload.get("model"),
                "message_count": len(chat_payload.get("messages") or []),
                "has_tools": bool(chat_payload.get("tools")),
            },
        }
        text = json.dumps(result, ensure_ascii=False, indent=2)
        if len(text) > MAX_RESULT_CHARS:
            return text[:MAX_RESULT_CHARS] + "\n...[截断]"
        return text
    except ProtocolAdaptError as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"ok": False, "error": f"协议适配失败: {exc}"}, ensure_ascii=False)
