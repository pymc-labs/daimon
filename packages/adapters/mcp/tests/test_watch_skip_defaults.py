"""With `watch_approval` skip nobody sees the card, so a new watch can only be the default one."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from daimon.adapters.mcp.tools import watches as tools
from fastmcp.exceptions import ToolError

ORIGIN = SimpleNamespace(responder_ma_agent_id="agent", platform="discord")
DEFAULTS: dict[str, Any] = {
    "delivery": "thread",
    "lifetime_days": 7,
    "permanent": False,
    "server": tools.DAIMON_SERVER_NAME,
}


def _auth() -> Any:
    return SimpleNamespace(tenant_id=uuid.uuid4())


def _patch(monkeypatch: pytest.MonkeyPatch, *, skipped: bool, satisfied: bool = False) -> None:
    monkeypatch.setattr(tools, "watch_card_skipped", AsyncMock(return_value=skipped))
    monkeypatch.setattr(tools, "card_satisfied", lambda _auth: satisfied)


async def _check(**changes: object) -> None:
    runtime = SimpleNamespace(session_factory=object())
    await tools.require_skip_defaults(
        cast(Any, runtime), _auth(), cast(Any, ORIGIN), **{**DEFAULTS, **changes}
    )


async def test_default_watch_passes_under_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default watch needs no card, so skip lets it through."""
    _patch(monkeypatch, skipped=True)
    await _check()


@pytest.mark.parametrize(
    "change",
    [
        {"delivery": "channel"},
        {"delivery": "dm"},
        {"lifetime_days": 30},
        {"lifetime_days": 1},
        {"permanent": True},
        {"server": "other-server"},
    ],
)
async def test_skip_refuses_anything_but_the_defaults(
    monkeypatch: pytest.MonkeyPatch, change: dict[str, object]
) -> None:
    """A model-chosen destination, lifetime or server is refused when no card shows it."""
    _patch(monkeypatch, skipped=True)
    with pytest.raises(ToolError, match="without a card"):
        await _check(**change)


async def test_ask_keeps_every_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    """With ask the card shows the choices, so they stay open."""
    _patch(monkeypatch, skipped=False)
    await _check(delivery="channel", lifetime_days=30, server="other-server")


async def test_an_approved_card_keeps_every_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    """A card the person approved for this exact call covers the choices."""
    _patch(monkeypatch, skipped=True, satisfied=True)
    await _check(delivery="channel", lifetime_days=30)


async def test_create_watch_runs_the_skip_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """`create_watch_impl` refuses before it enrolls anything."""
    _patch(monkeypatch, skipped=True)
    monkeypatch.setattr(tools, "require_attended_owner", AsyncMock(return_value=ORIGIN))
    auth = SimpleNamespace(tenant_id=uuid.uuid4(), platform_user_id="1")
    with pytest.raises(ToolError, match="without a card"):
        await tools.create_watch_impl(
            cast(Any, SimpleNamespace(session_factory=object())),
            cast(Any, auth),
            origin_context_id="ctx",
            instructions="Report.",
            source="poll",
            delivery="channel",
        )
