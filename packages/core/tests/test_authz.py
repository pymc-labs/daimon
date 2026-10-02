"""The `authorize` decision table, one row per rule of the formal model.

Each row names the guard it pins from `formal/access_control/AccessControl.tla`
(`G_*`), so a rule that drifts from the model fails here by name. Rows record
the rules as they stand on main. Action-time re-checks are covered where they
happen (`tests/turn/test_reauthorize.py`, the OAuth callback and publish tests).
The channel admin, channel default and channel environment rows have no guard
in the model yet.
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
    build_subject,
)

PINNED = TenantAccessPolicy(agent_channel_pins={"acme": ("C_ACME",)})
NOWHERE = TenantAccessPolicy(agent_channel_pins={"acme": ()})
DISPLAY_PINNED = TenantAccessPolicy(agent_channel_pins={"Acme Display": ("C_ACME",)})
SEALED = TenantAccessPolicy(sealed_channel_ids=("C_SEAL",))
PROTECTED = TenantAccessPolicy(protected_channel_ids=("C_PROT",))
INVOKERS = TenantAccessPolicy(invoker_user_ids=("U_OK",))
OPEN = TenantAccessPolicy()

MEMBER = Subject(is_admin=False, platform_user_id="U_MEM")
ADMIN = Subject(is_admin=True, platform_user_id="U_ADM")
BEARER = Subject(is_admin=True, platform_user_id=None)
ACME_CHANNEL_ADMIN = Subject(
    platform_user_id="U_CA", administered_channel_ids=frozenset({"C_ACME"})
)
TWO_CHANNELS = TenantAccessPolicy(agent_channel_pins={"acme": ("C_ACME", "C_OPS")})
AGENT_KEY_ADMIN = Subject(is_admin=True, platform_user_id="U_ADM", via_agent_key=True)
READER = AgentRef.of("acme-reader", "acme-reader", "acme")
AGENT_KEY_CHANNEL_ADMIN = Subject(
    platform_user_id="U_CA", via_agent_key=True, administered_channel_ids=frozenset({"C_ACME"})
)
ACME_SEALED = TenantAccessPolicy(sealed_channel_ids=("C_ACME",))
ACME = AgentRef.of("acme", "acme")
ACME_DISPLAY = AgentRef.of("acme-config", "Acme Display", "acme-config")
INSIDE = Place(channel_id="C_ACME", parent_channel_id="C_ACME")
THREAD_INSIDE = Place(channel_id="T1", parent_channel_id="C_ACME")
OUTSIDE = Place(channel_id="C_OTHER", parent_channel_id="C_OTHER")
ISOLATED = TenantAccessPolicy(
    sealed_channel_ids=("C_ACME",),
    isolated_channel_ids=("C_ACME",),
    agent_channel_pins={"acme": ("C_ACME",)},
)
SHARED = AgentRef.of("shared", "shared")
SETUP_ORIGIN = Place(channel_id="T_SETUP", parent_channel_id="C_ACME", setup_thread=True)


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
    # --- channel admins: a server admin's rights, limited to their channels ---
    (
        "channel admin of every pinned channel configures from anywhere",
        PINNED,
        {
            "subject": ACME_CHANNEL_ADMIN,
            "action": Action.CONFIGURE,
            "agent": ACME,
            "place": OUTSIDE,
        },
        ALLOW,
    ),
    (
        "channel admin of one pinned channel of two",
        TWO_CHANNELS,
        {
            "subject": ACME_CHANNEL_ADMIN,
            "action": Action.CONFIGURE,
            "agent": ACME,
            "place": OUTSIDE,
        },
        _deny("agent_pinned_elsewhere"),
    ),
    (
        "channel admin needs every pinned name's channels",
        TenantAccessPolicy(
            agent_channel_pins={"acme-config": ("C_ACME",), "Acme Display": ("C_OPS",)}
        ),
        {
            "subject": ACME_CHANNEL_ADMIN,
            "action": Action.CONFIGURE,
            "agent": ACME_DISPLAY,
            "place": OUTSIDE,
        },
        _deny("agent_pinned_elsewhere"),
    ),
    (
        "channel admin and a pin to no channel fails closed",
        NOWHERE,
        {
            "subject": ACME_CHANNEL_ADMIN,
            "action": Action.CONFIGURE,
            "agent": ACME,
            "place": INSIDE,
        },
        _deny("agent_pinned_elsewhere"),
    ),
    (
        "channel admin and a vanished target fails closed",
        PINNED,
        {
            "subject": ACME_CHANNEL_ADMIN,
            "action": Action.CONFIGURE,
            "agent": AgentRef.unresolved(),
        },
        _deny("agent_unresolved"),
    ),
    (
        "channel admin gets no server admin turn exemption",
        PINNED,
        {
            "subject": ACME_CHANNEL_ADMIN,
            "action": Action.RUN_AGENT,
            "surface": Surface.HUB,
            "agent": ACME,
        },
        _deny("agent_pinned_elsewhere"),
    ),
    (
        "channel admin never forks",
        TenantAccessPolicy(),
        {"subject": ACME_CHANNEL_ADMIN, "action": Action.FORK, "agent": ACME},
        _deny("admin_required"),
    ),
    # --- coding-tool tokens: a channel admin mints only bound, for their own agent ---
    (
        "channel admin mints for their channel's own agent, bound there",
        PINNED,
        {
            "subject": ACME_CHANNEL_ADMIN,
            "action": Action.MINT_CODING_TOKEN,
            "agent": ACME,
            "place": INSIDE,
        },
        ALLOW,
    ),
    (
        "channel admin never mints an unbound token",
        PINNED,
        {"subject": ACME_CHANNEL_ADMIN, "action": Action.MINT_CODING_TOKEN, "agent": ACME},
        _deny("admin_required"),
    ),
    (
        "channel admin mints nothing bound to another channel",
        PINNED,
        {
            "subject": ACME_CHANNEL_ADMIN,
            "action": Action.MINT_CODING_TOKEN,
            "agent": ACME,
            "place": OUTSIDE,
        },
        _deny("admin_required"),
    ),
    (
        "channel admin mints nothing for an agent pinned elsewhere",
        TenantAccessPolicy(agent_channel_pins={"acme": ("C_OPS",)}),
        {
            "subject": ACME_CHANNEL_ADMIN,
            "action": Action.MINT_CODING_TOKEN,
            "agent": ACME,
            "place": INSIDE,
        },
        _deny("agent_pinned_elsewhere"),
    ),
    (
        "channel admin of one pinned channel of two mints nothing",
        TWO_CHANNELS,
        {
            "subject": ACME_CHANNEL_ADMIN,
            "action": Action.MINT_CODING_TOKEN,
            "agent": ACME,
            "place": INSIDE,
        },
        _deny("agent_pinned_elsewhere"),
    ),
    (
        "channel admin of two channels mints nothing outside the agent's pin",
        PINNED,
        {
            "subject": Subject(
                platform_user_id="U_CA", administered_channel_ids=frozenset({"C_ACME", "C_OTHER"})
            ),
            "action": Action.MINT_CODING_TOKEN,
            "agent": ACME,
            "place": OUTSIDE,
        },
        _deny("agent_pinned_elsewhere"),
    ),
    (
        "channel admin mints nothing for an unpinned agent, which may answer anywhere",
        ACME_SEALED,
        {
            "subject": ACME_CHANNEL_ADMIN,
            "action": Action.MINT_CODING_TOKEN,
            "agent": ACME,
            "place": INSIDE,
        },
        _deny("admin_required"),
    ),
    (
        "channel admin and a pin to no channel mints nothing",
        NOWHERE,
        {
            "subject": ACME_CHANNEL_ADMIN,
            "action": Action.MINT_CODING_TOKEN,
            "agent": ACME,
            "place": INSIDE,
        },
        _deny("agent_pinned_elsewhere"),
    ),
    (
        "channel admin and a vanished agent mints nothing",
        PINNED,
        {
            "subject": ACME_CHANNEL_ADMIN,
            "action": Action.MINT_CODING_TOKEN,
            "agent": AgentRef.unresolved(),
            "place": INSIDE,
        },
        _deny("agent_unresolved"),
    ),
    (
        "an agent key never mints, whatever its grants",
        PINNED,
        {
            "subject": AGENT_KEY_CHANNEL_ADMIN,
            "action": Action.MINT_CODING_TOKEN,
            "agent": ACME,
            "place": INSIDE,
        },
        _deny("admin_required"),
    ),
    (
        "member mints nothing, even inside the pin",
        PINNED,
        {"subject": MEMBER, "action": Action.MINT_CODING_TOKEN, "agent": ACME, "place": INSIDE},
        _deny("admin_required"),
    ),
    (
        "server admin mints unbound, as before",
        PINNED,
        {"subject": ADMIN, "action": Action.MINT_CODING_TOKEN, "agent": ACME},
        ALLOW,
    ),
    (
        "server admin mints bound anywhere, as before",
        ACME_SEALED,
        {"subject": ADMIN, "action": Action.MINT_CODING_TOKEN, "agent": ACME, "place": INSIDE},
        ALLOW,
    ),
    # --- channel default: nobody binds a pinned agent outside its pin ---
    (
        "bind default outside the pin, even an admin",
        PINNED,
        {
            "subject": ADMIN,
            "action": Action.BIND_CHANNEL_DEFAULT,
            "agent": ACME,
            "place": Place(channel_id="C_OTHER"),
        },
        _deny("agent_pinned_elsewhere"),
    ),
    (
        "bind default under a display-name pin",
        DISPLAY_PINNED,
        {
            "subject": ADMIN,
            "action": Action.BIND_CHANNEL_DEFAULT,
            "agent": ACME_DISPLAY,
            "place": Place(channel_id="C_OTHER"),
        },
        _deny("agent_pinned_elsewhere"),
    ),
    (
        "bind default of an agent pinned nowhere",
        NOWHERE,
        {
            "subject": ADMIN,
            "action": Action.BIND_CHANNEL_DEFAULT,
            "agent": ACME,
            "place": Place(channel_id="C_ACME"),
        },
        _deny("agent_pinned_elsewhere"),
    ),
    (
        "bind default inside the pin",
        PINNED,
        {
            "subject": MEMBER,
            "action": Action.BIND_CHANNEL_DEFAULT,
            "agent": ACME,
            "place": Place(channel_id="C_ACME"),
        },
        ALLOW,
    ),
    (
        "bind default of an unpinned agent",
        TenantAccessPolicy(),
        {
            "subject": MEMBER,
            "action": Action.BIND_CHANNEL_DEFAULT,
            "agent": ACME,
            "place": Place(channel_id="C_OTHER"),
        },
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
    # --- Session reads (G_seal_session_read, admin trust model) ---
    (
        "admin_trust admin reads another account's sealed channel session from the hub",
        SEALED,
        {
            "subject": ADMIN,
            "action": Action.READ_SESSION,
            "surface": Surface.HUB,
            "session": SessionFacts(channel="C_SEAL", seal_ids=frozenset({"C_SEAL"}), owned=False),
        },
        ALLOW,
    ),
    (
        "admin_trust admin never reads another member's private DM",
        SEALED,
        {
            "subject": ADMIN,
            "action": Action.READ_SESSION,
            "surface": Surface.HUB,
            "session": SessionFacts(channel="D123", owned=False, private=True),
        },
        _deny("not_owner"),
    ),
    (
        "admin_trust admin reads their OWN sealed DM-shaped session from the hub",
        SEALED,
        {
            "subject": ADMIN,
            "action": Action.READ_SESSION,
            "surface": Surface.HUB,
            "session": SessionFacts(
                channel="D123", owned=True, private=True, seal_ids=frozenset({"C_SEAL"})
            ),
        },
        ALLOW,
    ),
    (
        "admin_trust admin never continues a sealed channel session",
        SEALED,
        {
            "subject": ADMIN,
            "action": Action.CONTINUE_SESSION,
            "surface": Surface.HUB,
            "session": SessionFacts(channel="C_SEAL", seal_ids=frozenset({"C_SEAL"})),
        },
        _deny("sealed"),
    ),
    (
        "G_seal_session_read member never reads another account's session",
        TenantAccessPolicy(),
        {
            "subject": MEMBER,
            "action": Action.READ_SESSION,
            "surface": Surface.HUB,
            "session": SessionFacts(channel="C_OPEN", owned=False),
        },
        _deny("not_owner"),
    ),
    (
        "admin_trust an admin's agent key gets no hub read exemption",
        SEALED,
        {
            "subject": AGENT_KEY_ADMIN,
            "action": Action.READ_SESSION,
            "surface": Surface.HUB,
            "session": SessionFacts(channel="C_SEAL", seal_ids=frozenset({"C_SEAL"}), owned=False),
        },
        _deny("not_owner"),
    ),
    # --- Channel admins read their channels' sessions from the hub ---
    (
        "channel_admin reads another account's sealed session in their channel",
        ACME_SEALED,
        {
            "subject": ACME_CHANNEL_ADMIN,
            "action": Action.READ_SESSION,
            "surface": Surface.HUB,
            "session": SessionFacts(channel="C_ACME", seal_ids=frozenset({"C_ACME"}), owned=False),
        },
        ALLOW,
    ),
    (
        "channel_admin reads a thread sealed on its own under their channel",
        ACME_SEALED,
        {
            "subject": ACME_CHANNEL_ADMIN,
            "action": Action.READ_SESSION,
            "surface": Surface.HUB,
            "session": SessionFacts(
                channel="C_ACME", thread="T1", seal_ids=frozenset({"T1"}), owned=False
            ),
        },
        ALLOW,
    ),
    (
        "channel_admin reads a slack thread sealed on its own under their channel",
        ACME_SEALED,
        {
            "subject": ACME_CHANNEL_ADMIN,
            "action": Action.READ_SESSION,
            "surface": Surface.HUB,
            "session": SessionFacts(
                channel="C_ACME",
                thread="171.2",
                seal_ids=frozenset({"C_ACME:171.2"}),
                owned=False,
            ),
        },
        ALLOW,
    ),
    (
        "channel_admin reads mixed seal ids that all lie in their channel",
        ACME_SEALED,
        {
            "subject": ACME_CHANNEL_ADMIN,
            "action": Action.READ_SESSION,
            "surface": Surface.HUB,
            "session": SessionFacts(
                channel="C_ACME",
                thread="T1",
                seal_ids=frozenset({"C_ACME", "T1", "C_ACME:T1"}),
                owned=False,
            ),
        },
        ALLOW,
    ),
    (
        "channel_admin never reads a seal inherited from another channel",
        ACME_SEALED,
        {
            "subject": ACME_CHANNEL_ADMIN,
            "action": Action.READ_SESSION,
            "surface": Surface.HUB,
            "session": SessionFacts(channel="C_ACME", seal_ids=frozenset({"C_OTHER"}), owned=False),
        },
        _deny("not_owner"),
    ),
    (
        "channel_admin never reads their own session under another channel's seal",
        ACME_SEALED,
        {
            "subject": ACME_CHANNEL_ADMIN,
            "action": Action.READ_SESSION,
            "surface": Surface.HUB,
            "session": SessionFacts(channel="C_ACME", seal_ids=frozenset({"C_OTHER"})),
        },
        _deny("sealed"),
    ),
    (
        "channel_admin never reads mixed seal ids with one outside their channels",
        ACME_SEALED,
        {
            "subject": ACME_CHANNEL_ADMIN,
            "action": Action.READ_SESSION,
            "surface": Surface.HUB,
            "session": SessionFacts(
                channel="C_ACME",
                thread="T1",
                seal_ids=frozenset({"C_ACME", "T1", "C_OTHER"}),
                owned=False,
            ),
        },
        _deny("not_owner"),
    ),
    (
        "channel_admin never reads a thread seal whose parent is unknown",
        ACME_SEALED,
        {
            "subject": ACME_CHANNEL_ADMIN,
            "action": Action.READ_SESSION,
            "surface": Surface.HUB,
            "session": SessionFacts(
                channel="C_ACME", thread="T1", seal_ids=frozenset({"T9"}), owned=False
            ),
        },
        _deny("not_owner"),
    ),
    (
        "channel_admin never reads a session that ran in another channel",
        ACME_SEALED,
        {
            "subject": ACME_CHANNEL_ADMIN,
            "action": Action.READ_SESSION,
            "surface": Surface.HUB,
            "session": SessionFacts(channel="C_OTHER", seal_ids=frozenset({"C_ACME"}), owned=False),
        },
        _deny("not_owner"),
    ),
    (
        "channel_admin never reads a private DM",
        ACME_SEALED,
        {
            "subject": ACME_CHANNEL_ADMIN,
            "action": Action.READ_SESSION,
            "surface": Surface.HUB,
            "session": SessionFacts(channel="C_ACME", owned=False, private=True),
        },
        _deny("not_owner"),
    ),
    (
        "channel_admin never reads an unstamped legacy session",
        ACME_SEALED,
        {
            "subject": ACME_CHANNEL_ADMIN,
            "action": Action.READ_SESSION,
            "surface": Surface.HUB,
            "session": SessionFacts(legacy_thread_id="T1", owned=False),
        },
        _deny("not_owner"),
    ),
    (
        "channel_admin never continues a sealed session",
        ACME_SEALED,
        {
            "subject": ACME_CHANNEL_ADMIN,
            "action": Action.CONTINUE_SESSION,
            "surface": Surface.HUB,
            "session": SessionFacts(channel="C_ACME", seal_ids=frozenset({"C_ACME"})),
        },
        _deny("sealed"),
    ),
    (
        "channel_admin read is the hub's only",
        ACME_SEALED,
        {
            "subject": ACME_CHANNEL_ADMIN,
            "action": Action.READ_SESSION,
            "surface": Surface.AGENT_CHAT,
            "session": SessionFacts(channel="C_ACME", seal_ids=frozenset({"C_ACME"}), owned=False),
        },
        _deny("not_owner"),
    ),
    (
        "channel_admin grants on an agent key read nothing",
        ACME_SEALED,
        {
            "subject": AGENT_KEY_CHANNEL_ADMIN,
            "action": Action.READ_SESSION,
            "surface": Surface.HUB,
            "session": SessionFacts(channel="C_ACME", seal_ids=frozenset({"C_ACME"}), owned=False),
        },
        _deny("not_owner"),
    ),
    (
        "channel_admin grants on an agent key configure no pinned agent",
        PINNED,
        {
            "subject": AGENT_KEY_CHANNEL_ADMIN,
            "action": Action.CONFIGURE,
            "surface": Surface.CONFIG,
            "agent": ACME,
        },
        _deny("agent_pinned_elsewhere"),
    ),
    # --- Isolation: an isolated channel's own agents stay in, others stay out ---
    (
        "isolation shared agent inside",
        ISOLATED,
        {"subject": MEMBER, "action": Action.RUN_AGENT, "agent": SHARED, "place": THREAD_INSIDE},
        _deny("channel_isolated"),
    ),
    (
        "isolation own agent inside",
        ISOLATED,
        {"subject": MEMBER, "action": Action.RUN_AGENT, "agent": ACME, "place": THREAD_INSIDE},
        ALLOW,
    ),
    (
        "isolation own agent by its MA name",
        ISOLATED,
        {
            "subject": MEMBER,
            "action": Action.RUN_AGENT,
            "agent": AgentRef.of("alias", "Team Acme", "acme"),
            "place": INSIDE,
        },
        ALLOW,
    ),
    (
        "isolation unresolved agent inside fails closed",
        ISOLATED,
        {
            "subject": MEMBER,
            "action": Action.RUN_AGENT,
            "agent": AgentRef.unresolved(),
            "place": INSIDE,
        },
        _deny("channel_isolated"),
    ),
    (
        "isolation setup thread answers as the built-in",
        ISOLATED,
        {
            "subject": MEMBER,
            "action": Action.RUN_AGENT,
            "agent": SHARED,
            "place": Place(channel_id="T1", parent_channel_id="C_ACME", setup_thread=True),
        },
        ALLOW,
    ),
    (
        "isolation server admin in their own DM",
        ISOLATED,
        {"subject": ADMIN, "action": Action.RUN_AGENT, "surface": Surface.DM, "agent": ACME},
        ALLOW,
    ),
    (
        "isolation channel admin of the channel in their hub",
        ISOLATED,
        {
            "subject": ACME_CHANNEL_ADMIN,
            "action": Action.RUN_AGENT,
            "surface": Surface.HUB,
            "agent": ACME,
        },
        ALLOW,
    ),
    (
        "isolation channel admin elsewhere is held",
        ISOLATED,
        {
            "subject": Subject(
                platform_user_id="U_CA", administered_channel_ids=frozenset({"C_X"})
            ),
            "action": Action.RUN_AGENT,
            "surface": Surface.HUB,
            "agent": ACME,
        },
        _deny("agent_pinned_elsewhere"),
    ),
    (
        "isolation channel admin on an agent key is held",
        ISOLATED,
        {
            "subject": AGENT_KEY_CHANNEL_ADMIN,
            "action": Action.RUN_AGENT,
            "surface": Surface.HUB,
            "agent": ACME,
        },
        _deny("agent_pinned_elsewhere"),
    ),
    (
        "isolation own agent never posts outside",
        ISOLATED,
        {"subject": ADMIN, "action": Action.POST, "agent": ACME, "place": Place(own_dm=True)},
        _deny("channel_isolated"),
    ),
    (
        "isolation shared agent never posts inside",
        ISOLATED,
        {"subject": MEMBER, "action": Action.POST, "agent": SHARED, "place": THREAD_INSIDE},
        _deny("channel_isolated"),
    ),
    (
        "isolation own agent sends no DM",
        ISOLATED,
        {
            "subject": MEMBER,
            "action": Action.DIRECT_MESSAGE,
            "agent": ACME,
            "recipient_id": "U_MEM",
        },
        _deny("channel_isolated"),
    ),
    (
        "isolation shared agent reads nothing inside",
        ISOLATED,
        {
            "subject": MEMBER,
            "action": Action.READ_CHANNEL,
            "agent": SHARED,
            "place": INSIDE,
            "origin_channel_ids": frozenset({"C_ACME"}),
        },
        _deny("channel_isolated"),
    ),
    (
        "isolation own agent reads inside",
        ISOLATED,
        {
            "subject": MEMBER,
            "action": Action.READ_CHANNEL,
            "agent": ACME,
            "place": INSIDE,
            "origin_channel_ids": frozenset({"C_ACME"}),
        },
        ALLOW,
    ),
    (
        "isolation shared agent saves no routine into a thread inside",
        ISOLATED,
        {"subject": MEMBER, "action": Action.SAVE_ROUTINE, "agent": SHARED, "place": THREAD_INSIDE},
        _deny("channel_isolated"),
    ),
    (
        "isolation shared agent is no channel default inside",
        ISOLATED,
        {"subject": ADMIN, "action": Action.BIND_CHANNEL_DEFAULT, "agent": SHARED, "place": INSIDE},
        _deny("channel_isolated"),
    ),
    # --- Isolation: C's setup thread and the sessions that ran in C ---
    (
        "isolation built-in in the setup thread reads none of the channel's sessions",
        ISOLATED,
        {
            "subject": MEMBER,
            "action": Action.READ_SESSION,
            "agent": SHARED,
            "origin_channel_ids": frozenset({"C_ACME", "T_SETUP"}),
            "session": SessionFacts(channel="C_ACME", thread="T1", seal_ids=frozenset({"C_ACME"})),
        },
        _deny("channel_isolated"),
    ),
    (
        "isolation built-in continues no session of the channel",
        ISOLATED,
        {
            "subject": MEMBER,
            "action": Action.CONTINUE_SESSION,
            "agent": SHARED,
            "origin_channel_ids": frozenset({"C_ACME", "T1"}),
            "session": SessionFacts(channel="C_ACME", thread="T1", seal_ids=frozenset({"T1"})),
        },
        _deny("channel_isolated"),
    ),
    (
        "isolation own agent reads the channel's sessions",
        ISOLATED,
        {
            "subject": MEMBER,
            "action": Action.READ_SESSION,
            "agent": ACME,
            "origin_channel_ids": frozenset({"C_ACME", "T1"}),
            "session": SessionFacts(channel="C_ACME", thread="T1", seal_ids=frozenset({"C_ACME"})),
        },
        ALLOW,
    ),
    (
        "isolation no agent reads a slack thread sealed under the channel",
        ISOLATED,
        {
            "subject": MEMBER,
            "action": Action.READ_SESSION,
            "origin_channel_ids": frozenset({"C_ACME", "C_ACME:1.2"}),
            "session": SessionFacts(seal_ids=frozenset({"C_ACME:1.2"}), channel="C_X"),
        },
        _deny("channel_isolated"),
    ),
    (
        "isolation setup thread holds the built-in to the channel",
        ISOLATED,
        {
            "subject": MEMBER,
            "action": Action.POST,
            "agent": SHARED,
            "place": OUTSIDE,
            "origin": SETUP_ORIGIN,
        },
        _deny("channel_isolated"),
    ),
    (
        "isolation setup thread built-in posts into its own thread",
        ISOLATED,
        {
            "subject": MEMBER,
            "action": Action.POST,
            "agent": SHARED,
            "place": SETUP_ORIGIN,
            "origin": SETUP_ORIGIN,
        },
        ALLOW,
    ),
    (
        "isolation a plain origin inside lets no outside agent post there",
        ISOLATED,
        {
            "subject": MEMBER,
            "action": Action.POST,
            "agent": SHARED,
            "place": THREAD_INSIDE,
            "origin": THREAD_INSIDE,
        },
        _deny("channel_isolated"),
    ),
    (
        "isolation setup thread built-in sends no DM",
        ISOLATED,
        {
            "subject": MEMBER,
            "action": Action.DIRECT_MESSAGE,
            "agent": SHARED,
            "recipient_id": "U_MEM",
            "origin": SETUP_ORIGIN,
        },
        _deny("channel_isolated"),
    ),
    (
        "isolation a routine saved in the setup thread stays in the channel",
        ISOLATED,
        {
            "subject": MEMBER,
            "action": Action.SAVE_ROUTINE,
            "agent": SHARED,
            "place": OUTSIDE,
            "origin": SETUP_ORIGIN,
        },
        _deny("channel_isolated"),
    ),
    (
        "isolation a routine saved in the setup thread for the own agent",
        ISOLATED,
        {
            "subject": MEMBER,
            "action": Action.SAVE_ROUTINE,
            "agent": ACME,
            "place": INSIDE,
            "origin": SETUP_ORIGIN,
        },
        ALLOW,
    ),
    (
        "isolation a thread with no known parent fails closed",
        ISOLATED,
        {
            "subject": Subject(),
            "action": Action.RUN_AGENT,
            "surface": Surface.ROUTINE,
            "agent": SHARED,
            "place": Place(channel_id="T9", parent_channel_id="T9", parent_unresolved=True),
        },
        _deny("channel_isolated"),
    ),
    (
        "a thread with no known parent runs while nothing is isolated",
        PINNED,
        {
            "subject": Subject(),
            "action": Action.RUN_AGENT,
            "surface": Surface.ROUTINE,
            "agent": SHARED,
            "place": Place(channel_id="T9", parent_channel_id="T9", parent_unresolved=True),
        },
        ALLOW,
    ),
    # --- Fork: admins only, never a pinned agent ---
    (
        "fork by a member",
        TenantAccessPolicy(),
        {"subject": MEMBER, "action": Action.FORK, "agent": SHARED},
        _deny("admin_required"),
    ),
    (
        "fork of a pinned agent",
        ISOLATED,
        {"subject": ADMIN, "action": Action.FORK, "agent": ACME},
        _deny("agent_pinned"),
    ),
    (
        "fork of a shared agent by an admin",
        ISOLATED,
        {"subject": ADMIN, "action": Action.FORK, "agent": SHARED},
        ALLOW,
    ),
    # --- channel environments: a channel admin picks their own channels' ---
    (
        "server admin sets the workspace default environment",
        ACME_SEALED,
        {"subject": ADMIN, "action": Action.SET_CHANNEL_ENVIRONMENT, "open_network": True},
        ALLOW,
    ),
    (
        "channel admin never sets the workspace default environment",
        SEALED,
        {"subject": ACME_CHANNEL_ADMIN, "action": Action.SET_CHANNEL_ENVIRONMENT},
        _deny("admin_required"),
    ),
    (
        "channel admin sets their channel's environment",
        SEALED,
        {
            "subject": ACME_CHANNEL_ADMIN,
            "action": Action.SET_CHANNEL_ENVIRONMENT,
            "place": Place(channel_id="C_ACME"),
            "open_network": True,
        },
        ALLOW,
    ),
    (
        "channel admin sets nothing in another channel",
        SEALED,
        {
            "subject": ACME_CHANNEL_ADMIN,
            "action": Action.SET_CHANNEL_ENVIRONMENT,
            "place": Place(channel_id="C_OTHER"),
        },
        _deny("admin_required"),
    ),
    (
        "channel admin sets a limited network in their sealed channel",
        ACME_SEALED,
        {
            "subject": ACME_CHANNEL_ADMIN,
            "action": Action.SET_CHANNEL_ENVIRONMENT,
            "place": Place(channel_id="C_ACME"),
        },
        ALLOW,
    ),
    (
        "channel admin never opens the network of their sealed channel",
        ACME_SEALED,
        {
            "subject": ACME_CHANNEL_ADMIN,
            "action": Action.SET_CHANNEL_ENVIRONMENT,
            "place": Place(channel_id="C_ACME"),
            "open_network": True,
        },
        _deny("sealed"),
    ),
    (
        "server admin opens the network of a sealed channel",
        ACME_SEALED,
        {
            "subject": ADMIN,
            "action": Action.SET_CHANNEL_ENVIRONMENT,
            "place": Place(channel_id="C_ACME"),
            "open_network": True,
        },
        ALLOW,
    ),
    (
        "channel admin never opens the network of a channel holding a sealed slack thread",
        TenantAccessPolicy(sealed_channel_ids=("C_ACME:1700000000.000100",)),
        {
            "subject": ACME_CHANNEL_ADMIN,
            "action": Action.SET_CHANNEL_ENVIRONMENT,
            "place": Place(channel_id="C_ACME"),
            "open_network": True,
        },
        _deny("sealed"),
    ),
    (
        "channel admin never opens the network of a sealed discord thread it names",
        TenantAccessPolicy(sealed_channel_ids=("T_ACME",)),
        {
            "subject": ACME_CHANNEL_ADMIN,
            "action": Action.SET_CHANNEL_ENVIRONMENT,
            "place": Place(channel_id="T_ACME", parent_channel_id="C_ACME"),
            "open_network": True,
        },
        _deny("sealed"),
    ),
    (
        "channel admin never opens the network from inside a sealed discord thread",
        TenantAccessPolicy(sealed_channel_ids=("T_ACME",)),
        {
            "subject": ACME_CHANNEL_ADMIN,
            "action": Action.SET_CHANNEL_ENVIRONMENT,
            "place": Place(channel_id="C_ACME"),
            "origin": Place(channel_id="T_ACME", parent_channel_id="C_ACME"),
            "open_network": True,
        },
        _deny("sealed"),
    ),
    (
        "a thread sealed under another channel leaves the channel admin's pick open",
        TenantAccessPolicy(sealed_channel_ids=("C_OTHER:1700000000.000100", "T_OTHER")),
        {
            "subject": ACME_CHANNEL_ADMIN,
            "action": Action.SET_CHANNEL_ENVIRONMENT,
            "place": Place(channel_id="C_ACME"),
            "origin": Place(channel_id="T_OTHER", parent_channel_id="C_OTHER"),
            "open_network": True,
        },
        ALLOW,
    ),
    # --- channel protection and seals: server admins only ---
    (
        "server admin protects or seals any channel",
        SEALED,
        {
            "subject": ADMIN,
            "action": Action.SET_CHANNEL_PROTECTION,
            "place": Place(channel_id="C_OTHER"),
        },
        ALLOW,
    ),
    (
        "channel admin protects or seals not even their own channel",
        ACME_SEALED,
        {
            "subject": ACME_CHANNEL_ADMIN,
            "action": Action.SET_CHANNEL_PROTECTION,
            "place": Place(channel_id="C_ACME"),
        },
        _deny("admin_required"),
    ),
    (
        "an agent key protects nothing, whoever minted it",
        PINNED,
        {
            "subject": AGENT_KEY_CHANNEL_ADMIN,
            "action": Action.SET_CHANNEL_PROTECTION,
            "place": Place(channel_id="C_ACME"),
        },
        _deny("admin_required"),
    ),
    (
        "server admin archives an isolation copy",
        OPEN,
        {"subject": ADMIN, "action": Action.ARCHIVE_ISOLATION_COPY},
        ALLOW,
    ),
    (
        "a channel admin archives no isolation copy",
        OPEN,
        {"subject": ACME_CHANNEL_ADMIN, "action": Action.ARCHIVE_ISOLATION_COPY},
        _deny("admin_required"),
    ),
    (
        "an admin's agent key archives no isolation copy",
        OPEN,
        {"subject": AGENT_KEY_ADMIN, "action": Action.ARCHIVE_ISOLATION_COPY},
        _deny("admin_required"),
    ),
    (
        "an agent key picks no environment, whoever minted it",
        PINNED,
        {
            "subject": AGENT_KEY_CHANNEL_ADMIN,
            "action": Action.SET_CHANNEL_ENVIRONMENT,
            "place": Place(channel_id="C_ACME"),
        },
        _deny("admin_required"),
    ),
    # --- channel budgets: server admins only, never a channel's own admins ---
    *(
        (
            name,
            TenantAccessPolicy(),
            {"subject": subject, "action": Action.SET_CHANNEL_BUDGET, "place": place},
            expected,
        )
        for name, subject, place, expected in [
            ("server admin sets any channel's budget", ADMIN, Place(channel_id="C_X"), ALLOW),
            (
                "channel admin can't set their own channel's budget",
                ACME_CHANNEL_ADMIN,
                Place(channel_id="C_ACME"),
                _deny("admin_required"),
            ),
            (
                "nor through one of its threads",
                ACME_CHANNEL_ADMIN,
                Place(channel_id="T1", parent_channel_id="C_ACME"),
                _deny("admin_required"),
            ),
            (
                "channel admin can't set another channel's budget",
                ACME_CHANNEL_ADMIN,
                Place(channel_id="C_OTHER"),
                _deny("admin_required"),
            ),
            (
                "a member sets no budget",
                MEMBER,
                Place(channel_id="C_ACME"),
                _deny("admin_required"),
            ),
            (
                "an agent key sets no budget, whoever minted it",
                AGENT_KEY_CHANNEL_ADMIN,
                Place(channel_id="C_ACME"),
                _deny("admin_required"),
            ),
            (
                "an admin's agent key keeps its budget rights",
                AGENT_KEY_ADMIN,
                Place(channel_id="C_X"),
                ALLOW,
            ),
        ]
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


def test_build_subject_gives_grants_only_to_a_person_without_an_agent_key() -> None:
    """An agent key or a token with no person behind it holds no channel admin grant."""
    grants = ("C_ACME",)
    person = build_subject(is_admin=False, platform_user_id="U1", administered_channel_ids=grants)
    key = build_subject(
        is_admin=False, platform_user_id="U1", via_agent_key=True, administered_channel_ids=grants
    )
    bearer = build_subject(is_admin=False, platform_user_id=None, administered_channel_ids=grants)
    assert person.administered_channel_ids == frozenset(grants), "a person keeps their grants"
    assert key.administered_channel_ids == frozenset(), "an agent key holds none"
    assert bearer.administered_channel_ids == frozenset(), "a platform-less token holds none"
