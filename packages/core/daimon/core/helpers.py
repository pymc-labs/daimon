"""Helpers: agents a turn's agent asks for help inside its own turn, on any provider.

The asking agent calls the `ask_helper` MCP tool, which records a `helper_asks`
row. The process running the asking turn serves those rows (`serve_helpers`):
each ask is a turn of the helper agent, admitted for the same person in the same
channel and thread, so every rule, seal, hold, budget and confirmation applies as
if that person had asked the helper there. A helper keeps its own conversation
per thread (`helper_thread_key`), posts nothing, and its last message is the reply.
Files go both ways by link (`helper_message`): the helper downloads the ask's files
and uploads its own with curl, so any backend whose sandbox reaches Daimon works.
Everything stops when the asking turn ends; a helper's turn asks no helpers.
An agent whose roster is `[self]` on a provider without native multiagent asks
copies of itself the same way (`daimon.core.self_helpers`), each under its own key.
"""

from __future__ import annotations

import asyncio
import contextvars
import shlex
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal

import structlog
from daimon.core.anthropic_guard import anthropic_disabled, without_anthropic
from daimon.core.billing_panel import spend_over_cap
from daimon.core.confirmation import ConfirmationHook
from daimon.core.context_prompt import TurnContext
from daimon.core.errors import UserFacingError
from daimon.core.ma_resolver import MAResolverMissError
from daimon.core.media.filenames import sanitize_title
from daimon.core.pricing import provider_prices
from daimon.core.scope import HelperTarget
from daimon.core.self_helpers import (
    TURN_BUDGET_USD,
    is_self_roster,
    self_helper_thread_key,
    translates_self_roster,
)
from daimon.core.stores import agent_helpers, helper_asks
from daimon.core.stores.domain import (
    HelperAskRow,
    HelperAskStatus,
    TurnOriginRow,
    helper_thread_key,
)
from daimon.core.stores.turn_origins import get_active_origin, mark_serving_helpers
from daimon.core.turn.admission import Admission, AdmitRequest, HelperCall
from daimon.core.turn.ceiling import turn_deadline
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.errors import AdmissionDenied, MissingTurnConfigError
from daimon.core.turn.host_spend_cap import priced_cents
from daimon.core.turn.lifecycle import TurnLifecycle
from daimon.core.turn.provider_actions import (
    NativeActionResponse,
    ProviderActionHook,
    ProviderActionPrompt,
)
from daimon.core.turn.state import TurnState, extract_final_response
from daimon.core.turn_origin import current_origin_id
from daimon.core.usage_totals import ProviderUsageTotals
from mux.profiles import get_profile

log = structlog.get_logger(__name__)

#: Helpers running at once for one turn; more asks wait their turn.
MAX_RUNNING = 3
#: Asks one turn may make, so a looping agent cannot run up the person's bill.
MAX_ASKS = 20
#: How often the asking turn's process looks for new asks.
POLL_S = 1.0
#: How long a stopped helper gets to end its turn before it is cancelled.
STOP_GRACE_S = 15.0
#: Turns that serve helpers: a person's own messages. A helper's, a routine's or a
#: relay's turn asks none.
SERVING_ORIGINS: frozenset[TurnContext] = frozenset({"chat", "handoff"})
_REFUSALS = {
    "balance_depleted": "the person's balance is used up",
    "cap_exceeded": "the person reached their spending cap",
    "channel_budget_exceeded": "this channel's budget is used up",
    "allowance_exhausted": "the person's allowance is used up",
    "invoker_not_allowed": "the person may not use it here",
    "writers_none": "the person may not use it here",
    "runs_elsewhere": "it is kept to other channels",
    "own_agents_only": "this channel only uses its own agents",
    "client_requester": "it can't run for someone from another organisation here",
    "provider_setup_pending": "its model provider is still being set up, so ask again shortly",
}
SELF_BUDGET_SPENT = "Your copies used up their spending limit for this message. Answer now."

#: An ask's outcome: its status, its reply, and what its turn cost in US dollars
#: (None when it could not be priced).
Answered = tuple[HelperAskStatus, str, Decimal | None]
#: Runs one ask as a turn; `lane` is a copy's own key, None for another agent.
Runner = Callable[[HelperAskRow, asyncio.Event, str | None], Awaitable[Answered]]


