"""The REST client the Discord tools build pings nobody unless a post says whom."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from daimon.adapters.mcp.tools.discord._client import rest_client
from daimon.core.outbound.mentions import ping_users

# Loaded by file path, as test_discord_wizard.py does, to dodge the conftest name collision.
_spec = importlib.util.spec_from_file_location(
    "_tools_conftest", Path(__file__).parent / "conftest.py"
)
assert _spec is not None and _spec.loader is not None
_tools_conftest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_tools_conftest)


async def _no_route(*_args: object) -> None:
    raise AssertionError("building the client must not call Discord")


async def test_rest_client_when_ping_users_unset_then_pings_no_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A send that skipped the outbound gate must never ping, so the default is no users."""
    _tools_conftest.patch_discord_http(monkeypatch, _no_route)
    assert ping_users.get() is None, "no post has set whom it may ping"

    async with rest_client("token") as client:
        allowed = client.allowed_mentions

    assert allowed is not None, "the client always carries an allow-list"
    assert allowed.users is False, "users default to nobody, not everyone"
    assert allowed.everyone is False and allowed.roles is False, "never everyone or a role"


async def test_rest_client_when_ping_users_set_then_pings_only_those(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rendered post's tagged people are the only ones the client may ping."""
    _tools_conftest.patch_discord_http(monkeypatch, _no_route)
    token = ping_users.set(frozenset({"7"}))
    try:
        async with rest_client("token") as client:
            allowed = client.allowed_mentions
    finally:
        ping_users.reset(token)

    assert allowed is not None and isinstance(allowed.users, list), "a tagged person may be pinged"
    assert [u.id for u in allowed.users] == [7], "and only that person"
