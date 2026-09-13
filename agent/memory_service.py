"""本地长期记忆：不依赖外部 Embed / Qdrant，避免 Connection error 与文件锁冲突。

仍保留可选 Mem0 路径；默认优先本地，Mem0 失败时自动回退。
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config_store import AgentConfig, DATA_DIR, mask_secret

MEMORY_DIR = DATA_DIR / "memory"
LOCAL_STORE = MEMORY_DIR / "local_memories.jsonl"
HISTORY_DB = MEMORY_DIR / "history.db"
QDRANT_PATH = MEMORY_DIR / "qdrant"
CHANNEL_HISTORY_STORE = MEMORY_DIR / "channel_history.jsonl"
# 短期记忆：逐轮对话的原始落盘记录（含 token 消耗与工具调用），
# 长期记忆（LOCAL_STORE / mem0）是从这些原始轮次里提炼出来的沉淀。
SESSION_STORE = MEMORY_DIR / "sessions.jsonl"

_lock = threading.Lock()
_cached_key: str | None = None
_cached_client: Any = None
_mem0_disabled_reason: str | None = None


def openai_compatible_base(url: str) -> str:
    base = (url or "").strip().rstrip("/")
    # 常见笔误：https 少写了 h
    if base.startswith("ttps://"):
        base = "h" + base
    elif base.startswith("ttp://"):
        base = "h" + base
    if base and "://" not in base:
        base = "https://" + base
    for suffix in ("/chat/completions", "/responses", "/messages", "/embeddings"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    return base or "https://api.openai.com/v1"


def _cache_key(config: AgentConfig) -> str:
    return "|".join(
        [
            openai_compatible_base(config.api_base_url),
            config.api_key,
            config.memory_llm_model or config.model,
            openai_compatible_base(config.memory_embed_base_url),
            config.memory_embed_api_key,
            config.memory_embed_model,
            str(config.memory_embed_dims),
            str(QDRANT_PATH),
        ]
    )


def invalidate_memory_client() -> None:
    global _cached_key, _cached_client, _mem0_disabled_reason
    with _lock:
        _cached_key = None
        _cached_client = None
        _mem0_disabled_reason = None


def _tokenize(text: str) -> set[str]:
    parts = re.findall(r"[\w\u4e00-\u9fff]+", (text or "").lower())
    return {p for p in parts if len(p) > 1}


# \u804a\u5929\u5185\u5bb9\u672c\u8eab\u53ef\u80fd\u5305\u542b\u7528\u6237\u8d34\u7684 curl \u547d\u4ee4\u3001\u65e5\u5fd7\u7247\u6bb5\u7b49\uff0c\u91cc\u9762\u53ef\u80fd\u5e26\u7740\u771f\u5b9e\u5bc6\u94a5\u3002
# agent_config_tools.py \u91cc\u7684 _redact_obj/_redact_text \u53ea\u5904\u7406"\u6e20\u9053\u914d\u7f6e\u6587\u4ef6"\u8fd9\u79cd\u7ed3\u6784\u5316/
# \u534a\u7ed3\u6784\u5316\u6570\u636e\uff0c\u7ba1\u4e0d\u5230\u81ea\u7531\u6587\u672c\u804a\u5929\u8bb0\u5f55\u2014\u2014\u8fd9\u91cc\u5355\u72ec\u505a\u4e00\u5c42\u901a\u7528\u7684\u5bc6\u94a5\u6a21\u5f0f\u8131\u654f\uff0c
# \u5199\u5165\u8bb0\u5fc6\u524d\u3001\u4ee5\u53ca\u6bcf\u6b21\u4ece\u8bb0\u5fc6\u8bfb\u51fa\u6765\u5c55\u793a/\u53ec\u56de\u65f6\u90fd\u4f1a\u8fc7\u4e00\u904d\uff0c\u53cc\u91cd\u4fdd\u9669\u3002
_SECRET_RE = re.compile(
    r"""(?ix)
    (?:
        (?P<bearer_prefix>bearer\s+)(?P<bearer_val>[a-z0-9._\-]{8,})
      | (?P<kv_prefix>(?:api[_-]?key|apikey|access[_-]?token|auth[_-]?token|secret[_-]?key)\s*[:=]\s*[\"']?)(?P<kv_val>[a-z0-9._\-]{8,})
      | (?P<bare_val>sk-[a-z0-9_\-]{6,})
    )
    """
)


def redact_secrets(text: str) -> str:
    if not text:
        return text

    def _mask(match: "re.Match[str]") -> str:
        bearer_val = match.group("bearer_val")
        if bearer_val:
            return f"{match.group('bearer_prefix')}{mask_secret(bearer_val)}"
        kv_val = match.group("kv_val")
        if kv_val:
            return f"{match.group('kv_prefix')}{mask_secret(kv_val)}"
        bare_val = match.group("bare_val")
        if bare_val:
            return mask_secret(bare_val)
        return match.group(0)

    return _SECRET_RE.sub(_mask, text)


def _read_local(user_id: str) -> list[dict[str, Any]]:
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    if not LOCAL_STORE.exists():
        return []
    rows: list[dict[str, Any]] = []
    with LOCAL_STORE.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if item.get("user_id") == user_id and not item.get("deleted"):
                rows.append(item)
    return rows


def _append_local(item: dict[str, Any]) -> None:
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    with _lock:
        with LOCAL_STORE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")


def local_search(config: AgentConfig, query: str, limit: int | None = None) -> list[dict[str, Any]]:
    top_k = limit or config.memory_top_k
    rows = _read_local(config.memory_user_id)
    q_tokens = _tokenize(query)
    scored: list[tuple[float, dict[str, Any]]] = []
    for row in rows:
        text = str(row.get("memory") or "")
        tokens = _tokenize(text)
        if not tokens:
            continue
        overlap = len(q_tokens & tokens)
        # 无交集时给极低分，保证“最近记忆”仍可召回
        score = overlap / max(1, len(q_tokens)) if q_tokens else 0.0
        if overlap == 0:
            score = 0.05
        scored.append((score, row))
    scored.sort(key=lambda x: (-x[0], str(x[1].get("created_at", ""))))
    out = []
    for score, row in scored[:top_k]:
        out.append(
            {
                "id": row.get("id"),
                "memory": redact_secrets(row.get("memory") or ""),
                "score": round(score, 4),
                "backend": "local",
            }
        )
    return out


def local_add(
    config: AgentConfig,
    messages: list[dict[str, str]],
    session_id: str | None = None,
) -> dict[str, Any]:
    chunks = []
    for msg in messages:
        role = msg.get("role")
        content = (msg.get("content") or "").strip()
        if role in {"user", "assistant"} and content:
            chunks.append(f"{role}: {redact_secrets(content)[:800]}")
    if not chunks:
        return {"ok": False, "reason": "empty", "backend": "local"}
    text = " | ".join(chunks)
    item = {
        "id": str(uuid.uuid4()),
        "user_id": config.memory_user_id,
        "memory": text[:2000],
        "created_at": datetime.now(timezone.utc).isoformat(),
        # 记下这条长期记忆是从哪个会话沉淀出来的，前端才能做"点击跳转回原会话"
        "session_id": session_id or "",
    }
    _append_local(item)
    return {"ok": True, "backend": "local", "id": item["id"]}


def local_list(config: AgentConfig, limit: int = 20) -> list[dict[str, Any]]:
    rows = _read_local(config.memory_user_id)
    rows.sort(key=lambda r: str(r.get("created_at", "")), reverse=True)
    return [
        {
            "id": r.get("id"),
            "memory": redact_secrets(r.get("memory") or ""),
            "backend": "local",
            "created_at": r.get("created_at", ""),
            "session_id": r.get("session_id", ""),
        }
        for r in rows[:limit]
    ]


def local_clear(config: AgentConfig) -> dict[str, Any]:
    rows = _read_local(config.memory_user_id)
    # 重写文件，去掉该用户
    with _lock:
        kept: list[str] = []
        if LOCAL_STORE.exists():
            for line in LOCAL_STORE.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if item.get("user_id") != config.memory_user_id:
                    kept.append(json.dumps(item, ensure_ascii=False))
        LOCAL_STORE.write_text(("\n".join(kept) + ("\n" if kept else "")), encoding="utf-8")
    return {"ok": True, "backend": "local", "cleared": len(rows)}


def get_memory_client(config: AgentConfig):
    """Lazy-create Mem0 client；失败时抛错，由上层回退本地。"""
    global _cached_key, _cached_client, _mem0_disabled_reason
    if not config.enable_memory:
        raise RuntimeError("记忆系统未启用")
    if not config.api_key:
        raise RuntimeError("记忆抽取需要文本模型 API Key")
    if not config.memory_embed_base_url:
        raise RuntimeError("请单独配置向量模型 API Base URL")
    if not config.memory_embed_api_key:
        raise RuntimeError("请单独配置向量模型 API Key")
    if _mem0_disabled_reason:
        raise RuntimeError(_mem0_disabled_reason)

    key = _cache_key(config)
    with _lock:
        if _cached_client is not None and _cached_key == key:
            return _cached_client

        MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        from mem0 import Memory

        text_base = openai_compatible_base(config.api_base_url)
        embed_base = openai_compatible_base(config.memory_embed_base_url)
        llm_model = config.memory_llm_model or config.model
        mem_config = {
            "vector_store": {
                "provider": "qdrant",
                "config": {
                    "collection_name": "kingswitch",
                    "path": str(QDRANT_PATH),
                    "embedding_model_dims": config.memory_embed_dims,
                },
            },
            "llm": {
                "provider": "openai",
                "config": {
                    "model": llm_model,
                    "api_key": config.api_key,
                    "openai_base_url": text_base,
                    "temperature": 0.1,
                    "max_tokens": 1024,
                },
            },
            "embedder": {
                "provider": "openai",
                "config": {
                    "model": config.memory_embed_model,
                    "api_key": config.memory_embed_api_key,
                    "openai_base_url": embed_base,
                },
            },
            "history_db_path": str(HISTORY_DB),
        }
        try:
            client = Memory.from_config(mem_config)
        except Exception as exc:  # noqa: BLE001
            _mem0_disabled_reason = f"Mem0/Qdrant 不可用，已改用本地记忆：{exc}"
            raise
        _cached_client = client
        _cached_key = key
        return client


def _normalize_hits(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if isinstance(raw, dict):
        results = raw.get("results")
        if isinstance(results, list):
            return [item for item in results if isinstance(item, dict)]
        return []
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    return []


def search_memories(config: AgentConfig, query: str, limit: int | None = None) -> list[dict[str, Any]]:
    # 优先本地（稳定）；若用户配置了 embed 且 Mem0 可用，合并结果
    local_hits = local_search(config, query, limit=limit)
    try:
        client = get_memory_client(config)
        top_k = limit or config.memory_top_k
        raw = client.search(
            query=query,
            top_k=top_k,
            filters={"user_id": config.memory_user_id},
        )
        hits = _normalize_hits(raw)
        remote: list[dict[str, Any]] = []
        for item in hits[:top_k]:
            text = item.get("memory") or item.get("text") or item.get("data") or ""
            if isinstance(text, dict):
                text = text.get("memory") or str(text)
            remote.append(
                {
                    "id": item.get("id"),
                    "memory": redact_secrets(str(text)),
                    "score": item.get("score"),
                    "backend": "mem0",
                }
            )
        # 本地在前，远程补齐
        merged = local_hits + [r for r in remote if r.get("memory")]
        return merged[: (limit or config.memory_top_k)]
    except Exception:
        return local_hits


def add_memory(
    config: AgentConfig,
    messages: list[dict[str, str]],
    session_id: str | None = None,
) -> dict[str, Any]:
    local_result = local_add(config, messages, session_id=session_id)
    try:
        client = get_memory_client(config)
        payload = [{"role": m["role"], "content": m["content"]} for m in messages if m.get("content")]
        if not payload:
            return local_result
        result = client.add(payload, user_id=config.memory_user_id)
        return {"ok": True, "backend": "local+mem0", "local": local_result, "mem0": result}
    except Exception as exc:  # noqa: BLE001
        # 本地已成功则视为成功
        local_result["mem0_skipped"] = str(exc)
        return local_result


def list_memories(config: AgentConfig, limit: int = 20) -> list[dict[str, Any]]:
    local_rows = local_list(config, limit=limit)
    try:
        client = get_memory_client(config)
        raw = client.get_all(filters={"user_id": config.memory_user_id}, top_k=limit)
        hits = _normalize_hits(raw)
        remote = []
        for item in hits[:limit]:
            text = item.get("memory") or item.get("text") or ""
            remote.append({"id": item.get("id"), "memory": redact_secrets(str(text)), "backend": "mem0"})
        return (local_rows + remote)[:limit]
    except Exception:
        return local_rows


def clear_memories(config: AgentConfig) -> dict[str, Any]:
    local_result = local_clear(config)
    try:
        client = get_memory_client(config)
        if hasattr(client, "delete_all"):
            client.delete_all(user_id=config.memory_user_id)
            return {"ok": True, "mode": "local+mem0_delete_all", "local": local_result}
        items = list_memories(config, limit=1000)
        deleted = 0
        for item in items:
            mid = item.get("id")
            if mid and item.get("backend") == "mem0" and hasattr(client, "delete"):
                client.delete(mid)
                deleted += 1
        return {"ok": True, "mode": "local+mem0_fallback", "local": local_result, "deleted": deleted}
    except Exception as exc:  # noqa: BLE001
        local_result["mem0_skipped"] = str(exc)
        return local_result


def format_memory_block(memories: list[dict[str, Any]]) -> str:
    if not memories:
        return ""
    lines = ["以下是与当前用户相关的长期记忆，请在回答时优先参考："]
    for idx, item in enumerate(memories, start=1):
        lines.append(f"{idx}. {item.get('memory', '').strip()}")
    return "\n".join(lines)


# --- 短期记忆：逐轮会话原始记录 + token 消耗 ------------------------------------
#
# 与"长期记忆"（上面 local_* / mem0，是提炼后的沉淀）的区别：
# 这里存的是每一轮问答的原始内容和这一轮真实花掉的 token，不做提炼、不做召回，
# 用于前端记忆库面板展示"这个会话花了多少 token / 调了哪些工具"，
# 以及支撑"点击一条长期记忆 → 跳回它产生时的那个会话"。


def _empty_usage() -> dict[str, int]:
    return {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "total_tokens": 0}


def record_session_turn(
    config: AgentConfig,
    session_id: str,
    user_message: str,
    assistant_message: str,
    usage: dict[str, int] | None = None,
    tool_calls: list[str] | None = None,
    rounds: int = 1,
) -> dict[str, Any]:
    """记录一轮完整问答。内容同样过一遍脱敏，避免用户贴进来的密钥落盘。"""
    if not session_id:
        return {"ok": False, "reason": "missing session_id"}
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    item = {
        "id": str(uuid.uuid4()),
        "session_id": session_id,
        "user_id": config.memory_user_id,
        "ts": datetime.now(timezone.utc).isoformat(),
        "user_message": redact_secrets(user_message or "")[:4000],
        "assistant_message": redact_secrets(assistant_message or "")[:4000],
        "usage": {**_empty_usage(), **(usage or {})},
        "tool_calls": tool_calls or [],
        "rounds": rounds,
    }
    with _lock:
        with SESSION_STORE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")
    return {"ok": True, "id": item["id"]}


def _read_sessions(user_id: str) -> list[dict[str, Any]]:
    if not SESSION_STORE.exists():
        return []
    rows: list[dict[str, Any]] = []
    with SESSION_STORE.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if item.get("user_id") == user_id:
                rows.append(item)
    return rows


def list_sessions(config: AgentConfig, limit: int = 20) -> list[dict[str, Any]]:
    """按会话聚合：每个会话的轮数、起止时间、累计 token、用过的工具、首条用户消息（当标题）。"""
    rows = _read_sessions(config.memory_user_id)
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        sid = row.get("session_id") or ""
        if not sid:
            continue
        bucket = grouped.setdefault(
            sid,
            {
                "session_id": sid,
                "turns": 0,
                "started_at": row.get("ts", ""),
                "last_at": row.get("ts", ""),
                "usage": _empty_usage(),
                "tool_calls": [],
                "title": "",
            },
        )
        bucket["turns"] += 1
        ts = str(row.get("ts", ""))
        if ts and (not bucket["started_at"] or ts < bucket["started_at"]):
            bucket["started_at"] = ts
            bucket["title"] = (row.get("user_message") or "")[:60]
        if ts and ts > bucket["last_at"]:
            bucket["last_at"] = ts
        if not bucket["title"]:
            bucket["title"] = (row.get("user_message") or "")[:60]
        usage = row.get("usage") or {}
        for key in bucket["usage"]:
            bucket["usage"][key] += int(usage.get(key) or 0)
        for name in row.get("tool_calls") or []:
            if name not in bucket["tool_calls"]:
                bucket["tool_calls"].append(name)

    sessions = sorted(grouped.values(), key=lambda s: str(s["last_at"]), reverse=True)
    return sessions[:limit]


def get_session_turns(config: AgentConfig, session_id: str) -> list[dict[str, Any]]:
    rows = [r for r in _read_sessions(config.memory_user_id) if r.get("session_id") == session_id]
    rows.sort(key=lambda r: str(r.get("ts", "")))
    return rows


def clear_sessions(config: AgentConfig) -> dict[str, Any]:
    """只清当前记忆用户的会话记录，其它用户的保留。"""
    rows = _read_sessions(config.memory_user_id)
    with _lock:
        kept: list[str] = []
        if SESSION_STORE.exists():
            for line in SESSION_STORE.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if item.get("user_id") != config.memory_user_id:
                    kept.append(json.dumps(item, ensure_ascii=False))
        SESSION_STORE.write_text(("\n".join(kept) + ("\n" if kept else "")), encoding="utf-8")
    return {"ok": True, "cleared": len(rows)}


# --- 渠道配置历史：结构化记忆，专门服务于 KingSwitch 自己的领域 -------------------
#
# 上面 local_add/local_search 那套是"通用聊天记忆"的思路：自由文本 + 词重叠打分。
# 但 KingSwitch 关心的问题其实很窄——"之前给哪个 Agent 配过哪个渠道"——用结构化记录
# 按 agent_id 精确查找，比指望模糊分词从聊天记录里蒙对更可靠。这里只是现有 local_*
# 记忆之外的一个补充，不影响 mem0 / 通用记忆那条路径。


def record_channel_write(
    agent_id: str,
    base_url: str,
    model: str,
    protocol: str | None,
    target_path: str,
    api_key: str,
) -> None:
    """每次 execute_write_agent_channel_config 成功后调用一次。只存脱敏后的 Key。"""
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    item = {
        "id": str(uuid.uuid4()),
        "ts": datetime.now(timezone.utc).isoformat(),
        "agent_id": agent_id,
        "base_url": base_url,
        "model": model,
        "protocol": protocol,
        "target_path": target_path,
        "api_key_masked": mask_secret(api_key),
    }
    with _lock:
        with CHANNEL_HISTORY_STORE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")


def get_channel_history(agent_id: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
    """按 agent_id 精确过滤（不传则返回所有 Agent 的最近记录），按时间倒序。"""
    if not CHANNEL_HISTORY_STORE.exists():
        return []
    rows: list[dict[str, Any]] = []
    with CHANNEL_HISTORY_STORE.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if agent_id and item.get("agent_id") != agent_id:
                continue
            rows.append(item)
    rows.sort(key=lambda r: str(r.get("ts", "")), reverse=True)
    return rows[:limit]