def refusal_words(error: Exception, helper_name: str) -> str:
    """Why a helper's turn could not start, for the agent that asked. Pure."""
    if isinstance(error, AdmissionDenied):
        why = _REFUSALS.get(error.reason, "its model provider can't run it here")
        return f"{helper_name} can't run for this person: {why}."
    if isinstance(error, UserFacingError):
        return f"{helper_name} can't run: {error}"
    if isinstance(error, MAResolverMissError | MissingTurnConfigError):
        return f"{helper_name} is not set up here any more."
    return f"{helper_name} stopped with an error before it answered."


class _Quiet(TurnLifecycle):
    """A helper's turn draws nothing: its reply goes back to the agent that asked."""

    async def on_render(self, state: TurnState) -> None:
        return None

    async def on_terminal_success(self, state: TurnState) -> None:
        return None

    async def on_terminal_failure(self, state: TurnState, err: Exception) -> None:
        return None


def helper_message(
    asker: str, ask: HelperAskRow, app_root: str | None, *, copy: bool = False
) -> str:
    """What a helper reads for one ask: the message, its files and how to send files back.

    Without a public URL there are no links, and `ask_helper` takes no files. A
    `copy` of the asking agent is told it shares no files with it.
    """
    text = f"{asker} asks:\n{ask.message}"
    if copy:
        text = (
            f"You are a copy of {asker}, asked for help. You share no files with it: "
            f"files move only by link.\n\n{text}"
        )
    if app_root is None or ask.files_token is None:
        return text
    base = f"{app_root}/helper-files/{ask.files_token}"
    if ask.files:
        # The name is a person's or an agent's; the helper pastes this line into a shell.
        saved_as = {f.id: shlex.quote(sanitize_title(f.name)) for f in ask.files}
        fetch = "\n".join(f'  curl -sS -o {name} "{base}/{id_}"' for id_, name in saved_as.items())
        text += f"\n\nFiles {asker} gave you; download each with:\n{fetch}"
    return (
        f"{text}\n\nTo send files back with your reply, upload each with:\n"
        f'  curl -sS -T <file> "{base}/"\n'
        "A file you only save, even under /mnt/session/outputs, is not sent."
    )


def turn_spend(state: TurnState, profile: str, model: str) -> Decimal | None:
    """What a finished helper turn cost at list price, in US dollars; None when unpriceable."""
    provider = get_profile(profile).provider
    price = provider_prices(profile).get((provider, model))
    if price is None or not isinstance(state.usage_totals, ProviderUsageTotals):
        return None
    cents = priced_cents(state.usage_totals.selected, price, provider)
    return None if cents is None else cents / 100


