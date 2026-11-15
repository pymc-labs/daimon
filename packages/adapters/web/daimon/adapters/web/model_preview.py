"""A model pick, previewed for the agent editor: what the person confirms."""

from __future__ import annotations

from daimon.core.agent_changes import SwitchConsent
from daimon.core.model_switch import ModelSwitchPreview, SwitchRefusal
from daimon.core.turn.notices import conversations_follow_line
from mux.contracts.config import DEFAULT_BACKEND
from pydantic import BaseModel


class PreviewRefusal(BaseModel):
    """Why a pick can't go ahead, and the one thing to offer."""

    line: str
    action: str | None


class ModelPreview(BaseModel):
    """What a pick changes. `moves` is false when it only edits the agent's own record."""

    moves: bool
    allowed: bool
    skills: list[str]
    servers: list[str]
    capabilities: list[str]
    shared_limits: list[str]
    #: Every drop the person accepts by confirming, as the apply must send them back.
    drops: list[str]
    cost: str
    threads: str
    digest: str
    revision: int
    refusal: PreviewRefusal | None = None


def preview_doc(switch: ModelSwitchPreview, *, agent_name: str) -> ModelPreview:
    """The preview as the editor shows it. Pure."""
    found = switch.preview
    if found is None:
        return ModelPreview(
            moves=False,
            allowed=True,
            skills=[],
            servers=[],
            capabilities=[],
            shared_limits=[],
            drops=[],
            cost="",
            threads="",
            digest="",
            revision=0,
        )
    stays = switch.model.provider == switch.before == DEFAULT_BACKEND
    return ModelPreview(
        moves=not stays,
        allowed=found.allowed,
        skills=list(found.unsupported_skills),
        servers=list(found.unsupported_mcps),
        capabilities=list(found.unsupported_capabilities),
        shared_limits=list(found.shared_thread_limits),
        drops=list(found.declared_drops),
        cost=found.cost_line,
        threads=""
        if stays
        else conversations_follow_line(agent_name, switch.model.provider, before=switch.before),
        digest=found.digest,
        revision=found.setting_revision,
    )


def refused_doc(refusal: SwitchRefusal) -> ModelPreview:
    """A pick that can't go ahead: nothing to confirm, the way out to offer. Pure."""
    return ModelPreview(
        moves=True,
        allowed=False,
        skills=[],
        servers=[],
        capabilities=[],
        shared_limits=[],
        drops=[],
        cost="",
        threads="",
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
