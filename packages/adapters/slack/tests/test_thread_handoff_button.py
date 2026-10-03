"""The Hand over button on a Slack thread whose channel now answers with another agent."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from daimon.adapters.slack import thread_handoff
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.adapters.slack.thread_handoff import HAND_OVER_ACTION_ID, handle_hand_over_click
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.scope import ChannelScopeRef, DeploymentDefault, ScopeContext
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.scoped_config_read import resolve
from daimon.core.stores.scoped_config_write import set_fields
from daimon.testing import ma_agent
from daimon.testing.factories import make_tenant
from daimon.testing.ma import MARouter, build_fake_anthropic
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from yarl import URL

_TEAM = "T_HANDOVER"
_CHANNEL = "C_PARENT"
_THREAD_TS = "1700000000.000100"
_DEFAULT = DeploymentDefault(agent_name="daimon")


def _payload() -> dict[str, Any]:
    return {
        "type": "block_actions",
        "team": {"id": _TEAM},
        "user": {"id": "U_CLICKER", "team_id": _TEAM},
        "channel": {"id": _CHANNEL},
        "container": {"channel_id": _CHANNEL, "message_ts": "1700000000.000200"},
        "message": {"ts": "1700000000.000200", "thread_ts": _THREAD_TS, "text": "notice"},
        "actions": [{"action_id": HAND_OVER_ACTION_ID, "value": "ag_research"}],
    }


async def _click(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    policy: TenantAccessPolicy | None = None,
) -> Any:
    tenant = await make_tenant(db_session, platform="slack", workspace_id=_TEAM)
    await set_fields(
        db_session,
        scope=ChannelScopeRef(tenant_id=tenant.id, channel_id=_CHANNEL),
        tenant_id=tenant.id,
        agent_name="research-bot",
    )
    if policy is not None:
        await set_access_policy(db_session, tenant_id=tenant.id, policy=policy)
    await db_session.commit()
    router = MARouter()
    router.add_agent(ma_agent(id="ag_research", name="research-bot", tenant_id=tenant.id))
    runtime = SimpleNamespace(
        anthropic=build_fake_anthropic(router.dispatch),
        sessionmaker=db_session_factory,
        deployment_default=_DEFAULT,
    )

    async def _client(_runtime: Any, *, team_id: str) -> Any:
        assert team_id == _TEAM
        return fake_slack_web_client.client

    monkeypatch.setattr(thread_handoff, "resolve_web_client", _client)
    await handle_hand_over_click(cast(SlackRuntime, runtime), _payload())
    return tenant


def _sent(fake_slack_web_client: Any, method: str) -> list[dict[str, Any]]:
    return [
        req.kwargs["json"]
        for (_m, url), reqs in fake_slack_web_client.mock.requests.items()
        if url == URL(f"https://slack.com/api/{method}")
        for req in reqs
    ]


async def test_a_click_hands_the_thread_to_the_channels_agent(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await _click(db_session, db_session_factory, fake_slack_web_client, monkeypatch)

    (posted,) = _sent(fake_slack_web_client, "chat.postMessage")
    assert posted["thread_ts"] == _THREAD_TS
    assert "research-bot answers in this conversation from the next message" in posted["text"]
    (updated,) = _sent(fake_slack_web_client, "chat.update")
    assert updated["blocks"] == [], "the notice loses its button"
    async with db_session_factory() as session:
        routed = await resolve(
            session,
            context=ScopeContext(
                tenant_id=tenant.id, channel_id=_CHANNEL, thread_id=_THREAD_TS, platform="slack"
            ),
            default=_DEFAULT,
        )
    assert routed.responder_ma_agent_id == "ag_research", "later messages go to research-bot"
    assert routed.thread_binding_kind == "handoff"


async def test_a_refused_click_answers_privately_and_writes_nothing(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await _click(
        db_session,
        db_session_factory,
        fake_slack_web_client,
        monkeypatch,
        policy=TenantAccessPolicy(protected_channel_ids=(_CHANNEL,)),
    )

    assert _sent(fake_slack_web_client, "chat.postMessage") == []
    (ephemeral,) = _sent(fake_slack_web_client, "chat.postEphemeral")
    assert ephemeral["user"] == "U_CLICKER"
    assert "Nothing was changed" in ephemeral["text"]
    async with db_session_factory() as session:
        routed = await resolve(
            session,
            context=ScopeContext(
                tenant_id=tenant.id, channel_id=_CHANNEL, thread_id=_THREAD_TS, platform="slack"
            ),
            default=_DEFAULT,
        )
    assert routed.thread_binding_id is None