@dataclass
class _Desk:
    """One asking turn's helpers: what they run on and what is running now."""

    deps: TurnDeps
    request: AdmitRequest
    origin: TurnOriginRow
    asker_name: str
    deadline: datetime
    confirm_write: ConfirmationHook | None
    provider_action: ProviderActionHook | None
    # The asking run's self-hosted guard, which a helper's fresh context would drop.
    no_anthropic: bool
    # The asking agent's id when it may ask copies of itself.
    self_id: str | None = None
    # Runs an ask; `_run` unless a test hands in its own.
    runner: Runner | None = None
    # Keyed by conversation: a helper's agent id, or a copy's own key.
    running: dict[str, asyncio.Task[None]] = field(default_factory=dict[str, asyncio.Task[None]])
    stops: dict[str, list[asyncio.Event]] = field(default_factory=dict[str, list[asyncio.Event]])
    copies: int = 0
    # What the copies cost so far; an unpriced one counts as over the limit.
    copies_spent: Decimal = Decimal(0)
    copies_unpriced: bool = False

    def copies_over_budget(self) -> bool:
        return self.copies_unpriced or spend_over_cap(float(self.copies_spent), TURN_BUDGET_USD)

    async def serve(self) -> None:
        while True:
            for helper_id, task in list(self.running.items()):
                if task.done():
                    del self.running[helper_id]
                    self.stops.pop(helper_id, None)
            try:
                async with self.deps.sessionmaker.begin() as session:
                    claimed = await helper_asks.claim_queued(
                        session,
                        origin_id=self.origin.id,
                        skip_helpers=self.running.keys(),
                        limit=MAX_RUNNING - len(self.running),
                        parallel_helper=self.self_id,
                    )
            # One failed poll is retried on the next; it must not end serving.
            except Exception as error:
                log.warning("helpers.poll_failed", err_type=type(error).__name__, exc_info=error)
                claimed = []
            for ask in claimed:
                lane = None
                if ask.helper_agent_id == self.self_id:
                    if self.copies_over_budget():
                        await self._finish(ask, "failed", SELF_BUDGET_SPENT)
                        continue
                    self.copies += 1
                    lane = self_helper_thread_key(self.origin.thread_id, self.copies)
                key = lane or ask.helper_agent_id
                cancel = asyncio.Event()
                self.stops[key] = [cancel]
                # A fresh context: the helper's turn records its own outcome and logs,
                # never into the asking turn's.
                self.running[key] = asyncio.create_task(
                    self._answer(ask, cancel, lane), context=contextvars.Context()
                )
            await asyncio.sleep(POLL_S)

    async def stop(self) -> None:
        for events in self.stops.values():
            for event in events:
                event.set()
        tasks = list(self.running.values())
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=STOP_GRACE_S)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        async with self.deps.sessionmaker.begin() as session:
            await helper_asks.stop_open_asks(
                session, origin_id=self.origin.id, now=datetime.now(UTC)
            )

    async def _answer(self, ask: HelperAskRow, cancel: asyncio.Event, lane: str | None) -> None:
        structlog.contextvars.bind_contextvars(
            helper_ask_id=str(ask.id), helper_of=str(self.origin.id)
        )
        status: HelperAskStatus
        spent: Decimal | None = None
        try:
            with without_anthropic() if self.no_anthropic else nullcontext():
                status, reply, spent = await (self.runner or self._run)(ask, cancel, lane)
        # A helper's turn is a named boundary: whatever stops it is the asking
        # agent's to hear about, and must never end the asking turn.
        except Exception as error:
            log.warning("helpers.ask_failed", err_type=type(error).__name__, exc_info=error)
            status, reply = "failed", refusal_words(error, ask.helper_name)
        if lane is not None:
            # A copy that may have spent without a price fails closed: no more copies.
            if spent is None:
                self.copies_unpriced = True
            else:
                self.copies_spent += spent
        await self._finish(ask, status, reply)

    async def _finish(self, ask: HelperAskRow, status: HelperAskStatus, reply: str) -> None:
        async with self.deps.sessionmaker.begin() as session:
            await helper_asks.finish_ask(
                session, ask_id=ask.id, status=status, reply=reply, now=datetime.now(UTC)
            )

    async def _run(self, ask: HelperAskRow, cancel: asyncio.Event, lane: str | None) -> Answered:
        """One ask as a turn of the helper. It runs in the asking turn's slot, which
        waits meanwhile; taking a slot of its own could wait on the asking turn."""
        from daimon.core.turn.admission import admit_helper, backend_facts, running_on
        from daimon.core.turn.prepare import bind_session
        from daimon.core.turn.run import run_prepared_turn
        from daimon.core.turn_origin import render_turn_origin, turn_origin

        deps, request, asking = self.deps, self.request, self.origin
        call = HelperCall(
            target=HelperTarget(ma_agent_id=ask.helper_agent_id, name=ask.helper_name),
            thread_key=lane or helper_thread_key(asking.thread_id, ask.helper_agent_id),
        )
        admission = await admit_helper(deps, request, call, now=datetime.now(UTC))
        prepared = await bind_session(
            deps,
            admission,
            tenant_id=request.tenant_id,
            platform=request.platform,
            external_user_id=request.external_user_id,
            thread_id=call.thread_key,
            session_account_id=admission.account_id,
            # Each copy starts fresh; another helper keeps its conversation in the thread.
            reuse_existing=lane is None,
            deadline=self.deadline,
        )
        async with turn_origin(
            deps.sessionmaker,
            tenant_id=request.tenant_id,
            account_id=admission.account_id,
            platform=asking.platform,
            parent_channel_id=asking.parent_channel_id,
            thread_id=asking.thread_id,
            responder_ma_agent_id=admission.agent.id,
            responder_name=admission.config.agent_name or admission.agent.name,
            role=asking.role,
            sender_foreign=admission.hold is not None and admission.hold.client,
            shared_channel=admission.shared_channel,
            platform_channel_id=asking.platform_channel_id,
            helper_of=asking.id,
            helper_key=lane,
        ) as origin:
            continued = prepared.continuity.state == "continued"
            controls = render_turn_origin(
                origin,
                **backend_facts(admission),
                session_state=None if continued else prepared.continuity.session_state(),
                effective_is_admin=admission.is_admin,
            )
            asks = helper_message(
                self.asker_name, ask, deps.mcp.app_root_url, copy=lane is not None
            )
            message = f"{controls}\n\n{asks}"

            async def reseed() -> str:
                return message

            def recovered(fresh: asyncio.Event) -> TurnLifecycle:
                # A replaced session's stop switch follows it.
                self.stops.setdefault(lane or ask.helper_agent_id, []).append(fresh)
                if cancel.is_set():
                    fresh.set()
                return lifecycle

            lifecycle = _Quiet()
            outcome = await run_prepared_turn(
                deps,
                prepared,
                tenant_id=request.tenant_id,
                platform=request.platform,
                thread_id=call.thread_key,
                external_user_id=request.external_user_id,
                user_message=message,
                request_text=ask.message,
                lifecycle=lifecycle,
                cancel=cancel,
                reseed_user_message=reseed,
                recovery_lifecycle=recovered,
                origin="helper",
                deadline=self.deadline,
                confirm_write=self.confirm_write,
                provider_action=_in_thread(self.provider_action, asking.thread_id),
            )
        revision = admission.backend_revision
        spent = (
            turn_spend(outcome.state, revision.profile, running_on(admission)["model"])
            if revision is not None
            else None
        )
        if cancel.is_set():
            return "stopped", f"{ask.helper_name} was stopped before it finished.", spent
        if outcome.state.error is not None:
            log.warning("helpers.turn_errored", kind=outcome.state.error.kind)
            return (
                "failed",
                f"{ask.helper_name} stopped with an error before it answered.",
                spent,
            )
        reply = extract_final_response(outcome.state.content).strip()
        return "done", reply or f"{ask.helper_name} finished without a reply.", spent


