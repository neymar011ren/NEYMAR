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

from config_store import AgentConfig, DATA_DIR

MEMORY_DIR = DATA_DIR / "memory"
LOCAL_STORE = MEMORY_DIR / "local_memories.jsonl"
HISTORY_DB = MEMORY_DIR / "history.db"
QDRANT_PATH = MEMORY_DIR / "qdrant"

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
    scored.sort(key=lambda x: (-x[0], x[1].get("created_at", "")), reverse=False)
    scored.sort(key=lambda x: (-x[0], str(x[1].get("created_at", ""))))
    out = []
    for score, row in scored[:top_k]:
        out.append({"id": row.get("id"), "memory": row.get("memory"), "score": round(score, 4), "backend": "local"})
    return out


def local_add(config: AgentConfig, messages: list[dict[str, str]]) -> dict[str, Any]:
    chunks = []
    for msg in messages:
        role = msg.get("role")
        content = (msg.get("content") or "").strip()
        if role in {"user", "assistant"} and content:
            chunks.append(f"{role}: {content[:800]}")
    if not chunks:
        return {"ok": False, "reason": "empty", "backend": "local"}
    text = " | ".join(chunks)
    item = {
        "id": str(uuid.uuid4()),
        "user_id": config.memory_user_id,
        "memory": text[:2000],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _append_local(item)
    return {"ok": True, "backend": "local", "id": item["id"]}


def local_list(config: AgentConfig, limit: int = 20) -> list[dict[str, Any]]:
    rows = _read_local(config.memory_user_id)
    rows.sort(key=lambda r: str(r.get("created_at", "")), reverse=True)
    return [{"id": r.get("id"), "memory": r.get("memory"), "backend": "local"} for r in rows[:limit]]


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
                    "memory": str(text),
                    "score": item.get("score"),
                    "backend": "mem0",
                }
            )
        # 本地在前，远程补齐
        merged = local_hits + [r for r in remote if r.get("memory")]
        return merged[: (limit or config.memory_top_k)]
    except Exception:
        return local_hits


def add_memory(config: AgentConfig, messages: list[dict[str, str]]) -> dict[str, Any]:
    local_result = local_add(config, messages)
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
            remote.append({"id": item.get("id"), "memory": str(text), "backend": "mem0"})
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
