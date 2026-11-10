"""The deployment tier: `DAIMON_FEATURES__<KEY>`, or a feature's deprecated variable.

The only reader of the deprecated deployment-wide fields (`progress_plans.mode`,
`thread_participation.mode`, `mcp_context.enabled`, `_LEGACY_FLAG`); `tests/features/
test_feature_contracts.py` holds every other reader to that.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from functools import cache

import structlog
from daimon.core.config import FeatureEnvSettings, Settings
from daimon.core.features.registry import FeatureSpec, parse_deployment
from daimon.core.features.resolver import TierValue
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


def _set(block: object | None, name: str) -> bool | None:
    """A deprecated bool field of a settings block, or None while the operator left it unset."""
    if block is None or name not in getattr(block, "model_fields_set", ()):
        return None
    return bool(getattr(block, name))


_LEGACY_FLAG: Mapping[str, Callable[[Settings], bool | None]] = {
    "thread_naming": lambda s: _set(s.thread_naming, "enabled"),
    "private_channel_messages": lambda s: _set(s.private_channel_messages, "enabled"),
    "native_footer_cost": lambda s: _set(s.turn, "native_footer_cost"),
    "status_log": lambda s: _set(s.turn, "status_log"),
    "writers_none_notice": lambda s: _set(s.discord, "writers_none_notice"),
    "unmentioned_reply_hint": lambda s: _set(s.discord, "unmentioned_reply_hint"),
    "waiting_card": lambda s: _set(s.discord, "tenant_cap_waiting_card"),
    "watches": lambda s: _set(s.watches, "enabled"),
    "compute": lambda s: _set(s.compute, "enabled"),
    "artifact_images": lambda s: _set(s.artifacts, "embed_images"),
    "tool_safety": lambda s: _set(s.tool_safety, "enabled"),
    "reach_budget": lambda s: _set(s.outbound, "reach_budget_enabled"),
    "require_routine_destination": lambda s: _set(s.mcp, "require_routine_destination"),
}
"""A feature's deprecated deployment-wide bool, read nowhere but here."""


def _number(block: object | None, name: str, *, zero_off: bool = False) -> str | None:
    """A deprecated number field of a settings block as text, or None while it is unset
    (or 0 where 0 means no limit, which a limit cannot say)."""
    if block is None or name not in getattr(block, "model_fields_set", ()):
        return None
    value = getattr(block, name)
    return None if zero_off and not value else str(value)


_LEGACY_LIMIT: Mapping[str, Callable[[Settings], str | None]] = {
    "long_jobs_max_usd": lambda s: _number(s.long_jobs, "max_usd"),
    "long_jobs_max_deadline": lambda s: _number(s.long_jobs, "max_deadline_minutes"),
    "outbound_threads": lambda s: _number(s.outbound, "max_threads"),
    "outbound_places": lambda s: _number(s.outbound, "max_places"),
    "outbound_people": lambda s: _number(s.outbound, "max_people"),
    "outbound_length": lambda s: _number(s.outbound, "max_length", zero_off=True),
}
"""A limit's deprecated deployment-wide number, read nowhere but here."""


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
    flag = _LEGACY_FLAG.get(spec.key)
    if flag is not None and (enabled := flag(settings)) is not None:
        for env in spec.legacy_env:
            _warn_legacy(env, spec.key)
        return TierValue("deployment", "on" if enabled else "off")
    limit = _LEGACY_LIMIT.get(spec.key)
    if limit is not None and (number := limit(settings)) is not None:
        for env in spec.legacy_env:
            _warn_legacy(env, spec.key)
        return TierValue("deployment", number)
    mode = _legacy_mode(settings, spec.key)
    if mode is None:
        return None
    for env in spec.legacy_env:
        _warn_legacy(env, spec.key)
    # A deprecated `disabled` is Off + Locked.
    if mode is CascadeMode.DISABLED:
        return TierValue("deployment", "off", True)
    return TierValue("deployment", mode.value)
