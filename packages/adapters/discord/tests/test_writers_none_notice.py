"""Opt-in denial reactions: actual ingress, persisted policies and SDK HTTP effects."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

import discord
import pytest
from daimon.adapters.discord import bot as module
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.config import DiscordSettings, ThreadParticipationSettings, TurnQueueSettings
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.tool_safety import OPEN_TOOL_SAFETY
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.protection import ProtectionState
from daimon.testing.db import DatabasePreflight, require_owned_sessionmaker
from daimon.testing.factories import make_tenant
from daimon.testing.turn_fakes import deps_for
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from structlog.testing import capture_logs

from .harness import make_bot

TENANT = derive_tenant_uuid(platform="discord", workspace_id="60")


@pytest.fixture
async def bot(
    db_preflight: DatabasePreflight, db_engine: AsyncEngine, db_clean: None, monkeypatch
) -> AsyncIterator[module.DaimonBot]:
    del db_preflight, db_clean
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)
    require_owned_sessionmaker(sessions)
    async with sessions() as session:
        await make_tenant(session, workspace_id="60")
        await session.commit()
    settings = MagicMock()
    settings.discord = DiscordSettings(bot_token=SecretStr("offline"), writers_none_notice=True)
    settings.turn_queue = TurnQueueSettings()
    settings.thread_participation = ThreadParticipationSettings()
    settings.agent_identity.enabled = False
    settings.billing.markup = Decimal("1")
    runtime = DiscordRuntime(
        settings=settings,
        anthropic=MagicMock(spec=object),  # Any native access fails; no SDK constructed.
        sessionmaker=sessions,
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
        turn_deps=replace(deps_for(), turn_path=None),
    )
    client = make_bot(runtime)
    client.wake_poller_enabled = True
    client._connection.user = discord.ClientUser(
        state=client._connection,
        data={"id": "999", "username": "offline", "discriminator": "0", "avatar": None},
    )
    monkeypatch.setattr(module, "remember_guild_user", MagicMock())
    monkeypatch.setattr(
        client.http, "send_message", AsyncMock(side_effect=AssertionError("no post"))
    )
    monkeypatch.setattr(
        client.http, "start_thread_with_message", AsyncMock(side_effect=AssertionError("no thread"))
    )
    monkeypatch.setattr(client.http, "add_reaction", AsyncMock())
    yield client
    assert client.http.send_message.await_count == 0
    assert client.http.start_thread_with_message.await_count == 0
    assert not client._bg_tasks
    await client.close()


def message(
    bot,
    *,
    channel_id=50,
    thread_id=None,
    message_id=101,
    author_id=10,
    category=None,
    cached=True,
    mention=True,
    bot_author=False,
    webhook=None,
):
    guild = MagicMock(spec=discord.Guild)
    guild.id = 60
    guild.get_member.return_value = None
    state = bot._connection
    parent = discord.TextChannel(
        state=state,
        guild=guild,
        data={
            "id": str(channel_id),
            "type": 0,
            "name": "origin",
            "position": 0,
            "permission_overwrites": [],
            "parent_id": str(category) if category else None,
            "topic": None,
            "nsfw": False,
            "last_message_id": None,
        },
    )
    guild.get_channel.return_value = parent if cached else None
    channel = parent
    if thread_id is not None:
        channel = discord.Thread(
            state=state,
            guild=guild,
            data={
                "id": str(thread_id),
                "parent_id": str(channel_id),
                "owner_id": str(author_id),
                "name": "origin-thread",
                "type": 11,
                "message_count": 0,
                "member_count": 0,
                "thread_metadata": {
                    "archived": False,
                    "auto_archive_duration": 60,
                    "archive_timestamp": "2026-10-11T00:00:00Z",
                    "locked": False,
                },
            },
        )
    return discord.Message(
        state=state,
        channel=channel,
        data={
            "id": str(message_id),
            "type": 0,
            "mentions": [
                {
                    "id": "999",
                    "username": "offline",
                    "discriminator": "0",
                    "avatar": None,
                    "bot": True,
                }
            ]
            if mention
            else [],
            "content": "<@999> hidden user text" if mention else "@everyone hidden user text",
            "author": {
                "id": str(author_id),
                "username": "requester",
                "discriminator": "0",
                "avatar": None,
                "bot": bot_author,
            },
            "attachments": [],
            "webhook_id": str(webhook) if webhook else None,
        },
    )


async def persist(bot, *, channels=(), categories=()):
    async with bot.runtime.sessionmaker() as session:
        await set_access_policy(
            session,
            tenant_id=TENANT,
            policy=TenantAccessPolicy(
                protected_channel_ids=tuple(map(str, channels)),
                protected_category_ids=tuple(map(str, categories)),
            ),
        )
        await session.commit()


@pytest.mark.parametrize("provider", ["anthropic", "openai", "gemini"])
@pytest.mark.parametrize("place", ["channel", "parent", "thread", "category"])
async def test_real_ingress_confirms_persisted_rule_and_reacts_without_native_turn(
    bot, provider, place, monkeypatch
):
    # The denial precedes provider admission; provider labels grant no native execution credit.
    bot.runtime.settings.turn.default_backend = provider
    trigger = message(
        bot,
        thread_id=70 if place in ("parent", "thread") else None,
        category=80 if place == "category" else None,
    )
    await persist(
        bot,
        channels=(70,) if place == "thread" else (50,) if place != "category" else (),
        categories=(80,) if place == "category" else (),
    )
    liveness = AsyncMock(side_effect=AssertionError("denied ingress cannot provision"))
    monkeypatch.setattr(module, "get_tenant_liveness", liveness)
    with capture_logs() as logs:
        await bot.on_message(trigger)
    assert isinstance(trigger, discord.Message)
    bot.http.add_reaction.assert_awaited_once_with(
        70 if place in ("parent", "thread") else 50, 101, "⛔"
    )
    assert any(row["event"] == "turn.skipped.writers_none" for row in logs)
    liveness.assert_not_awaited()
    assert not bot._processing and not bot._pending


def _notices(bot) -> int:
    """The opt-in notice's own reactions, apart from the default protected-channel signal."""
    return sum(call.args[2] == "⛔" for call in bot.http.add_reaction.await_args_list)


