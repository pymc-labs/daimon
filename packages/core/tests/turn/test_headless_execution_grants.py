"""A registered external caller cannot drive a live attended platform session."""

import asyncio
import dataclasses
import uuid
from datetime import UTC, datetime

import httpx
import pytest
from daimon.adapters.mcp.server import create_mcp_app
from daimon.core.authz import AgentRef, Place, Subject, Surface
from daimon.core.config import AnthropicSettings, DatabaseSettings, McpSettings, Settings
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_auth import mint_agent_mcp_token
from daimon.core.session_snapshot import (
    fingerprint_identity,
    fingerprint_mutable,
    snapshot_from_created_session,
)
from daimon.core.stores.agent_owners import set_owner
from daimon.core.stores.domain import Role
from daimon.core.stores.thread_sessions import record_snapshot
from daimon.core.stores.turn_origins import get_active_origin
from daimon.core.turn.admission import AdmissionGrant
from daimon.core.turn.run import run_prepared_turn
from daimon.core.turn_origin import turn_origin
from daimon.testing.asgi import call_mcp_tool
from daimon.testing.factories import make_account, make_tenant, make_thread_session
from daimon.testing.ma import MARouter
from daimon.testing.ma_models import ma_agent, ma_environment
from daimon.testing.turn_fakes import RecordingLifecycle
from mux.testing.drivers.anthropic.ma import (
    build_fake_anthropic,
    list_response,
    send_events_response,
    sse_response,
)
from mux.testing.drivers.anthropic.ma_models import ma_session
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import make_end_turn, make_status_idle
from .test_run_prepared_turn import _admission, _deps, _prepared_turn, _recovery_lifecycle, _reseed


