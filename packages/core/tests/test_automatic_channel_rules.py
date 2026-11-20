"""The automatic narrow rule: a narrow room reads `inside`, computed on load, never stored.

Rooms below are fictional: OPEN reads like any room, NARROW has a computed `inside` rule.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

import pytest
import structlog.testing
from anthropic.types.beta import BetaEnvironment
from anthropic.types.beta.beta_cloud_config_params import BetaCloudConfigParams
from daimon.core import approval_gate, workspace_settings
from daimon.core import channel_environments as environments
from daimon.core.access_policy import (
    OPEN_RULE,
    ChannelRule,
    TenantAccessPolicy,
    attach_automatic_rules,
)
from daimon.core.audience_facts import UNKNOWN_FACTS, RoomFacts
from daimon.core.authz import AgentRef, Subject
from daimon.core.channel_audience import AudienceMode
from daimon.core.channel_changes import ChannelActor, Expected, set_access
from daimon.core.channel_directory import _tags  # pyright: ignore[reportPrivateUsage]
from daimon.core.channel_environments import (
    _sealed_moves,  # pyright: ignore[reportPrivateUsage]
    update_needs_confirm,
)
from daimon.core.channel_rules import ChannelRuleRefused, set_category_rule, set_channel_rule
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME
from daimon.core.direct_messages import sealed_among
from daimon.core.mcp_auth import coding_token_channel
from daimon.core.permissions import (
    agent_permissions,
    any_readers_limited,
    channel_permissions,
    channel_rule,
    effective_channel_rule,
    limited_among,
    limited_ids,
    limiting_ids_at,
    memory_writable,
    readable_from,
    sealed_outside,
    session_readable_from,
    with_channel_rule,
)
from daimon.core.scope import ChannelScopeRef, DeploymentDefault
from daimon.core.stores import access_policy as store
from daimon.core.stores.audience_facts import StoredRoom, mark_unknown, publish_facts
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.turn.deps import TurnDeps
from daimon.testing.factories import make_tenant
from mux.testing.drivers.anthropic.ma import (
    FakeMAState,
    build_fake_anthropic,
    make_fake_ma_handler,
)
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

OPEN = "1" * 17
NARROW = "3" * 17
HAND_OWN = "4" * 17
OTHER_OPEN = "5" * 17
UNLISTED = "9" * 17
ADMIN = Subject(is_admin=True, platform_user_id="7" * 17)
INSIDE = ChannelRule(readers="inside")
IN_NARROW = Subject(
    is_admin=True, platform_user_id="7" * 17, origin_channel_ids=frozenset({NARROW})
)
DEFAULT = DeploymentDefault(agent_name="daimon")


def _policy(**stored: object) -> TenantAccessPolicy:
    return attach_automatic_rules(
        TenantAccessPolicy.model_validate(stored),
        {OPEN: OPEN_RULE, NARROW: INSIDE, OTHER_OPEN: OPEN_RULE},
        unknown_narrow=False,
    )


# --- the model ---------------------------------------------------------------------------


def test_a_stored_payload_with_an_automatic_key_is_rejected() -> None:
    for key, value in (
        ("automatic_channel_rules", {NARROW: {"readers": "inside"}}),
        ("automatic_unknown_narrow", True),
    ):
        with pytest.raises(ValueError, match="never stored"):
            TenantAccessPolicy.model_validate({key: value})


def test_automatic_rules_are_never_dumped_and_a_rebuilt_policy_starts_without_them() -> None:
    policy = _policy()
    assert policy.automatic_channel_rules and "automatic" not in policy.model_dump_json()
    assert "automatic_channel_rules" not in policy.model_dump()
    rebuilt = with_channel_rule(policy, HAND_OWN, ChannelRule(writers="none"))
    assert rebuilt.automatic_channel_rules == {}, "a writer builds from stored rules only"
    assert rebuilt.model_dump()["channel_rules"] == {
        HAND_OWN: {"readers": "any", "writers": "none"}
    }


def test_a_no_op_edit_compares_equal_with_or_without_automatic_rules() -> None:
    stored = TenantAccessPolicy(channel_rules={HAND_OWN: ChannelRule(writers="none")})
    assert _policy(channel_rules={HAND_OWN: {"writers": "none"}}) == stored
    assert _policy(channel_rules={HAND_OWN: {"writers": "none"}}) != TenantAccessPolicy()


# --- the merge ---------------------------------------------------------------------------


def test_a_hand_set_rule_beats_the_automatic_inside_which_beats_any() -> None:
    policy = _policy(
        channel_rules={
            NARROW: {"readers": "own", "writers": "own"},
            OTHER_OPEN: {"writers": "none"},
        }
    )
    assert effective_channel_rule(policy, NARROW).readers == "own", "own > automatic inside"
    assert effective_channel_rule(policy, OTHER_OPEN) == ChannelRule(writers="none")
    narrow_only = _policy()
    assert effective_channel_rule(narrow_only, NARROW) == INSIDE
    assert effective_channel_rule(narrow_only, OPEN) == OPEN_RULE
    hand_inside = _policy(channel_rules={OPEN: {"readers": "inside"}})
    assert effective_channel_rule(hand_inside, OPEN).readers == "inside", "inside > any"


def test_a_writer_that_reads_channel_rule_sees_only_the_stored_rule() -> None:
    policy = _policy(channel_rules={OTHER_OPEN: {"writers": "none"}})
    assert channel_rule(policy, NARROW) == OPEN_RULE, "never saved as a hand-set rule"
    assert channel_rule(policy, OTHER_OPEN) == ChannelRule(writers="none")
    assert set(policy.channel_rules) == {OTHER_OPEN}


def test_a_stored_writers_rule_on_a_narrow_room_still_reads_inside() -> None:
    policy = _policy(channel_rules={NARROW: {"writers": "none"}})
    assert effective_channel_rule(policy, NARROW) == ChannelRule(readers="inside", writers="none")
    assert NARROW in limited_ids(policy)


def test_unknown_counts_as_narrow_for_a_discord_room_and_nothing_else() -> None:
    policy = attach_automatic_rules(TenantAccessPolicy(), {OPEN: OPEN_RULE}, unknown_narrow=True)
    assert effective_channel_rule(policy, UNLISTED).readers == "inside"
    assert effective_channel_rule(policy, OPEN).readers == "any", "a known open room is open"
    for not_a_room in ("dm:someone", "C01AB:1700000000.000200", "web"):
        assert effective_channel_rule(policy, not_a_room).readers == "any"
    assert any_readers_limited(policy)
    assert not any_readers_limited(TenantAccessPolicy())


def test_a_policy_with_no_automatic_rules_enforces_what_it_stored() -> None:
    policy = TenantAccessPolicy(channel_rules={HAND_OWN: ChannelRule(readers="inside")})
    assert limited_ids(policy) == {HAND_OWN}
    assert effective_channel_rule(policy, UNLISTED) == OPEN_RULE
    assert not limiting_ids_at(policy, channel_id=UNLISTED, thread_id=None)


# --- spec checks 6 and 7: what a turn can read from where -------------------------------


def test_check_6_a_turn_in_a_narrow_room_reads_an_open_room() -> None:
    policy = _policy()
    assert readable_from(policy, {NARROW}, OTHER_OPEN)
    assert readable_from(policy, {NARROW}, NARROW)


def test_check_7_a_turn_in_an_open_room_cannot_read_the_narrow_room_or_its_sessions() -> None:
    policy = _policy()
    assert not readable_from(policy, {OPEN}, NARROW)
    assert not readable_from(policy, {OPEN}, "6" * 17, parent_channel_id=NARROW), "its threads"
    seal = limiting_ids_at(policy, channel_id=NARROW, thread_id=None)
    assert seal == {NARROW}
    session = {"channel": NARROW, "thread": None, "seal_ids": seal, "legacy_thread_id": None}
    assert not session_readable_from(policy, {OPEN}, **session)
    assert session_readable_from(policy, {NARROW}, **session)
    still_sealed = _policy().model_copy(update={"automatic_channel_rules": {NARROW: OPEN_RULE}})
    assert not session_readable_from(still_sealed, {OPEN}, **session), "stamped seals stay"


def test_check_7_memory_written_in_a_narrow_room_is_read_only_there() -> None:
    """Admission makes memory read-only for any sealed turn: the seal and the memory rule."""
    policy = _policy()
    agent = agent_permissions(policy, ("helper",))
    for room, writable in ((NARROW, False), (OPEN, True)):
        here = channel_permissions(policy, channel_id=room)
        assert memory_writable(agent, here) is writable
        assert bool(limiting_ids_at(policy, channel_id=room, thread_id=None)) is not writable


def test_the_display_and_token_callers_see_the_automatic_rule() -> None:
    policy = _policy()
    assert _tags(policy, NARROW, False) == ("kept_inside",)
    assert _tags(policy, OPEN, False) == ()
    agent = AgentRef.of("helper")
    assert coding_token_channel(policy, agent=agent, channel_id=NARROW) == NARROW
    assert coding_token_channel(policy, agent=agent, channel_id=OPEN) is None


# --- the loader ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_shadow_log() -> None:
    store._SHADOW_LOGGED.clear()  # pyright: ignore[reportPrivateUsage]


def _mode(monkeypatch: pytest.MonkeyPatch, mode: AudienceMode) -> None:
    async def read(*_: object, **__: object) -> AudienceMode:
        return mode

    monkeypatch.setattr(store, "audience_mode", read)


async def _publish(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    usable: bool = True,
    fresh_for: timedelta = timedelta(minutes=10),
    open_rooms: tuple[str, ...] = (),
) -> None:
    now = datetime.now(UTC)
    await publish_facts(
        session,
        tenant_id=tenant_id,
        rooms={
            OPEN: StoredRoom(RoomFacts(contained=False, narrow=False)),
            NARROW: StoredRoom(RoomFacts(contained=False, narrow=True)),
            "8" * 17: StoredRoom(UNKNOWN_FACTS),
            **{room: StoredRoom(RoomFacts(contained=False, narrow=False)) for room in open_rooms},
        },
        now=now,
        fresh_until=now + fresh_for,
    )
    if not usable:
        await mark_unknown(session, tenant_id=tenant_id, now=now)
    await session.commit()


async def test_mode_off_loads_the_stored_policy_and_nothing_more(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session, platform="discord", workspace_id="4101")
    await _publish(db_session, tenant.id)
    policy = await store.load_access_policy(db_session, tenant_id=tenant.id)
    assert policy.automatic_channel_rules == {} and not policy.automatic_unknown_narrow


async def test_mode_shadow_computes_and_logs_without_enforcing(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mode(monkeypatch, "shadow")
    tenant = await make_tenant(db_session, platform="discord", workspace_id="4102")
    await _publish(db_session, tenant.id)
    with structlog.testing.capture_logs() as logs:
        policy = await store.load_access_policy(db_session, tenant_id=tenant.id)
        await store.load_access_policy(db_session, tenant_id=tenant.id)
    assert policy.automatic_channel_rules == {} and not limited_ids(policy)
    events = [entry for entry in logs if entry["event"] == "audience.shadow"]
    assert len(events) == 1, "one line a minute for a server"
    assert events[0]["kind"] == "narrow_rule" and events[0]["tenant_id"] == str(tenant.id)
    assert events[0]["reason"] == "narrow_rooms=1 facts_usable=True"
    assert {"channel_id", "agent"} <= events[0].keys()


async def test_mode_on_fills_narrow_rooms_from_the_stored_facts(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mode(monkeypatch, "on")
    tenant = await make_tenant(db_session, platform="discord", workspace_id="4103")
    await _publish(db_session, tenant.id)
    policy = await store.load_access_policy(db_session, tenant_id=tenant.id)
    assert policy.automatic_channel_rules == {OPEN: OPEN_RULE, NARROW: INSIDE}
    assert limited_ids(policy) == {NARROW}
    assert effective_channel_rule(policy, "8" * 17).readers == "inside", "no fact: narrow"
    assert effective_channel_rule(policy, OPEN).readers == "any"


async def test_mode_on_with_lapsed_facts_makes_every_discord_room_narrow(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mode(monkeypatch, "on")
    tenant = await make_tenant(db_session, platform="discord", workspace_id="4104")
    await _publish(db_session, tenant.id, usable=False)
    policy = await store.load_access_policy(db_session, tenant_id=tenant.id)
    assert policy.automatic_channel_rules == {}
    assert effective_channel_rule(policy, OPEN).readers == "inside"


async def test_mode_on_leaves_a_server_of_another_platform_alone(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mode(monkeypatch, "on")
    tenant = await make_tenant(db_session, platform="slack", workspace_id="T4105")
    policy = await store.load_access_policy(db_session, tenant_id=tenant.id)
    assert not policy.automatic_unknown_narrow and policy.automatic_channel_rules == {}


async def test_a_stored_payload_carrying_the_key_is_unreadable(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, platform="discord", workspace_id="4106")
    await db_session.execute(
        text(
            "INSERT INTO tenant_access_policies (tenant_id, policy) VALUES (:t, CAST(:p AS jsonb))"
        ),
        {"t": tenant.id, "p": '{"automatic_channel_rules": {"1": {"readers": "inside"}}}'},
    )
    await db_session.commit()
    with pytest.raises(store.AccessPolicyUnreadable):
        await store.load_access_policy(db_session, tenant_id=tenant.id)


# --- the setup screens: no automatic rule is saved, none is lost ------------------------------

HAND = ChannelRule(writers="none")


async def _stored(session: AsyncSession, tenant_id: uuid.UUID) -> dict[str, object]:
    row = await session.execute(
        text("SELECT policy FROM tenant_access_policies WHERE tenant_id = :t"), {"t": tenant_id}
    )
    return cast("dict[str, object]", row.scalar_one())


async def test_editing_a_policy_in_each_setup_screen_saves_no_automatic_rule_and_loses_none(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mode(monkeypatch, "on")
    tenant = await make_tenant(db_session, platform="discord", workspace_id="4107")
    await store.set_access_policy(
        db_session,
        tenant_id=tenant.id,
        policy=TenantAccessPolicy(channel_rules={OTHER_OPEN: HAND}),
    )
    await _publish(db_session, tenant.id, open_rooms=(HAND_OWN, OTHER_OPEN))
    original = await _stored(db_session, tenant.id)

    async def check(label: str) -> None:
        async with db_session_factory() as session:
            stored = await _stored(session, tenant.id)
            policy = await store.load_access_policy(session, tenant_id=tenant.id)
        assert "automatic" not in str(stored), f"{label}: nothing automatic was saved"
        assert policy.automatic_channel_rules.get(NARROW) == INSIDE, f"{label}: none was lost"
        assert effective_channel_rule(policy, NARROW).readers == "inside", label
        assert policy.channel_rules.get(OTHER_OPEN) == HAND, f"{label}: hand-set rules stay"
        assert all(
            rule.readers != "inside" for key, rule in policy.channel_rules.items() if key == NARROW
        ), f"{label}: the automatic rule was not copied in as a hand-set one"

    # Channel settings (Discord, Slack, Teams, web, CLI, MCP): a rule on another room.
    client = build_fake_anthropic(make_fake_ma_handler(FakeMAState()))
    await set_channel_rule(
        client,
        db_session_factory,
        tenant_id=tenant.id,
        platform="discord",
        channel_id=HAND_OWN,
        readers="inside",
        default=DEFAULT,
        actor_account_id=None,
        subject=ADMIN,
    )
    await check("channel rule")

    # A narrow room's own settings screen: the person sets writers and leaves readers.
    await set_channel_rule(
        client,
        db_session_factory,
        tenant_id=tenant.id,
        platform="discord",
        channel_id=NARROW,
        writers="none",
        default=DEFAULT,
        actor_account_id=None,
        subject=IN_NARROW,
    )
    await check("writers on the narrow room")
    async with db_session_factory() as session:
        policy = await store.load_access_policy(session, tenant_id=tenant.id)
    assert policy.channel_rules[NARROW] == ChannelRule(readers="any", writers="none"), (
        "readers stay as the person left them; the automatic rule is not copied in"
    )

    # Category rule screen.
    await set_category_rule(
        db_session_factory,
        tenant_id=tenant.id,
        category_id="2" * 17,
        writers="none",
        subject=ADMIN,
    )
    await check("category rule")

    # Approval gate, for the server and for a room.
    await approval_gate.set_approval_gate(
        db_session_factory,
        tenant_id=tenant.id,
        platform="discord",
        target=None,
        value="on",
        subject=ADMIN,
    )
    await approval_gate.set_approval_gate(
        db_session_factory,
        tenant_id=tenant.id,
        platform="discord",
        target=NARROW,
        value="on",
        subject=IN_NARROW,
    )
    await check("approvals")

    # Invokers (workspace settings), which rebuilds from a dump.
    async with db_session_factory() as session:
        await workspace_settings.set_invokers(
            session, tenant_id=tenant.id, subject=ADMIN, platform="discord", user_ids=["6" * 17]
        )
        await session.commit()
    await check("invokers")

    assert original != await _stored(db_session, tenant.id)


async def test_saving_the_loaded_policy_unchanged_stores_exactly_what_was_stored(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Discord permissions screen copies the loaded policy (`model_copy`) and saves it."""
    _mode(monkeypatch, "on")
    tenant = await make_tenant(db_session, platform="discord", workspace_id="4108")
    await store.set_access_policy(
        db_session,
        tenant_id=tenant.id,
        policy=TenantAccessPolicy(channel_rules={OTHER_OPEN: HAND}),
    )
    await _publish(db_session, tenant.id)
    before = await _stored(db_session, tenant.id)
    loaded = await store.load_access_policy(db_session, tenant_id=tenant.id)
    assert loaded.automatic_channel_rules

    screen = loaded.model_copy(update={"internal_role_ids": ("6" * 17,)})
    cli = TenantAccessPolicy.model_validate(loaded.model_dump() | {"invoker_role_ids": ()})
    await store.set_access_policy(db_session, tenant_id=tenant.id, policy=screen)
    assert await _stored(db_session, tenant.id) == {**before, "internal_role_ids": ["6" * 17]}
    await store.set_access_policy(db_session, tenant_id=tenant.id, policy=cli)
    assert await _stored(db_session, tenant.id) == before


