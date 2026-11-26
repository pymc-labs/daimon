"""`resolve_features`: every feature resolved once for one place, on every session path.

One read resolves every requested feature for a place into a `FeatureSnapshot`.
Stored rows are cached per (tenant, revision, place); every write bumps the revision,
so a cached read is never stale. Approvals stay in the access policy and are read
fresh each time.
"""

from __future__ import annotations

import uuid
from collections import OrderedDict
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cache
from typing import Final, cast

import structlog
from daimon.core.access_policy import place_thread_id
from daimon.core.config import Settings, get_repo_memory_settings
from daimon.core.features.deployment import deployment_value
from daimon.core.features.registry import (
    FEATURES,
    FeatureSpec,
    Requirement,
    Tier,
    resolution_order,
)
from daimon.core.features.resolver import (
    ResolvedFeature,
    TierValue,
    Unavailable,
    resolve_feature,
)
from daimon.core.stores.domain import FeatureValueRow
from daimon.core.stores.feature_values import feature_revision, place_values
from daimon.core.stores.support import get_support_setup
from daimon.core.stores.support_rows import SupportSetup
from daimon.core.support import SupportPlace
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

Sessions = async_sessionmaker[AsyncSession]
log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class FeaturePlace:
    """Where a turn or session runs. `thread_key` is the stores' key (Slack: ``<channel>:<ts>``).
    A routine's run also has the routine's own tier (`routine_id`)."""

    tenant_id: uuid.UUID
    platform: str
    channel_id: str | None = None
    thread_id: str | None = None
    agent_id: str | None = None
    backend: str | None = None
    routine_id: uuid.UUID | None = None

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
        routine_id: uuid.UUID | None = None,
    ) -> FeaturePlace:
        """From a turn's ids; a thread equal to its channel (or a timer's scope) is none,
        and so is an empty agent id (a blank `?agent=` is no agent, not an agent tier)."""
        thread_id = place_thread_id(thread_id)
        if thread_id and platform == "slack" and ":" in thread_id:
            channel, _, thread_id = thread_id.partition(":")
            channel_id = channel_id or channel
        if not thread_id or thread_id == channel_id or not channel_id:
            thread_id = None
        return cls(
            tenant_id,
            platform,
            channel_id or None,
            thread_id,
            agent_id or None,
            backend,
            routine_id,
        )

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
        s.compute.enabled and bool(s.compute.deployment_id) and s.compute.provider_configured
    ),
    "notebook_host": lambda s: (
        s.notebook.host_url is not None and s.notebook.admin_secret is not None
    ),
    "report_host": lambda s: (
        s.report_host.host_url is not None and s.report_host.admin_secret is not None
    ),
    "gemini_key": lambda s: s.gemini.api_key is not None,
    # Every memory store lives with Anthropic, whichever backend answers.
    "anthropic_key": lambda s: s.anthropic.api_key is not None,
    "microsoft_accounts": lambda s: s.microsoft_accounts_enabled and bool(s.crypto.keys),
    "repo_memory_setup": lambda s: get_repo_memory_settings(s).enabled,
    "teams_public_url": lambda s: s.teams is not None and s.teams.public_url is not None,
}
"""One integration's setup, exactly the condition its tools need to run. Unmet, the session is not
offered them, so a check here must never be stricter than the tools. Watches and GitHub have none:
their tools work (and list or remove what exists) without the operator gate or the App."""

SERVER_SETUP: Final[frozenset[Requirement]] = frozenset(
    {"compute_setup", "notebook_host", "report_host", "gemini_key", "microsoft_accounts"}
)
"""Setup only the MCP server's tools use. A worker may not carry it, so a session's toolset leaves
it to the MCP server, whose capability gate judges it with its own settings."""


_SUPPORT: Final = frozenset({"support_channel", "support_credits"})


