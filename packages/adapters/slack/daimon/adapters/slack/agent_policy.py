"""One shared-agent gate for every Slack write that targets an agent.

`daimon.core.operation_policy.decide_operation` owns the rule — blast radius
of one agent, open to any member; blast radius of the whole install, admin
only — and the order in which the facts are consulted. This module is its
shell half on Slack: it resolves the caller's live admin status, fetches the
target agent, reads reachability only when the decision actually turns on it,
and renders the refusal. The chat-initiated credential path
(`credential_submissions`) routes through here so a refusal reads the same
wherever the request came from.

Two entry points, differing only in how the target is named:

- `refuse_unless_allowed` takes daimon's derived agent uuid, which is what a
  `credential_requests` row carries. A uuid that no longer resolves to a live
  MA agent is a refusal (`AGENT_GONE_MESSAGE`): the row was minted against an
  agent that has since been archived, and writing anywhere else would be
  wrong.
- `refuse_unless_allowed_for_agent_name` takes the tenant-scoped name, which
  is what a caller naming an agent carries. A name that resolves to no live agent is NOT a
  refusal here: the panel's own stale handling re-renders, and a name can
  still own a config row, so the decision continues with
  `is_daimon_managed=False` and the reachability read.

Both re-resolve admin status live (never from the rendered view or from
`private_metadata`) and re-read reachability from the database on every call.
"""

from __future__ import annotations

import uuid
from typing import Final

import structlog
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.adapters.slack.admin import resolve_is_admin
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag, find_agent_by_derived_uuid
from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED, MA_METADATA_KEY_NAME
from daimon.core.operation_policy import (
    OperationKind,
    PolicyOutcome,
    TargetFacts,
    decide_operation,
    needs_reachability_read,
)
from daimon.core.stores.scoped_config_read import is_agent_reachable_in_tenant
from slack_sdk.web.async_client import AsyncWebClient

__all__ = [
    "AGENT_GONE_MESSAGE",
    "MANAGED_AGENT_MESSAGE",
    "NEEDS_ADMIN_SPEC_MESSAGE",
    "SHARED_AGENT_MESSAGE",
    "gather_target_facts",
    "refusal_message",
    "refuse_unless_allowed",
    "refuse_unless_allowed_for_agent_name",
]

log = structlog.get_logger()

#: A spec edit against the deployment's own agent. Refused for everyone,
#: admins included: a panel edit never stamps the reconciler's spec hash, so
#: the drift would survive every later reconcile with no way back.
MANAGED_AGENT_MESSAGE: Final[str] = (
    ":lock: This is the workspace's built-in agent, so its setup cannot be changed "
    "from here — not even by an admin. Fork it to get an editable copy you own."
)

#: A spec edit by a member against an agent the workspace currently depends on.
NEEDS_ADMIN_SPEC_MESSAGE: Final[str] = (
    ":lock: This agent is currently the default for this workspace or a "
    "channel, so changing its setup needs workspace-admin permission. "
    "Creating or forking your own agent is not restricted."
)

#: An attachment write (repo binding, keys, MCP server) by a member against a
#: shared agent. One string for both the managed and the reachable limb on
#: purpose: which limb fired says nothing the caller can act on, and the fix
#: is the same either way.
SHARED_AGENT_MESSAGE: Final[str] = (
    ":lock: This agent is shared — it is either the workspace's built-in agent or the "
    "current default for this workspace or a channel — so changing its repo or its "
    "keys needs workspace-admin permission. Fork it to get an "
    "editable copy you own; the fork starts with no keys of its own."
)

AGENT_GONE_MESSAGE: Final[str] = (
    "That agent no longer exists — ask again and a fresh request will be posted."
)


async def gather_target_facts(
    runtime: SlackRuntime,
    *,
    tenant_id: uuid.UUID,
    agent: BetaManagedAgentsAgent,
) -> TargetFacts:
    """Both facts the policy table reads about `agent`.

    `is_daimon_managed` comes off the MA metadata the caller already fetched;
    `is_reachable_in_tenant` is one fresh database read, never cached and
    never taken from the rendered view.
    """
    async with runtime.sessionmaker() as session:
        reachable = await is_agent_reachable_in_tenant(
            session,
            tenant_id=tenant_id,
            agent_name=_agent_name_of(agent),
            default=runtime.deployment_default,
        )
    return TargetFacts(
        is_daimon_managed=_is_daimon_managed(agent), is_reachable_in_tenant=reachable
    )


async def refuse_unless_allowed(
    runtime: SlackRuntime,
    client: AsyncWebClient,
    *,
    operation: OperationKind,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    channel_id: str,
    user_id: str,
    thread_ts: str | None = None,
) -> bool:
    """Decide `operation` against the agent daimon knows as `agent_id`.

    Returns True when the caller must stop (the refusal has been posted as an
    ephemeral), False to proceed.
    """
    is_admin = await resolve_is_admin(client, user_id=user_id)
    if _allowed_whatever_the_target(operation, is_admin=is_admin):
        return False

    agent = await find_agent_by_derived_uuid(
        runtime.anthropic, tenant_id=tenant_id, agent_id=agent_id
    )
    if agent is None:
        log.warning(
            "slack.agent_policy.agent_gone",
            tenant_id=str(tenant_id),
            agent_id=str(agent_id),
            operation=operation,
        )
        await _post_refusal(
            client,
            channel_id=channel_id,
            user_id=user_id,
            thread_ts=thread_ts,
            text=AGENT_GONE_MESSAGE,
        )
        return True

    facts = await _facts_for_decision(
        runtime, operation=operation, is_admin=is_admin, tenant_id=tenant_id, agent=agent
    )
    return await _render_outcome(
        client,
        operation=operation,
        outcome=decide_operation(operation, is_admin=is_admin, target=facts),
        channel_id=channel_id,
        user_id=user_id,
        thread_ts=thread_ts,
    )


