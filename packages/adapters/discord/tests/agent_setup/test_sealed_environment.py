"""An admin inside a private channel can still pick its environment from the Discord panel."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from daimon.adapters.discord.agent_setup import channel_environment as env
from daimon.adapters.discord.origin import interaction_origin
from daimon.core.access_policy import ChannelRule, TenantAccessPolicy
from daimon.core.authz import Action, Place, Subject, authorize
from daimon.testing.shared_helpers import Session as _Session

SEALED = TenantAccessPolicy(channel_rules={"123": ChannelRule(readers="own", writers="own")})


def _noargs_obj(*_: object) -> object:
    return object()


def _noargs_none(*_: object) -> None:
    return None


def _interaction(channel_id: int, parent_id: int | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        user=object(), channel_id=channel_id, channel=SimpleNamespace(parent_id=parent_id)
    )


def test_a_thread_is_inside_its_parent_channel() -> None:
    assert interaction_origin(_interaction(999, parent_id=123)) >= {"123"}  # pyright: ignore[reportArgumentType]


@pytest.mark.asyncio
@pytest.mark.parametrize(("channel_id", "allowed"), [(123, True), (456, False)])
async def test_the_picker_subject_is_inside_only_where_the_press_started(
    monkeypatch: pytest.MonkeyPatch, channel_id: int, allowed: bool
) -> None:
    async def live(*_: object, **__: object) -> Subject:
        return Subject(is_admin=True)

    monkeypatch.setattr(env, "load_live_subject", live)
    monkeypatch.setattr(env, "admin_caller", _noargs_obj)
    monkeypatch.setattr(env, "panel_tenant_id", _noargs_none)
    runtime = SimpleNamespace(sessionmaker=lambda: _Session())
    subject = await env.load_picker_subject(
        _interaction(channel_id),  # pyright: ignore[reportArgumentType]
        runtime=runtime,  # pyright: ignore[reportArgumentType]
        state=None,  # pyright: ignore[reportArgumentType]
        live=True,
    )
    decision = authorize(
        SEALED,
        subject=subject,
        action=Action.SET_CHANNEL_ENVIRONMENT,
        place=Place(channel_id="123"),
    )
    assert decision.allowed is allowed