def support_met(
    need: Requirement, settings: Settings, platform: str, setup: SupportSetup | None
) -> bool:
    """Whether a support requirement holds on `platform`. `setup` is the workspace's own;
    None checks the deployment alone."""
    if need == "support_channel":
        return settings.support.channel_for(platform) is not None or (
            setup is not None
            and (setup.own_platform == platform or setup.menu_channel)
            and deployment_on(settings, "support_own_channel", platform)
        )
    return settings.support.credits_per_user > 0 or (setup is not None and setup.limits)


def unavailable_reason(
    spec: FeatureSpec,
    settings: Settings | None,
    place: FeaturePlace,
    support: SupportSetup | None = None,
    *,
    server_setup: bool = True,
) -> Unavailable | None:
    """Why `spec` cannot apply at `place`, or None. `depends_on` is checked by the caller;
    without `settings` (a reader of a feature with no deployment tier) `requires` is too,
    and without `server_setup` the requirements in `SERVER_SETUP` are.
    `support` is the workspace's own setup (`support_setup`), for the support requirements."""
    if not spec.on_platform(place.platform):
        return "platform"
    judged: list[Requirement] = [
        need for need in spec.requires if server_setup or need not in SERVER_SETUP
    ]
    if settings is not None and (
        ("platform_adapter" in judged and not _adapter_set_up(settings, place.platform))
        or not all(
            support_met(need, settings, place.platform, support)
            for need in judged
            if need in _SUPPORT
        )
        or not all(_SETUP[need](settings) for need in judged if need in _SETUP)
    ):
        return "requires"
    if not spec.tiers_on(place.backend):
        return "backend"
    return None


def server_setup_met(spec: FeatureSpec, settings: Settings) -> bool:
    """Whether `settings` carry every `SERVER_SETUP` requirement of `spec`."""
    return all(_SETUP[need](settings) for need in spec.requires if need in SERVER_SETUP)


def lacks_only_server_setup(
    spec: FeatureSpec,
    settings: Settings | None,
    place: FeaturePlace,
    support: SupportSetup | None = None,
) -> bool:
    """Whether `spec` is unavailable at `place` only for `SERVER_SETUP`, which a worker may not
    carry: there it can't tell setup the MCP server has from setup nobody has."""
    return unavailable_reason(spec, settings, place, support) == "requires" and (
        unavailable_reason(spec, settings, place, support, server_setup=False) is None
    )


async def support_setup(
    session: AsyncSession,
    settings: Settings | None,
    place: FeaturePlace,
    specs: Iterable[FeatureSpec],
) -> SupportSetup | None:
    """The workspace's own support setup, read only when the deployment alone leaves a
    support requirement of `specs` unmet at `place`. Not cached: a change elsewhere shows
    on the next read."""
    if settings is None or not place.platform:
        return None
    short = any(
        not support_met(need, settings, place.platform, None)
        for spec in specs
        for need in spec.requires
        if need in _SUPPORT
    )
    if not short:
        return None
    where = SupportPlace(channel_id=place.channel_id, agent_id=place.agent_id)
    return await get_support_setup(session, tenant_id=place.tenant_id, place=where)


@cache
def _warn_deprecated(env: str, key: str) -> None:
    log.warning("features.legacy_env_deprecated", env=env, feature=key)


def env_map_values(settings: Settings, path: str) -> Mapping[uuid.UUID, bool]:
    """A deprecated env map by its `FeatureSpec.env_map` path (dotted for a nested block)."""
    found: object = settings
    for name in path.split("."):
        found = getattr(found, name)
    return cast(Mapping[uuid.UUID, bool], found)


