"""End-to-end proof that a wizard Submit tap spawns exactly one billed,
session-reusing turn through the shared admission/binding/run chokepoint.

Drives `WizardSubmitButton` directly (the real dispatch entry point a Discord
component interaction triggers) against a real `DaimonBot`/`DiscordRuntime`
built the way `tests/parity/drivers/discord_driver.py` and
`tests/integration/test_discord_turn_e2e.py` build one: real Postgres, a
transport-level fake `AsyncAnthropic` (`MARouter` + SSE), and only
`daimon.core.turn.prepare.create_session` boundary-stubbed (Discord-API-only
session provisioning unrelated to what this suite tests). `admit`,
`bind_session`, and `run_prepared_turn` are NEVER patched -- the real
chokepoint runs on every test here, which is the whole point of this file.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import httpx
import pytest
from anthropic.types.beta import BetaEnvironment, BetaManagedAgentsAgent, BetaManagedAgentsSession
from anthropic.types.beta.beta_managed_agents_model_config import BetaManagedAgentsModelConfig
from anthropic.types.beta.beta_managed_agents_session_agent import BetaManagedAgentsSessionAgent
from anthropic.types.beta.beta_managed_agents_session_stats import BetaManagedAgentsSessionStats
from anthropic.types.beta.beta_managed_agents_session_usage import BetaManagedAgentsSessionUsage
from anthropic.types.beta.sessions.beta_managed_agents_agent_message_event import (
    BetaManagedAgentsAgentMessageEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_session_end_turn import (
    BetaManagedAgentsSessionEndTurn,
)
from anthropic.types.beta.sessions.beta_managed_agents_session_status_idle_event import (
    BetaManagedAgentsSessionStatusIdleEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_span_model_request_end_event import (
    BetaManagedAgentsSpanModelRequestEndEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_span_model_usage import (
    BetaManagedAgentsSpanModelUsage,
)
from anthropic.types.beta.sessions.beta_managed_agents_text_block import (
    BetaManagedAgentsTextBlock,
)
from daimon.adapters.discord.bot import DaimonBot
from daimon.adapters.discord.runtime import DiscordRuntime, build_turn_deps
from daimon.adapters.discord.wizard_submit import WizardSubmitButton
from daimon.core.config import McpSettings
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME, MA_METADATA_KEY_TENANT
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import tenant_ledger, usage_events
from daimon.core.stores.accounts import get_account
from daimon.core.stores.domain import TenantRow, WizardSessionRow
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.thread_sessions import get_live_thread_session
from daimon.core.stores.wizard_session import get_wizard_session
from daimon.core.wizard.answers import format_answer_block
from daimon.core.wizard.spec import Option, Step, StepKind, WizardSpec
from daimon.core.wizard.state import WizardState, WizardStatus, build_custom_id
from daimon.testing.factories import make_tenant, make_thread_session, make_wizard_session
from daimon.testing.ma import (
    EMPTY_CLOUD_CONFIG,
    MARouter,
    build_fake_anthropic,
    json_body,
    list_response,
    sse_response,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.asyncio

_AGENT_ID = "ag_wizard_submit_test"
_ENV_ID = "env_wizard_submit_test"
_MODEL_ID = "claude-sonnet-4-6"
_AGENT_TEXT = "Thanks -- here's your plan!"
_REQUESTER_ID = "700000000000000001"
_MESSAGE_ID = "900000000000000003"


def _spec() -> WizardSpec:
    return WizardSpec(
        prompt="Plan the picnic",
        steps=[
            Step(
                key="colour",
                question="Favourite colour?",
                kind=StepKind.CHOICE,
                options=[Option(label="Red", value="red"), Option(label="Blue", value="blue")],
            ),
        ],
    )


def _build_router(
    tenant_id_str: str,
    *,
    sent_events: list[dict[str, Any]],
    stream_hits: list[str],
) -> MARouter:
    """MARouter handling agent/environment resolution plus a turn SSE stream
    that emits an `agent.message`, a `span.model_request_end` (the event the
    billing chokepoint metering binds on), then `session.status_idle`.
    `sent_events` records every `POST .../events` body (the resumed turn's
    user message); `stream_hits` records every session id the SSE stream was
    opened against (which session id the turn actually ran on)."""
    agent_item = BetaManagedAgentsAgent(
        id=_AGENT_ID,
        type="agent",
        name="test-agent",
        model=BetaManagedAgentsModelConfig(id=_MODEL_ID),
        metadata={MA_METADATA_KEY_TENANT: tenant_id_str, MA_METADATA_KEY_NAME: "test-agent"},
        description=None,
        created_at="2026-06-14T00:00:00Z",  # pyright: ignore[reportArgumentType]
        updated_at="2026-06-14T00:00:00Z",  # pyright: ignore[reportArgumentType]
        version=1,
        mcp_servers=[],
        skills=[],
        tools=[],
        system=None,
    ).model_dump(mode="json")

    env_item = BetaEnvironment(
        id=_ENV_ID,
        type="environment",
        name="test-env",
        config=EMPTY_CLOUD_CONFIG,
        metadata={MA_METADATA_KEY_TENANT: tenant_id_str, MA_METADATA_KEY_NAME: "test-env"},
        description="",
        created_at="2026-06-14T00:00:00Z",
        updated_at="2026-06-14T00:00:00Z",
    ).model_dump(mode="json")

    now = datetime.now(UTC)
    agent_message_event = BetaManagedAgentsAgentMessageEvent(
        id="evt_wizard_submit_msg",
        type="agent.message",
        processed_at=now,
        content=[BetaManagedAgentsTextBlock(type="text", text=_AGENT_TEXT)],
    ).model_dump(mode="json")
    model_request_end_event = BetaManagedAgentsSpanModelRequestEndEvent(
        id="evt_wizard_submit_usage",
        is_error=False,
        model_request_start_id="start_wizard_submit",
        model_usage=BetaManagedAgentsSpanModelUsage(
            input_tokens=100,
            output_tokens=50,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
        processed_at=now,
        type="span.model_request_end",
    ).model_dump(mode="json")
    idle_event = BetaManagedAgentsSessionStatusIdleEvent(
        id="evt_wizard_submit_idle",
        type="session.status_idle",
        processed_at=now,
        stop_reason=BetaManagedAgentsSessionEndTurn(type="end_turn"),
    ).model_dump(mode="json")

    def _handle_send_events(request: httpx.Request, _match: re.Match[str]) -> httpx.Response:
        sent_events.append(json_body(request))
        return httpx.Response(200, json={"data": None})

    def _handle_stream(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        stream_hits.append(match["session_id"])
        return sse_response([agent_message_event, model_request_end_event, idle_event])

    router = MARouter()
    router.add("GET", r"/v1/agents", lambda req, _m: list_response([agent_item]))
    router.add("GET", r"/v1/agents/[^/]+", lambda req, _m: httpx.Response(200, json=agent_item))
    router.add("GET", r"/v1/environments", lambda req, _m: list_response([env_item]))
    router.add("GET", r"/v1/environments/[^/]+", lambda req, _m: httpx.Response(200, json=env_item))
    router.add("POST", r"/v1/sessions/(?P<session_id>[^/]+)/events", _handle_send_events)
    router.add("GET", r"/v1/sessions/(?P<session_id>[^/]+)/events/stream", _handle_stream)
    return router


def _make_runtime(
    sessionmaker: async_sessionmaker[AsyncSession], router: MARouter
) -> DiscordRuntime:
    settings = MagicMock()
    settings.mcp = McpSettings()
    settings.billing.markup = Decimal("1.0")
    settings.billing.signup_credit = Decimal("0")
    settings.crypto.keys = []
    settings.github.oauth_scopes = ()
    discord_settings = MagicMock()
    discord_settings.max_concurrent_turns_per_tenant = 100
    discord_settings.bot_display_name = "daimon"
    discord_settings.per_caller_thread_sessions = True
    settings.discord = discord_settings

    anthropic = build_fake_anthropic(router.dispatch)
    resolver_cache = new_resolver_cache()
    deployment_default = DeploymentDefault(agent_name="test-agent", environment_name="test-env")
    return DiscordRuntime(
        settings=settings,
        anthropic=anthropic,
        sessionmaker=sessionmaker,
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,  # billing disabled -> is_over_cap always False
        deployment_default=deployment_default,
        resolver_cache=resolver_cache,
        turn_deps=build_turn_deps(
            settings,
            anthropic,
            sessionmaker,
            deployment_default=deployment_default,
            resolver_cache=resolver_cache,
            billing_config=None,
        ),
    )


def _make_bot(runtime: DiscordRuntime) -> DaimonBot:
    intents = discord.Intents.default()
    intents.message_content = True
    bot = DaimonBot(runtime=runtime, intents=intents)
    bot._connection.user = MagicMock(spec=discord.ClientUser)  # pyright: ignore[reportPrivateUsage]
    bot._connection.user.id = 999  # pyright: ignore[reportPrivateUsage]
    return bot


def _make_channel(*, thread_id: int, parent_id: int) -> MagicMock:
    channel = MagicMock(spec=discord.Thread)
    channel.id = thread_id
    channel.parent_id = parent_id
    message_ref = MagicMock()
    message_ref.id = 42
    message_ref.edit = AsyncMock()
    channel.send = AsyncMock(return_value=message_ref)
    return channel


def _interaction(
    *, user_id: str, client: DaimonBot, channel: discord.Thread, guild_id: int
) -> MagicMock:
    interaction = MagicMock()
    interaction.user = (
        MagicMock()
    )  # no spec=Member -> isinstance(..., Member) is False, is_admin=False
    interaction.user.id = int(user_id)
    interaction.client = client
    interaction.message.id = int(_MESSAGE_ID)
    interaction.channel = channel
    interaction.guild_id = guild_id
    interaction.guild = MagicMock(spec=discord.Guild)
    interaction.guild.owner_id = 0
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.response.is_done = MagicMock(return_value=False)
    interaction.edit_original_response = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


def _submit_match(short_id: str) -> re.Match[str]:
    custom_id = build_custom_id(short_id, "submit")
    matched = WizardSubmitButton.__discord_ui_compiled_template__.fullmatch(custom_id)
    assert matched is not None, "test action must satisfy WizardSubmitButton's own template"
    return matched


def _make_fake_session(*, session_id: str) -> BetaManagedAgentsSession:
    now = datetime.now(UTC)
    return BetaManagedAgentsSession(
        id=session_id,
        agent=BetaManagedAgentsSessionAgent(
            id=_AGENT_ID,
            mcp_servers=[],
            model=BetaManagedAgentsModelConfig(id=_MODEL_ID),
            name="test-agent",
            skills=[],
            tools=[],
            type="agent",
            version=1,
        ),
        created_at=now,
        environment_id=_ENV_ID,
        metadata={},
        resources=[],
        stats=BetaManagedAgentsSessionStats(),
        status="idle",
        type="session",
        updated_at=now,
        usage=BetaManagedAgentsSessionUsage(),
        vault_ids=[],
        outcome_evaluations=[],
    )


async def _seed_funded_tenant(db_session: AsyncSession, *, workspace_id: str) -> TenantRow:
    tenant = await make_tenant(db_session, platform="discord", workspace_id=workspace_id)
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant.id,
        delta_usd=Decimal("100.00"),
        reason="trial",
        idempotency_key=f"trial:{tenant.id}",
    )
    await db_session.commit()
    return tenant


async def _seed_review_row(
    db_session_factory: async_sessionmaker[AsyncSession], *, tenant: TenantRow
) -> WizardSessionRow:
    async with db_session_factory() as session, session.begin():
        return await make_wizard_session(
            session,
            tenant=tenant,
            requester_platform_user_id=_REQUESTER_ID,
            message_id=_MESSAGE_ID,
            spec=_spec().model_dump(mode="json"),
            answers={"colour": ["red"]},
            current_step=1,  # one past the last step index -> the review screen
            status="open",
        )


# --- session reuse -----------------------------------------------------------


async def test_submitting_a_form_resumes_the_threads_existing_session(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await _seed_funded_tenant(db_session, workspace_id="800001001")
    row = await _seed_review_row(db_session_factory, tenant=tenant)

    channel = _make_channel(thread_id=5001, parent_id=4001)
    existing_session_id = "sess_already_live"
    async with db_session_factory() as session, session.begin():
        principal = await get_or_create_platform_principal(
            session, tenant_id=tenant.id, platform="discord", external_id=_REQUESTER_ID
        )
        account = await get_account(session, principal.account_id)
        assert account is not None
        existing = await make_thread_session(
            session,
            tenant=tenant,
            account=account,
            platform="discord",
            thread_id=str(channel.id),
            ma_session_id=existing_session_id,
        )

    sent_events: list[dict[str, Any]] = []
    stream_hits: list[str] = []
    router = _build_router(str(tenant.id), sent_events=sent_events, stream_hits=stream_hits)
    runtime = _make_runtime(db_session_factory, router)
    bot = _make_bot(runtime)
    interaction = _interaction(user_id=_REQUESTER_ID, client=bot, channel=channel, guild_id=123)

    with patch("daimon.core.turn.prepare.create_session") as mock_create_session:
        item = await WizardSubmitButton.from_custom_id(
            interaction, MagicMock(), _submit_match(row.id)
        )
        assert await item.interaction_check(interaction) is True
        await item.callback(interaction)

        tasks = list(bot._bg_tasks)  # pyright: ignore[reportPrivateUsage]  # asserting the spawn contract
        assert len(tasks) == 1, "a winning claim must spawn exactly one background task"
        await asyncio.gather(*tasks)

        mock_create_session.assert_not_called()

    assert stream_hits == [existing_session_id], (
        "the resumed turn must run against the ALREADY-LIVE session id, not a fresh one"
    )
    async with db_session_factory() as session:
        after = await get_live_thread_session(
            session,
            tenant_id=tenant.id,
            platform="discord",
            thread_id=str(channel.id),
            account_id=principal.account_id,
        )
    assert after is not None
    assert after.id == existing.id, (
        "no new thread_sessions row must be created for a reused session"
    )
    assert after.ma_session_id == existing_session_id


# --- answer block as the user message ----------------------------------------


async def test_submitting_a_form_sends_the_keyed_answer_block_as_the_user_message(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await _seed_funded_tenant(db_session, workspace_id="800002001")
    row = await _seed_review_row(db_session_factory, tenant=tenant)

    channel = _make_channel(thread_id=5002, parent_id=4002)
    sent_events: list[dict[str, Any]] = []
    stream_hits: list[str] = []
    router = _build_router(str(tenant.id), sent_events=sent_events, stream_hits=stream_hits)
    runtime = _make_runtime(db_session_factory, router)
    bot = _make_bot(runtime)
    interaction = _interaction(user_id=_REQUESTER_ID, client=bot, channel=channel, guild_id=123)

    with patch("daimon.core.turn.prepare.create_session") as mock_create_session:
        mock_create_session.return_value = _make_fake_session(session_id="sess_fresh_msg")
        item = await WizardSubmitButton.from_custom_id(
            interaction, MagicMock(), _submit_match(row.id)
        )
        assert await item.interaction_check(interaction) is True
        await item.callback(interaction)
        await asyncio.gather(*list(bot._bg_tasks))  # pyright: ignore[reportPrivateUsage]  # asserting the spawn contract

    assert sent_events, "the resumed turn must post at least one user.message event"
    expected = format_answer_block(
        _spec(),
        WizardState(
            short_id=row.id,
            current_step=row.current_step,
            answers=row.answers,
            status=WizardStatus.SUBMITTED,
        ),
    )
    sent_texts = [
        block["text"]
        for event in sent_events
        for block in event["events"][0]["content"]
        if block.get("type") == "text"
    ]
    assert expected in sent_texts, (
        f"expected the fenced, JSON-keyed answer block among the sent user.message texts, "
        f"got: {sent_texts}"
    )
    for text in sent_texts:
        assert "Favourite colour?" not in text, (
            "the resumed turn's message must carry raw answer VALUES keyed by step, not "
            "restated question prose -- the agent that authored the spec already knows the "
            "question text"
        )


# --- billing chokepoint --------------------------------------------------------


async def test_submitting_a_form_records_usage(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await _seed_funded_tenant(db_session, workspace_id="800003001")
    row = await _seed_review_row(db_session_factory, tenant=tenant)

    channel = _make_channel(thread_id=5003, parent_id=4003)
    sent_events: list[dict[str, Any]] = []
    stream_hits: list[str] = []
    router = _build_router(str(tenant.id), sent_events=sent_events, stream_hits=stream_hits)
    runtime = _make_runtime(db_session_factory, router)
    bot = _make_bot(runtime)
    interaction = _interaction(user_id=_REQUESTER_ID, client=bot, channel=channel, guild_id=123)

    with patch("daimon.core.turn.prepare.create_session") as mock_create_session:
        mock_create_session.return_value = _make_fake_session(session_id="sess_fresh_usage")
        item = await WizardSubmitButton.from_custom_id(
            interaction, MagicMock(), _submit_match(row.id)
        )
        assert await item.interaction_check(interaction) is True
        await item.callback(interaction)
        await asyncio.gather(*list(bot._bg_tasks))  # pyright: ignore[reportPrivateUsage]  # asserting the spawn contract

    usage_rows = await usage_events.list_for_tenant(db_session, tenant_id=tenant.id)
    assert len(usage_rows) == 1, (
        "the resumed turn must write exactly one usage_events row -- proof it ran through "
        "the shared billing chokepoint rather than a private turn path"
    )
    assert usage_rows[0].model == _MODEL_ID


async def test_two_concurrent_submits_bill_exactly_one_turn(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await _seed_funded_tenant(db_session, workspace_id="800004001")
    row = await _seed_review_row(db_session_factory, tenant=tenant)

    channel = _make_channel(thread_id=5004, parent_id=4004)
    sent_events: list[dict[str, Any]] = []
    stream_hits: list[str] = []
    router = _build_router(str(tenant.id), sent_events=sent_events, stream_hits=stream_hits)
    runtime = _make_runtime(db_session_factory, router)
    bot = _make_bot(runtime)
    interaction_a = _interaction(user_id=_REQUESTER_ID, client=bot, channel=channel, guild_id=123)
    interaction_b = _interaction(user_id=_REQUESTER_ID, client=bot, channel=channel, guild_id=123)

    with patch("daimon.core.turn.prepare.create_session") as mock_create_session:
        mock_create_session.return_value = _make_fake_session(session_id="sess_fresh_concurrent")
        item_a = await WizardSubmitButton.from_custom_id(
            interaction_a, MagicMock(), _submit_match(row.id)
        )
        item_b = await WizardSubmitButton.from_custom_id(
            interaction_b, MagicMock(), _submit_match(row.id)
        )
        assert await item_a.interaction_check(interaction_a) is True
        assert await item_b.interaction_check(interaction_b) is True

        await asyncio.gather(item_a.callback(interaction_a), item_b.callback(interaction_b))

        tasks = list(bot._bg_tasks)  # pyright: ignore[reportPrivateUsage]  # asserting the spawn contract
        assert len(tasks) == 1, (
            "only the winning claim may spawn a background turn -- a second spawned task "
            "here would mean a second billed turn"
        )
        await asyncio.gather(*tasks)

    usage_rows = await usage_events.list_for_tenant(db_session, tenant_id=tenant.id)
    assert len(usage_rows) == 1, (
        "two concurrent submits must bill exactly ONE turn -- a second usage_events row "
        "here would be a real double charge against the tenant's balance"
    )


# --- per-tenant concurrency cap ---------------------------------------------------


async def test_a_submit_turn_claims_and_releases_a_per_tenant_in_flight_slot(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A wizard turn costs the same upstream capacity as a mention turn, so it
    counts against the same cap -- and must give the slot back."""
    tenant = await _seed_funded_tenant(db_session, workspace_id="800006001")
    row = await _seed_review_row(db_session_factory, tenant=tenant)

    channel = _make_channel(thread_id=5006, parent_id=4006)
    sent_events: list[dict[str, Any]] = []
    stream_hits: list[str] = []
    router = _build_router(str(tenant.id), sent_events=sent_events, stream_hits=stream_hits)
    runtime = _make_runtime(db_session_factory, router)
    bot = _make_bot(runtime)
    interaction = _interaction(user_id=_REQUESTER_ID, client=bot, channel=channel, guild_id=123)

    with patch("daimon.core.turn.prepare.create_session") as mock_create_session:
        mock_create_session.return_value = _make_fake_session(session_id="sess_fresh_slot")
        item = await WizardSubmitButton.from_custom_id(
            interaction, MagicMock(), _submit_match(row.id)
        )
        assert await item.interaction_check(interaction) is True
        await item.callback(interaction)
        await asyncio.gather(*list(bot._bg_tasks))  # pyright: ignore[reportPrivateUsage]  # asserting the spawn contract

    assert stream_hits, "the turn must actually have run"
    assert bot._inflight == {}, (  # pyright: ignore[reportPrivateUsage]  # asserting the cap bookkeeping contract
        "the in-flight slot the turn claimed must be released once it finishes"
    )


