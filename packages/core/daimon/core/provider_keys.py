"""Which providers this deployment holds a usable key for: the default backend allowlist.

A tenant whose admin never chose its providers may use Anthropic plus every
provider the operator keyed for the whole deployment. That is the operator's
consent for the install, read when the policy is read; nothing is written per
tenant, and an admin's own choice (`request_provider_allowlist`) always wins.

A provider counts as keyed when its deployment key (`DEFAULT_KEY_ENV`) is set
in this process, or when the configured provider runtime manifest
(`DAIMON_TURN__PROVIDER_RUNTIME_FILE`) has a row on one of its profiles whose
`api_key_env` is set. An unkeyed provider is never in the default. The code
runtime defaults read the same `provider_key_present`, so a provider is allowed by
default exactly when the deployment can run it. Self-hosted backends are
operator-registered hosts, not a key, so they are never defaulted on.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import get_args

from mux.contracts.ids import Provider
from mux.errors import InvalidConfig
from mux.profiles import get_profile
from pydantic import TypeAdapter, ValidationError

_MANIFEST_ENV = "DAIMON_TURN__PROVIDER_RUNTIME_FILE"
_MAX_BYTES = 1_048_576
_ROWS = TypeAdapter(list[dict[str, object]])
# The order a default allowlist lists its providers in.
_ORDER: tuple[Provider, ...] = get_args(Provider)
_DEFAULTABLE: frozenset[Provider] = frozenset({"openai", "gemini"})
# The deployment-wide key each provider's code runtime defaults use.
DEFAULT_KEY_ENV: dict[Provider, str] = {
    "openai": "DAIMON_CHANNEL_OPENAI_KEY",
    "gemini": "DAIMON_CHANNEL_GEMINI_KEY",
}
_CacheKey = tuple[str, int, int, int]
_cache: tuple[_CacheKey, frozenset[tuple[Provider, str]]] | None = None


def _keyed_rows(path: str) -> frozenset[tuple[Provider, str]]:
    """(provider, key variable) of every well-formed manifest row; malformed rows are skipped."""
    global _cache
    try:
        stat = Path(path).stat()
    except OSError:
        return frozenset()
    key: _CacheKey = (path, stat.st_mtime_ns, stat.st_size, stat.st_ino)
    if _cache is not None and _cache[0] == key:
        return _cache[1]
    try:
        with Path(path).open("rb") as manifest:
            raw = manifest.read(_MAX_BYTES + 1)
        rows = _ROWS.validate_json(raw) if len(raw) <= _MAX_BYTES else []
    except (OSError, ValidationError):
        rows = []
    found: set[tuple[Provider, str]] = set()
    for row in rows:
        profile, env = row.get("profile"), row.get("api_key_env")
        if not isinstance(profile, str) or not isinstance(env, str) or not env:
            continue
        try:
            provider = get_profile(profile).provider
        except InvalidConfig:
            continue
        if provider in _DEFAULTABLE:
            found.add((provider, env))
    result = frozenset(found)
    _cache = (key, result)
    return result


def provider_key_present(provider: Provider) -> bool:
    """Whether this deployment's own key for `provider` is set (not blank)."""
    env = DEFAULT_KEY_ENV.get(provider)
    return env is not None and bool((os.environ.get(env) or "").strip())


def deployment_keyed_providers() -> frozenset[Provider]:
    """Anthropic, plus each provider whose deployment key, or a manifest row's key, is set."""
    keyed: set[Provider] = {"anthropic"}
    keyed.update(provider for provider in _DEFAULTABLE if provider_key_present(provider))
    path = os.environ.get(_MANIFEST_ENV)
    if path:
        keyed.update(
            provider for provider, env in _keyed_rows(path) if (os.environ.get(env) or "").strip()
        )
    return frozenset(keyed)


def default_enabled_backends() -> tuple[Provider, ...]:
    """The keyed providers in a stable order, Anthropic first."""
    keyed = deployment_keyed_providers()
    return tuple(provider for provider in _ORDER if provider in keyed)


def effective_enabled_backends(
    stored: tuple[Provider, ...], *, explicit: bool
) -> tuple[Provider, ...]:
    """An admin's stored choice when there is one, else the deployment default."""
    return stored if explicit else default_enabled_backends()
