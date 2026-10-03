"""`authorize` decides exactly as the checks it replaced did.

Each `_old_*` function below is the decision a caller made before the checks
were routed through `daimon.core.authz.authorize`, reconstructed, with their predicates unchanged, from the
code it replaced (I/O and refusal copy stripped, the policy predicates
unchanged). Every test runs the old and new decisions over a grid of pin maps,
agent names (including empty and missing names, and a display name that
differs from the config name), places and callers, and asserts they agree on
every point. A rule that drifts from what main did fails here with the
counterexample.
"""

from __future__ import annotations

import itertools

import pytest
from daimon.core.access_policy import (
    TenantAccessPolicy,
    is_invoker_allowed,
    is_outside_agent_pin,
    is_write_protected,
)
from daimon.core.authz import (
    Action,
    AgentRef,
    Place,
    SessionFacts,
    Subject,
    Surface,
    authorize,
)
from daimon.core.permissions import readable_from

# --- the grid -----------------------------------------------------------------

_PIN_KEYS = ("", "acme", "Acme Display")
_PIN_VALUES: tuple[tuple[str, ...], ...] = ((), ("C1",), ("C1", "C2"))


def _pin_maps() -> list[dict[str, tuple[str, ...]]]:
    maps: list[dict[str, tuple[str, ...]]] = [{}]
    for key in _PIN_KEYS:
        for value in _PIN_VALUES:
            maps.append({key: value})
    maps.append({"acme": ("C1",), "": ("C2",)})
    maps.append({"acme": ("C1",), "Acme Display": ("C2",)})
    maps.append({"acme": (), "other": ("C1",)})
    return maps


POLICIES = [TenantAccessPolicy(agent_channel_pins=pins) for pins in _pin_maps()]
NAME_SETS: list[tuple[str | None, ...]] = [
    (),
    (None,),
    ("",),
    ("acme",),
    ("acme", "acme"),
    ("acme", None),
    ("acme", ""),
    ("acme-config", "Acme Display", "acme-config"),
    ("acme-config", "", "acme-config"),
    ("Acme Display", None),
    ("other",),
    ("other", "acme"),
    ("", None, ""),
]
CHANNELS: list[tuple[str | None, str | None]] = [
    (None, None),
    ("C1", "C1"),
    ("C2", "C2"),
    ("C3", "C3"),
    ("T1", "C1"),
    ("T1", "C3"),
    ("C1", None),
    ("C3", None),
]
ORIGINS: list[tuple[str | None, str | None]] = [
    (None, None),
    ("C1", None),
    ("C1", "T1"),
    ("C3", "T1"),
    ("C1", "dm:abc"),
    ("C3", "dm:abc"),
]

# --- the old decisions, reconstructed with their predicates unchanged -------------------------


def _old_origin_pin_location(
    *, parent_channel_id: str | None, thread_id: str | None
) -> tuple[str | None, str | None]:
    # access_policy.origin_pin_location
    if thread_id is not None and thread_id.startswith("dm:"):
        return None, None
    return thread_id or parent_channel_id, parent_channel_id


def _old_pin_write_refused(
    policy: TenantAccessPolicy,
    *,
    is_admin: bool,
    agent_names: tuple[str | None, ...] | None,
    parent_channel_id: str | None,
    thread_id: str | None,
) -> bool:
    # agent_pins.pin_write_refused
    if is_admin or not policy.agent_channel_pins:
        return False
    if agent_names is None:
        return True
    channel_id, parent = _old_origin_pin_location(
        parent_channel_id=parent_channel_id, thread_id=thread_id
    )
    return is_outside_agent_pin(
        policy, agent_names=agent_names, channel_id=channel_id, parent_channel_id=parent
    )


def _old_routine_save_refused(
    policy: TenantAccessPolicy, *, agent_names: tuple[str | None, ...], channel_id: str | None
) -> bool:
    # tools/routines._check_agent_pin
    if not any(name in policy.agent_channel_pins for name in agent_names if name):
        return False
    return is_outside_agent_pin(policy, agent_names=agent_names, channel_id=channel_id)


def _old_admission_pin_refused(
    policy: TenantAccessPolicy,
    *,
    is_dm: bool,
    is_admin: bool,
    agent_names: tuple[str | None, ...],
    thread_id: str | None,
    channel_id: str,
) -> bool:
    # turn/admission.admit_impl
    return not (is_dm and is_admin) and is_outside_agent_pin(
        policy,
        agent_names=agent_names,
        channel_id=None if is_dm else (thread_id or channel_id),
        parent_channel_id=None if is_dm else channel_id,
    )


