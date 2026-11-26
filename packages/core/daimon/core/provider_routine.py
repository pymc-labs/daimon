"""A routine fire on OpenAI or Gemini, run as an unattended turn of the chat path.

Managed Agents routines open a headless session (`headless_runner`); another provider
has no such session, so its fire is admitted and driven like a chat turn nobody
watches, in the routine's own channel scope. One rule then serves both: the turn's unattended
origin sends every gated Daimon call to the host call gate's unattended verdict, and
the turn is metered to the tenant after admission's balance and cap gates. Built-in
tools run only with the routine's sandbox opt-in, as on Managed Agents.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from daimon.core.access_policy import CHANNEL_SCOPE_PREFIX
from daimon.core.authz import Subject, Surface
from daimon.core.output_runtime import OutputRuntime
from daimon.core.routine_provider import routine_thread_id
from daimon.core.routine_sandbox import RoutineSandbox, builtins_allowed, provider_run_matches_pin
from daimon.core.rule_views import RoutineOrigin, routine_destination_channel
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
from daimon.core.turn_hold import RequesterOrg, TurnHold
from mux.profiles import get_profile

SHELL_NEEDS_SANDBOX = (
    "This routine's agent is on OpenAI, which always runs it with a shell. "
    "Ask a server admin to turn on sandbox tools for this routine."
)


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
    """A finished provider fire: its final text, state (for the agent's own posts) and files."""

    text: str
    state: TurnState
    outputs: OutputRuntime | None = None


def routine_scope_channel(row: RoutineRow) -> str:
    """Where the fire is admitted: its destination's channel, else where it was made."""
    return routine_destination_channel(row) or row.channel_id or f"{CHANNEL_SCOPE_PREFIX}{row.id}"


def routine_scope_thread(routine_id: uuid.UUID) -> str:
    """The one thread a routine's provider fires bind, a fresh session each time."""
    return f"{CHANNEL_SCOPE_PREFIX}routine:{routine_id}"


async def admit_provider_routine(
    deps: TurnDeps,
    row: RoutineRow,
    *,
    platform: str,
    owner: str,
    subject: Subject,
    sandbox: RoutineSandbox | None,
    hold: TurnHold | None = None,
    origin_place: RoutineOrigin | None = None,
) -> Admission:
    """Admit the fire as its owner, for the routine's agent, on the place's provider:
    a thread destination's kept one, else its channel's or agent's.

    Refused unless it runs on another provider: a place that now answers on Claude
    takes its routine through Managed Agents on the next fire, never through here.
    `sandbox` is read by `with_routine_sandbox`; `hold` and `origin_place` place it
    as `headless_runner` places a Claude fire (`at_fire_place`).
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
        # A thread destination keeps its own provider, as `routine_provider` routes it.
        routine_thread_id=routine_thread_id(row, platform=platform),
    )
    if not is_provider_turn(admission):
        raise AdmissionDenied(reason="backend_unsupported")
    admission = at_fire_place(admission, hold=hold, origin_place=origin_place)
    return await with_routine_sandbox(
        deps, admission, sandbox, tenant_id=row.tenant_id, platform=platform
    )


def at_fire_place(
    admission: Admission, *, hold: TurnHold | None, origin_place: RoutineOrigin | None
) -> Admission:
    """The fire held and sealed where it posts, with read-only memory, as a Claude fire.

    Admission placed it in its destination channel only: the thread it posts to and that
    thread's own seal come from `origin_place`, and the scheduler's hold (`routine_hold`)
    holds it even where admission read no shared channel.
    """
    if hold is not None and admission.hold is None and admission.grant is not None:
        grant = admission.grant
        subject = replace(grant.subject, is_admin=False, client=hold.client)
        admission = replace(admission, hold=hold, grant=replace(grant, hold=hold, subject=subject))
    if origin_place is not None:
        admission = replace(
            admission,
            origin_thread_id=origin_place.thread_id,
            origin_seal_ids=admission.origin_seal_ids | origin_place.seal_ids,
        )
    return replace(admission, memory_read_only=True)


async def with_routine_sandbox(
    deps: TurnDeps,
    admission: Admission,
    sandbox: RoutineSandbox | None,
    *,
    tenant_id: uuid.UUID,
    platform: str,
) -> Admission:
    """A provider routine's admission with built-ins on only while the opt-in covers
    this fire's provider, agent and environment and `unattended_capabilities` still
    allows them; a drifted pin is reported. OpenAI's shell cannot be left out, so
    there a fire without the opt-in is refused."""
    revision = admission.backend_revision
    assert revision is not None, "a provider turn has a revision"
    opted_in = False
    if sandbox is not None:
        opted_in = await provider_run_matches_pin(
            deps.anthropic, sandbox.pin, admission, tenant_id=tenant_id, records=deps.sessionmaker
        )
        if not opted_in:
            await sandbox.on_drift()
        opted_in = opted_in and await builtins_allowed(
            deps.sessionmaker, deps.settings, tenant_id, platform, admission.agent.id
        )
    if not opted_in and get_profile(revision.profile).implicit_execution:
        raise AdmissionDenied(reason="backend_unsupported", detail=SHELL_NEEDS_SANDBOX)
    return replace(admission, routine_sandbox_tools=opted_in)


async def run_provider_routine(
    deps: TurnDeps,
    admission: Admission,
    *,
    routine_id: uuid.UUID,
    tenant_id: uuid.UUID,
    platform: str,
    owner: str,
    prompt: str,
) -> ProviderRoutineRun:
    """Run the admitted fire once, unattended, in the routine's own scope.

    Each fire replaces that scope's session, so a run starts fresh, as a Managed
    Agents fire does, and a routine keeps one binding. A failed turn raises its error.
    """
    scope_id = routine_scope_thread(routine_id)
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
    return ProviderRoutineRun(
        extract_final_response(outcome.state.content), outcome.state, outcome.outputs
    )
