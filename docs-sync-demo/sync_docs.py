#!/usr/bin/env python3
"""Incremental docs sync demo for sites that publish llms.txt.

Workflow:
  1. Fetch each source's llms.txt index
  2. Extract Markdown page URLs
  3. Download pages (bounded concurrency + delay)
  4. Compare SHA-256 content hashes against local state
  5. Write only new/changed pages; drop deleted ones when the index no longer lists them

Examples:
  python sync_docs.py --list-sources
  python sync_docs.py --source claude --max-pages 5
  python sync_docs.py --source all --max-pages 3 --interval 60
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
import time
from dataclasses import dataclass, field
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


@dataclass
class PageResult:
    url: str
    status: str  # added | changed | unchanged | deleted | error | skipped
    path: str | None = None
    error: str | None = None


@dataclass
class SyncSummary:
    source: str
    added: int = 0
    changed: int = 0
    unchanged: int = 0
    deleted: int = 0
    errors: int = 0
    skipped: int = 0
    pages: list[PageResult] = field(default_factory=list)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_config(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def slugify_url(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.strip("/") or "index"
    # Preserve .md / .md.txt endings as .md on disk
    if path.endswith(".md.txt"):
        path = path[: -len(".md.txt")] + ".md"
    elif not path.endswith(".md"):
        path = path + ".md"
    safe = re.sub(r"[^a-zA-Z0-9._/-]+", "_", path)
    return safe


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
) -> SyncSummary:
    summary = SyncSummary(source=source_key)
    llms_url = source_cfg["llms_txt"]
    console.print(f"\n[bold cyan]→ {source_cfg.get('name', source_key)}[/]  {llms_url}")

    _, llms_body, _ = await fetch_text(client, llms_url)
    assert llms_body is not None
    urls = extract_urls(llms_body)
    urls = filter_urls(urls, source_cfg.get("include_url_regex"))
    console.print(f"  index links after filter: [green]{len(urls)}[/]")

    if max_pages is not None:
        urls = urls[:max_pages]
        console.print(f"  demo cap --max-pages={max_pages}")

    source_dir = output_dir / source_key
    pages_dir = source_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    state_file = state_path(output_dir, source_key)
    state = load_state(state_file)
    old_pages: dict[str, Any] = state.get("pages", {})
    new_pages: dict[str, Any] = {}

    concurrency = int(getattr(client, "_demo_concurrency", 4))
    sem = asyncio.Semaphore(concurrency)

    async def process_one(url: str) -> PageResult:
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
                    return PageResult(url=url, status="unchanged", path=str(out_path))

                assert body is not None
                digest = content_hash(body)
                if prev.get("hash") == digest and out_path.exists():
                    new_pages[url] = {
                        **prev,
                        "etag": meta.get("etag") or prev.get("etag"),
                        "last_modified": meta.get("last_modified") or prev.get("last_modified"),
                        "checked_at": utc_now(),
                    }
                    return PageResult(url=url, status="unchanged", path=str(out_path))

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
                return PageResult(url=url, status=status, path=str(out_path))
            except Exception as exc:  # noqa: BLE001 - demo should keep going
                return PageResult(url=url, status="error", error=str(exc))

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

    # Pages that disappeared from the index.
    # When --max-pages caps the run, only consider the capped URL set as "seen"
    # for hashing, but do not treat the rest of the index as deletions.
    if max_pages is None:
        indexed = set(urls)
        for url, meta in old_pages.items():
            if url in indexed:
                continue
            summary.deleted += 1
            summary.pages.append(PageResult(url=url, status="deleted", path=meta.get("path")))
            if not dry_run:
                rel = meta.get("path")
                if rel:
                    victim = source_dir / rel
                    if victim.exists():
                        victim.unlink()
        # Keep only pages still in the full index
        final_pages = new_pages
    else:
        # Preserve known pages outside this demo slice
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
        # Always refresh a local copy of the index for debugging
        (source_dir / "llms.txt").write_text(llms_body, encoding="utf-8")

    return summary


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


async def run_once(args: argparse.Namespace, config: dict[str, Any]) -> list[SyncSummary]:
    sources_cfg: dict[str, Any] = config["sources"]
    if args.source == "all":
        selected = list(sources_cfg.keys())
    else:
        if args.source not in sources_cfg:
            raise SystemExit(f"Unknown source '{args.source}'. Choose from: {', '.join(sources_cfg)}|all")
        selected = [args.source]

    output_dir = Path(args.output or config.get("output_dir", "./data")).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    headers = {"User-Agent": config.get("user_agent", "docs-sync-demo/0.1")}
    timeout = httpx.Timeout(config.get("request_timeout_sec", 30.0))
    concurrency = int(args.concurrency or config.get("concurrency", 4))
    delay_sec = float(args.delay if args.delay is not None else config.get("delay_sec", 0.2))

    async with httpx.AsyncClient(
        headers=headers,
        timeout=timeout,
        follow_redirects=True,
    ) as client:
        client._demo_concurrency = concurrency  # type: ignore[attr-defined]
        summaries: list[SyncSummary] = []
        for key in selected:
            summary = await sync_source(
                client,
                key,
                sources_cfg[key],
                output_dir=output_dir,
                max_pages=args.max_pages,
                delay_sec=delay_sec,
                dry_run=args.dry_run,
            )
            summaries.append(summary)
        return summaries


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Incremental llms.txt docs sync demo")
    p.add_argument("--config", default="config.yaml", help="Path to config.yaml")
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
    config_path = Path(args.config)
    if not config_path.exists():
        # Allow running from repo root
        alt = Path(__file__).resolve().parent / "config.yaml"
        if alt.exists():
            config_path = alt
        else:
            raise SystemExit(f"Config not found: {args.config}")

    config = load_config(config_path)

    if args.list_sources:
        for key, cfg in config["sources"].items():
            console.print(f"[bold]{key}[/]: {cfg.get('name')} → {cfg['llms_txt']}")
        return

    try:
        while True:
            started = time.time()
            summaries = asyncio.run(run_once(args, config))
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
