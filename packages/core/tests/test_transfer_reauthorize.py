"""Regression: a policy change during a session transfer applies before the successor exists.

From the independent review of the action-time re-check (model gap: decide-then-do).
"""

from dataclasses import replace

import pytest
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import Surface, build_agent_ref, build_subject, build_turn_place
from daimon.core.session_preparation import PreparedReplacement
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.turn.admission import AdmissionDenied, AdmissionGrant
from daimon.core.turn.prepare import bind_session
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy.ext.asyncio import async_sessionmaker

from .test_session_preparation import _NOW, _admission, _agent, _deps, _register, _Transport


@pytest.mark.parametrize("change", ["pin", "seal"])
async def test_policy_change_during_transfer_is_applied_before_successor(
    db_session, db_nullpool_engine, change
):
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    factory = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    transport = _Transport()
    deps = _deps(factory, transport)

    def admitted(agent):
        place = build_turn_place(channel_id="vault", thread_id="thread-1")
        grant = AdmissionGrant(
            tenant_id=tenant.id,
            subject=build_subject(is_admin=False, platform_user_id="user-1"),
            surface=Surface.CHANNEL,
            turn_place=place,
            agent=build_agent_ref(agent.name, agent.metadata, "daimon"),
            run_place=place,
            channel_id="vault",
            thread_id="thread-1",
            is_dm=False,
        )
        return replace(
            _admission(account=account, agent=agent),
            origin_channel_id="vault",
            origin_thread_id="thread-1",
            grant=grant,
        )

    async def bind(admission, transfer):
        return await bind_session(
            deps,
            admission,
            tenant_id=tenant.id,
            platform="discord",
            external_user_id="user-1",
            thread_id="thread-1",
            session_account_id=account.id,
            reuse_existing=True,
            transfer=transfer,
            now=lambda: _NOW,
        )

    async def no_transfer(**kwargs):
        raise AssertionError("initial create does not transfer")

    await bind(admitted(_agent()), no_transfer)
    moved = _agent(model_id="claude-opus-4-6")
    _register(transport.state, moved)
    before = transport.creates

    async def change_policy_during_transfer(**kwargs):
        policy = (
            TenantAccessPolicy(agent_channel_pins={"daimon": ("elsewhere",)})
            if change == "pin"
            else TenantAccessPolicy(sealed_channel_ids=("vault",))
        )
        async with factory() as session, session.begin():
            await set_access_policy(session, tenant_id=tenant.id, policy=policy)
        return PreparedReplacement(
            extra_resources=(),
            transfer_file_id=None,
            transfer_kind="transcript",
            user_prefix="previous work",
        )

    if change == "pin":
        with pytest.raises(AdmissionDenied):
            await bind(admitted(moved), change_policy_during_transfer)
        assert transport.creates == before
    else:
        second = await bind(admitted(moved), change_policy_during_transfer)
        assert second.admission.memory_read_only
        assert "vault" in second.admission.origin_seal_ids
        assert (
            transport.state.sessions[second.ma_session_id].metadata.get("daimon_sealed") == "vault"
        )
