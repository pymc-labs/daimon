"""The `authorize` decision table, one row per rule of the formal model.

Each row names the guard it pins from `formal/access_control/AccessControl.tla`
(`G_*`), so a rule that drifts from the model fails here by name. Rows record
the rules as they stand on main. Action-time re-checks are covered where they
happen (`tests/turn/test_reauthorize.py`, the OAuth callback and publish tests).
"""

from __future__ import annotations

import pytest
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import (
    Action,
    AgentRef,
    Decision,
    Place,
    SessionFacts,
    Subject,
    Surface,
    authorize,
)

PINNED = TenantAccessPolicy(agent_channel_pins={"acme": ("C_ACME",)})
NOWHERE = TenantAccessPolicy(agent_channel_pins={"acme": ()})
DISPLAY_PINNED = TenantAccessPolicy(agent_channel_pins={"Acme Display": ("C_ACME",)})
SEALED = TenantAccessPolicy(sealed_channel_ids=("C_SEAL",))
PROTECTED = TenantAccessPolicy(protected_channel_ids=("C_PROT",))
INVOKERS = TenantAccessPolicy(invoker_user_ids=("U_OK",))

MEMBER = Subject(is_admin=False, platform_user_id="U_MEM")
ADMIN = Subject(is_admin=True, platform_user_id="U_ADM")
BEARER = Subject(is_admin=True, platform_user_id=None)
AGENT_KEY_ADMIN = Subject(is_admin=True, platform_user_id="U_ADM", via_agent_key=True)
READER = AgentRef.of("acme-reader", "acme-reader", "acme")
ACME = AgentRef.of("acme", "acme")
ACME_DISPLAY = AgentRef.of("acme-config", "Acme Display", "acme-config")
INSIDE = Place(channel_id="C_ACME", parent_channel_id="C_ACME")
THREAD_INSIDE = Place(channel_id="T1", parent_channel_id="C_ACME")
OUTSIDE = Place(channel_id="C_OTHER", parent_channel_id="C_OTHER")


def _deny(reason: str) -> Decision:
    return Decision(False, reason)  # pyright: ignore[reportArgumentType]


ALLOW = Decision(True)

