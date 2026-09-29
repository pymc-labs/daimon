"""Scenario: a configuration change reaching a live thread through a real mention.

Every platform, through the real entry points (`DaimonBot.on_message`,
`SlackApp._handle_app_mention`, Teams' `/api/messages`), because the promise is user-facing: a key
saved between two messages is usable in the next one, and a model change moves
the task onto the new model instead of quietly billing the old one.

The mapping row each test seeds is what a previous turn left behind — the
drivers stub `create_session` (a boundary concern, see `drivers/`), so the
session a first mention would have created is written directly, including the
configuration it froze.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

import httpx
import pytest
from anthropic.types.beta.beta_file_scope import BetaFileScope
from anthropic.types.beta.file_metadata import FileMetadata
from anthropic.types.beta.sessions.beta_managed_agents_delete_session_resource import (
    BetaManagedAgentsDeleteSessionResource,
)
from anthropic.types.beta.sessions.beta_managed_agents_file_resource import (
    BetaManagedAgentsFileResource,
)
from daimon.core.credential_env import assemble_env_bytes
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.session_snapshot import (
    SessionSnapshot,
    desired_snapshot,
    fingerprint_identity,
    fingerprint_mutable,
    hash_env_bytes,
)
from daimon.core.stores import tenant_ledger, usage_events
from daimon.core.stores.agent_files import list_agent_files, put_agent_file
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.thread_sessions import (
    create_thread_session,
    get_live_thread_session,
    get_thread_session_by_id,
)
from daimon.testing import ma_agent
from daimon.testing.factories import make_tenant
from daimon.testing.ma import MARouter, list_response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import AGENT_ID, ENV_ID, MODEL_ID, build_turn_router
from .drivers.discord_driver import DiscordDriver
from .drivers.protocol import thread_ids
from .drivers.slack_driver import SlackDriver
from .drivers.teams_driver import TeamsDriver

# The id both drivers' `create_session` stub returns, i.e. the session any
# replacement lands on.
_REPLACEMENT_SESSION_ID = "sess_parity_test"
_LIVE_SESSION_ID = "sess_before_the_change"
_OLD_ENV_RESOURCE_ID = "res_env_old"


def _build_router(
    tenant_id: uuid.UUID,
    *,
    turn_session_id: str,
    model_id: str = MODEL_ID,
    resource_calls: list[tuple[str, str]] | None = None,
) -> MARouter:
    """Agent/environment resolution, one turn, and the `.env` swap endpoints.

    The event routes are registered for `turn_session_id` ONLY: a turn that
    ran anywhere else has no route and fails the test loudly, which is how
    these scenarios prove which session the turn actually used.
    """
    calls = resource_calls if resource_calls is not None else []
    router = build_turn_router(
        str(tenant_id), session_id=turn_session_id, model_id=model_id, fresh_event_ids=True
    )

    def _upload(request: httpx.Request, _match: object) -> httpx.Response:
        calls.append(("POST", "/v1/files"))
        meta = FileMetadata(
            id=f"file_{uuid.uuid4().hex[:12]}",
            created_at=datetime.now(UTC),
            filename=".env",
            mime_type="text/plain",
            size_bytes=len(request.content),
            type="file",
            downloadable=False,
            scope=BetaFileScope(id=turn_session_id, type="session"),
        )
        return httpx.Response(200, json=meta.model_dump(mode="json"))

    def _delete_resource(request: httpx.Request, _match: object) -> httpx.Response:
        resource_id = request.url.path.rsplit("/", 1)[-1]
        calls.append(("DELETE", resource_id))
        return httpx.Response(
            200,
            json=BetaManagedAgentsDeleteSessionResource(
                id=resource_id, type="session_resource_deleted"
            ).model_dump(mode="json"),
        )

    def _add_resource(request: httpx.Request, _match: object) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(("POST", str(body["mount_path"])))
        now = datetime.now(UTC)
        return httpx.Response(
            200,
            json=BetaManagedAgentsFileResource(
                id=f"res_{uuid.uuid4().hex[:12]}",
                created_at=now,
                file_id=str(body["file_id"]),
                mount_path=f"/mnt/session/uploads/{str(body['mount_path']).lstrip('/')}",
                type="file",
                updated_at=now,
            ).model_dump(mode="json"),
        )

    # A replacement first reads the OLD session's event log to decide whether a
    # checkpoint turn is worth spending. An empty log means nothing to carry,
    # so the successor is created directly and no billed checkpoint runs.
    router.add("GET", r"/v1/sessions/[^/]+/events", lambda req, _m: list_response([]))
    router.add("POST", r"/v1/files", _upload)
    router.add("DELETE", r"/v1/sessions/[^/]+/resources/[^/]+", _delete_resource)
    router.add("POST", r"/v1/sessions/[^/]+/resources", _add_resource)
    return router


def _snapshot(
    *,
    model_id: str = MODEL_ID,
    env_sha256: str | None = None,
    env_resource_id: str | None = None,
) -> SessionSnapshot:
    """What the session this row maps to froze when a previous turn created it."""
    snapshot = desired_snapshot(
        ma_agent(id=AGENT_ID, model=model_id),
        hidden_mcp_server_names=frozenset(),
        environment_id=ENV_ID,
        env_sha256=env_sha256,
        repo_url=None,
        repo_branch=None,
        memory_store_id=None,
        vault_id=None,
    )
    return snapshot.model_copy(update={"env_resource_id": env_resource_id})


async def _provision(
    session: AsyncSession,
    *,
    platform: Literal["discord", "slack", "teams"],
    workspace_id: str,
    user_id: str,
) -> tuple[uuid.UUID, uuid.UUID]:
    """A provisioned tenant with credit and a resolved caller. Returns (tenant, account)."""
    tenant = await make_tenant(session, platform=platform, workspace_id=workspace_id)
    await tenant_ledger.insert_entry(
        session,
        tenant_id=tenant.id,
        delta_usd=Decimal("100.00"),
        reason="trial",
        idempotency_key=f"trial:{tenant.id}",
    )
    principal = await get_or_create_platform_principal(
        session, tenant_id=tenant.id, platform=platform, external_id=user_id
    )
    await session.commit()
    return tenant.id, principal.account_id


async def _seed_row(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    platform: Literal["discord", "slack", "teams"],
    thread_id: str,
    ma_session_id: str,
    snapshot: SessionSnapshot,
    watermark: str,
) -> uuid.UUID:
    """The mapping row a previous turn left behind, with the config it froze."""
    row = await create_thread_session(
        session,
        tenant_id=tenant_id,
        platform=platform,
        thread_id=thread_id,
        account_id=account_id,
        ma_session_id=ma_session_id,
        ma_agent_id=AGENT_ID,
        watermark_message_id=watermark,
        effective_config=snapshot,
        identity_fingerprint=fingerprint_identity(snapshot),
        mutable_fingerprint=fingerprint_mutable(snapshot),
    )
    await session.commit()
    return row.id


async def _put_key(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    key: str,
    value: str,
) -> str:
    """Save a secret for the responder agent; return the `.env` hash that implies."""
    agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=AGENT_ID)
    async with sessionmaker() as session, session.begin():
        await put_agent_file(
            session,
            tenant_id=tenant_id,
            agent_id=agent_uuid,
            key=key,
            content=value,
            set_by_account_id=None,
        )
    async with sessionmaker() as session:
        rows = await list_agent_files(session, tenant_id=tenant_id, agent_id=agent_uuid)
    return hash_env_bytes(assemble_env_bytes(rows))


@pytest.mark.parametrize("platform", ["discord", "teams"])
async def test_a_key_saved_between_mentions_is_swapped_into_the_same_session(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    platform: Literal["discord", "teams"],
) -> None:
    workspace_id, user_id, _parent, thread_id = thread_ids(
        platform, workspace=900002101, user=555000321, thread=200100
    )

    tenant_id, account_id = await _provision(
        db_session, platform=platform, workspace_id=workspace_id, user_id=user_id
    )
    seeded_hash = await _put_key(db_session_factory, tenant_id=tenant_id, key="A", value="1")
    row_id = await _seed_row(
        db_session,
        tenant_id=tenant_id,
        account_id=account_id,
        platform=platform,
        thread_id=thread_id,
        ma_session_id=_LIVE_SESSION_ID,
        snapshot=_snapshot(env_sha256=seeded_hash, env_resource_id=_OLD_ENV_RESOURCE_ID),
        watermark="100",
    )

    calls: list[tuple[str, str]] = []
    router = _build_router(tenant_id, turn_session_id=_LIVE_SESSION_ID, resource_calls=calls)
    driver = DiscordDriver() if platform == "discord" else TeamsDriver()

    posted = await driver.dispatch_turn(
        sessionmaker=db_session_factory,
        router=router,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        channel_id=thread_id,
        user_id=user_id,
        text="first mention",
    )
    assert posted, "the unchanged-configuration turn must still answer"
    assert calls == [], "a session already running the caller's secrets is not touched"

    # The user saves a second key between the two mentions.
    new_hash = await _put_key(db_session_factory, tenant_id=tenant_id, key="B", value="2")

    posted = await driver.dispatch_turn(
        sessionmaker=db_session_factory,
        router=router,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        channel_id=thread_id,
        user_id=user_id,
        text="second mention",
    )
    assert posted, "the refreshed turn must answer on the same session"

    assert [method for method, _ in calls] == ["POST", "DELETE", "POST"], (
        "the swap is upload, then delete the old mount, then add the new one"
    )
    assert calls[1][1] == _OLD_ENV_RESOURCE_ID, "the delete must name the recorded resource"
    assert calls[2][1] == ".env", "and the new file must be mounted back at .env"

    live = await get_live_thread_session(
        db_session,
        tenant_id=tenant_id,
        platform=platform,
        thread_id=thread_id,
        account_id=account_id,
    )
    assert live is not None and live.id == row_id, (
        "a key change must never cost the caller their session"
    )
    assert live.ma_session_id == _LIVE_SESSION_ID
    assert live.effective_config is not None
    assert live.effective_config.env_sha256 == new_hash, (
        "the row must record the secrets the session now mounts"
    )

    rows = await usage_events.list_for_tenant(db_session, tenant_id=tenant_id)
    assert {row.managed_session_id for row in rows} == {_LIVE_SESSION_ID}, (
        "both turns ran, and were billed, on the one session"
    )


async def test_a_model_change_moves_a_slack_thread_onto_a_new_session_billed_at_the_new_model(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = "T_SESSION_REFRESH_PARITY"
    user_id = "U_SESSION_REFRESH_PARITY"
    thread_id = "9000006000.000001"

    tenant_id, account_id = await _provision(
        db_session, platform="slack", workspace_id=workspace_id, user_id=user_id
    )
    row_id = await _seed_row(
        db_session,
        tenant_id=tenant_id,
        account_id=account_id,
        platform="slack",
        thread_id=thread_id,
        ma_session_id=_LIVE_SESSION_ID,
        # The session froze haiku; the agent has since been moved to sonnet.
        snapshot=_snapshot(model_id="claude-haiku-4-5"),
        watermark="9000006000.000000",
    )

    router = _build_router(tenant_id, turn_session_id=_REPLACEMENT_SESSION_ID)
    driver = SlackDriver()

    posted = await driver.dispatch_turn(
        sessionmaker=db_session_factory,
        router=router,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        channel_id="C_SESSION_REFRESH_PARITY",
        user_id=user_id,
        text="carry on",
        thread_ts=thread_id,
    )
    assert posted, "the turn must run on the replacement session"

    old = await get_thread_session_by_id(db_session, id=row_id)
    assert old is not None and old.status == "superseded", (
        "the session frozen on the old model hands its task on"
    )
    live = await get_live_thread_session(
        db_session,
        tenant_id=tenant_id,
        platform="slack",
        thread_id=thread_id,
        account_id=account_id,
    )
    assert live is not None and live.id != row_id, "the caller is on a new session"
    assert live.ma_session_id == _REPLACEMENT_SESSION_ID
    assert live.predecessor_id == row_id, "and the new row names where the task came from"
    assert old.replaced_by_id == live.id

    rows = await usage_events.list_for_tenant(db_session, tenant_id=tenant_id)
    assert len(rows) == 1, "the replaced turn is billed once"
    assert rows[0].model == MODEL_ID, (
        "the turn must be billed at the model the session it ran on actually froze"
    )
    assert rows[0].managed_session_id == _REPLACEMENT_SESSION_ID


@pytest.mark.parametrize("platform", ["discord", "teams"])
async def test_a_model_change_moves_a_thread_onto_a_new_session_billed_at_the_new_model(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    platform: Literal["discord", "teams"],
) -> None:
    workspace_id, user_id, _parent, thread_id = thread_ids(
        platform, workspace=900002102, user=555000322, thread=200200
    )

    tenant_id, account_id = await _provision(
        db_session, platform=platform, workspace_id=workspace_id, user_id=user_id
    )
    row_id = await _seed_row(
        db_session,
        tenant_id=tenant_id,
        account_id=account_id,
        platform=platform,
        thread_id=thread_id,
        ma_session_id=_LIVE_SESSION_ID,
        # The session froze haiku; the agent has since been moved to sonnet.
        snapshot=_snapshot(model_id="claude-haiku-4-5"),
        watermark="200",
    )

    router = _build_router(tenant_id, turn_session_id=_REPLACEMENT_SESSION_ID)
    driver = DiscordDriver() if platform == "discord" else TeamsDriver()

    posted = await driver.dispatch_turn(
        sessionmaker=db_session_factory,
        router=router,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        channel_id=thread_id,
        user_id=user_id,
        text="carry on",
    )
    assert posted, "the turn must run on the replacement session"

    old = await get_thread_session_by_id(db_session, id=row_id)
    assert old is not None and old.status == "superseded", (
        "the session frozen on the old model hands its task on"
    )
    live = await get_live_thread_session(
        db_session,
        tenant_id=tenant_id,
        platform=platform,
        thread_id=thread_id,
        account_id=account_id,
    )
    assert live is not None and live.id != row_id, "the caller is on a new session"
    assert live.ma_session_id == _REPLACEMENT_SESSION_ID
    assert live.predecessor_id == row_id, "and the new row names where the task came from"
    assert old.replaced_by_id == live.id

    rows = await usage_events.list_for_tenant(db_session, tenant_id=tenant_id)
    assert len(rows) == 1, "the replaced turn is billed once"
    assert rows[0].model == MODEL_ID, (
        "the turn must be billed at the model the session it ran on actually froze"
    )
    assert rows[0].managed_session_id == _REPLACEMENT_SESSION_ID


async def test_a_key_saved_between_mentions_is_swapped_into_the_same_slack_session(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The Slack half of the `.env` swap — same contract, same copy surface."""
    workspace_id = "T_SESSION_REFRESH_ENV"
    user_id = "U_SESSION_REFRESH_ENV"
    thread_id = "9000007000.000001"

    tenant_id, account_id = await _provision(
        db_session, platform="slack", workspace_id=workspace_id, user_id=user_id
    )
    row_id = await _seed_row(
        db_session,
        tenant_id=tenant_id,
        account_id=account_id,
        platform="slack",
        thread_id=thread_id,
        ma_session_id=_LIVE_SESSION_ID,
        snapshot=_snapshot(env_sha256=None),
        watermark="9000007000.000000",
    )

    new_hash = await _put_key(db_session_factory, tenant_id=tenant_id, key="TOGGL", value="tok")

    calls: list[tuple[str, str]] = []
    router = _build_router(tenant_id, turn_session_id=_LIVE_SESSION_ID, resource_calls=calls)
    # The session had no `.env` at all, so there is nothing to delete: the
    # bind lists the session's resources to be sure, then mounts the new file.
    router.add("GET", r"/v1/sessions/[^/]+/resources", lambda req, _m: list_response([]))

    posted = await SlackDriver().dispatch_turn(
        sessionmaker=db_session_factory,
        router=router,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        channel_id="C_SESSION_REFRESH_ENV",
        user_id=user_id,
        text="use my key",
        thread_ts=thread_id,
    )
    assert posted, "the turn must answer on the same session"
    assert [method for method, _ in calls] == ["POST", "POST"], (
        "an agent that had no secrets mounts the new .env without deleting anything"
    )

    live = await get_thread_session_by_id(db_session, id=row_id)
    assert live is not None and live.status == "live", "a key change is not a replacement"
    assert live.effective_config is not None
    assert live.effective_config.env_sha256 == new_hash
    assert live.effective_config.env_resource_id is not None, (
        "the row must record the new mount, so the next key swap can delete it"
    )
