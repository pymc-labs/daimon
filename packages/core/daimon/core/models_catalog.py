"""The model catalog surfaced to users picking an agent's model.

One source for every surface: `agent_catalog()` gives the priced, runnable
models, a tenant's provider allowlist and `check_backend` narrow it, and
`model_name()` names each entry. Claude's priced ids (fast presets included)
come from here too, so nothing else reads the raw id table.
"""

from __future__ import annotations

from daimon.core.agent_model_names import (
    AgentModel,
    AmbiguousModel,
    agent_catalog,
    model_name,
    resolve_model,
)
from daimon.core.channel_backend import (
    AgentBackendRefused,
    BackendUnsupported,
    agent_selection,
    backend_is_enabled,
    check_backend,
)
from daimon.core.constants import MODEL_DISPLAY_NAMES
from daimon.core.pricing import AGENT_MODEL_PRICING
from daimon.core.stores.domain import TenantBackendPolicy
from mux.contracts.config import DEFAULT_BACKEND, ConfigRevision
from mux.contracts.ids import ChannelRef, ModelRef, Provider
from pydantic import BaseModel, ConfigDict

_DEFAULT_DESCRIPTION = "the default"


class ModelChoice(BaseModel):
    """One selectable model, as presented to a user."""

    model_config = ConfigDict(frozen=True)

    id: str
    label: str
    description: str | None
    is_default: bool
    provider: Provider = DEFAULT_BACKEND
    price: str | None = None

    @property
    def model(self) -> ModelRef:
        return ModelRef(provider=self.provider, id=self.id)

    @property
    def value(self) -> str:
        """`provider:id`, the form a select option or CLI flag carries."""
        return f"{self.provider}:{self.id}"


def priced_claude_ids() -> tuple[str, ...]:
    """Every Claude id an agent record may name, fast presets included."""
    return tuple(AGENT_MODEL_PRICING)


def is_priced_claude_model(model_id: str) -> bool:
    return model_id in AGENT_MODEL_PRICING


def claude_model_choices(*, default: str) -> tuple[ModelChoice, ...]:
    """Every Claude model an agent record may name, `default` flagged.

    Raises `ValueError` if `default` is not a priced Claude id.
    """
    if not is_priced_claude_model(default):
        raise ValueError(f"default model {default!r} is not one of {priced_claude_ids()}")
    return tuple(
        ModelChoice(
            id=model_id,
            label=MODEL_DISPLAY_NAMES.get(model_id, model_id),
            description=_DEFAULT_DESCRIPTION if model_id == default else None,
            is_default=model_id == default,
        )
        for model_id in priced_claude_ids()
    )


def _runnable(entry: AgentModel, tenant: TenantBackendPolicy | None) -> bool:
    provider = entry.model.provider
    if provider == DEFAULT_BACKEND:
        return True
    if tenant is None or not backend_is_enabled(provider, tenant.enabled_backends):
        return False
    channel = ChannelRef(tenant_id="catalog", platform="cli", channel_id="catalog")
    try:
        check_backend(
            ConfigRevision.create(channel, 1, agent_selection(provider, entry.model.id, None))
        )
    except (BackendUnsupported, AgentBackendRefused):
        return False
    return True


def list_model_choices(
    *, tenant: TenantBackendPolicy | None, default: ModelRef | None
) -> tuple[ModelChoice, ...]:
    """Every model this tenant's agents can run now, `default` flagged.

    `tenant` None means provider choice is off here: Claude only. Unpriced,
    expired and unrunnable models are absent.
    """
    # The turn package imports thread_models, which imports this module.
    from daimon.core.turn.notices import provider_name

    entries = agent_catalog()
    if tenant is not None:
        listed = {(e.model.provider, e.model.id) for e in entries}
        entries += _granted_entries(str(tenant.tenant_id), listed)
    return tuple(
        ModelChoice(
            id=entry.model.id,
            label=model_name(entry.model),
            description=f"{provider_name(entry.model.provider)}, {entry.price}",
            is_default=entry.model == default,
            provider=entry.model.provider,
            price=entry.price,
        )
        for entry in entries
        if _runnable(entry, tenant)
    )


def _granted_entries(tenant_id: str, listed: set[tuple[str, str]]) -> tuple[AgentModel, ...]:
    """Models an operator grants this tenant's agents, beyond the public catalog."""
    from daimon.core.turn.runtimes import agent_granted_prices

    return tuple(
        AgentModel(
            ModelRef.model_validate({"provider": rate.provider, "id": rate.model}),
            f"~${rate.input} input / ${rate.output} output per million tokens",
            float(rate.input),
            float(rate.output),
        )
        for rate in agent_granted_prices(tenant_id).values()
        if (rate.provider, rate.model) not in listed
    )


def catalog_model(value: str, *, tenant: TenantBackendPolicy | None) -> ModelRef:
    """The catalog model `value` names (`provider:id`, an id or a name).

    Raises `AgentBackendRefused("model_not_selectable")` for anything else, or
    `AmbiguousModel` when a name matches several.
    """
    available = [choice.model for choice in list_model_choices(tenant=tenant, default=None)]
    provider, sep, model_id = value.strip().partition(":")
    if sep:
        ref = next((m for m in available if (m.provider, m.id) == (provider, model_id)), None)
        if ref is None:
            raise AgentBackendRefused("model_not_selectable")
        return ref
    try:
        return resolve_model(value, available=available)
    except AmbiguousModel:
        raise
    except ValueError as err:
        raise AgentBackendRefused("model_not_selectable") from err
