"""The deployment tier: `DAIMON_FEATURES__<KEY>`, or a feature's deprecated variable.

The only reader of `progress_plans.mode`, `thread_participation.mode` and `mcp_context.enabled`
(`tests/features/test_feature_contracts.py` holds every other reader to that).
"""

from __future__ import annotations

from collections.abc import Mapping
from functools import cache

import structlog
from daimon.core.config import (
    FeatureEnvSettings,
    ProgressPlansSettings,
    Settings,
    ThreadParticipationSettings,
    get_repo_memory_settings,
)
from daimon.core.features.registry import FEATURES, FeatureSpec, parse_deployment
from daimon.core.features.resolver import TierValue, as_cascade, from_mode, resolve_feature
from daimon.core.scope_cascade import CascadeMode

log = structlog.get_logger(__name__)


@cache
def _warn_legacy(env: str, key: str) -> None:
    log.warning("features.legacy_env_used", env=env, use=f"DAIMON_FEATURES__{key.upper()}")


def _legacy_mode(settings: Settings, key: str) -> CascadeMode | None:
    if key == "progress_plans" and "mode" in settings.progress_plans.model_fields_set:
        return settings.progress_plans.mode
    if key == "participation" and "mode" in settings.thread_participation.model_fields_set:
        return settings.thread_participation.mode
    if key == "mcp_briefing" and "enabled" in settings.mcp_context.model_fields_set:
        return CascadeMode.ON if settings.mcp_context.enabled else CascadeMode.OFF
    return None


def _repo_memory(settings: Settings) -> TierValue:
    """Repository memory is the operator's to turn on, with its tools: Off is locked."""
    if "enabled" in get_repo_memory_settings(settings).model_fields_set:
        _warn_legacy("DAIMON_REPO_MEMORY__ENABLED", "repo_memory")
    if get_repo_memory_settings(settings).enabled:
        return TierValue("deployment", "on")
    return TierValue("deployment", "off", True)


@cache
def env_features() -> Mapping[str, str]:
    """`DAIMON_FEATURES__*` read once, for writers handed no `Settings` (an agent edit)."""
    return FeatureEnvSettings().features  # pyright: ignore[reportCallIssue]


def deployment_value(settings: Settings | None, spec: FeatureSpec) -> TierValue | None:
    """What the operator set for `spec`, or None to fall back to the registry default.
    Without `settings` the process env answers, and deprecated variables are not read."""
    features = settings.features if settings is not None else env_features()
    raw = features.get(spec.key)
    if raw is not None:
        value, locked = parse_deployment(spec.key, raw)
        return TierValue("deployment", value, locked)
    if settings is None:
        return None
    if spec.key == "repo_memory":
        return _repo_memory(settings)
    mode = _legacy_mode(settings, spec.key)
    if mode is None:
        return None
    for env in spec.legacy_env:
        _warn_legacy(env, spec.key)
    value, locked = from_mode(mode)
    return TierValue("deployment", value, locked)


def deployment_mode(settings: Settings, key: str) -> CascadeMode:
    """The deployment tier (or the registry default) as an on/off/disabled cascade mode."""
    spec = FEATURES[key]
    found = deployment_value(settings, spec)
    return as_cascade(resolve_feature(spec, [found] if found is not None else [])).mode


def block_mode(block: ThreadParticipationSettings | ProgressPlansSettings) -> CascadeMode:
    """A settings block's own deprecated mode, for callers handed only the block."""
    return block.mode
