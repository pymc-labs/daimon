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
from daimon.adapters.discord.checks import is_guild_admin
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.stores.scoped_config_read import is_agent_reachable_in_tenant

import discord

log = structlog.get_logger()

_SYSTEM_AGENT_MESSAGE = (
    "This agent ships with the deployment and is managed from the repo defaults. "
    "Fork it to make an editable copy."
)
_REACHABLE_AGENT_MESSAGE = (
    "This agent is currently the default for this channel or the server, so "
    "changing its setup needs Manage Server. Fork it to make an editable copy."
)
_SHARED_AGENT_MESSAGE = (
    "This agent is shared — it either ships with the deployment or is the "
    "current default for this channel or the server — so changing its repo or "
    "keys needs Manage Server. Ask a server admin to make this change in `/agent-setup`, "
    "or fork it to make an editable copy; the fork starts with no keys of its own."
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
       the target currently resolves for some channel or the workspace.
    """
    if entry is None:
        log.debug("agent_setup.authz.no_target")
        return True
    if entry.is_system:
        await _send_ephemeral(interaction, _SYSTEM_AGENT_MESSAGE)
        return True
    if is_guild_admin(interaction):  # pyright: ignore[reportArgumentType]  # discord.Interaction vs Interaction[commands.Bot]; is_guild_admin only reads user/guild
        return False
    tenant_id = await resolve_tenant_for_panel(runtime, interaction)
    async with runtime.sessionmaker() as session:
        reachable = await is_agent_reachable_in_tenant(
            session,
            tenant_id=tenant_id,
            agent_name=entry.name,
            default=runtime.deployment_default,
        )
    if reachable:
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
       the target currently resolves for some channel or the workspace.

    Both refusals carry the same message on purpose: telling the caller which
    limb fired would say nothing they can act on, and either way the fix is the
    same — ask an admin, or fork.
    """
    if entry is None:
        log.debug("agent_setup.authz.no_target")
        return True
    if is_guild_admin(interaction):  # pyright: ignore[reportArgumentType]  # discord.Interaction vs Interaction[commands.Bot]; is_guild_admin only reads user/guild
        return False
    if entry.is_system:
        await _send_ephemeral(interaction, _SHARED_AGENT_MESSAGE)
        return True
    tenant_id = await resolve_tenant_for_panel(runtime, interaction)
    async with runtime.sessionmaker() as session:
        reachable = await is_agent_reachable_in_tenant(
            session,
            tenant_id=tenant_id,
            agent_name=entry.name,
            default=runtime.deployment_default,
        )
    if reachable:
        await _send_ephemeral(interaction, _SHARED_AGENT_MESSAGE)
        return True
    return False
