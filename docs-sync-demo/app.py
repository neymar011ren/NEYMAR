"""Web UI for the llms.txt docs sync demo."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from sync_docs import (
    ROOT,
    list_pages,
    load_config,
    load_state,
    output_dir_from_config,
    read_page,
    run_sync,
    source_status,
    state_path,
)

STATIC_DIR = ROOT / "static"

app = FastAPI(title="Docs Sync Demo", version="0.2.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@dataclass
class JobState:
    running: bool = False
    watch: bool = False
    cancel: bool = False
    started_at: str | None = None
    finished_at: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    summaries: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    subscribers: list[asyncio.Queue] = field(default_factory=list)
    task: asyncio.Task | None = None


job = JobState()


class SyncRequest(BaseModel):
    sources: list[str] = Field(default_factory=lambda: ["claude"])
    max_pages: int | None = 5
    concurrency: int | None = None
    delay_sec: float | None = None
    dry_run: bool = False
    watch: bool = False
    interval_sec: float = 60


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def push_event(event: dict[str, Any]) -> None:
    event = {**event, "ts": now_iso()}
    job.events.append(event)
    job.events = job.events[-300:]
    for q in list(job.subscribers):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass


async def progress_cb(event: dict[str, Any]) -> None:
    push_event(event)


async def run_job(req: SyncRequest) -> None:
    job.running = True
    job.watch = req.watch
    job.cancel = False
    job.error = None
    job.summaries = []
    job.started_at = now_iso()
    job.finished_at = None
    job.params = req.model_dump()
    push_event({"type": "job_start", "message": "Job started", "params": job.params})

    try:
        while True:
            if job.cancel:
                push_event({"type": "job_cancelled", "message": "Job cancelled"})
                break
            summaries = await run_sync(
                sources=req.sources if req.sources != ["all"] else "all",
                max_pages=req.max_pages,
                concurrency=req.concurrency,
                delay_sec=req.delay_sec,
                dry_run=req.dry_run,
                progress=progress_cb,
            )
            job.summaries = [s.to_dict() for s in summaries]
            if not req.watch or job.cancel:
                break
            push_event(
                {
                    "type": "watch_sleep",
                    "message": f"Watching — next sync in {req.interval_sec:.0f}s",
                    "interval_sec": req.interval_sec,
                }
            )
            slept = 0.0
            while slept < req.interval_sec:
                if job.cancel:
                    break
                await asyncio.sleep(min(1.0, req.interval_sec - slept))
                slept += 1.0
            if job.cancel:
                push_event({"type": "job_cancelled", "message": "Watch stopped"})
                break
    except Exception as exc:  # noqa: BLE001
        job.error = str(exc)
        push_event({"type": "job_error", "message": str(exc)})
    finally:
        job.running = False
        job.watch = False
        job.finished_at = now_iso()
        job.task = None
        push_event({"type": "job_done", "message": "Job finished", "error": job.error})


@app.get("/", response_class=HTMLResponse)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/sources")
async def api_sources() -> dict[str, Any]:
    config = load_config()
    out = output_dir_from_config(config)
    items = [source_status(out, key, cfg) for key, cfg in config["sources"].items()]
    return {"sources": items, "defaults": {
        "concurrency": config.get("concurrency", 4),
        "delay_sec": config.get("delay_sec", 0.2),
        "output_dir": str(out),
    }}


@app.get("/api/status")
async def api_status() -> dict[str, Any]:
    return {
        "running": job.running,
        "watch": job.watch,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "params": job.params,
        "summaries": job.summaries,
        "error": job.error,
        "events": job.events[-80:],
    }


@app.post("/api/sync")
async def api_sync(req: SyncRequest) -> dict[str, Any]:
    if job.running:
        raise HTTPException(status_code=409, detail="A sync job is already running")
    if not req.sources:
        raise HTTPException(status_code=400, detail="Select at least one source")
    config = load_config()
    if "all" in req.sources:
        req.sources = ["all"]
    else:
        unknown = [s for s in req.sources if s not in config["sources"]]
        if unknown:
            raise HTTPException(status_code=400, detail=f"Unknown sources: {unknown}")
    job.events = []
    job.task = asyncio.create_task(run_job(req))
    return {"ok": True, "message": "Sync started"}


@app.post("/api/stop")
async def api_stop() -> dict[str, Any]:
    if not job.running:
        return {"ok": True, "message": "No running job"}
    job.cancel = True
    push_event({"type": "cancel_requested", "message": "Cancel requested…"})
    return {"ok": True, "message": "Cancel requested"}


@app.get("/api/events")
async def api_events() -> Any:
    from fastapi.responses import StreamingResponse

    queue: asyncio.Queue = asyncio.Queue(maxsize=200)
    job.subscribers.append(queue)

    async def gen():
        try:
            # snapshot first
            yield f"data: {json.dumps({'type': 'hello', 'message': 'connected', 'ts': now_iso()}, ensure_ascii=False)}\n\n"
            while True:
                event = await queue.get()
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                if event.get("type") in {"job_done", "job_cancelled"} and not job.running:
                    # keep stream open for next jobs; do not break
                    pass
        except asyncio.CancelledError:
            pass
        finally:
            if queue in job.subscribers:
                job.subscribers.remove(queue)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/api/pages")
async def api_pages(source: str = Query(...), limit: int = Query(100, ge=1, le=1000)) -> dict[str, Any]:
    config = load_config()
    if source not in config["sources"]:
        raise HTTPException(status_code=404, detail="Unknown source")
    out = output_dir_from_config(config)
    state = load_state(state_path(out, source))
    return {
        "source": source,
        "updated_at": state.get("updated_at"),
        "pages": list_pages(out, source, limit=limit),
    }


@app.get("/api/page")
async def api_page(source: str = Query(...), path: str = Query(...)) -> dict[str, Any]:
    config = load_config()
    if source not in config["sources"]:
        raise HTTPException(status_code=404, detail="Unknown source")
    out = output_dir_from_config(config)
    try:
        content = read_page(out, source, path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Page not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"source": source, "path": path, "content": content}
