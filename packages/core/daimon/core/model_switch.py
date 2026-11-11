"""The setup panels' one model switch: pick a model, see what changes, confirm once.

Discord, Slack and Teams show the same select (`read_picker`), the same preview
(`preview_model_switch`, `switch_fact_lines`) and the same write (`confirm_model_switch`,
which accepts the losses the person was shown through `change_model`). A pick on another
provider and a pick on another Claude model are the same flow; only the write differs.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from daimon.core.agent_changes import ModelChanged, change_model
from daimon.core.agent_editing import AgentActor, AgentEditDeps, AgentEditError
from daimon.core.agent_model_names import model_name
from daimon.core.authz import Subject, Surface
from daimon.core.backend_refusals import RefusalAction, refusal_card, switch_back_to_claude
from daimon.core.channel_backend import (
    AgentBackendRefused,
    AgentBackendService,
    BackendActor,
    BackendUnsupported,
    load_backend_agent,
)
from daimon.core.constants import DEFAULT_AGENT_MODEL
from daimon.core.models_catalog import ModelChoice, catalog_model, list_model_choices
from daimon.core.turn.notices import conversations_follow_line, provider_name
from mux.contracts.config import DEFAULT_BACKEND
from mux.contracts.ids import ModelRef, Provider
from mux.profiles import backend_selection

if TYPE_CHECKING:
    from anthropic import AsyncAnthropic
    from daimon.core.config import Settings
    from daimon.core.ma_resolver import ResolverCache
    from daimon.core.scope import DeploymentDefault
    from daimon.core.stores.domain import BackendAgent, CompatibilityPreview
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

#: Discord's select holds 25 options; every panel keeps the current model in them.
SELECT_LIMIT: Final = 25
_NAMES_LIMIT: Final = 500
#: The refusal card's two buttons, worded the same on every panel.
SWITCH_BACK_LABEL: Final = "Switch back to Claude"
REVIEW_AGAIN_LABEL: Final = "Review again"


@dataclass(frozen=True)
class ModelPicker:
    """What a panel's model select shows: the catalog this tenant can run, and the current one."""

    choices: tuple[ModelChoice, ...]
    current: ModelRef

    def limited(self, limit: int = SELECT_LIMIT) -> tuple[ModelChoice, ...]:
        """At most `limit` choices, the current model always among them."""
        if len(self.choices) <= limit:
            return self.choices
        head = self.choices[:limit]
        if any(choice.model == self.current for choice in head):
            return head
        current = next((choice for choice in self.choices if choice.model == self.current), None)
        return (*head[: limit - 1], current) if current is not None else head


@dataclass(frozen=True)
class ModelSwitchPreview:
    """One pick, previewed: what the person confirms."""

    model: ModelRef
    label: str
    #: The provider the agent runs on now.
    before: str
    #: None when provider choice is off here and the pick only edits the agent's record.
    preview: CompatibilityPreview | None

    @property
    def digest(self) -> str:
        return self.preview.digest if self.preview is not None else ""

    @property
    def revision(self) -> int:
        return self.preview.setting_revision if self.preview is not None else 0

    @property
    def allowed(self) -> bool:
        return self.preview is None or self.preview.allowed


def panel_edit_deps(
    settings: Settings,
    *,
    client: AsyncAnthropic,
    session_factory: async_sessionmaker[AsyncSession],
    resolver_cache: ResolverCache,
    deployment_default: DeploymentDefault,
) -> AgentEditDeps:
    """The edit dependencies a chat app's panel writes a model with."""
    public_url = settings.mcp.public_url
    return AgentEditDeps(
        client=client,
        session_factory=session_factory,
        public_url=str(public_url) if public_url is not None else None,
        denied_tools=settings.tool_safety.denied,
        resolver_cache=resolver_cache,
        deployment_default=deployment_default,
    )


def panel_actor(*, tenant_id: uuid.UUID, account_id: uuid.UUID, subject: Subject) -> AgentActor:
    """The actor a panel's model write runs as."""
    return AgentActor(
        tenant_id=tenant_id, account_id=account_id, subject=subject, surface=Surface.CONFIG
    )


