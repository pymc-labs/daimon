"""Tests for the task-continuity wiring in `DaimonBot._orchestrate`.

Covers what happens after `bind_session` returns or raises: the
`SessionPreparationFailed` / `SessionAgentMismatch` copy paths (no turn runs
either way), `is_setup` derived from `thread_binding_kind` rather than
`thread_binding_id`, `session_state` threaded into `render_turn_origin`, the
pre-answer loss/replacement notices, and the post-answer "must finish"
follow-up. `bind_session` and `run_prepared_turn` are patched at the names
`bot.py` imports (mirrors the existing `build_context_xml` patching
precedent in `test_orchestration.py`) so each test controls exactly the
`ContinuityOutcome` it wants to observe, without driving the full
session-preparation/compat pipeline (covered at the core level).
"""

from __future__ import annotations

import types
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal
from unittest.mock import AsyncMock, MagicMock, patch

import anthropic as _anthropic
import discord
import httpx
import pytest
from daimon.adapters.discord.runtime import DiscordRuntime, build_turn_deps
from daimon.core.config import McpSettings, ThreadNamingSettings
from daimon.core.continuity.messages import (
    render_current_work_must_finish,
    render_replacement_summary,
    render_unexpected_loss,
)
from daimon.core.ma_resolver import ResolverCache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import DeploymentDefault, ResolvedConfig
from daimon.core.stores import tenant_ledger
from daimon.core.stores.turn_card_intents import list_recoverable_turn_card_intents
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.errors import (
    SessionAgentMismatch,
    SessionBusyError,
    SessionPreparationFailed,
)
from daimon.core.turn.prepare import ContinuityOutcome, PreparedTurn
from daimon.core.turn.run import RunOutcome
from daimon.core.turn.state import TextBlock, TurnState
from daimon.core.turn_origin import turn_origin as real_turn_origin
from daimon.testing import ma_agent, ma_environment
from daimon.testing.factories import make_tenant
from daimon.testing.ma import build_stub_anthropic
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .harness import make_bot

_TENANT_UUID_NS = uuid.UUID("00000000-0000-0000-0000-0000000000aa")


async def _noop_recorder(*, event: object) -> None:
    return None


#: The text a driven turn "answers" with. Short enough that a prepended
#: notice never pushes the first chunk past Discord's message limit.
_ANSWER = "Here is the answer you asked for."


def _run_turn_revealing_answer(outcome: RunOutcome, *, answer: str = _ANSWER) -> object:
    """A `run_prepared_turn` stand-in that actually reveals an answer.

    The real driver ends a turn by calling `on_terminal_success`, which is
    where the adapter's continuity notices are folded into the answer text. A
    mock that only returns a `RunOutcome` never reaches that code, so these
    tests drive the lifecycle exactly as the driver would before returning.
    """

    async def _run(*_args: object, **kwargs: object) -> RunOutcome:
        lifecycle = kwargs["lifecycle"]
        await lifecycle.on_terminal_success(  # pyright: ignore[reportAttributeAccessIssue]
            TurnState(content=[TextBlock(kind="text", text=answer)])
        )
        return outcome

    return _run


def _final_answer_text(message: MagicMock) -> str:
    """The content of the last edit that carried answer text."""
    contents = [
        c.kwargs["content"]
        for c in message.channel.send.return_value.edit.call_args_list
        if c.kwargs.get("content")
    ]
    assert contents, "the answer must have replaced the embed"
    return str(contents[-1])


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


def _stub_resolved_config(
    *,
    thread_binding_id: uuid.UUID | None = None,
    thread_binding_kind: str | None = None,
) -> ResolvedConfig:
    return ResolvedConfig(
        agent_name="test-agent",
        agent_name_tier="tenant",
        environment_name="test-env",
        environment_name_tier="tenant",
        thread_binding_id=thread_binding_id,
        thread_binding_kind=thread_binding_kind,  # pyright: ignore[reportArgumentType]
    )


