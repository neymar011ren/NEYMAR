from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from config_store import AgentConfig
from tools import LOCAL_TOOL_NAMES, TOOL_DEFINITIONS, run_tool


_PIPELINE_PROGRESS_PREFIX = "【渠道配置流水线"


def _upsert_pipeline_progress(messages: list[dict[str, Any]], state) -> None:
    """把流水线进度写成独立 system 消息，每轮覆盖，避免模型靠聊天记录猜进度。"""
    from pipeline import format_progress

    content = format_progress(state)
    for idx, msg in enumerate(messages):
        if (
            msg.get("role") == "system"
            and isinstance(msg.get("content"), str)
            and msg["content"].startswith(_PIPELINE_PROGRESS_PREFIX)
        ):
            messages[idx] = {"role": "system", "content": content}
            return
    insert_at = 1 if messages and messages[0].get("role") == "system" else 0
    messages.insert(insert_at, {"role": "system", "content": content})



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


def _build_chat_payload(config: AgentConfig, messages: list[dict[str, Any]], *, stream: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": config.model,
        "messages": messages,
        "temperature": config.temperature,
        "max_tokens": config.max_tokens,
        "stream": stream,
    }
    if stream:
        # 不显式请求 include_usage，多数 OpenAI 兼容网关在流式响应里不会带 usage 字段，
        # 导致 web_search_queries 统计永远拿不到值。
        payload["stream_options"] = {"include_usage": True}
    tools: list[dict[str, Any]] = []
    if config.enable_web_search:
        tools.append({"type": "web_search"})
    if config.enable_tools:
        tools.extend(TOOL_DEFINITIONS)
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    return payload


def _merge_tool_call_delta(
    bucket: dict[int, dict[str, Any]],
    tc_delta: dict[str, Any],
) -> None:
    idx = int(tc_delta.get("index") or 0)
    slot = bucket.setdefault(
        idx,
        {
            "id": "",
            "type": "function",
            "function": {"name": "", "arguments": ""},
        },
    )
    if tc_delta.get("id"):
        slot["id"] = tc_delta["id"]
    if tc_delta.get("type"):
        slot["type"] = tc_delta["type"]
    fn = tc_delta.get("function") or {}
    if fn.get("name"):
        slot["function"]["name"] += fn["name"]
    if fn.get("arguments"):
        slot["function"]["arguments"] += fn["arguments"]


