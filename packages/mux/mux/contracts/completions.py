"""One bounded text completion outside any session: titles, verdicts, short summaries."""

from __future__ import annotations

from typing import Literal, Protocol

from mux.contracts._base import Contract
from mux.contracts.ids import Scope

type CompletionEffort = Literal["none", "low", "medium", "high"]


class CompletionUsage(Contract):
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int = 0


class CompletionResult(Contract):
    text: str
    usage: CompletionUsage
    refused: bool = False
    """The provider declined to answer; `text` is empty and the usage is still billable."""


class Completions(Protocol):
    async def complete(
        self,
        scope: Scope,
        model: str,
        system: str,
        prompt: str,
        max_tokens: int,
        effort: CompletionEffort | None = None,
    ) -> CompletionResult: ...


__all__ = ["CompletionEffort", "CompletionResult", "CompletionUsage", "Completions"]
