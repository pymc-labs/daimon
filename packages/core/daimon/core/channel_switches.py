"""Thread participation and progress plans at one scope, from any surface, recorded.

Both switches walk the same cascade (`daimon.core.scope_cascade`) under the same rules:
a thread's switch is any member's, a channel's or the workspace's an admin's, and
`disabled` is final below the scope that sets it. Participation runs on Discord and
Teams only. The chat tools and the web call `set_switch`, which writes through
`daimon.core.features.service.change_feature`; `switch_refusal` is its pure
first gate, which a tool runs before it looks a thread's parent up on the platform.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from daimon.core.channel_changes import (
    NEEDS_ADMIN,
    ChannelActor,
    ChannelChangeRefused,
    Expected,
)
from daimon.core.config import Settings
from daimon.core.errors import StaleChannelValue
from daimon.core.features.registry import DEFAULT, Tier
from daimon.core.features.resolver import as_cascade, from_mode
from daimon.core.features.service import FeatureActor, change_feature
from daimon.core.features.snapshot import FeaturePlace
from daimon.core.scope_cascade import CascadeMode, CascadeModes, CascadeScope, ResolvedCascade
from daimon.core.stores.channel_changes import WORKSPACE_ID
from daimon.core.stores.switch_modes import Switch
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

SWITCH_PLATFORMS: Final[dict[Switch, frozenset[str]]] = {
    "participation": frozenset({"discord", "teams"}),
    "progress_plans": frozenset({"discord", "slack", "teams"}),
}

_NAMES: Final[dict[Switch, str]] = {
    "participation": "Participation",
    "progress_plans": "Progress plans",
}
_WRONG_PLATFORM: Final[dict[Switch, str]] = {
    "participation": "Following threads isn't available here yet, so there is nothing to turn on.",
    "progress_plans": (
        "Progress plans show on the chat card, so there is nothing to turn on here."
    ),
}
_DEPLOYMENT_DISABLED: Final[dict[Switch, str]] = {
    "participation": (
        "Following is turned off for all of Daimon and cannot be turned on. "
        "I only reply when @mentioned; the operator would have to change that."
    ),
    "progress_plans": (
        "Progress plans are turned off for all of Daimon and cannot be turned on; "
        "the operator would have to change that."
    ),
}
_THREAD_DISABLED: Final[dict[Switch, str]] = {
    "participation": (
        "'disabled' is not a thread-level setting — use mode='off' to stop "
        "following this thread. 'disabled' locks a whole channel or everything here and "
        "is an admin action there."
    ),
    "progress_plans": (
        "'disabled' is not a thread-level setting — use mode='off' to stop showing "
        "a plan in this thread. 'disabled' locks a whole channel or everything here and "
        "is an admin action there."
    ),
}


@dataclass(frozen=True)
class SwitchTarget:
    """Where a switch is set: the scope, its row id, and the channel and thread it reads."""

    scope: CascadeScope
    scope_id: str | None
    channel_id: str | None
    """The channel tier the cascade reads: the channel, or a thread's parent once known."""
    thread_id: str | None = None

    @classmethod
    def workspace(cls) -> SwitchTarget:
        return cls(CascadeScope.WORKSPACE, None, None)

    @classmethod
    def channel(cls, channel_id: str) -> SwitchTarget:
        return cls(CascadeScope.CHANNEL, channel_id, channel_id)

    @classmethod
    def thread(cls, thread_id: str, parent_id: str | None) -> SwitchTarget:
        return cls(CascadeScope.THREAD, thread_id, parent_id, thread_id)


@dataclass(frozen=True)
class SwitchChange:
    before: CascadeMode | None
    """The scope's own mode before; None when it inherited."""
    after: CascadeMode | None
    effective: ResolvedCascade
    """What the whole cascade resolves to at the scope now."""


def own_mode(modes: CascadeModes, scope: CascadeScope) -> CascadeMode | None:
    """The scope's own explicit mode in a cascade read. Pure."""
    if scope is CascadeScope.WORKSPACE:
        return modes.workspace
    return modes.channel if scope is CascadeScope.CHANNEL else modes.thread


