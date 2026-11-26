"""Switch this conversation's model when someone asks in words ("use opus", "think harder").

``register_thread_model_tools(mcp, runtime)`` wires the ``@mcp.tool`` closure;
it delegates to ``_set_thread_model_impl`` so the gate is testable without a
FastMCP Context. ``/model`` on Discord writes the same setting
(`daimon.core.thread_models`).

Only a person's own turn may switch it: the call must name this execution's
verified origin, and that origin must be attended. Routines, wakes and
agent-chat callers have no attended origin, so they can never change the model
a conversation's people chose.

A pick within the conversation's provider is kept at once
(`daimon.core.thread_model_switch.pick_thread`); its note says what is off on the new
provider. A pick that moves it to another provider previews first and applies on the
person's card (`provider_move_applies`).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from functools import partial

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._authz_facts import mcp_subject
from daimon.adapters.mcp.tools._card import provider_move_applies, provider_move_line
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools.setup_target import refuse_helper, require_turn_origin
from daimon.core import channel_backend
from daimon.core.admin_actions import DelegatedAction
from daimon.core.agent_model_names import AmbiguousModel
from daimon.core.backend_refusals import refusal_card
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.model_switch import SwitchRefusal
from daimon.core.models_catalog import catalog_model
from daimon.core.stores import agent_backends
from daimon.core.stores.domain import TurnOriginRow
from daimon.core.thread_model_switch import (
    ThreadPick,
    moves_conversation,
    pick_thread,
    preview_thread_pick,
)
from daimon.core.thread_models import (
    EFFORT_OFF_CLAUDE,
    THREAD_MODEL_CHOICES,
    ThreadModelRefused,
    choose_thread_model,
    effort_refusal,
    model_efforts,
    resolve_effort,
)
from daimon.core.turn.notices import uses_phrase
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from mux.contracts.config import DEFAULT_BACKEND
from mux.contracts.ids import ModelRef, Provider

_UNATTENDED = (
    "Only someone in the conversation can change its model, and nobody started "
    "this run. Nothing was changed."
)


@dataclass(frozen=True)
class ThreadModelResult:
    """Result returned from set_thread_model."""

    model: str
    """The model now chosen, 'default' for the agent's own."""
    note: str
    """What changed, and what is off on the new provider; report it back to the person."""
    effort: str | None = None
    """The effort now chosen, None for the agent's own."""
    revision: int | None = None
    """Set when nothing changed yet: pass it back to move to another provider on the card."""


async def _set_thread_model_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    origin_context_id: str,
    model: str | None = None,
    effort: str | None = None,
    revision: int | None = None,
) -> ThreadModelResult:
    origin = refuse_helper(await require_turn_origin(runtime, auth, origin_context_id))
    if not origin.attended:
        raise ToolError(_UNATTENDED)
    try:
        resolve_effort(effort)  # a bad effort refuses before anything changes
    except ThreadModelRefused as exc:
        raise ToolError(f"{exc} Nothing changed.") from exc
    wants_effort = effort is not None and bool(effort.strip())
    if model is None or not model.strip():
        if not wants_effort:
            raise ToolError("Name a model, an effort, or both. Nothing changed.")
        return await _choose_claude(runtime, auth, origin, None, effort)
    chosen, _reset = resolve_effort(effort)
    model_id = THREAD_MODEL_CHOICES.get(model.strip().lower())
    off = await _agent_provider(runtime, auth, origin) is not None
    if (
        chosen is not None
        and model_id is not None
        and not off
        and chosen not in model_efforts(model_id)
    ):
        # Refuse before the model is saved: a refusal changes nothing.
        raise ToolError(f"{effort_refusal(DEFAULT_BACKEND, model_id)} Nothing changed.")
    result = await _set_model(runtime, auth, origin, model, revision)
    if not wants_effort or result.revision is not None:
        return result
    if model_id is not None and off:
        return _with_note(result, EFFORT_OFF_CLAUDE)
    try:
        return await _choose_claude(runtime, auth, origin, None, effort)
    except ToolError as refused:
        return _with_note(result, f"The effort was not set: {refused}")