@pytest.mark.parametrize("enabled,owner", [(False, True), (True, False), (False, False)])
async def test_disabled_or_non_owner_does_not_allocate_or_call_notice(
    bot, enabled, owner, monkeypatch
):
    bot.runtime.settings.discord.writers_none_notice = enabled
    bot.wake_poller_enabled = owner
    await persist(bot, channels=(50,))
    callback = AsyncMock(side_effect=AssertionError("disabled callback must not run"))
    monkeypatch.setattr(bot, "_notice_writers_none", callback)
    await bot.on_message(message(bot))
    callback.assert_not_awaited()
    assert not hasattr(bot, "_writers_none_notice_limiter")
    # Off, the opt-in notice never reacts; the default protected-channel signal may.
    assert _notices(bot) == 0


async def test_unknown_protection_is_never_relabelled_as_confirmed(bot, monkeypatch):
    await persist(bot, channels=(50,))
    monkeypatch.setattr(
        module, "_channel_protection_state", AsyncMock(return_value=ProtectionState.UNKNOWN)
    )
    callback = AsyncMock()
    monkeypatch.setattr(bot, "_notice_writers_none", callback)
    await bot.on_message(message(bot))
    callback.assert_not_awaited()
    assert not hasattr(bot, "_writers_none_notice_limiter")


async def test_unresolved_category_alone_is_not_a_real_rule_for_notice(bot):
    await persist(bot, categories=(80,))
    trigger = message(bot, thread_id=70, category=80, cached=False)
    trigger.guild.fetch_channel = AsyncMock(
        side_effect=discord.NotFound(SimpleNamespace(status=404, reason="gone"), "gone")
    )
    await bot.on_message(trigger)
    trigger.guild.fetch_channel.assert_awaited_once()  # Existing protection read only.
    bot.http.add_reaction.assert_not_awaited()


