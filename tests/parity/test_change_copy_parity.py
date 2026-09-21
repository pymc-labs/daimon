"""Scenario: the Discord adapter's configuration-change acks are the core
renderer's own output, byte-for-byte -- never adapter-side hand-written copy.

`daimon.core.continuity.messages.render_change_confirmation` is the single
place person-facing configuration-change copy is written; every adapter ack
site is required to call it rather than compose its own string. This file
drives the Discord key-add entry point on `credential_modals.py`, the one
module that still owns a configuration-change ack after the legacy setup
editor was removed, and asserts the posted text equals a fresh, independent
call to the renderer with the same fields. The remaining ack sites
(model/instructions, mcp/mcp_removed, skill_removed, repo) are covered the
same way as unit tests colocated with each adapter module
(`packages/adapters/discord/tests/...`); this file is the cross-adapter
parity anchor, not the exhaustive sweep.

A posted control has no ack of its own: its receipt is the card the request
was posted as, re-rendered in place. So the key-add case below reads the
card the form edited rather than an ephemeral, and compares it to the same
renderer call -- the card adds a state marker to the first line and nothing
else.

Each test function name carries `discord` so the Slack half of this suite
(added separately, to the same file) never collides with these.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import discord
from daimon.adapters.discord.credential_modals import EnvCredentialModal
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.continuity.messages import ConfigurationChange, render_change_confirmation
from daimon.core.credential_requests import mint_request_token
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.posted_controls import classify_card_state
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.agent_files import list_agent_files
from daimon.core.stores.credential_requests import create_credential_request
from daimon.testing import ma_agent
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class _AsyncIter:
    def __init__(self, items: list[object]) -> None:
        self._items = iter(items)

    def __aiter__(self) -> _AsyncIter:
        return self

    async def __anext__(self) -> object:
        try:
            return next(self._items)
        except StopIteration as err:
            raise StopAsyncIteration from err


def _make_runtime(
    sessionmaker: async_sessionmaker[AsyncSession], *, agents: list[object] | None = None
) -> DiscordRuntime:
    settings = MagicMock()
    settings.mcp.public_url = None
    agent_list = list(agents or [])
    anthropic = AsyncMock()

    # A fresh iterator per call: this stub client's agent list is walked more
    # than once per submit (the target-availability gate, then the ack's own
    # name lookup), and a single shared iterator would starve the second walk.
    def _list_agents(**_kwargs: object) -> _AsyncIter:
        return _AsyncIter(list(agent_list))

    anthropic.beta.agents.list = MagicMock(side_effect=_list_agents)
    return DiscordRuntime(
        settings=settings,
        anthropic=anthropic,
        sessionmaker=sessionmaker,
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
        turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]  # never runs a turn
    )


def _interaction(*, user_id: int = 100000000000000001, guild_id: int | None = None) -> MagicMock:
    """A live guild-admin interaction by default -- every write below routes
    through `refuse_if_shared_and_not_admin`, which admits a guild admin
    without any tenant read."""
    interaction = MagicMock()
    interaction.guild_id = guild_id
    interaction.user = MagicMock(spec=discord.Member)
    interaction.user.id = user_id
    interaction.user.guild_permissions.administrator = True
    interaction.user.guild_permissions.manage_guild = False
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    return interaction


def _card_interaction(*, user_id: int, guild_id: int) -> MagicMock:
    """An interaction on the posted card of a request minted with one."""
    interaction = _interaction(user_id=user_id, guild_id=guild_id)
    interaction.type = discord.InteractionType.modal_submit
    interaction.message = None
    interaction.channel_id = 333
    interaction.channel = MagicMock(spec=discord.Thread)
    interaction.channel.parent_id = 222
    _card_message(interaction).edit = AsyncMock()
    return interaction


def _card_message(interaction: MagicMock) -> MagicMock:
    channel = interaction.client.get_partial_messageable.return_value
    return channel.get_partial_message.return_value  # pyright: ignore[reportAny]


def _rendered_card(interaction: MagicMock) -> str:
    """The text of the last card the modal re-rendered."""
    view = _card_message(interaction).edit.call_args.kwargs["view"]
    return "\n".join(
        item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay)
    )


async def test_discord_env_key_add_ack_matches_core_renderer(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = "800000001"
    requester_user_id = 100000000000000042
    ma_agent_id = "ag_parity_copy"
    token = mint_request_token()
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, platform="discord", workspace_id=workspace_id)
        row = await create_credential_request(
            session,
            token=token,
            kind="env",
            tenant_id=tenant.id,
            agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=ma_agent_id),
            account_id=uuid.uuid4(),
            target="STRIPE_KEY",
            mcp_server_url=None,
            requester_platform_user_id=str(requester_user_id),
            channel_id="333",
            platform="discord",
            parent_channel_id="222",
            origin_thread_id="333",
            posted_message_id="444",
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
            idempotency_key=uuid.uuid4(),
            target_ma_agent_id=ma_agent_id,
            target_name="stripe-bot",
            requested_work="finish the payout reconciliation",
        )

    agent = ma_agent(id=ma_agent_id, name="stripe-bot", tenant_id=tenant.id)
    runtime = _make_runtime(db_session_factory, agents=[agent])
    modal = EnvCredentialModal(runtime=runtime, request_row=row)
    modal.value_input._value = "sk_live_do_not_leak"  # pyright: ignore[reportPrivateUsage]

    interaction = _card_interaction(user_id=requester_user_id, guild_id=int(workspace_id))
    await modal.on_submit(interaction)

    posted = _rendered_card(interaction)
    expected = render_change_confirmation(
        ConfigurationChange(
            target_name="stripe-bot", kind="key", availability="next_message", detail="STRIPE_KEY"
        )
    )
    headline, *facts = expected.split("\n")
    posted_headline, *posted_facts = posted.split("\n")
    assert classify_card_state(posted_headline.strip("*")) == "applied", (
        f"the card must mark the state on its first line, got {posted_headline!r}"
    )
    assert headline in posted_headline, (
        f"the headline must be the renderer's own first line, got {posted_headline!r}"
    )
    assert [fact.removeprefix("-# ") for fact in posted_facts] == facts, (
        f"the rest must be the renderer's own copy, got {posted!r}"
    )
    interaction.followup.send.assert_not_awaited()

    async with db_session_factory() as session:
        stored = await list_agent_files(session, tenant_id=row.tenant_id, agent_id=row.agent_id)
    assert [f.key for f in stored] == ["STRIPE_KEY"], "sanity: the key write actually happened"