ROWS: list[tuple[str, TenantAccessPolicy, dict[str, object], Decision]] = [
    # --- G_pin_turn: a pinned agent runs only in its channels ---
    (
        "G_pin_turn member outside",
        PINNED,
        {"subject": MEMBER, "action": Action.RUN_AGENT, "agent": ACME, "place": OUTSIDE},
        _deny("agent_pinned_elsewhere"),
    ),
    (
        "G_pin_turn member inside",
        PINNED,
        {"subject": MEMBER, "action": Action.RUN_AGENT, "agent": ACME, "place": INSIDE},
        ALLOW,
    ),
    (
        "G_pin_turn thread under the pinned channel",
        PINNED,
        {"subject": MEMBER, "action": Action.RUN_AGENT, "agent": ACME, "place": THREAD_INSIDE},
        ALLOW,
    ),
    (
        "G_pin_turn admin in a channel is still held",
        PINNED,
        {"subject": ADMIN, "action": Action.RUN_AGENT, "agent": ACME, "place": OUTSIDE},
        _deny("agent_pinned_elsewhere"),
    ),
    (
        "G_pin_turn empty pin list fails closed",
        NOWHERE,
        {"subject": MEMBER, "action": Action.RUN_AGENT, "agent": ACME, "place": INSIDE},
        _deny("agent_pinned_elsewhere"),
    ),
    (
        "G_pin_turn unpinned agent runs anywhere",
        PINNED,
        {
            "subject": MEMBER,
            "action": Action.RUN_AGENT,
            "agent": AgentRef.of("other"),
            "place": OUTSIDE,
        },
        ALLOW,
    ),
    (
        "G_pin_offchannel member DM is outside every pin",
        PINNED,
        {"subject": MEMBER, "action": Action.RUN_AGENT, "surface": Surface.DM, "agent": ACME},
        _deny("agent_pinned_elsewhere"),
    ),
    # --- admin trust model: admins exempt where only they see the output ---
    (
        "admin trust model: admin DM is pin-exempt",
        PINNED,
        {"subject": ADMIN, "action": Action.RUN_AGENT, "surface": Surface.DM, "agent": ACME},
        ALLOW,
    ),
    (
        "admin trust model: admin hub turn is pin-exempt",
        PINNED,
        {"subject": ADMIN, "action": Action.RUN_AGENT, "surface": Surface.HUB, "agent": ACME},
        ALLOW,
    ),
    (
        "admin trust model: member hub turn is held",
        PINNED,
        {"subject": MEMBER, "action": Action.RUN_AGENT, "surface": Surface.HUB, "agent": ACME},
        _deny("agent_pinned_elsewhere"),
    ),
    # --- G_operator_bypass_closed: no-platform bearers are never exempt ---
    (
        "G_operator_bypass_closed bearer agent chat",
        PINNED,
        {
            "subject": BEARER,
            "action": Action.RUN_AGENT,
            "surface": Surface.AGENT_CHAT,
            "agent": ACME,
        },
        _deny("agent_pinned_elsewhere"),
    ),
    (
        "G_operator_bypass_closed bearer on the hub surface",
        PINNED,
        {"subject": BEARER, "action": Action.RUN_AGENT, "surface": Surface.HUB, "agent": ACME},
        _deny("agent_pinned_elsewhere"),
    ),
    # --- G_pin_routine(_names) / G_pin_handoff(_names): every name, no admin ---
    (
        "G_pin_routine_names pin on the display name",
        DISPLAY_PINNED,
        {
            "subject": ADMIN,
            "action": Action.RUN_AGENT,
            "surface": Surface.ROUTINE,
            "agent": ACME_DISPLAY,
            "place": Place(),
        },
        _deny("agent_pinned_elsewhere"),
    ),
    (
        "G_pin_routine into the pinned channel",
        DISPLAY_PINNED,
        {
            "subject": MEMBER,
            "action": Action.RUN_AGENT,
            "surface": Surface.ROUTINE,
            "agent": ACME_DISPLAY,
            "place": Place(channel_id="C_ACME"),
        },
        ALLOW,
    ),
    (
        "G_pin_handoff_names from a DM origin",
        DISPLAY_PINNED,
        {
            "subject": ADMIN,
            "action": Action.RUN_AGENT,
            "surface": Surface.HANDOFF,
            "agent": ACME_DISPLAY,
            "place": Place.from_origin(parent_channel_id="C_ACME", thread_id="dm:abc"),
        },
        _deny("agent_pinned_elsewhere"),
    ),
    # --- G_pin_write / G_pin_write_submit: configuring a pinned agent ---
    (
        "G_pin_write member outside",
        PINNED,
        {"subject": MEMBER, "action": Action.CONFIGURE, "agent": ACME, "place": OUTSIDE},
        _deny("agent_pinned_elsewhere"),
    ),
    (
        "G_pin_write member inside",
        PINNED,
        {"subject": MEMBER, "action": Action.CONFIGURE, "agent": ACME, "place": INSIDE},
        ALLOW,
    ),
    (
        "G_pin_write admin anywhere",
        PINNED,
        {"subject": ADMIN, "action": Action.CONFIGURE, "agent": ACME, "place": OUTSIDE},
        ALLOW,
    ),
    (
        "G_pin_write_submit vanished target fails closed",
        PINNED,
        {"subject": MEMBER, "action": Action.CONFIGURE, "agent": AgentRef.unresolved()},
        _deny("agent_unresolved"),
    ),
    (
        "G_pin_write unpinned tenant",
        TenantAccessPolicy(),
        {"subject": MEMBER, "action": Action.CONFIGURE, "agent": AgentRef.unresolved()},
        ALLOW,
    ),
    # --- pinned sends: a pinned agent posts only inside its pin ---
    (
        "pinned send outside",
        PINNED,
        {"subject": ADMIN, "action": Action.POST, "agent": ACME, "place": OUTSIDE},
        _deny("agent_pinned_elsewhere"),
    ),
    (
        "pinned send into the requester's own DM",
        PINNED,
        {
            "subject": ADMIN,
            "action": Action.POST,
            "agent": ACME,
            "place": Place(channel_id="D123", own_dm=True),
        },
        ALLOW,
    ),
    (
        "pinned send by the operator (no executing agent)",
        PINNED,
        {"subject": BEARER, "action": Action.POST, "place": OUTSIDE},
        ALLOW,
    ),
    (
        "pinned send by an unresolved agent fails closed",
        PINNED,
        {
            "subject": MEMBER,
            "action": Action.POST,
            "agent": AgentRef.unresolved(),
            "place": OUTSIDE,
        },
        _deny("agent_unresolved"),
    ),
    (
        "protection holds for everyone",
        PROTECTED,
        {"subject": ADMIN, "action": Action.POST, "place": Place(channel_id="C_PROT")},
        _deny("channel_protected"),
    ),
    (
        "pinned DM to someone else",
        PINNED,
        {
            "subject": ADMIN,
            "action": Action.DIRECT_MESSAGE,
            "agent": ACME,
            "recipient_id": "U_OTHER",
        },
        _deny("dm_recipient_not_requester"),
    ),
    (
        "pinned DM to the requester",
        PINNED,
        {"subject": ADMIN, "action": Action.DIRECT_MESSAGE, "agent": ACME, "recipient_id": "U_ADM"},
        ALLOW,
    ),
    # --- G_fork_admin / G_pin_fork ---
    (
        "G_fork_admin member",
        TenantAccessPolicy(),
        {"subject": MEMBER, "action": Action.FORK, "agent": AgentRef.of("other")},
        _deny("admin_required"),
    ),
    (
        "G_pin_fork admin forking a pinned agent",
        DISPLAY_PINNED,
        {"subject": ADMIN, "action": Action.FORK, "agent": ACME_DISPLAY},
        _deny("agent_pinned"),
    ),
    (
        "G_pin_fork admin forking an unpinned agent",
        DISPLAY_PINNED,
        {"subject": ADMIN, "action": Action.FORK, "agent": AgentRef.of("other")},
        ALLOW,
    ),
    # --- caller gates on a platform turn ---
    (
        "protected reply target refuses the turn",
        PROTECTED,
        {"subject": ADMIN, "action": Action.START_TURN, "place": Place(channel_id="C_PROT")},
        _deny("channel_protected"),
    ),
    (
        "invoker allowlist refuses a member",
        INVOKERS,
        {"subject": MEMBER, "action": Action.START_TURN},
        _deny("invoker_not_allowed"),
    ),
    (
        "invoker allowlist exempts an admin",
        INVOKERS,
        {"subject": ADMIN, "action": Action.START_TURN},
        ALLOW,
    ),
    # --- G_seal_session_read and channel reads ---
    (
        "sealed channel read from outside",
        SEALED,
        {"subject": MEMBER, "action": Action.READ_CHANNEL, "place": Place(channel_id="C_SEAL")},
        _deny("sealed"),
    ),
    (
        "sealed channel read from inside",
        SEALED,
        {
            "subject": MEMBER,
            "action": Action.READ_CHANNEL,
            "place": Place(channel_id="T9", parent_channel_id="C_SEAL"),
            "origin_channel_ids": frozenset({"C_SEAL", "T9"}),
        },
        ALLOW,
    ),
    (
        "G_seal_session_read a session stays under every seal after an unseal",
        TenantAccessPolicy(),
        {
            "subject": MEMBER,
            "action": Action.READ_SESSION,
            "session": SessionFacts(channel="C_SEAL", seal_ids=frozenset({"C_SEAL"})),
        },
        _deny("sealed"),
    ),
    (
        "G_seal_session_read inside the thread that sealed it",
        TenantAccessPolicy(),
        {
            "subject": MEMBER,
            "action": Action.READ_SESSION,
            "session": SessionFacts(channel="C1", thread="T1", seal_ids=frozenset({"T1"})),
            "origin_channel_ids": frozenset({"C1", "T1"}),
        },
        ALLOW,
    ),
    (
        "G_seal_session_read a legacy session only inside its thread",
        SEALED,
        {
            "subject": MEMBER,
            "action": Action.READ_SESSION,
            "session": SessionFacts(legacy_thread_id="T7"),
            "origin_channel_ids": frozenset({"C9"}),
        },
        _deny("sealed"),
    ),
    (
        "G_seal_session_read a headless session",
        SEALED,
        {"subject": MEMBER, "action": Action.READ_SESSION, "session": SessionFacts()},
        ALLOW,
    ),
    # --- Agent keys are never exempt as admins, whoever minted them ---
    (
        "G_selfedit_gate admin's agent key configuring a pinned agent",
        PINNED,
        {
            "subject": AGENT_KEY_ADMIN,
            "action": Action.CONFIGURE,
            "surface": Surface.CONFIG,
            "agent": ACME,
        },
        _deny("agent_pinned_elsewhere"),
    ),
    (
        "admin_trust admin's agent key on the hub is held to the pin",
        PINNED,
        {
            "subject": AGENT_KEY_ADMIN,
            "action": Action.RUN_AGENT,
            "surface": Surface.HUB,
            "agent": ACME,
        },
        _deny("agent_pinned_elsewhere"),
    ),
    (
        "G_fork_admin an admin's agent key cannot fork",
        TenantAccessPolicy(),
        {"subject": AGENT_KEY_ADMIN, "action": Action.FORK, "agent": ACME},
        _deny("admin_required"),
    ),
    # --- G_pin_report_reader: a reader variant carries its source's names ---
    (
        "G_pin_report_reader a pinned agent's reader off-channel",
        PINNED,
        {
            "subject": MEMBER,
            "action": Action.RUN_AGENT,
            "surface": Surface.AGENT_CHAT,
            "agent": READER,
        },
        _deny("agent_pinned_elsewhere"),
    ),
]


@pytest.mark.parametrize(
    ("policy", "kwargs", "expected"),
    [pytest.param(policy, kwargs, expected, id=name) for name, policy, kwargs, expected in ROWS],
)
def test_authorize_decision_table(
    policy: TenantAccessPolicy, kwargs: dict[str, object], expected: Decision
) -> None:
    assert authorize(policy, **kwargs) == expected  # pyright: ignore[reportArgumentType]


def test_place_from_origin_maps_a_dm_scope_to_no_channel() -> None:
    assert Place.from_origin(parent_channel_id="C1", thread_id="dm:x") == Place()
    assert Place.from_origin(parent_channel_id="C1", thread_id="T1") == Place(
        channel_id="T1", parent_channel_id="C1"
    )
    assert Place.from_origin(parent_channel_id="C1", thread_id=None) == Place(
        channel_id="C1", parent_channel_id="C1"
    )