@pytest.mark.parametrize("kind", ["unprotected", "wrong-category", "foreign-policy"])
async def test_no_reaction_without_positive_origin_rule(bot, kind):
    await persist(
        bot,
        channels=(51,) if kind == "foreign-policy" else (),
        categories=(81,) if kind == "wrong-category" else (),
    )
    await bot._notice_writers_none(message(bot, category=80), TENANT)
    bot.http.add_reaction.assert_not_awaited()


@pytest.mark.parametrize(
    "kind",
    [
        "reply-only",
        "webhook",
        "untrusted-bot",
        "self-bot",
        "foreign-tenant",
        "foreign-guild",
        "no-origin",
    ],
)
async def test_ineligible_origins_do_not_allocate_or_read_policy(bot, kind, monkeypatch):
    await persist(bot, channels=(50,))
    trigger = message(
        bot,
        mention=kind != "reply-only",
        webhook=11 if kind == "webhook" else None,
        bot_author=kind in ("untrusted-bot", "self-bot"),
        author_id=999 if kind == "self-bot" else 10,
    )
    tenant = uuid.uuid4() if kind == "foreign-tenant" else TENANT
    if kind == "foreign-guild":
        trigger.channel.guild = MagicMock(spec=discord.Guild)
        trigger.channel.guild.id = 61
    if kind == "no-origin":
        trigger.channel = SimpleNamespace(id=None)
    read = AsyncMock(side_effect=AssertionError("untrusted origin must not read"))
    monkeypatch.setattr(module, "load_access_policy", read)
    await bot._notice_writers_none(trigger, tenant)
    read.assert_not_awaited()
    assert not hasattr(bot, "_writers_none_notice_limiter")
    bot.http.add_reaction.assert_not_awaited()


async def test_trusted_qa_bot_can_receive_same_signal(bot):
    bot.runtime.settings.discord.qa_bot_user_ids = ("10",)
    await persist(bot, channels=(50,))
    await bot.on_message(message(bot, bot_author=True))
    bot.http.add_reaction.assert_awaited_once_with(50, 101, "⛔")


@pytest.mark.parametrize(
    "enabled,unprompted,confirmed",
    [(True, False, True), (False, False, True), (True, True, True), (True, False, False)],
)
async def test_late_admission_denial_keeps_real_rule_and_prompt_gates(
    bot, enabled, unprompted, confirmed, monkeypatch
):
    bot.runtime.settings.discord.writers_none_notice = enabled
    await persist(bot, channels=(50,) if confirmed else ())

    async def refused(*args, **kwargs):
        raise AdmissionDenied(reason="writers_none")

    monkeypatch.setattr(module, "admit", refused)
    await bot._orchestrate(message(bot, thread_id=70), "60", TENANT, unprompted=unprompted)
    assert _notices(bot) == int(enabled and not unprompted and confirmed)
    if enabled and not unprompted and confirmed:
        bot.http.add_reaction.assert_awaited_once_with(70, 101, "⛔")
    if not enabled or unprompted:
        assert not hasattr(bot, "_writers_none_notice_limiter")


async def test_concurrent_duplicates_reserve_before_policy_await_and_never_retry(bot, monkeypatch):
    await persist(bot, channels=(50,))
    entered, release = asyncio.Event(), asyncio.Event()
    real_read = module.load_access_policy

    async def read(*args, **kwargs):
        entered.set()
        await release.wait()
        return await real_read(*args, **kwargs)

    observed = AsyncMock(side_effect=read)
    monkeypatch.setattr(module, "load_access_policy", observed)
    first = asyncio.create_task(bot._notice_writers_none(message(bot), TENANT))
    await entered.wait()
    try:
        await bot._notice_writers_none(message(bot), TENANT)
        assert observed.await_count == 1
        assert bot.http.add_reaction.await_count == 0
    finally:
        release.set()
        await first
    await bot._notice_writers_none(message(bot), TENANT)
    observed.assert_awaited_once()
    bot.http.add_reaction.assert_awaited_once_with(50, 101, "⛔")