def _old_mcp_pin_refused(
    policy: TenantAccessPolicy,
    *,
    pin_exempt: bool,
    platform_user_id: str | None,
    agent_names: tuple[str | None, ...],
) -> bool:
    # tools/_ctx._admit
    return (
        not (pin_exempt and platform_user_id is not None)
        and bool(policy.agent_channel_pins)
        and is_outside_agent_pin(policy, agent_names=agent_names, channel_id=None)
    )


def _old_send_refused(
    policy: TenantAccessPolicy,
    *,
    executing: bool,
    agent_names: tuple[str | None, ...] | None,
    channel_id: str,
    parent_channel_id: str | None,
    own_dm: bool,
) -> bool:
    # tools/_channel_policy._require_send_inside_pin + _executing_agent_names
    # (agent_names None is an agent that could not be resolved: refused)
    if not policy.agent_channel_pins:
        return False
    if own_dm:
        return False
    if not executing:
        return False
    if agent_names is None:
        return True
    if not any(name is not None and name in policy.agent_channel_pins for name in agent_names):
        return False
    return is_outside_agent_pin(
        policy, agent_names=agent_names, channel_id=channel_id, parent_channel_id=parent_channel_id
    )


def _old_dm_refused(
    policy: TenantAccessPolicy,
    *,
    executing: bool,
    agent_names: tuple[str | None, ...] | None,
    recipient_id: str,
    requester_id: str | None,
) -> bool:
    # tools/_channel_policy.require_dm_recipient_allowed
    if not executing:
        return False
    if not policy.agent_channel_pins:
        return False
    if agent_names is None:
        return True
    if not any(name is not None and name in policy.agent_channel_pins for name in agent_names):
        return False
    return recipient_id != requester_id


def _old_fork_pinned(policy: TenantAccessPolicy, *, names: tuple[str | None, ...]) -> bool:
    # tools/agents._fork_agent_impl and cli commands/agents (after the admin check)
    return any(name in policy.agent_channel_pins for name in names if name is not None)


def _old_channel_allows(
    policy: TenantAccessPolicy,
    origin: frozenset[str],
    channel_id: str,
    parent_channel_id: str | None,
) -> bool:
    # tools/_channel_policy.ChannelReadPolicy.allows
    sealed = policy.sealed_channel_ids
    if channel_id in sealed:
        return channel_id in origin
    if parent_channel_id is not None and parent_channel_id in sealed:
        return parent_channel_id in origin
    return True


def _old_seal_allows(
    policy: TenantAccessPolicy,
    origin: frozenset[str],
    *,
    channel: str | None,
    thread: str | None,
    seal_ids: frozenset[str],
    legacy_thread_id: str | None,
) -> bool:
    # tools/_session_access._seal_allows
    if channel is not None:
        if not seal_ids <= origin:
            return False
        if thread is None:
            return _old_channel_allows(policy, origin, channel, None)
        return _old_channel_allows(policy, origin, thread, channel) and _old_channel_allows(
            policy, origin, f"{channel}:{thread}", channel
        )
    if legacy_thread_id is None or not policy.sealed_channel_ids:
        return True
    return legacy_thread_id in origin


# --- the comparisons ------------------------------------------------------------


def test_configuration_writes_match() -> None:
    mismatches = []
    for policy, names, (parent, thread), is_admin in itertools.product(
        POLICIES, [*NAME_SETS, None], ORIGINS, (False, True)
    ):
        old = _old_pin_write_refused(
            policy,
            is_admin=is_admin,
            agent_names=names,
            parent_channel_id=parent,
            thread_id=thread,
        )
        new = not authorize(
            policy,
            subject=Subject(is_admin=is_admin),
            action=Action.CONFIGURE,
            surface=Surface.CONFIG,
            agent=AgentRef.unresolved() if names is None else AgentRef.of(*names),
            place=Place.from_origin(parent_channel_id=parent, thread_id=thread),
        )
        if old != new:
            mismatches.append((policy.agent_channel_pins, names, parent, thread, is_admin))
    assert mismatches == []


@pytest.mark.parametrize("surface", [Surface.ROUTINE])
def test_routine_saves_match(surface: Surface) -> None:
    mismatches = []
    for policy, names, channel in itertools.product(POLICIES, NAME_SETS, [None, "C1", "C2", "C3"]):
        old = _old_routine_save_refused(policy, agent_names=names, channel_id=channel)
        new = not authorize(
            policy,
            subject=Subject(),
            action=Action.SAVE_ROUTINE,
            surface=surface,
            agent=AgentRef.of(*names),
            place=Place(channel_id=channel),
        )
        if old != new:
            mismatches.append((policy.agent_channel_pins, names, channel))
    assert mismatches == []


