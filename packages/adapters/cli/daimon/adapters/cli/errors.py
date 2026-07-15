"""Adapter-edge error boundary for the CLI.

`run_cli` is the single call site that translates an async CLI entry's
exceptions into Rich output and a Typer exit code. Every Typer command
function calls it in place of `asyncio.run(...)`.

Adapters catch `DaimonError | anthropic.APIError` per `daimon.core.errors`.
`typer.BadParameter` / `typer.UsageError` are not caught — Typer handles
those as exit 2 (usage errors), and that behavior is intentional.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

import typer
from anthropic import APIError
from daimon.core.errors import DaimonError
from rich.console import Console


def run_cli(coro: Coroutine[Any, Any, None], *, console: Console) -> None:
    try:
        asyncio.run(coro)
    except APIError as err:
        console.print(f"[red]✗ upstream: {err}[/red]")
        raise typer.Exit(code=1) from err
    except DaimonError as err:
        console.print(f"[red]✗ {err}[/red]")
        raise typer.Exit(code=1) from err
