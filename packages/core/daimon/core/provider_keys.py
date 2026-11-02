"""Which providers this deployment holds its own key for: the default backend allowlist.

A tenant whose admin never chose its providers may use Anthropic plus every
provider the operator keyed for the whole deployment. That is the operator's
consent for the install, read when the policy is read; nothing is written per
tenant, and an admin's own choice (`request_provider_allowlist`) always wins.

Only deployment keys count (`DEFAULT_KEY_ENV`), never a tenant's runtime row: one
tenant's row must not turn a provider on for another. The code runtime defaults
serve exactly these providers, so a provider allowed by default can also run.
Self-hosted backends are operator-registered hosts, not a key, so they are never
defaulted on.
"""

from __future__ import annotations

import os
from typing import get_args

from mux.contracts.ids import Provider
from mux.profiles import INLINE_REUSE, MANAGED_AGENTS, PERSISTENT_WORKSPACE

# The deployment-wide key each provider's code runtime defaults use.
DEFAULT_KEY_ENV: dict[Provider, str] = {
    PERSISTENT_WORKSPACE.provider: "DAIMON_CHANNEL_OPENAI_KEY",
    INLINE_REUSE.provider: "DAIMON_CHANNEL_GEMINI_KEY",
}
# The order a default allowlist lists its providers in.
_ORDER: tuple[Provider, ...] = get_args(Provider)


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


def default_enabled_backends() -> tuple[Provider, ...]:
    """The keyed providers in a stable order, Anthropic first."""
    keyed = deployment_keyed_providers()
    return tuple(provider for provider in _ORDER if provider in keyed)
