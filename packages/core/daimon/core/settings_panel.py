"""The `/settings` panel every chat platform draws, from the feature registry.

Three screens, one control kind. The first picks a group of features, the second a
feature of it, the third sets that feature where the viewer may: for this thread, this
channel, the server, or an agent picked from a list. Each control is one choice
(`features.surface.parse_choice`) applied by `apply_choice`, which re-checks the presser.
A new registry key shows up here with no surface code.

A platform draws a `PanelScreen`: its selects, a way back and a notice. Pressing a
control or `open:<group>[:<key>[@<agent>]]` is `press`. Pure apart from the reads.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from typing import Final

from anthropic import AsyncAnthropic
from daimon.core.admin_actions import DelegatedAction
from daimon.core.channel_changes import ChannelChangeRefused
from daimon.core.config import Settings
from daimon.core.defaults.ma_index import list_agents_by_tenant
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME
from daimon.core.features.registry import DEFAULT, FEATURES, LOCKABLE, Category, FeatureSpec
from daimon.core.features.service import (
    FeatureActor,
    FeatureView,
    TierSetting,
    admin_word,
    change_feature,
    read_features,
    tier_word,
    where,
)
from daimon.core.features.service import change_refusal as feature_refusal
from daimon.core.features.snapshot import FeaturePlace
from daimon.core.features.surface import (
    CATEGORIES,
    change_note,
    own_text,
    parse_choice,
    source_text,
    tier_options,
)
from daimon.core.stores.identity import find_platform_principal
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

Sessions = async_sessionmaker[AsyncSession]
TITLE: Final = "Settings"
OPEN: Final = "open"
SELECT_LIMIT: Final = 25
"""The most options a select offers on every platform that has one."""


@dataclass(frozen=True)
class AgentChoice:
    id: str
    name: str


AgentLoader = Callable[[], Awaitable[Sequence[AgentChoice]]]


def tenant_agents(
    client: AsyncAnthropic, tenant_id: uuid.UUID, records: Sessions | None = None
) -> AgentLoader:
    """The agents a viewer may pick for the agent level: the workspace's own, by name."""

    async def load() -> Sequence[AgentChoice]:
        agents = await list_agents_by_tenant(client, tenant_id=tenant_id, records=records)
        named = [AgentChoice(a.id, a.metadata.get(MA_METADATA_KEY_NAME) or a.name) for a in agents]
        return sorted(named, key=lambda agent: agent.name.lower())

    return load


async def chat_actor(
    sessions: Sessions,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    user_id: str,
    admin_for: Callable[[DelegatedAction | None], Awaitable[bool]],
) -> FeatureActor:
    """A person pressing a chat control, recorded under their account (never created here)."""
    async with sessions() as session:
        found = await find_platform_principal(
            session, tenant_id=tenant_id, platform=platform, external_id=user_id
        )
    account = found.account_id if found is not None else None
    return FeatureActor(tenant_id, account, "panel", user_id, admin_for)


async def apply_choice(
    sessions: Sessions,
    settings: Settings,
    actor: FeatureActor,
    raw: object,
    place: FeaturePlace,
) -> tuple[bool, str] | None:
    """Apply a pressed choice (`features.surface.parse_choice`): ``(True, what changed)``,
    ``(False, why not)``, or None for a forged choice, which changes nothing and says nothing."""
    if (choice := parse_choice(raw)) is None:
        return None
    if choice.agent:
        place = replace(place, agent_id=choice.agent)
    try:
        change = await change_feature(
            sessions,
            settings,
            actor,
            key=choice.key,
            tier=choice.tier,
            value=choice.value,
            place=place,
            locked=choice.locked,
        )
    except ChannelChangeRefused as exc:
        return False, str(exc)
    return True, change_note(
        change, place.platform, overrides_active=settings.tool_safety.gate_overrides_active
    )


@dataclass(frozen=True)
class PanelOption:
    value: str
    label: str
    description: str = ""


@dataclass(frozen=True)
class PanelSelect:
    placeholder: str
    options: tuple[PanelOption, ...]


