"""`share_secret_with` sends a key to the asker unless an admin names someone, never in its result."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace as NS
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from daimon.adapters.mcp.tools import secret_shares as tool
from daimon.core.stores import secret_shares as store
from daimon.core.stores.secret_shares import ShareView
from fastmcp.exceptions import ToolError

SHARE = ShareView(uuid.uuid4(), "1", "1", "Toggl", None, None, datetime.now(UTC))


@asynccontextmanager
async def _cm(value: Any = None) -> AsyncIterator[Any]:
    yield value


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    origin = MagicMock(sender_foreign=False, helper_of=None, responder_ma_agent_id="ag_1")
    monkeypatch.setattr(tool, "require_turn_origin", AsyncMock(return_value=origin))
    monkeypatch.setattr(tool, "feature_in", AsyncMock(return_value=MagicMock(on=True)))
    monkeypatch.setattr(tool, "current_admin", AsyncMock(return_value=False))
    monkeypatch.setattr(tool, "get_agent_file", AsyncMock(return_value=MagicMock(content="s3cret")))
    monkeypatch.setattr(tool, "env_dm_policy", MagicMock())
    monkeypatch.setattr(tool, "_require_bot_token", MagicMock(return_value="token"))
    member = MagicMock(bot=False)
    member.create_dm = AsyncMock(return_value=NS(id=9, send=AsyncMock(return_value=NS(id=8))))
    guild = MagicMock(fetch_member=AsyncMock(return_value=member))
    client = MagicMock(fetch_guild=AsyncMock(return_value=guild))
    monkeypatch.setattr(tool, "rest_client", MagicMock(return_value=_cm(client)))
    create = AsyncMock(return_value=SHARE)
    monkeypatch.setattr(store, "create_share", create)
    monkeypatch.setattr(store, "set_share_card", AsyncMock())
    return {"create": create, "member": member}


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


async def test_it_goes_to_the_asker_and_the_result_holds_no_value(wired: dict[str, Any]) -> None:
    result = await _call(None)
    assert result.recipient_id == "1"
    assert "s3cret" not in result.model_dump_json()
    assert wired["create"].await_args.kwargs["recipient"] == "1"
    embed = wired["member"].create_dm.return_value.send.await_args.kwargs["embed"]
    assert "s3cret" not in str(embed.to_dict())


async def test_only_an_admin_sends_it_to_someone_else(wired: dict[str, Any]) -> None:
    with pytest.raises(ToolError, match="Only an admin"):
        await _call("2")
    wired["create"].assert_not_awaited()