def _in_thread(hook: ProviderActionHook | None, thread_id: str) -> ProviderActionHook | None:
    """``hook`` with prompts placed in the asking thread: the helper's session runs under
    its own key, but the person answers where they asked."""
    if hook is None:
        return None

    async def placed(prompt: ProviderActionPrompt) -> NativeActionResponse | None:
        requester = replace(prompt.context.requester, thread_id=thread_id)
        return await hook(replace(prompt, context=replace(prompt.context, requester=requester)))

    return placed


def asks_copies(admission: Admission) -> bool:
    """Whether the turn's agent asks copies of itself: its roster is `[self]` and its
    provider has no native multiagent, which would run the roster itself."""
    revision = admission.backend_revision
    roster = admission.agent.multiagent
    return (
        revision is not None
        and roster is not None
        and translates_self_roster(revision.profile)
        and is_self_roster(roster.agents, admission.agent.id)
    )


async def _wind_down(serving: asyncio.Task[None], desk: _Desk) -> None:
    serving.cancel()
    await asyncio.gather(serving, return_exceptions=True)
    await desk.stop()


@asynccontextmanager
async def serve_helpers(
    deps: TurnDeps,
    admission: Admission,
    *,
    origin: TurnContext,
    deadline: datetime | None,
    confirm_write: ConfirmationHook | None,
    provider_action: ProviderActionHook | None,
) -> AsyncIterator[None]:
    """Run this turn's helper asks while the turn runs; stop them all when it ends.

    A no-op unless a person invoked the turn (a message, not a wake), it has a
    trusted origin and its agent has helpers, so other turns pay one query at most.
    """
    origin_id = current_origin_id.get()
    if (
        origin not in SERVING_ORIGINS
        or origin_id is None
        or admission.grant is None
        or not admission.grant.human_invocation
        or admission.request is None
    ):
        yield
        return
    copies = asks_copies(admission)
    async with deps.sessionmaker() as session:
        helpers = copies or await agent_helpers.list_helper_ids(
            session, tenant_id=admission.request.tenant_id, agent_id=admission.agent.id
        )
        asking = (
            await get_active_origin(
                session,
                origin_id=origin_id,
                tenant_id=admission.request.tenant_id,
                account_id=admission.account_id,
                platform=admission.request.platform,
                now=datetime.now(UTC),
            )
            if helpers
            else None
        )
    if asking is None or asking.helper_of is not None:
        yield
        return
    desk = _Desk(
        deps=deps,
        request=admission.request,
        origin=asking,
        asker_name=admission.config.agent_name or admission.agent.name,
        deadline=deadline or turn_deadline(now=datetime.now(UTC)),
        confirm_write=confirm_write,
        provider_action=provider_action,
        no_anthropic=anthropic_disabled(),
        self_id=admission.agent.id if copies else None,
    )
    async with deps.sessionmaker.begin() as session:
        await mark_serving_helpers(session, origin_id=asking.id, self_helpers=copies)
    serving = asyncio.create_task(desk.serve())
    try:
        yield
    finally:
        # Helpers stop even when the asking turn is cancelled meanwhile.
        stopping = asyncio.create_task(_wind_down(serving, desk))
        try:
            await asyncio.shield(stopping)
        except asyncio.CancelledError:
            await stopping
            raise