def test_routine_fires_and_handoffs_match() -> None:
    # scheduler fire checks and task_continuity handoff: is_outside_agent_pin on
    # every name, no precondition, no admin exemption.
    mismatches = []
    for policy, names, (parent, thread) in itertools.product(POLICIES, NAME_SETS, ORIGINS):
        channel, par = _old_origin_pin_location(parent_channel_id=parent, thread_id=thread)
        old = is_outside_agent_pin(
            policy, agent_names=names, channel_id=channel, parent_channel_id=par
        )
        for surface in (Surface.ROUTINE, Surface.HANDOFF):
            new = not authorize(
                policy,
                subject=Subject(),
                action=Action.RUN_AGENT,
                surface=surface,
                agent=AgentRef.of(*names),
                place=Place.from_origin(parent_channel_id=parent, thread_id=thread),
            )
            if old != new:
                mismatches.append((policy.agent_channel_pins, names, parent, thread, surface))
    assert mismatches == []


def test_turn_admission_pins_match() -> None:
    mismatches = []
    for policy, names, (thread, channel), is_dm, is_admin in itertools.product(
        POLICIES,
        NAME_SETS,
        [(None, "C1"), ("T1", "C1"), (None, "C3"), ("T1", "C3")],
        (False, True),
        (False, True),
    ):
        old = _old_admission_pin_refused(
            policy,
            is_dm=is_dm,
            is_admin=is_admin,
            agent_names=names,
            thread_id=thread,
            channel_id=channel,
        )
        new = not authorize(
            policy,
            subject=Subject(is_admin=is_admin, platform_user_id="U1"),
            action=Action.RUN_AGENT,
            surface=Surface.DM if is_dm else Surface.CHANNEL,
            agent=AgentRef.of(*names),
            place=(
                Place() if is_dm else Place(channel_id=thread or channel, parent_channel_id=channel)
            ),
        )
        if old != new:
            mismatches.append((policy.agent_channel_pins, names, thread, channel, is_dm, is_admin))
    assert mismatches == []


def test_mcp_and_hub_turn_pins_match() -> None:
    mismatches = []
    for policy, names, pin_exempt, user in itertools.product(
        POLICIES, NAME_SETS, (False, True), ("U1", None)
    ):
        old = _old_mcp_pin_refused(
            policy, pin_exempt=pin_exempt, platform_user_id=user, agent_names=names
        )
        # _ctx._admit: the hub admin skips the check; otherwise pins must exist.
        hub_admin = pin_exempt and user is not None
        new = (
            bool(policy.agent_channel_pins)
            and not hub_admin
            and not authorize(
                policy,
                subject=Subject(is_admin=pin_exempt, platform_user_id=user),
                action=Action.RUN_AGENT,
                surface=Surface.HUB if pin_exempt else Surface.AGENT_CHAT,
                agent=AgentRef.of(*names),
            )
        )
        if old != new:
            mismatches.append((policy.agent_channel_pins, names, pin_exempt, user))
    assert mismatches == []


def test_pinned_sends_and_direct_messages_match() -> None:
    mismatches = []
    for policy, names, (channel, parent), executing in itertools.product(
        POLICIES, [*NAME_SETS, None], [c for c in CHANNELS if c[0] is not None], (False, True)
    ):
        assert channel is not None
        for own_dm in (False, True):
            old = _old_send_refused(
                policy,
                executing=executing,
                agent_names=names,
                channel_id=channel,
                parent_channel_id=parent,
                own_dm=own_dm,
            )
            if not executing:
                agent = AgentRef.none()
            elif names is None:
                agent = AgentRef.unresolved()
            else:
                agent = AgentRef.of(*names)
            new = not authorize(
                policy,
                subject=Subject(platform_user_id="U1"),
                action=Action.POST,
                agent=agent,
                place=Place(channel_id=channel, parent_channel_id=parent, own_dm=own_dm),
            )
            if old != new:
                mismatches.append(("post", policy.agent_channel_pins, names, channel, own_dm))
        for recipient in ("U1", "U2"):
            old = _old_dm_refused(
                policy,
                executing=executing,
                agent_names=names,
                recipient_id=recipient,
                requester_id="U1",
            )
            if not executing:
                agent = AgentRef.none()
            elif names is None:
                agent = AgentRef.unresolved()
            else:
                agent = AgentRef.of(*names)
            new = not authorize(
                policy,
                subject=Subject(platform_user_id="U1"),
                action=Action.DIRECT_MESSAGE,
                agent=agent,
                recipient_id=recipient,
            )
            if old != new:
                mismatches.append(("dm", policy.agent_channel_pins, names, recipient))
    assert mismatches == []


