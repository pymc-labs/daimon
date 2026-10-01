"""Click-time authorization gates for the panel's mutating writes.

`EditView`'s controls are disabled when the render-time snapshot says a control
shouldn't be usable — that snapshot is a rendering hint, not a boundary. This
module is the boundary: the gates here re-derive admin status and reachability
from live state rather than trusting anything the view was constructed with, so
a stale open panel or a demoted admin cannot write a change the rendered panel
had already forbidden.

Two gates live here, and which one a write routes through follows from whether
the field it writes is part of the agent spec:

- Spec fields — prompt, model, skills, MCP servers — route through
  `refuse_if_reachable_and_not_admin`. That gate refuses a defaults-managed
  agent even for an admin, because a panel spec edit never stamps the
  reconciler's spec hash: an admin bypass would leave permanent, silent drift
  against the repo defaults that reconcile never notices.

- Per-agent attachments — the repo binding and the keys — route through
  `refuse_if_shared_and_not_admin`. Attachments never enter the agent spec, so
  the system-agent absolutism above does not apply to them, and an admin
  binding a repo to the seeded agent is the first-run onboarding step. That
  gate is still closed to non-admins on any shared agent, whether the agent is
  shared because it ships with the deployment or because something currently
  resolves to it.

A new mutating control belongs to one of those two sets. Pick its gate from the
set the field is in, not from the button it happens to sit next to.

There are now two writes bounded by a single-use `credential_requests` token
rather than by guild-admin status: `credential_modals.py`'s env/MCP credential
writes, and the repo bind reached from a chat-posted request button. What
admits a write to that class is the token, consumed by the write itself, not
proximity to this panel — a control a member reaches by clicking with no such
token still belongs to one of the two sets above.

A token alone is sufficient authorization for an env secret or an MCP token:
the requester supplies a value only they hold, scoped to one key on one agent.
It is not sufficient for a repo binding, which changes what code the agent
clones and runs and, on a shared agent, reaches every member of the install.
So the repo-bind path additionally re-derives this module's
`refuse_if_shared_and_not_admin` decision from primitives, at click time, in
`credential_repo_bind.refuse_if_shared_and_not_admin_for_request`.
"""

from __future__ import annotations

import structlog
from daimon.adapters.discord.agent_setup.state import RosterEntry
from daimon.adapters.discord.agent_setup.tenant import resolve_tenant_for_panel
from daimon.adapters.discord.checks import ADMIN_NOUN, channel_admin_caller, is_guild_admin
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.agent_reach import load_target_facts
from daimon.core.operation_policy import (
    OperationKind,
    TargetFacts,
    decide_operation,
    needs_reachability_read,
)

import discord

log = structlog.get_logger()

_SYSTEM_AGENT_MESSAGE = (
    "This is a starting agent and can't be changed directly. Ask an admin to copy it, "
    "or ask me to make you a new agent."
)
_REACHABLE_AGENT_MESSAGE = (
    f"This agent answers for other people here, so this change needs {ADMIN_NOUN}. "
    "Ask me and I'll write the request for them."
)
_SHARED_AGENT_MESSAGE = (
    "This agent answers for other people here, so changing its repo or its keys "
    f"needs {ADMIN_NOUN}. Ask me and I'll write the request for them, or ask me "
    "to make you a new agent of your own."
)


async def _target_facts(
    interaction: discord.Interaction,  # type: ignore[type-arg]  # discord.Interaction default generic arg; only user/guild_id are used
    *,
    runtime: DiscordRuntime,
    entry: RosterEntry,
    operation: OperationKind,
    is_admin: bool,
) -> TargetFacts:
    """The live facts `operation` turns on, channel admin locality included, as Slack reads them."""
    if not needs_reachability_read(operation, is_admin=is_admin, is_daimon_managed=entry.is_system):
        return TargetFacts(is_daimon_managed=entry.is_system, is_reachable_in_tenant=False)
    tenant_id = await resolve_tenant_for_panel(runtime, interaction)
    caller = channel_admin_caller(interaction.user).model_copy(update={"is_server_admin": is_admin})
    async with runtime.sessionmaker() as session:
        return await load_target_facts(
            session,
            operation,
            tenant_id=tenant_id,
            platform="discord",
            agent_name=entry.name,
            default=runtime.deployment_default,
            caller=caller,
            is_daimon_managed=entry.is_system,
        )


