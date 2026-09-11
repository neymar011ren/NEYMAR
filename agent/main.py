from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from config_store import AgentConfig, load_config, public_config, save_config
from llm import LLMError, probe_connection, run_agent
from memory_service import clear_memories, invalidate_memory_client, list_memories

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"

app = FastAPI(title="Configurable Local Agent", version="0.1.0")
app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")


class ConfigUpdate(BaseModel):
    api_base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    protocol: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    system_prompt: str | None = None
    enable_tools: bool | None = None
    enable_web_search: bool | None = None
    request_timeout_seconds: int | None = None
    enable_memory: bool | None = None
    memory_user_id: str | None = None
    memory_embed_base_url: str | None = None
    memory_embed_api_key: str | None = None
    memory_embed_model: str | None = None
    memory_llm_model: str | None = None
    memory_top_k: int | None = None
    memory_embed_dims: int | None = None


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    history: list[dict[str, Any]] = Field(default_factory=list)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/config")
async def get_config() -> dict[str, Any]:
    return public_config()


@app.put("/api/config")
async def update_config(payload: ConfigUpdate) -> dict[str, Any]:
    current = load_config()
    data = current.model_dump()
    updates = payload.model_dump(exclude_unset=True)

    # 空字符串表示保持原 API Key 不变
    if "api_key" in updates and (updates["api_key"] is None or updates["api_key"] == ""):
        updates.pop("api_key")
    if "memory_embed_api_key" in updates and (
        updates["memory_embed_api_key"] is None or updates["memory_embed_api_key"] == ""
    ):
        updates.pop("memory_embed_api_key")

    data.update(updates)
    try:
        saved = save_config(AgentConfig.model_validate(data))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    invalidate_memory_client()
    return public_config(saved)


@app.get("/api/memory")
async def get_memories(limit: int = 20) -> dict[str, Any]:
    config = load_config()
    if not config.enable_memory:
        raise HTTPException(status_code=400, detail="记忆系统未启用")
    try:
        items = list_memories(config, limit=max(1, min(limit, 100)))
        return {"ok": True, "user_id": config.memory_user_id, "items": items}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/memory")
async def delete_memories() -> dict[str, Any]:
    config = load_config()
    if not config.enable_memory:
        raise HTTPException(status_code=400, detail="记忆系统未启用")
    try:
        result = clear_memories(config)
        return result
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/config/test")
async def test_config() -> dict[str, Any]:
    config = load_config()
    try:
        return await probe_connection(config)
    except LLMError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"连接失败: {exc}") from exc


@app.post("/api/chat")
async def chat(req: ChatRequest) -> StreamingResponse:
    config = load_config()

    async def event_stream():
        try:
            async for event in run_agent(config, req.message, req.history):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except LLMError as exc:
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)}, ensure_ascii=False)}\n\n"
        except Exception as exc:  # noqa: BLE001
            yield f"data: {json.dumps({'type': 'error', 'message': f'内部错误: {exc}'}, ensure_ascii=False)}\n\n"
        yield "data: {\"type\": \"done\"}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
