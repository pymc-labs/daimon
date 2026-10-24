"""Operator creation binds the only recipient to a live tenant owner."""

from io import StringIO
from types import SimpleNamespace

import pytest
from daimon.adapters.cli.commands.routines import run_create_dm_routine
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME, MA_METADATA_KEY_TENANT
from daimon.core.errors import UserFacingError
from daimon.core.membership import CachedMembershipResolver, Membership
from daimon.core.stores.direct_messages import set_dm_enabled
from daimon.core.stores.routines import list_routines_for_tenant
from daimon.testing import ma_agent
from daimon.testing.factories import make_tenant
from daimon.testing.ma import MARouter
from mux.testing.drivers.anthropic.ma import list_response
from rich.console import Console

from ..harness import build_cli_runtime


@pytest.mark.parametrize("dry_run", [True, False])
async def test_create_dm_verifies_owner_and_dry_run_does_not_write(db_session_factory, dry_run):
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session, platform="discord", workspace_id="424242")
        await set_dm_enabled(session, tenant_id=tenant.id, enabled=True)
    agent = ma_agent(
        id="ag",
        name="daimon",
        metadata={
            MA_METADATA_KEY_NAME: "daimon",
            MA_METADATA_KEY_TENANT: str(tenant.id),
        },
    )
    router = MARouter()
    router.add(
        "GET", r"/v1/agents", lambda _req, _m: list_response([agent.model_dump(mode="json")])
    )
    rt = build_cli_runtime(
        db_session_factory, router=router, settings=SimpleNamespace(direct_message_policies={})
    )
    calls = []

    async def member(tenant_id, guild_id, user_id):
        calls.append((tenant_id, guild_id, user_id))
        return Membership(is_member=True)

    output = StringIO()
    await run_create_dm_routine(
        rt=rt,
        console=Console(file=output),
        tenant_id=tenant.id,
        owner="123",
        agent_name="daimon",
        schedule="0 9 * * *",
        timezone="UTC",
        trigger="summarize",
        source_channel=None,
        dry_run=dry_run,
        resolver=CachedMembershipResolver(member),
    )
    async with db_session_factory() as session:
        rows = await list_routines_for_tenant(session, tenant_id=tenant.id)
    assert len(rows) == (0 if dry_run else 1)
    assert calls == [(tenant.id, "424242", "123")]
    assert "Owner / recipient: 123; budget source: tenant/account" in output.getvalue()
    if rows:
        assert rows[0].destination_id == rows[0].created_by_user_id == "123"


async def test_create_dm_unknown_membership_fails_closed(db_session_factory):
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session, platform="discord", workspace_id="424242")
        await set_dm_enabled(session, tenant_id=tenant.id, enabled=True)
    rt = build_cli_runtime(db_session_factory, settings=SimpleNamespace(direct_message_policies={}))

    async def unknown(*args):
        return Membership()

    with pytest.raises(UserFacingError, match="verified"):
        await run_create_dm_routine(
            rt=rt,
            console=Console(file=StringIO()),
            tenant_id=tenant.id,
            owner="123",
            agent_name="daimon",
            schedule="0 9 * * *",
            timezone="UTC",
            trigger="summarize",
            source_channel=None,
            dry_run=True,
            resolver=CachedMembershipResolver(unknown),
        )


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize(
    "case",
    [
        "foreign",
        "missing",
        "nonnumeric",
        "unsupported",
        "parent_foreign",
        "parent_restricted",
        "thread_restricted",
        "valid_thread",
    ],
)
async def test_source_is_verified_and_sealed_before_cli_save(db_session_factory, dry_run, case):
    import httpx
    from daimon.core.access_policy import ChannelRule, TenantAccessPolicy
    from daimon.core.stores.access_policy import set_access_policy
    from pydantic import SecretStr

    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session, platform="discord", workspace_id="424242")
        await set_dm_enabled(session, tenant_id=tenant.id, enabled=True)
        restriction = {"parent_restricted": "456", "thread_restricted": "789"}.get(case)
        if restriction:
            await set_access_policy(
                session,
                tenant_id=tenant.id,
                policy=TenantAccessPolicy(
                    channel_rules={restriction: ChannelRule(readers="inside")}
                ),
            )
    agent = ma_agent(
        id="ag",
        name="daimon",
        metadata={MA_METADATA_KEY_NAME: "daimon", MA_METADATA_KEY_TENANT: str(tenant.id)},
    )
    router = MARouter()
    router.add(
        "GET", r"/v1/agents", lambda _req, _m: list_response([agent.model_dump(mode="json")])
    )
    rt = build_cli_runtime(
        db_session_factory,
        router=router,
        settings=SimpleNamespace(
            direct_message_policies={}, discord=SimpleNamespace(bot_token=SecretStr("offline"))
        ),
    )
    requests = []

    def channel(request):
        requests.append(request.url.path)
        if case == "missing":
            return httpx.Response(404)
        if request.url.path.endswith("/456"):
            return httpx.Response(
                200,
                json={
                    "id": "456",
                    "guild_id": "999" if case == "parent_foreign" else "424242",
                    "type": 0,
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "789",
                "guild_id": "999" if case == "foreign" else "424242",
                "type": 2 if case == "unsupported" else 11,
                "parent_id": "456",
            },
        )

    async def member(*args):
        return Membership(is_member=True)

    async def run():
        await run_create_dm_routine(
            rt=rt,
            console=Console(file=StringIO()),
            tenant_id=tenant.id,
            owner="123",
            agent_name="daimon",
            schedule="0 9 * * *",
            timezone="UTC",
            trigger="summary",
            source_channel="abc" if case == "nonnumeric" else "789",
            dry_run=dry_run,
            resolver=CachedMembershipResolver(member),
            source_transport=httpx.MockTransport(channel),
        )

    if case == "valid_thread":
        await run()
    else:
        with pytest.raises(UserFacingError):
            await run()
    async with db_session_factory() as session:
        rows = await list_routines_for_tenant(session, tenant_id=tenant.id)
    assert len(rows) == (1 if case == "valid_thread" and not dry_run else 0)
    if rows:
        assert (rows[0].channel_id, rows[0].source_thread_id) == ("456", "789")
    if case == "nonnumeric":
        assert requests == []
