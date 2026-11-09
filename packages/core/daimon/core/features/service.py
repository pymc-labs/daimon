"""Read and change features at each tier of a place: the one writer for every surface.

`change_feature` refuses before any write: an unknown key or value, a tier the place
or backend lacks, an actor without the tier's authority, a Lock below the agent tier
or by a non-admin, and a value a locked tier above rules out. It then checks the
caller's last read (`expected`), writes, records the change in `channel_changes` and
drops the resolver's cache. Approvals are written to the access policy
(`set_approval_gate`) by a live, uncached native admin only.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from typing import Literal, cast

from daimon.core.access_policy import Approvals, TenantAccessPolicy
from daimon.core.admin_actions import DelegatedAction
from daimon.core.approval_admin import approval_admin
from daimon.core.approval_gate import ApprovalChange, resolve_approval_gate, set_approval_gate
from daimon.core.authz import Subject
from daimon.core.channel_changes import (
    ChannelActor,
    ChannelChangeRefused,
    Expected,
    after_write,
)
from daimon.core.config import Settings
from daimon.core.errors import StaleChannelValue
from daimon.core.features.registry import DEFAULT, FEATURES, LOCKABLE, FeatureSpec, Tier
from daimon.core.features.resolver import ResolvedFeature, TierValue, resolve_above
from daimon.core.features.snapshot import (
    CACHE,
    FeaturePlace,
    approvals_from_gate,
    resolve_one,
    tier_values,
    unavailable_reason,
)
from daimon.core.platform_words import NEUTRAL_ADMIN, words_for
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.channel_changes import WORKSPACE_ID, WriteHook, record_change
from daimon.core.stores.domain import CHAT_PLATFORMS, ChangeSurface, ChannelSetting, FeatureTarget
from daimon.core.stores.feature_values import (
    clear_feature_value,
    place_values,
    set_feature_value,
)
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

Sessions = async_sessionmaker[AsyncSession]
_THREAD_SETTING: dict[str, ChannelSetting] = {
    "participation": "thread_participation",
    "progress_plans": "thread_progress_plans",
    "approvals": "thread_approvals",
}


@dataclass(frozen=True)
class FeatureActor:
    """Who changes a feature. `native_user_id` is the chat identity checked live for
    approvals; `admin_for` answers for a delegated action as the surface decides it;
    `owns_agent` answers whether the actor owns an agent (the agent tier's authority)."""

    tenant_id: uuid.UUID
    account_id: uuid.UUID | None
    surface: ChangeSurface
    native_user_id: str | None
    admin_for: Callable[[DelegatedAction | None], Awaitable[bool]]
    via_agent_key: bool = False
    on_write: WriteHook | None = None
    owns_agent: Callable[[str], Awaitable[bool]] | None = None


@dataclass(frozen=True)
class TierSetting:
    """One tier's own value at a place; `DEFAULT` when it sets none."""

    tier: Tier
    value: str = DEFAULT
    locked: bool = False

    @property
    def text(self) -> str:
        """``default``, ``<value>`` or ``<value>:locked``: what `expected` compares."""
        return f"{self.value}:locked" if self.locked else self.value


@dataclass(frozen=True)
class FeatureView:
    """One feature at one place: each tier's own value and what is in effect, and why."""

    spec: FeatureSpec
    tiers: tuple[TierSetting, ...]
    resolved: ResolvedFeature

    def own(self, tier: Tier) -> TierSetting | None:
        return next((item for item in self.tiers if item.tier == tier), None)


@dataclass(frozen=True)
class FeatureChange:
    key: str
    tier: Tier
    before: TierSetting
    after: TierSetting
    view: FeatureView


def place_tiers(spec: FeatureSpec, place: FeaturePlace) -> tuple[Tier, ...]:
    """The tiers of `spec` this place has, broadest first."""
    present = {
        "deployment": True,
        "workspace": True,
        "agent": place.agent_id is not None,
        "channel": place.channel_id is not None,
        "thread": place.thread_id is not None,
    }
    return tuple(tier for tier in spec.tiers_on(place.backend) if present[tier])


def _own(
    spec: FeatureSpec, place: FeaturePlace, values: Collection[TierValue]
) -> tuple[TierSetting, ...]:
    by_tier = {value.tier: value for value in values}
    return tuple(
        TierSetting(tier, by_tier[tier].value, by_tier[tier].locked)
        if tier in by_tier
        else TierSetting(tier)
        for tier in place_tiers(spec, place)
    )


def _approvals_view(policy: TenantAccessPolicy, place: FeaturePlace) -> FeatureView:
    spec = FEATURES["approvals"]

    def own(raw: str | None) -> str:
        return DEFAULT if raw is None or raw == "inherit" else raw

    rules = policy.channel_rules
    found = {
        "workspace": own(policy.approvals),
        "channel": own(rules[place.channel_id].approvals if place.channel_id in rules else None),
        "thread": own(rules[place.thread_key].approvals if place.thread_key in rules else None),
    }
    gate = resolve_approval_gate(
        policy, platform=place.platform, channel_id=place.channel_id, thread_id=place.thread_id
    )
    tiers = tuple(TierSetting(tier, found[tier]) for tier in place_tiers(spec, place))
    return FeatureView(spec, tiers, approvals_from_gate(gate.value, gate.scope))


async def read_features(
    sessions: Sessions,
    settings: Settings,
    place: FeaturePlace,
    *,
    keys: Collection[str] | None = None,
) -> tuple[FeatureView, ...]:
    """Every requested feature at `place`, registry order, read fresh. Anyone may read."""
    specs = [spec for key, spec in FEATURES.items() if keys is None or key in keys]
    async with sessions() as session:
        rows = await place_values(
            session,
            tenant_id=place.tenant_id,
            agent_id=place.agent_id,
            channel_id=place.channel_id,
            thread_key=place.thread_key,
        )
        policy = (
            await load_access_policy(session, tenant_id=place.tenant_id)
            if any(spec.key == "approvals" for spec in specs)
            else None
        )
    resolved: dict[str, ResolvedFeature] = {}
    views: list[FeatureView] = []
    for spec in specs:
        if spec.key == "approvals":
            view = _approvals_view(cast(TenantAccessPolicy, policy), place)
        else:
            values = tier_values(spec, settings, place, rows)
            view = FeatureView(
                spec, _own(spec, place, values), resolve_one(spec, settings, place, rows, resolved)
            )
        resolved[spec.key] = view.resolved
        views.append(view)
    return tuple(views)


# ---------- Changing ----------


def _refused(
    message: str, reason: str, kind: Literal["forbidden", "invalid"] = "invalid"
) -> ChannelChangeRefused:
    return ChannelChangeRefused(message, kind=kind, reason=reason)


def admin_word(platform: str | None) -> str:
    return words_for(platform).admin if platform in CHAT_PLATFORMS else NEUTRAL_ADMIN


def needs_admin(spec: FeatureSpec, platform: str) -> ChannelChangeRefused:
    admin = admin_word(platform)
    if spec.authority.native:
        return _refused(
            f"Only {admin} can change approvals. Ask {admin}.", "native_admin_required", "forbidden"
        )
    return _refused(
        f"Only {admin} can change {spec.name.lower()} here. Ask {admin}.",
        "admin_required",
        "forbidden",
    )


def feature_refusal(
    key: str,
    tier: str,
    value: str,
    *,
    locked: bool,
    place: FeaturePlace,
    settings: Settings,
) -> ChannelChangeRefused | None:
    """Why the names, place or lock rule out the change, before any read; None if not. Pure."""
    spec = FEATURES.get(key)
    if spec is None:
        return _refused(
            f"There is no setting called {key}. Choose one of: {', '.join(FEATURES)}.",
            "unknown_key",
        )
    reason = unavailable_reason(spec, settings, place)
    for failed, message, code in (
        (
            value != DEFAULT and value not in spec.values,
            f"{spec.name} can be {', '.join((*spec.values, DEFAULT))}.",
            "bad_value",
        ),
        (
            reason in ("platform", "backend"),
            f"{spec.name} isn't available here, so nothing was changed.",
            "wrong_platform",
        ),
        (
            reason == "requires",
            f"{spec.name} is turned off for all of Daimon; the operator would have to change that.",
            "deployment_disabled",
        ),
        (
            tier not in place_tiers(spec, place) or tier == "deployment",
            f"{spec.name} can't be set for this {tier} here.",
            "no_such_level",
        ),
        (
            locked and (tier not in LOCKABLE or value == DEFAULT or spec.authority.native),
            f"{spec.name} can't be locked at the {tier} level.",
            "bad_lock",
        ),
    ):
        if failed:
            return _refused(message, code)
    return None


async def _native_admin(
    sessions: Sessions, settings: Settings, actor: FeatureActor, platform: str
) -> Subject:
    """A live, uncached native admin check of the actor's chat identity, or a refusal."""
    if not actor.via_agent_key and actor.native_user_id:
        subject = await approval_admin(
            sessions, settings, tenant_id=actor.tenant_id, user_id=actor.native_user_id
        )
        if subject.is_admin:
            return subject
    raise needs_admin(FEATURES["approvals"], platform)


async def may_change(
    actor: FeatureActor, spec: FeatureSpec, tier: Tier, *, locked: bool, agent_id: str | None
) -> bool:
    """The tier's authority (not approvals'): a thread is anyone's where the spec says so,
    an agent its owner's or an admin's, the rest an admin's. A Lock is an admin's."""
    if tier == "thread" and spec.authority.thread_anyone and not locked:
        return True
    if actor.via_agent_key:
        return False
    owner = actor.owns_agent
    if tier == "agent" and not locked and agent_id and owner and await owner(agent_id):
        return True
    return await actor.admin_for(spec.authority.action)


async def change_refusal(
    sessions: Sessions,
    settings: Settings,
    actor: FeatureActor,
    *,
    key: str,
    tier: Tier,
    place: FeaturePlace,
    locked: bool = False,
) -> ChannelChangeRefused | None:
    """Why the actor may not change `key` at `tier`, an early answer before a card or a
    control is shown; `change_feature` decides again when it writes."""
    spec = FEATURES[key]
    try:
        if spec.authority.native:
            await _native_admin(sessions, settings, actor, place.platform)
        elif not await may_change(actor, spec, tier, locked=locked, agent_id=place.agent_id):
            raise needs_admin(spec, place.platform)
    except ChannelChangeRefused as exc:
        return exc
    return None


def _target(tier: Tier, place: FeaturePlace) -> FeatureTarget:
    assert tier != "deployment", "the deployment tier is the operator's environment"
    return FeatureTarget(
        tier,
        agent_id=place.agent_id if tier == "agent" else None,
        channel_id=place.channel_id if tier in ("channel", "thread") else None,
        thread_key=place.thread_key if tier == "thread" else None,
    )


def _history(setting: TierSetting, place: FeaturePlace) -> JsonValue:
    """A tier's value as `channel_changes` records it; the legacy `disabled` is Off+Lock."""
    if setting.value == DEFAULT:
        return None
    value = "disabled" if setting.locked and setting.value == "off" else setting.text
    if setting.tier == "thread":
        return {"thread": place.thread_key, "mode": value}
    if setting.tier == "agent":
        return {"agent": place.agent_id, "value": value}
    return value


async def _change_approvals(
    sessions: Sessions,
    settings: Settings,
    actor: FeatureActor,
    tier: Tier,
    value: str,
    place: FeaturePlace,
    expected: Expected[str] | None,
) -> TierSetting:
    subject = await _native_admin(sessions, settings, actor, place.platform)

    def raw(text: str) -> Approvals:
        return cast(Approvals, "inherit" if text == DEFAULT else text)

    gate = await set_approval_gate(
        sessions,
        tenant_id=actor.tenant_id,
        platform=place.platform,
        target={"workspace": None, "channel": place.channel_id, "thread": place.thread_key}[tier],
        value=raw(value),
        subject=subject,
        record=ApprovalChange(
            place.channel_id if tier != "workspace" else None,
            place.thread_key if tier == "thread" else None,
            actor.account_id,
            actor.surface,
            actor.on_write,
        ),
        expected=raw(expected.value) if expected is not None else None,
    )
    before = gate.before or "inherit"
    return TierSetting(tier, DEFAULT if before == "inherit" else before)


def _locked_above(
    spec: FeatureSpec, above: ResolvedFeature, place: FeaturePlace, tier: Tier
) -> ChannelChangeRefused:
    where = (
        "everywhere" if above.tier in ("deployment", "default") else f"at the {above.tier} level"
    )
    return _refused(
        f"{spec.name} is locked to {spec.label(above.value)} {where}, so this {tier} can't "
        f"change it. Ask {admin_word(place.platform)}.",
        "locked_above",
    )


async def change_feature(
    sessions: Sessions,
    settings: Settings,
    actor: FeatureActor,
    *,
    key: str,
    tier: Tier,
    value: str,
    place: FeaturePlace,
    locked: bool = False,
    expected: Expected[str] | None = None,
) -> FeatureChange:
    """Set (`DEFAULT`: clear) `key` at `tier` of `place`; refused before any write.

    Raises `ChannelChangeRefused` (its message is for people) or `StaleChannelValue` when
    `expected` is not the tier's own `TierSetting.text` now."""
    refusal = feature_refusal(key, tier, value, locked=locked, place=place, settings=settings)
    if refusal is not None:
        raise refusal
    spec = FEATURES[key]
    after = TierSetting(tier, value, locked)
    if spec.authority.native:
        before = await _change_approvals(sessions, settings, actor, tier, value, place, expected)
    else:
        if not await may_change(actor, spec, tier, locked=locked, agent_id=place.agent_id):
            raise needs_admin(spec, place.platform)
        before = await _write(sessions, settings, actor, spec, after, place, expected)
    CACHE.clear()
    (view,) = await read_features(sessions, settings, place, keys=(key,))
    return FeatureChange(key, tier, before, after, view)


async def _write(
    sessions: Sessions,
    settings: Settings,
    actor: FeatureActor,
    spec: FeatureSpec,
    after: TierSetting,
    place: FeaturePlace,
    expected: Expected[str] | None,
) -> TierSetting:
    tier = after.tier
    async with sessions.begin() as session:
        rows = await place_values(
            session,
            tenant_id=place.tenant_id,
            agent_id=place.agent_id,
            channel_id=place.channel_id,
            thread_key=place.thread_key,
            keys=(spec.key,),
        )
        values = tier_values(spec, settings, place, rows)
        before = next((s for s in _own(spec, place, values) if s.tier == tier), TierSetting(tier))
        if expected is not None and expected.value != before.text:
            raise StaleChannelValue(spec.key, before.text)
        above = resolve_above(spec, values, tier)
        if above.locked and after.value not in (DEFAULT, above.value):
            raise _locked_above(spec, above, place, tier)
        target = _target(tier, place)
        if after.value == DEFAULT:
            await clear_feature_value(
                session, tenant_id=place.tenant_id, target=target, key=spec.key
            )
        else:
            await set_feature_value(
                session,
                tenant_id=place.tenant_id,
                target=target,
                key=spec.key,
                value=after.value,
                locked=after.locked,
                set_by=actor.account_id,
                surface=actor.surface,
            )
        await record_change(
            session,
            tenant_id=place.tenant_id,
            platform=place.platform,
            channel_id=place.channel_id
            if tier in ("channel", "thread") and place.channel_id
            else WORKSPACE_ID,
            setting=_THREAD_SETTING.get(spec.key, cast(ChannelSetting, spec.key))
            if tier == "thread"
            else cast(ChannelSetting, spec.key),
            before=_history(before, place),
            after=_history(after, place),
            actor_account_id=actor.account_id,
            surface=actor.surface,
        )
        channel_actor = ChannelActor(
            actor.tenant_id, actor.account_id, Subject(), actor.surface, actor.on_write
        )
        await after_write(session, channel_actor, before != after)
    return before
