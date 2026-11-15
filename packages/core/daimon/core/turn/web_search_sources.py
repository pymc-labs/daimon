"""The sources rule for a turn with web search on, the same on every provider.

Built-in search takes no custom description on Claude, OpenAI or Gemini, so the
rule rides in a short block in front of this turn's user message, like
`progress_plans`. The system prompt and tools stay the same, so a prompt cache
survives a mid-thread toggle. A turn without search gets no block.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from daimon.core.features.model_input import SessionToolset, builtin_enabled
from mux.contracts.resources import AgentSpec

GUIDANCE = "After you use web search, end your reply with the sources you used, as links."

# The search tool's name in a source spec and in each provider's own.
_NATIVE_NAMES = frozenset({"web_search", "google_search"})


def sources_block() -> str:
    """The block sent in front of the user message on a turn with web search on."""
    return f'<web_search state="on">\n{GUIDANCE}\n</web_search>\n\n'


def sources_prefix(*, on: bool) -> str:
    """The block when web search is on for this turn, else ``""``."""
    return sources_block() if on else ""


def claude_searches(tools: Sequence[Any], toolset: SessionToolset | None = None) -> bool:
    """Whether a Claude agent's `tools`, with the place's overrides applied, offer web search."""
    dumped = [cast(dict[str, Any], tool) for tool in tools]
    if toolset is not None:
        dumped = toolset.apply_builtins(dumped)
    return builtin_enabled(cast(Any, dumped), "web_search") is True


def native_searches(spec: AgentSpec | None) -> bool:
    """Whether an OpenAI or Gemini agent spec offers search without asking first.

    A search that must ask first is a named loss of the switch, so it is not on.
    """
    return spec is not None and any(
        tool.kind == "builtin" and tool.name in _NATIVE_NAMES and tool.permission == "auto"
        for tool in spec.tools or ()
    )