async def refuse_unless_allowed_for_agent_name(
    runtime: SlackRuntime,
    client: AsyncWebClient,
    *,
    operation: OperationKind,
    tenant_id: uuid.UUID,
    agent_name: str,
    channel_id: str,
    user_id: str,
    thread_ts: str | None = None,
) -> bool:
    """Decide `operation` against the agent the panel calls `agent_name`.

    A name with no live MA agent behind it is not refused here — see the
    module docstring — it is decided as an unmanaged target, which leaves the
    reachability read as the only thing standing between a member and the
    write.
    """
    is_admin = await resolve_is_admin(client, user_id=user_id)
    if _allowed_whatever_the_target(operation, is_admin=is_admin):
        return False

    agent = await find_agent_by_daimon_tag(runtime.anthropic, tenant_id=tenant_id, name=agent_name)
    is_daimon_managed = agent is not None and _is_daimon_managed(agent)
    reachable = False
    if needs_reachability_read(operation, is_admin=is_admin, is_daimon_managed=is_daimon_managed):
        async with runtime.sessionmaker() as session:
            reachable = await is_agent_reachable_in_tenant(
                session,
                tenant_id=tenant_id,
                agent_name=agent_name,
                default=runtime.deployment_default,
            )
    facts = TargetFacts(is_daimon_managed=is_daimon_managed, is_reachable_in_tenant=reachable)
    return await _render_outcome(
        client,
        operation=operation,
        outcome=decide_operation(operation, is_admin=is_admin, target=facts),
        channel_id=channel_id,
        user_id=user_id,
        thread_ts=thread_ts,
    )


def _allowed_whatever_the_target(operation: OperationKind, *, is_admin: bool) -> bool:
    """True when no fact about the target can change this caller's outcome.

    Asks the policy table rather than restating its short-circuits: an admin
    doing an attachment write, and anyone doing a posted-token write, are
    allowed against every combination of facts, so neither should pay for the
    MA fetch that gathers them.
    """
    return all(
        decide_operation(
            operation,
            is_admin=is_admin,
            target=TargetFacts(is_daimon_managed=managed, is_reachable_in_tenant=reachable),
        )
        == "allow"
        for managed in (False, True)
        for reachable in (False, True)
    )


async def _facts_for_decision(
    runtime: SlackRuntime,
    *,
    operation: OperationKind,
    is_admin: bool,
    tenant_id: uuid.UUID,
    agent: BetaManagedAgentsAgent,
) -> TargetFacts:
    """`gather_target_facts`, minus the read the decision does not turn on.

    When `needs_reachability_read` says the outcome is already settled, the
    reachability value cannot change it, so the read is skipped and the field
    carries its unreachable default.
    """
    is_daimon_managed = _is_daimon_managed(agent)
    if not needs_reachability_read(
        operation, is_admin=is_admin, is_daimon_managed=is_daimon_managed
    ):
        return TargetFacts(is_daimon_managed=is_daimon_managed, is_reachable_in_tenant=False)
    return await gather_target_facts(runtime, tenant_id=tenant_id, agent=agent)


def _is_daimon_managed(agent: BetaManagedAgentsAgent) -> bool:
    return agent.metadata.get(MA_METADATA_KEY_MANAGED) == "true"


def _agent_name_of(agent: BetaManagedAgentsAgent) -> str:
    return str(agent.metadata.get(MA_METADATA_KEY_NAME) or agent.name)


def refusal_message(operation: OperationKind, outcome: PolicyOutcome) -> str:
    """The copy for a refused `operation`.

    Spec edits and attachment writes refuse for different reasons and point
    at different ways forward, so the family picks the wording; within the
    attachment family both refused outcomes share one string.
    """
    if operation == "agent_spec_edit":
        return MANAGED_AGENT_MESSAGE if outcome == "managed_agent" else NEEDS_ADMIN_SPEC_MESSAGE
    return SHARED_AGENT_MESSAGE


async def _render_outcome(
    client: AsyncWebClient,
    *,
    operation: OperationKind,
    outcome: PolicyOutcome,
    channel_id: str,
    user_id: str,
    thread_ts: str | None,
) -> bool:
    if outcome == "allow":
        return False
    await _post_refusal(
        client,
        channel_id=channel_id,
        user_id=user_id,
        thread_ts=thread_ts,
        text=refusal_message(operation, outcome),
    )
    return True


async def _post_refusal(
    client: AsyncWebClient,
    *,
    channel_id: str,
    user_id: str,
    thread_ts: str | None,
    text: str,
) -> None:
    await client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
        channel=channel_id or user_id,
        user=user_id,
        text=text,
        thread_ts=thread_ts,
    )
