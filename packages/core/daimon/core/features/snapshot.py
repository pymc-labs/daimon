"""`resolve_features`: every feature resolved once for one place, on every session path.

One read resolves every requested feature for a place into a `FeatureSnapshot`.
Stored rows are cached per (tenant, revision, place); every write bumps the revision,
so a cached read is never stale. Approvals stay in the access policy and are read
fresh each time.
"""

from __future__ import annotations

import uuid
from collections import OrderedDict
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cache
from typing import Final, cast

import structlog
from daimon.core.config import Settings
from daimon.core.features.deployment import deployment_value
from daimon.core.features.registry import (
    ENV_IMPORTED,
    FEATURES,
    FeatureSpec,
    Requirement,
    Tier,
)
from daimon.core.features.resolver import (
    ResolvedFeature,
    TierValue,
    Unavailable,
    resolve_feature,
)
from daimon.core.stores.domain import FeatureValueRow
from daimon.core.stores.feature_values import feature_revision, place_values
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

Sessions = async_sessionmaker[AsyncSession]
log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class FeaturePlace:
    """Where a turn or session runs. `thread_key` is the stores' key (Slack: ``<channel>:<ts>``)."""

    tenant_id: uuid.UUID
    platform: str
    channel_id: str | None = None
    thread_id: str | None = None
    agent_id: str | None = None
    backend: str | None = None

    @classmethod
    def of(
        cls,
        tenant_id: uuid.UUID,
        platform: str,
        channel_id: str | None,
        thread_id: str | None = None,
        *,
        agent_id: str | None = None,
        backend: str | None = None,
    ) -> FeaturePlace:
        """From a turn's ids; a thread equal to its channel is none."""
        if thread_id and platform == "slack" and ":" in thread_id:
            channel, _, thread_id = thread_id.partition(":")
            channel_id = channel_id or channel
        if not thread_id or thread_id == channel_id or not channel_id:
            thread_id = None
        return cls(tenant_id, platform, channel_id or None, thread_id, agent_id, backend)

    @property
    def thread_key(self) -> str | None:
        if self.thread_id is None or self.platform != "slack":
            return self.thread_id
        return f"{self.channel_id}:{self.thread_id}"


def _adapter_set_up(settings: Settings, platform: str) -> bool:
    adapters = {"discord": settings.discord, "slack": settings.slack, "teams": settings.teams}
    return adapters.get(platform) is not None


_SETUP: Final[Mapping[Requirement, Callable[[Settings], bool]]] = {
    "compute_setup": lambda s: (
        s.compute.enabled
        and bool(s.compute.deployment_id)
        and s.compute.api_token is not None
        and bool(s.compute.org_slug)
    ),
    "notebook_host": lambda s: (
        s.notebook.host_url is not None and s.notebook.admin_secret is not None
    ),
    "report_host": lambda s: (
        s.report_host.host_url is not None and s.report_host.admin_secret is not None
    ),
    "gemini_key": lambda s: s.gemini.api_key is not None,
    "microsoft_accounts": lambda s: s.microsoft_accounts_enabled and bool(s.crypto.keys),
}
"""One integration's setup, exactly the condition its tools need to run. Unmet, the session is not
offered them, so a check here must never be stricter than the tools. Watches and GitHub have none:
their tools work (and list or remove what exists) without the operator gate or the App."""


def support_channel_set(settings: Settings, platform: str) -> bool:
    """Whether `platform` has an escalation channel it can post in and credits to spend."""
    support = settings.support
    channel = {
        "discord": support.escalation_channel_id,
        "slack": support.slack_escalation_channel_id,
        "teams": support.teams_escalation_channel_id,
    }.get(platform)
    return (
        bool(channel)
        and not (platform == "discord" and (channel or "").startswith("19:"))
        and (support.credits_per_user > 0)
    )


def unavailable_reason(
    spec: FeatureSpec, settings: Settings | None, place: FeaturePlace
) -> Unavailable | None:
    """Why `spec` cannot apply at `place`, or None. `depends_on` is checked by the caller;
    without `settings` (a reader of a feature with no deployment tier) `requires` is too."""
    if not spec.on_platform(place.platform):
        return "platform"
    if settings is not None and (
        ("platform_adapter" in spec.requires and not _adapter_set_up(settings, place.platform))
        or (
            "support_channel" in spec.requires and not support_channel_set(settings, place.platform)
        )
        or not all(_SETUP[need](settings) for need in spec.requires if need in _SETUP)
    ):
        return "requires"
    if not spec.tiers_on(place.backend):
        return "backend"
    return None


@cache
def _warn_ignored(env: str, key: str) -> None:
    log.warning("features.legacy_env_ignored", env=env, imported_to=key)


