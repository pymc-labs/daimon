"""Who answers where, for one install, as one readable object.

The cascade is the same one `daimon.core.scope` resolves for a single
mention; this module lays it out whole — every channel that names an agent,
the workspace default, the deployment fall-through — so a reader can see the
precedence rather than infer it from one resolved answer. Adapters render
this; they never re-derive precedence.

`build_answering_map` is pure. `load_answering_map` is its shell: two store
reads, no decisions of its own.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from daimon.core.channel_isolation import IsolationViewer
from daimon.core.scope import ChannelConfigRow, DeploymentDefault, TenantConfigRow
from daimon.core.stores.domain import ThreadAgentBindingRow
from daimon.core.stores.scoped_config_read import list_propagations_for_tenant
from daimon.core.stores.thread_agent_bindings import list_active_setup_bindings_for_tenant
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession


class ChannelAnswer(BaseModel):
    """One channel whose own setting names the agent that answers there."""

    model_config = ConfigDict(frozen=True)

    channel_id: str
    agent_name: str
    set_by_account_id: uuid.UUID | None = None
    set_at: datetime | None = None


class TenantAnswer(BaseModel):
    """The install-wide default, which every channel without its own falls to."""

    model_config = ConfigDict(frozen=True)

    agent_name: str
    set_by_account_id: uuid.UUID | None = None
    set_at: datetime | None = None


class SetupThreadRef(BaseModel):
    """A live setup conversation, enough to link to it and say what it is for."""

    model_config = ConfigDict(frozen=True)

    thread_id: str
    parent_channel_id: str
    target_name: str | None = None
    creator_account_id: uuid.UUID | None = None
    updated_at: datetime


class AnsweringMap(BaseModel):
    """Every tier of one install's routing, plus its live setup conversations.

    `tenant_consumes_fallthrough` is the fact a renderer cannot recover from
    the other fields: a workspace default does not merely sit above the
    deployment default, it removes it from the cascade entirely, so naming
    the deployment default as reachable alongside it would be wrong.
    """

    model_config = ConfigDict(frozen=True)

    channel_overrides: tuple[ChannelAnswer, ...] = ()
    tenant_default: TenantAnswer | None = None
    deployment_default: str | None = None
    tenant_consumes_fallthrough: bool = False
    setup_threads: tuple[SetupThreadRef, ...] = ()
    setup_threads_truncated: bool = False


def build_answering_map(
    *,
    tenant: TenantConfigRow | None,
    channels: Sequence[ChannelConfigRow],
    default: DeploymentDefault,
    setup_threads: Sequence[ThreadAgentBindingRow],
    setup_threads_truncated: bool,
) -> AnsweringMap:
    """Fold config rows and live setup conversations into one readable map.

    A row counts as an override only in mode='agent' with a non-empty
    agent_name — the same gate the cascade applies, so a channel handed to a
    user-active session is not reported as routing to an agent. Channels come
    out ordered by channel_id.

    Pure — no I/O, no clock.
    """
    overrides = tuple(
        ChannelAnswer(
            channel_id=row.channel_id,
            agent_name=row.agent_name,
            set_by_account_id=row.agent_name_set_by_account_id,
            set_at=row.agent_name_set_at,
        )
        for row in sorted(channels, key=lambda row: row.channel_id)
        if row.mode == "agent" and row.agent_name
    )
    tenant_default: TenantAnswer | None = None
    if tenant is not None and tenant.mode == "agent" and tenant.agent_name:
        tenant_default = TenantAnswer(
            agent_name=tenant.agent_name,
            set_by_account_id=tenant.agent_name_set_by_account_id,
            set_at=tenant.agent_name_set_at,
        )
    return AnsweringMap(
        channel_overrides=overrides,
        tenant_default=tenant_default,
        deployment_default=default.agent_name,
        tenant_consumes_fallthrough=tenant_default is not None,
        setup_threads=tuple(
            SetupThreadRef(
                thread_id=binding.thread_id,
                parent_channel_id=binding.parent_channel_id,
                target_name=binding.configuration_target_name,
                creator_account_id=binding.creator_account_id,
                updated_at=binding.updated_at,
            )
            for binding in setup_threads
        ),
        setup_threads_truncated=setup_threads_truncated,
    )


def hide_across_isolation(answering: AnsweringMap, viewer: IsolationViewer) -> AnsweringMap:
    """Only the routing on the reader's side of every isolated channel's line. Pure."""
    outside = viewer.inside_channel_id is None
    tenant_default = answering.tenant_default
    deployment_default = answering.deployment_default if outside else None
    return answering.model_copy(
        update={
            "channel_overrides": tuple(
                row
                for row in answering.channel_overrides
                if viewer.sees_place(row.channel_id) and viewer.sees(row.agent_name)
            ),
            "tenant_default": tenant_default if outside else None,
            "deployment_default": deployment_default,
            "setup_threads": tuple(
                ref
                for ref in answering.setup_threads
                if viewer.sees_place(ref.parent_channel_id)
                and (ref.target_name is None or viewer.sees(ref.target_name))
            ),
        }
    )


def routed_agent_names(answering_map: AnsweringMap) -> frozenset[str]:
    """Every agent some tier routes to, anywhere in the install.

    The deployment default counts only while no workspace default has taken
    the fall-through away from it.
    """
    names = {answer.agent_name for answer in answering_map.channel_overrides}
    if answering_map.tenant_default is not None:
        names.add(answering_map.tenant_default.agent_name)
    if answering_map.deployment_default and not answering_map.tenant_consumes_fallthrough:
        names.add(answering_map.deployment_default)
    return frozenset(names)


async def load_answering_map(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    default: DeploymentDefault,
    viewer: IsolationViewer | None = None,
) -> AnsweringMap:
    """Read one install's config rows and live setup conversations, then fold them.

    `viewer` hides what an isolated channel keeps from the caller.
    """
    tenant_row, channel_rows = await list_propagations_for_tenant(session, tenant_id=tenant_id)
    setup_threads, truncated = await list_active_setup_bindings_for_tenant(
        session, tenant_id=tenant_id, platform=platform
    )
    answering = build_answering_map(
        tenant=tenant_row,
        channels=channel_rows,
        default=default,
        setup_threads=setup_threads,
        setup_threads_truncated=truncated,
    )
    return answering if viewer is None else hide_across_isolation(answering, viewer)
