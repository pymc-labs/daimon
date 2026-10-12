"""Tests for the Slack user-group lookup in auth/group_members.py."""

from __future__ import annotations

import asyncio
import re
from typing import Any
from unittest.mock import patch

import pytest
from aioresponses import CallbackResult
from aioresponses import aioresponses as AioResponsesMock
from cryptography.fernet import Fernet
from daimon.adapters.mcp.auth.group_members import GroupLookups
from daimon.adapters.mcp.slack_http import SlackHttpSession
from daimon.core.channel_admins import GroupLookupFailed
from daimon.core.github_credentials import build_multifernet, encrypt_token
from daimon.core.stores.slack_bot_tokens import upsert_slack_bot_token
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_USERGROUPS = re.compile(r"^https://slack\.com/api/usergroups\.users\.list.*$")


async def _lookups(
    sessionmaker: async_sessionmaker[AsyncSession], slack_http: SlackHttpSession
) -> GroupLookups:
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    async with sessionmaker() as s, s.begin():
        await upsert_slack_bot_token(
            s, team_id="T_GROUPS", encrypted_token=encrypt_token(fernet, "xoxb-groups")
        )
    return GroupLookups(
        sessionmaker=sessionmaker, fernet=fernet, teams_client=None, slack_http=slack_http
    )


async def test_slack_group_lookup_answers_over_the_shared_session(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The lookup reads the group's members through the app's session."""
    slack_http = SlackHttpSession()
    lookups = await _lookups(sessionmaker, slack_http)
    live = lookups.live_members("slack", "T_GROUPS")
    assert live is not None, "a fernet makes Slack groups resolvable"

    with AioResponsesMock() as mock:
        mock.get(_USERGROUPS, payload={"ok": True, "users": ["U1", "U2"]})
        members = await live("S1")
    await slack_http.close()

    assert members == frozenset({"U1", "U2"}), "the group's members come back"


async def test_slack_group_lookup_still_times_out_on_the_shared_session(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The shared session's longer timeout must not loosen the lookup's own bound."""

    async def stall(_url: Any, **_kwargs: Any) -> CallbackResult:
        await asyncio.sleep(1)
        return CallbackResult(payload={"ok": True, "users": []})

    slack_http = SlackHttpSession()
    lookups = await _lookups(sessionmaker, slack_http)
    live = lookups.live_members("slack", "T_GROUPS")
    assert live is not None, "a fernet makes Slack groups resolvable"

    with (
        patch("daimon.adapters.mcp.auth.group_members._SLACK_TIMEOUT_S", 0.01),
        AioResponsesMock() as mock,
    ):
        mock.get(_USERGROUPS, callback=stall)
        with pytest.raises(GroupLookupFailed, match="TimeoutError"):
            await live("S1")
    await slack_http.close()