@pytest.mark.parametrize(
    "failure", ["policy", "http", "permission", "deleted", "rate-limit", "timeout", "lost-ack"]
)
async def test_failure_consumes_reservation_and_discloses_only_error_class(
    bot, failure, monkeypatch
):
    await persist(bot, channels=(50,))
    secret = "hidden-policy credential body"
    if failure == "policy":
        monkeypatch.setattr(module, "load_access_policy", AsyncMock(side_effect=ValueError(secret)))
    elif failure == "timeout":
        # Exercise the same deadline cancellation without spending 3s per offline probe.
        timeout = module.asyncio.timeout
        monkeypatch.setattr(module.asyncio, "timeout", lambda _seconds: timeout(0))
    else:
        error = RuntimeError(secret)
        if failure in ("permission", "deleted", "rate-limit"):
            status = {"permission": 403, "deleted": 404, "rate-limit": 429}[failure]
            error = discord.HTTPException(SimpleNamespace(status=status, reason=secret), secret)
        bot.http.add_reaction.side_effect = error
    with capture_logs() as logs:
        await bot._notice_writers_none(message(bot), TENANT)
        first_count = bot.http.add_reaction.await_count
        await bot._notice_writers_none(message(bot), TENANT)
    assert bot.http.add_reaction.await_count == first_count
    assert first_count == int(failure not in ("policy", "timeout"))
    failure_rows = [r for r in logs if r["event"] == "discord.writers_none_notice_failed"]
    assert len(failure_rows) == 1
    assert set(failure_rows[0]) == {"event", "error_type", "log_level"}
    assert secret not in repr(logs)


def reserve(limiter, now, *, channel="50", requester="10", message_id="101", tenant="60"):
    return limiter.reserve(
        tenant=tenant, channel=channel, requester=requester, message=message_id, now=now
    )


def test_cooldowns_are_per_requester_per_parent_channel_and_duplicate_message():
    limiter = module._WritersNoneNoticeLimiter()
    assert reserve(limiter, 0)
    assert not reserve(limiter, 29.999, requester="11", message_id="102")
    assert reserve(limiter, 30, requester="11", message_id="102")
    assert not reserve(limiter, 60, message_id="103")  # Original requester still cooling down.
    assert not reserve(limiter, 60, channel="51", requester="12")  # Message replay elsewhere.
    assert reserve(limiter, 300, message_id="103")


def test_global_rolling_quota_and_ttl_boundaries():
    limiter = module._WritersNoneNoticeLimiter()
    for index in range(10):
        assert reserve(limiter, 0, channel=str(index), requester=str(index), message_id=str(index))
    assert not reserve(limiter, 59.999, channel="new", message_id="new")
    assert reserve(limiter, 60, channel="new", message_id="new")
    assert len(limiter._attempts) == 1


def test_full_memo_suppresses_without_evicting_active_cooldowns(monkeypatch):
    limiter = module._WritersNoneNoticeLimiter()
    monkeypatch.setattr(limiter, "MAX_ENTRIES", 3)
    assert reserve(limiter, 0)
    before = dict(limiter._until)
    assert not reserve(limiter, 60, channel="other", message_id="other")
    assert all(limiter._until[key] == expiry for key, expiry in before.items() if expiry > 60)
    assert reserve(limiter, 300, channel="other", message_id="other")
    assert len(limiter._until) == 3


async def test_sibling_threads_and_many_users_share_parent_channel_cooldown(bot, monkeypatch):
    await persist(bot, channels=(50,))
    time = [0.0]
    monkeypatch.setattr(module, "monotonic", lambda: time[0])
    read = AsyncMock(wraps=module.load_access_policy)
    monkeypatch.setattr(module, "load_access_policy", read)
    await bot._notice_writers_none(message(bot, thread_id=70), TENANT)
    await bot._notice_writers_none(message(bot, thread_id=71, author_id=11, message_id=102), TENANT)
    read.assert_awaited_once()
    time[0] = 30
    await bot._notice_writers_none(message(bot, thread_id=71, author_id=11, message_id=102), TENANT)
    assert read.await_count == 2
    assert [(c.args[0], c.args[1], c.args[2]) for c in bot.http.add_reaction.await_args_list] == [
        (70, 101, "⛔"),
        (71, 102, "⛔"),
    ]


