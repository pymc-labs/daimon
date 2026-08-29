"""Shared post-ack authorization gates for Slack /agent-setup.

Field-follows-the-gate rule for the next contributor wiring up a new
mutating action. Two gates live here, and which one a write routes through
follows from whether the field it writes is part of the agent spec:

- Spec fields -- skills, MCP servers, the model, the system prompt -- route
  through ``refuse_if_reachable_and_not_admin``. That gate refuses a
  defaults-managed agent unconditionally, admins included, because a panel
  edit never stamps the reconciler's spec hash: the resulting drift would
  survive every later reconcile with no way back. Forking is the editable
  path. Below that, a non-admin is refused whenever the target is currently
  reachable (a channel or workspace default) -- an unreachable agent has no
  live gate to defend, so any member may configure it.

- Per-agent attachments -- the repo binding, the inline token, env-variable
  credentials -- route through ``refuse_if_shared_and_not_admin``. They never
  enter the agent spec, so the defaults-managed absolutism above does not
  apply to them: an admin binding a repo to the workspace's built-in agent is
  a supported first-run step. They are NOT open to non-admins on a shared
  agent, though, because a repo re-point or a secret overwrite changes state
  every member of the workspace depends on.

A new mutating action routes through exactly one of the two. "No gate" is
not one of the options.

The two open-time behaviours differ on purpose. The edit-repo form is gated
when it is pushed, because that form prompts for a GitHub personal access
token and no such credential should transit for a write that is going to be
refused anyway. The paste-secrets form prompts for nothing on push, so it is
gated at submission only -- the boundary that actually writes.

Both gates resolve admin status live via ``resolve_is_admin`` (never trusting
anything carried in the rendered view or in ``private_metadata``) and re-read
reachability fresh from the DB on every call -- no caching, matching the
panel's existing re-resolve discipline for admin status. Both live here rather
than at the call sites so a newly-wired mutating action inherits its checks by
routing through one helper.
"""

from __future__ import annotations

import uuid

from daimon.adapters.slack.admin import resolve_is_admin
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag
from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED
from daimon.core.stores.scoped_config_read import is_agent_reachable_in_tenant
from slack_sdk.web.async_client import AsyncWebClient

__all__ = ["refuse_if_reachable_and_not_admin", "refuse_if_shared_and_not_admin"]

_SEEDED_AGENT_MESSAGE = (
    ":lock: This is the workspace's built-in agent, so its setup cannot be changed "
    "from here — not even by an admin. Fork it to get an editable copy you own."
)

_SHARED_AGENT_MESSAGE = (
    ":lock: This agent is shared — it is either the workspace's built-in agent or the "
    "current default for this workspace or a channel — so changing its repo or its "
    "environment variables needs workspace-admin permission. Fork it to get an "
    "editable copy you own; the fork starts with no environment variables of its own."
)