def current_model(agent: BackendAgent, backend: Provider | None, model: str | None) -> ModelRef:
    """The model an agent runs on, from its backend revision's provider and model."""
    if backend in (None, DEFAULT_BACKEND):
        return ModelRef(provider=DEFAULT_BACKEND, id=agent.model or DEFAULT_AGENT_MODEL)
    picked = model or backend_selection(backend).model
    assert picked is not None, "a non-default provider always names a model"
    return ModelRef(provider=backend, id=picked)


async def read_picker(
    service: AgentBackendService, edit: AgentEditDeps, *, tenant_id: uuid.UUID, agent_name: str
) -> ModelPicker | None:
    """The select for `agent_name`, or None when the agent can't be read."""
    try:
        agent = await load_backend_agent(
            edit.client, edit.session_factory, tenant_id=tenant_id, agent_name=agent_name
        )
    except AgentBackendRefused:
        return None
    revision = await service.current(agent)
    current = current_model(
        agent,
        revision.backend if revision is not None else None,
        revision.model if revision is not None else None,
    )
    policy = await service.tenant_policy(tenant_id) if service.enabled else None
    return ModelPicker(choices=list_model_choices(tenant=policy, default=current), current=current)


async def preview_model_switch(
    service: AgentBackendService,
    edit: AgentEditDeps,
    *,
    actor: AgentActor,
    agent_name: str,
    value: str,
) -> ModelSwitchPreview:
    """What picking `value` (`provider:id`) would change. Refused as a write would be."""
    agent, backend = await _load(service, edit, actor, agent_name)
    model = await _resolve(service, agent, value)
    if service.enabled and not await service.may_edit(agent, backend):
        raise AgentBackendRefused("owner_or_admin_required", provider=model.provider)
    revision = await service.current(agent)
    before = revision.backend if revision is not None and revision.backend else DEFAULT_BACKEND
    claude = model.provider == DEFAULT_BACKEND
    preview = None
    if service.enabled and not (claude and before == DEFAULT_BACKEND):
        preview = await service.preview(
            agent, None if claude else model.provider, None if claude else model.id
        )
    return ModelSwitchPreview(model=model, label=model_name(model), before=before, preview=preview)


async def confirm_model_switch(
    service: AgentBackendService,
    edit: AgentEditDeps,
    *,
    actor: AgentActor,
    agent_name: str,
    value: str,
    digest: str,
    revision: int,
) -> ModelChanged:
    """Write the pick the person confirmed, with the losses they were shown accepted.

    The preview is read again; if it no longer matches the shown digest or revision the
    write is refused as `stale_preview` and nothing changes.
    """
    agent, _backend = await _load(service, edit, actor, agent_name)
    model = await _resolve(service, agent, value)
    shown = await preview_model_switch(
        service, edit, actor=actor, agent_name=agent_name, value=value
    )
    if shown.digest != digest or shown.revision != revision:
        raise AgentBackendRefused("stale_preview", provider=model.provider)
    return await change_model(
        agent,
        model,
        actor=actor,
        edit=edit,
        backends=service,
        accepted_drops=shown.preview.declared_drops if shown.preview is not None else None,
        expected_digest=shown.digest or None,
        expected_revision=shown.revision if shown.preview is not None else None,
    )


@dataclass(frozen=True)
class SwitchRefusal:
    """A pick that wasn't applied: the line to show and the one action to offer."""

    line: str
    action: RefusalAction | None
    reason: str


def refusal_of(
    error: AgentBackendRefused | BackendUnsupported | AgentEditError, *, admin: bool
) -> SwitchRefusal:
    """Words for a refused pick, from the one table every surface uses."""
    if isinstance(error, AgentEditError):
        return SwitchRefusal(str(error), None, error.kind)
    line, action = refusal_card(error, admin=admin)
    return SwitchRefusal(
        line, action, error.reason if isinstance(error, AgentBackendRefused) else "unsupported"
    )