def _make_turn_deps(
    settings: MagicMock,
    anthropic: MagicMock,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    resolver_cache: ResolverCache,
    deployment_default: DeploymentDefault,
) -> TurnDeps:
    return build_turn_deps(
        settings,
        anthropic,
        sessionmaker,
        deployment_default=deployment_default,
        resolver_cache=resolver_cache,
        billing_config=None,
    )


def _make_runtime(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    anthropic: _anthropic.AsyncAnthropic | None = None,
) -> DiscordRuntime:
    settings = MagicMock()
    settings.mcp = McpSettings()
    settings.billing.markup = Decimal("1.0")
    settings.billing.signup_credit = Decimal("0")
    discord_settings = MagicMock()
    discord_settings.max_concurrent_turns_per_tenant = 100
    discord_settings.per_caller_thread_sessions = True
    settings.discord = discord_settings
    settings.thread_naming = ThreadNamingSettings(enabled=False)
    if anthropic is None:
        anthropic = AsyncMock()
        anthropic.beta.agents.retrieve = AsyncMock(return_value=ma_agent())
        anthropic.beta.environments.retrieve = AsyncMock(return_value=ma_environment())
        anthropic.beta.agents.list = MagicMock(return_value=_AsyncIter([]))
    from daimon.core.ma_resolver import new_resolver_cache

    resolver_cache = new_resolver_cache()
    deployment_default = DeploymentDefault()
    return DiscordRuntime(
        settings=settings,
        anthropic=anthropic,
        sessionmaker=sessionmaker,
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=deployment_default,
        resolver_cache=resolver_cache,
        turn_deps=_make_turn_deps(
            settings,
            anthropic,
            sessionmaker,
            resolver_cache=resolver_cache,
            deployment_default=deployment_default,
        ),
    )


def _make_thread_message(
    *,
    content: str = "<@999> hello",
    guild_id: int = 123456,
    thread_id: int = 5555,
    parent_id: int = 789,
    author_id: int = 111,
) -> MagicMock:
    message = MagicMock(spec=discord.Message)
    message.content = content
    message.author = MagicMock()
    message.author.bot = False
    message.author.id = author_id
    message.guild = MagicMock(spec=discord.Guild)
    message.guild.id = guild_id
    message.guild.owner_id = 1
    thread = MagicMock(spec=discord.Thread)
    thread.id = thread_id
    thread.parent_id = parent_id
    message_ref = MagicMock()
    message_ref.id = 42
    message_ref.edit = AsyncMock()
    thread.send = AsyncMock(return_value=message_ref)
    message.channel = thread
    message.add_reaction = AsyncMock()
    message.attachments = []
    message.mentions = [types.SimpleNamespace(id=999)]
    return message


def _make_prepared_turn(
    *, continuity: ContinuityOutcome, account_id: uuid.UUID, mapping_id: uuid.UUID
) -> PreparedTurn:
    from daimon.core.turn.admission import Admission

    admission = Admission(
        account_id=account_id,
        agent=ma_agent(),
        environment=ma_environment(),
        config=_stub_resolved_config(),
    )
    return PreparedTurn(
        admission=admission,
        ma_session_id="sess_test",
        mapping_id=mapping_id,
        watermark=None,
        reused=True,
        session_account_id=account_id,
        _record=_noop_recorder,
        continuity=continuity,
    )


async def _seed_tenant(db_session: AsyncSession, *, guild_id: str) -> uuid.UUID:
    tenant = await make_tenant(db_session, platform="discord", workspace_id=guild_id)
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant.id,
        delta_usd=Decimal("100.00"),
        reason="trial",
        idempotency_key=f"trial:{tenant.id}",
    )
    await db_session.commit()
    return tenant.id


