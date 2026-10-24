"""Provider runtime rows built in code, so a never-configured tenant can run them.

When the operator's provider runtime manifest has no row for this tenant's
profile, these rows serve its turns instead, for every provider the deployment
keys (`daimon.core.provider_keys`). They are ordinary manifest rows: the same
matcher selects them and the same plan models validate them. Each names this
request's tenant and wildcards only platform, channel and account, so one
tenant's default never serves another.

Served: Gemini agent, channel and worker turns; OpenAI workers; and an OpenAI
agent's own turn once its tenant's hosted environment is known
(`request.openai_environment_id`). A channel pinned to OpenAI needs a native agent
of its own, so it still needs an operator row.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from daimon.core.provider_keys import DEFAULT_KEY_ENV, provider_key_present
from daimon.core.sessions import native_session_budget_cents
from mux.profiles import (
    CONVERSATION_ONLY,
    INLINE_REUSE,
    PERSISTENT_WORKSPACE,
    selectable_models,
    worker_models,
)

if TYPE_CHECKING:
    from daimon.core.turn.prepare import ProviderPreparationRequest
    from pydantic import JsonValue

_ANY = "*"
GEMINI, OPENAI = INLINE_REUSE.provider, CONVERSATION_ONLY.provider
# A namespace label for this deployment's own provider resources. OpenAI turns
# from these rows send no project header: the key's default project applies.
DEFAULT_PROJECT = "daimon-deployment"
# OpenAI sessions from these rows are capped unless the turn brings its own
# native budget (Astra must never run uncapped).
OPENAI_SPEND_LIMIT_CENTS = 50
# Per-attempt holds. A worker's hold also sizes its bounded token envelope (Argon's
# worst case must fit it), so it stays at $0.49.
_GEMINI_HOLD, _GEMINI_WORKER_HOLD = "0.25", "0.49"
# Root turns and workers of one account share one hold ledger (same project), and
# every attempt checks the ledger's whole total against its own cap. The cap limits
# outstanding reservations, not spend (billing is the measured usage). One cap for
# both, with room for a root turn and three workers at once (0.25 + 3 x 0.49):
# a cap equal to the worker hold let one open hold block every other Gemini call.
_GEMINI_CAP = _GEMINI_WORKER_CAP = "1.75"


def _grant(profile: str, models: tuple[str, ...]) -> dict[str, JsonValue]:
    return {
        "profiles": [profile],
        "models": list(models),
        "thread_modes": ["per_caller"],
        "requires": {},
    }


def _scope(tenant_id: str, profile: str) -> dict[str, JsonValue]:
    return {
        "profile": profile,
        "tenant_id": tenant_id,
        "platform": _ANY,
        "channel_id": _ANY,
        "account_id": _ANY,
        "project": DEFAULT_PROJECT,
    }


def _gemini_rows(tenant_id: str) -> list[dict[str, JsonValue]]:
    profile = INLINE_REUSE.profile_id
    base: dict[str, JsonValue] = {
        **_scope(tenant_id, profile),
        "api_key_env": DEFAULT_KEY_ENV[GEMINI],
        "storage_backend": "postgres",
        "tools": [],
    }
    turn: dict[str, JsonValue] = {
        **base,
        "configuration_grant": _grant(profile, selectable_models(GEMINI)),
        "hold_usd": _GEMINI_HOLD,
        "cap_usd": _GEMINI_CAP,
    }
    return [
        {
            **turn,
            "agent_name": "daimon-agent",
            "environment": {"name": "daimon-agent-template"},
            "root_materialization": True,
            "template_capabilities": "mcp",
        },
        # A channel pinned to Gemini: its own fixed, tool-free agent.
        {**turn, "agent_name": "daimon-channel", "environment": {"name": "daimon-channel"}},
        {
            **base,
            "configuration_grant": _grant(profile, worker_models(GEMINI)),
            "hold_usd": _GEMINI_WORKER_HOLD,
            "cap_usd": _GEMINI_WORKER_CAP,
            "agent_name": "daimon-fleet-worker",
            "environment": {"name": "fleet-tool-free", "execution": "hosted"},
            "source_agent_id": _ANY,
            "source_agent_version": _ANY,
            "worker_materialization": True,
        },
    ]


def _native_budget(request: ProviderPreparationRequest | None) -> Decimal | None:
    if request is None:
        return None
    if request.worker is not None:
        return request.worker.limits.native_budget_usd
    return request.native_budget_usd


def _openai_rows(
    tenant_id: str, request: ProviderPreparationRequest | None
) -> list[dict[str, JsonValue]]:
    common: dict[str, JsonValue] = {
        "api_key_env": DEFAULT_KEY_ENV[OPENAI],
        "project_header": False,
    }
    # The host refuses a row limit that differs from the turn's own native budget.
    budget = _native_budget(request)
    limit = OPENAI_SPEND_LIMIT_CENTS if budget is None else native_session_budget_cents(budget)
    worker: dict[str, JsonValue] = {
        **_scope(tenant_id, CONVERSATION_ONLY.profile_id),
        **common,
        "configuration_grant": _grant(CONVERSATION_ONLY.profile_id, worker_models(OPENAI)),
        "source_agent_id": _ANY,
        "source_agent_version": _ANY,
        "worker_materialization": True,
    }
    if budget is None:
        worker["spend_limit_usd_cents"] = limit
    rows = [worker]
    environment = getattr(request, "openai_environment_id", None)
    if isinstance(environment, str) and environment:
        # The tenant's own hosted environment, provisioned on its first GPT turn.
        rows.append(
            {
                **_scope(tenant_id, PERSISTENT_WORKSPACE.profile_id),
                **common,
                "spend_limit_usd_cents": limit,
                "configuration_grant": _grant(
                    PERSISTENT_WORKSPACE.profile_id, selectable_models(OPENAI)
                ),
                "environment_id": environment,
                "root_materialization": True,
            }
        )
    return rows


def template_rows(
    tenant_id: str, request: ProviderPreparationRequest | None = None
) -> list[dict[str, JsonValue]]:
    """This tenant's code-default rows for every provider, keyed or not."""
    return [*_gemini_rows(tenant_id), *_openai_rows(tenant_id, request)]


def default_rows(
    tenant_id: str, request: ProviderPreparationRequest | None = None
) -> list[dict[str, JsonValue]]:
    """This tenant's code-default rows for every provider the deployment keys."""
    keyed = {DEFAULT_KEY_ENV[p] for p in DEFAULT_KEY_ENV if provider_key_present(p)}
    return [row for row in template_rows(tenant_id, request) if row["api_key_env"] in keyed]