async def refuse_if_reachable_and_not_admin(
    runtime: SlackRuntime,
    web_client: AsyncWebClient,
    *,
    tenant_id: uuid.UUID,
    agent_name: str,
    channel_id: str,
    user_id: str,
) -> bool:
    """Refuse a spec-touching action against a built-in or reachable agent.

    Returns ``True`` when the caller must return early (refused); ``False``
    when the caller should proceed.

    A defaults-managed agent is refused first and unconditionally, admins
    included, so that check precedes the admin short-circuit. Otherwise an
    admin caller is never refused and never pays for a DB read. A non-admin
    caller is refused only when the target agent is currently reachable
    (scoped as a channel or workspace default); an unreachable agent has no
    live gate to defend, so any member may still configure it.

    Args:
        runtime:       Injected ``SlackRuntime`` (sessionmaker, deployment
                        default).
        web_client:     Per-event ``AsyncWebClient``.
        tenant_id:      Derived from the verified Socket Mode workspace id --
                        never accepted from the interactive payload.
        agent_name:     The target agent's name -- used only as a
                        tenant-scoped lookup key.
        channel_id:     Invoking channel, for the refusal ephemeral.
        user_id:        Invoking user, for the admin check and the ephemeral.

    Returns:
        ``True`` if the caller must refuse and return early, ``False`` to
        proceed.
    """
    # Defaults-managed agents are off-limits to everyone, admins included, and
    # this check therefore runs BEFORE the admin short-circuit. A panel edit
    # never stamps the reconciler's spec hash, so an edited seeded agent would
    # be skipped by the hash short-circuit on every subsequent reconcile and
    # drift from the shipped defaults permanently. Forking is the editable path.
    # Read fresh from the roster rather than from anything the rendered view or
    # private_metadata carried, matching this module's re-resolve discipline.
    seeded = await find_agent_by_daimon_tag(runtime.anthropic, tenant_id=tenant_id, name=agent_name)
    if seeded is not None and seeded.metadata.get(MA_METADATA_KEY_MANAGED) == "true":
        await web_client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
            channel=channel_id or user_id,
            user=user_id,
            text=_SEEDED_AGENT_MESSAGE,
        )
        return True

    is_admin = await resolve_is_admin(web_client, user_id=user_id)
    if is_admin:
        return False

    async with runtime.sessionmaker() as session:
        reachable = await is_agent_reachable_in_tenant(
            session,
            tenant_id=tenant_id,
            agent_name=agent_name,
            default=runtime.deployment_default,
        )
    if not reachable:
        return False

    await web_client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
        channel=channel_id or user_id,
        user=user_id,
        text=(
            ":lock: This agent is currently the default for this workspace or a "
            "channel, so changing its setup needs workspace-admin permission. "
            "Creating or forking your own agent is not restricted."
        ),
    )
    return True


async def refuse_if_shared_and_not_admin(
    runtime: SlackRuntime,
    web_client: AsyncWebClient,
    *,
    tenant_id: uuid.UUID,
    agent_name: str,
    channel_id: str,
    user_id: str,
) -> bool:
    """Refuse an attachment write against a shared agent by a non-admin.

    ``refuse_if_shared_and_not_admin`` is the gate for per-agent attachments --
    the repo binding, the inline token, env-variable credentials. Spec-touching
    writes route through ``refuse_if_reachable_and_not_admin`` instead.

    Returns ``True`` when the caller must return early (refused); ``False``
    when the caller should proceed. Order, each short-circuiting:

    1. A live workspace admin -> allow, before any MA or DB read. This is the
       one step ordered differently from ``refuse_if_reachable_and_not_admin``,
       and it is what keeps an admin able to bind a repo to the workspace's
       built-in agent -- the first-run onboarding step.
    2. The target is a defaults-managed agent -> refuse; it ships with the
       deployment and every member of the workspace shares it.
    3. Otherwise read reachability fresh from the DB and refuse when the
       target currently resolves for the workspace or some channel. An
       unreachable agent has no shared state to defend, so any member may
       configure it.

    Both refusals carry the same message on purpose: telling the caller which
    limb fired would say nothing they can act on, and either way the fix is the
    same -- ask an admin, or fork.

    Args:
        runtime:        Injected ``SlackRuntime`` (sessionmaker, deployment
                        default).
        web_client:     Per-event ``AsyncWebClient``.
        tenant_id:      Derived from the verified Socket Mode workspace id --
                        never accepted from the interactive payload.
        agent_name:     The target agent's name -- used only as a
                        tenant-scoped lookup key.
        channel_id:     Invoking channel, for the refusal ephemeral.
        user_id:        Invoking user, for the admin check and the ephemeral.

    Returns:
        ``True`` if the caller must refuse and return early, ``False`` to
        proceed.
    """
    is_admin = await resolve_is_admin(web_client, user_id=user_id)
    if is_admin:
        return False

    seeded = await find_agent_by_daimon_tag(runtime.anthropic, tenant_id=tenant_id, name=agent_name)
    if seeded is not None and seeded.metadata.get(MA_METADATA_KEY_MANAGED) == "true":
        await web_client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
            channel=channel_id or user_id,
            user=user_id,
            text=_SHARED_AGENT_MESSAGE,
        )
        return True

    async with runtime.sessionmaker() as session:
        reachable = await is_agent_reachable_in_tenant(
            session,
            tenant_id=tenant_id,
            agent_name=agent_name,
            default=runtime.deployment_default,
        )
    if not reachable:
        return False

    await web_client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
        channel=channel_id or user_id,
        user=user_id,
        text=_SHARED_AGENT_MESSAGE,
    )
    return True
