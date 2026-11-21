"""`share_secret_with` sends a key to the asker unless an admin names someone, never in its
result, and only for a person who may hold it."""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace as NS
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.mcp.tools import secret_shares as tool
from daimon.adapters.mcp.tools.setup_target import HELPER_REFUSAL
from daimon.core import secret_share_flow as flow
from daimon.core.stores import secret_shares as store
from daimon.core.stores.secret_shares import ShareView
from fastmcp.exceptions import ToolError

SHARE = ShareView(uuid.uuid4(), "1", "1", "Toggl", None, None, datetime.now(UTC))


@asynccontextmanager
async def _cm(value: Any = None) -> AsyncIterator[Any]:
    yield value


def _origin(**changes: Any) -> Any:
    fields = {
        "sender_foreign": False,
        "helper_of": None,
        "responder_ma_agent_id": "ag_1",
        "attended": True,
        "human_invocation": True,
    }
    return MagicMock(**(fields | changes))


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    origin = AsyncMock(return_value=_origin())
    monkeypatch.setattr(tool, "require_turn_origin", origin)
    monkeypatch.setattr(tool, "may_own", MagicMock(return_value=True))
    monkeypatch.setattr(flow, "sharing_refusal", AsyncMock(return_value=None))
    admin = AsyncMock(return_value=False)
    monkeypatch.setattr(tool, "current_admin", admin)
    owns = AsyncMock(return_value=True)
    monkeypatch.setattr(tool, "caller_owns_agent", owns)
    key = MagicMock(content="s3cret", last_set_by_account_id=None)
    monkeypatch.setattr(tool, "get_agent_file", AsyncMock(return_value=key))
    monkeypatch.setattr(tool, "_require_bot_token", MagicMock(return_value="token"))
    monkeypatch.setattr(tool, "_require_guild_id", MagicMock(return_value="7"))
    members = {
        uid: MagicMock(bot=False, create_dm=AsyncMock(return_value=_dm(uid))) for uid in "12"
    }
    guild = MagicMock(fetch_member=AsyncMock(side_effect=lambda uid: members[str(uid)]))
    client = MagicMock(fetch_guild=AsyncMock(return_value=guild))
    monkeypatch.setattr(tool, "rest_client", MagicMock(return_value=_cm(client)))
    create = AsyncMock(return_value=SHARE)
    monkeypatch.setattr(store, "create_share", create)
    monkeypatch.setattr(store, "set_share_card", AsyncMock())
    return {
        "create": create,
        "members": members,
        "origin": origin,
        "admin": admin,
        "owns": owns,
    }


def _dm(uid: str) -> Any:
    return NS(id=int(uid) + 10, send=AsyncMock(return_value=NS(id=8)))


def _fresh() -> AbstractAsyncContextManager[Any]:
    return _cm()


def _runtime() -> Any:
    factory = MagicMock(side_effect=_fresh)
    factory.begin = MagicMock(side_effect=_fresh)
    return MagicMock(session_factory=factory)


def _auth() -> Any:
    return MagicMock(
        platform="discord",
        platform_user_id="1",
        tenant_id=uuid.uuid4(),
        agent_id=None,
        external_id="5",
    )


async def _call(recipient_id: str | None) -> tool.ShareSecretResult:
    return await tool.share_secret_with_impl(
        _runtime(),
        _auth(),
        origin_context_id=str(uuid.uuid4()),
        label="Toggl",
        key_name="TOGGL_API_TOKEN",
        recipient_id=recipient_id,
    )


def _card(wired: dict[str, Any], uid: str) -> str:
    sent = wired["members"][uid].create_dm.return_value.send.await_args.kwargs
    return str(sent["embed"].to_dict()) + str(sent["view"].children[0].custom_id)


async def test_it_goes_to_the_asker_and_the_result_holds_no_value(wired: dict[str, Any]) -> None:
    result = await _call(None)
    assert result.recipient_id == "1"
    assert "s3cret" not in result.model_dump_json()
    assert wired["create"].await_args.kwargs["recipient"] == "1"
    assert "s3cret" not in _card(wired, "1") and "sss:" in _card(wired, "1")


async def test_only_an_admin_sends_it_to_someone_else(wired: dict[str, Any]) -> None:
    with pytest.raises(ToolError, match="Only an admin"):
        await _call("2")
    wired["create"].assert_not_awaited()


async def test_a_key_for_someone_else_waits_for_the_askers_send(wired: dict[str, Any]) -> None:
    wired["admin"].return_value = True
    result = await _call("2")
    assert wired["create"].await_args.kwargs["awaiting_sender"] is True
    assert "ssc:" in _card(wired, "1"), "the Send card goes to the asker"
    wired["members"]["2"].create_dm.assert_not_awaited()
    assert "Send button" in result.note


async def test_only_an_admin_the_owner_or_the_setter_gets_a_key(wired: dict[str, Any]) -> None:
    wired["owns"].return_value = False
    with pytest.raises(ToolError, match="whoever saved the key"):
        await _call(None)
    wired["create"].assert_not_awaited()


@pytest.mark.parametrize(
    ("changes", "refusal"),
    [
        ({"helper_of": "ag_2"}, HELPER_REFUSAL),
        ({"attended": False}, "Only a person in this conversation"),
        ({"human_invocation": False}, "Only a person in this conversation"),
        ({"sender_foreign": True}, "Only a member of this server"),
    ],
)
async def test_a_helper_an_unattended_turn_or_a_foreign_sender_is_refused(
    wired: dict[str, Any], changes: dict[str, Any], refusal: str
) -> None:
    wired["origin"].return_value = _origin(**changes)
    with pytest.raises(ToolError, match=re.escape(refusal)):
        await _call(None)
    wired["create"].assert_not_awaited()


async def test_a_failed_send_deletes_the_share(
    wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    delete = AsyncMock()
    monkeypatch.setattr(store, "delete_share", delete)
    dm = wired["members"]["1"].create_dm.return_value
    dm.send.side_effect = discord.Forbidden(MagicMock(status=403), "closed")
    with pytest.raises(ToolError):
        await _call(None)
    delete.assert_awaited_once()