def _env_map_value(
    spec: FeatureSpec,
    settings: Settings | None,
    place: FeaturePlace,
    rows: Iterable[FeatureValueRow],
) -> TierValue | None:
    """A deprecated per-workspace env map as the workspace tier, until it is imported."""
    if spec.env_map is None or settings is None:
        return None
    raw = cast(Mapping[uuid.UUID, bool], getattr(settings, spec.env_map)).get(place.tenant_id)
    if raw is None:
        return None
    if any(row.key == ENV_IMPORTED for row in rows):
        _warn_ignored(spec.legacy_env[0], spec.key)
        return None
    return TierValue("workspace", "on" if raw else "off")


def tier_values(
    spec: FeatureSpec,
    settings: Settings | None,
    place: FeaturePlace,
    rows: Iterable[FeatureValueRow],
    agent_builtins: Mapping[str, bool | None] | None = None,
) -> list[TierValue]:
    """The deployment value plus this place's stored rows for `spec`, on its backend's tiers.
    A `tool.*` agent tier is the agent's own tool config (`agent_builtins`), never a row."""
    tiers: tuple[Tier, ...] = spec.tiers_on(place.backend)
    deployment = deployment_value(settings, spec) if "deployment" in spec.tiers else None
    found = [deployment] if deployment is not None else []
    rows = list(rows)
    from_env = _env_map_value(spec, settings, place, rows) if "workspace" in tiers else None
    found += [from_env] if from_env is not None else []
    found += [
        TierValue(row.tier, row.value, row.locked)
        for row in rows
        if row.key == spec.key and row.tier in tiers
    ]
    builtin = spec.key.removeprefix("tool.")
    if agent_builtins is not None and "agent" in tiers and spec.key != builtin:
        enabled = agent_builtins.get(builtin)
        if enabled is not None:
            found.append(TierValue("agent", "on" if enabled else "off"))
    return found


def resolve_one(
    spec: FeatureSpec,
    settings: Settings | None,
    place: FeaturePlace,
    rows: Iterable[FeatureValueRow],
    resolved: Mapping[str, ResolvedFeature] | None = None,
    agent_builtins: Mapping[str, bool | None] | None = None,
) -> ResolvedFeature:
    """Pure: one feature at one place from its rows; `resolved` answers `depends_on`."""
    reason = unavailable_reason(spec, settings, place)
    if reason is None and any(
        resolved is not None and dep in resolved and not resolved[dep].on for dep in spec.depends_on
    ):
        reason = "depends_on"
    fixed = spec.backend_fixed.get(place.backend) if reason == "backend" and place.backend else None
    values = tier_values(spec, settings, place, rows, agent_builtins)
    return resolve_feature(
        spec, values, unavailable=reason, fixed=fixed, default=spec.default_for(place.platform)
    )


@dataclass(frozen=True)
class FeatureSnapshot:
    """Every requested feature resolved for one place."""

    place: FeaturePlace
    revision: int
    features: Mapping[str, ResolvedFeature] = field(default_factory=dict[str, ResolvedFeature])

    def get(self, key: str) -> ResolvedFeature:
        """The resolved feature; a key outside the snapshot is a bug, so it raises."""
        return self.features[key]

    def on(self, key: str) -> bool:
        return self.get(key).on

    def value(self, key: str) -> str:
        return self.get(key).value


class FeatureCache:
    """Stored rows per (tenant, revision, place): a bounded memo that cannot go stale."""

    def __init__(self, size: int = 2048) -> None:
        self._size = size
        self._rows: OrderedDict[tuple[object, ...], list[FeatureValueRow]] = OrderedDict()

    def get(self, key: tuple[object, ...]) -> list[FeatureValueRow] | None:
        found = self._rows.get(key)
        if found is not None:
            self._rows.move_to_end(key)
        return found

    def put(self, key: tuple[object, ...], rows: list[FeatureValueRow]) -> None:
        self._rows[key] = rows
        self._rows.move_to_end(key)
        while len(self._rows) > self._size:
            self._rows.popitem(last=False)

    def clear(self) -> None:
        self._rows.clear()


CACHE = FeatureCache()
"""The process's memo. Keyed by revision, so sharing it is safe; `change_feature` clears it."""


async def _place_rows(
    session: AsyncSession, place: FeaturePlace, cache: FeatureCache
) -> tuple[int, list[FeatureValueRow]]:
    revision = await feature_revision(session, tenant_id=place.tenant_id)
    key = (place.tenant_id, revision, place.agent_id, place.channel_id, place.thread_key)
    rows = cache.get(key)
    if rows is None:
        rows = await place_values(
            session,
            tenant_id=place.tenant_id,
            agent_id=place.agent_id,
            channel_id=place.channel_id,
            thread_key=place.thread_key,
        )
        cache.put(key, rows)
    return revision, rows