async def test_global_limit_suppresses_policy_reads_and_http_across_channels(bot, monkeypatch):
    await persist(bot, channels=tuple(range(50, 61)))
    monkeypatch.setattr(module, "monotonic", lambda: 0)
    read = AsyncMock(wraps=module.load_access_policy)
    monkeypatch.setattr(module, "load_access_policy", read)
    for index in range(11):
        await bot._notice_writers_none(
            message(bot, channel_id=50 + index, message_id=101 + index, author_id=10 + index),
            TENANT,
        )
    assert read.await_count == bot.http.add_reaction.await_count == 10


def test_real_settings_default_and_explicit_opt_in():
    assert DiscordSettings(bot_token=SecretStr("offline")).writers_none_notice is False
    assert (
        DiscordSettings(
            bot_token=SecretStr("offline"), writers_none_notice=True
        ).writers_none_notice
        is True
    )


async def test_actual_admission_after_policy_change_is_still_denied_before_native_calls(
    bot, monkeypatch
):
    from dataclasses import replace
    from pathlib import Path

    from daimon.core.config import McpSettings
    from daimon.core.stores.tenants import set_provision_status
    from daimon.core.turn.deps import TurnDeps

    deps = TurnDeps(
        anthropic=bot.runtime.anthropic,
        sessionmaker=bot.runtime.sessionmaker,
        deployment_default=bot.runtime.deployment_default,
        resolver_cache=bot.runtime.resolver_cache,
        defaults_root=Path("defaults"),
        mcp=McpSettings(),
        billing_config=None,
        markup=Decimal("1"),
        fernet=None,
        github_fallback_pat=None,
        github_app_id=None,
        github_app_private_key=None,
        public_url=None,
        tool_safety=OPEN_TOOL_SAFETY,
    )
    bot.runtime = replace(bot.runtime, turn_deps=deps)
    await set_provision_status(bot.runtime.sessionmaker, tenant_id=TENANT, status="ready")
    real_liveness = module.get_tenant_liveness

    async def liveness(*args, **kwargs):
        # This runs after the real early protection gate saw an open policy.
        await persist(bot, channels=(50,))
        return await real_liveness(*args, **kwargs)

    monkeypatch.setattr(module, "get_tenant_liveness", liveness)
    with capture_logs() as logs:
        await bot.on_message(message(bot, thread_id=70))
    assert any(row["event"] == "turn.skipped.writers_none" for row in logs)
    # Main sends its early-feedback reaction before admission. The denied turn
    # still adds exactly one opt-in stop reaction, without any native request.
    assert bot.http.add_reaction.await_args_list == [
        call(70, 101, "👀"),
        call(70, 101, "⛔"),
    ]
    assert not bot._processing and not bot._pending


async def test_persisted_unreadable_policy_cannot_confirm_notice(bot):
    from sqlalchemy import text

    await persist(bot, channels=(50,))
    async with bot.runtime.sessionmaker() as session:
        await session.execute(
            text(
                "UPDATE tenant_access_policies SET policy = '{\"protected_channel_ids\": 5}'::jsonb WHERE tenant_id = :tenant"
            ),
            {"tenant": TENANT},
        )
        await session.commit()
    with capture_logs() as logs:
        await bot._notice_writers_none(message(bot), TENANT)
    bot.http.add_reaction.assert_not_awaited()
    assert any(
        row["event"] == "discord.writers_none_notice_failed"
        and row["error_type"] == "AccessPolicyUnreadable"
        for row in logs
    )


def test_nested_env_flag_is_explicit_and_defaults_off(monkeypatch):
    from daimon.core.config import load_settings

    monkeypatch.setenv(
        "DAIMON_DATABASE__URL",
        "postgresql+asyncpg://daimon:daimon@localhost:5432/daimon_test_nc_N4",
    )
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "offline")
    monkeypatch.setenv("DAIMON_DISCORD__BOT_TOKEN", "offline")
    monkeypatch.delenv("DAIMON_DISCORD__WRITERS_NONE_NOTICE", raising=False)
    assert load_settings(_env_file=None).discord.writers_none_notice is False
    monkeypatch.setenv("DAIMON_DISCORD__WRITERS_NONE_NOTICE", "true")
    assert load_settings(_env_file=None).discord.writers_none_notice is True