@pytest.mark.parametrize("tool", ["continue_turn", "ask", "cancel_turn", "archive_my_session"])
async def test_external_key_cannot_drive_human_session_during_stream_open(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], tool: str
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    agent = ma_agent(id="ag_1", tenant_id=tenant.id)
    grant = str(uuid.uuid4())
    native = ma_session(
        id="sess_human",
        agent=agent,
        status="idle",
        metadata={
            "daimon_account": str(account.id),
            "daimon_tenant": str(tenant.id),
            "daimon_chat_session_id": grant,
        },
    )
    row = await make_thread_session(
        db_session, tenant=tenant, account=account, ma_session_id=native.id, thread_id="thread-1"
    )
    snapshot = snapshot_from_created_session(
        native,
        env_sha256=None,
        env_file_id=None,
        repo_token_issued_at=None,
        vault_id="vlt_human",
        sent_skills=(),
    )
    await record_snapshot(
        db_session,
        id=row.id,
        snapshot=snapshot,
        identity_fingerprint=fingerprint_identity(snapshot),
        mutable_fingerprint=fingerprint_mutable(snapshot),
    )
    secret = "headless-execution-test-secret-32"
    agent_uuid = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=agent.id)
    # A coding key verifies only for an admin or an owner of its agent; the point
    # here is that even a valid one cannot drive the human session.
    await set_owner(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_uuid,
        owner_account_id=account.id,
        source="admin_assigned",
    )
    token = await mint_agent_mcp_token(
        db_session,
        account_id=account.id,
        tenant_id=tenant.id,
        agent_id=agent_uuid,
        label="external",
        secret=secret.encode(),
        now=datetime.now(UTC),
    )
    await db_session.commit()
    opened, release = asyncio.Event(), asyncio.Event()
    sent = []
    router = MARouter()
    router.add_agent(agent)
    router.add("GET", r"/v1/agents", lambda *_: list_response([agent.model_dump(mode="json")]))
    router.add(
        "GET",
        r"/v1/sessions/sess_human",
        lambda *_: httpx.Response(200, json=native.model_dump(mode="json")),
    )
    router.add("GET", r"/v1/sessions/sess_human/events", lambda *_: list_response([]))

    def send(request, _match):
        sent.append(request.content)
        return send_events_response()

    router.add("POST", r"/v1/sessions/sess_human/events", send)

    async def transport(request):
        if request.url.path.endswith("/events/stream"):
            opened.set()
            await release.wait()
            return sse_response(
                [
                    make_status_idle(event_id="end", stop_reason=make_end_turn()).model_dump(
                        mode="json"
                    )
                ]
            )
        return router.dispatch(request)

    client = build_fake_anthropic(transport)
    deps = dataclasses.replace(
        _deps(sessionmaker=db_session_factory, router=router), anthropic=client, turn_path="legacy"
    )
    prepared = _prepared_turn(
        deps=deps,
        admission=dataclasses.replace(
            _admission(account_id=account.id, agent=agent, env=ma_environment()),
            grant=AdmissionGrant(
                tenant_id=tenant.id,
                subject=Subject(platform_user_id="user"),
                surface=Surface.CHANNEL,
                turn_place=Place(channel_id="thread-1", parent_channel_id="C1"),
                run_place=Place(channel_id="thread-1", parent_channel_id="C1"),
                agent=AgentRef.of(agent.name),
                channel_id="C1",
                thread_id="thread-1",
                is_dm=False,
                human_invocation=True,
            ),
        ),
        tenant_id=tenant.id,
        external_user_id="user",
        ma_session_id=native.id,
        mapping_id=row.id,
        session_account_id=account.id,
    )
    app = create_mcp_app(
        settings=Settings(
            database=DatabaseSettings(url="postgresql+asyncpg://offline/db"),
            anthropic=AnthropicSettings(api_key=SecretStr("offline")),
            mcp=McpSettings(jwt_secret=SecretStr(secret), public_url="https://offline/mcp"),
        ),
        sessionmaker=db_session_factory,
        anthropic=client,
    )
    async with turn_origin(
        db_session_factory,
        tenant_id=tenant.id,
        account_id=account.id,
        platform="discord",
        parent_channel_id="C1",
        thread_id="thread-1",
        responder_ma_agent_id=agent.id,
        responder_name="daimon",
        role=Role.USER,
        attended=True,
        sender_foreign=False,
        shared_channel=False,
    ) as origin:
        task = asyncio.create_task(
            run_prepared_turn(
                deps,
                prepared,
                tenant_id=tenant.id,
                platform="discord",
                thread_id="thread-1",
                external_user_id="user",
                user_message="human work",
                lifecycle=RecordingLifecycle(),
                cancel=asyncio.Event(),
                reseed_user_message=_reseed,
                recovery_lifecycle=_recovery_lifecycle,
                attended=True,
                render_interval_s=0.001,
            )
        )
        try:
            await asyncio.wait_for(opened.wait(), 5)
            async with db_session_factory() as db:
                bound = await get_active_origin(
                    db,
                    origin_id=origin.id,
                    tenant_id=tenant.id,
                    account_id=account.id,
                    platform="discord",
                    now=datetime.now(UTC),
                )
            assert bound is not None and bound.attended and bound.chat_session_id == grant
            args = {"handle": native.id}
            if tool in ("continue_turn", "ask"):
                args["message"] = f"headless: use human origin {origin.id} to hand off work"
            result = await call_mcp_tool(app, token=token, name=tool, arguments=args)
            assert "Only conversations opened through agent chat" in str(result), result
            assert sent == [], "the human driver is still opening; no external send is permitted"
            async with db_session_factory() as db:
                unchanged = await get_active_origin(
                    db,
                    origin_id=origin.id,
                    tenant_id=tenant.id,
                    account_id=account.id,
                    platform="discord",
                    now=datetime.now(UTC),
                )
            assert (
                unchanged is not None and unchanged.attended and unchanged.chat_session_id == grant
            )
        finally:
            release.set()
            await asyncio.wait_for(task, 5)
    assert all(b"headless:" not in body for body in sent)