# --- review fixes -------------------------------------------------------------------------


def test_equal_policies_hash_alike_whatever_the_mapping_order() -> None:
    first = {OPEN: ChannelRule(writers="none"), NARROW: ChannelRule(readers="inside")}
    one = TenantAccessPolicy(channel_rules=first)
    two = TenantAccessPolicy(channel_rules=dict(reversed(first.items())))
    assert one == two and hash(one) == hash(two) and len({one, two}) == 1
    assert hash(_policy()) == hash(TenantAccessPolicy()), "automatic rules are not content"


def test_a_settings_change_from_outside_a_narrow_room_is_refused_and_writes_stay_stored() -> None:
    policy = _policy()
    assert sealed_outside(policy, {OPEN}, channel_ids=(NARROW,)) == NARROW
    assert sealed_outside(policy, {NARROW}, channel_ids=(NARROW,)) is None
    assert sealed_outside(policy, {OPEN}, channel_ids=(OPEN,)) is None
    assert sealed_outside(policy, set(), channel_ids=(NARROW,), operator=True) is None
    unknown = attach_automatic_rules(TenantAccessPolicy(), {}, unknown_narrow=True)
    assert sealed_outside(unknown, {OPEN}, channel_ids=(UNLISTED,)) == UNLISTED
    assert with_channel_rule(policy, OPEN, ChannelRule(writers="none")).channel_rules.keys() == {
        OPEN
    }, "a write rebuilds from stored rules"


