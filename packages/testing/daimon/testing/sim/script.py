"""What the model does in a simulated turn, said once for every provider.

Each provider fake renders a `Turn` into its own native events: Managed Agents
session events, OpenAI Agents turn items, Gemini interaction steps.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from pydantic import JsonValue


@dataclass(frozen=True)
class Tool:
    """A tool call the model makes before it answers; `server` names its MCP server."""

    name: str
    input: dict[str, JsonValue] = field(default_factory=dict[str, JsonValue])
    server: str | None = None
    result: str = "done"
    # The fake calls `server` itself, from the MCP servers the request names, and
    # reports what it answered instead of `result` (Gemini only).
    live: bool = False


@dataclass(frozen=True)
class Turn:
    """One model turn: an optional tool call, then the reply text, and its tokens."""

    text: str
    tool: Tool | None = None
    input_tokens: int = 1200
    output_tokens: int = 80


@dataclass
class Script:
    """Turns queued for the next streams, in order; an empty queue is a test bug."""

    turns: deque[Turn] = field(default_factory=deque[Turn])

    def add(self, *turns: Turn) -> None:
        self.turns.extend(turns)

    def next(self) -> Turn:
        if not self.turns:
            raise AssertionError("the model was asked for a turn the test did not script")
        return self.turns.popleft()