def approvals_from_gate(value: str, scope: str) -> ResolvedFeature:
    """The approval gate's answer as a resolved feature (its scope is ``server`` or
    ``<channel|thread>:<id>``; ``inherit`` everywhere is the registry default)."""
    if value == "inherit":
        return ResolvedFeature("approvals", FEATURES["approvals"].default, "default")
    kind = scope.split(":", 1)[0]
    tier: Tier = "workspace" if kind == "server" else "thread" if kind == "thread" else "channel"
    return ResolvedFeature("approvals", value, tier)


async def resolve_features(
    sessions: Sessions,
    settings: Settings | None,
    place: FeaturePlace,
    *,
    keys: Collection[str] | None = None,
    cache: FeatureCache = CACHE,
) -> FeatureSnapshot:
    """Resolve `keys` (every registered feature by default) for `place` in one read.
    Without `settings` the deployment tier is the registry default."""
    # Imported here: the access policy reaches `defaults`, whose agent writers resolve
    # features through this module.
    from daimon.core.approval_gate import resolve_approval_gate
    from daimon.core.stores.access_policy import load_access_policy

    wanted = [key for key in FEATURES if keys is None or key in keys]
    resolved: dict[str, ResolvedFeature] = {}
    async with sessions() as session:
        revision, rows = await _place_rows(session, place, cache)
        if "approvals" in wanted:
            policy = await load_access_policy(session, tenant_id=place.tenant_id)
            gate = resolve_approval_gate(
                policy,
                platform=place.platform,
                channel_id=place.channel_id,
                thread_id=place.thread_id,
            )
            resolved["approvals"] = approvals_from_gate(gate.value, gate.scope)
    for key in wanted:
        if key != "approvals":
            resolved[key] = resolve_one(FEATURES[key], settings, place, rows, resolved)
    return FeatureSnapshot(place, revision, resolved)


async def resolve_agent_features(
    sessions: Sessions | None,
    settings: Settings | None,
    *,
    tenant_id: uuid.UUID,
    agent_id: str | None,
    keys: Collection[str],
    spec_values: Mapping[str, str] | None = None,
    backend: str | None = None,
    cache: FeatureCache = CACHE,
) -> FeatureSnapshot:
    """`keys` for an agent alone (no channel): what an agent writer resolves before it saves.

    `spec_values` are the agent's `AgentSpec.features`, the agent tier of an agent not yet
    saved or synced; a person's own agent-tier value still wins over them."""
    place = FeaturePlace(tenant_id, "", agent_id=agent_id, backend=backend)
    revision, rows = 0, list[FeatureValueRow]()
    if sessions is not None and agent_id is not None:
        async with sessions() as session:
            revision, rows = await _place_rows(session, place, cache)
    if spec_values:
        personal = {row.key for row in rows if row.tier == "agent" and row.surface != "spec"}
        rows = [
            row
            for row in rows
            if row.tier != "agent" or row.key not in spec_values or row.key in personal
        ]
        rows += [
            FeatureValueRow(
                tenant_id=tenant_id,
                tier="agent",
                agent_id=agent_id or "",
                channel_id="",
                thread_key="",
                key=key,
                value=value,
                locked=False,
                set_by=None,
                surface="spec",
                set_at=datetime.now(UTC),
                revision=revision,
            )
            for key, value in spec_values.items()
            if key not in personal
        ]
    resolved = {key: resolve_one(FEATURES[key], settings, place, rows) for key in keys}
    return FeatureSnapshot(place, revision, resolved)


async def place_feature(
    sessions: Sessions, settings: Settings | None, place: FeaturePlace, key: str
) -> ResolvedFeature:
    """One feature at `place`, for a reader with no admission in hand (a delivery, a
    notice). A read that fails keeps the registry default, as `cost_footer_shown` does."""
    try:
        return (await resolve_features(sessions, settings, place, keys=(key,))).get(key)
    except Exception:
        log.warning("features.place_read_failed", key=key, exc_info=True)
        spec = FEATURES[key]
        return ResolvedFeature(key, spec.default_for(place.platform), "default")


async def feature_in(
    session: AsyncSession, settings: Settings | None, place: FeaturePlace, key: str
) -> ResolvedFeature:
    """One feature at `place` inside the caller's open session, for a reader already in a
    transaction (a gate that also reads other rows). Not cached; a failed read raises."""
    rows = await place_values(
        session,
        tenant_id=place.tenant_id,
        agent_id=place.agent_id,
        channel_id=place.channel_id,
        thread_key=place.thread_key,
        keys=(key, ENV_IMPORTED),
    )
    return resolve_one(FEATURES[key], settings, place, rows)