def test_limited_among_counts_a_room_with_no_fact_and_limited_ids_lists_known_ones() -> None:
    policy = attach_automatic_rules(
        TenantAccessPolicy(), {OPEN: OPEN_RULE, NARROW: INSIDE}, unknown_narrow=True
    )
    assert limited_among(policy, [OPEN, NARROW, UNLISTED, "dm:x"]) == {NARROW, UNLISTED}
    assert limited_ids(policy) == {NARROW}


class _Deps:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.sessionmaker = factory


@pytest.mark.parametrize("facts", ["missing", "expired", "fresh"])
async def test_a_forward_from_a_room_with_no_usable_fact_counts_as_sealed(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    facts: str,
) -> None:
    """The Discord forward filter asks about each source: `sealed_among`."""
    _mode(monkeypatch, "on")
    tenant = await make_tenant(db_session, platform="discord", workspace_id=f"41{facts[:2]}")
    if facts == "expired":
        await _publish(db_session, tenant.id, fresh_for=timedelta(seconds=-5))
    elif facts == "fresh":
        await _publish(db_session, tenant.id)
    await db_session.commit()
    sealed = await sealed_among(
        cast("TurnDeps", _Deps(db_session_factory)),
        tenant_id=tenant.id,
        ids={OPEN, NARROW, UNLISTED},
    )
    expected = {OPEN, NARROW, UNLISTED} if facts != "fresh" else {NARROW, UNLISTED}
    assert sealed == expected, "a known open room is open; a room with no fact is narrow"


