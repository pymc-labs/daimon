"""Constants for daimon-core."""

from __future__ import annotations

from daimon.core.pricing import AGENT_MODEL_PRICING

# The Anthropic models the /agent-setup panel allows. Single source of truth:
# `pricing.AGENT_MODEL_PRICING.keys()` — when a model is added or repriced, both
# surfaces update in lockstep. Tool-pinned models (`pricing.TOOL_MODEL_PRICING`)
# are metered but never selectable here.
#
# UX-25-03: the Model TextInput is free-text because Discord modals cannot
# contain Select components, so validation happens at submit time against this
# tuple.
ALLOWED_MODEL_IDS: tuple[str, ...] = tuple(AGENT_MODEL_PRICING.keys())
