"""One read rule for every lead reader: each step refuses on its own."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from daimon.core.access_policy import AgentRule, ChannelRule, TenantAccessPolicy
from daimon.core.authz import AgentRef, Place
from daimon.core.lead_intake import (
    IntakeConfig,
    IntakeRecord,
    LeadDestination,
    LeadEffectState,
    LeadProgress,
)
from daimon.core.lead_intake_access import (
    LeadReadDenied,
    LeadReader,
    LeadReadTarget,
    NativeChannel,
    authorize_lead_read,
    discovery_target,
    read_target,
)
from daimon.core.membership import Membership

TENANT = uuid4()
AGENT = uuid4()
GUILD, SALES, THREAD, HOME = "1", "20", "30", "40"


def _config(channel: str = SALES) -> IntakeConfig:
    return IntakeConfig(
        agent_id=AGENT,
        guild_id=GUILD,
        channel_id=channel,
        source_connection_id="gmail",
        gmail_mailbox="owner@example.test",
        crm_connection_id="crm",
        portal_id="3",
        reconciliation_start=datetime.now(UTC),
    )


def _channel(channel_id: str = SALES, **changes: object) -> NativeChannel:
    channel = NativeChannel(
        id=channel_id,
        guild_id=GUILD,
        parent_id=None,
        category_id=None,
        is_text=True,
        is_thread=False,
        private_thread=False,
        everyone_can_view=False,
        can_view=True,
        can_read_history=True,
        in_thread_or_manages=True,
    )
    return replace(channel, **changes)  # pyright: ignore[reportArgumentType]


def _thread(**changes: object) -> NativeChannel:
    thread = _channel(THREAD, parent_id=SALES, is_text=False, is_thread=True)
    return replace(thread, **changes)  # pyright: ignore[reportArgumentType]


class FakeNative:
    def __init__(self, *channels: NativeChannel) -> None:
        self.channels = {channel.id: channel for channel in channels}
        self.asked: list[str] = []

    async def channel(self, channel_id: str) -> NativeChannel | None:
        self.asked.append(channel_id)
        return self.channels.get(channel_id)


def _reader(**changes: object) -> LeadReader:
    reader = LeadReader(
        tenant_id=TENANT,
        platform_user_id="7",
        facts=Membership(is_member=True, role_ids=("42",)),
        policy=TenantAccessPolicy(invoker_role_ids=("42",)),
        config=_config(),
        agent_id=AGENT,
    )
    return replace(reader, **changes)  # pyright: ignore[reportArgumentType]


def _target(**changes: object) -> LeadReadTarget:
    target = LeadReadTarget(tenant_id=TENANT, guild_id=GUILD, channel_id=SALES)
    return replace(target, **changes)  # pyright: ignore[reportArgumentType]


async def _reason(target: LeadReadTarget, reader: LeadReader, native: FakeNative) -> str | None:
    try:
        await authorize_lead_read(target, reader, native)
    except LeadReadDenied as exc:
        return str(exc)
    return None


async def test_a_reader_of_the_parent_and_thread_history_is_allowed() -> None:
    assert await _reason(_target(), _reader(), FakeNative(_channel())) is None
    native = FakeNative(_channel(), _thread())
    assert await _reason(_target(thread_id=THREAD), _reader(), native) is None
    assert native.asked == [SALES, THREAD]


@pytest.mark.parametrize(
    "target,reason",
    [
        (_target(tenant_id=UUID(int=1)), "lead_tenant_mismatch"),
        (_target(guild_id="other"), "lead_tenant_mismatch"),
        (_target(channel_id=""), "lead_destination_missing"),
        (_target(thread_unresolvable=True), "dossier_thread_reconciliation_required"),
    ],
)
async def test_a_target_outside_the_reader_or_unreconciled_is_refused(
    target: LeadReadTarget, reason: str
) -> None:
    assert await _reason(target, _reader(), FakeNative(_channel())) == reason


async def test_admission_is_live_and_routines_need_their_execution() -> None:
    native = FakeNative(_channel())
    outsider = _reader(facts=Membership(is_member=False))
    assert await _reason(_target(), outsider, native) == "intake_actor_not_admitted"
    stranger = _reader(facts=Membership(is_member=True, role_ids=("99",)))
    assert await _reason(_target(), stranger, native) == "invoker_not_allowed"
    other_agent = _reader(agent_id=uuid4())
    assert await _reason(_target(), other_agent, native) == "intake_actor_not_admitted"
    routine = _reader(routine_id=uuid4(), execution_active=True)
    assert await _reason(_target(), routine, native) == "routine_not_authorized"


async def test_a_resolver_read_needs_the_live_business_grant() -> None:
    reader = _reader(facts=Membership(is_member=True, role_ids=("42",)), resolve=True)
    assert await _reason(_target(), reader, FakeNative(_channel())) is None
    reader = _reader(
        policy=TenantAccessPolicy(invoker_user_ids=("7",)),
        facts=Membership(is_member=True),
        resolve=True,
    )
    reason = await _reason(_target(), reader, FakeNative(_channel()))
    assert reason == "sales_resolver_or_core_role_required"


@pytest.mark.parametrize(
    "parent,reason",
    [
        (None, "sales_channel_required"),
        (_channel(is_text=False), "sales_channel_required"),
        (_channel(guild_id="other"), "sales_channel_required"),
        (_channel(everyone_can_view=True), "restricted_sales_channel_required"),
        (_channel(can_view=False), "sales_channel_history_required"),
        # View without history, and no thread yet: still refused.
        (_channel(can_read_history=False), "sales_channel_history_required"),
    ],
)
async def test_the_parent_needs_restricted_view_and_history(
    parent: NativeChannel | None, reason: str
) -> None:
    native = FakeNative(*([parent] if parent else []))
    assert await _reason(_target(), _reader(), native) == reason


@pytest.mark.parametrize(
    "thread,reason",
    [
        (None, "dossier_thread_mismatch"),
        (_thread(parent_id="other"), "dossier_thread_mismatch"),
        (_thread(is_thread=False), "dossier_thread_mismatch"),
        (_thread(can_read_history=False), "dossier_thread_history_required"),
        (_thread(can_view=False), "dossier_thread_history_required"),
        (
            _thread(private_thread=True, in_thread_or_manages=False),
            "dossier_thread_membership_required",
        ),
    ],
)
async def test_the_thread_is_checked_even_when_the_parent_passes(
    thread: NativeChannel | None, reason: str
) -> None:
    native = FakeNative(_channel(), *([thread] if thread else []))
    assert await _reason(_target(thread_id=THREAD), _reader(), native) == reason


async def test_a_private_thread_member_reads_it() -> None:
    native = FakeNative(_channel(), _thread(private_thread=True, in_thread_or_manages=True))
    assert await _reason(_target(thread_id=THREAD), _reader(), native) is None


async def test_channel_rules_apply_to_parent_and_thread() -> None:
    inside = TenantAccessPolicy(
        invoker_role_ids=("42",), channel_rules={THREAD: ChannelRule(readers="inside")}
    )
    native = FakeNative(_channel(), _thread())
    reason = await _reason(_target(thread_id=THREAD), _reader(policy=inside), native)
    assert reason == "lead_read_not_a_reader"
    from_inside = _reader(policy=inside, origin_channel_ids=frozenset({THREAD}))
    assert await _reason(_target(thread_id=THREAD), from_inside, native) is None


async def test_an_agent_held_to_its_own_channel_reads_no_lead_elsewhere() -> None:
    # The agent is HOME's own; the person can read the sales channel, and the
    # call runs in an admin DM, outside every channel. Nothing may be carried home.
    policy = TenantAccessPolicy(
        invoker_role_ids=("42",),
        channel_rules={HOME: ChannelRule(readers="own", writers="own")},
        agent_rules={"sales": AgentRule(runs_in=(HOME,))},
    )
    reader = _reader(policy=policy, agent=AgentRef.of("sales"), origin=Place())
    assert await _reason(_target(), reader, FakeNative(_channel())) == "lead_read_own_agents_only"
    unresolved = _reader(policy=policy, agent=AgentRef.unresolved())
    reason = await _reason(_target(), unresolved, FakeNative(_channel()))
    assert reason == "lead_read_agent_unresolved"
    # A person's own reply carries nothing: no agent, no hold.
    assert await _reason(_target(), _reader(policy=policy), FakeNative(_channel())) is None


def _record(**changes: object) -> IntakeRecord:
    now = datetime.now(UTC)
    record = IntakeRecord(
        id=uuid4(),
        tenant_id=TENANT,
        source_digest="digest",
        status="pending",
        submission=None,
        destination=LeadDestination(guild_id=GUILD, channel_id=SALES),
        config_revision=1,
        created_at=now,
        expires_at=now + timedelta(days=1),
        raw_expires_at=now + timedelta(hours=1),
    )
    return record.model_copy(update=changes)


def _effect(step: str, remote_id: str | None) -> LeadEffectState:
    return LeadEffectState(
        step=step,
        stable_key="key",
        state="completed" if remote_id else "uncertain",
        remote_id=remote_id,
        created_at=datetime.now(UTC),
    )


def test_read_target_uses_the_captured_destination_and_actual_thread() -> None:
    assert read_target(_record()) == _target()
    threaded = _record(progress=LeadProgress(discord_thread_id=THREAD))
    assert read_target(threaded).thread_id == THREAD
    from_effect = _record(effects=[_effect("thread", THREAD)])
    assert read_target(from_effect).thread_id == THREAD
    # An uncertain creation reads the thread Discord would open at the starter.
    uncertain = _record(effects=[_effect("starter", "55"), _effect("thread", None)])
    assert read_target(uncertain).thread_id == "55"
    lost = _record(effects=[_effect("thread", None)])
    assert read_target(lost).thread_unresolvable
    with pytest.raises(LeadReadDenied, match="lead_destination_missing"):
        read_target(_record(destination=None))


def test_a_discovery_is_read_against_the_current_destination() -> None:
    target = discovery_target(_config(channel="B"), TENANT)
    assert (target.guild_id, target.channel_id, target.thread_id) == (GUILD, "B", None)
