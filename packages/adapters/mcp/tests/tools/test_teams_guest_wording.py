"""A person from another organisation is told no 1:1 chat is open to them.

Teams opens no 1:1 chat across organisations, so the tool results that point to one say
who the action is for instead. A local sender keeps the exact text it had.
"""

from __future__ import annotations

import re
import typing
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._words import setup_instruction
from daimon.adapters.mcp.tools.skill_uploads import (  # pyright: ignore[reportPrivateUsage]
    NoCardReason,
    _no_card_preview,
    _no_card_refusal,
    _sender_is_foreign,
)
from daimon.adapters.mcp.tools.teams._files import post_files
from daimon.adapters.mcp.tools.teams._send import (  # pyright: ignore[reportPrivateUsage]
    _post_teams_wizard_impl,
)
from daimon.core.stores.domain import Role
from daimon.core.stores.turn_origins import create_origin
from daimon.core.wizard.spec import Option, Step, StepKind, WizardSpec
from daimon.testing.factories import make_account, make_tenant
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

#: "1:1" as the chat, not inside a time such as "21:15".
_ONE_TO_ONE = re.compile(r"(?<![\d:])1:1(?![\d:])")


_HOST = "organisation that added Daimon"
_GROUP_CHAT = "19:group@thread.v2"
_SPEC = WizardSpec(
    prompt="Pick",
    steps=[
        Step(
            key="stage",
            question="Pick a stage",
            kind=StepKind.CHOICE,
            options=[Option(label="dev", value="dev")],
        )
    ],
)
_REASONS = typing.get_args(NoCardReason)


def _auth() -> AuthIdentity:
    return AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        role=Role.USER,
        platform="teams",
        external_id="entra",
        platform_user_id="aad",
    )


def test_setup_is_asked_of_the_host_organisation_for_a_guest() -> None:
    guest = setup_instruction("teams", foreign=True)

    assert guest == f"ask someone in the {_HOST} to send `setup`"
    assert setup_instruction("teams") == "send `setup` in a 1:1 chat with Daimon"


@pytest.mark.parametrize("reason", _REASONS)
def test_the_add_skill_fallbacks_send_a_guest_to_no_one_to_one_chat(reason: NoCardReason) -> None:
    for foreign in (False, True):
        refusal = _no_card_refusal("teams", reason, "helper", foreign=foreign)
        preview = _no_card_preview("teams", reason, "helper", foreign=foreign)
        for text in (refusal, preview):
            assert ("1:1 chat" in text) is not foreign, text
            assert (_HOST in text) is foreign, text


@pytest.mark.parametrize("foreign", [False, True])
async def test_a_form_refused_in_a_group_chat_names_a_one_to_one_chat_only_to_a_local_sender(
    foreign: bool,
) -> None:
    with (
        patch(
            "daimon.adapters.mcp.tools.teams._send.caller_is_foreign",
            new=AsyncMock(return_value=foreign),
        ),
        pytest.raises(ToolError) as refused,
    ):
        await _post_teams_wizard_impl(
            MagicMock(),
            _auth(),
            channel_id=_GROUP_CHAT,
            spec=_SPEC,
            short_id="s",
        )

    assert (_ONE_TO_ONE.search(str(refused.value)) is not None) is not foreign


@pytest.mark.parametrize("foreign", [False, True])
async def test_files_refused_in_a_group_chat_name_a_one_to_one_chat_only_to_a_local_sender(
    foreign: bool,
) -> None:
    with (
        patch(
            "daimon.adapters.mcp.tools.teams._files.caller_is_foreign",
            new=AsyncMock(return_value=foreign),
        ),
        pytest.raises(ToolError) as refused,
    ):
        await post_files(MagicMock(), _auth(), MagicMock(), _GROUP_CHAT, "hi", [])

    assert (_ONE_TO_ONE.search(str(refused.value)) is not None) is not foreign


async def _running_turn(
    sessionmaker: async_sessionmaker[AsyncSession], *, foreign: bool | None
) -> tuple[McpRuntime, AuthIdentity]:
    """A Teams person, with a running turn from a guest, a local sender, or none."""
    async with sessionmaker.begin() as session:
        tenant = await make_tenant(session, platform="teams", workspace_id=str(uuid.uuid4()))
        account = await make_account(session, tenant=tenant)
        if foreign is not None:
            now = datetime.now(UTC)
            await create_origin(
                session,
                tenant_id=tenant.id,
                account_id=account.id,
                platform="teams",
                parent_channel_id="19:c@thread.tacv2",
                thread_id="19:c@thread.tacv2;messageid=1",
                responder_ma_agent_id="agent_x",
                responder_name="daimon",
                configuration_target_ma_agent_id=None,
                configuration_target_name=None,
                role=Role.USER,
                shared_channel=False,
                expires_at=now + timedelta(minutes=10),
                now=now,
                sender_foreign=foreign,
            )
    auth = AuthIdentity(
        account_id=account.id,
        tenant_id=tenant.id,
        role=Role.USER,
        platform="teams",
        external_id="entra",
        platform_user_id="aad",
    )
    runtime = MagicMock()
    runtime.session_factory = sessionmaker
    return runtime, auth


@pytest.mark.parametrize(
    ("running", "known"),
    [(True, True), (False, False), (None, None)],
    ids=["guest", "local", "none"],
)
async def test_add_skill_without_an_origin_follows_the_persons_running_turn(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    running: bool | None,
    known: bool | None,
) -> None:
    runtime, auth = await _running_turn(committing_sessionmaker, foreign=running)

    foreign = await _sender_is_foreign(runtime, auth, None)  # pyright: ignore[reportPrivateUsage]
    refusal = _no_card_refusal("teams", "no_origin_context", "helper", foreign=foreign)
    preview = _no_card_preview("teams", "no_origin_context", "helper", foreign=foreign)

    assert foreign is known
    for text in (refusal, preview):
        assert ("1:1 chat" in text) is (running is False), text
        assert (_HOST in text) is (running is True), text
        if running is None:
            assert "open the setup panel" in text, "nothing is known: no chat is named"