@pytest.mark.parametrize("facts", ["missing", "expired", "fresh"])
async def test_a_channel_with_an_environment_pick_and_no_fact_counts_as_sealed(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    facts: str,
) -> None:
    """`_sealed_moves` (the environment checks) tests each picked channel."""
    _mode(monkeypatch, "on")
    tenant = await make_tenant(db_session, platform="discord", workspace_id=f"42{facts[:2]}")
    if facts == "expired":
        await _publish(db_session, tenant.id, fresh_for=timedelta(seconds=-5))
    elif facts == "fresh":
        await _publish(db_session, tenant.id)
    await set_fields(
        db_session,
        scope=ChannelScopeRef(tenant_id=tenant.id, channel_id=OPEN),
        tenant_id=tenant.id,
        environment_name="open",
    )
    await db_session.commit()
    policy = await store.load_access_policy(db_session, tenant_id=tenant.id)
    moves = await _sealed_moves(
        db_session,
        policy,
        tenant_id=tenant.id,
        default=DEFAULT,
        change=lambda picks: picks.without("open"),
    )
    assert moves == (frozenset() if facts == "fresh" else frozenset({"default"}))


async def test_the_web_access_write_saves_no_automatic_rule_and_loses_none(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The web channel form calls `set_access`; it sends the stored rule as `expected`."""
    _mode(monkeypatch, "on")
    tenant = await make_tenant(db_session, platform="discord", workspace_id="4301")
    await _publish(db_session, tenant.id)
    client = build_fake_anthropic(make_fake_ma_handler(FakeMAState()))
    actor = ChannelActor(tenant_id=tenant.id, subject=IN_NARROW, account_id=None, surface="web")
    await set_access(
        client,
        db_session_factory,
        actor,
        platform="discord",
        channel_id=NARROW,
        readers=None,
        writers="none",
        default=DEFAULT,
        expected=Expected(ChannelRule(readers="any", writers="any")),
    )
    async with db_session_factory() as session:
        stored = await _stored(session, tenant.id)
        policy = await store.load_access_policy(session, tenant_id=tenant.id)
    assert "automatic" not in str(stored)
    assert stored["channel_rules"] == {NARROW: {"readers": "any", "writers": "none"}}
    assert effective_channel_rule(policy, NARROW) == ChannelRule(readers="inside", writers="none")


class _Counter:
    def __init__(self, session: AsyncSession) -> None:
        self.statements: list[str] = []
        self._bind = session.sync_session.get_bind()
        event.listen(self._bind, "before_cursor_execute", self._record)

    def _record(self, _c: object, _cur: object, statement: str, *_: object) -> None:
        self.statements.append(statement)

    def close(self) -> None:
        event.remove(self._bind, "before_cursor_execute", self._record)


async def _queries(session: AsyncSession, tenant_id: uuid.UUID, *, whole: bool) -> list[str]:
    counter = _Counter(session)
    try:
        if whole:
            await store.load_access_policy(session, tenant_id=tenant_id)
        else:
            await store._load_stored_policy(session, tenant_id=tenant_id)  # pyright: ignore[reportPrivateUsage]
    finally:
        counter.close()
    return counter.statements


async def test_off_adds_one_query_and_a_second_load_in_the_session_adds_none(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session, platform="discord", workspace_id="4401")
    await db_session.commit()
    async with db_session_factory() as session:
        base = await _queries(session, tenant.id, whole=False)
    async with db_session_factory() as session:
        first = await _queries(session, tenant.id, whole=True)
        second = await _queries(session, tenant.id, whole=True)
    assert len(first) - len(base) <= 1, "the mode read is the one extra query"
    assert len(second) == len(base), "the same session does not read the mode again"


async def test_shadow_reads_the_room_rows_at_most_once_a_minute(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mode(monkeypatch, "shadow")
    tenant = await make_tenant(db_session, platform="discord", workspace_id="4402")
    await _publish(db_session, tenant.id)
    seen: list[list[str]] = []
    for _ in range(2):
        async with db_session_factory() as session:
            seen.append(await _queries(session, tenant.id, whole=True))
    rows = [sum("discord_channel_audience" in q for q in statements) for statements in seen]
    assert rows == [1, 0], "the second load, in a new session, reads no room rows"


async def test_an_admin_outside_a_narrow_room_cannot_change_its_rule(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mode(monkeypatch, "on")
    tenant = await make_tenant(db_session, platform="discord", workspace_id="4501")
    await _publish(db_session, tenant.id)
    client = build_fake_anthropic(make_fake_ma_handler(FakeMAState()))
    with pytest.raises(ChannelRuleRefused, match="inside the channel"):
        await set_channel_rule(
            client,
            db_session_factory,
            tenant_id=tenant.id,
            platform="discord",
            channel_id=NARROW,
            writers="none",
            default=DEFAULT,
            subject=ADMIN,
        )
    async with db_session_factory() as session:
        policy = await store.load_access_policy(session, tenant_id=tenant.id)
    assert policy.channel_rules == {}


async def test_a_later_load_after_a_commit_reads_the_facts_again(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The read is kept for one transaction only: facts can turn narrow between two."""
    _mode(monkeypatch, "on")
    tenant = await make_tenant(db_session, platform="discord", workspace_id="4601")
    await _publish(db_session, tenant.id, open_rooms=(HAND_OWN,))
    first = await store.load_access_policy(db_session, tenant_id=tenant.id)
    again = await store.load_access_policy(db_session, tenant_id=tenant.id)
    assert effective_channel_rule(first, HAND_OWN).readers == "any" and again == first
    await db_session.commit()
    now = datetime.now(UTC)
    await publish_facts(
        db_session,
        tenant_id=tenant.id,
        rooms={HAND_OWN: StoredRoom(RoomFacts(contained=False, narrow=True))},
        now=now,
        fresh_until=now + timedelta(minutes=10),
    )
    later = await store.load_access_policy(db_session, tenant_id=tenant.id)
    assert effective_channel_rule(later, HAND_OWN).readers == "inside"
    await db_session.commit()
    await _publish(db_session, tenant.id, fresh_for=timedelta(seconds=-5))
    expired = await store.load_access_policy(db_session, tenant_id=tenant.id)
    assert effective_channel_rule(expired, HAND_OWN).readers == "inside", "expired: narrow"
    assert expired.automatic_facts_missing


@pytest.mark.parametrize("facts", ["missing", "expired", "fresh"])
async def test_a_room_in_the_workspace_default_environment_needs_confirmation_without_facts(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, facts: str
) -> None:
    """No channel pick at all: an unknown room runs in the workspace default."""
    _mode(monkeypatch, "on")
    tenant = await make_tenant(db_session, platform="discord", workspace_id=f"47{facts[:2]}")
    if facts == "expired":
        await _publish(db_session, tenant.id, fresh_for=timedelta(seconds=-5))
    elif facts == "fresh":  # every room known and open
        now = datetime.now(UTC)
        await publish_facts(
            db_session,
            tenant_id=tenant.id,
            rooms={OPEN: StoredRoom(RoomFacts(contained=False, narrow=False))},
            now=now,
            fresh_until=now + timedelta(minutes=10),
        )
    await db_session.commit()
    policy = await store.load_access_policy(db_session, tenant_id=tenant.id)
    moves = await _sealed_moves(
        db_session,
        policy,
        tenant_id=tenant.id,
        default=DEFAULT,
        change=lambda picks: replace(picks, workspace="open"),
    )
    assert moves == (frozenset() if facts == "fresh" else frozenset({"open"}))
    monkeypatch.setattr(environments, "opens_network", lambda *_: True)
    environment = cast(
        "BetaEnvironment", SimpleNamespace(metadata={MA_METADATA_KEY_NAME: "default"})
    )
    needs = await update_needs_confirm(
        db_session,
        tenant_id=tenant.id,
        environment=environment,
        config=cast("BetaCloudConfigParams", {}),
        default=DEFAULT,
    )
    assert needs is (facts != "fresh")