async def _send_ephemeral(interaction: discord.Interaction, content: str) -> None:  # type: ignore[type-arg]  # discord.Interaction default generic arg; only response/followup are used
    if interaction.response.is_done():
        await interaction.followup.send(content, ephemeral=True)
    else:
        await interaction.response.send_message(content, ephemeral=True)


async def refuse_if_reachable_and_not_admin(
    interaction: discord.Interaction,  # type: ignore[type-arg]  # discord.Interaction default generic arg; only response/followup/guild_id are used
    *,
    runtime: DiscordRuntime,
    entry: RosterEntry | None,
) -> bool:
    """Click-time re-check shared by every panel callback that writes an agent spec.

    Returns True when the caller must return immediately (the write is
    refused); False when the write may proceed. Order, each short-circuiting:

    1. No target selected -> refuse silently (belt and braces; every call
       site already narrows `entry` past its own `selected is None` check).
    2. The target is a system agent -> refuse, unconditionally, even for an
       admin (a panel edit never stamps the seed's spec hash, so an admin
       bypass here would reintroduce silent drift against the defaults).
    3. A live guild admin -> allow, without reading the database.
    4. Otherwise, read reachability fresh from the database and refuse when
       the target currently resolves for some channel or the workspace,
       unless it is local to channels the caller administers.

    The decision itself is `daimon.core.operation_policy.decide_operation`'s;
    this function only supplies the facts and renders the outcome.
    """
    if entry is None:
        log.debug("agent_setup.authz.no_target")
        return True
    is_admin = is_guild_admin(interaction)  # pyright: ignore[reportArgumentType]  # discord.Interaction vs Interaction[commands.Bot]; is_guild_admin only reads user/guild
    facts = await _target_facts(
        interaction, runtime=runtime, entry=entry, operation="agent_spec_edit", is_admin=is_admin
    )
    outcome = decide_operation("agent_spec_edit", is_admin=is_admin, target=facts)
    if outcome == "managed_agent":
        await _send_ephemeral(interaction, _SYSTEM_AGENT_MESSAGE)
        return True
    if outcome == "needs_admin":
        await _send_ephemeral(interaction, _REACHABLE_AGENT_MESSAGE)
        return True
    return False


async def refuse_if_shared_and_not_admin(
    interaction: discord.Interaction,  # type: ignore[type-arg]  # discord.Interaction default generic arg; only response/followup/guild_id are used
    *,
    runtime: DiscordRuntime,
    entry: RosterEntry | None,
) -> bool:
    """Click-time re-check shared by every callback writing a per-agent attachment.

    Returns True when the caller must return immediately (the write is
    refused); False when the write may proceed. Order, each short-circuiting:

    1. No target selected -> refuse silently (belt and braces; every call
       site already narrows `entry` past its own `selected is None` check).
    2. A live guild admin -> allow, before any MA or database read. This is
       the one step ordered differently from `refuse_if_reachable_and_not_admin`,
       and it is what keeps an admin able to bind a repo to the seeded agent.
    3. The target is a system agent -> refuse; it belongs to the deployment
       and every member of the install shares it.
    4. Otherwise, read reachability fresh from the database and refuse when
       the target currently resolves for some channel or the workspace,
       unless it is local to channels the caller administers.

    Both refusals carry the same message on purpose: telling the caller which
    limb fired would say nothing they can act on, and either way the fix is the
    same — ask an admin, or fork.

    The decision itself is `daimon.core.operation_policy.decide_operation`'s;
    this function only supplies the facts and renders the outcome.
    """
    if entry is None:
        log.debug("agent_setup.authz.no_target")
        return True
    is_admin = is_guild_admin(interaction)  # pyright: ignore[reportArgumentType]  # discord.Interaction vs Interaction[commands.Bot]; is_guild_admin only reads user/guild
    if is_admin:
        return False
    facts = await _target_facts(
        interaction, runtime=runtime, entry=entry, operation="key_replace", is_admin=is_admin
    )
    outcome = decide_operation("key_replace", is_admin=is_admin, target=facts)
    if outcome in ("managed_agent", "needs_admin"):
        await _send_ephemeral(interaction, _SHARED_AGENT_MESSAGE)
        return True
    return False
