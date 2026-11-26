"""The model a reply actually runs on, as every adapter's running card names it.

The agent's own record names a Claude model; a conversation or channel on another
provider runs the model of its backend revision, which a card must show instead
(`Admission.backend_revision`). One rule, so no adapter reads `agent.model.id`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from daimon.core.agent_model_names import model_name
from daimon.core.constants import MODEL_DISPLAY_NAMES
from daimon.core.thread_models import model_label
from daimon.core.turn.notices import provider_name
from mux.contracts.ids import PROVIDERS, ModelRef, Provider
from mux.profiles import MANAGED_AGENTS, backend_selection, get_profile, selectable_models

if TYPE_CHECKING:
    from daimon.core.turn.admission import Admission


def card_label(model_id: str) -> str:
    """A model id as a card names it: Claude's short name ("Opus 5.5"), another provider's
    full one ("GPT-6 Luna"), and an id nobody knows as it is."""
    others = (p for p in PROVIDERS if p != MANAGED_AGENTS.provider)
    if model_id not in MODEL_DISPLAY_NAMES and any(
        model_id in selectable_models(p) for p in others
    ):
        return model_name(ModelRef(provider=MANAGED_AGENTS.provider, id=model_id))
    return model_label(model_id)


@dataclass(frozen=True)
class ShownModel:
    id: str
    """The model id the reply runs on; the key for Claude's own token pricing."""
    label: str
    """Its name on a card: "Opus 5.5", "GPT-6 Luna"."""
    provider: Provider
    provider_label: str
    """The provider as people say it: "Anthropic", "OpenAI"."""


def shown_model(admission: Admission) -> ShownModel:
    """The model and provider this admitted message runs on, never the agent's settings."""
    revision = admission.backend_revision
    provider = (
        get_profile(revision.profile).provider if revision is not None else MANAGED_AGENTS.provider
    )
    model = revision.model if revision is not None else None
    if model is None:
        model = (
            admission.agent.model.id
            if provider == MANAGED_AGENTS.provider
            else backend_selection(provider).model
        ) or admission.agent.model.id
    return ShownModel(model, card_label(model), provider, provider_name(provider))