def normalize_usage(raw: Any, protocol: str) -> dict[str, int]:
    """把三种协议各不相同的 usage 字段归一成同一套计数。

    - chat_completions: prompt_tokens / completion_tokens / prompt_tokens_details.cached_tokens
    - responses:        input_tokens  / output_tokens     / input_tokens_details.cached_tokens
    - anthropic:        input_tokens  / output_tokens     / cache_read_input_tokens
                        （另有 cache_creation_input_tokens，属于"写缓存"，单独并入 cached 统计口径外）
    """
    out = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "total_tokens": 0}
    if not isinstance(raw, dict):
        return out

    def _int(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    if protocol == "anthropic_messages":
        out["prompt_tokens"] = _int(raw.get("input_tokens"))
        out["completion_tokens"] = _int(raw.get("output_tokens"))
        out["cached_tokens"] = _int(raw.get("cache_read_input_tokens"))
    else:
        # Chat Completions 与 Responses 两套命名都兜一遍，谁有值用谁
        out["prompt_tokens"] = _int(raw.get("prompt_tokens")) or _int(raw.get("input_tokens"))
        out["completion_tokens"] = _int(raw.get("completion_tokens")) or _int(raw.get("output_tokens"))
        details = raw.get("prompt_tokens_details") or raw.get("input_tokens_details") or {}
        if isinstance(details, dict):
            out["cached_tokens"] = _int(details.get("cached_tokens"))
    out["total_tokens"] = _int(raw.get("total_tokens")) or (out["prompt_tokens"] + out["completion_tokens"])
    return out


def accumulate_usage(target: dict[str, int], delta: dict[str, int]) -> dict[str, int]:
    for key in ("prompt_tokens", "completion_tokens", "cached_tokens", "total_tokens"):
        target[key] = int(target.get(key, 0)) + int(delta.get(key, 0))
    return target


async def _stream_chat_completions(
    config: AgentConfig,
    messages: list[dict[str, Any]],
) -> AsyncIterator[dict[str, Any]]:
    """流式调用 Chat Completions，产出 delta / message 事件。"""
    payload = _build_chat_payload(config, messages, stream=True)
    url = _chat_url(config)
    timeout = httpx.Timeout(config.request_timeout_seconds)
    content_parts: list[str] = []
    tool_buckets: dict[int, dict[str, Any]] = {}
    web_search_queries = None
    stream_usage: dict[str, int] | None = None

    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("POST", url, headers=_auth_headers(config), json=payload) as response:
            if response.status_code >= 400:
                body = await response.aread()
                raise LLMError(f"上游 API {response.status_code}: {body[:1200].decode('utf-8', errors='replace')}")
            async for line in response.aiter_lines():
                if not line:
                    continue
                if line.startswith(":"):
                    continue
                if not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                usage = chunk.get("usage")
                if isinstance(usage, dict):
                    if "web_search_queries" in usage:
                        web_search_queries = usage.get("web_search_queries")
                    # stream_options.include_usage 让上游在末尾 chunk 里带上真实用量
                    normalized = normalize_usage(usage, "chat_completions")
                    if normalized["total_tokens"]:
                        stream_usage = normalized
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                piece = delta.get("content")
                if piece:
                    content_parts.append(piece)
                    yield {"type": "delta", "content": piece}
                for tc in delta.get("tool_calls") or []:
                    if isinstance(tc, dict):
                        _merge_tool_call_delta(tool_buckets, tc)

    message: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(content_parts) or None,
    }
    if tool_buckets:
        message["tool_calls"] = [tool_buckets[i] for i in sorted(tool_buckets)]
        for call in message["tool_calls"]:
            if not call.get("id"):
                call["id"] = f"call_{call['function'].get('name', 'tool')}"
    if web_search_queries is not None:
        message["_web_search_queries"] = web_search_queries
    if stream_usage is not None:
        message["_usage"] = stream_usage
    yield {"type": "message", "message": message}


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
        payload = _build_chat_payload(config, messages, stream=False)
        data = await _post_json(config, _chat_url(config), payload)
        message = _extract_chat_message(data)
        usage = data.get("usage") if isinstance(data, dict) else None
        if isinstance(usage, dict) and "web_search_queries" in usage:
            message["_web_search_queries"] = usage.get("web_search_queries")
        message["_usage"] = normalize_usage(usage, "chat_completions")
        return message

    if config.protocol == "responses":
        payload = {
            "model": config.model,
            "input": _to_responses_input(messages),
            "temperature": config.temperature,
            "max_output_tokens": config.max_tokens,
        }
        tools = []
        if config.enable_web_search:
            tools.append({"type": "web_search"})
        if config.enable_tools:
            tools.extend(
                [
                    {
                        "type": "function",
                        "name": item["function"]["name"],
                        "description": item["function"].get("description", ""),
                        "parameters": item["function"].get("parameters", {}),
                    }
                    for item in TOOL_DEFINITIONS
                ]
            )
        if tools:
            payload["tools"] = tools
        data = await _post_json(config, _responses_url(config), payload)
        message = _extract_responses_message(data)
        message["_usage"] = normalize_usage(data.get("usage") if isinstance(data, dict) else None, "responses")
        return message

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
        message = _extract_anthropic_message(data)
        message["_usage"] = normalize_usage(
            data.get("usage") if isinstance(data, dict) else None, "anthropic_messages"
        )
        return message

    raise LLMError(f"不支持的协议: {config.protocol}")


async def iter_model_events(
    config: AgentConfig,
    messages: list[dict[str, Any]],
) -> AsyncIterator[dict[str, Any]]:
    """统一模型调用事件流：优先 Chat Completions 真流式，其他协议回退一次性。"""
    if not config.api_key:
        raise LLMError("尚未配置 API Key，请先在设置页填写。")
    if not config.model:
        raise LLMError("尚未配置模型 ID。")
    if config.protocol == "chat_completions":
        async for event in _stream_chat_completions(config, messages):
            yield event
        return
    message = await call_model(config, messages)
    content = message.get("content") or ""
    # 非流式协议：按块伪流式，改善观感
    step = 24
    for i in range(0, len(content), step):
        yield {"type": "delta", "content": content[i : i + step]}
    yield {"type": "message", "message": message}


