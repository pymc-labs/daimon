"""Private cards stay bound to the authenticated requester and original location."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock
from uuid import uuid4

import discord
import pytest
from daimon.adapters.discord.credential_origin import is_credential_interaction_valid
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.domain import CredentialRequestRow


@pytest.mark.parametrize(
    "mismatch",
    [
        None,
        "requester",
        "tenant",
        "platform",
        "parent",
        "thread",
        "card",
        "missing_modal_card",
        "missing_button_card",
    ],
)
@pytest.mark.parametrize("legacy", [False, True])
def test_credential_origin_rechecks_requester_install_and_stored_destination(
    mismatch: str | None, legacy: bool
) -> None:
    now = datetime.now(UTC)
    row = CredentialRequestRow(
        idempotency_key=uuid4(),
        token="request-token",
        kind="env",
        tenant_id=derive_tenant_uuid(platform="discord", workspace_id="111"),
        account_id=uuid4(),
        agent_id=uuid4(),
        target="API_KEY",
        mcp_server_url=None,
        used_at=None,
        requester_platform_user_id="42",
        channel_id="333",
        created_at=now,
        expires_at=now + timedelta(minutes=30),
        platform=None if legacy else "discord",
        parent_channel_id=None if legacy else "222",
        origin_thread_id=None if legacy else "333",
        posted_message_id=None if legacy else "444",
    )
    interaction = MagicMock(spec=discord.Interaction)
    interaction.user.id = 43 if mismatch == "requester" else 42
    interaction.guild_id = 112 if mismatch == "tenant" else 111
    interaction.channel_id = 334 if mismatch == "thread" else 333
    interaction.channel = MagicMock(spec=discord.Thread)
    interaction.channel.parent_id = 223 if mismatch == "parent" else 222
    interaction.message.id = 445 if mismatch == "card" else 444
    if mismatch in ("missing_modal_card", "missing_button_card"):
        interaction.message = None
        interaction.type = (
            discord.InteractionType.modal_submit
            if mismatch == "missing_modal_card"
            else discord.InteractionType.component
        )
    if mismatch == "platform" and not legacy:
        row = row.model_copy(update={"platform": "slack"})
    expected = mismatch in (None, "missing_modal_card") or (
        legacy and mismatch not in ("requester", "tenant")
    )
    assert is_credential_interaction_valid(interaction, row) is expected, (
        "card validation must reject changed authority while retaining verified legacy cards"
    )
