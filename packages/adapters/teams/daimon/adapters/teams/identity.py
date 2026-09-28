"""Verified Entra identity mapping for Teams personal-chat activities.

The resolver is the only place inbound activity fields are trusted: it
checks the activity is a genuine personal-chat message from the configured
Entra tenant, then maps it onto daimon identity — tenant derivation through
``ma_identity.derive_tenant_uuid`` and tenant readiness through
``stores.tenants``. The admitted user's platform principal is deliberately
NOT resolved here: ``admit()`` owns get-or-create, so a first-contact user
in a provisioned tenant is authorized like any guild or workspace member on
the other adapters.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast
from uuid import UUID

from daimon.adapters.teams.app import AuthorizedTeamsActivity
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.tenants import get_tenant
from microsoft_teams.api import MessageActivity  # pyright: ignore[reportMissingTypeStubs]
from microsoft_teams.apps.routing import (  # pyright: ignore[reportMissingTypeStubs]
    ActivityContext,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

PERSONAL_CHAT_ONLY_MESSAGE = "This agent is only available in a personal (1:1) chat."
DENIED_MESSAGE = "This agent is not available for this account."
INPUT_TOO_LONG_MESSAGE = "That message is too long for this agent. Please shorten it."
MAX_INBOUND_MESSAGE_BYTES = 16 * 1024


def _value(obj: object | None, name: str) -> object | None:
    if obj is None:
        return None
    if isinstance(obj, Mapping):
        return cast("Mapping[str, object]", obj).get(name)
    return getattr(obj, name, None)


def _canonical_uuid(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return str(UUID(value))
    except ValueError:
        return None


@dataclass(frozen=True)
class VerifiedTeamsTurnResolver:
    """Map an authenticated personal-chat ``MessageActivity`` into authorized facts.

    Fails closed (deny + ``None``) unless ALL of: the activity arrived on the
    ``msteams`` channel in a personal conversation (``is_group`` absent or
    False — Teams omits it on 1:1, and a contradictory flag fails closed);
    the conversation tenant AND channel-data tenant both equal the configured
    Entra tenant; the sender's ``aad_object_id`` is a well-formed UUID; ids
    and text are present; and the derived daimon tenant row exists, is
    ``platform="teams"``, is unarchived, and has ``provision_status="ready"``.
    """

    sessionmaker: async_sessionmaker[AsyncSession]
    entra_tenant_id: str
    denied_message: str = DENIED_MESSAGE

    async def __call__(
        self,
        ctx: ActivityContext[MessageActivity],
    ) -> AuthorizedTeamsActivity | None:
        activity = ctx.activity
        conversation = _value(activity, "conversation")
        channel_data = _value(activity, "channel_data")
        channel_tenant = _value(_value(channel_data, "tenant"), "id")
        conversation_tenant = _value(conversation, "tenant_id")
        aad_object_id = _canonical_uuid(_value(_value(activity, "from_"), "aad_object_id"))
        configured_tenant = _canonical_uuid(self.entra_tenant_id)
        conversation_id = _value(conversation, "id")
        activity_id = _value(activity, "id")
        text = _value(activity, "text")
        is_group = _value(conversation, "is_group")

        if _value(conversation, "conversation_type") != "personal":
            await ctx.send(PERSONAL_CHAT_ONLY_MESSAGE)
            return None

        verified = (
            configured_tenant is not None
            and _canonical_uuid(conversation_tenant) == configured_tenant
            and _canonical_uuid(channel_tenant) == configured_tenant
            and aad_object_id is not None
            and _value(activity, "channel_id") == "msteams"
            and (is_group is None or is_group is False)
            and isinstance(conversation_id, str)
            and bool(conversation_id.strip())
            and isinstance(activity_id, str)
            and bool(activity_id.strip())
            and isinstance(text, str)
            and bool(text.strip())
        )
        if not verified:
            await ctx.send(self.denied_message)
            return None

        assert configured_tenant is not None
        assert aad_object_id is not None
        assert isinstance(conversation_id, str)
        assert isinstance(activity_id, str)
        assert isinstance(text, str)

        normalized_text = text.strip()
        if len(normalized_text.encode("utf-8")) > MAX_INBOUND_MESSAGE_BYTES:
            await ctx.send(INPUT_TOO_LONG_MESSAGE)
            return None

        tenant_id = derive_tenant_uuid(platform="teams", workspace_id=configured_tenant)
        async with self.sessionmaker() as session:
            tenant = await get_tenant(session, tenant_id)

        if (
            tenant is None
            or tenant.platform != "teams"
            or tenant.external_id != configured_tenant
            or tenant.provision_status != "ready"
            or tenant.archived_at is not None
        ):
            await ctx.send(self.denied_message)
            return None

        return AuthorizedTeamsActivity(
            tenant_id=tenant_id,
            external_user_id=aad_object_id,
            conversation_id=conversation_id,
            activity_id=activity_id,
            message=normalized_text,
        )
