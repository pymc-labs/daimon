"""J5 offline E2E (M06): "make me my own agent called latent-mine, then have it say hi here".

Latent audit M06: a member's new agent answered nowhere, and Daimon told them to fetch
an admin ("ask an admin to say: make latent-mine answer in #..."). The owner needs no
admin to talk to their own agent in their own conversation (#1425's owner hand-off):
the create result says so, the hand-off binds this thread to it, and the next message
here is answered by it, for its owner only. J5's member harness; a cheap model.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from daimon.adapters.mcp.tools.agents import (
    _create_agent_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.task_continuity import (
    _hand_off_task_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.specs import AgentSpec

from .test_j2_gemini_root_e2e import fresh_chat as fresh_chat
from .test_j5_owner_edits_e2e import MEMBER
from .test_j5_owner_edits_e2e import owner as owner

NAME = "latent-mine"


async def _create(chat, auth=None):
    return await _create_agent_impl(
        chat.runtime,
        auth or chat.auth,
        AgentSpec(name=NAME, model="claude-haiku-5-5", system="Say hi when asked."),
        origin_context_id=str(chat.origin.id),
    )


def _here(chat, account_id):
    from daimon.core.scope import ScopeContext

    origin = chat.origin
    return ScopeContext(
        tenant_id=chat.auth.tenant_id,
        channel_id=origin.parent_channel_id,
        account_id=account_id,
        platform=origin.platform,
        thread_id=origin.thread_id,
    )


@MEMBER
async def test_j5_the_owner_has_their_new_agent_say_hi_here_without_an_admin(owner):
    from daimon.core.errors import UserFacingError
    from daimon.core.stores.scoped_config_read import resolve
    from daimon.core.stores.tenants import get_tenant
    from daimon.core.stores.thread_agent_bindings import get_binding
    from daimon.testing.factories import make_account

    chat = owner
    info = await _create(chat)
    # 1. The create result offers the owner's own way to talk to it, not an admin.
    assert info.owned_by_you is True
    assert info.answering is not None
    assert "It's yours, so you can talk to it here without an admin" in info.answering
    assert f'"hand this conversation to {NAME}"' in info.answering
    assert "An admin can ask Daimon to make it answer in a channel." not in info.answering
    # 2. "have it say hi here": this conversation is handed to it, no admin step.
    handed = await _hand_off_task_impl(
        chat.runtime,
        chat.auth,
        origin_context_id=str(chat.origin.id),
        agent_id=info.id,
        continuation="say hi",
    )
    assert handed.destination_ma_agent_id == info.id
    origin = chat.origin
    async with chat.runtime.session_factory() as session:
        binding = await get_binding(
            session,
            tenant_id=chat.auth.tenant_id,
            platform=origin.platform,
            parent_channel_id=origin.parent_channel_id,
            thread_id=origin.thread_id,
        )
        assert binding is not None and binding.kind == "owner"
        # 3. The next message here is answered by latent-mine, for its owner only.
        mine = await resolve(
            session,
            context=_here(chat, chat.auth.account_id),
            default=chat.runtime.deployment_default,
        )
        assert mine.responder_ma_agent_id == info.id
        other = await make_account(session, tenant=await get_tenant(session, chat.auth.tenant_id))
        await session.commit()
        with pytest.raises(UserFacingError, match="private copy"):
            await resolve(
                session, context=_here(chat, other.id), default=chat.runtime.deployment_default
            )


@MEMBER
async def test_j5_outside_a_conversation_the_note_stays_the_admin_route(owner):
    """No chat turn (a CLI or panel credential): nothing here to hand over."""
    info = await _create(owner, auth=replace(owner.auth, chat_agent_id=None))
    assert info.answering is not None
    assert "It's yours" not in info.answering
    assert "An admin can ask Daimon to make it answer in a channel." in info.answering
