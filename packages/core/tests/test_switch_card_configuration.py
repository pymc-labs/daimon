"""Pressing Daimon's own card for a configuration change (fleet, allowlist, channel pin).

Every refusal at the press changes nothing and says so; only the requester's press
on its own message counts, and it re-runs the Claude card's own checks with the
presser's current role.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

from daimon.core.channel_backend import channel_ref, current_backend, set_channel_backend
from daimon.core.config import TurnSettings
from daimon.core.fleet_configuration import (
    ConfigurationReference,
    channel_pin_digest,
    configured_fleet_grant,
    provider_preview_digest,
)
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import agent_owners
from daimon.core.stores.backend_switch_cards import create_card, mark_posted, waiting_for
from daimon.core.switch_cards import press_switch_card
from mux.contracts.config import BackendConfig

from .test_backend_switch_confirmation import owned_preview as owned_preview
from .test_fleet_configuration import config

NOW = datetime.now(UTC)
CHANNEL = "pinned-channel"


async def _card(owned_preview, reference: ConfigurationReference, *, client: bool = False):
    _, agent, actor, sessions, *_ = owned_preview
    async with sessions.begin() as session:
        card = await create_card(
            session,
            tenant_id=agent.tenant_id,
            account_id=actor.account_id,
            platform="discord",
            channel_id=CHANNEL,
            thread_id="thread-1",
            setup_thread=False,
            requester_platform_user_id="requester",
            client=client,
            answering_agent_name="Researcher",
            reference=reference.model_dump(mode="json"),
            expires_at=NOW + timedelta(minutes=10),
        )
        await mark_posted(session, card_id=card.id, message_id="posted-1")
    return card


def _press(owned_preview, card_id, *, admin: bool, clicker: str = "requester"):
    _, agent, _, sessions, client, *_ = owned_preview
    return press_switch_card(
        deps=None,
        sessionmaker=sessions,
        anthropic=client,
        turn_settings=TurnSettings(path="mux", channel_backends=True),
        deployment_default=DeploymentDefault(agent_name="daimon"),
        tenant_id=agent.tenant_id,
        card_id=card_id,
        action="confirm",
        clicker=clicker,
        current_admin=AsyncMock(return_value=admin),
        place_word="server",
        now=datetime.now(UTC),
        message_id="posted-1",
    )


async def _allowlist_card(owned_preview, *, client: bool = False):
    service, agent, *_ = owned_preview
    policy = await service.tenant_policy(agent.tenant_id)
    reference = ConfigurationReference.model_validate(
        {
            "operation": "provider_allowlist",
            "providers": ("anthropic",),
            "expected_revision": policy.revision,
            "preview_digest": provider_preview_digest(policy, ("anthropic",)),
        }
    )
    return await _card(owned_preview, reference, client=client)


async def test_only_the_requester_can_press_a_configuration_card(owned_preview):
    service, agent, *_ = owned_preview
    card = await _allowlist_card(owned_preview)
    assert await _press(owned_preview, card.id, admin=True, clicker="someone-else") is None
    assert "openai" in (await service.tenant_policy(agent.tenant_id)).enabled_backends


async def test_an_admin_demoted_before_pressing_changes_nothing(owned_preview):
    service, agent, *_ = owned_preview
    card = await _allowlist_card(owned_preview)
    press = await _press(owned_preview, card.id, admin=False)
    assert press is not None and not press.applied
    assert press.text.endswith("Nothing changed."), press.text
    assert "openai" in (await service.tenant_policy(agent.tenant_id)).enabled_backends


async def test_a_requester_from_another_organisation_is_never_an_admin_at_the_press(
    owned_preview,
):
    service, agent, *_ = owned_preview
    card = await _allowlist_card(owned_preview, client=True)
    press = await _press(owned_preview, card.id, admin=True)
    assert press is not None and not press.applied
    assert press.text.endswith("Nothing changed."), press.text
    assert "openai" in (await service.tenant_policy(agent.tenant_id)).enabled_backends


async def test_a_non_admin_owner_cannot_raise_fleet_limits_over_the_owner_cap(owned_preview):
    service, agent, actor, sessions, client, trace, payload, reply, _ = owned_preview
    async with sessions.begin() as session:
        await agent_owners.set_owner(
            session,
            tenant_id=agent.tenant_id,
            agent_id=agent.agent_id,
            owner_account_id=actor.account_id,
            source="created",
        )
    value = config(max_workers=8, expires_at=NOW + timedelta(days=90))
    grant = await configured_fleet_grant(value, service, client, sessions, agent, now=NOW)
    preview = await service.preview_fleet_grant(agent, grant, now=NOW)
    reference = ConfigurationReference.model_validate(
        {
            "operation": "fleet_set",
            "agent_name": "Researcher",
            "config": value.model_dump(mode="json"),
            "expected_revision": preview.setting_revision,
            "preview_digest": preview.digest,
        }
    )
    card = await _card(owned_preview, reference)
    trace.queue(reply())
    press = await _press(owned_preview, card.id, admin=False)
    assert press is not None and not press.applied
    # The specific reason once #1492 lands; the generic refusal before it.
    assert "Only an admin can raise fleet limits" in press.text or "permission" in press.text
    assert press.text.endswith("Nothing changed."), press.text
    assert await service.read_fleet_grant(agent.tenant_id, agent.agent_id, now=NOW) is None


async def _pin_card(owned_preview):
    _, agent, _, sessions, *_ = owned_preview
    here = channel_ref(agent.tenant_id, "discord", CHANNEL)
    async with sessions.begin() as session:
        pin = await set_channel_backend(
            session,
            here,
            BackendConfig(
                backend="openai", profile="openai.persistent_workspace", model="gpt-6-luna"
            ),
        )
    reference = ConfigurationReference.model_validate(
        {
            "operation": "channel_pin_clear",
            "expected_revision": pin.local,
            "preview_digest": channel_pin_digest(pin),
        }
    )
    return here, await _card(owned_preview, reference)


async def test_an_admin_clears_this_channels_pin_through_the_card(owned_preview):
    _, _, _, sessions, *_ = owned_preview
    here, card = await _pin_card(owned_preview)
    press = await _press(owned_preview, card.id, admin=True)
    assert press is not None and press.applied, press
    async with sessions() as session:
        latest = await current_backend(session, here)
    assert latest is None or latest.backend != "openai"


async def test_a_non_admins_pin_press_changes_nothing(owned_preview):
    _, _, _, sessions, *_ = owned_preview
    here, card = await _pin_card(owned_preview)
    press = await _press(owned_preview, card.id, admin=False)
    assert press is not None and not press.applied
    assert press.text.endswith("Nothing changed."), press.text
    async with sessions() as session:
        latest = await current_backend(session, here)
    assert latest is not None and latest.backend == "openai"


async def test_a_waiting_pin_card_holds_only_its_own_channel(owned_preview):
    _, agent, _, sessions, *_ = owned_preview
    await _pin_card(owned_preview)
    async with sessions() as session:

        async def waiting(channel: str) -> bool:
            return await waiting_for(
                session,
                tenant_id=agent.tenant_id,
                requester_platform_user_id="requester",
                matching={"operation": "channel_pin_clear"},
                channel_id=channel,
                now=datetime.now(UTC),
            )

        assert await waiting(CHANNEL) is True
        assert await waiting("another-channel") is False
