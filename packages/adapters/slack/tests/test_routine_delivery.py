"""Slack's routine result poster (FEAT-085)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from daimon.adapters.slack import routine_delivery as poster_mod
from daimon.adapters.slack.routine_delivery import make_slack_routine_poster
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.agent_identity import AgentIdentity
from daimon.core.scope import ChannelScopeRef, DeploymentDefault
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.domain import RoutineRow
from daimon.core.stores.routines import create_routine
from daimon.core.stores.scoped_config_write import set_fields
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def _routine(db_session: AsyncSession, *, kind: str, destination_id: str) -> RoutineRow:
    tenant = await make_tenant(db_session, platform="slack", workspace_id="T_ROUTINES")
    row = await create_routine(
        db_session,
        tenant_id=tenant.id,
        created_by_user_id="U1",
        agent_id="ag",
        agent_name="daimon",
        cron_expr="0 9 * * 1",
        timezone_="UTC",
        trigger_message="go",
        destination_kind=kind,  # type: ignore[arg-type]
        destination_id=destination_id,
    )
    await db_session.commit()
    text = "Done <!channel> ping <@U7>."
    return row.model_copy(update={"last_result_tail": text, "delivery_payload": text})


def _poster(
    sm: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> tuple[Any, MagicMock]:
    client = MagicMock()
    client.chat_postMessage = AsyncMock()
    client.users_info = AsyncMock(
        return_value={"user": {"id": "U1", "team_id": "T_ROUTINES", "deleted": False}}
    )
    client.conversations_open = AsyncMock(return_value={"channel": {"id": "D_CREATOR"}})
    client.conversations_info = AsyncMock(
        side_effect=lambda channel: {"channel": {"id": channel, "is_private": False}}
    )
    client.conversations_members = AsyncMock(return_value={"members": ["U1"]})
    client.conversations_replies = AsyncMock(return_value={"messages": [{"ts": "1717.5"}]})

    async def fake_resolve(runtime: object, *, team_id: str) -> object:
        assert team_id == "T_ROUTINES", "the client is built for the routine's own workspace"
        return client

    monkeypatch.setattr(poster_mod, "resolve_web_client", fake_resolve)
    runtime = cast(
        SlackRuntime,
        SimpleNamespace(
            sessionmaker=sm,
            anthropic=MagicMock(),
            settings=SimpleNamespace(direct_message_policies={}),
            deployment_default=DeploymentDefault(),
        ),
    )
    return make_slack_routine_poster(runtime), client


async def test_posts_into_a_thread_without_broadcasting(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = await _routine(db_session, kind="thread", destination_id="C1:1717.5")
    post, client = _poster(db_session_factory, monkeypatch)

    outcome = await post(row)

    assert outcome.status == "delivered"
    kwargs = client.chat_postMessage.await_args.kwargs
    assert (kwargs["channel"], kwargs["thread_ts"]) == ("C1", "1717.5")
    client.conversations_replies.assert_awaited_once_with(channel="C1", ts="1717.5", limit=1)
    assert "<!channel>" not in kwargs["text"], "a routine never broadcasts"
    assert "<@U7>" in kwargs["text"], "mentions of people survive"


def _with_identity(monkeypatch: pytest.MonkeyPatch, identity: AgentIdentity) -> None:
    async def resolve(*args: object, **kwargs: Any) -> AgentIdentity:
        assert kwargs["platform"] == "slack" and kwargs["workspace_id"] == "T_ROUTINES"
        return identity

    monkeypatch.setattr(poster_mod, "resolve_routine_identity", resolve)


async def test_with_identity_the_result_posts_as_the_agent_without_from_wording(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = await _routine(db_session, kind="channel", destination_id="C1")
    post, client = _poster(db_session_factory, monkeypatch)
    client.token = "xoxb-routine-identity"
    _with_identity(
        monkeypatch, AgentIdentity(name="research", avatar_url="https://app/a.png", builtin=False)
    )

    assert (await post(row)).status == "delivered"

    kwargs = client.chat_postMessage.await_args.kwargs
    assert (kwargs["username"], kwargs["icon_url"]) == ("research", "https://app/a.png")
    assert kwargs["text"].startswith("Routine result (0 9 * * 1, UTC):\n\nDone")
    assert "<!channel>" not in kwargs["text"], "a routine never broadcasts"


async def test_without_customize_scope_the_plain_post_keeps_the_agents_name(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from slack_sdk.errors import SlackApiError
    from slack_sdk.web.async_slack_response import AsyncSlackResponse

    row = await _routine(db_session, kind="channel", destination_id="C1")
    post, client = _poster(db_session_factory, monkeypatch)
    client.token = "xoxb-routine-no-customize"
    response = MagicMock(spec=AsyncSlackResponse)
    response.data = {"ok": False, "error": "missing_scope", "needed": "chat:write.customize"}
    client.chat_postMessage = AsyncMock(side_effect=[SlackApiError("scope", response), None])
    _with_identity(monkeypatch, AgentIdentity(name="research", avatar_url=None, builtin=False))

    assert (await post(row)).status == "delivered"

    retry = client.chat_postMessage.await_args_list[-1].kwargs
    assert "username" not in retry
    assert retry["text"].startswith("Routine result from daimon (0 9 * * 1, UTC):")


async def test_the_built_in_agent_keeps_todays_text(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = await _routine(db_session, kind="channel", destination_id="C1")
    post, client = _poster(db_session_factory, monkeypatch)
    _with_identity(monkeypatch, AgentIdentity(name="daimon", avatar_url=None, builtin=True))

    assert (await post(row)).status == "delivered"

    kwargs = client.chat_postMessage.await_args.kwargs
    assert "username" not in kwargs
    assert kwargs["text"].startswith("Routine result from daimon (0 9 * * 1, UTC):")


async def test_a_protected_channel_is_refused(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = await _routine(db_session, kind="channel", destination_id="C_ANN")
    await set_access_policy(
        db_session,
        tenant_id=row.tenant_id,
        policy=TenantAccessPolicy(protected_channel_ids=("C_ANN",)),
    )
    await db_session.commit()
    post, client = _poster(db_session_factory, monkeypatch)

    outcome = await post(row)

    assert (outcome.status, outcome.note) == ("delivered", "dm_fallback:protected_channel")
    (call,) = client.chat_postMessage.await_args_list
    assert call.kwargs["channel"] == "D_CREATOR", "the result went to the creator, not C_ANN"
    assert "lets nobody write there" in call.kwargs["text"]


async def test_a_malformed_thread_destination_is_skipped(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = await _routine(db_session, kind="thread", destination_id="C1")
    post, client = _poster(db_session_factory, monkeypatch)

    outcome = await post(row)

    assert (outcome.status, outcome.note) == ("delivered", "dm_fallback:destination_unavailable")
    (call,) = client.chat_postMessage.await_args_list
    assert call.kwargs["channel"] == "D_CREATOR"


async def test_slack_refusing_the_channel_falls_back_to_a_dm(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from slack_sdk.errors import SlackApiError
    from slack_sdk.web.async_slack_response import AsyncSlackResponse

    row = await _routine(db_session, kind="channel", destination_id="C1")
    post, client = _poster(db_session_factory, monkeypatch)
    response = MagicMock(spec=AsyncSlackResponse)
    response.data = {"ok": False, "error": "not_in_channel"}

    async def post_message(**kwargs: object) -> object:
        if kwargs["channel"] == "C1":
            raise SlackApiError("not_in_channel", response)
        return {"ts": "1.0"}

    client.chat_postMessage = AsyncMock(side_effect=post_message)

    outcome = await post(row)

    assert (outcome.status, outcome.note) == ("delivered", "dm_fallback:destination_unavailable")


@pytest.mark.parametrize(
    "agent", ["daimon", "local"], ids=["outside-agent-posting-in", "own-agent"]
)
async def test_an_isolated_channels_routine_never_leaves_it(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    agent: str,
) -> None:
    from slack_sdk.errors import SlackApiError
    from slack_sdk.web.async_slack_response import AsyncSlackResponse

    row = await _routine(db_session, kind="channel", destination_id="C1")
    row = row.model_copy(update={"agent_name": agent})
    await set_fields(
        db_session,
        scope=ChannelScopeRef(tenant_id=row.tenant_id, channel_id="C1"),
        tenant_id=row.tenant_id,
        agent_name="local",
        mode="agent",
    )
    await set_access_policy(
        db_session,
        tenant_id=row.tenant_id,
        policy=TenantAccessPolicy(
            sealed_channel_ids=("C1",),
            isolated_channel_ids=("C1",),
            agent_channel_pins={"local": ("C1",)},
        ),
    )
    await db_session.commit()
    post, client = _poster(db_session_factory, monkeypatch)
    response = MagicMock(spec=AsyncSlackResponse)
    response.data = {"ok": False, "error": "not_in_channel"}
    client.chat_postMessage = AsyncMock(side_effect=SlackApiError("not_in_channel", response))

    outcome = await post(row)

    assert (outcome.status, outcome.note) == ("skipped", "destination_unavailable")
    client.conversations_open.assert_not_awaited()


async def test_no_dm_for_a_creator_the_dm_policy_excludes(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from daimon.core.config import DirectMessagePolicy

    row = await _routine(db_session, kind="thread", destination_id="C1")  # malformed thread
    _post, client = _poster(db_session_factory, monkeypatch)
    runtime = cast(
        SlackRuntime,
        SimpleNamespace(
            sessionmaker=db_session_factory,
            settings=SimpleNamespace(
                direct_message_policies={row.tenant_id: DirectMessagePolicy(mode="disabled")}
            ),
            deployment_default=DeploymentDefault(),
        ),
    )

    outcome = await make_slack_routine_poster(runtime)(row)

    assert (outcome.status, outcome.note) == ("skipped", "destination_unavailable")
    client.chat_postMessage.assert_not_awaited()


@pytest.mark.parametrize(
    ("destination_id", "kind", "policy", "unreadable", "note"),
    [
        (
            "C_ANN",
            "channel",
            TenantAccessPolicy(protected_channel_ids=("C_ANN",), invoker_user_ids=("OTHER",)),
            False,
            "invoker_not_allowed",
        ),
        (
            "C1",
            "thread",
            TenantAccessPolicy(invoker_user_ids=("OTHER",)),
            False,
            "invoker_not_allowed",
        ),
        ("C1", "thread", None, True, "access_policy_unreadable"),
    ],
)
async def test_a_creator_who_is_not_cleared_gets_nothing_not_even_a_dm(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    destination_id: str,
    kind: str,
    policy: TenantAccessPolicy | None,
    unreadable: bool,
    note: str,
) -> None:
    """Review regression (round 2): protected/unavailable (a malformed thread
    here) must not become a DM for a creator the policy no longer clears."""
    import daimon.core.routine_delivery as delivery_mod
    from daimon.core.stores.access_policy import AccessPolicyUnreadable

    row = await _routine(db_session, kind=kind, destination_id=destination_id)
    if policy is not None:
        await set_access_policy(db_session, tenant_id=row.tenant_id, policy=policy)
        await db_session.commit()
    if unreadable:

        async def unreadable_policy(*args: object, **kwargs: object) -> TenantAccessPolicy:
            raise AccessPolicyUnreadable(tenant_id=row.tenant_id)

        monkeypatch.setattr(delivery_mod, "load_access_policy", unreadable_policy)
    post, client = _poster(db_session_factory, monkeypatch)

    outcome = await post(row)

    assert (outcome.status, outcome.note) == ("skipped", note)
    client.chat_postMessage.assert_not_awaited()
    client.conversations_open.assert_not_awaited()


async def test_a_private_channel_the_creator_is_not_in_gets_their_dm_not_a_post(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review regression (round 3): daimon is in the private channel, the
    creator is not — the routine must not post there for them."""
    row = await _routine(db_session, kind="channel", destination_id="C_PRIV")
    post, client = _poster(db_session_factory, monkeypatch)
    client.conversations_info = AsyncMock(
        return_value={"channel": {"id": "C_PRIV", "is_private": True}}
    )
    client.conversations_members = AsyncMock(return_value={"members": ["U_SOMEONE_ELSE"]})

    outcome = await post(row)

    assert outcome.note == "dm_fallback:creator_cannot_post"
    (call,) = client.chat_postMessage.await_args_list
    assert call.kwargs["channel"] == "D_CREATOR", "only the creator's own DM, never C_PRIV"


@pytest.mark.parametrize("deleted_as", ["error", "empty"])
async def test_a_deleted_thread_is_not_posted_at_the_channel_root(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    deleted_as: str,
) -> None:
    """Review regression (round 3): chat.postMessage accepts a missing
    thread_ts and posts at the channel root; the thread is checked first."""
    from slack_sdk.errors import SlackApiError
    from slack_sdk.web.async_slack_response import AsyncSlackResponse

    row = await _routine(db_session, kind="thread", destination_id="C1:1717.5")
    post, client = _poster(db_session_factory, monkeypatch)
    if deleted_as == "error":
        response = MagicMock(spec=AsyncSlackResponse)
        response.data = {"ok": False, "error": "thread_not_found"}
        client.conversations_replies = AsyncMock(
            side_effect=SlackApiError("thread_not_found", response)
        )
    else:
        client.conversations_replies = AsyncMock(return_value={"messages": []})

    outcome = await post(row)

    assert outcome.note == "dm_fallback:destination_unavailable"
    channels = [c.kwargs["channel"] for c in client.chat_postMessage.await_args_list]
    assert channels == ["D_CREATOR"], "nothing posted to C1"
