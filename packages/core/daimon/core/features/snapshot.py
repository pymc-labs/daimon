"""`resolve_features`: every feature resolved once for one place, on every session path.

One read resolves every requested feature for a place into a `FeatureSnapshot`.
Stored rows are cached per (tenant, revision, place); every write bumps the revision,
so a cached read is never stale. Approvals stay in the access policy and are read
fresh each time.
"""

from __future__ import annotations

import uuid
from collections import OrderedDict
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field

from daimon.core.approval_gate import resolve_approval_gate
from daimon.core.config import Settings
from daimon.core.features.deployment import deployment_value
from daimon.core.features.registry import FEATURES, FeatureSpec, Tier
from daimon.core.features.resolver import (
    ResolvedFeature,
    TierValue,
    Unavailable,
    resolve_feature,
)
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.domain import FeatureValueRow
from daimon.core.stores.feature_values import feature_revision, place_values
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

Sessions = async_sessionmaker[AsyncSession]


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


def unavailable_reason(
    spec: FeatureSpec, settings: Settings | None, place: FeaturePlace
) -> Unavailable | None:
    """Why `spec` cannot apply at `place`, or None. `depends_on` is checked by the caller;
    without `settings` (a reader of a feature with no deployment tier) `requires` is too."""
    if place.platform not in spec.platforms:
        return "platform"
    if settings is not None and "platform_adapter" in spec.requires:
        adapters = {"discord": settings.discord, "slack": settings.slack, "teams": settings.teams}
        if adapters.get(place.platform) is None:
            return "requires"
    if not spec.tiers_on(place.backend):
        return "backend"
    return None


def tier_values(
    spec: FeatureSpec,
    settings: Settings | None,
    place: FeaturePlace,
    rows: Iterable[FeatureValueRow],
) -> list[TierValue]:
    """The deployment value plus this place's stored rows for `spec`, on its backend's tiers."""
    tiers: tuple[Tier, ...] = spec.tiers_on(place.backend)
    deployment = deployment_value(settings, spec) if settings is not None else None
    found = [deployment] if deployment is not None else []
    found += [
        TierValue(row.tier, row.value, row.locked)
        for row in rows
        if row.key == spec.key and row.tier in tiers
    ]
    return found


def resolve_one(
    spec: FeatureSpec,
    settings: Settings | None,
    place: FeaturePlace,
    rows: Iterable[FeatureValueRow],
    resolved: Mapping[str, ResolvedFeature] | None = None,
) -> ResolvedFeature:
    """Pure: one feature at one place from its rows; `resolved` answers `depends_on`."""
    reason = unavailable_reason(spec, settings, place)
    if reason is None and any(
        resolved is not None and dep in resolved and not resolved[dep].on for dep in spec.depends_on
    ):
        reason = "depends_on"
    return resolve_feature(spec, tier_values(spec, settings, place, rows), unavailable=reason)


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
    settings: Settings,
    place: FeaturePlace,
    *,
    keys: Collection[str] | None = None,
    cache: FeatureCache = CACHE,
) -> FeatureSnapshot:
    """Resolve `keys` (every registered feature by default) for `place` in one read."""
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
