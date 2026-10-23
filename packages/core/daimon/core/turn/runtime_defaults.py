"""Provider runtime rows built in code, so a never-configured tenant can run them.

When the provider runtime manifest has no row for a turn, these templates serve it
instead, for every tenant the deployment keys (`daimon.core.provider_keys`). They
are ordinary manifest rows: the same matcher selects them and the same plan models
validate them, and any file row for the turn wins over them. Each row names this
request's tenant and wildcards only platform, channel and account, so one tenant's
default never serves another.

Served today: Gemini agent turns (a tenant-wide agent template) and Gemini and
OpenAI fleet workers. An OpenAI agent's own turn needs a hosted environment
(`openai.persistent_workspace`), which a tenant gets from its own row until it is
provisioned automatically.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from daimon.core.provider_keys import DEFAULT_KEY_ENV, provider_key_configured
from mux.profiles import selectable_models, worker_models

if TYPE_CHECKING:
    from daimon.core.turn.prepare import ProviderPreparationRequest
    from pydantic import JsonValue

_ANY = "*"
# A namespace label for this deployment's own provider resources. OpenAI turns
# from these rows send no project header: the key's default project applies.
DEFAULT_PROJECT = "daimon-deployment"
# Every OpenAI worker from these rows is capped per session unless the worker
# brings its own native budget (Astra must never run uncapped).
OPENAI_WORKER_SPEND_LIMIT_CENTS = 50
_GEMINI_ROOT_HOLD, _GEMINI_ROOT_CAP = "0.25", "0.50"
_GEMINI_WORKER_HOLD = _GEMINI_WORKER_CAP = "0.49"


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
    profile = "gemini.inline_reuse"
    base = {
        **_scope(tenant_id, profile),
        "api_key_env": DEFAULT_KEY_ENV["gemini"],
        "storage_backend": "postgres",
        "tools": [],
    }
    return [
        {
            **base,
            "configuration_grant": _grant(profile, selectable_models("gemini")),
            "hold_usd": _GEMINI_ROOT_HOLD,
            "cap_usd": _GEMINI_ROOT_CAP,
            "agent_name": "daimon-agent",
            "environment": {"name": "daimon-agent-template"},
            "root_materialization": True,
            "template_capabilities": "mcp",
        },
        {
            **base,
            "configuration_grant": _grant(profile, worker_models("gemini")),
            "hold_usd": _GEMINI_WORKER_HOLD,
            "cap_usd": _GEMINI_WORKER_CAP,
            "agent_name": "daimon-fleet-worker",
            "environment": {"name": "fleet-tool-free", "execution": "hosted"},
            "source_agent_id": _ANY,
            "source_agent_version": _ANY,
            "worker_materialization": True,
        },
    ]


def _openai_rows(
    tenant_id: str, request: ProviderPreparationRequest | None
) -> list[dict[str, JsonValue]]:
    profile = "openai.conversation_only"
    models = worker_models("openai")
    # A worker that brings its own native budget is capped by it; the host refuses a
    # row limit that differs. Without one, every model here is capped by the row.
    budgeted = (
        request is not None
        and request.worker is not None
        and request.worker.limits.native_budget_usd is not None
    )
    row: dict[str, JsonValue] = {
        **_scope(tenant_id, profile),
        "api_key_env": DEFAULT_KEY_ENV["openai"],
        "project_header": False,
        "configuration_grant": _grant(profile, models),
        "source_agent_id": _ANY,
        "source_agent_version": _ANY,
        "worker_materialization": True,
    }
    if not budgeted:
        row["spend_limit_usd_cents"] = OPENAI_WORKER_SPEND_LIMIT_CENTS
    return [row]


def template_rows(
    tenant_id: str, request: ProviderPreparationRequest | None = None
) -> list[dict[str, JsonValue]]:
    """This tenant's code-default rows for every provider, keyed or not.

    Reads no credential, so capability evidence and hold lookups can use it; only
    selection (`default_rows`) decides whether a provider is configured.
    """
    return [*_gemini_rows(tenant_id), *_openai_rows(tenant_id, request)]


def default_rows(
    tenant_id: str, request: ProviderPreparationRequest | None = None
) -> list[dict[str, JsonValue]]:
    """This tenant's code-default rows for every provider the deployment keys."""
    keyed = {
        "gemini": provider_key_configured("gemini"),
        "openai": provider_key_configured("openai"),
    }
    return [
        row
        for row in template_rows(tenant_id, request)
        if keyed[str(row["profile"]).partition(".")[0]]
    ]
