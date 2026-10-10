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
`private_metadata`) and re-read reachability and channel admin grants from the
database on every call.

`refuse_unless_pin_allows` applies the pin rule (`daimon.core.agent_pins.
pin_refusal`, the private forms' rule) with the panel's channel as the place.
"""

from __future__ import annotations

import uuid
from typing import Final

import structlog
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.adapters.slack.admin import ADMIN_NOUN, resolve_is_admin
from daimon.adapters.slack.channel_admin_groups import channel_admin_caller
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.agent_pins import agent_pin_names, pin_refusal
from daimon.core.agent_reach import load_target_facts
from daimon.core.authz import Place, Subject
from daimon.core.channel_admins import ChannelAdminCaller, load_live_subject
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag, find_agent_by_derived_uuid
from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED
from daimon.core.operation_policy import (
    OperationKind,
    PolicyOutcome,
    TargetFacts,
    decide_operation,
    needs_reachability_read,
)
from slack_sdk.web.async_client import AsyncWebClient

__all__ = [
    "AGENT_GONE_MESSAGE",
    "MANAGED_AGENT_MESSAGE",
    "NEEDS_ADMIN_SKILL_MESSAGE",
    "NEEDS_ADMIN_SPEC_MESSAGE",
    "SHARED_AGENT_MESSAGE",
    "SHARED_AGENT_SKILLS_MESSAGE",
    "gather_target_facts",
    "refusal_message",
    "refuse_unless_allowed",
    "refuse_unless_allowed_for_agent_name",
    "refuse_unless_pin_allows",
]

log = structlog.get_logger()

#: A spec edit against the deployment's own agent. Refused for everyone,
#: admins included: a panel edit never stamps the reconciler's spec hash, so
#: the drift would survive every later reconcile with no way back.
MANAGED_AGENT_MESSAGE: Final[str] = (
    "You can't change a starting agent.\n\n"
    "Ask me to make you a new agent, or ask an admin to copy this one."
)

#: A spec edit by a member against an agent the workspace currently depends on.
NEEDS_ADMIN_SPEC_MESSAGE: Final[str] = (
    f"This agent answers for other people here, so this change needs {ADMIN_NOUN}. "
    "Ask me and I'll write the request for them. Making your own agent is not restricted."
)

#: A skill added or removed by a member on an agent someone else also uses.
NEEDS_ADMIN_SKILL_MESSAGE: Final[str] = (
    "Others use this agent (a default, a thread, or someone else's routine or "
    f"conversation), so changing its skills needs {ADMIN_NOUN} or an admin of every "
    "channel it answers in."
)

#: An attachment write (repo binding, keys, MCP server) by a member against a
#: shared agent. One string for both the managed and the reachable limb on
#: purpose: which limb fired says nothing the caller can act on, and the fix
#: is the same either way.
SHARED_AGENT_MESSAGE: Final[str] = (
    "Other people use this agent. Changing its repo or keys needs an admin.\n\n"
    "Ask me to draft a request for an admin, or to make you a new agent."
)

#: A skill-repo import by a member onto a shared agent: the imported skills
#: would reach everyone it answers.
SHARED_AGENT_SKILLS_MESSAGE: Final[str] = (
    "Other people use this agent. Adding skills needs an admin.\n\n"
    "Ask me to draft a request for an admin, or to make you a new agent."
)

AGENT_GONE_MESSAGE: Final[str] = (
    "That agent no longer exists — ask again and a fresh request will be posted."
)


async def gather_target_facts(
    runtime: SlackRuntime,
    *,
    operation: OperationKind,
    tenant_id: uuid.UUID,
    agent_names: tuple[str | None, ...],
    ma_agent_id: str | None,
    is_daimon_managed: bool,
    caller: ChannelAdminCaller,
    caller_account_id: uuid.UUID | None = None,
) -> TargetFacts:
    """The policy facts about one target, read fresh and only while the decision turns on them.

    Reachability and channel admin locality are database reads, never cached and
    never taken from the rendered view. A caller is a channel admin when listed
    by user id or through a user group (`channel_admin_caller`). A decision that turns on neither
    opens no session at all.
    """
    if not needs_reachability_read(
        operation, is_admin=caller.is_server_admin, is_daimon_managed=is_daimon_managed
    ):
        return TargetFacts(is_daimon_managed=is_daimon_managed, is_reachable_in_tenant=False)
    async with runtime.sessionmaker() as session:
        return await load_target_facts(
            session,
            operation,
            tenant_id=tenant_id,
            platform="slack",
            agent_names=agent_names,
            ma_agent_id=ma_agent_id,
            default=runtime.deployment_default,
            caller=caller,
            is_daimon_managed=is_daimon_managed,
            caller_account_id=caller_account_id,
            caller_platform_user_id=caller.platform_user_id,
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
    caller_account_id: uuid.UUID | None = None,
) -> bool:
    """Decide `operation` against the agent daimon knows as `agent_id`.

    Returns True when the caller must stop (the refusal has been posted as an
    ephemeral), False to proceed. `caller_account_id` leaves the caller's own
    live sessions out of the sharing read; None counts them.
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

    facts = await gather_target_facts(
        runtime,
        operation=operation,
        tenant_id=tenant_id,
        agent_names=agent_pin_names(agent.name, agent.metadata),
        ma_agent_id=str(agent.id),
        is_daimon_managed=_is_daimon_managed(agent),
        caller=await channel_admin_caller(
            runtime, client, tenant_id=tenant_id, user_id=user_id, is_admin=is_admin
        ),
        caller_account_id=caller_account_id,
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
    caller_account_id: uuid.UUID | None = None,
    agent: BetaManagedAgentsAgent | None = None,
    refusal_text: str | None = None,
) -> bool:
    """Decide `operation` against the agent the panel calls `agent_name`.

    `refusal_text`, when given, replaces the operation's generic refusal.

    A name with no live MA agent behind it is not refused here — see the
    module docstring — it is decided as an unmanaged target, which leaves the
    reachability read as the only thing standing between a member and the
    write. `caller_account_id` leaves the caller's own live sessions out of
    the sharing read; None counts them. `agent`, when the caller holds a fresh
    read, is decided as it is rather than looked up again by name.
    """
    is_admin = await resolve_is_admin(client, user_id=user_id)
    if _allowed_whatever_the_target(operation, is_admin=is_admin):
        return False

    if agent is None:
        agent = await find_agent_by_daimon_tag(
            runtime.anthropic, tenant_id=tenant_id, name=agent_name
        )
    facts = await gather_target_facts(
        runtime,
        operation=operation,
        tenant_id=tenant_id,
        agent_names=(agent_name,)
        if agent is None
        else (agent_name, *agent_pin_names(agent.name, agent.metadata)),
        ma_agent_id=None if agent is None else str(agent.id),
        is_daimon_managed=agent is not None and _is_daimon_managed(agent),
        caller=await channel_admin_caller(
            runtime, client, tenant_id=tenant_id, user_id=user_id, is_admin=is_admin
        ),
        caller_account_id=caller_account_id,
    )
    return await _render_outcome(
        client,
        operation=operation,
        outcome=decide_operation(operation, is_admin=is_admin, target=facts),
        channel_id=channel_id,
        user_id=user_id,
        thread_ts=thread_ts,
        text=refusal_text,
    )


async def refuse_unless_pin_allows(
    runtime: SlackRuntime,
    client: AsyncWebClient,
    *,
    tenant_id: uuid.UUID,
    agent_name: str,
    channel_id: str,
    user_id: str,
    is_admin: bool,
    agent: BetaManagedAgentsAgent | None = None,
) -> bool:
    """Refuse a configuration write the agent's pin forbids from the panel in `channel_id`.

    A server admin, or an admin of every channel in the pin, may write from
    anywhere; anyone else only from inside the pin. Without `agent` the name is
    looked up, and only under a pin. Returns True when refused (posted).
    """
    caller = await channel_admin_caller(
        runtime, client, tenant_id=tenant_id, user_id=user_id, is_admin=is_admin
    )

    async def target() -> BetaManagedAgentsAgent | None:
        if agent is not None:
            return agent
        return await find_agent_by_daimon_tag(
            runtime.anthropic, tenant_id=tenant_id, name=agent_name
        )

    async with runtime.sessionmaker() as session:

        async def live_subject() -> Subject:
            return await load_live_subject(
                session, tenant_id=tenant_id, platform="slack", caller=caller
            )

        refusal = await pin_refusal(
            session,
            tenant_id=tenant_id,
            load_subject=live_subject,
            load_agent=target,
            place=Place.from_origin(parent_channel_id=channel_id or None, thread_id=None),
        )
    if refusal is None:
        return False
    await _post_refusal(
        client, channel_id=channel_id, user_id=user_id, thread_ts=None, text=refusal
    )
    return True


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


def _is_daimon_managed(agent: BetaManagedAgentsAgent) -> bool:
    return agent.metadata.get(MA_METADATA_KEY_MANAGED) == "true"


def refusal_message(operation: OperationKind, outcome: PolicyOutcome) -> str:
    """The copy for a refused `operation`.

    Spec edits and attachment writes refuse for different reasons and point
    at different ways forward, so the family picks the wording; within the
    attachment family both refused outcomes share one string.
    """
    if operation == "agent_spec_edit":
        return MANAGED_AGENT_MESSAGE if outcome == "managed_agent" else NEEDS_ADMIN_SPEC_MESSAGE
    if operation in ("skill_add", "skill_remove"):
        return MANAGED_AGENT_MESSAGE if outcome == "managed_agent" else NEEDS_ADMIN_SKILL_MESSAGE
    if operation == "skill_repo_connect":
        return SHARED_AGENT_SKILLS_MESSAGE
    return SHARED_AGENT_MESSAGE


async def _render_outcome(
    client: AsyncWebClient,
    *,
    operation: OperationKind,
    outcome: PolicyOutcome,
    channel_id: str,
    user_id: str,
    thread_ts: str | None,
    text: str | None = None,
) -> bool:
    if outcome == "allow":
        return False
    await _post_refusal(
        client,
        channel_id=channel_id,
        user_id=user_id,
        thread_ts=thread_ts,
        text=text or refusal_message(operation, outcome),
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
