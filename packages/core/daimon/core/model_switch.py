"""The setup panels' one model switch: pick a model and it applies.

Discord, Slack and Teams show the same select (`read_picker`) and the same pick
(`pick_or_refusal`), which writes through `change_model` like any model change. What the
new provider can't carry is off there and said once as information (`ModelChanged.lines`).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Final

from daimon.core.agent_catalog import provider_efforts
from daimon.core.agent_changes import ModelChanged, change_model
from daimon.core.agent_editing import AgentActor, AgentEditDeps, AgentEditError
from daimon.core.agent_model_names import model_name
from daimon.core.authz import Subject, Surface
from daimon.core.backend_refusals import RefusalAction, refusal_card, switch_back_to_claude
from daimon.core.channel_backend import (
    AgentBackendRefused,
    AgentBackendService,
    AgentSettings,
    BackendActor,
    BackendUnsupported,
    load_backend_agent,
)
from daimon.core.constants import DEFAULT_AGENT_MODEL
from daimon.core.models_catalog import ModelChoice, catalog_model, list_model_choices
from daimon.core.switch_card import switch_lines
from mux.contracts.config import DEFAULT_BACKEND
from mux.contracts.ids import ModelRef, Provider
from mux.contracts.profile import Effort
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
#: The refusal card's buttons, worded the same on every panel.
CANCEL_LABEL: Final = "Cancel"
#: The button under what a pick turned off: back to the agent.
BACK_LABEL: Final = "Back"
SWITCH_BACK_LABEL: Final = "Switch back to Claude"
TRY_AGAIN_LABEL: Final = "Try again"


@dataclass(frozen=True)
class ModelPicker:
    """What a panel's model select shows: the catalog this tenant can run, and the current one."""

    choices: tuple[ModelChoice, ...]
    current: ModelRef
    #: The effort levels the current provider takes, empty on Claude (its own setting).
    efforts: tuple[Effort, ...] = ()
    #: The effort the agent states now, None for the provider's default.
    effort: Effort | None = None

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
    """One pick, previewed: what would be off on the new provider."""

    model: ModelRef
    label: str
    #: The provider the agent runs on now.
    before: str
    #: None when provider choice is off here and the pick only edits the agent's record.
    preview: CompatibilityPreview | None

    @property
    def allowed(self) -> bool:
        return self.preview is None or self.preview.allowed

    def lines(self, *, escape: Callable[[str], str] = str) -> tuple[str, ...]:
        """What is off on the new provider; `escape` makes names safe for a surface's markup."""
        return switch_lines(self.preview, escape=escape) if self.preview is not None else ()


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
    return ModelPicker(
        choices=list_model_choices(tenant=policy, default=current, agent_id=agent.agent_id),
        current=current,
        efforts=provider_efforts(current.provider) if service.enabled else (),
        effort=revision.effort if revision is not None else None,
    )


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
    return await _preview(service, agent, backend, await _resolve(service, agent, value))


async def _preview(
    service: AgentBackendService, agent: BackendAgent, backend: BackendActor, model: ModelRef
) -> ModelSwitchPreview:
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


async def preview_or_refusal(
    service: AgentBackendService,
    edit: AgentEditDeps,
    *,
    actor: AgentActor,
    agent_name: str,
    value: str,
    admin: bool,
) -> ModelSwitchPreview | SwitchRefusal:
    """`preview_model_switch`, with a refusal returned as the words a surface shows."""
    try:
        return await preview_model_switch(
            service, edit, actor=actor, agent_name=agent_name, value=value
        )
    except (AgentBackendRefused, BackendUnsupported, AgentEditError) as error:
        return refusal_of(error, admin=admin)


async def pick_or_refusal(
    service: AgentBackendService,
    edit: AgentEditDeps,
    *,
    actor: AgentActor,
    agent_name: str,
    value: str,
    admin: bool,
    escape: Callable[[str], str] = str,
) -> ModelChanged | SwitchRefusal:
    """A panel's pick: applied at once, like any model change, or refused with why.

    Every panel calls this. The result's `lines` say what is off on the new provider;
    `escape` makes names in them safe for the panel's markup.
    """
    try:
        agent, backend = await _load(service, edit, actor, agent_name)
        shown = await _preview(service, agent, backend, await _resolve(service, agent, value))
        changed = await change_model(agent, shown.model, actor=actor, edit=edit, backends=service)
        return replace(changed, lines=shown.lines(escape=escape))
    except (AgentBackendRefused, BackendUnsupported, AgentEditError) as error:
        return refusal_of(error, admin=admin)


async def set_effort(
    service: AgentBackendService,
    edit: AgentEditDeps,
    *,
    actor: AgentActor,
    agent_name: str,
    effort: str,
    admin: bool,
) -> Effort | SwitchRefusal:
    """The effort select's one write: state `effort` on the agent's provider, or say why not.

    The provider and model stay; the fresh preview's revision guards the write.
    """
    try:
        agent, backend = await _load(service, edit, actor, agent_name)
        if not await service.may_edit(agent, backend):
            raise AgentBackendRefused("owner_or_admin_required")
        revision = await service.current(agent)
        provider = revision.backend if revision is not None else None
        offered: dict[str, Effort] = {e: e for e in provider_efforts(provider or DEFAULT_BACKEND)}
        level = offered.get(effort)
        if level is None:
            raise AgentEditError("That effort isn't available for this model.", kind="invalid")
        settings = replace(AgentSettings.of(revision), effort=level)
        preview = await service.preview_settings(agent, settings)
        if not preview.allowed:
            raise AgentBackendRefused("backend_not_allowed", provider=provider)
        await service.set_settings(agent, settings, preview.setting_revision, backend)
    except (AgentBackendRefused, BackendUnsupported, AgentEditError) as error:
        return refusal_of(error, admin=admin)
    return level


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
    return catalog_model(value, tenant=policy, agent_id=agent.agent_id)
