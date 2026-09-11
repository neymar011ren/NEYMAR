from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from config_store import AgentConfig, DATA_DIR

MEMORY_DIR = DATA_DIR / "memory"
HISTORY_DB = MEMORY_DIR / "history.db"
QDRANT_PATH = MEMORY_DIR / "qdrant"

_lock = threading.Lock()
_cached_key: str | None = None
_cached_client: Any = None


def openai_compatible_base(url: str) -> str:
    base = (url or "").strip().rstrip("/")
    for suffix in ("/chat/completions", "/responses", "/messages"):
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
    global _cached_key, _cached_client
    with _lock:
        _cached_key = None
        _cached_client = None


def get_memory_client(config: AgentConfig):
    """Lazy-create a Mem0 Memory client bound to the current API settings."""
    global _cached_key, _cached_client
    if not config.enable_memory:
        raise RuntimeError("记忆系统未启用")
    if not config.api_key:
        raise RuntimeError("记忆抽取需要文本模型 API Key")
    if not config.memory_embed_base_url:
        raise RuntimeError("请单独配置向量模型 API Base URL")
    if not config.memory_embed_api_key:
        raise RuntimeError("请单独配置向量模型 API Key")

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
                    "collection_name": "forge_agent",
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
        client = Memory.from_config(mem_config)
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
    client = get_memory_client(config)
    top_k = limit or config.memory_top_k
    raw = client.search(
        query=query,
        top_k=top_k,
        filters={"user_id": config.memory_user_id},
    )
    hits = _normalize_hits(raw)
    cleaned: list[dict[str, Any]] = []
    for item in hits[:top_k]:
        text = item.get("memory") or item.get("text") or item.get("data") or ""
        if isinstance(text, dict):
            text = text.get("memory") or str(text)
        score = item.get("score")
        cleaned.append(
            {
                "id": item.get("id"),
                "memory": str(text),
                "score": score,
            }
        )
    return cleaned


def add_memory(config: AgentConfig, messages: list[dict[str, str]]) -> dict[str, Any]:
    client = get_memory_client(config)
    payload = [{"role": m["role"], "content": m["content"]} for m in messages if m.get("content")]
    if not payload:
        return {"ok": False, "reason": "empty"}
    result = client.add(payload, user_id=config.memory_user_id)
    return {"ok": True, "result": result}


def list_memories(config: AgentConfig, limit: int = 20) -> list[dict[str, Any]]:
    client = get_memory_client(config)
    raw = client.get_all(filters={"user_id": config.memory_user_id}, top_k=limit)
    hits = _normalize_hits(raw)
    out: list[dict[str, Any]] = []
    for item in hits[:limit]:
        text = item.get("memory") or item.get("text") or ""
        out.append({"id": item.get("id"), "memory": str(text)})
    return out


def clear_memories(config: AgentConfig) -> dict[str, Any]:
    client = get_memory_client(config)
    if hasattr(client, "delete_all"):
        client.delete_all(user_id=config.memory_user_id)
        return {"ok": True, "mode": "delete_all"}
    items = list_memories(config, limit=1000)
    deleted = 0
    for item in items:
        mid = item.get("id")
        if mid and hasattr(client, "delete"):
            client.delete(mid)
            deleted += 1
    return {"ok": True, "mode": "fallback", "deleted": deleted}


def format_memory_block(memories: list[dict[str, Any]]) -> str:
    if not memories:
        return ""
    lines = ["以下是与当前用户相关的长期记忆，请在回答时优先参考："]
    for idx, item in enumerate(memories, start=1):
        lines.append(f"{idx}. {item.get('memory', '').strip()}")
    return "\n".join(lines)