def _env_map_value(
    spec: FeatureSpec,
    settings: Settings | None,
    place: FeaturePlace,
    rows: Iterable[FeatureValueRow],
) -> TierValue | None:
    """A deprecated per-workspace env map as the workspace tier; a stored row beats it."""
    if spec.env_map is None or settings is None:
        return None
    raw = env_map_values(settings, spec.env_map).get(place.tenant_id)
    if raw is None:
        return None
    _warn_deprecated(spec.legacy_env[0], spec.key)
    if any(row.key == spec.key and row.tier == "workspace" for row in rows):
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
    support: SupportSetup | None = None,
    *,
    server_setup: bool = True,
) -> ResolvedFeature:
    """Pure: one feature at one place from its rows; `resolved` answers `depends_on`,
    `support` the workspace's own support setup."""
    reason = unavailable_reason(spec, settings, place, support, server_setup=server_setup)
    if reason is None and any(
        resolved is not None and dep in resolved and not resolved[dep].on for dep in spec.depends_on
    ):
        reason = "depends_on"
    fixed = spec.backend_fixed.get(place.backend) if reason == "backend" and place.backend else None
    values = tier_values(spec, settings, place, rows, agent_builtins)
    return resolve_feature(
        spec, values, unavailable=reason, fixed=fixed, default=spec.default_for(place.platform)
    )


def may_turn_on(settings: Settings, key: str, platform: str) -> bool:
    """Whether any narrower tier could still turn the feature on: false where it cannot
    run (participation without its adapter) or the deployment locks it Off."""
    place = FeaturePlace(uuid.UUID(int=0), platform)
    resolved = resolve_one(FEATURES[key], settings, place, ())
    return resolved.unavailable is None and (resolved.on or not resolved.locked)


def deployment_on(settings: Settings, key: str, platform: str) -> bool:
    """Whether a deployment-only feature is on for `platform`: its value and its setup."""
    place = FeaturePlace(uuid.UUID(int=0), platform)
    return resolve_one(FEATURES[key], settings, place, ()).on


def deployment_on_everywhere(settings: Settings, key: str, platforms: Sequence[str]) -> bool:
    """Whether a deployment-only feature is on for every one of `platforms`.

    The strictest reading, for an agent that answers on several platforms: a
    capability counts as kept only where no platform turns it off.
    """
    return all(deployment_on(settings, key, platform) for platform in platforms)


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
    """Stored rows per (tenant, revision, place): a bounded memo that cannot go stale.
    `generation` counts clears, so a memo built on it can tell a feature changed."""

    def __init__(self, size: int = 2048) -> None:
        self._size = size
        self._rows: OrderedDict[tuple[object, ...], list[FeatureValueRow]] = OrderedDict()
        self.generation = 0

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
        self.generation += 1


CACHE = FeatureCache()
"""The process's memo. Keyed by revision, so sharing it is safe; `change_feature` clears it."""


async def _place_rows(
    session: AsyncSession, place: FeaturePlace, cache: FeatureCache
) -> tuple[int, list[FeatureValueRow]]:
    revision = await feature_revision(session, tenant_id=place.tenant_id)
    key = (
        place.tenant_id,
        revision,
        place.agent_id,
        place.channel_id,
        place.thread_key,
        place.routine_id,
    )
    rows = cache.get(key)
    if rows is None:
        rows = await place_values(
            session,
            tenant_id=place.tenant_id,
            agent_id=place.agent_id,
            channel_id=place.channel_id,
            thread_key=place.thread_key,
            routine_id=place.routine_id,
        )
        cache.put(key, rows)
    return revision, rows


def approvals_from_gate(value: str, scope: str) -> ResolvedFeature:
    """The approval gate's answer as a resolved feature (its scope is ``server`` or
    ``<agent|channel|thread>:<id>``; ``inherit`` everywhere is the registry default)."""
    if value == "inherit":
        return ResolvedFeature("approvals", FEATURES["approvals"].default, "default")
    kind = scope.split(":", 1)[0]
    tier = cast(Tier, "workspace" if kind == "server" else kind)
    return ResolvedFeature("approvals", value, tier)


