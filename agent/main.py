from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agent_config_tools import execute_write_agent_channel_config
from agent_install import check_agent_installed, prepare_agent_install, run_agent_install
from config_store import AgentConfig, load_config, public_config, save_config
from llm import LLMError, probe_connection, run_agent
from memory_service import (
    clear_memories,
    clear_sessions,
    get_session_turns,
    invalidate_memory_client,
    list_memories,
    list_sessions,
)
from scan_service import REPORTS_DIR, render_report_markdown, save_report, scan_all_agents_async

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"

app = FastAPI(title="KingSwitch", version="0.2.0")
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
    enable_auto_scan: bool | None = None


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    history: list[dict[str, Any]] = Field(default_factory=list)
    session_id: str | None = None


class InstallRequest(BaseModel):
    agent_id: str = Field(min_length=1)
    session_id: str | None = None


class WriteChannelRequest(BaseModel):
    agent_id: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    model: str = Field(min_length=1)
    api_key: str = Field(min_length=1)
    protocol: str | None = None
    create_if_missing: bool = True
    session_id: str | None = None


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/agents/{agent_id}/install-info")
async def agent_install_info(agent_id: str) -> dict[str, Any]:
    status = check_agent_installed(agent_id)
    if not status.get("ok"):
        raise HTTPException(status_code=404, detail=status.get("error") or "unknown agent")
    offer = prepare_agent_install(agent_id)
    return {"status": status, "offer": offer}


@app.post("/api/agents/install")
async def install_agent(req: InstallRequest) -> StreamingResponse:
    agent_id = req.agent_id.strip()

    async def event_stream():
        try:
            from pipeline import mark_installed

            for event in run_agent_install(agent_id):
                if isinstance(event, dict) and event.get("type") == "done" and event.get("ok"):
                    pipe = mark_installed(agent_id, session_id=getattr(req, "session_id", None))
                    event = {**event, "pipeline": pipe.to_public_dict()}
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception as exc:  # noqa: BLE001
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)}, ensure_ascii=False)}\n\n"
        yield "data: {\"type\": \"finished\"}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/api/agents/write-config")
async def write_agent_config(req: WriteChannelRequest) -> dict[str, Any]:
    """真正把渠道配置写入目标 Agent 的本地文件。

    这是唯一真正执行写入的入口，只能由前端"确认写入"卡片上的按钮触发
    （用户显式点击之后）。聊天里的 write_agent_channel_config 工具只生成预览，
    模型没有办法绕开这个接口直接改动用户的真实配置文件。
    """
    result = await asyncio.to_thread(
        execute_write_agent_channel_config,
        req.agent_id.strip(),
        req.base_url,
        req.model,
        req.api_key,
        protocol=req.protocol,
        create_if_missing=req.create_if_missing,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "写入失败")
    from pipeline import mark_written

    state = mark_written(
        req.agent_id.strip(),
        session_id=req.session_id,
        base_url=req.base_url,
        model=req.model,
    )
    result = {**result, "pipeline": state.to_public_dict()}
    return result


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


@app.get("/api/sessions")
async def get_sessions(limit: int = 30) -> dict[str, Any]:
    """短期记忆：按会话聚合的逐轮对话记录 + token 消耗。

    不受 enable_memory 开关影响——那个开关管的是"长期记忆（提炼 + 召回）"，
    而会话记录是用量统计的事实来源，关掉长期记忆不该连账单一起看不到。
    """
    config = load_config()
    try:
        sessions = await asyncio.to_thread(list_sessions, config, max(1, min(limit, 200)))
        totals = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "total_tokens": 0}
        for session in sessions:
            for key in totals:
                totals[key] += int(session["usage"].get(key) or 0)
        return {"ok": True, "user_id": config.memory_user_id, "sessions": sessions, "totals": totals}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/sessions/{session_id}")
async def get_session_detail(session_id: str) -> dict[str, Any]:
    config = load_config()
    try:
        turns = await asyncio.to_thread(get_session_turns, config, session_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not turns:
        raise HTTPException(status_code=404, detail="会话不存在或已被清空")
    return {"ok": True, "session_id": session_id, "turns": turns}


@app.delete("/api/sessions")
async def delete_sessions() -> dict[str, Any]:
    config = load_config()
    try:
        return await asyncio.to_thread(clear_sessions, config)
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


@app.get("/api/scan")
async def scan_agents(tool_call: bool = True, fidelity: bool = True) -> dict[str, Any]:
    """巡检本机已知的所有 Agent 档案：安装状态 + 已配置渠道的连通性/工具调用探测 +
    协议转换保真度。前端在页面加载时自动调用一次；结果同时落一份 Markdown 报告到
    data/reports/，供用户下载留存。"""
    result = await scan_all_agents_async(include_tool_call=tool_call, include_fidelity=fidelity)
    markdown = render_report_markdown(result)
    report_path = await asyncio.to_thread(save_report, markdown)
    return {**result, "report_file": report_path.name}


@app.get("/api/scan/report/{filename}")
async def download_scan_report(filename: str) -> FileResponse:
    safe_name = Path(filename).name  # 防止路径穿越，只允许访问 REPORTS_DIR 下的同名文件
    path = REPORTS_DIR / safe_name
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="报告不存在")
    return FileResponse(path, media_type="text/markdown; charset=utf-8", filename=safe_name)


@app.post("/api/chat")
async def chat(req: ChatRequest) -> StreamingResponse:
    config = load_config()

    async def event_stream():
        try:
            async for event in run_agent(config, req.message, req.history, session_id=req.session_id):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except LLMError as exc:
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)}, ensure_ascii=False)}\n\n"
        except Exception as exc:  # noqa: BLE001
            yield f"data: {json.dumps({'type': 'error', 'message': f'内部错误: {exc}'}, ensure_ascii=False)}\n\n"
        yield "data: {\"type\": \"done\"}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
