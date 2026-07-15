"""Pre-flight checks for `apply_defaults` that probe upstream constraints
before any destructive write.

Today the only check is model acceptance: MA's agent-create endpoint
maintains an internal allowlist of model IDs that can drift independently
of the SDK's declared `BetaManagedAgentsModelParam` Literal. We learned
this the hard way on 2026-05-21 when MA briefly rejected every Claude
model id on a working API key (request_id `req_011CbG9WXcnZWX987LT3i5dp`);
the daimon agent was archived, and reconcile couldn't recreate it.

The probe creates a throwaway agent with the candidate model and archives
it immediately. Result: `None` on acceptance, a short reason string on
rejection. `apply_defaults` aggregates these per-model and aborts the
whole apply before touching skills/envs/agents if any are rejected.
"""

from __future__ import annotations

import uuid

import anthropic
import structlog
from anthropic import AsyncAnthropic

_log = structlog.get_logger(__name__)


async def check_model_accepted(client: AsyncAnthropic, model: str) -> str | None:
    """Probe whether `client.beta.agents.create(model=...)` is currently accepted.

    Returns:
        ``None`` if MA accepted the create (probe agent is archived
        before this returns), or a short human-readable reason if MA
        returned a 400 ``model.id ... is not supported`` error. Any
        other upstream failure re-raises — those are real outages, not
        our problem to classify.
    """
    probe_name = f"daimon-preflight-{uuid.uuid4().hex[:8]}"
    try:
        agent = await client.beta.agents.create(
            model=model,
            name=probe_name,
            metadata={"daimon_preflight": "true"},
        )
    except anthropic.APIStatusError as err:
        if err.status_code == 400 and "is not supported" in str(err):
            return f"MA rejected model {model!r}: {str(err)[:200]}"
        raise
    await client.beta.agents.archive(agent.id)
    return None


async def check_models_accepted(client: AsyncAnthropic, models: set[str]) -> dict[str, str | None]:
    """Probe each unique model in turn. Order is unspecified; caller doesn't depend on it.

    Sequential rather than concurrent because each probe is a cheap two-call
    burst (create + archive) and parallelizing would multiply the orphan-risk
    if archive fails mid-probe.
    """
    results: dict[str, str | None] = {}
    for model in models:
        result = await check_model_accepted(client, model)
        results[model] = result
        if result is not None:
            _log.warning("defaults.preflight.model_rejected", model=model, reason=result)
        else:
            _log.info("defaults.preflight.model_accepted", model=model)
    return results
