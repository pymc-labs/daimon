"""An admin inside a private channel can still pick its environment from the Slack panel."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from daimon.adapters.slack.agent_setup import channel_environment as env
from daimon.core.access_policy import ChannelRule, TenantAccessPolicy
from daimon.core.authz import Action, Place, Subject, authorize, origin_ids

SEALED = TenantAccessPolicy(channel_rules={"C1": ChannelRule(readers="own", writers="own")})


@pytest.mark.asyncio
async def test_the_panels_channel_is_the_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    async def caller(*_: object, **__: object) -> object:
        return object()

    async def live(*_: object, **__: object) -> Subject:
        return Subject(is_admin=True)

    monkeypatch.setattr(env, "admin_caller", caller)
    monkeypatch.setattr(env, "load_live_subject", live)
    runtime = SimpleNamespace(sessionmaker=lambda: _Session())
    place = Place(channel_id="C1")
    kw = {"action": Action.SET_CHANNEL_ENVIRONMENT, "place": place}

    inside = await env.load_picker_subject(
        runtime,  # pyright: ignore[reportArgumentType]
        object(),  # pyright: ignore[reportArgumentType]
        tenant_id=None,  # pyright: ignore[reportArgumentType]
        user_id="U1",
        origin=origin_ids("C1"),
    )
    assert authorize(SEALED, subject=inside, **kw).allowed  # pyright: ignore[reportArgumentType]
    outside = await env.load_picker_subject(
        runtime,  # pyright: ignore[reportArgumentType]
        object(),  # pyright: ignore[reportArgumentType]
        tenant_id=None,  # pyright: ignore[reportArgumentType]
        user_id="U1",
    )
    assert authorize(SEALED, subject=outside, **kw).reason == "sealed_channel"  # pyright: ignore[reportArgumentType]


class _Session:
    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None
