"""Replay structured events emitted by the real lifecycle against the model."""

from __future__ import annotations

import json
import subprocess
import sys
import time
import types
import uuid
from pathlib import Path
from typing import Any

import structlog
from daimon.adapters.discord.lifecycle import DiscordTurnLifecycle
from daimon.core.turn.state import TextBlock, ToolUseBlock, TurnState


async def test_fake_card_trace_is_accepted_by_model_replay() -> None:
    card = types.SimpleNamespace(id=1234)

    async def send(**kwargs: Any) -> Any:
        return card

    async def edit(ref: Any, **kwargs: Any) -> None:
        assert ref is card

    lifecycle = DiscordTurnLifecycle(
        send=send, edit=edit, agent_name="test", model_id="m", turn_id=uuid.uuid4()
    )
    with structlog.testing.capture_logs() as logs:
        await lifecycle.post_initial()
        lifecycle._last_flush = time.monotonic() - 11  # pyright: ignore[reportPrivateUsage]
        await lifecycle.on_render(
            TurnState(
                content=[
                    ToolUseBlock(
                        kind="tool_use", id="t", type="agent.tool_use", name="bash", input={}
                    )
                ]
            )
        )
        await lifecycle.on_terminal_success(
            TurnState(content=[TextBlock(kind="text", text="Done")])
        )

    model_replay = Path(__file__).resolve().parents[4] / "formal/discord_card_lifecycle/replay.py"
    payload = "\n".join(json.dumps(item, default=str) for item in logs)
    result = subprocess.run(
        [sys.executable, str(model_replay), "-"],
        input=payload,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "accepted 1/1 replayable traces" in result.stdout


def test_replay_rejects_terminal_overtaking_progress() -> None:
    model_replay = Path(__file__).resolve().parents[4] / "formal/discord_card_lifecycle/replay.py"
    events = [
        {
            "event": "turn.card_write_issued",
            "turn_id": "turn",
            "message_id": "1",
            "write_id": "p",
            "kind": "progress",
        },
        {
            "event": "turn.card_write_dispatched",
            "turn_id": "turn",
            "message_id": "1",
            "write_id": "p",
            "kind": "progress",
        },
        {
            "event": "turn.card_write_issued",
            "turn_id": "turn",
            "message_id": "1",
            "write_id": "t",
            "kind": "terminal",
        },
        {
            "event": "turn.card_write_dispatched",
            "turn_id": "turn",
            "message_id": "1",
            "write_id": "t",
            "kind": "terminal",
        },
    ]
    result = subprocess.run(
        [sys.executable, str(model_replay), "-"],
        input="\n".join(json.dumps(item) for item in events),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert "terminal dispatched while progress is on wire" in result.stderr
