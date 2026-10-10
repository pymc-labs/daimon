"""Run a disposable, authenticated Daimon MCP host for a live QA run.

    uv run python scripts/qa_mcp_host.py up --database-url postgresql+asyncpg://…/daimon_qa
    uv run python scripts/qa_mcp_host.py up … --tunnel --lead-go inbox/<ts>-LEAD-GO.md
    uv run python scripts/qa_mcp_host.py down --database-url … \
        --manifest /tmp/daimon-qa-mcp/<run>/manifest.json

`up` builds the host (`daimon.testing.qa_mcp_host`), serves its gate on a
loopback port, writes the run bearer to `<root>/<run>/bearer` (mode 0600,
never printed) and serves until interrupted or the bearer expires; then it
cleans up whatever the manifest owns. A tunnel opens only with `--tunnel`
and a recorded `--lead-go`.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as dt
import os
import signal
import sys
from pathlib import Path

from daimon.testing.qa_mcp_host import (
    DEFAULT_ROOT,
    build_host,
    cleanup,
    open_tunnel,
    serve,
)


async def _up(args: argparse.Namespace) -> int:
    qa = await build_host(
        database_url=args.database_url,
        port=args.port,
        ttl=dt.timedelta(minutes=args.ttl_minutes),
        root=args.root,
    )
    manifest_path = qa.manifest.path(args.root)
    bearer_path = args.root / qa.manifest.run_id / "bearer"
    bearer_path.touch(mode=0o600)
    bearer_path.write_text(qa.bearer.token + "\n")
    try:
        async with serve(qa) as url:
            print(f"manifest: {manifest_path}")
            print(f"bearer file: {bearer_path}")
            print(f"local MCP URL: {url}")
            if args.tunnel:
                print(f"tunnel MCP URL: {await open_tunnel(qa, lead_go=args.lead_go or '')}")
            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            for signum in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(signum, stop.set)
            remaining = (qa.bearer.expires_at - dt.datetime.now(dt.UTC)).total_seconds()
            # Serve until interrupted or until the bearer expires, whichever is first.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=max(remaining, 0))
    finally:
        await qa.engine.dispose()
        cleaned = await cleanup(manifest_path, database_url=args.database_url)
        bearer_path.unlink(missing_ok=True)
        print(f"cleaned: {cleaned.status}")
    return 0


async def _down(args: argparse.Namespace) -> int:
    cleaned = await cleanup(args.manifest, database_url=args.database_url)
    (args.manifest.parent / "bearer").unlink(missing_ok=True)
    print(f"cleaned: {cleaned.status}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="A disposable, authenticated Daimon MCP host.")
    commands = parser.add_subparsers(dest="command", required=True)
    up = commands.add_parser("up")
    up.add_argument(
        "--database-url", default=os.environ.get("DAIMON_QA_DATABASE_URL"), required=False
    )
    up.add_argument("--port", type=int, default=8765)
    up.add_argument("--ttl-minutes", type=int, default=30)
    up.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    up.add_argument("--tunnel", action="store_true")
    up.add_argument("--lead-go", default=None)
    down = commands.add_parser("down")
    down.add_argument("--manifest", type=Path, required=True)
    down.add_argument("--database-url", default=os.environ.get("DAIMON_QA_DATABASE_URL"))
    args = parser.parse_args()
    if not args.database_url:
        parser.error("--database-url (or DAIMON_QA_DATABASE_URL) is required")
    return asyncio.run(_up(args) if args.command == "up" else _down(args))


if __name__ == "__main__":
    sys.exit(main())
