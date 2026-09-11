from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from config_store import AgentConfig
from tools import TOOL_DEFINITIONS, run_tool


class LLMError(RuntimeError):
    pass


def _auth_headers(config: AgentConfig) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if config.protocol == "anthropic_messages":
        headers["x-api-key"] = config.api_key
        headers["anthropic-version"] = "2023-06-01"
    else:
        headers["Authorization"] = f"Bearer {config.api_key}"
    return headers


def _chat_url(config: AgentConfig) -> str:
    base = config.api_base_url
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


def _responses_url(config: AgentConfig) -> str:
    base = config.api_base_url
    if base.endswith("/responses"):
        return base
    return f"{base}/responses"


def _messages_url(config: AgentConfig) -> str:
    base = config.api_base_url
    if base.endswith("/messages"):
        return base
    return f"{base}/messages"


async def _post_json(config: AgentConfig, url: str, payload: dict[str, Any]) -> dict[str, Any]:
    timeout = httpx.Timeout(config.request_timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, headers=_auth_headers(config), json=payload)
    if response.status_code >= 400:
        raise LLMError(f"上游 API {response.status_code}: {response.text[:1200]}")
    return response.json()


def _extract_chat_message(data: dict[str, Any]) -> dict[str, Any]:
    try:
        return data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"Chat Completions 响应格式异常: {json.dumps(data)[:800]}") from exc


def _extract_responses_message(data: dict[str, Any]) -> dict[str, Any]:
    output = data.get("output") or []
    texts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for item in output:
        item_type = item.get("type")
        if item_type == "message":
            for part in item.get("content") or []:
                if part.get("type") in {"output_text", "text"} and part.get("text"):
                    texts.append(part["text"])
        elif item_type in {"function_call", "tool_call"}:
            tool_calls.append(
                {
                    "id": item.get("call_id") or item.get("id") or f"call_{len(tool_calls)}",
                    "type": "function",
                    "function": {
                        "name": item.get("name") or "",
                        "arguments": item.get("arguments")
                        if isinstance(item.get("arguments"), str)
                        else json.dumps(item.get("arguments") or {}, ensure_ascii=False),
                    },
                }
            )
    message: dict[str, Any] = {"role": "assistant", "content": "".join(texts) or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return message


def _extract_anthropic_message(data: dict[str, Any]) -> dict[str, Any]:
    texts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for block in data.get("content") or []:
        if block.get("type") == "text":
            texts.append(block.get("text") or "")
        elif block.get("type") == "tool_use":
            tool_calls.append(
                {
                    "id": block.get("id") or f"tool_{len(tool_calls)}",
                    "type": "function",
                    "function": {
                        "name": block.get("name") or "",
                        "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                    },
                }
            )
    message: dict[str, Any] = {"role": "assistant", "content": "".join(texts) or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return message


def _to_anthropic_messages(messages: list[dict[str, Any]]) -> tuple[str | None, list[dict[str, Any]]]:
    system_parts: list[str] = []
    converted: list[dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role")
        if role == "system":
            system_parts.append(str(msg.get("content") or ""))
            continue
        if role == "tool":
            converted.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": msg.get("tool_call_id"),
                            "content": str(msg.get("content") or ""),
                        }
                    ],
                }
            )
            continue
        if role == "assistant" and msg.get("tool_calls"):
            content: list[dict[str, Any]] = []
            if msg.get("content"):
                content.append({"type": "text", "text": msg["content"]})
            for call in msg["tool_calls"]:
                args = call.get("function", {}).get("arguments") or "{}"
                try:
                    parsed = json.loads(args) if isinstance(args, str) else args
                except json.JSONDecodeError:
                    parsed = {"raw": args}
                content.append(
                    {
                        "type": "tool_use",
                        "id": call.get("id"),
                        "name": call.get("function", {}).get("name"),
                        "input": parsed,
                    }
                )
            converted.append({"role": "assistant", "content": content})
            continue
        converted.append({"role": role, "content": msg.get("content") or ""})
    return ("\n".join(system_parts) if system_parts else None), converted


def _to_responses_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role")
        if role == "system":
            items.append(
                {
                    "role": "system",
                    "content": [{"type": "input_text", "text": str(msg.get("content") or "")}],
                }
            )
        elif role == "user":
            items.append(
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": str(msg.get("content") or "")}],
                }
            )
        elif role == "assistant":
            if msg.get("tool_calls"):
                if msg.get("content"):
                    items.append(
                        {
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": str(msg["content"])}],
                        }
                    )
                for call in msg["tool_calls"]:
                    items.append(
                        {
                            "type": "function_call",
                            "call_id": call.get("id"),
                            "name": call.get("function", {}).get("name"),
                            "arguments": call.get("function", {}).get("arguments") or "{}",
                        }
                    )
            else:
                items.append(
                    {
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": str(msg.get("content") or "")}],
                    }
                )
        elif role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": msg.get("tool_call_id"),
                    "output": str(msg.get("content") or ""),
                }
            )
    return items