def switch_refusal(
    switch: Switch,
    actor: ChannelActor,
    *,
    platform: str,
    deployment: CascadeMode,
    scope: CascadeScope,
    mode: CascadeMode | None,
) -> ChannelChangeRefused | None:
    """Why the switch can't be set at `scope` before any read; None when it may. Pure."""
    if platform not in SWITCH_PLATFORMS[switch]:
        return ChannelChangeRefused(
            _WRONG_PLATFORM[switch], kind="invalid", reason="wrong_platform"
        )
    if deployment is CascadeMode.DISABLED:
        return ChannelChangeRefused(
            _DEPLOYMENT_DISABLED[switch], kind="invalid", reason="deployment_disabled"
        )
    subject = actor.subject
    if scope is not CascadeScope.THREAD and (not subject.is_admin or subject.via_agent_key):
        return ChannelChangeRefused(NEEDS_ADMIN, kind="forbidden", reason="admin_required")
    if scope is CascadeScope.THREAD and mode is CascadeMode.DISABLED:
        return ChannelChangeRefused(
            _THREAD_DISABLED[switch], kind="invalid", reason="thread_disabled"
        )
    return None


_TIERS: Final[dict[CascadeScope, Tier]] = {
    CascadeScope.WORKSPACE: "workspace",
    CascadeScope.CHANNEL: "channel",
    CascadeScope.THREAD: "thread",
}


def _as_mode(value: str, locked: bool) -> CascadeMode | None:
    if value == DEFAULT:
        return None
    return CascadeMode.DISABLED if locked and value == "off" else CascadeMode(value)


def _text(mode: CascadeMode | None) -> str:
    """A mode as `TierSetting.text`."""
    if mode is None:
        return DEFAULT
    value, locked = from_mode(mode)
    return f"{value}:locked" if locked else value


def _mode(text: str) -> str | None:
    found = _as_mode(text.removesuffix(":locked"), text.endswith(":locked"))
    return None if found is None else found.value


async def set_switch(
    sessionmaker: async_sessionmaker[AsyncSession],
    actor: ChannelActor,
    switch: Switch,
    *,
    platform: str,
    deployment: CascadeMode,
    target: SwitchTarget,
    mode: CascadeMode | None,
    expected: Expected[CascadeMode | None] | None = None,
    settings: Settings,
) -> SwitchChange:
    """Set (None: drop) the switch's mode at `target` through `change_feature`; refused
    before any write. Turning a scope on under a locked wider tier is refused."""
    refusal = switch_refusal(
        switch, actor, platform=platform, deployment=deployment, scope=target.scope, mode=mode
    )
    if refusal is not None:
        raise refusal
    thread = target.thread_id
    channel = target.channel_id
    if thread is not None and platform == "slack" and ":" in thread:
        channel, _, thread = thread.partition(":")
    place = FeaturePlace(actor.tenant_id, platform, channel, thread)
    value, locked = (DEFAULT, False) if mode is None else from_mode(mode)

    async def is_admin(_: object) -> bool:
        return actor.subject.is_admin

    try:
        change = await change_feature(
            sessionmaker,
            settings,
            FeatureActor(
                actor.tenant_id,
                actor.account_id,
                actor.surface,
                actor.subject.platform_user_id,
                is_admin,
                actor.subject.via_agent_key,
                actor.on_write,
            ),
            key=switch,
            tier=_TIERS[target.scope],
            value=value,
            place=place,
            locked=locked,
            expected=Expected(_text(expected.value)) if expected is not None else None,
        )
    except StaleChannelValue as exc:
        raise StaleChannelValue(switch, _mode(str(exc.current))) from exc
    except ChannelChangeRefused as exc:
        if exc.reason != "locked_above":
            raise
        raise ChannelChangeRefused(str(exc), kind=exc.kind, reason="disabled_above") from exc
    return SwitchChange(
        _as_mode(change.before.value, change.before.locked),
        mode,
        as_cascade(change.view.resolved),
    )


__all__ = [
    "SWITCH_PLATFORMS",
    "WORKSPACE_ID",
    "Switch",
    "SwitchChange",
    "SwitchTarget",
    "own_mode",
    "set_switch",
    "switch_refusal",
]
