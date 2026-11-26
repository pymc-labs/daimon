"""A routine fire on OpenAI or Gemini, run as an unattended turn of the chat path.

Managed Agents routines open a headless session (`headless_runner`); another provider
has no such session, so its fire is admitted and driven like a chat turn nobody
watches, in a fresh channel scope. One rule then serves both: the turn's unattended
origin sends every gated Daimon call to the host call gate's unattended verdict, and
the turn is metered to the tenant after admission's balance and cap gates.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from daimon.core.access_policy import CHANNEL_SCOPE_PREFIX
from daimon.core.authz import Subject, Surface
from daimon.core.rule_views import routine_destination_channel
from daimon.core.stores.domain import Role, RoutineRow
from daimon.core.turn.admission import Admission, admit
from daimon.core.turn.ceiling import turn_deadline
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.lifecycle import TurnLifecycle
from daimon.core.turn.prepare import bind_session
from daimon.core.turn.run import run_prepared_turn
from daimon.core.turn.state import TurnState, extract_final_response
from daimon.core.turn.unattended import is_provider_turn
from daimon.core.turn_hold import RequesterOrg


class _Unrendered(TurnLifecycle):
    """Nothing renders a routine turn: the scheduler delivers its final text."""

    async def on_render(self, state: TurnState) -> None:
        return None

    async def on_terminal_success(self, state: TurnState) -> None:
        return None

    async def on_terminal_failure(self, state: TurnState, err: Exception) -> None:
        return None


@dataclass(frozen=True)
class ProviderRoutineRun:
    """A finished provider fire: its final text and state (for the agent's own posts)."""

    text: str
    state: TurnState


def routine_scope_channel(row: RoutineRow) -> str:
    """Where the fire is admitted: its destination's channel, else where it was made."""
    return routine_destination_channel(row) or row.channel_id or f"{CHANNEL_SCOPE_PREFIX}{row.id}"


async def admit_provider_routine(
    deps: TurnDeps, row: RoutineRow, *, platform: str, owner: str, subject: Subject
) -> Admission:
    """Admit the fire as its owner, for the routine's agent, on the place's provider.

    Refused unless it runs on another provider: a place that now answers on Claude
    takes its routine through Managed Agents on the next fire, never through here.
    """
    admission = await admit(
        deps,
        tenant_id=row.tenant_id,
        platform=platform,
        external_user_id=owner,
        channel_id=routine_scope_channel(row),
        now=datetime.now(UTC),
        role=Role.ADMIN if subject.is_admin else Role.USER,
        role_ids=subject.role_ids,
        verified_membership=subject.verified_membership,
        requester_org=RequesterOrg.HOST,
        origin="routine",
        attended=False,
        surface=Surface.ROUTINE,
        routine_agent_name=row.agent_name,
    )
    if not is_provider_turn(admission):
        raise AdmissionDenied(reason="backend_unsupported")
    return admission


async def run_provider_routine(
    deps: TurnDeps,
    admission: Admission,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    owner: str,
    prompt: str,
) -> ProviderRoutineRun:
    """Run the admitted fire once, unattended, in a fresh scope of its channel.

    A failed turn raises its error, as a Managed Agents fire does.
    """
    scope_id = f"{CHANNEL_SCOPE_PREFIX}{uuid.uuid4()}"
    deadline = turn_deadline(now=datetime.now(UTC), ceiling_s=admission.turn_ceiling_s)
    prepared = await bind_session(
        deps,
        admission,
        tenant_id=tenant_id,
        platform=platform,
        external_user_id=owner,
        thread_id=scope_id,
        session_account_id=admission.account_id,
        reuse_existing=False,
        deadline=deadline,
    )

    async def reseed() -> str:
        return prompt

    outcome = await run_prepared_turn(
        deps,
        prepared,
        tenant_id=tenant_id,
        platform=platform,
        thread_id=scope_id,
        external_user_id=owner,
        user_message=prompt,
        lifecycle=_Unrendered(),
        cancel=asyncio.Event(),
        reseed_user_message=reseed,
        recovery_lifecycle=lambda _: _Unrendered(),
        origin="routine",
        attended=False,
        deadline=deadline,
    )
    if outcome.state.error is not None:
        raise outcome.state.error
    return ProviderRoutineRun(extract_final_response(outcome.state.content), outcome.state)
