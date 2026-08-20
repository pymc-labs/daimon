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

# What a new agent gets when the creator does not name a model: every panel
# prefill and every "no model supplied" fallback reads this one value. It used
# to be a bare literal repeated across the Discord panel, the Discord section
# modal and the Slack submit path, which is how three surfaces ended up a
# generation behind `defaults/agents/daimon.yaml` while each looked correct in
# isolation. Keep it equal to the model that file pins.
DEFAULT_AGENT_MODEL: str = "claude-sonnet-5"

# The per-agent skill and MCP-server limits every surface enforces. The setup
# panel (disabling its add controls) and the chat update path (refusing a
# merge that would exceed the cap) both read these two values, so the two
# surfaces cannot disagree.
#
# These are deliberate product caps chosen to sit below the provider's own
# per-agent limits — not a copy of them. The provider's real limit is
# established by the re-runnable probe at
# `packages/core/tests/contracts/test_skill_cap.py`, which reads the number
# out of the provider's own rejection message rather than a hardcoded second
# value here, because that number has moved before.
AGENT_SKILL_CAP: int = 20
AGENT_MCP_CAP: int = 20

# How many times the SDK retries a request before giving up. The SDK's own
# default is 2, which retries a 429 twice honouring `retry-after` and then
# raises. That is enough for a burst and not enough for a sustained overage:
# the Skills API is capped per ORG at 100 requests/minute, so a cold defaults
# sweep across every tenant can sit over the line for longer than two retries
# can wait out. Every adapter runtime passes this when constructing its
# client, so a boot-time sweep paces itself instead of failing half-applied.
MA_MAX_RETRIES: int = 8
