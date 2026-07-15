"""Pure leak-policy decisions: destination resolution + DM gating."""

from __future__ import annotations

from daimon.adapters.mcp.tools.slack._leak_policy import (
    is_dm_destination,
    resolve_destination,
)


def test_resolve_destination_single_channel_returns_it() -> None:
    assert resolve_destination(frozenset({"C1"})) == "C1", (
        "exactly one live turn channel is an unambiguous destination"
    )


def test_resolve_destination_zero_or_many_fails_closed() -> None:
    assert resolve_destination(frozenset()) is None, (
        "no live turn context (routines, stale state) must fail closed"
    )
    assert resolve_destination(frozenset({"C1", "C2"})) is None, (
        "concurrent turns in two channels are ambiguous — fail closed"
    )


def test_is_dm_destination() -> None:
    assert is_dm_destination("D9") is True, "im channel ids start with D"
    assert is_dm_destination("C1") is False, "public/private channels are not DMs"
    assert is_dm_destination(None) is False, "unknown destination is not a DM"
