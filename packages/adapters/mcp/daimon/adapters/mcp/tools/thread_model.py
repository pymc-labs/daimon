"""Switch this conversation's model when someone asks in words ("use opus", "think harder").

``register_thread_model_tools(mcp, runtime)`` wires the ``@mcp.tool`` closure;
it delegates to ``_set_thread_model_impl`` so the gate is testable without a
FastMCP Context. ``/model`` on Discord writes the same setting
(`daimon.core.thread_models`).

Only a person's own turn may switch it: the call must name this execution's
verified origin, and that origin must be attended. Routines, wakes and
agent-chat callers have no attended origin, so they can never change the model
a conversation's people chose.

A pick that needs consent (`daimon.core.thread_model_switch`) is two calls: the
first returns what changes, the second, after the person says yes, keeps it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import partial

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._authz_facts import mcp_subject
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools.setup_target import refuse_helper, require_turn_origin
from daimon.core import channel_backend
from daimon.core.admin_actions import DelegatedAction
from daimon.core.agent_model_names import AmbiguousModel
from daimon.core.backend_refusals import refusal_card
from daimon.core.features.snapshot import deployment_on
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.model_switch import switch_fact_lines
from daimon.core.models_catalog import catalog_model
from daimon.core.stores import agent_backends
from daimon.core.stores.domain import TurnOriginRow
from daimon.core.thread_model_switch import (
    APPLIES_TO,
    ThreadPick,
    confirm_thread_model,
    preview_thread_model,
)
from daimon.core.thread_models import THREAD_MODEL_CHOICES, ThreadModelRefused, choose_thread_model
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from mux.contracts.config import DEFAULT_BACKEND
from mux.contracts.ids import ModelRef

_NO_CARD = (
    "Moving this conversation to another provider needs an approval card, and none can "
    "be shown here. Nothing changed."
)
_UNATTENDED = (
    "Only someone in the conversation can change its model, and nobody started "
    "this run. Nothing was changed."
)


@dataclass(frozen=True)
class ThreadModelResult:
    """Result returned from set_thread_model."""

    model: str
    """The model now chosen, 'default' for the agent's own; the pick when not yet kept."""
    note: str
    """The sentence to report back to the person."""
    changes: list[str] = field(default_factory=list[str])
    """What the pick changes, when it waits for the person's yes; read these out."""
    digest: str | None = None
    """Pass back, with `revision`, to keep the pick once the person agrees."""
    revision: int | None = None


async def _set_thread_model_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    origin_context_id: str,
    model: str,
    digest: str | None = None,
    revision: int | None = None,
) -> ThreadModelResult:
    origin = refuse_helper(await require_turn_origin(runtime, auth, origin_context_id))
    if not origin.attended:
        raise ToolError(_UNATTENDED)
    word = model.strip().lower()
    if word == "default":
        return await _choose_claude(runtime, auth, origin, word)
    value = (
        f"{DEFAULT_BACKEND}:{THREAD_MODEL_CHOICES[word]}" if word in THREAD_MODEL_CHOICES else model
    )
    picked = await _catalog_model(runtime, auth, value, model)
    if picked.provider == DEFAULT_BACKEND and not await _off_claude(runtime, auth, origin):
        return await _choose_claude(runtime, auth, origin, _word_of(picked.id, said=model))
    pick = await _pick(runtime, auth, origin)
    # With the host call gate off nothing cards the confirm, so only same-provider picks run.
    uncarded = auth.no_confirmation_surface and not deployment_on(
        runtime.settings, "host_call_gate", origin.platform
    )
    try:
        if digest is None or revision is None or uncarded:
            shown = await preview_thread_model(pick, value)
            if uncarded and shown.model.provider != shown.before:
                raise ToolError(_NO_CARD)
            if shown.preview is None:
                # The agent moved to Claude meanwhile: nothing to consent to.
                word = _word_of(shown.model.id, said=model)
                return await _choose_claude(runtime, auth, origin, word)
            if digest is None or revision is None:
                lines = switch_fact_lines(shown, agent_name=None, applies_to=APPLIES_TO)
                return ThreadModelResult(
                    model=f"{shown.model.provider}:{shown.model.id}",
                    note="Read these changes to the person and ask whether to go ahead.",
                    changes=list(lines),
                    digest=shown.digest,
                    revision=shown.revision,
                )
        change = await confirm_thread_model(
            pick, value, thread_id=origin.thread_id, digest=digest, revision=revision
        )
    except (channel_backend.AgentBackendRefused, channel_backend.BackendUnsupported) as exc:
        line, _action = refusal_card(exc, admin=auth.admin_for(DelegatedAction.AGENT_SETTINGS))
        raise ToolError(f"{line} Nothing changed.") from exc
    return ThreadModelResult(model=change.model_id or "default", note=change.note)


async def _choose_claude(
    runtime: McpRuntime, auth: AuthIdentity, origin: TurnOriginRow, word: str
) -> ThreadModelResult:
    try:
        change = await choose_thread_model(
            runtime.session_factory,
            tenant_id=auth.tenant_id,
            platform=origin.platform,
            thread_id=origin.thread_id,
            choice=word,
        )
    except ThreadModelRefused as exc:
        raise ToolError(str(exc)) from exc
    return ThreadModelResult(model=change.model_id or "default", note=change.note)


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


async def _off_claude(runtime: McpRuntime, auth: AuthIdentity, origin: TurnOriginRow) -> bool:
    """Whether the conversation's agent runs on another provider, so a Claude word needs consent."""
    if not channel_backend.backend_selection_enabled(runtime.settings.turn):
        return False
    agent_id = derive_agent_uuid(tenant_id=auth.tenant_id, ma_agent_id=origin.responder_ma_agent_id)
    async with runtime.session_factory() as session:
        revision = await agent_backends.current(session, auth.tenant_id, agent_id)
    return revision is not None and revision.backend not in (None, DEFAULT_BACKEND)


def register_thread_model_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool
    async def set_thread_model(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        origin_context_id: str,
        model: str,
        digest: str | None = None,
        revision: int | None = None,
    ) -> ThreadModelResult:
        """Run this conversation on another model when the person asks in words: "use opus here", "use GPT-5 here".

        Only on the person's direct instruction in this conversation, never on
        your own judgement or because a model is mentioned. ``model`` is
        ``haiku``, ``sonnet``, ``opus``, ``default`` (the agent's own model) or
        a model from ``list_models`` as ``provider:id``. "Think harder" means
        ``opus``. There is no effort setting. The conversation keeps the choice
        for every later reply until someone changes it; the reply in progress
        keeps its model. Other conversations are unaffected.

        A model on another provider returns ``changes`` and does nothing yet:
        read them to the person, and only after they agree in a later message
        call again with the same ``model``, ``digest`` and ``revision``.

        ``origin_context_id`` must name this execution's attended origin;
        routines and other unattended runs are refused. Read the returned
        ``note`` back to the person.
        """  # noqa: E501
        return await _set_thread_model_impl(
            runtime,
            await _auth(ctx),
            origin_context_id=origin_context_id,
            model=model,
            digest=digest,
            revision=revision,
        )
