"""A model pick, previewed for the agent editor and the chat: what is off on the new provider."""

from __future__ import annotations

from daimon.core.model_switch import ModelSwitchPreview, SwitchRefusal
from mux.contracts.config import DEFAULT_BACKEND
from pydantic import BaseModel


class PreviewRefusal(BaseModel):
    """Why a pick can't go ahead, and the one thing to offer."""

    line: str
    action: str | None


class ModelPreview(BaseModel):
    """What a pick changes. `moves` is false when it only edits the agent's own record;
    `lines` say what is off on the new provider, as information."""

    moves: bool
    allowed: bool
    skills: list[str]
    servers: list[str]
    capabilities: list[str]
    #: What is off on the new provider, as every surface says it.
    lines: list[str]
    #: The parts that are off there, by kind.
    drops: list[str]
    refusal: PreviewRefusal | None = None


def preview_doc(switch: ModelSwitchPreview) -> ModelPreview:
    """The preview as the editor and the chat show it. Pure."""
    found = switch.preview
    if found is None:
        return ModelPreview(
            moves=False, allowed=True, skills=[], servers=[], capabilities=[], lines=[], drops=[]
        )
    return ModelPreview(
        moves=not switch.model.provider == switch.before == DEFAULT_BACKEND,
        allowed=found.allowed,
        skills=list(found.unsupported_skills),
        servers=list(found.unsupported_mcps),
        capabilities=list(found.unsupported_capabilities),
        lines=list(switch.lines()),
        drops=list(found.declared_drops),
    )


def refused_doc(refusal: SwitchRefusal) -> ModelPreview:
    """A pick that can't go ahead, and the way out to offer. Pure."""
    return ModelPreview(
        moves=True,
        allowed=False,
        skills=[],
        servers=[],
        capabilities=[],
        lines=[],
        drops=[],
        refusal=PreviewRefusal(line=refusal.line, action=refusal.action),
    )