def _with_note(result: ThreadModelResult, extra: str) -> ThreadModelResult:
    return replace(result, note=f"{result.note} {extra}")


async def _set_model(
    runtime: McpRuntime,
    auth: AuthIdentity,
    origin: TurnOriginRow,
    model: str,
    revision: int | None,
) -> ThreadModelResult:
    word = model.strip().lower()
    if word == "default":
        return await _choose_claude(runtime, auth, origin, word)
    value = (
        f"{DEFAULT_BACKEND}:{THREAD_MODEL_CHOICES[word]}" if word in THREAD_MODEL_CHOICES else model
    )
    picked = await _catalog_model(runtime, auth, value, model)
    if picked.provider == DEFAULT_BACKEND and await _agent_provider(runtime, auth, origin) is None:
        return await _choose_claude(runtime, auth, origin, _word_of(picked.id, said=model))
    pick = await _pick(runtime, auth, origin)
    admin = auth.admin_for(DelegatedAction.AGENT_SETTINGS)
    moves = await moves_conversation(pick, picked.provider, thread_id=origin.thread_id)
    named = f"{picked.provider}:{picked.id}"
    if not await provider_move_applies(
        runtime,
        auth,
        origin,
        tool_name="set_thread_model",
        moves=moves,
        confirming=revision is not None,
    ):
        shown = await preview_thread_pick(pick, value, admin=admin)
        if isinstance(shown, SwitchRefusal):
            raise ToolError(f"{shown.line} Nothing changed.")
        assert shown.preview is not None  # a move always previews on its provider
        line = provider_move_line(
            "this conversation",
            uses_phrase(picked.provider, picked.id),
            sends="its messages",
            field="revision",
        )
        return ThreadModelResult(
            model=named,
            note=" ".join((line, f'Pass model "{named}".', *shown.lines())),
            revision=shown.preview.setting_revision,
        )
    if moves and value.strip() != named:
        # The card names the provider from the call's own model.
        raise ToolError(f'Call again with model "{named}". Nothing changed.')
    done = await pick_thread(
        pick,
        value,
        thread_id=origin.thread_id,
        admin=admin,
        expected_revision=revision if moves else None,
    )
    if isinstance(done, SwitchRefusal):
        raise ToolError(f"{done.line} Nothing changed.")
    return ThreadModelResult(model=done.model_id or "default", note=done.note)


async def _choose_claude(
    runtime: McpRuntime,
    auth: AuthIdentity,
    origin: TurnOriginRow,
    word: str | None,
    effort: str | None = None,
) -> ThreadModelResult:
    try:
        change = await choose_thread_model(
            runtime.session_factory,
            tenant_id=auth.tenant_id,
            platform=origin.platform,
            thread_id=origin.thread_id,
            choice=word,
            effort=effort,
            agent_provider=await _agent_provider(runtime, auth, origin),
        )
    except ThreadModelRefused as exc:
        raise ToolError(str(exc)) from exc
    return ThreadModelResult(
        model=change.model_id or "default", note=change.note, effort=change.effort
    )


async def _pick(runtime: McpRuntime, auth: AuthIdentity, origin: TurnOriginRow) -> ThreadPick:
    from daimon.adapters.mcp.tools.agents import (
        _capture_backend_source,  # pyright: ignore[reportPrivateUsage]
    )

    service = channel_backend.make_backend_service(
        runtime.session_factory,
        runtime.settings.turn,
        capture_source=partial(_capture_backend_source, runtime, auth, origin),
    )
    try:
        agent = await channel_backend.load_backend_agent(
            runtime.client,
            runtime.session_factory,
            tenant_id=auth.tenant_id,
            agent_name=origin.responder_name,
        )
    except channel_backend.AgentBackendRefused as exc:
        line, _action = refusal_card(exc)
        raise ToolError(f"{line} Nothing changed.") from exc
    if agent.agent_id != derive_agent_uuid(
        tenant_id=auth.tenant_id, ma_agent_id=origin.responder_ma_agent_id
    ):
        raise ToolError("This conversation's agent changed. Nothing changed.")
    actor = channel_backend.BackendActor(
        tenant_id=auth.tenant_id,
        account_id=auth.account_id,
        subject=mcp_subject(auth, is_admin=auth.admin_for(DelegatedAction.AGENT_SETTINGS)),
    )
    return ThreadPick(
        service=service,
        sessionmaker=runtime.session_factory,
        settings=runtime.settings,
        agent=agent,
        actor=actor,
        ma_agent_id=origin.responder_ma_agent_id,
        platform=origin.platform,
        platform_user_id=auth.platform_user_id,
    )


