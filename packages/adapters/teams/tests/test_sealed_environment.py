"""An admin inside a private channel can still pick its environment from the Teams panel."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from daimon.adapters.teams import channel_settings as cs
from daimon.adapters.teams.card_actions import Actor
from daimon.core.access_policy import ChannelRule, TenantAccessPolicy
from daimon.core.authz import Action, Place, Subject, authorize

CHANNEL = "19:abc@thread.tacv2"
SEALED = TenantAccessPolicy(channel_rules={CHANNEL: ChannelRule(readers="own", writers="own")})


class _Session:
    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("conversation", "allowed"),
    [(CHANNEL, True), (f"{CHANNEL};messageid=9", True), ("a:1personal", False)],
    ids=["in-channel", "in-thread", "one-to-one"],
)
async def test_the_dialog_subject_is_inside_only_where_the_click_started(
    monkeypatch: pytest.MonkeyPatch, conversation: str, allowed: bool
) -> None:
    async def live(*_: object, **__: object) -> Subject:
        return Subject(is_admin=True)

    monkeypatch.setattr(cs, "load_live_subject", live)
    dialog = cs.ChannelSettingsDialog(
        SimpleNamespace(sessionmaker=lambda: _Session()),  # pyright: ignore[reportArgumentType]
        channel_names=None,  # pyright: ignore[reportArgumentType]
    )
    actor = Actor("u1", uuid.uuid4(), True, conversation, True)
    subject = await dialog._subject(actor)  # pyright: ignore[reportPrivateUsage]
    decision = authorize(
        SEALED,
        subject=subject,
        action=Action.SET_CHANNEL_ENVIRONMENT,
        place=Place(channel_id=CHANNEL),
    )
    assert decision.allowed is allowed
