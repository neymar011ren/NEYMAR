#!/usr/bin/env python3
"""Launch KingSwitch locally."""

from __future__ import annotations

import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="KingSwitch")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()
    uvicorn.run("main:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
