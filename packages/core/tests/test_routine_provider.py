"""A routine's fire is routed from the place admission admits it."""

from __future__ import annotations

from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.routine_provider import routine_provider
from daimon.core.stores import agent_backends, mux_state
from daimon.testing.factories import make_platform_principal, make_routine, make_tenant
from mux.contracts.config import DEFAULT_BACKEND
from mux.contracts.ids import ChannelRef, ThreadRef
from mux.contracts.resources import ProviderBinding
from mux.profiles import MANAGED_AGENTS
from sqlalchemy.ext.asyncio import AsyncSession


async def test_a_thread_that_kept_claude_keeps_its_routine_on_claude(
    db_session: AsyncSession,
) -> None:
    """Admission keeps the thread's Claude binding, so the agent's OpenAI preference
    must not route the fire elsewhere: it would be refused on every fire."""
    tenant = await make_tenant(db_session)
    principal = await make_platform_principal(
        db_session, platform="discord", external_id="owner", tenant=tenant
    )
    row = await make_routine(db_session, tenant=tenant, created_by_user_id="owner")
    row = row.model_copy(
        update={"destination_kind": "thread", "destination_id": "thread", "channel_id": "parent"}
    )
    await agent_backends.put_revision(
        db_session,
        tenant_id=tenant.id,
        agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=row.agent_id),
        local=1,
        backend="openai",
        model="gpt-5.5",
        profile="openai.persistent_workspace",
        digest="openai",
        previous_local=None,
        agent_name=row.agent_name,
        preview_digest="openai",
    )
    channel = ChannelRef(tenant_id=str(tenant.id), platform="discord", channel_id="parent")
    await mux_state.put_binding(
        db_session,
        ProviderBinding(
            id=f"binding-{tenant.id}",
            thread=ThreadRef(channel=channel, thread_id="thread"),
            provider=DEFAULT_BACKEND,
            profile=MANAGED_AGENTS.profile_id,
            native_refs={"session": "sesn-1"},
            generation=1,
            config_revision=0,
            legacy_account_id=str(principal.account_id),
        ),
        expected_generation=0,
    )
    provider = await routine_provider(db_session, row, platform="discord")
    assert provider == DEFAULT_BACKEND, f"the thread's kept Claude wins: {provider}"