@dataclass(frozen=True)
class PanelScreen:
    title: str
    lines: tuple[str, ...]
    selects: tuple[PanelSelect, ...] = ()
    back: PanelOption | None = None
    notice: str | None = None
    ok: bool = True
    """False when `notice` says a change was refused."""


def open_op(category: str | None = None, key: str | None = None, agent: str | None = None) -> str:
    if category is None:
        return OPEN
    raw = f"{OPEN}:{category}" + (f":{key}" if key else "")
    return f"{raw}@{agent}" if agent else raw


def parse_open(raw: object) -> tuple[Category | None, str | None, str | None] | None:
    """``(group, key, agent)`` of an `open` op, or None when it names none of the registry's."""
    if not isinstance(raw, str):
        return None
    body, _, agent = raw.partition("@")
    head, *rest = body.split(":")
    if head != OPEN or len(rest) > 2 or (agent and len(rest) != 2):
        return None
    if not rest:
        return None, None, None
    category: Category | None = next((c for c in CATEGORIES if c == rest[0]), None)
    key = rest[1] if len(rest) == 2 else None
    if category is None or (
        key is not None and (key not in FEATURES or FEATURES[key].category != category)
    ):
        return None
    return category, key, agent or None


def _on_platform(place: FeaturePlace) -> list[FeatureSpec]:
    return [spec for spec in FEATURES.values() if spec.on_platform(place.platform)]


def _short(view: FeatureView, platform: str) -> str:
    resolved = view.resolved
    if resolved.unavailable is not None:
        return source_text(view.spec, resolved, platform)
    return view.spec.label(resolved.value) + (", locked" if resolved.locked else "")


def _root(place: FeaturePlace) -> PanelScreen:
    groups = {spec.category for spec in _on_platform(place)}
    here = "this thread" if place.thread_id else "this channel"
    return PanelScreen(
        TITLE,
        (f"Settings for {here}.", "Pick a group."),
        (
            PanelSelect(
                "Pick a group",
                tuple(
                    PanelOption(open_op(category), name)
                    for category, name in CATEGORIES.items()
                    if category in groups
                ),
            ),
        ),
    )


async def _category(
    sessions: Sessions,
    settings: Settings,
    actor: FeatureActor,
    place: FeaturePlace,
    category: Category,
) -> PanelScreen:
    specs = [spec for spec in _on_platform(place) if spec.category == category]
    views = await read_features(sessions, settings, place, keys=[s.key for s in specs])
    shorts = [(view.spec, _short(view, place.platform)) for view in views]
    return PanelScreen(
        CATEGORIES[category],
        tuple(f"{spec.name}: {short}" for spec, short in shorts),
        (
            PanelSelect(
                "Pick a setting",
                tuple(
                    PanelOption(open_op(category, spec.key), spec.name, short)
                    for spec, short in shorts[:SELECT_LIMIT]
                ),
            ),
        ),
        back=PanelOption(open_op(), "Back"),
    )


async def _feature(
    sessions: Sessions,
    settings: Settings,
    actor: FeatureActor,
    place: FeaturePlace,
    key: str,
    agent: str | None,
    agents: AgentLoader | None,
) -> PanelScreen:
    spec = FEATURES[key]
    if agent:
        place = replace(place, agent_id=agent)
    (view,) = await read_features(sessions, settings, place, keys=(key,))
    back = PanelOption(open_op(spec.category, key if agent else None), "Back")
    title = spec.name
    lines = [f"In effect: {source_text(spec, view.resolved, place.platform)}."]
    if view.resolved.unavailable is not None:
        return PanelScreen(title, tuple(lines), back=PanelOption(open_op(spec.category), "Back"))
    selects: list[PanelSelect] = []
    can_lock = await actor.admin_for(spec.authority.action) and not spec.authority.native
    for own in view.tiers:
        if own.tier == "deployment":
            continue
        word = tier_word(own.tier, place.platform).capitalize()
        lines.append(f"{word}: {own_text(spec, own)}{_overridden(view, own, place.platform)}.")
        if (own.tier == "agent") != bool(agent):
            continue
        if await feature_refusal(sessions, settings, actor, key=key, tier=own.tier, place=place):
            continue
        options = tier_options(
            spec, own.tier, own, can_lock=can_lock and own.tier in LOCKABLE, agent=agent
        )
        if options:
            choices = tuple(PanelOption(value, label) for value, label in options)
            selects.append(PanelSelect(f"Change for {where(own.tier, place.platform)}", choices))
    selects.reverse()
    if not agent and agents is not None and _agent_level(spec):
        picker = await _agent_picker(sessions, settings, actor, place, spec, agents)
        if picker is not None:
            selects.append(picker)
    if not selects:
        who = admin_word(place.platform)
        lines.append(f"Only {who} can change this. Ask {who}.")
    return PanelScreen(title, tuple(lines), tuple(selects), back)


