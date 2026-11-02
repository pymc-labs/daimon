"""Provider runtime rows built in code, so a never-configured tenant can run them.

When the operator's provider runtime manifest has no row for this tenant's
profile, these rows serve its turns instead, for every provider the deployment
keys (`daimon.core.provider_keys`). They are ordinary manifest rows: the same
matcher selects them and the same plan models validate them. Each names this
request's tenant and wildcards only platform, channel and account, so one
tenant's default never serves another.

Served: Gemini agent and channel turns, and an OpenAI agent's own turn once its
tenant's hosted environment is known
(`request.openai_environment_id`). Both agent templates keep the agent's Daimon
MCP tools (`template_capabilities: "mcp"`), with a per-turn caller credential.
A channel pinned to OpenAI needs a native agent of its own, so it still needs an
operator row.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from daimon.core.provider_keys import DEFAULT_KEY_ENV, provider_key_present
from mux.profiles import INLINE_REUSE, PERSISTENT_WORKSPACE, selectable_models

if TYPE_CHECKING:
    from daimon.core.turn.prepare import ProviderPreparationRequest
    from pydantic import JsonValue

_ANY = "*"
GEMINI, OPENAI = INLINE_REUSE.provider, PERSISTENT_WORKSPACE.provider
# A namespace label for this deployment's own provider resources. OpenAI turns
# from these rows send no project header: the key's default project applies.
DEFAULT_PROJECT = "daimon-deployment"
# The deployment's requested session spend limit; profile capability checks
# determine whether the provider can honor it.
OPENAI_SPEND_LIMIT_CENTS = 50
# Per-attempt reservations and the shared per-account hold cap protect ordinary
# Gemini requests, including concurrent conversations and provider fallback.
_GEMINI_HOLD = "0.25"
# Concurrent conversations share this account/project hold cap. Each attempt
# checks outstanding reservations; measured usage determines billing.
_GEMINI_CAP = "1.75"


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
    ]


def _openai_rows(
    tenant_id: str, request: ProviderPreparationRequest | None
) -> list[dict[str, JsonValue]]:
    common: dict[str, JsonValue] = {
        "api_key_env": DEFAULT_KEY_ENV[OPENAI],
        "project_header": False,
    }
    # The host refuses a row limit that differs from the turn's own native budget.
    limit = OPENAI_SPEND_LIMIT_CENTS
    rows: list[dict[str, JsonValue]] = []
    environment = getattr(request, "openai_environment_id", None)
    if isinstance(environment, str) and environment:
        # The tenant's own hosted environment, provisioned on its first GPT turn.
        rows.append({**_openai_root(tenant_id, common, limit), "environment_id": environment})
    return rows


def _openai_root(
    tenant_id: str, common: dict[str, JsonValue], limit: int | None
) -> dict[str, JsonValue]:
    return {
        **_scope(tenant_id, PERSISTENT_WORKSPACE.profile_id),
        **common,
        "spend_limit_usd_cents": limit,
        "configuration_grant": _grant(PERSISTENT_WORKSPACE.profile_id, selectable_models(OPENAI)),
        "root_materialization": True,
        # Keeps the agent's Daimon MCP tools with a per-turn caller credential.
        "template_capabilities": "mcp",
    }


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


def capture_template_rows(tenant_id: str) -> list[dict[str, JsonValue]]:
    """This tenant's code-default agent templates, as source capture reads them.

    Capture reads only a template's scope, grant and capabilities, before any turn.
    The OpenAI one serves a turn only once the tenant's hosted environment is known,
    so it is listed here without one; selection still requires it.

    Both are listed whether or not this process has the provider's key: capture
    runs in the MCP server, which may not carry a key the chat host has.
    Turn selection still enforces `provider_key_present`, so listing one here can
    never serve a provider this deployment is not keyed for (smoke 0eee1b8e J6).
    """
    common: dict[str, JsonValue] = {"api_key_env": DEFAULT_KEY_ENV[OPENAI], "project_header": False}
    return [
        *(row for row in _gemini_rows(tenant_id) if row.get("root_materialization") is True),
        _openai_root(tenant_id, common, OPENAI_SPEND_LIMIT_CENTS),
    ]
