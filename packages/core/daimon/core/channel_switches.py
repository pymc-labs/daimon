"""Thread participation and progress plans at one scope, from any surface, recorded.

Both switches walk the same cascade (`daimon.core.scope_cascade`) under the same rules:
a thread's switch is any member's, a channel's or the workspace's an admin's, and
`disabled` is final below the scope that sets it. Participation runs on Discord and
Teams only. The chat tools and the web call `set_switch`; `switch_refusal` is its pure
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
    after_write,
)
from daimon.core.errors import StaleChannelValue
from daimon.core.scope_cascade import (
    CascadeMode,
    CascadeModes,
    CascadeScope,
    ResolvedCascade,
    resolve_above,
    resolve_modes,
    scope_label,
    tier_word,
)
from daimon.core.stores.channel_changes import WORKSPACE_ID, record_change
from daimon.core.stores.domain import ChannelSetting
from daimon.core.stores.switch_modes import Switch, get_switch_modes, set_switch_mode
from pydantic import JsonValue
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
_THREAD_SETTINGS: Final[dict[Switch, ChannelSetting]] = {
    "participation": "thread_participation",
    "progress_plans": "thread_progress_plans",
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


def _mode_json(mode: CascadeMode | None, thread_id: str | None) -> JsonValue:
    if thread_id is None:
        return None if mode is None else mode.value
    return None if mode is None else {"thread": thread_id, "mode": mode.value}


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
) -> SwitchChange:
    """Set (None: drop) the switch's mode at `target`; refused before any write.

    Turning a scope on under a `disabled` wider tier is refused, naming that tier.
    """
    refusal = switch_refusal(
        switch, actor, platform=platform, deployment=deployment, scope=target.scope, mode=mode
    )
    if refusal is not None:
        raise refusal
    async with sessionmaker.begin() as session:
        before_modes = await get_switch_modes(
            session,
            switch,
            tenant_id=actor.tenant_id,
            platform=platform,
            channel_id=target.channel_id,
            thread_id=target.thread_id,
        )
        before = own_mode(before_modes, target.scope)
        if expected is not None and expected.value != before:
            raise StaleChannelValue(switch, None if before is None else before.value)
        above = resolve_above(target.scope, deployment, before_modes)
        if mode is CascadeMode.ON and above.mode is CascadeMode.DISABLED:
            tier = tier_word(above.tier, platform)
            label = scope_label(target.scope, target.scope_id, platform)
            raise ChannelChangeRefused(
                f"{_NAMES[switch]} {'is' if switch == 'participation' else 'are'} disabled "
                f"at the {tier} level, so {label} cannot be turned on. "
                f"An admin would have to change the {tier} setting first.",
                kind="invalid",
                reason="disabled_above",
            )
        await set_switch_mode(
            session,
            switch,
            tenant_id=actor.tenant_id,
            platform=platform,
            scope=target.scope,
            scope_id=target.scope_id,
            mode=mode,
            parent_id=target.channel_id,
        )
        thread = target.scope is CascadeScope.THREAD
        await record_change(
            session,
            tenant_id=actor.tenant_id,
            platform=platform,
            channel_id=(
                WORKSPACE_ID
                if target.scope is CascadeScope.WORKSPACE
                else target.channel_id or target.scope_id or WORKSPACE_ID
            ),
            setting=_THREAD_SETTINGS[switch] if thread else switch,
            before=_mode_json(before, target.thread_id),
            after=_mode_json(mode, target.thread_id),
            actor_account_id=actor.account_id,
            surface=actor.surface,
        )
        await after_write(session, actor, before != mode)
        after_modes = await get_switch_modes(
            session,
            switch,
            tenant_id=actor.tenant_id,
            platform=platform,
            channel_id=target.channel_id,
            thread_id=target.thread_id,
        )
    return SwitchChange(before, mode, resolve_modes(deployment, after_modes))


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