async def call_model(config: AgentConfig, messages: list[dict[str, Any]]) -> dict[str, Any]:
    if not config.api_key:
        raise LLMError("尚未配置 API Key，请先在设置页填写。")
    if not config.model:
        raise LLMError("尚未配置模型 ID。")

    if config.protocol == "chat_completions":
        payload: dict[str, Any] = {
            "model": config.model,
            "messages": messages,
            "temperature": config.temperature,
            "max_tokens": config.max_tokens,
        }
        if config.enable_tools:
            payload["tools"] = TOOL_DEFINITIONS
            payload["tool_choice"] = "auto"
        data = await _post_json(config, _chat_url(config), payload)
        return _extract_chat_message(data)

    if config.protocol == "responses":
        payload = {
            "model": config.model,
            "input": _to_responses_input(messages),
            "temperature": config.temperature,
            "max_output_tokens": config.max_tokens,
        }
        if config.enable_tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "name": item["function"]["name"],
                    "description": item["function"].get("description", ""),
                    "parameters": item["function"].get("parameters", {}),
                }
                for item in TOOL_DEFINITIONS
            ]
        data = await _post_json(config, _responses_url(config), payload)
        return _extract_responses_message(data)

    if config.protocol == "anthropic_messages":
        system, converted = _to_anthropic_messages(messages)
        payload = {
            "model": config.model,
            "messages": converted,
            "max_tokens": config.max_tokens,
            "temperature": config.temperature,
        }
        if system:
            payload["system"] = system
        if config.enable_tools:
            payload["tools"] = [
                {
                    "name": item["function"]["name"],
                    "description": item["function"].get("description", ""),
                    "input_schema": item["function"].get("parameters", {"type": "object", "properties": {}}),
                }
                for item in TOOL_DEFINITIONS
            ]
        data = await _post_json(config, _messages_url(config), payload)
        return _extract_anthropic_message(data)

    raise LLMError(f"不支持的协议: {config.protocol}")


async def run_agent(
    config: AgentConfig,
    user_message: str,
    history: list[dict[str, Any]] | None = None,
    max_rounds: int = 8,
) -> AsyncIterator[dict[str, Any]]:
    import asyncio

    from memory_service import add_memory, format_memory_block, search_memories

    system_prompt = config.system_prompt
    recalled: list[dict[str, Any]] = []

    if config.enable_memory and config.api_key and config.memory_embed_api_key and config.memory_embed_base_url:
        yield {"type": "status", "message": "检索长期记忆…"}
        try:
            recalled = await asyncio.to_thread(search_memories, config, user_message)
            block = format_memory_block(recalled)
            if block:
                system_prompt = f"{config.system_prompt}\n\n{block}"
                yield {
                    "type": "memory",
                    "action": "recall",
                    "count": len(recalled),
                    "items": recalled,
                }
        except Exception as exc:  # noqa: BLE001
            yield {
                "type": "memory",
                "action": "recall_error",
                "message": f"记忆检索失败（已跳过）：{exc}",
            }

    messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    if history:
        for item in history:
            role = item.get("role")
            content = item.get("content")
            if role in {"user", "assistant"} and content:
                messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_message})

    yield {"type": "status", "message": "开始处理…"}

    for round_idx in range(max_rounds):
        yield {"type": "status", "message": f"调用模型（第 {round_idx + 1} 轮）…"}
        assistant = await call_model(config, messages)
        tool_calls = assistant.get("tool_calls") or []
        content = assistant.get("content")

        if tool_calls and config.enable_tools:
            messages.append(assistant)
            for call in tool_calls:
                name = call.get("function", {}).get("name") or ""
                raw_args = call.get("function", {}).get("arguments") or "{}"
                yield {
                    "type": "tool_call",
                    "name": name,
                    "arguments": raw_args,
                }
                result = run_tool(name, raw_args)
                yield {
                    "type": "tool_result",
                    "name": name,
                    "result": result[:4000],
                }
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id"),
                        "content": result,
                    }
                )
            continue

        final_text = content or ""
        yield {"type": "final", "content": final_text}

        if (
            config.enable_memory
            and config.api_key
            and config.memory_embed_api_key
            and config.memory_embed_base_url
            and final_text.strip()
        ):
            yield {"type": "status", "message": "写入长期记忆…"}
            try:
                await asyncio.to_thread(
                    add_memory,
                    config,
                    [
                        {"role": "user", "content": user_message},
                        {"role": "assistant", "content": final_text},
                    ],
                )
                yield {"type": "memory", "action": "write", "ok": True}
            except Exception as exc:  # noqa: BLE001
                yield {
                    "type": "memory",
                    "action": "write_error",
                    "message": f"记忆写入失败（不影响本次回答）：{exc}",
                }
        return

    yield {
        "type": "final",
        "content": "已达到最大工具循环次数，请拆分任务后重试。",
    }


async def probe_connection(config: AgentConfig) -> dict[str, Any]:
    probe_messages = [
        {"role": "system", "content": "Reply with exactly: ok"},
        {"role": "user", "content": "ping"},
    ]
    # 探测时关闭工具，避免上游因 tools 字段失败
    probe_config = config.model_copy(update={"enable_tools": False, "max_tokens": min(64, config.max_tokens)})
    message = await call_model(probe_config, probe_messages)
    return {
        "ok": True,
        "protocol": config.protocol,
        "model": config.model,
        "sample": (message.get("content") or "")[:200],
    }
