"""Normalized turn-event schema.

Anthropic streams fine-grained SSE events, OpenAI reports turns and items,
Google reports chained interactions. Mux normalizes to one schema and accepts
the loss of backend-specific ordering detail; backends needing native detail
use their own SDK directly, outside mux.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

MuxEventKind = Literal["text", "tool_use", "tool_result", "done", "error"]


class MuxEvent(BaseModel):
    """One normalized event from any backend. Frozen: history is append-only."""

    model_config = ConfigDict(frozen=True)

    backend: str
    kind: MuxEventKind
    name: str = ""
    text: str = ""