def _split_answer_and_refs(text: str) -> tuple[str, list[dict[str, str]]]:
    """把正文与参考资料粗分，便于前端分区。"""
    if not text:
        return "", []
    import re

    patterns = [
        r"\n---+\s*\n\s*\*{0,2}参考资料\*{0,2}\s*[:：]?\*{0,2}\s*\n",
        r"\n\*{0,2}参考资料\*{0,2}\s*[:：]?\*{0,2}\s*\n",
        r"\n参考资料\s*[:：]\s*\n",
    ]
    split_at = None
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            split_at = m
            break
    if not split_at:
        # 兜底：单独一行的“参考资料”
        m = re.search(r"\n[^\S\n]*参考资料[^\n]*\n", text)
        if m:
            split_at = m
    if not split_at:
        return text, []
    body = text[: split_at.start()].rstrip()
    # 若正文末尾残留 --- 分隔线，去掉
    body = re.sub(r"\n---+\s*$", "", body).rstrip()
    refs_raw = text[split_at.end() :]
    refs: list[dict[str, str]] = []
    for line in refs_raw.splitlines():
        line = line.strip().lstrip("-").strip()
        if not line:
            continue
        m = re.match(r"^(?:\d+[\.\)]\s*)?\[([^\]]+)\]\(([^)]+)\)\s*$", line)
        if m:
            refs.append({"title": m.group(1), "url": m.group(2)})
            continue
        m2 = re.match(r"^(?:\d+[\.\)]\s*)?(.+?)\s*[—\-]\s*(https?://\S+)\s*$", line)
        if m2:
            refs.append({"title": m2.group(1).strip(), "url": m2.group(2)})
            continue
        m3 = re.search(r"(https?://\S+)", line)
        if m3:
            refs.append(
                {
                    "title": line.replace(m3.group(1), "").strip(" -—:") or m3.group(1),
                    "url": m3.group(1).rstrip(")，。]"),
                }
            )
        else:
            refs.append({"title": line, "url": ""})
    return body, refs


