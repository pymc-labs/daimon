"""A model pick, previewed for the agent editor: the switch card when it asks first."""

from __future__ import annotations

from daimon.core.agent_changes import SwitchConsent
from daimon.core.model_switch import ModelSwitchPreview, SwitchRefusal, switch_card_text
from mux.contracts.config import DEFAULT_BACKEND
from pydantic import BaseModel


class PreviewRefusal(BaseModel):
    """Why a pick can't go ahead, and the one thing to offer."""

    line: str
    action: str | None


class ModelPreview(BaseModel):
    """What a pick changes. `moves` is false when it only edits the agent's own record;
    `card` is true when the pick asks first, with the card's `title` and `lines`."""

    moves: bool
    card: bool
    allowed: bool
    skills: list[str]
    servers: list[str]
    capabilities: list[str]
    title: str
    #: The lines under the title, as every switch card says them.
    lines: list[str]
    #: Every drop the person accepts by confirming, as the apply must send them back.
    drops: list[str]
    digest: str
    revision: int
    refusal: PreviewRefusal | None = None


def preview_doc(
    switch: ModelSwitchPreview, *, agent_name: str | None, card: bool | None = None
) -> ModelPreview:
    """The preview as the editor shows it; `card` defaults to the switch's own rule. Pure."""
    found = switch.preview
    title, *lines = switch_card_text(switch, agent_name=agent_name)
    if found is None:
        return ModelPreview(
            moves=False,
            card=False,
            allowed=True,
            skills=[],
            servers=[],
            capabilities=[],
            title=title,
            lines=[],
            drops=[],
            digest="",
            revision=0,
        )
    return ModelPreview(
        moves=not switch.model.provider == switch.before == DEFAULT_BACKEND,
        card=switch.asks_first if card is None else card,
        allowed=found.allowed,
        skills=list(found.unsupported_skills),
        servers=list(found.unsupported_mcps),
        capabilities=list(found.unsupported_capabilities),
        title=title,
        lines=lines,
        drops=list(found.declared_drops),
        digest=found.digest,
        revision=found.setting_revision,
    )


def refused_doc(refusal: SwitchRefusal) -> ModelPreview:
    """A pick that can't go ahead: nothing to confirm, the way out to offer. Pure."""
    return ModelPreview(
        moves=True,
        card=True,
        allowed=False,
        skills=[],
        servers=[],
        capabilities=[],
        title="",
        lines=[],
        drops=[],
        digest="",
        revision=0,
        refusal=PreviewRefusal(line=refusal.line, action=refusal.action),
    )


def consent_of(
    drops: list[str] | None, digest: str | None, revision: int | None
) -> SwitchConsent | None:
    """The consent an apply carries, or None when the pick showed no preview. Pure."""
    if digest is None or revision is None:
        return None
    return SwitchConsent(accepted_drops=tuple(drops or ()), digest=digest, revision=revision)
