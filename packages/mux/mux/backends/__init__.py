"""Backend capability tables. One module per backend; values are starting
positions from the draft spec, not conclusions."""

from __future__ import annotations

from mux.backends.anthropic import CAPABILITIES as ANTHROPIC_CAPABILITIES
from mux.backends.google import CAPABILITIES as GOOGLE_CAPABILITIES
from mux.backends.openai import CAPABILITIES as OPENAI_CAPABILITIES
from mux.capabilities import BackendId, Capabilities

BACKENDS: dict[BackendId, Capabilities] = {
    "anthropic": ANTHROPIC_CAPABILITIES,
    "openai": OPENAI_CAPABILITIES,
    "google": GOOGLE_CAPABILITIES,
}
