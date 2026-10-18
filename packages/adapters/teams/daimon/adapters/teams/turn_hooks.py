"""A turn's mid-turn cards: tool confirmations and provider-action questions."""

from __future__ import annotations

import uuid

from daimon.adapters.teams.identity import TeamsInbound
from daimon.adapters.teams.provider_actions import (
    ProviderActionOptions,
    TeamsProviderActions,
    provider_action_options,
)
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.adapters.teams.tool_confirmation import TeamsConfirmationCards
from daimon.core.confirmation import ConfirmationHook
from daimon.core.turn.admission import Admission
from daimon.core.turn.prepare import PreparedTurn


class TurnHooks(ProviderActionOptions):
    confirm_write: ConfirmationHook


def turn_hooks(
    confirmations: TeamsConfirmationCards,
    provider_actions: TeamsProviderActions,
    *,
    runtime: TeamsRuntime,
    inbound: TeamsInbound,
    prepared: PreparedTurn,
    admission: Admission,
    tenant_id: uuid.UUID,
) -> TurnHooks:
    """`run_prepared_turn`'s card hooks for one turn of `inbound`."""
    return {
        "confirm_write": confirmations.hook(
            runtime=runtime,
            tenant_id=tenant_id,
            account_id=admission.account_id,
            admission=admission,
            conversation_id=inbound.conversation_id,
            service_url=inbound.service_url,
            team_group_id=inbound.team_group_id,
            requester_display_name=inbound.user_name,
        ),
        **provider_action_options(
            prepared,
            lambda: provider_actions.hook(
                conversation_id=inbound.conversation_id,
                service_url=inbound.service_url,
                thread_id=inbound.thread_id,
                team_group_id=inbound.team_group_id,
            ),
        ),
    }