def _overridden(view: FeatureView, own: TierSetting, platform: str) -> str:
    """`, overridden by the server` when another level decides and its value differs from
    this one's own, so a row never reads as in effect when it is not."""
    resolved = view.resolved
    if own.value in (DEFAULT, resolved.value) or resolved.tier in (own.tier, "default"):
        return ""
    by = "the server" if resolved.tier == "deployment" else where(resolved.tier, platform)
    return f", overridden by {by}"


def _agent_level(spec: FeatureSpec) -> bool:
    """Whether a chat person can set the feature for an agent: not a built-in tool, which is
    set where the agent is edited."""
    return "agent" in spec.tiers and not spec.key.startswith("tool.")


async def _agent_picker(
    sessions: Sessions,
    settings: Settings,
    actor: FeatureActor,
    place: FeaturePlace,
    spec: FeatureSpec,
    agents: AgentLoader,
) -> PanelSelect | None:
    refused = await feature_refusal(
        sessions,
        settings,
        actor,
        key=spec.key,
        tier="agent",
        place=FeaturePlace(actor.tenant_id, place.platform, agent_id=None),
    )
    if refused is not None:
        return None
    found = (await agents())[:SELECT_LIMIT]
    if not found:
        return None
    return PanelSelect(
        "Change for an agent",
        tuple(
            PanelOption(open_op(spec.category, spec.key, agent.id), agent.name) for agent in found
        ),
    )


async def show(
    sessions: Sessions,
    settings: Settings,
    actor: FeatureActor,
    place: FeaturePlace,
    op: object = OPEN,
    *,
    agents: AgentLoader | None = None,
) -> PanelScreen | None:
    """The screen an `open` op names, or None for an op that names none (a forged press)."""
    found = parse_open(op)
    if found is None:
        return None
    category, key, agent = found
    if category is None:
        return _root(place)
    if key is None:
        return await _category(sessions, settings, actor, place, category)
    if not FEATURES[key].on_platform(place.platform):
        return None
    return await _feature(sessions, settings, actor, place, key, agent, agents)


async def press(
    sessions: Sessions,
    settings: Settings,
    actor: FeatureActor,
    raw: object,
    place: FeaturePlace,
    *,
    agents: AgentLoader | None = None,
) -> PanelScreen | None:
    """A pressed control: open the screen it names, or apply the change and show the feature
    again with the result as its notice. None for a forged press, which says nothing."""
    if (choice := parse_choice(raw)) is None:
        return await show(sessions, settings, actor, place, raw, agents=agents)
    if not FEATURES[choice.key].on_platform(place.platform):
        return None
    done = await apply_choice(sessions, settings, actor, raw, place)
    if done is None:
        return None
    screen = await _feature(sessions, settings, actor, place, choice.key, choice.agent, agents)
    return replace(screen, notice=done[1], ok=done[0])


async def open_panel(
    sessions: Sessions,
    settings: Settings,
    actor: FeatureActor,
    place: FeaturePlace,
    *,
    agents: AgentLoader | None = None,
) -> PanelScreen:
    screen = await show(sessions, settings, actor, place, agents=agents)
    assert screen is not None
    return screen


__all__ = [
    "DEFAULT",
    "AgentChoice",
    "AgentLoader",
    "PanelOption",
    "PanelScreen",
    "PanelSelect",
    "apply_choice",
    "chat_actor",
    "open_op",
    "open_panel",
    "parse_open",
    "press",
    "show",
    "tenant_agents",
]
