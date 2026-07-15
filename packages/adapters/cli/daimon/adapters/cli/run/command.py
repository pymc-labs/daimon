"""`daimon run` — single-turn subprocess entrypoint."""

from __future__ import annotations

import asyncio
import sys
import uuid
from typing import Annotated

import anthropic
import typer
from daimon.adapters.cli.logging import configure_admin_logging
from daimon.adapters.cli.run.events import (
    TerminalFailed,
    serialize_event,
    serialize_turn_state,
)
from daimon.adapters.cli.run.lifecycle import NdjsonLifecycle
from daimon.adapters.cli.runtime import CliRuntime, build_runtime
from daimon.core.config import load_settings
from daimon.core.turn.driver import run_turn
from daimon.core.turn.state import TurnState


def run_command(
    positional_message: Annotated[str | None, typer.Argument(metavar="[MESSAGE]")] = None,
    session: Annotated[str, typer.Option("--session", help="MA session id")] = "",
    message_flag: Annotated[
        str | None,
        typer.Option("--message", "-m", help="User message. Use '-' to read from stdin."),
    ] = None,
) -> None:
    configure_admin_logging()

    if not session:
        print("--session is required", file=sys.stderr)
        raise typer.Exit(code=1)
    if positional_message is not None and message_flag is not None:
        print("pass message as positional OR --message, not both", file=sys.stderr)
        raise typer.Exit(code=1)
    raw_message = positional_message if positional_message is not None else message_flag

    if raw_message is None:
        print("a user message is required for a new turn", file=sys.stderr)
        raise typer.Exit(code=1)

    user_message = _resolve_user_message(raw_message)

    settings = load_settings()

    async def _with_defaults() -> int:
        async with build_runtime(settings) as rt:
            return await run_conversation(rt=rt, session_id=session, user_message=user_message)

    exit_code = asyncio.run(_with_defaults())
    raise typer.Exit(code=exit_code)


def _resolve_user_message(raw: str) -> str:
    if raw == "-":
        return sys.stdin.read()
    return raw


async def run_conversation(
    *,
    rt: CliRuntime,
    session_id: str,
    user_message: str,
) -> int:
    turn_id = f"turn_{uuid.uuid4().hex[:12]}"
    lifecycle = NdjsonLifecycle(stdout=sys.stdout, session_id=session_id, turn_id=turn_id)
    cancel = asyncio.Event()

    try:
        state = await run_turn(
            anthropic=rt.anthropic,
            session_id=session_id,
            user_message=user_message,
            lifecycle=lifecycle,
            cancel=cancel,
        )
    except anthropic.APIError as err:
        _emit_failed_terminal(
            lifecycle,
            session_id=session_id,
            turn_id=turn_id,
            message=str(err),
        )
        return 1

    return 0 if state.error is None else 1


def _emit_failed_terminal(
    lifecycle: NdjsonLifecycle,
    *,
    session_id: str,
    turn_id: str,
    message: str,
) -> None:
    event = TerminalFailed(
        session_id=session_id,
        turn_id=turn_id,
        error={"kind": "upstream", "message": message},
        state=serialize_turn_state(TurnState()),
    )
    lifecycle.stdout.write(serialize_event(event) + "\n")
    lifecycle.stdout.flush()