async def test_a_submit_over_the_per_tenant_cap_runs_no_turn_and_says_so(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await _seed_funded_tenant(db_session, workspace_id="800007001")
    row = await _seed_review_row(db_session_factory, tenant=tenant)

    channel = _make_channel(thread_id=5007, parent_id=4007)
    sent_events: list[dict[str, Any]] = []
    stream_hits: list[str] = []
    router = _build_router(str(tenant.id), sent_events=sent_events, stream_hits=stream_hits)
    runtime = _make_runtime(db_session_factory, router)
    runtime.settings.discord.max_concurrent_turns_per_tenant = 1
    bot = _make_bot(runtime)
    bot._inflight[tenant.id] = 1  # pyright: ignore[reportPrivateUsage]  # simulates a mention turn already holding this tenant's only slot
    interaction = _interaction(user_id=_REQUESTER_ID, client=bot, channel=channel, guild_id=123)

    with patch("daimon.core.turn.prepare.create_session"):
        item = await WizardSubmitButton.from_custom_id(
            interaction, MagicMock(), _submit_match(row.id)
        )
        assert await item.interaction_check(interaction) is True
        await item.callback(interaction)
        await asyncio.gather(*list(bot._bg_tasks))  # pyright: ignore[reportPrivateUsage]  # asserting the spawn contract

    assert stream_hits == [], "an over-cap submit must never reach the SSE turn stream"
    assert bot._inflight == {tenant.id: 1}, (  # pyright: ignore[reportPrivateUsage]  # asserting the cap bookkeeping contract
        "a refused turn must not claim (or release) a slot it never took"
    )

    posted_texts = [
        call.args[0]
        for call in channel.send.call_args_list
        if call.args and isinstance(call.args[0], str)
    ]
    assert any("recorded" in text and "in flight" in text for text in posted_texts), (
        f"the refusal must say the answers were recorded and the server is busy, "
        f"got: {posted_texts}"
    )

    usage_rows = await usage_events.list_for_tenant(db_session, tenant_id=tenant.id)
    assert usage_rows == [], "an over-cap refusal must write zero usage_events rows"


# --- post-hoc admission refusal -------------------------------------------------


async def test_a_submitter_over_balance_is_told_and_no_turn_runs(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    # Deliberately NOT funded via _seed_funded_tenant -- an empty ledger sums
    # to Decimal("0"), and is_over_balance treats balance <= 0 as depleted
    # (mirrors tests/parity/test_turn_blocked_balance.py).
    tenant = await make_tenant(db_session, platform="discord", workspace_id="800005001")
    await db_session.commit()
    row = await _seed_review_row(db_session_factory, tenant=tenant)

    channel = _make_channel(thread_id=5005, parent_id=4005)
    sent_events: list[dict[str, Any]] = []
    stream_hits: list[str] = []
    router = _build_router(str(tenant.id), sent_events=sent_events, stream_hits=stream_hits)
    runtime = _make_runtime(db_session_factory, router)
    bot = _make_bot(runtime)
    interaction = _interaction(user_id=_REQUESTER_ID, client=bot, channel=channel, guild_id=123)

    with patch("daimon.core.turn.prepare.create_session") as mock_create_session:
        item = await WizardSubmitButton.from_custom_id(
            interaction, MagicMock(), _submit_match(row.id)
        )
        assert await item.interaction_check(interaction) is True
        await item.callback(interaction)
        tasks = list(bot._bg_tasks)  # pyright: ignore[reportPrivateUsage]  # asserting the spawn contract
        assert len(tasks) == 1, (
            "the claim must still win and spawn the turn -- admission runs AFTER the claim"
        )
        await asyncio.gather(*tasks)
        mock_create_session.assert_not_called()

    assert stream_hits == [], "an over-balance refusal must never reach the SSE turn stream"

    posted_texts = [
        call.args[0]
        for call in channel.send.call_args_list
        if call.args and isinstance(call.args[0], str)
    ]
    assert any("recorded" in text for text in posted_texts), (
        f"the refusal must say the answers were recorded, got: {posted_texts}"
    )
    assert any("credit is depleted" in text for text in posted_texts), (
        f"the refusal must reuse the existing credit-depleted copy, got: {posted_texts}"
    )

    usage_rows = await usage_events.list_for_tenant(db_session, tenant_id=tenant.id)
    assert usage_rows == [], "an over-balance refusal must write zero usage_events rows"

    async with db_session_factory() as session:
        after = await get_wizard_session(session, short_id=row.id)
    assert after is not None
    assert after.status == "submitted", (
        "the row must stay claimed -- the claim commits before admission runs, so a "
        "post-hoc refusal must not lose the submitter's answers"
    )


# --- per-turn ceiling (19-04) -------------------------------------------------


async def test_a_ceiling_outcome_takes_the_existing_turn_error_branch(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A `run_prepared_turn` outcome carrying a `"ceiling"` error is not a
    special case for the wizard-submit path -- it takes the same
    `turn_state.error is not None` branch any other turn error takes: no
    watermark write, no unhandled exception out of the background task.
    """
    from daimon.core.errors import TurnError
    from daimon.core.turn.run import RunOutcome
    from daimon.core.turn.state import TurnState

    tenant = await _seed_funded_tenant(db_session, workspace_id="800008001")
    row = await _seed_review_row(db_session_factory, tenant=tenant)

    channel = _make_channel(thread_id=5008, parent_id=4008)
    sent_events: list[dict[str, Any]] = []
    stream_hits: list[str] = []
    router = _build_router(str(tenant.id), sent_events=sent_events, stream_hits=stream_hits)
    runtime = _make_runtime(db_session_factory, router)
    bot = _make_bot(runtime)
    interaction = _interaction(user_id=_REQUESTER_ID, client=bot, channel=channel, guild_id=123)

    ceiling_outcome = RunOutcome(
        state=TurnState(error=TurnError(kind="ceiling", message="this turn stopped responding")),
        ma_session_id="sess_ceiling_wizard",
        mapping_id=None,
        recovered=False,
    )

    with (
        patch("daimon.core.turn.prepare.create_session") as mock_create_session,
        patch(
            "daimon.adapters.discord.wizard_submit.run_prepared_turn", new_callable=AsyncMock
        ) as mock_run_prepared_turn,
    ):
        mock_create_session.return_value = _make_fake_session(session_id="sess_ceiling_wizard")
        mock_run_prepared_turn.return_value = ceiling_outcome
        item = await WizardSubmitButton.from_custom_id(
            interaction, MagicMock(), _submit_match(row.id)
        )
        assert await item.interaction_check(interaction) is True
        await item.callback(interaction)
        tasks = list(bot._bg_tasks)  # pyright: ignore[reportPrivateUsage]  # asserting the spawn contract
        assert len(tasks) == 1
        await asyncio.gather(*tasks)

    mock_run_prepared_turn.assert_called_once()
    call_kwargs = mock_run_prepared_turn.call_args.kwargs
    assert "deadline" in call_kwargs, "run_prepared_turn must be given the shared core deadline"

    async with db_session_factory() as session:
        after = await get_wizard_session(session, short_id=row.id)
    assert after is not None and after.status == "submitted", (
        "a ceiling turn error must not lose or revert the claimed row"
    )


async def test_a_bind_phase_ceiling_does_not_escape_the_background_task(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """`bind_session` raising the ceiling `TurnError` is caught by
    `run_wizard_submit_turn`'s own `except (DaimonError, ...)` boundary --
    same as any other bind-phase failure -- and rendered through the
    existing turn-failure path rather than escaping the background task.
    """
    from daimon.core.turn.ceiling import ceiling_error

    tenant = await _seed_funded_tenant(db_session, workspace_id="800009001")
    row = await _seed_review_row(db_session_factory, tenant=tenant)

    channel = _make_channel(thread_id=5009, parent_id=4009)
    sent_events: list[dict[str, Any]] = []
    stream_hits: list[str] = []
    router = _build_router(str(tenant.id), sent_events=sent_events, stream_hits=stream_hits)
    runtime = _make_runtime(db_session_factory, router)
    bot = _make_bot(runtime)
    interaction = _interaction(user_id=_REQUESTER_ID, client=bot, channel=channel, guild_id=123)

    with patch(
        "daimon.adapters.discord.wizard_submit.bind_session", new_callable=AsyncMock
    ) as mock_bind_session:
        mock_bind_session.side_effect = ceiling_error()
        item = await WizardSubmitButton.from_custom_id(
            interaction, MagicMock(), _submit_match(row.id)
        )
        assert await item.interaction_check(interaction) is True
        await item.callback(interaction)
        tasks = list(bot._bg_tasks)  # pyright: ignore[reportPrivateUsage]  # asserting the spawn contract
        assert len(tasks) == 1
        # No exception escapes -- the background task's own error boundary
        # catches TurnError (a DaimonError) and renders it instead.
        await asyncio.gather(*tasks)

    assert stream_hits == [], "a bind-phase ceiling must never reach the SSE turn stream"
    posted_texts = [
        call.args[0]
        for call in channel.send.call_args_list
        if call.args and isinstance(call.args[0], str)
    ]
    assert posted_texts, "the bind-phase ceiling must render through the existing turn-failure path"
