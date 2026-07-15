"""Unit tests for `run_cli` — the CLI adapter-edge error boundary."""

from __future__ import annotations

from io import StringIO

import httpx
import pytest
import typer
from anthropic import APIStatusError
from daimon.adapters.cli.errors import run_cli
from daimon.core.errors import StoreError
from rich.console import Console


def test_run_cli_returns_none_when_coro_completes() -> None:
    buf = StringIO()
    console = Console(file=buf, force_terminal=False, highlight=False, width=200)

    async def ok() -> None:
        return None

    assert run_cli(ok(), console=console) is None, "run_cli returns None on clean completion"


def test_run_cli_prints_cross_and_exits_1_on_store_error() -> None:
    buf = StringIO()
    console = Console(file=buf, force_terminal=False, highlight=False, width=200)

    async def boom() -> None:
        raise StoreError("no agent named 'x' in your account.")

    with pytest.raises(typer.Exit) as exc:
        run_cli(boom(), console=console)

    assert exc.value.exit_code == 1, "StoreError must map to exit code 1"
    assert "✗ no agent named 'x' in your account." in buf.getvalue(), (
        "StoreError message must appear on the ✗ line verbatim"
    )
    assert "Traceback" not in buf.getvalue(), "no Rich traceback on StoreError"


def test_run_cli_prints_upstream_prefix_and_exits_1_on_api_error() -> None:
    buf = StringIO()
    console = Console(file=buf, force_terminal=False, highlight=False, width=200)

    async def boom() -> None:
        request = httpx.Request("POST", "https://api.anthropic.com/v1/agents")
        response = httpx.Response(
            500, request=request, json={"error": {"type": "api_error", "message": "boom"}}
        )
        raise APIStatusError("boom", response=response, body=None)

    with pytest.raises(typer.Exit) as exc:
        run_cli(boom(), console=console)

    assert exc.value.exit_code == 1, "APIError must map to exit code 1"
    assert "✗ upstream:" in buf.getvalue(), "APIError must print the 'upstream:' prefix"
