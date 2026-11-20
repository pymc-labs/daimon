"""Which providers this deployment holds its own key for, and the default allowlist.

A key makes a provider available, not enabled: sending a tenant's conversations
to another company needs that tenant's admin to opt in
(`request_provider_allowlist`, the web admin settings or the CLI). A tenant whose
admin never chose therefore stays on Anthropic, however many keys the operator set,
unless the deployment has no Anthropic key: then nothing else can answer, and it
may use the keyed providers.

Only deployment keys count (`DEFAULT_KEY_ENV`), never a tenant's runtime row: one
tenant's row must not make a provider available to another. The code runtime
defaults serve exactly these providers, so an opted-in provider can also run.
Self-hosted backends are operator-registered hosts, not a key.
"""

from __future__ import annotations

import os
from functools import cache

from mux.contracts.ids import Provider
from mux.profiles import INLINE_REUSE, MANAGED_AGENTS, PERSISTENT_WORKSPACE

# The deployment-wide key each provider's code runtime defaults use.
DEFAULT_KEY_ENV: dict[Provider, str] = {
    PERSISTENT_WORKSPACE.provider: "DAIMON_CHANNEL_OPENAI_KEY",
    INLINE_REUSE.provider: "DAIMON_CHANNEL_GEMINI_KEY",
}


def provider_key_present(provider: Provider) -> bool:
    """Whether this deployment's own key for `provider` is set and not blank.

    The allowlist default and runtime selection both use this one check, so a
    provider allowed by default can also run. It returns only a bool: the value
    is never kept, logged or used to pick a credential. Tests that forbid
    credential reads allow exactly this helper (`daimon.testing.key_presence`).
    """
    env = DEFAULT_KEY_ENV.get(provider)
    return env is not None and bool((os.environ.get(env) or "").strip())


def deployment_keyed_providers() -> frozenset[Provider]:
    """Anthropic, plus each provider whose deployment key is set."""
    keyed: set[Provider] = {p for p in DEFAULT_KEY_ENV if provider_key_present(p)}
    return frozenset({MANAGED_AGENTS.provider, *keyed})


@cache
def anthropic_key_present() -> bool:
    """Whether the deployment's Anthropic key is set: the check its client is built by.

    Read once per process: the settings re-read `.env` on each build, and the key
    only changes with a restart. Tests reset it (`daimon.testing.pytest_plugin`).
    """
    from daimon.core.anthropic_guard import anthropic_key_configured
    from daimon.core.config import AnthropicEnvSettings

    return anthropic_key_configured(AnthropicEnvSettings().anthropic.resolved_api_key())


def default_enabled_backends() -> tuple[Provider, ...]:
    """What a tenant whose admin never chose may use.

    Anthropic only, or with no Anthropic key the keyed providers too, since
    nothing else can answer there. An allowlist always lists Anthropic.
    """
    keyed: tuple[Provider, ...] = tuple(p for p in DEFAULT_KEY_ENV if provider_key_present(p))
    if keyed and not anthropic_key_present():
        return (MANAGED_AGENTS.provider, *keyed)
    return (MANAGED_AGENTS.provider,)