async def run_agent(
    config: AgentConfig,
    user_message: str,
    history: list[dict[str, Any]] | None = None,
    max_rounds: int = 12,
    session_id: str | None = None,
) -> AsyncIterator[dict[str, Any]]:
    import asyncio

    from memory_service import add_memory, format_memory_block, record_session_turn, search_memories

    system_prompt = config.system_prompt
    recalled: list[dict[str, Any]] = []
    # 这一轮问答（可能跨多次模型调用）的累计消耗，用于短期记忆落盘和前端展示
    turn_usage: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "total_tokens": 0}
    turn_tool_calls: list[str] = []

    from pipeline import bind_session, get_state, reset_session

    session_token = bind_session(session_id)
    try:
        if config.enable_memory and config.api_key:
            yield {"type": "phase", "phase": "memory", "message": "检索长期记忆…"}
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
                else:
                    yield {"type": "phase", "phase": "memory", "message": "暂无相关记忆"}
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

        yield {"type": "phase", "phase": "think", "message": "开始处理…"}

        for round_idx in range(max_rounds):
            yield {
                "type": "phase",
                "phase": "think",
                "message": f"调用模型（第 {round_idx + 1} 轮）…",
            }
            assistant: dict[str, Any] | None = None
            saw_delta = False
            _upsert_pipeline_progress(messages, get_state(session_id))
            yield {"type": "pipeline", "state": get_state(session_id).to_public_dict()}
            async for event in iter_model_events(config, messages):
                if event.get("type") == "delta":
                    piece = event.get("content") or ""
                    if piece:
                        saw_delta = True
                        yield {"type": "delta", "content": piece}
                elif event.get("type") == "message":
                    assistant = event["message"]
            if assistant is None:
                raise LLMError("模型未返回消息")

            tool_calls = assistant.get("tool_calls") or []
            content = assistant.get("content")
            web_search_queries = assistant.pop("_web_search_queries", None)
            round_usage = assistant.pop("_usage", None)
            if isinstance(round_usage, dict):
                accumulate_usage(turn_usage, round_usage)
            if web_search_queries is not None:
                yield {
                    "type": "phase",
                    "phase": "think",
                    "message": f"联网搜索次数：{web_search_queries}",
                }

            local_tool_calls = [
                call
                for call in tool_calls
                if (call.get("function", {}) or {}).get("name") in LOCAL_TOOL_NAMES
            ]

            if local_tool_calls and config.enable_tools:
                if saw_delta:
                    yield {"type": "round_reset", "reason": "tool_calls"}
                messages.append(assistant)
                for call in local_tool_calls:
                    name = call.get("function", {}).get("name") or ""
                    raw_args = call.get("function", {}).get("arguments") or "{}"
                    turn_tool_calls.append(name)
                    yield {
                        "type": "tool_call",
                        "name": name,
                        "arguments": raw_args,
                    }
                    # 工具里有会阻塞的同步 I/O（比如 test_agent_channel 的 httpx.Client 请求、
                    # 文件读写），放线程池里跑，避免卡住整个事件循环，影响其它并发请求。
                    result = await asyncio.to_thread(run_tool, name, raw_args)
                    yield {
                        "type": "tool_result",
                        "name": name,
                        "result": result[:4000],
                    }
                    if name == "prepare_agent_install":
                        try:
                            offer = json.loads(result)
                        except json.JSONDecodeError:
                            offer = None
                        if isinstance(offer, dict) and offer.get("offer_install"):
                            yield {
                                "type": "install_offer",
                                "agent_id": offer.get("agent_id"),
                                "name": offer.get("name"),
                                "button_label": offer.get("button_label") or f"安装 {offer.get('name')}",
                                "os": offer.get("os"),
                                "package_name": offer.get("package_name"),
                                "auto_installable": bool(offer.get("auto_installable")),
                                "commands": offer.get("commands") or [],
                                "docs_url": offer.get("docs_url") or "",
                                "download_url": offer.get("download_url") or "",
                                "notes": offer.get("notes") or "",
                                "search_notes": offer.get("search_notes") or "",
                                "manual_hint": offer.get("manual_hint"),
                            }
                    if name == "write_agent_channel_config":
                        try:
                            preview = json.loads(result)
                        except json.JSONDecodeError:
                            preview = None
                        if isinstance(preview, dict) and preview.get("ok") and preview.get("confirm_required"):
                            # 真正的 api_key 不从工具返回值里取（那里只有脱敏值，会话记录/
                            # 调试面板都不该看到明文），而是直接读模型这次调用时传入的原始参数——
                            # 这些参数本来就来自用户在对话里输入的内容，没有引入新的暴露面。
                            try:
                                call_args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
                            except json.JSONDecodeError:
                                call_args = {}
                            yield {
                                "type": "write_offer",
                                "agent_id": preview.get("agent_id"),
                                "name": preview.get("name"),
                                "target_path": preview.get("target_path"),
                                "file_exists": preview.get("file_exists"),
                                "will_create_new_file": preview.get("will_create_new_file"),
                                "native_protocol": preview.get("native_protocol"),
                                "api_key_masked": preview.get("api_key_masked"),
                                "message": preview.get("message"),
                                "params": {
                                    "agent_id": call_args.get("agent_id") or preview.get("agent_id"),
                                    "base_url": call_args.get("base_url"),
                                    "model": call_args.get("model"),
                                    "api_key": call_args.get("api_key"),
                                    "protocol": call_args.get("protocol"),
                                    "create_if_missing": call_args.get("create_if_missing", True),
                                },
                            }
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.get("id"),
                            "content": result,
                        }
                    )
                yield {"type": "pipeline", "state": get_state(session_id).to_public_dict()}
                continue

            final_text = content or ""
            # 非流式协议可能只伪流式推过 delta；若完全没推过则补一次
            if not saw_delta and final_text:
                yield {"type": "phase", "phase": "answer", "message": "生成回复…"}
                step = 32
                for i in range(0, len(final_text), step):
                    yield {"type": "delta", "content": final_text[i : i + step]}
            body, refs = _split_answer_and_refs(final_text)
            yield {
                "type": "final",
                "content": body,
                "raw": final_text,
                "references": refs,
                "usage": turn_usage,
                "tool_calls": turn_tool_calls,
            }

            # 短期记忆：不论是否启用长期记忆都落盘，因为它是"这轮到底花了多少 token"的
            # 唯一真实来源，关掉长期记忆不该连用量统计一起丢掉。
            if session_id and final_text.strip():
                try:
                    await asyncio.to_thread(
                        record_session_turn,
                        config,
                        session_id,
                        user_message,
                        final_text,
                        turn_usage,
                        turn_tool_calls,
                        round_idx + 1,
                    )
                except Exception as exc:  # noqa: BLE001
                    yield {
                        "type": "memory",
                        "action": "session_write_error",
                        "message": f"会话记录写入失败（不影响本次回答）：{exc}",
                    }

            if config.enable_memory and config.api_key and final_text.strip():
                yield {"type": "phase", "phase": "memory", "message": "写入长期记忆…"}
                try:
                    result = await asyncio.to_thread(
                        add_memory,
                        config,
                        [
                            {"role": "user", "content": user_message},
                            {"role": "assistant", "content": final_text},
                        ],
                        session_id,
                    )
                    yield {
                        "type": "memory",
                        "action": "write",
                        "ok": True,
                        "backend": (result or {}).get("backend", "local"),
                    }
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
            "raw": "",
            "references": [],
        }


    finally:
        reset_session(session_token)

async def probe_connection(config: AgentConfig) -> dict[str, Any]:
    probe_messages = [
        {"role": "system", "content": "Reply with exactly: ok"},
        {"role": "user", "content": "ping"},
    ]
    # 探测时关闭工具，避免上游因 tools 字段失败
    probe_config = config.model_copy(
        update={
            "enable_tools": False,
            "enable_web_search": False,
            "max_tokens": min(64, config.max_tokens),
        }
    )
    message = await call_model(probe_config, probe_messages)
    return {
        "ok": True,
        "protocol": config.protocol,
        "model": config.model,
        "sample": (message.get("content") or "")[:200],
    }
