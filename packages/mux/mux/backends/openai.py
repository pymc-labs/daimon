"""OpenAI backend: flexible execution, partial steering.

The Agents API allows dropping the sandbox entirely and supports multi-agent
handoffs, but steering is partial and the model range is unspecified in the
public docs. Partial steering reads as not-steerable under negotiation: a
flag must be honest, so `can_steer` stays False until conformance says
otherwise.
"""

from __future__ import annotations

from mux.capabilities import Capabilities

BACKEND_ID = "openai"

CAPABILITIES = Capabilities(
    can_steer=False,
    can_schedule=False,
    durable_fs=True,
    self_hosted_sandbox=True,
)
