#!/usr/bin/env python3
"""Incremental docs sync for sites that publish llms.txt.

Library + CLI. The Web UI imports `run_sync` / `load_config` from here.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import yaml
from rich.console import Console
from rich.table import Table

console = Console()

MARKDOWN_LINK_RE = re.compile(r"\[[^\]]*\]\((https?://[^)\s]+)\)")
ProgressCb = Callable[[dict[str, Any]], Awaitable[None] | None]

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config.yaml"


@dataclass
class PageResult:
    url: str
    status: str  # added | changed | unchanged | deleted | error | skipped
    path: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SyncSummary:
    source: str
    added: int = 0
    changed: int = 0
    unchanged: int = 0
    deleted: int = 0
    errors: int = 0
    skipped: int = 0
    index_urls: int = 0
    pages: list[PageResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "added": self.added,
            "changed": self.changed,
            "unchanged": self.unchanged,
            "deleted": self.deleted,
            "errors": self.errors,
            "skipped": self.skipped,
            "index_urls": self.index_urls,
            "pages": [p.to_dict() for p in self.pages],
        }


async def emit(cb: ProgressCb | None, event: dict[str, Any]) -> None:
    if cb is None:
        return
    result = cb(event)
    if asyncio.iscoroutine(result) or isinstance(result, Awaitable):
        await result  # type: ignore[arg-type]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def resolve_config_path(path: str | Path | None = None) -> Path:
    if path is None:
        return DEFAULT_CONFIG
    p = Path(path)
    if p.exists():
        return p
    alt = ROOT / p.name
    if alt.exists():
        return alt
    raise FileNotFoundError(f"Config not found: {path}")


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    with resolve_config_path(path).open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def output_dir_from_config(config: dict[str, Any], override: str | Path | None = None) -> Path:
    base = Path(override or config.get("output_dir", "./data"))
    if not base.is_absolute():
        base = ROOT / base
    base.mkdir(parents=True, exist_ok=True)
    return base.resolve()


def slugify_url(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.strip("/") or "index"
    if path.endswith(".md.txt"):
        path = path[: -len(".md.txt")] + ".md"
    elif not path.endswith(".md"):
        path = path + ".md"
    return re.sub(r"[^a-zA-Z0-9._/-]+", "_", path)


def extract_urls(llms_text: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for match in MARKDOWN_LINK_RE.finditer(llms_text):
        url = match.group(1).rstrip(").,;")
        if url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def filter_urls(urls: list[str], patterns: list[str] | None) -> list[str]:
    if not patterns:
        return urls
    compiled = [re.compile(p) for p in patterns]
    return [u for u in urls if any(r.search(u) for r in compiled)]


def content_hash(text: str) -> str:
    normalized = text.replace("\r\n", "\n").strip() + "\n"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def state_path(output_dir: Path, source_key: str) -> Path:
    return output_dir / source_key / "state.json"


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"pages": {}, "updated_at": None}
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
        f.write("\n")
    tmp.replace(path)


def source_status(output_dir: Path, source_key: str, source_cfg: dict[str, Any]) -> dict[str, Any]:
    state = load_state(state_path(output_dir, source_key))
    pages = state.get("pages") or {}
    return {
        "key": source_key,
        "name": source_cfg.get("name", source_key),
        "llms_txt": source_cfg["llms_txt"],
        "updated_at": state.get("updated_at"),
        "page_count": len(pages),
        "has_data": bool(pages),
    }


async def fetch_text(
    client: httpx.AsyncClient,
    url: str,
    *,
    etag: str | None = None,
    last_modified: str | None = None,
) -> tuple[int, str | None, dict[str, str]]:
    headers: dict[str, str] = {}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified

    response = await client.get(url, headers=headers)
    meta = {
        "etag": response.headers.get("etag", ""),
        "last_modified": response.headers.get("last-modified", ""),
    }
    if response.status_code == 304:
        return 304, None, meta
    response.raise_for_status()
    return response.status_code, response.text, meta


async def sync_source(
    client: httpx.AsyncClient,
    source_key: str,
    source_cfg: dict[str, Any],
    *,
    output_dir: Path,
    max_pages: int | None,
    delay_sec: float,
    dry_run: bool,
    concurrency: int = 4,
    progress: ProgressCb | None = None,
) -> SyncSummary:
    summary = SyncSummary(source=source_key)
    llms_url = source_cfg["llms_txt"]
    await emit(
        progress,
        {"type": "source_start", "source": source_key, "llms_txt": llms_url, "message": f"Fetching index for {source_key}"},
    )
    console.print(f"\n[bold cyan]→ {source_cfg.get('name', source_key)}[/]  {llms_url}")

    _, llms_body, _ = await fetch_text(client, llms_url)
    assert llms_body is not None
    urls = extract_urls(llms_body)
    urls = filter_urls(urls, source_cfg.get("include_url_regex"))
    summary.index_urls = len(urls)
    console.print(f"  index links after filter: [green]{len(urls)}[/]")
    await emit(
        progress,
        {"type": "index", "source": source_key, "index_urls": len(urls), "message": f"{source_key}: {len(urls)} URLs in index"},
    )

    if max_pages is not None:
        urls = urls[:max_pages]
        console.print(f"  demo cap --max-pages={max_pages}")
        await emit(
            progress,
            {"type": "cap", "source": source_key, "max_pages": max_pages, "message": f"{source_key}: capped to {max_pages} pages"},
        )

    source_dir = output_dir / source_key
    pages_dir = source_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    state_file = state_path(output_dir, source_key)
    state = load_state(state_file)
    old_pages: dict[str, Any] = state.get("pages", {})
    new_pages: dict[str, Any] = {}
    sem = asyncio.Semaphore(concurrency)
    total = len(urls)
    done = 0

    async def process_one(url: str) -> PageResult:
        nonlocal done
        async with sem:
            if delay_sec > 0:
                await asyncio.sleep(delay_sec)
            prev = old_pages.get(url, {})
            rel = slugify_url(url)
            out_path = pages_dir / rel
            try:
                status_code, body, meta = await fetch_text(
                    client,
                    url,
                    etag=prev.get("etag"),
                    last_modified=prev.get("last_modified"),
                )
                if status_code == 304 and prev.get("hash"):
                    new_pages[url] = prev
                    result = PageResult(url=url, status="unchanged", path=str(out_path))
                else:
                    assert body is not None
                    digest = content_hash(body)
                    if prev.get("hash") == digest and out_path.exists():
                        new_pages[url] = {
                            **prev,
                            "etag": meta.get("etag") or prev.get("etag"),
                            "last_modified": meta.get("last_modified") or prev.get("last_modified"),
                            "checked_at": utc_now(),
                        }
                        result = PageResult(url=url, status="unchanged", path=str(out_path))
                    else:
                        status = "changed" if url in old_pages else "added"
                        if not dry_run:
                            out_path.parent.mkdir(parents=True, exist_ok=True)
                            out_path.write_text(body, encoding="utf-8")
                        new_pages[url] = {
                            "path": str(out_path.relative_to(source_dir)),
                            "hash": digest,
                            "etag": meta.get("etag") or None,
                            "last_modified": meta.get("last_modified") or None,
                            "bytes": len(body.encode("utf-8")),
                            "synced_at": utc_now(),
                            "checked_at": utc_now(),
                        }
                        result = PageResult(url=url, status=status, path=str(out_path))
            except Exception as exc:  # noqa: BLE001
                result = PageResult(url=url, status="error", error=str(exc))

            done += 1
            await emit(
                progress,
                {
                    "type": "page",
                    "source": source_key,
                    "url": result.url,
                    "status": result.status,
                    "error": result.error,
                    "done": done,
                    "total": total,
                    "message": f"{source_key} [{done}/{total}] {result.status}: {result.url}",
                },
            )
            return result

    results = await asyncio.gather(*(process_one(u) for u in urls))
    for result in results:
        summary.pages.append(result)
        if result.status == "added":
            summary.added += 1
        elif result.status == "changed":
            summary.changed += 1
        elif result.status == "unchanged":
            summary.unchanged += 1
        elif result.status == "error":
            summary.errors += 1
        else:
            summary.skipped += 1

    if max_pages is None:
        indexed = set(urls)
        for url, meta in old_pages.items():
            if url in indexed:
                continue
            summary.deleted += 1
            deleted = PageResult(url=url, status="deleted", path=meta.get("path"))
            summary.pages.append(deleted)
            await emit(
                progress,
                {
                    "type": "page",
                    "source": source_key,
                    "url": url,
                    "status": "deleted",
                    "message": f"{source_key} deleted: {url}",
                },
            )
            if not dry_run:
                rel = meta.get("path")
                if rel:
                    victim = source_dir / rel
                    if victim.exists():
                        victim.unlink()
        final_pages = new_pages
    else:
        final_pages = {**old_pages, **new_pages}

    if not dry_run:
        save_state(
            state_file,
            {
                "source": source_key,
                "llms_txt": llms_url,
                "updated_at": utc_now(),
                "page_count": len(final_pages),
                "pages": final_pages,
            },
        )
        (source_dir / "llms.txt").write_text(llms_body, encoding="utf-8")

    await emit(
        progress,
        {
            "type": "source_done",
            "source": source_key,
            "summary": summary.to_dict(),
            "message": (
                f"{source_key} done — +{summary.added} ~{summary.changed} "
                f"={summary.unchanged} -{summary.deleted} !{summary.errors}"
            ),
        },
    )
    return summary


async def run_sync(
    *,
    sources: list[str] | str = "all",
    config: dict[str, Any] | None = None,
    config_path: str | Path | None = None,
    output_dir: str | Path | None = None,
    max_pages: int | None = None,
    concurrency: int | None = None,
    delay_sec: float | None = None,
    dry_run: bool = False,
    progress: ProgressCb | None = None,
) -> list[SyncSummary]:
    cfg = config or load_config(config_path)
    sources_cfg: dict[str, Any] = cfg["sources"]
    if sources == "all" or sources == ["all"]:
        selected = list(sources_cfg.keys())
    elif isinstance(sources, str):
        selected = [sources]
    else:
        selected = list(sources)

    for key in selected:
        if key not in sources_cfg:
            raise ValueError(f"Unknown source '{key}'. Choose from: {', '.join(sources_cfg)}|all")

    out = output_dir_from_config(cfg, output_dir)
    headers = {"User-Agent": cfg.get("user_agent", "docs-sync-demo/0.1")}
    timeout = httpx.Timeout(cfg.get("request_timeout_sec", 30.0))
    conc = int(concurrency if concurrency is not None else cfg.get("concurrency", 4))
    delay = float(delay_sec if delay_sec is not None else cfg.get("delay_sec", 0.2))

    await emit(progress, {"type": "run_start", "sources": selected, "message": f"Starting sync: {', '.join(selected)}"})

    async with httpx.AsyncClient(headers=headers, timeout=timeout, follow_redirects=True) as client:
        summaries: list[SyncSummary] = []
        for key in selected:
            summary = await sync_source(
                client,
                key,
                sources_cfg[key],
                output_dir=out,
                max_pages=max_pages,
                delay_sec=delay,
                dry_run=dry_run,
                concurrency=conc,
                progress=progress,
            )
            summaries.append(summary)

    await emit(
        progress,
        {
            "type": "run_done",
            "summaries": [s.to_dict() for s in summaries],
            "message": "Sync finished",
        },
    )
    return summaries


def list_pages(output_dir: Path, source_key: str, *, limit: int = 200, status_filter: str | None = None) -> list[dict[str, Any]]:
    state = load_state(state_path(output_dir, source_key))
    pages = state.get("pages") or {}
    rows: list[dict[str, Any]] = []
    for url, meta in pages.items():
        rows.append(
            {
                "url": url,
                "path": meta.get("path"),
                "hash": meta.get("hash"),
                "bytes": meta.get("bytes"),
                "synced_at": meta.get("synced_at"),
                "checked_at": meta.get("checked_at"),
            }
        )
    rows.sort(key=lambda r: r.get("synced_at") or "", reverse=True)
    return rows[:limit]


def read_page(output_dir: Path, source_key: str, rel_path: str) -> str:
    source_dir = (output_dir / source_key).resolve()
    target = (source_dir / rel_path).resolve()
    if not str(target).startswith(str(source_dir)):
        raise ValueError("Invalid path")
    if not target.exists() or not target.is_file():
        raise FileNotFoundError(rel_path)
    return target.read_text(encoding="utf-8")


def print_summary(summaries: list[SyncSummary]) -> None:
    table = Table(title="Sync summary")
    table.add_column("Source")
    table.add_column("Added", justify="right")
    table.add_column("Changed", justify="right")
    table.add_column("Unchanged", justify="right")
    table.add_column("Deleted", justify="right")
    table.add_column("Errors", justify="right")
    for s in summaries:
        table.add_row(
            s.source,
            str(s.added),
            str(s.changed),
            str(s.unchanged),
            str(s.deleted),
            str(s.errors),
        )
    console.print(table)

    for s in summaries:
        interesting = [p for p in s.pages if p.status in {"added", "changed", "deleted", "error"}]
        if not interesting:
            continue
        console.print(f"\n[bold]{s.source} details[/]")
        for p in interesting[:20]:
            extra = f"  ({p.error})" if p.error else ""
            console.print(f"  [{p.status}] {p.url}{extra}")
        if len(interesting) > 20:
            console.print(f"  … {len(interesting) - 20} more")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Incremental llms.txt docs sync demo")
    p.add_argument("--config", default=str(DEFAULT_CONFIG), help="Path to config.yaml")
    p.add_argument("--source", default="claude", help="Source key, or 'all'")
    p.add_argument("--list-sources", action="store_true", help="List configured sources and exit")
    p.add_argument("--max-pages", type=int, default=None, help="Cap pages per source (demo)")
    p.add_argument("--output", default=None, help="Override output directory")
    p.add_argument("--concurrency", type=int, default=None)
    p.add_argument("--delay", type=float, default=None, help="Delay between requests (sec)")
    p.add_argument("--interval", type=float, default=None, help="Re-sync every N seconds (watch mode)")
    p.add_argument("--dry-run", action="store_true", help="Detect changes without writing files")
    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    config = load_config(args.config)

    if args.list_sources:
        for key, cfg in config["sources"].items():
            console.print(f"[bold]{key}[/]: {cfg.get('name')} → {cfg['llms_txt']}")
        return

    try:
        while True:
            started = time.time()
            summaries = asyncio.run(
                run_sync(
                    sources=args.source,
                    config=config,
                    output_dir=args.output,
                    max_pages=args.max_pages,
                    concurrency=args.concurrency,
                    delay_sec=args.delay,
                    dry_run=args.dry_run,
                )
            )
            print_summary(summaries)
            console.print(f"\n[dim]finished in {time.time() - started:.1f}s[/]")
            if not args.interval:
                break
            console.print(f"[yellow]sleeping {args.interval}s before next sync…[/] (Ctrl+C to stop)")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        console.print("\n[yellow]stopped[/]")
        sys.exit(0)


if __name__ == "__main__":
    main()