async def resolve_features(
    sessions: Sessions,
    settings: Settings | None,
    place: FeaturePlace,
    *,
    keys: Collection[str] | None = None,
    cache: FeatureCache = CACHE,
    agent_builtins: Mapping[str, bool | None] | None = None,
    server_setup: bool = True,
) -> FeatureSnapshot:
    """Resolve `keys` (every registered feature by default) for `place` in one read;
    `agent_builtins` (`model_input.agent_builtins`) is the agent tier of each `tool.*`.
    A session's toolset passes `server_setup=False`: see `SERVER_SETUP`.

    Without `settings` the deployment tier is the registry default. A database failure
    raises; this reader never falls back to a default. Callers that must keep working
    wrap it (`place_feature`) or fail closed (`place_feature_or_none`)."""
    # Imported here: the access policy reaches `defaults`, whose agent writers resolve
    # features through this module.
    from daimon.core.approval_gate import resolve_approval_gate
    from daimon.core.stores.access_policy import load_access_policy

    wanted = [key for key in FEATURES if keys is None or key in keys]
    resolved: dict[str, ResolvedFeature] = {}
    order = resolution_order(wanted)
    async with sessions() as session:
        revision, rows = await _place_rows(session, place, cache)
        support = await support_setup(session, settings, place, order)
        if "approvals" in wanted:
            policy = await load_access_policy(session, tenant_id=place.tenant_id)
            gate = resolve_approval_gate(
                policy,
                platform=place.platform,
                channel_id=place.channel_id,
                thread_id=place.thread_id,
                agent_id=place.agent_id,
            )
            resolved["approvals"] = approvals_from_gate(gate.value, gate.scope)
    # A feature's dependencies resolve first, even ones the caller did not ask for.
    for spec in order:
        if spec.key != "approvals":
            resolved[spec.key] = resolve_one(
                spec,
                settings,
                place,
                rows,
                resolved,
                agent_builtins,
                support,
                server_setup=server_setup,
            )
    return FeatureSnapshot(place, revision, {key: resolved[key] for key in wanted})


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
    resolved: dict[str, ResolvedFeature] = {}
    for spec in resolution_order(keys):
        resolved[spec.key] = resolve_one(spec, settings, place, rows, resolved)
    return FeatureSnapshot(place, revision, {key: resolved[key] for key in keys})


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


async def place_feature_or_none(
    sessions: Sessions, settings: Settings | None, place: FeaturePlace, key: str
) -> ResolvedFeature | None:
    """`place_feature` for a background worker: None when the read fails, so the caller
    skips this tick's work instead of running it on a guessed default."""
    try:
        return (await resolve_features(sessions, settings, place, keys=(key,))).get(key)
    except Exception:
        log.warning("features.background_read_failed", key=key, exc_info=True)
        return None


async def place_guardrail(
    sessions: Sessions, settings: Settings | None, place: FeaturePlace, key: str
) -> ResolvedFeature:
    """`place_feature` for a guardrail: a read that fails applies its strictest value.
    A deployment already at the strictest decides without a read."""
    strictest = list(FEATURES[key].values)[-1]
    try:
        floor = deployment_value(settings, FEATURES[key])
        if floor is not None and floor.value == strictest:
            return ResolvedFeature(key, strictest, "deployment", floor.locked)
        return (await resolve_features(sessions, settings, place, keys=(key,))).get(key)
    except Exception:
        log.warning("features.guardrail_read_failed", key=key, exc_info=True)
        return ResolvedFeature(key, strictest, "default")


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
        routine_id=place.routine_id,
        keys=(key,),
    )
    spec = FEATURES[key]
    support = await support_setup(session, settings, place, (spec,))
    return resolve_one(spec, settings, place, rows, support=support)


async def feature_in_or_none(
    session: AsyncSession, settings: Settings | None, place: FeaturePlace, key: str
) -> ResolvedFeature | None:
    """`feature_in` for a background worker: None when the read fails, so the caller skips
    this item. The read runs in a savepoint, so a failure leaves the transaction usable."""
    try:
        async with session.begin_nested():
            return await feature_in(session, settings, place, key)
    except Exception:
        log.warning("features.background_read_failed", key=key, exc_info=True)
        return None
