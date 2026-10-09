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
import json
import re
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import discord
import httpx
import pytest
from daimon.adapters.discord.bot import DaimonBot
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.adapters.discord.views import CancelView
from daimon.adapters.discord.wizard_submit import WizardSubmitButton
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.config import McpSettings, TurnQueueSettings
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import tenant_ledger, usage_events
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.accounts import get_account, set_role
from daimon.core.stores.agent_memory_stores import insert_memory_store
from daimon.core.stores.domain import Role, TenantRow, WizardSessionRow
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.thread_sessions import get_live_thread_session, list_orphaned_turns
from daimon.core.stores.turn_card_intents import list_recoverable_turn_card_intents
from daimon.core.stores.wizard_session import get_wizard_session
from daimon.core.turn.deps import build_turn_deps
from daimon.core.turn.prepare import bind_session
from daimon.core.turn.run import run_prepared_turn
from daimon.core.turn_queue import TurnTicket
from daimon.core.wizard.answers import format_answer_block
from daimon.core.wizard.spec import Option, Step, StepKind, WizardSpec
from daimon.core.wizard.state import WizardState, WizardStatus, build_custom_id
from daimon.testing import build_turn_router, ma_session
from daimon.testing.factories import make_tenant, make_thread_session, make_wizard_session
from daimon.testing.ma import (
    MARouter,
    build_fake_anthropic,
)
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
    """The shared turn router (agent/environment resolution plus a turn SSE
    stream whose `span.model_request_end` the billing chokepoint metering
    binds on) with a session retrieve route on top. `sent_events` records
    every `POST .../events` body (the resumed turn's user message);
    `stream_hits` records every session id the SSE stream was opened
    against (which session id the turn actually ran on)."""
    router = build_turn_router(
        tenant_id_str,
        agent_id=_AGENT_ID,
        env_id=_ENV_ID,
        model_id=_MODEL_ID,
        agent_text=_AGENT_TEXT,
        usage_event_id="evt_wizard_submit_usage",
        sent_event_bodies=sent_events,
        stream_hits=stream_hits,
    )

    def _handle_session_retrieve(_request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        # A mapping row written before sessions recorded their configuration
        # makes the bind read the session once, to learn what it is running.
        session = ma_session(
            id=match["session_id"], agent_id=_AGENT_ID, model=_MODEL_ID, environment_id=_ENV_ID
        )
        return httpx.Response(200, json=session.model_dump(mode="json"))

    router.add("GET", r"/v1/sessions/(?P<session_id>[^/]+)", _handle_session_retrieve)
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
    settings.turn_queue = TurnQueueSettings()
    discord_settings.thread_open_notice_after_s = 3.0
    discord_settings.bot_display_name = "daimon"
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
            ma_agent_id=_AGENT_ID,
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
    assert after.active_turn_message_id is None, (
        "wizard terminal cleanup must clear the active-turn marker"
    )
    initial_post = channel.send.call_args_list[0]
    view = initial_post.kwargs["view"]
    assert view.cancel_button.custom_id.startswith("daimon:cancel:"), (
        "the wizard status card must carry its durable intent ID in CancelView"
    )
    async with db_session_factory() as session:
        intent = (
            await session.execute(
                sql_text("SELECT status, message_id FROM turn_card_intents WHERE id = :intent_id"),
                {"intent_id": UUID(view.cancel_button.custom_id.removeprefix("daimon:cancel:"))},
            )
        ).one_or_none()
    assert intent is not None and intent.status == "retired" and intent.message_id == "42", (
        "a normally completed wizard turn must persist its response ID and retire the intent"
    )


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
        mock_create_session.return_value = ma_session(
            id="sess_fresh_msg", agent_id=_AGENT_ID, model=_MODEL_ID, environment_id=_ENV_ID
        )
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
        mock_create_session.return_value = ma_session(
            id="sess_fresh_usage", agent_id=_AGENT_ID, model=_MODEL_ID, environment_id=_ENV_ID
        )
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
        mock_create_session.return_value = ma_session(
            id="sess_fresh_concurrent", agent_id=_AGENT_ID, model=_MODEL_ID, environment_id=_ENV_ID
        )
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


@pytest.mark.parametrize("table_rendering", [False, True])
async def test_a_submit_turn_claims_and_releases_a_per_tenant_in_flight_slot(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    table_rendering: bool,
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
    runtime.settings.table_rendering = {tenant.id: table_rendering}
    bot = _make_bot(runtime)
    interaction = _interaction(user_id=_REQUESTER_ID, client=bot, channel=channel, guild_id=123)

    real_run_prepared_turn = run_prepared_turn

    async def _assert_marked_before_run(*args: Any, **kwargs: Any) -> Any:
        async with db_session_factory() as session:
            active = await list_orphaned_turns(session, platform="discord")
            intent_id = await session.scalar(
                sql_text(
                    "SELECT id FROM turn_card_intents WHERE thread_id = :thread_id AND status = 'posted'"
                ),
                {"thread_id": str(channel.id)},
            )
        assert intent_id in bot._live_turn_card_intent_ids  # pyright: ignore[reportPrivateUsage]
        assert any(
            marker.thread_id == str(channel.id) and marker.active_turn_message_id == "42"
            for marker in active
        ), "wizard turn marker must be durable before run_prepared_turn starts"
        assert kwargs["lifecycle"]._render_tables is table_rendering
        recovery = kwargs["recovery_lifecycle"](asyncio.Event())
        assert recovery._render_tables is table_rendering
        kwargs["lifecycle"] = recovery
        return await real_run_prepared_turn(*args, **kwargs)

    with (
        patch("daimon.core.turn.prepare.create_session") as mock_create_session,
        patch(
            "daimon.adapters.discord.wizard_submit.run_prepared_turn",
            side_effect=_assert_marked_before_run,
        ),
    ):
        mock_create_session.return_value = ma_session(
            id="sess_fresh_slot", agent_id=_AGENT_ID, model=_MODEL_ID, environment_id=_ENV_ID
        )
        item = await WizardSubmitButton.from_custom_id(
            interaction, MagicMock(), _submit_match(row.id)
        )
        assert await item.interaction_check(interaction) is True
        await item.callback(interaction)
        await asyncio.gather(*list(bot._bg_tasks))  # pyright: ignore[reportPrivateUsage]  # asserting the spawn contract

    assert stream_hits, "the turn must actually have run"
    assert bot.turn_queue.in_flight() == 0, (
        "the in-flight slot the turn claimed must be released once it finishes"
    )
    assert await list_orphaned_turns(db_session, platform="discord") == [], (
        "a completed wizard turn must release its active marker"
    )


async def test_a_submit_over_the_per_tenant_cap_with_a_full_queue_runs_no_turn_and_says_so(
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
    # A mention turn already holds this tenant's only slot, and the queue is full.
    bot.turn_queue.max_queued_per_tenant = 0
    bot.turn_queue.claim(tenant.id)
    interaction = _interaction(user_id=_REQUESTER_ID, client=bot, channel=channel, guild_id=123)

    with patch("daimon.core.turn.prepare.create_session"):
        item = await WizardSubmitButton.from_custom_id(
            interaction, MagicMock(), _submit_match(row.id)
        )
        assert await item.interaction_check(interaction) is True
        await item.callback(interaction)
        await asyncio.gather(*list(bot._bg_tasks))  # pyright: ignore[reportPrivateUsage]  # asserting the spawn contract

    assert stream_hits == [], "an over-cap submit must never reach the SSE turn stream"
    assert bot.turn_queue.in_flight(tenant.id) == 1, (
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


async def _queued_submit(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    workspace_id: str,
    thread_id: int,
    max_wait_s: float = 300.0,
) -> tuple[DaimonBot, TurnTicket, MagicMock, list[str], MagicMock]:
    """A submit over its tenant's cap (another turn holds the only slot)."""
    tenant = await _seed_funded_tenant(db_session, workspace_id=workspace_id)
    row = await _seed_review_row(db_session_factory, tenant=tenant)
    channel = _make_channel(thread_id=thread_id, parent_id=thread_id - 1000)
    stream_hits: list[str] = []
    router = _build_router(str(tenant.id), sent_events=[], stream_hits=stream_hits)
    runtime = _make_runtime(db_session_factory, router)
    runtime.settings.discord.max_concurrent_turns_per_tenant = 1
    bot = _make_bot(runtime)
    bot.turn_queue.max_wait_s = max_wait_s
    held = bot.turn_queue.claim(tenant.id)
    interaction = _interaction(user_id=_REQUESTER_ID, client=bot, channel=channel, guild_id=123)
    item = await WizardSubmitButton.from_custom_id(interaction, MagicMock(), _submit_match(row.id))
    assert await item.interaction_check(interaction) is True
    await item.callback(interaction)
    return bot, held, channel, stream_hits, interaction


async def _until_queued_with_card(bot: DaimonBot, channel: MagicMock) -> None:
    async with asyncio.timeout(5):
        while not (bot.turn_queue.depth() and channel.send.await_count):
            await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)


async def test_a_queued_submit_shows_its_card_and_binds_only_once_it_has_a_slot(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Same UX as a queued mention: the card (with Stop) and its durable intent
    go up at once; the session is bound only once the slot is granted, so
    preparation stays inside the caps; then the turn runs."""
    bound_while: list[tuple[int, int]] = []

    async def _recording_bind(*args: Any, **kwargs: Any) -> Any:
        bound_while.append((bot.turn_queue.depth(), bot.turn_queue.in_flight()))
        return await bind_session(*args, **kwargs)

    with (
        patch("daimon.core.turn.prepare.create_session") as mock_create_session,
        patch("daimon.adapters.discord.wizard_submit.bind_session", side_effect=_recording_bind),
    ):
        mock_create_session.return_value = ma_session(
            id="sess_queued_submit", agent_id=_AGENT_ID, model=_MODEL_ID, environment_id=_ENV_ID
        )
        bot, held, channel, stream_hits, _ = await _queued_submit(
            db_session, db_session_factory, workspace_id="800007003", thread_id=5027
        )
        await _until_queued_with_card(bot, channel)
        card_kwargs = channel.send.await_args.kwargs
        assert isinstance(card_kwargs["view"], CancelView), "the card carries Stop"
        assert bound_while == [], "no session is bound while the submit waits"
        assert stream_hits == [], "no turn runs while it waits"
        async with db_session_factory() as session:
            (intent,) = await list_recoverable_turn_card_intents(session, platform="discord")
        assert intent.message_id == "42", "a restart now would retire this card"
        held.release()
        await asyncio.gather(*list(bot._bg_tasks))  # pyright: ignore[reportPrivateUsage]

    assert bound_while == [(0, 1)], "bound once, holding the slot, with nobody queued"
    assert stream_hits, "the queued submit turn ran once the slot freed"
    assert bot.turn_queue.in_flight() == 0 and bot.turn_queue.depth() == 0


@pytest.mark.parametrize("ending", ["stopped", "timed_out"])
async def test_a_queued_submit_that_never_runs_collapses_its_card_and_leaves_nothing_behind(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], ending: str
) -> None:
    """Stop while it waits, or the max wait: no session is bound, the card
    ends as Stopped or with the ordinary error, its intent is retired, no
    marker is left, and the other turn keeps its slot."""
    with (
        patch("daimon.core.turn.prepare.create_session") as mock_create_session,
        patch("daimon.adapters.discord.wizard_submit.bind_session") as mock_bind,
    ):
        workspace = "800007004" if ending == "stopped" else "800007005"
        bot, held, channel, stream_hits, _ = await _queued_submit(
            db_session,
            db_session_factory,
            workspace_id=workspace,
            thread_id=5037,
            max_wait_s=300.0 if ending == "stopped" else 0.05,
        )
        if ending == "stopped":
            await _until_queued_with_card(bot, channel)
            view = channel.send.await_args.kwargs["view"]
            view._cancel.set()  # pyright: ignore[reportPrivateUsage]  # the Stop click
        await asyncio.gather(*list(bot._bg_tasks))  # pyright: ignore[reportPrivateUsage]
        mock_bind.assert_not_called()
        mock_create_session.assert_not_called()

    assert stream_hits == []
    card = channel.send.return_value
    expected = (
        "Stopped.\nSend a message to start again."
        if ending == "stopped"
        else "Something went wrong. Mention me to try again."
    )
    assert [c.kwargs.get("content") for c in card.edit.await_args_list] == [expected]
    assert await list_orphaned_turns(db_session, platform="discord") == []
    async with db_session_factory() as session:
        intents = await list_recoverable_turn_card_intents(session, platform="discord")
    assert intents == [], "the card's intent is retired"
    assert bot.turn_queue.depth() == 0
    assert bot.turn_queue.in_flight() == 1, "only the other turn holds a slot"
    held.release()


async def test_mention_claiming_last_slot_during_submit_cap_read_sheds_submit(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await _seed_funded_tenant(db_session, workspace_id="800007002")
    row = await _seed_review_row(db_session_factory, tenant=tenant)
    channel = _make_channel(thread_id=5017, parent_id=4017)
    stream_hits: list[str] = []
    router = _build_router(str(tenant.id), sent_events=[], stream_hits=stream_hits)
    runtime = _make_runtime(db_session_factory, router)
    runtime.settings.discord.max_concurrent_turns_per_tenant = 1
    bot = _make_bot(runtime)
    interaction = _interaction(user_id=_REQUESTER_ID, client=bot, channel=channel, guild_id=123)
    cap_read = asyncio.Event()
    release_cap = asyncio.Event()

    async def delayed_cap(*args: Any, **kwargs: Any) -> int:
        cap_read.set()
        await release_cap.wait()
        return 1

    with patch("daimon.adapters.discord.wizard_submit.get_turn_cap", side_effect=delayed_cap):
        item = await WizardSubmitButton.from_custom_id(
            interaction, MagicMock(), _submit_match(row.id)
        )
        assert await item.interaction_check(interaction) is True
        await item.callback(interaction)
        try:
            await asyncio.wait_for(cap_read.wait(), timeout=2)
            # A mention claims the last slot while the submit waits; the queue is full.
            bot.turn_queue.max_queued_per_tenant = 0
            bot.turn_queue.claim(tenant.id)
        finally:
            release_cap.set()
        await asyncio.gather(*list(bot._bg_tasks))  # pyright: ignore[reportPrivateUsage]

    assert stream_hits == []
    assert bot.turn_queue.in_flight(tenant.id) == 1


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


async def test_a_demoted_admin_outside_the_allowlist_is_refused_on_submit(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """SYS-047: the stored role still says admin, but the live interaction does
    not; the invoker policy must judge the live role and refuse the turn."""
    tenant = await _seed_funded_tenant(db_session, workspace_id="800007001")
    async with db_session_factory() as session, session.begin():
        principal = await get_or_create_platform_principal(
            session, tenant_id=tenant.id, platform="discord", external_id=_REQUESTER_ID
        )
        await set_role(session, principal.account_id, Role.ADMIN)
        await set_access_policy(
            session, tenant_id=tenant.id, policy=TenantAccessPolicy(invoker_user_ids=("staff",))
        )
    row = await _seed_review_row(db_session_factory, tenant=tenant)

    channel = _make_channel(thread_id=7007, parent_id=6007)
    stream_hits: list[str] = []
    router = _build_router(str(tenant.id), sent_events=[], stream_hits=stream_hits)
    bot = _make_bot(_make_runtime(db_session_factory, router))
    # No spec=Member on the user -> the live role is non-admin.
    interaction = _interaction(user_id=_REQUESTER_ID, client=bot, channel=channel, guild_id=123)

    with patch("daimon.core.turn.prepare.create_session") as mock_create_session:
        item = await WizardSubmitButton.from_custom_id(
            interaction, MagicMock(), _submit_match(row.id)
        )
        assert await item.interaction_check(interaction) is True
        await item.callback(interaction)
        await asyncio.gather(*bot._bg_tasks)  # pyright: ignore[reportPrivateUsage]  # drain the spawned turn
        mock_create_session.assert_not_called()

    assert stream_hits == [], "a refused submitter must never reach the turn stream"
    posted_texts = [
        call.args[0]
        for call in channel.send.call_args_list
        if call.args and isinstance(call.args[0], str)
    ]
    assert any("recorded" in t and "can start a turn" in t for t in posted_texts), (
        f"the refusal must say the answers were recorded and why, got: {posted_texts}"
    )
    async with db_session_factory() as session:
        account = await get_account(session, principal.account_id)
    assert account is not None and account.role is Role.USER, (
        "the live non-admin role must be persisted, replacing the stale admin one"
    )


@pytest.mark.parametrize(
    ("policy", "saturate"),
    [
        (TenantAccessPolicy(protected_channel_ids=("7008",)), False),
        (TenantAccessPolicy(protected_channel_ids=("6008",), invoker_user_ids=("staff",)), False),
        (TenantAccessPolicy(protected_channel_ids=("6008",)), True),
    ],
    ids=["protected-thread-open-parent", "guest-in-protected-channel", "over-cap-in-protected"],
)
async def test_a_submit_in_a_protected_channel_posts_nothing_and_runs_no_turn(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    policy: TenantAccessPolicy,
    saturate: bool,
) -> None:
    """SYS-048: no reply, no refusal and no capacity notice in a protected
    thread or channel; the answers stay recorded."""
    tenant = await _seed_funded_tenant(db_session, workspace_id="800008001")
    async with db_session_factory() as session, session.begin():
        await set_access_policy(session, tenant_id=tenant.id, policy=policy)
    row = await _seed_review_row(db_session_factory, tenant=tenant)

    channel = _make_channel(thread_id=7008, parent_id=6008)
    channel.parent = MagicMock()
    channel.parent.category_id = None
    stream_hits: list[str] = []
    router = _build_router(str(tenant.id), sent_events=[], stream_hits=stream_hits)
    bot = _make_bot(_make_runtime(db_session_factory, router))
    if saturate:
        bot.turn_queue.max_queued_per_tenant = 0  # saturate the cap, no queue room
        for _ in range(100):
            bot.turn_queue.claim(tenant.id)
    interaction = _interaction(user_id=_REQUESTER_ID, client=bot, channel=channel, guild_id=123)

    with patch("daimon.core.turn.prepare.create_session") as mock_create_session:
        item = await WizardSubmitButton.from_custom_id(
            interaction, MagicMock(), _submit_match(row.id)
        )
        assert await item.interaction_check(interaction) is True
        await item.callback(interaction)
        await asyncio.gather(*bot._bg_tasks)  # pyright: ignore[reportPrivateUsage]  # drain the spawned turn
        mock_create_session.assert_not_called()

    assert stream_hits == [], "a protected target must never reach the turn stream"
    channel.send.assert_not_called()
    async with db_session_factory() as session:
        after = await get_wizard_session(session, short_id=row.id)
    assert after is not None and after.status == "submitted", "the answers stay recorded"


async def test_a_submit_whose_protection_cannot_be_read_posts_nothing(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Unknown protection is treated like protected: no capacity notice, no
    refusal, no error render, no turn."""
    from sqlalchemy.exc import OperationalError

    tenant = await _seed_funded_tenant(db_session, workspace_id="800009001")
    row = await _seed_review_row(db_session_factory, tenant=tenant)
    channel = _make_channel(thread_id=7009, parent_id=6009)
    channel.parent = MagicMock()
    channel.parent.category_id = None
    stream_hits: list[str] = []
    router = _build_router(str(tenant.id), sent_events=[], stream_hits=stream_hits)
    bot = _make_bot(_make_runtime(db_session_factory, router))
    interaction = _interaction(user_id=_REQUESTER_ID, client=bot, channel=channel, guild_id=123)

    with (
        patch(
            "daimon.core.turn.protection.load_access_policy",
            new=AsyncMock(side_effect=OperationalError("SELECT", {}, Exception("pool gone"))),
        ),
        patch("daimon.core.turn.prepare.create_session") as mock_create_session,
    ):
        item = await WizardSubmitButton.from_custom_id(
            interaction, MagicMock(), _submit_match(row.id)
        )
        assert await item.interaction_check(interaction) is True
        await item.callback(interaction)
        await asyncio.gather(*bot._bg_tasks)  # pyright: ignore[reportPrivateUsage]  # drain the spawned turn
        mock_create_session.assert_not_called()

    assert stream_hits == []
    channel.send.assert_not_called()


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
        mock_create_session.return_value = ma_session(
            id="sess_ceiling_wizard", agent_id=_AGENT_ID, model=_MODEL_ID, environment_id=_ENV_ID
        )
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


@pytest.mark.parametrize(
    "sealed_id", ["5001", "4001", None], ids=["sealed-thread", "sealed-parent", "open"]
)
async def test_wizard_origin_controls_the_actual_memory_mount(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    sealed_id: str | None,
) -> None:
    tenant = await _seed_funded_tenant(db_session, workspace_id="800019001")
    await set_access_policy(
        db_session,
        tenant_id=tenant.id,
        policy=TenantAccessPolicy(sealed_channel_ids=() if sealed_id is None else (sealed_id,)),
    )
    await insert_memory_store(
        db_session,
        tenant_id=tenant.id,
        agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=_AGENT_ID),
        memory_store_id="memstore_wizard_policy",
    )
    await db_session.commit()
    row = await _seed_review_row(db_session_factory, tenant=tenant)
    channel = _make_channel(thread_id=5001, parent_id=4001)
    sent_events: list[dict[str, Any]] = []
    stream_hits: list[str] = []
    created: list[dict[str, Any]] = []
    router = _build_router(str(tenant.id), sent_events=sent_events, stream_hits=stream_hits)

    def create(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        body = json.loads(request.content)
        created.append(body)
        session = ma_session(
            id="ses_wizard_policy",
            agent_id=_AGENT_ID,
            model=_MODEL_ID,
            environment_id=_ENV_ID,
            resources=body["resources"],
        )
        return httpx.Response(200, json=session.model_dump(mode="json"))

    router.add("POST", r"/v1/sessions", create)
    runtime = _make_runtime(db_session_factory, router)
    bot = _make_bot(runtime)
    interaction = _interaction(user_id=_REQUESTER_ID, client=bot, channel=channel, guild_id=123)
    item = await WizardSubmitButton.from_custom_id(interaction, MagicMock(), _submit_match(row.id))
    assert await item.interaction_check(interaction) is True
    await item.callback(interaction)
    await asyncio.gather(*list(bot._bg_tasks))

    assert len(created) == 1
    memory = next(
        resource for resource in created[0]["resources"] if resource["type"] == "memory_store"
    )
    assert memory["access"] == ("read_write" if sealed_id is None else "read_only")
    if sealed_id is not None:
        assert "mounted read-only" in memory["instructions"]
    assert stream_hits == ["ses_wizard_policy"]