def test_fork_pin_refusal_matches() -> None:
    mismatches = []
    for policy, names in itertools.product(POLICIES, NAME_SETS):
        old = _old_fork_pinned(policy, names=names)
        new = (
            authorize(
                policy,
                subject=Subject(is_admin=True),
                action=Action.FORK,
                agent=AgentRef.of(*names),
            ).reason
            == "agent_pinned"
        )
        if old != new:
            mismatches.append((policy.agent_channel_pins, names))
    assert mismatches == []


_SEAL_POLICIES = [
    TenantAccessPolicy(),
    TenantAccessPolicy(sealed_channel_ids=("C1",)),
    TenantAccessPolicy(sealed_channel_ids=("T1",)),
    TenantAccessPolicy(sealed_channel_ids=("C1:T1",)),
    TenantAccessPolicy(sealed_channel_ids=("C1", "T1", "C3")),
]
_ORIGIN_SETS = [
    frozenset(),
    frozenset({"C1"}),
    frozenset({"C1", "T1"}),
    frozenset({"C1", "T1", "C1:T1"}),
    frozenset({"C3"}),
]


def test_channel_reads_match() -> None:
    mismatches = []
    for policy, origin, (channel, parent) in itertools.product(
        _SEAL_POLICIES, _ORIGIN_SETS, [c for c in CHANNELS if c[0] is not None]
    ):
        assert channel is not None
        old = _old_channel_allows(policy, origin, channel, parent)
        new = readable_from(policy, origin, channel, parent) and bool(
            authorize(
                policy,
                subject=Subject(),
                action=Action.READ_CHANNEL,
                place=Place(channel_id=channel, parent_channel_id=parent),
                origin_channel_ids=origin,
            )
        )
        if old != new:
            mismatches.append((policy.sealed_channel_ids, origin, channel, parent))
    assert mismatches == []


def test_session_seal_reads_match() -> None:
    stamps: list[tuple[str | None, str | None, frozenset[str], str | None]] = [
        (None, None, frozenset(), None),
        (None, None, frozenset(), "T1"),
        ("C1", None, frozenset(), None),
        ("C1", "T1", frozenset(), None),
        ("C1", None, frozenset({"C1"}), None),
        ("C1", "T1", frozenset({"T1"}), None),
        ("C1", "T1", frozenset({"C1", "T1"}), None),
        ("C1", "T1", frozenset({"C1:T1"}), None),
    ]
    mismatches = []
    for policy, origin, (channel, thread, seals, legacy) in itertools.product(
        _SEAL_POLICIES, _ORIGIN_SETS, stamps
    ):
        old = _old_seal_allows(
            policy,
            origin,
            channel=channel,
            thread=thread,
            seal_ids=seals,
            legacy_thread_id=legacy,
        )
        new = bool(
            authorize(
                policy,
                subject=Subject(),
                action=Action.READ_SESSION,
                origin_channel_ids=origin,
                session=SessionFacts(
                    channel=channel,
                    thread=thread if channel is not None else None,
                    seal_ids=seals,
                    legacy_thread_id=legacy,
                ),
            )
        )
        if old != new:
            mismatches.append((policy.sealed_channel_ids, origin, channel, thread, seals, legacy))
    assert mismatches == []


def test_turn_caller_gates_match() -> None:
    policies = [
        TenantAccessPolicy(),
        TenantAccessPolicy(protected_channel_ids=("C1",)),
        TenantAccessPolicy(protected_category_ids=("K1",)),
        TenantAccessPolicy(invoker_user_ids=("U_OK",)),
        TenantAccessPolicy(protected_channel_ids=("C1",), invoker_user_ids=("U_OK",)),
    ]
    mismatches = []
    for policy, (thread, channel), (category, unresolved), user, is_admin in itertools.product(
        policies,
        [(None, "C1"), ("T1", "C1"), (None, "C2"), ("T1", "C2")],
        [(None, False), ("K1", False), (None, True)],
        ("U_OK", "U_NO"),
        (False, True),
    ):
        # turn/admission.admit_impl: protection first, then the invoker allowlist.
        if is_write_protected(
            policy,
            channel_id=thread or channel,
            parent_channel_id=channel,
            category_id=category,
            category_unresolved=unresolved,
        ):
            old: str | None = "channel_protected"
        elif not is_invoker_allowed(policy, external_user_id=user, is_admin=is_admin):
            old = "invoker_not_allowed"
        else:
            old = None
        new = authorize(
            policy,
            subject=Subject(is_admin=is_admin, platform_user_id=user),
            action=Action.START_TURN,
            place=Place(
                channel_id=thread or channel,
                parent_channel_id=channel,
                category_id=category,
                category_unresolved=unresolved,
            ),
        ).reason
        if old != new:
            mismatches.append((policy, thread, channel, category, unresolved, user, is_admin))
    assert mismatches == []
