"""Tests for FeedbackModal.on_submit -- the free-text write path.

Rows are seeded and read back exclusively through `record_vote`
(`daimon.core.stores.message_feedback`), never through raw ORM: a re-vote
with the SAME vote value leaves `feedback_text` untouched (see that
function's docstring) and returns the current row, which is exactly the
"read it back" operation these tests need.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import structlog
from daimon.adapters.discord.feedback_modal import FeedbackModal
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.message_feedback import record_vote
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import build_stub_anthropic
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_VOTER_ID = "100000000000000001"
_OTHER_USER_ID = "200000000000000002"


def _runtime(*, sessionmaker: async_sessionmaker[AsyncSession]) -> DiscordRuntime:
    return DiscordRuntime(
        settings=MagicMock(),
        anthropic=build_stub_anthropic(),
        sessionmaker=sessionmaker,
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
        turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]  # feedback-modal tests never run a turn
    )


def _interaction(*, user_id: str) -> MagicMock:
    interaction = MagicMock()
    interaction.user.id = int(user_id)
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


def _modal(*, runtime: DiscordRuntime, feedback_id: uuid.UUID, text: str) -> FeedbackModal:
    modal = FeedbackModal(runtime=runtime, feedback_id=feedback_id)
    modal.text_input._value = text  # pyright: ignore[reportPrivateUsage]
    return modal


async def _seed_vote(
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    message_id: str,
    platform_user_id: str = _VOTER_ID,
) -> uuid.UUID:
    async with db_session_factory() as session, session.begin():
        result = await record_vote(
            session,
            tenant_id=tenant_id,
            platform="discord",
            message_id=message_id,
            channel_id="chan-1",
            platform_user_id=platform_user_id,
            account_id=account_id,
            ma_session_id="sess_a",
            vote="down",
        )
    return result.row.id


async def _read_back_feedback_text(
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    message_id: str,
    platform_user_id: str = _VOTER_ID,
) -> str | None:
    """Re-vote with the SAME value -- record_vote never touches feedback_text on update."""
    async with db_session_factory() as session, session.begin():
        result = await record_vote(
            session,
            tenant_id=tenant_id,
            platform="discord",
            message_id=message_id,
            channel_id="chan-1",
            platform_user_id=platform_user_id,
            account_id=account_id,
            ma_session_id="sess_a",
            vote="down",
        )
    return result.row.feedback_text


async def test_submitting_non_empty_text_attaches_it_to_the_voters_row(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, workspace_id="guild-submit")
        account = await make_account(session, tenant=tenant)
    feedback_id = await _seed_vote(
        db_session_factory, tenant_id=tenant.id, account_id=account.id, message_id="msg-submit"
    )
    runtime = _runtime(sessionmaker=db_session_factory)
    modal = _modal(runtime=runtime, feedback_id=feedback_id, text="the answer was wrong")

    interaction = _interaction(user_id=_VOTER_ID)
    await modal.on_submit(interaction)

    stored_text = await _read_back_feedback_text(
        db_session_factory, tenant_id=tenant.id, account_id=account.id, message_id="msg-submit"
    )
    assert stored_text == "the answer was wrong", (
        "the submitted text must attach to the voter's own row"
    )
    interaction.followup.send.assert_awaited_once()
    kwargs = interaction.followup.send.call_args.kwargs
    assert kwargs.get("ephemeral") is True, "the confirmation reply must be ephemeral"


async def test_submitting_from_a_different_user_writes_nothing(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, workspace_id="guild-wronguser")
        account = await make_account(session, tenant=tenant)
    feedback_id = await _seed_vote(
        db_session_factory, tenant_id=tenant.id, account_id=account.id, message_id="msg-wronguser"
    )
    runtime = _runtime(sessionmaker=db_session_factory)
    modal = _modal(runtime=runtime, feedback_id=feedback_id, text="not my row")

    interaction = _interaction(user_id=_OTHER_USER_ID)
    await modal.on_submit(interaction)

    stored_text = await _read_back_feedback_text(
        db_session_factory, tenant_id=tenant.id, account_id=account.id, message_id="msg-wronguser"
    )
    assert stored_text is None, "a click carrying someone else's row id must write nothing"
    message = interaction.followup.send.call_args.args[0]
    assert "expired" in message.lower(), (
        "a foreign-row submission must report unavailability, not distinguish it from a missing row"
    )


async def test_submitting_for_a_missing_feedback_id_reports_unavailable_and_writes_nothing(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    runtime = _runtime(sessionmaker=db_session_factory)
    modal = _modal(runtime=runtime, feedback_id=uuid.uuid4(), text="ghost row")

    interaction = _interaction(user_id=_VOTER_ID)
    await modal.on_submit(interaction)

    message = interaction.followup.send.call_args.args[0]
    assert "expired" in message.lower(), (
        "a missing row must report the same unavailability message as a foreign row"
    )


async def test_submitting_whitespace_only_text_writes_nothing(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, workspace_id="guild-whitespace")
        account = await make_account(session, tenant=tenant)
    feedback_id = await _seed_vote(
        db_session_factory, tenant_id=tenant.id, account_id=account.id, message_id="msg-whitespace"
    )
    runtime = _runtime(sessionmaker=db_session_factory)
    modal = _modal(runtime=runtime, feedback_id=feedback_id, text="   \n\t  ")

    interaction = _interaction(user_id=_VOTER_ID)
    await modal.on_submit(interaction)

    stored_text = await _read_back_feedback_text(
        db_session_factory, tenant_id=tenant.id, account_id=account.id, message_id="msg-whitespace"
    )
    assert stored_text is None, "whitespace-only text must never be written"
    message = interaction.followup.send.call_args.args[0]
    assert message == "Write a few words first."


async def test_second_submission_overwrites_the_first(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, workspace_id="guild-overwrite")
        account = await make_account(session, tenant=tenant)
    feedback_id = await _seed_vote(
        db_session_factory, tenant_id=tenant.id, account_id=account.id, message_id="msg-overwrite"
    )
    runtime = _runtime(sessionmaker=db_session_factory)

    first_modal = _modal(runtime=runtime, feedback_id=feedback_id, text="first draft")
    await first_modal.on_submit(_interaction(user_id=_VOTER_ID))

    second_modal = _modal(runtime=runtime, feedback_id=feedback_id, text="second, better draft")
    await second_modal.on_submit(_interaction(user_id=_VOTER_ID))

    stored_text = await _read_back_feedback_text(
        db_session_factory, tenant_id=tenant.id, account_id=account.id, message_id="msg-overwrite"
    )
    assert stored_text == "second, better draft", "the last submission must win"


async def test_submitted_text_never_appears_in_a_log_event(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, workspace_id="guild-loghygiene")
        account = await make_account(session, tenant=tenant)
    feedback_id = await _seed_vote(
        db_session_factory, tenant_id=tenant.id, account_id=account.id, message_id="msg-loghygiene"
    )
    runtime = _runtime(sessionmaker=db_session_factory)
    secret_text = "the model hallucinated a nonexistent API endpoint"
    modal = _modal(runtime=runtime, feedback_id=feedback_id, text=secret_text)

    with structlog.testing.capture_logs() as captured_logs:
        await modal.on_submit(_interaction(user_id=_VOTER_ID))

    for event in captured_logs:
        assert secret_text not in repr(event), (
            "the submitted free text must never reach a structlog event"
        )


# ---------------------------------------------------------------------------
# SupportSettings.feedback_to_support: the submitted text also goes to the
# support channel, for a tenant that turned it on.
# ---------------------------------------------------------------------------

_SUPPORT_CHANNEL_ID = "300000000000000003"


def _routed_runtime(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    routed: dict[uuid.UUID, bool],
    channel_id: str | None = _SUPPORT_CHANNEL_ID,
) -> DiscordRuntime:
    from daimon.core.config import SupportSettings

    runtime = _runtime(sessionmaker=sessionmaker)
    runtime.settings.support = SupportSettings(  # pyright: ignore[reportAttributeAccessIssue]  # MagicMock settings
        escalation_channel_id=channel_id, feedback_to_support=routed
    )
    return runtime


def _interaction_with_channel(*, user_id: str) -> tuple[MagicMock, MagicMock]:
    import discord

    interaction = _interaction(user_id=user_id)
    interaction.user.mention = f"<@{user_id}>"
    interaction.user.__str__.return_value = "critic"  # pyright: ignore[reportAttributeAccessIssue]
    channel = MagicMock(spec=discord.TextChannel)
    channel.send = AsyncMock()
    interaction.client.get_channel.return_value = channel
    return interaction, channel


async def _routed_setup(
    db_session_factory: async_sessionmaker[AsyncSession], *, guild: str, message_id: str
) -> tuple[uuid.UUID, uuid.UUID]:
    from daimon.testing.factories import make_thread_session

    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, workspace_id=guild)
        account = await make_account(session, tenant=tenant)
        await make_thread_session(
            session,
            tenant=tenant,
            account=account,
            platform="discord",
            thread_id="chan-1",
            ma_session_id="sess_a",
            ma_agent_id="agent_d",
        )
    feedback_id = await _seed_vote(
        db_session_factory, tenant_id=tenant.id, account_id=account.id, message_id=message_id
    )
    return tenant.id, feedback_id


async def test_routed_text_posts_once_to_the_support_channel_without_pings(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    import discord

    tenant_id, feedback_id = await _routed_setup(
        db_session_factory, guild="guild-routed", message_id="msg-routed"
    )
    runtime = _routed_runtime(sessionmaker=db_session_factory, routed={tenant_id: True})
    modal = _modal(runtime=runtime, feedback_id=feedback_id, text="wrong totals @everyone")
    interaction, channel = _interaction_with_channel(user_id=_VOTER_ID)

    await modal.on_submit(interaction)

    interaction.client.get_channel.assert_called_once_with(int(_SUPPORT_CHANNEL_ID))
    channel.send.assert_awaited_once()
    body = channel.send.call_args.args[0]
    lines = body.split("\n")
    assert lines[0] == f"**\N{THUMBS DOWN SIGN} Feedback** from <@{_VOTER_ID}> (critic)"
    assert lines[1] == "https://discord.com/channels/guild-routed/chan-1/msg-routed"
    assert lines[2] == "Agent `agent_d`, session `sess_a`"
    assert "@everyone" not in lines[4] and "wrong totals" in lines[4]
    mentions = channel.send.call_args.kwargs["allowed_mentions"]
    assert mentions.everyone is False and mentions.users is False
    assert isinstance(mentions, discord.AllowedMentions)
    interaction.followup.send.assert_awaited_once()


async def test_unrouted_text_stays_in_the_database(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _tenant_id, feedback_id = await _routed_setup(
        db_session_factory, guild="guild-unrouted", message_id="msg-unrouted"
    )
    runtime = _routed_runtime(sessionmaker=db_session_factory, routed={uuid.uuid4(): True})
    modal = _modal(runtime=runtime, feedback_id=feedback_id, text="wrong")
    interaction, channel = _interaction_with_channel(user_id=_VOTER_ID)

    await modal.on_submit(interaction)

    channel.send.assert_not_awaited()
    interaction.client.get_channel.assert_not_called()


async def test_someone_elses_row_is_never_routed(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, feedback_id = await _routed_setup(
        db_session_factory, guild="guild-other", message_id="msg-other"
    )
    runtime = _routed_runtime(sessionmaker=db_session_factory, routed={tenant_id: True})
    modal = _modal(runtime=runtime, feedback_id=feedback_id, text="not mine")
    interaction, channel = _interaction_with_channel(user_id=_OTHER_USER_ID)

    await modal.on_submit(interaction)

    channel.send.assert_not_awaited()