async def _catalog_model(
    runtime: McpRuntime, auth: AuthIdentity, value: str, said: str
) -> ModelRef:
    service = channel_backend.make_backend_service(
        runtime.session_factory, runtime.settings.turn, capture_source=channel_backend.NO_CAPTURE
    )
    policy = await service.tenant_policy(auth.tenant_id) if service.enabled else None
    try:
        return catalog_model(value, tenant=policy)
    except (channel_backend.AgentBackendRefused, AmbiguousModel) as exc:
        offered = ", ".join((*THREAD_MODEL_CHOICES, "default"))
        raise ToolError(
            f"{said.strip() or 'That'} isn't a model this conversation can use. "
            f"Pick one of: {offered}, or a model from list_models."
        ) from exc


def _word_of(model_id: str, *, said: str) -> str:
    """The word a Claude pick is kept by; one without a word is refused as `said`."""
    return next((w for w, m in THREAD_MODEL_CHOICES.items() if m == model_id), said)


async def _agent_provider(
    runtime: McpRuntime, auth: AuthIdentity, origin: TurnOriginRow
) -> Provider | None:
    """The provider the conversation's agent runs on when it is not Claude's, else None."""
    if not channel_backend.backend_selection_enabled(runtime.settings.turn):
        return None
    agent_id = derive_agent_uuid(tenant_id=auth.tenant_id, ma_agent_id=origin.responder_ma_agent_id)
    async with runtime.session_factory() as session:
        revision = await agent_backends.current(session, auth.tenant_id, agent_id)
    if revision is None or revision.backend in (None, DEFAULT_BACKEND):
        return None
    return revision.backend


def register_thread_model_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool
    async def set_thread_model(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        origin_context_id: str,
        model: str | None = None,
        effort: str | None = None,
        digest: str | None = None,  # Ignored: from an older confirm.
        revision: int | None = None,
    ) -> ThreadModelResult:
        """Run this conversation on another model or effort when the person asks in words: "use opus here", "think harder".

        Only on the person's direct instruction in this conversation, never on
        your own judgement or because a model is mentioned. ``model`` is
        ``haiku``, ``sonnet``, ``opus``, ``fable``, ``default`` (the agent's own
        model) or a model from ``list_models`` as ``provider:id``. ``effort`` is
        ``low``, ``medium``, ``high``, ``xhigh``, ``max`` or ``default`` (the
        agent's own), and may be given alone: "think harder" means a higher
        ``effort``, not another model. A model pick keeps the chosen effort; Claude
        models and OpenAI (low, medium, high) take one, Gemini none, and the note
        says when it was not set. The
        conversation keeps the choice for every later reply until someone
        changes it; the reply in progress keeps its model. Other conversations
        are unaffected.

        A pick within the conversation's provider applies at once. A pick on
        another provider changes nothing yet and returns a ``revision``: call
        again with the same arguments and that ``revision``, and the person
        confirms on a card that names the provider. What the new provider can't
        carry is off there; the note says what.

        ``origin_context_id`` must name this execution's attended origin;
        routines and other unattended runs are refused. Read the returned
        ``note`` back to the person.
        """  # noqa: E501
        return await _set_thread_model_impl(
            runtime,
            await _auth(ctx),
            origin_context_id=origin_context_id,
            model=model,
            effort=effort,
            revision=revision,
        )
