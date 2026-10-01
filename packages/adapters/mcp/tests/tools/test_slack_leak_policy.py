"""Pure leak-policy decisions: destination resolution + DM gating."""

from __future__ import annotations

from daimon.adapters.mcp.tools.slack._leak_policy import is_dm_destination


def test_is_dm_destination() -> None:
    assert is_dm_destination("D9") is True, "im channel ids start with D"
    assert is_dm_destination("C1") is False, "public/private channels are not DMs"
    assert is_dm_destination(None) is False, "unknown destination is not a DM"