async def _assert_no_recoverable_cards(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker() as session:
        assert await list_recoverable_turn_card_intents(session, platform="discord") == []


@patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)
@patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock)
@patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
async def test_session_preparation_failed_posts_copy_and_runs_no_turn(
    mock_resolve_config: AsyncMock,
    mock_resolve_env: AsyncMock,
    mock_resolve_agent: AsyncMock,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    guild_id = "700000001"
    tenant_id = await _seed_tenant(db_session, guild_id=guild_id)
    mock_resolve_config.return_value = _stub_resolved_config()
    mock_resolve_agent.return_value = "ag_test"
    mock_resolve_env.return_value = "env_test"

    runtime = _make_runtime(db_session_factory)
    bot = make_bot(runtime)
    message = _make_thread_message(guild_id=int(guild_id))

    with (
        patch(
            "daimon.adapters.discord.bot.bind_session",
            new_callable=AsyncMock,
            side_effect=SessionPreparationFailed(
                reasons=("agent_identity",),
                stage="checkpointed",
                retry_after=datetime.now(UTC),
            ),
        ),
        patch(
            "daimon.adapters.discord.bot.run_prepared_turn", new_callable=AsyncMock
        ) as mock_run_prepared_turn,
    ):
        await bot.on_message(message)

    mock_run_prepared_turn.assert_not_called()
    posted = [
        c.kwargs.get("content") for c in message.channel.send.return_value.edit.call_args_list
    ]
    assert any(p is not None and "could not get test-agent ready" in p.lower() for p in posted), (
        f"expected the preparation-failed copy, got {posted}"
    )
    assert any(p is not None and "mention me again to retry" in p.lower() for p in posted)
    await _assert_no_recoverable_cards(db_session_factory)
    _ = tenant_id


@patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)
@patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock)
@patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
async def test_responder_changed_without_handoff_posts_offer_and_runs_no_turn(
    mock_resolve_config: AsyncMock,
    mock_resolve_env: AsyncMock,
    mock_resolve_agent: AsyncMock,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    guild_id = "700000002"
    tenant_id = await _seed_tenant(db_session, guild_id=guild_id)
    mock_resolve_config.return_value = _stub_resolved_config()
    mock_resolve_agent.return_value = "ag_test"
    mock_resolve_env.return_value = "env_test"

    runtime = _make_runtime(db_session_factory)
    # The owner-name lookup walks the tenant's live agents; give it one match
    # for the mismatch's `source_agent_id` so the copy names the real owner
    # rather than falling back to "the previous agent".
    runtime.anthropic.beta.agents.list = MagicMock(  # pyright: ignore[reportAttributeAccessIssue]
        return_value=_AsyncIter([ma_agent(id="ag_owner", name="owner-bot", tenant_id=tenant_id)])
    )
    bot = make_bot(runtime)
    message = _make_thread_message(guild_id=int(guild_id))

    with (
        patch(
            "daimon.adapters.discord.bot.bind_session",
            new_callable=AsyncMock,
            side_effect=SessionAgentMismatch(
                mapping_id=uuid.uuid4(),
                session_id="sess_dead",
                source_agent_id="ag_owner",
                destination_agent_id="ag_test",
            ),
        ),
        patch(
            "daimon.adapters.discord.bot.run_prepared_turn", new_callable=AsyncMock
        ) as mock_run_prepared_turn,
    ):
        await bot.on_message(message)

    mock_run_prepared_turn.assert_not_called()
    posted = [
        c.kwargs.get("content") for c in message.channel.send.return_value.edit.call_args_list
    ]
    assert any(
        p is not None and "test-agent now answers" in p and "belongs to owner-bot" in p
        for p in posted
    ), f"expected the responder-changed-without-handoff offer, got {posted}"
    assert any(p is not None and "have test-agent take over this task" in p for p in posted)
    await _assert_no_recoverable_cards(db_session_factory)


@patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)
@patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock)
@patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
@patch("daimon.adapters.discord.bot.build_context_xml", new_callable=AsyncMock)
async def test_is_setup_false_for_a_handoff_binding(
    mock_build_context_xml: AsyncMock,
    mock_resolve_config: AsyncMock,
    mock_resolve_env: AsyncMock,
    mock_resolve_agent: AsyncMock,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    mock_build_context_xml.return_value = ("<user_query>hello</user_query>", [])
    guild_id = "700000003"
    tenant_id = await _seed_tenant(db_session, guild_id=guild_id)
    mock_resolve_config.return_value = _stub_resolved_config(
        thread_binding_id=uuid.uuid4(), thread_binding_kind="handoff"
    )
    mock_resolve_agent.return_value = "ag_test"
    mock_resolve_env.return_value = "env_test"

    agent = ma_agent(tenant_id=tenant_id)

    def _agents_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=agent.model_dump(mode="json"))

    runtime = _make_runtime(db_session_factory, anthropic=build_stub_anthropic(_agents_handler))
    bot = make_bot(runtime)
    message = _make_thread_message(guild_id=int(guild_id))
    account_id = uuid.uuid4()
    mapping_id = uuid.uuid4()
    prepared = _make_prepared_turn(
        continuity=ContinuityOutcome(), account_id=account_id, mapping_id=mapping_id
    )
    run_outcome = RunOutcome(
        state=TurnState(),
        ma_session_id="sess_test",
        mapping_id=mapping_id,
        recovered=False,
    )

    with (
        patch(
            "daimon.adapters.discord.bot.bind_session",
            new_callable=AsyncMock,
            return_value=prepared,
        ),
        patch(
            "daimon.adapters.discord.bot.run_prepared_turn",
            new_callable=AsyncMock,
            return_value=run_outcome,
        ),
        patch("daimon.adapters.discord.bot.turn_origin", wraps=real_turn_origin) as spy,
    ):
        await bot.on_message(message)

    assert spy.call_args is not None
    assert spy.call_args.kwargs["is_setup"] is False, (
        "a handoff binding must not read as a setup conversation"
    )


@patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)
@patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock)
@patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
@patch("daimon.adapters.discord.bot.build_context_xml", new_callable=AsyncMock)
async def test_bind_replaced_after_loss_never_fires_before_the_turn(
    mock_build_context_xml: AsyncMock,
    mock_resolve_config: AsyncMock,
    mock_resolve_env: AsyncMock,
    mock_resolve_agent: AsyncMock,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """`bind_session`'s own `PreparedTurn.continuity` can never be
    "replaced_after_loss" -- that state only exists on the post-turn
    `RunOutcome`, set when `run_prepared_turn`'s recovery cycle recreates the
    session mid-call. This is a regression guard for the dead pre-turn branch
    removed from `_orchestrate`: even if `prepared.continuity` somehow carried
    that state, no loss notice is posted from it -- only `outcome.continuity`
    (covered by the tests below) can trigger one.
    """
    mock_build_context_xml.return_value = ("<user_query>hello</user_query>", [])
    guild_id = "700000004"
    await _seed_tenant(db_session, guild_id=guild_id)
    mock_resolve_config.return_value = _stub_resolved_config()
    mock_resolve_agent.return_value = "ag_test"
    mock_resolve_env.return_value = "env_test"

    runtime = _make_runtime(db_session_factory)
    bot = make_bot(runtime)
    message = _make_thread_message(guild_id=int(guild_id))
    account_id = uuid.uuid4()
    mapping_id = uuid.uuid4()
    prepared = _make_prepared_turn(
        continuity=ContinuityOutcome(state="replaced_after_loss", transfer_kind="transcript"),
        account_id=account_id,
        mapping_id=mapping_id,
    )
    # The real run_prepared_turn always restates a mid-call recovery through
    # `outcome.continuity`, never leaves `prepared.continuity` as the final
    # word -- so the fake outcome here reports an ordinary completed turn,
    # exactly what would happen if bind_session's decision were (wrongly)
    # trusted post-turn.
    run_outcome = RunOutcome(
        state=TurnState(),
        ma_session_id="sess_test",
        mapping_id=mapping_id,
        recovered=False,
        continuity=ContinuityOutcome(),
    )

    with (
        patch(
            "daimon.adapters.discord.bot.bind_session",
            new_callable=AsyncMock,
            return_value=prepared,
        ),
        patch(
            "daimon.adapters.discord.bot.run_prepared_turn",
            new_callable=AsyncMock,
            return_value=run_outcome,
        ),
    ):
        await bot.on_message(message)

    sent_texts = [c.args[0] for c in message.channel.send.call_args_list if c.args]
    assert not any("lost the workspace this task was running in" in t for t in sent_texts), (
        f"prepared.continuity alone must never trigger the loss notice, got {sent_texts}"
    )


@pytest.mark.parametrize(
    ("transfer_kind", "expected_phrase"),
    [
        ("transcript", "the files that were saved to your task"),
        (None, "not the earlier conversation"),
    ],
    ids=["transcript_variant", "history_variant_default"],
)
@patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)
@patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock)
@patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
@patch("daimon.adapters.discord.bot.build_context_xml", new_callable=AsyncMock)
async def test_outcome_replaced_after_loss_prepends_exactly_one_loss_notice_to_the_answer(
    mock_build_context_xml: AsyncMock,
    mock_resolve_config: AsyncMock,
    mock_resolve_env: AsyncMock,
    mock_resolve_agent: AsyncMock,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    transfer_kind: Literal["transcript"] | None,
    expected_phrase: str,
) -> None:
    """A dead-session recovery must tell the person their workspace was lost.
    `run_prepared_turn` only learns this mid-call, so the fact is keyed off
    `outcome.continuity` -- never `prepared.continuity`. By then the answer has
    already replaced the embed posted at mention time, so the message is edited
    once more to put the notice above the answer rather than sending it
    underneath, where it would read as a footnote to the thing it explains."""
    mock_build_context_xml.return_value = ("<user_query>hello</user_query>", [])
    guild_id = "700000006"
    await _seed_tenant(db_session, guild_id=guild_id)
    mock_resolve_config.return_value = _stub_resolved_config()
    mock_resolve_agent.return_value = "ag_test"
    mock_resolve_env.return_value = "env_test"

    runtime = _make_runtime(db_session_factory)
    bot = make_bot(runtime)
    message = _make_thread_message(guild_id=int(guild_id))
    account_id = uuid.uuid4()
    mapping_id = uuid.uuid4()
    prepared = _make_prepared_turn(
        continuity=ContinuityOutcome(), account_id=account_id, mapping_id=mapping_id
    )
    run_outcome = RunOutcome(
        state=TurnState(),
        ma_session_id="sess_recovered",
        mapping_id=mapping_id,
        recovered=True,
        continuity=ContinuityOutcome(state="replaced_after_loss", transfer_kind=transfer_kind),
    )

    with (
        patch(
            "daimon.adapters.discord.bot.bind_session",
            new_callable=AsyncMock,
            return_value=prepared,
        ),
        patch(
            "daimon.adapters.discord.bot.run_prepared_turn",
            new_callable=AsyncMock,
            side_effect=_run_turn_revealing_answer(run_outcome),
        ),
    ):
        await bot.on_message(message)

    notice = render_unexpected_loss("transcript" if transfer_kind == "transcript" else "history")
    assert expected_phrase in notice, "the parametrized phrase must pin the right variant"
    assert _final_answer_text(message) == f"{notice}\n\n{_ANSWER}", (
        "the loss notice must be the answer's first paragraph, exactly once"
    )
    sent_texts = [c.args[0] for c in message.channel.send.call_args_list if c.args]
    assert not any("lost the workspace this task was running in" in t for t in sent_texts), (
        f"the loss notice must not also be sent as its own message, got {sent_texts}"
    )


@patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)
@patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock)
@patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
@patch("daimon.adapters.discord.bot.build_context_xml", new_callable=AsyncMock)
async def test_ordinary_turn_posts_no_loss_notice(
    mock_build_context_xml: AsyncMock,
    mock_resolve_config: AsyncMock,
    mock_resolve_env: AsyncMock,
    mock_resolve_agent: AsyncMock,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    mock_build_context_xml.return_value = ("<user_query>hello</user_query>", [])
    guild_id = "700000007"
    await _seed_tenant(db_session, guild_id=guild_id)
    mock_resolve_config.return_value = _stub_resolved_config()
    mock_resolve_agent.return_value = "ag_test"
    mock_resolve_env.return_value = "env_test"

    runtime = _make_runtime(db_session_factory)
    bot = make_bot(runtime)
    message = _make_thread_message(guild_id=int(guild_id))
    account_id = uuid.uuid4()
    mapping_id = uuid.uuid4()
    prepared = _make_prepared_turn(
        continuity=ContinuityOutcome(), account_id=account_id, mapping_id=mapping_id
    )
    run_outcome = RunOutcome(
        state=TurnState(),
        ma_session_id="sess_test",
        mapping_id=mapping_id,
        recovered=False,
        continuity=ContinuityOutcome(),
    )

    with (
        patch(
            "daimon.adapters.discord.bot.bind_session",
            new_callable=AsyncMock,
            return_value=prepared,
        ),
        patch(
            "daimon.adapters.discord.bot.run_prepared_turn",
            new_callable=AsyncMock,
            return_value=run_outcome,
        ),
    ):
        await bot.on_message(message)

    sent_texts = [c.args[0] for c in message.channel.send.call_args_list if c.args]
    assert not any("lost the workspace this task was running in" in t for t in sent_texts), (
        f"an ordinary turn must post no loss notice, got {sent_texts}"
    )


@patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)
@patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock)
@patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
@patch("daimon.adapters.discord.bot.build_context_xml", new_callable=AsyncMock)
async def test_pending_change_posts_must_finish_copy_after_the_answer(
    mock_build_context_xml: AsyncMock,
    mock_resolve_config: AsyncMock,
    mock_resolve_env: AsyncMock,
    mock_resolve_agent: AsyncMock,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    mock_build_context_xml.return_value = ("<user_query>hello</user_query>", [])
    guild_id = "700000005"
    await _seed_tenant(db_session, guild_id=guild_id)
    mock_resolve_config.return_value = _stub_resolved_config()
    mock_resolve_agent.return_value = "ag_test"
    mock_resolve_env.return_value = "env_test"

    runtime = _make_runtime(db_session_factory)
    bot = make_bot(runtime)
    message = _make_thread_message(guild_id=int(guild_id))
    account_id = uuid.uuid4()
    mapping_id = uuid.uuid4()
    prepared = _make_prepared_turn(
        continuity=ContinuityOutcome(pending=("model",)),
        account_id=account_id,
        mapping_id=mapping_id,
    )
    run_outcome = RunOutcome(
        state=TurnState(), ma_session_id="sess_test", mapping_id=mapping_id, recovered=False
    )

    with (
        patch(
            "daimon.adapters.discord.bot.bind_session",
            new_callable=AsyncMock,
            return_value=prepared,
        ),
        patch(
            "daimon.adapters.discord.bot.run_prepared_turn",
            new_callable=AsyncMock,
            return_value=run_outcome,
        ),
    ):
        await bot.on_message(message)

    sent_texts = [c.args[0] for c in message.channel.send.call_args_list if c.args]
    assert any(
        "still working on the previous message here" in t and "picks it up on the next message" in t
        for t in sent_texts
    ), f"expected the must-finish copy posted after the answer, got {sent_texts}"


@patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)
@patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock)
@patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
@patch("daimon.adapters.discord.bot.build_context_xml", new_callable=AsyncMock)
async def test_replaced_makes_the_replacement_summary_the_answers_first_paragraph(
    mock_build_context_xml: AsyncMock,
    mock_resolve_config: AsyncMock,
    mock_resolve_env: AsyncMock,
    mock_resolve_agent: AsyncMock,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The summary explains the answer, so it has to be readable above it.

    The answer is an in-place edit of the embed posted at mention time, so a
    summary sent as its own message always lands below it -- the reader saw
    "the file vanished" before the explanation of why. It rides the answer
    text instead."""
    mock_build_context_xml.return_value = ("<user_query>hello</user_query>", [])
    guild_id = "700000008"
    await _seed_tenant(db_session, guild_id=guild_id)
    mock_resolve_config.return_value = _stub_resolved_config()
    mock_resolve_agent.return_value = "ag_test"
    mock_resolve_env.return_value = "env_test"

    runtime = _make_runtime(db_session_factory)
    bot = make_bot(runtime)
    message = _make_thread_message(guild_id=int(guild_id))
    mapping_id = uuid.uuid4()
    prepared = _make_prepared_turn(
        continuity=ContinuityOutcome(state="replaced", transfer_kind="transcript"),
        account_id=uuid.uuid4(),
        mapping_id=mapping_id,
    )
    run_outcome = RunOutcome(
        state=TurnState(),
        ma_session_id="sess_test",
        mapping_id=mapping_id,
        recovered=False,
        continuity=prepared.continuity,
    )

    with (
        patch(
            "daimon.adapters.discord.bot.bind_session",
            new_callable=AsyncMock,
            return_value=prepared,
        ),
        patch(
            "daimon.adapters.discord.bot.run_prepared_turn",
            new_callable=AsyncMock,
            side_effect=_run_turn_revealing_answer(run_outcome),
        ),
    ):
        await bot.on_message(message)

    summary = render_replacement_summary("transcript", [])
    assert _final_answer_text(message) == f"{summary}\n\n{_ANSWER}", (
        "the replacement summary must be the answer's first paragraph"
    )
    sent_texts = [c.args[0] for c in message.channel.send.call_args_list if c.args]
    assert summary not in sent_texts, (
        f"the summary must not also be sent as its own message, got {sent_texts}"
    )


@patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)
@patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock)
@patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
@patch("daimon.adapters.discord.bot.build_context_xml", new_callable=AsyncMock)
async def test_replaced_falls_back_to_its_own_message_when_no_answer_is_revealed(
    mock_build_context_xml: AsyncMock,
    mock_resolve_config: AsyncMock,
    mock_resolve_env: AsyncMock,
    mock_resolve_agent: AsyncMock,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A tool-only, cancelled or failed turn reveals no answer to carry the
    summary. The person still has to be told what the replacement carried
    across, so it goes out on its own rather than being dropped."""
    mock_build_context_xml.return_value = ("<user_query>hello</user_query>", [])
    guild_id = "700000009"
    await _seed_tenant(db_session, guild_id=guild_id)
    mock_resolve_config.return_value = _stub_resolved_config()
    mock_resolve_agent.return_value = "ag_test"
    mock_resolve_env.return_value = "env_test"

    runtime = _make_runtime(db_session_factory)
    bot = make_bot(runtime)
    message = _make_thread_message(guild_id=int(guild_id))
    mapping_id = uuid.uuid4()
    prepared = _make_prepared_turn(
        continuity=ContinuityOutcome(state="replaced", transfer_kind="full"),
        account_id=uuid.uuid4(),
        mapping_id=mapping_id,
    )
    run_outcome = RunOutcome(
        state=TurnState(),
        ma_session_id="sess_test",
        mapping_id=mapping_id,
        recovered=False,
        continuity=prepared.continuity,
    )

    with (
        patch(
            "daimon.adapters.discord.bot.bind_session",
            new_callable=AsyncMock,
            return_value=prepared,
        ),
        patch(
            "daimon.adapters.discord.bot.run_prepared_turn",
            new_callable=AsyncMock,
            return_value=run_outcome,
        ),
    ):
        await bot.on_message(message)

    sent_texts = [c.args[0] for c in message.channel.send.call_args_list if c.args]
    assert render_replacement_summary("full", []) in sent_texts, (
        f"with no answer to carry it, the summary must still reach the person, got {sent_texts}"
    )


@patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)
@patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock)
@patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
@patch("daimon.adapters.discord.bot.build_context_xml", new_callable=AsyncMock)
async def test_replaced_with_no_transfer_kind_posts_no_summary_prefix(
    mock_build_context_xml: AsyncMock,
    mock_resolve_config: AsyncMock,
    mock_resolve_env: AsyncMock,
    mock_resolve_agent: AsyncMock,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A fresh start (`transfer_kind=None`) is a `replaced` bind with nothing
    carried across -- not a transfer whose contents need summarizing. The old
    `transfer_kind or "history"` fallback rendered a transfer summary on a
    fresh start, contradicting the fresh-start confirmation already posted.
    No prefix should be rendered at all."""
    mock_build_context_xml.return_value = ("<user_query>hello</user_query>", [])
    guild_id = "700000010"
    await _seed_tenant(db_session, guild_id=guild_id)
    mock_resolve_config.return_value = _stub_resolved_config()
    mock_resolve_agent.return_value = "ag_test"
    mock_resolve_env.return_value = "env_test"

    runtime = _make_runtime(db_session_factory)
    bot = make_bot(runtime)
    message = _make_thread_message(guild_id=int(guild_id))
    mapping_id = uuid.uuid4()
    prepared = _make_prepared_turn(
        continuity=ContinuityOutcome(state="replaced", transfer_kind=None),
        account_id=uuid.uuid4(),
        mapping_id=mapping_id,
    )
    run_outcome = RunOutcome(
        state=TurnState(),
        ma_session_id="sess_test",
        mapping_id=mapping_id,
        recovered=False,
        continuity=prepared.continuity,
    )

    with (
        patch(
            "daimon.adapters.discord.bot.bind_session",
            new_callable=AsyncMock,
            return_value=prepared,
        ),
        patch(
            "daimon.adapters.discord.bot.run_prepared_turn",
            new_callable=AsyncMock,
            side_effect=_run_turn_revealing_answer(run_outcome),
        ),
    ):
        await bot.on_message(message)

    assert _final_answer_text(message) == _ANSWER, (
        "a fresh start must not prefix the answer with a replacement summary"
    )
    sent_texts = [c.args[0] for c in message.channel.send.call_args_list if c.args]
    assert render_replacement_summary("full", []) not in sent_texts
    assert render_replacement_summary("transcript", []) not in sent_texts
    assert render_replacement_summary("history", []) not in sent_texts


@patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock)
@patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock)
@patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock)
async def test_session_busy_posts_must_finish_copy_and_runs_no_turn(
    mock_resolve_config: AsyncMock,
    mock_resolve_env: AsyncMock,
    mock_resolve_agent: AsyncMock,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A responder change that arrives while the previous turn is still running
    must not be made *around* that turn: the session still in flight belongs to
    the outgoing responder, so running this turn on it would answer as one agent
    inside another agent's workspace. No turn runs; the person is told the
    in-flight message finishes first."""
    guild_id = "700000010"
    await _seed_tenant(db_session, guild_id=guild_id)
    mock_resolve_config.return_value = _stub_resolved_config()
    mock_resolve_agent.return_value = "ag_test"
    mock_resolve_env.return_value = "env_test"

    runtime = _make_runtime(db_session_factory)
    bot = make_bot(runtime)
    message = _make_thread_message(guild_id=int(guild_id))

    with (
        patch(
            "daimon.adapters.discord.bot.bind_session",
            new_callable=AsyncMock,
            side_effect=SessionBusyError(
                pending_reasons=("agent_identity",), retry_after=datetime.now(UTC)
            ),
        ),
        patch(
            "daimon.adapters.discord.bot.run_prepared_turn", new_callable=AsyncMock
        ) as mock_run_prepared_turn,
    ):
        await bot.on_message(message)

    mock_run_prepared_turn.assert_not_called()
    edited = [
        c.kwargs.get("content") for c in message.channel.send.return_value.edit.call_args_list
    ]
    assert render_current_work_must_finish("test-agent", handoff=True) in edited, (
        f"expected the busy copy edited into the status embed, got {edited}"
    )
    await _assert_no_recoverable_cards(db_session_factory)