async def confirm_or_refusal(
    service: AgentBackendService,
    edit: AgentEditDeps,
    *,
    actor: AgentActor,
    agent_name: str,
    value: str,
    digest: str,
    revision: int,
    admin: bool,
) -> ModelChanged | SwitchRefusal:
    """`confirm_model_switch`, with a refusal returned as the words a panel shows."""
    try:
        return await confirm_model_switch(
            service,
            edit,
            actor=actor,
            agent_name=agent_name,
            value=value,
            digest=digest,
            revision=revision,
        )
    except (AgentBackendRefused, BackendUnsupported, AgentEditError) as error:
        return refusal_of(error, admin=admin)


async def preview_or_refusal(
    service: AgentBackendService,
    edit: AgentEditDeps,
    *,
    actor: AgentActor,
    agent_name: str,
    value: str,
    admin: bool,
) -> ModelSwitchPreview | SwitchRefusal:
    """`preview_model_switch`, with a refusal returned as the words a panel shows."""
    try:
        return await preview_model_switch(
            service, edit, actor=actor, agent_name=agent_name, value=value
        )
    except (AgentBackendRefused, BackendUnsupported, AgentEditError) as error:
        return refusal_of(error, admin=admin)


async def back_to_claude(
    service: AgentBackendService,
    edit: AgentEditDeps,
    *,
    actor: AgentActor,
    agent_name: str,
    platform: str,
    channel_id: str,
    admin: bool,
) -> str | SwitchRefusal:
    """The "Switch back to Claude" button: the line to show, or why it was refused."""
    try:
        agent, backend = await _load(service, edit, actor, agent_name)
        return await switch_back_to_claude(
            edit.session_factory,
            agent,
            tenant_id=actor.tenant_id,
            platform=platform,
            channel_id=channel_id,
            actor=actor,
            backend_actor=backend,
            edit=edit,
            backends=service,
        )
    except (AgentBackendRefused, BackendUnsupported, AgentEditError) as error:
        return refusal_of(error, admin=admin)


async def _load(
    service: AgentBackendService, edit: AgentEditDeps, actor: AgentActor, agent_name: str
) -> tuple[BackendAgent, BackendActor]:
    agent = await load_backend_agent(
        edit.client, edit.session_factory, tenant_id=actor.tenant_id, agent_name=agent_name
    )
    return agent, BackendActor(
        tenant_id=actor.tenant_id, account_id=actor.account_id, subject=actor.subject
    )


async def _resolve(service: AgentBackendService, agent: BackendAgent, value: str) -> ModelRef:
    policy = await service.tenant_policy(agent.tenant_id) if service.enabled else None
    return catalog_model(value, tenant=policy)


def switch_fact_lines(
    switch: ModelSwitchPreview,
    *,
    agent_name: str | None,
    escape: Callable[[str], str] = str,
    applies_to: str | None = None,
) -> tuple[str, ...]:
    """The preview as lines every panel shows; `escape` makes names safe for its markup.

    A pick on the same provider has no losses to list, so it shows its price only.
    `applies_to` replaces the line on which conversations follow, for a narrower pick.
    """
    lines = [f"Model: {escape(switch.label)}"]
    preview = switch.preview
    if preview is None:
        return tuple(lines)
    if switch.model.provider == switch.before:
        lines.append(escape(preview.cost_line))
        return tuple(lines)
    for label, items in (
        ("Unsupported skills", preview.unsupported_skills),
        ("Unsupported connections", preview.unsupported_mcps),
        ("Unsupported capabilities", preview.unsupported_capabilities),
        ("Shared-thread limits", preview.shared_thread_limits),
    ):
        names = ", ".join(escape(item) for item in items) or "None"
        shown = names if len(names) <= _NAMES_LIMIT else names[:_NAMES_LIMIT] + "… (truncated)"
        lines.append(f"{label}: {shown}")
    lines.append(escape(preview.cost_line))
    lines.append(
        f"Applies to: {applies_to}"
        if applies_to is not None
        else conversations_follow_line(agent_name, switch.model.provider, before=switch.before)
        + " Channel settings still apply."
    )
    if not preview.allowed:
        lines.append(f"{provider_name(switch.model.provider)} is off here. Pick another.")
    return tuple(lines)
