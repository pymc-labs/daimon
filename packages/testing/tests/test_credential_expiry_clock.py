"""Real expiry poll/card acknowledgement under the explicit offline opt-in profile.

These owned-DB component controls do not certify native credential-target ingress
or whole NEW21 source execution. No button click or synthetic expiry edit occurs.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import discord
import pytest
import pytest_asyncio
from daimon.adapters.discord.posted_controls import build_card_view, edit_expired_posted_card
from daimon.core.credential_requests import mint_request_token
from daimon.core.posted_controls import card_for_request
from daimon.core.posted_controls.cards import EXPIRED_HEADLINE
from daimon.core.routine_delivery import DeliveryOutcome
from daimon.core.stores import credential_requests as requests
from daimon.core.stores.domain import CredentialRequestRow, RoutineRow
from daimon.testing.db import build_test_engine, require_owned_sessionmaker
from daimon.testing.discord_surface import DiscordSurfaceHarness
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.routine_clock import (
    ClockedRoutineJobs,
    CredentialFormExpiryBinding,
    LongWaitUnavailable,
    RoutineJobClock,
)
from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

if TYPE_CHECKING:
    from discord.types.message import Message as MessagePayload


ANCHOR = datetime(2026, 10, 11, tzinfo=UTC)


class NoCaps:
    async def is_over_cap(self, tenant_id: uuid.UUID, user_id: str) -> bool:
        raise RuntimeError("expiry must not execute routine admission")


async def no_fire(row: RoutineRow) -> None:
    raise RuntimeError("expiry must not execute routine fire/native factory")


async def no_post(row: RoutineRow) -> DeliveryOutcome:
    raise RuntimeError("expiry must not post routine deliveries")


@dataclass(frozen=True)
class ExpiryContext:
    sessions: async_sessionmaker[AsyncSession]
    row: CredentialRequestRow
    harness: DiscordSurfaceHarness
    jobs: ClockedRoutineJobs
    clock: RoutineJobClock


def admit_expiry_fixture_database(url: str, schema: str) -> None:
    """Authorize the configured destination without requesting a DB fixture."""
    probe = build_test_engine(url, schema)
    try:
        try:
            require_owned_sessionmaker(probe)
        except ValueError:
            if (
                probe.url.get_backend_name() == "postgresql"
                and probe.url.host in ("localhost", "127.0.0.1")
                and not probe.url.query
                and probe.url.database == "daimon_test"
                and not (
                    os.environ.get("CI") == "true" and os.environ.get("GITHUB_ACTIONS") == "true"
                )
            ):
                pytest.skip(
                    "expiry clock refuses plain local daimon_test; use an owned lane database"
                )
            raise
    finally:
        # This unconnected policy probe owns no async connections.
        probe.sync_engine.dispose()


@pytest.fixture
def expiry_engine(db_preflight, request: pytest.FixtureRequest, db_schema: str) -> AsyncEngine:
    url = os.environ.get("DAIMON_DATABASE__TEST_URL")
    if not url:
        raise ValueError("Expiry fixtures require a configured owned test database")
    admit_expiry_fixture_database(url, db_schema)
    # Dynamic resolution is deliberately synchronous and strictly after admission.
    engine = cast(AsyncEngine, request.getfixturevalue("db_engine"))
    require_owned_sessionmaker(engine)
    request.getfixturevalue("db_clean")
    return engine


@pytest.mark.parametrize("ci", [False, True])
def test_fixture_preflight_retains_owned_lane_and_registered_ci_policy(
    monkeypatch: pytest.MonkeyPatch, ci: bool
) -> None:
    monkeypatch.setenv("CI", "true" if ci else "false")
    monkeypatch.setenv("GITHUB_ACTIONS", "true" if ci else "false")
    schema = f"test_w{os.getpid()}_1234abcd"
    admit_expiry_fixture_database("postgresql+asyncpg://localhost/daimon_test_nc_N5", schema)
    if ci:
        admit_expiry_fixture_database("postgresql+asyncpg://localhost/daimon_test", schema)
        with pytest.raises(ValueError, match="owned"):
            admit_expiry_fixture_database("postgresql+asyncpg://localhost/daimon_test", "public")
    else:
        with pytest.raises(pytest.skip.Exception, match="plain local"):
            admit_expiry_fixture_database("postgresql+asyncpg://localhost/daimon_test", schema)
    with pytest.raises(ValueError, match="owned"):
        admit_expiry_fixture_database("postgresql+asyncpg://remote.invalid/daimon_test", schema)


@pytest.mark.parametrize(
    ("url", "skipped"),
    [
        ("postgresql+asyncpg://localhost/daimon_test", True),
        ("postgresql+asyncpg://remote.invalid/daimon_test_nc_N5", False),
    ],
)
def test_fixture_preflight_precedes_connections_sweep_and_cleanup(
    tmp_path: Path, url: str, skipped: bool
) -> None:
    trap = tmp_path / "forbidden-fixture-step"
    plugin = tmp_path / "n5_expiry_connection_trap.py"
    plugin.write_text(
        "from pathlib import Path\n"
        "from sqlalchemy import event\n"
        "from daimon.testing import db\n"
        f"trap = Path({str(trap)!r})\n"
        "def forbid(*args, **kwargs):\n"
        "    trap.write_text('connection or destructive fixture reached')\n"
        "    raise RuntimeError('N5_CONNECTION_OR_CLEANUP_TRAP')\n"
        "async def forbid_async(*args, **kwargs):\n"
        "    forbid()\n"
        "def pytest_sessionstart(session):\n"
        "    original = db.build_test_engine\n"
        "    def build(*args, **kwargs):\n"
        "        engine = original(*args, **kwargs)\n"
        "        event.listen(engine.sync_engine, 'do_connect', forbid)\n"
        "        return engine\n"
        "    db.build_test_engine = build\n"
        "    db.sweep_orphan_schemas = forbid_async\n"
        "    db._create_schema_with_tables = forbid_async\n"
        "    db.truncate_all = forbid_async\n"
    )
    environment = dict(os.environ)
    environment["DAIMON_DATABASE__TEST_URL"] = url
    environment["PYTHONPATH"] = str(tmp_path)
    environment.pop("GITHUB_ACTIONS", None)
    environment.pop("CI", None)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            plugin.stem,
            "-p",
            "no:cacheprovider",
            "-q",
            "-rs",
            f"{Path(__file__).resolve()}::"
            "test_1920_runs_actual_expiry_and_captures_ack_under_opt_in_profile",
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    output = result.stdout + result.stderr
    if trap.exists() or "N5_CONNECTION_OR_CLEANUP_TRAP" in output:
        raise RuntimeError("refused fixture reached a connection, schema sweep or cleanup")
    if skipped:
        if result.returncode != 0 or "1 skipped" not in output or "owned lane" not in output:
            raise RuntimeError(f"contributor database was not skipped before setup: {output}")
    elif result.returncode != 1 or "require owned local lane engines" not in output:
        raise RuntimeError(f"foreign database was not refused before setup: {output}")


@pytest_asyncio.fixture
async def expiry(
    db_preflight, expiry_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[ExpiryContext]:
    sessions = async_sessionmaker(expiry_engine, expire_on_commit=False)
    require_owned_sessionmaker(sessions)
    clock = RoutineJobClock(anchor=ANCHOR)
    harness = DiscordSurfaceHarness(
        evidence_id="control:actual-credential-card",
        session_id="control:no-native-session-certified",
        root_turn_id="control:no-native-root-certified",
        clock=clock,
    )
    # N4 NOTE035229Z: repair the required SDK fetch metadata locally; do not
    # mutate captured payloads/card text or edit the shared gateway implementation.
    fetch = harness.gateway.get_message

    async def fetch_with_author(channel_id: int, message_id: int) -> MessagePayload:
        payload = await fetch(channel_id, message_id)
        payload["author"]["avatar"] = None
        return payload

    monkeypatch.setattr(harness.gateway, "get_message", fetch_with_author)
    state = harness.client._connection  # pyright: ignore[reportPrivateUsage]
    bot_data = {
        "id": "104",
        "username": "Daimon",
        "discriminator": "0000",
        "bot": True,
        "avatar": None,
    }
    state.user = discord.ClientUser(state=state, data=cast(Any, bot_data))
    guild = harness.thread.guild
    guild._roles[100] = discord.Role(  # pyright: ignore[reportPrivateUsage]
        guild=guild,
        state=state,
        data=cast(Any, {"id": "100", "name": "@everyone", "permissions": "8"}),
    )
    guild._members[104] = discord.Member(  # pyright: ignore[reportPrivateUsage]
        guild=guild,
        state=state,
        data=cast(Any, {"user": bot_data, "roles": [], "flags": 0}),
    )
    async with sessions() as session, session.begin():
        tenant = await make_tenant(session, workspace_id="100")
        account = await make_account(session, tenant=tenant)
        row = await requests.create_credential_request(
            session,
            token=mint_request_token(),
            kind="env",
            tenant_id=tenant.id,
            agent_id=uuid.uuid4(),
            account_id=account.id,
            target="OFFLINE_REQUESTED_KEY",
            mcp_server_url=None,
            requester_platform_user_id="103",
            channel_id="102",
            expires_at=clock.wall_time() + timedelta(minutes=30),
            idempotency_key=uuid.uuid4(),
            target_ma_agent_id="control:credential-target-not-native-certified",
            target_name="offline-agent",
            requested_work=None,
            responder_name="Daimon",
            platform="discord",
            parent_channel_id="101",
            origin_thread_id="102",
        )
    # Real store + product renderer + SDK HTTP path create the requested card.
    # This is not a native provider/MCP ingress assertion.
    message = await harness.thread.send(
        view=build_card_view(card_for_request(row, state="requested")),
        allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
    )
    async with sessions() as session, session.begin():
        await requests.update_credential_request_message(
            session, token=row.token, posted_message_id=str(message.id)
        )
        stored = await requests.peek_credential_request(session, token=row.token)
    if stored is None:
        raise RuntimeError("created request disappeared")

    async def edit(actual: CredentialRequestRow) -> bool:
        return await edit_expired_posted_card(harness.client, row=actual)

    jobs = ClockedRoutineJobs(
        sessionmaker=sessions,
        clock=clock,
        caps=NoCaps(),
        fire=no_fire,
        post=no_post,
        gateway=harness.gateway,
        bindings=(),
        credential_form_expiry=CredentialFormExpiryBinding(tenant_id=tenant.id, edit=edit),
    )
    yield ExpiryContext(sessions, stored, harness, jobs, clock)


async def read_request(context: ExpiryContext) -> CredentialRequestRow:
    require_owned_sessionmaker(context.sessions)
    async with context.sessions() as session:
        row = await requests.peek_credential_request(session, token=context.row.token)
    if row is None:
        raise RuntimeError("request disappeared")
    return row


async def test_1920_runs_actual_expiry_and_captures_ack_under_opt_in_profile(
    expiry: ExpiryContext,
) -> None:
    before = await read_request(expiry)
    receipt = await expiry.jobs.advance(1920, job="credential_form_expiry")
    row = await read_request(expiry)
    edits = [event for tick in receipt.ticks for event in tick.posts if event.operation == "edit"]
    if (
        before.used_at is not None
        or before.expires_at != ANCHOR + timedelta(seconds=1800)
        or row.used_at != ANCHOR + timedelta(seconds=1800)
        or row.outcome != "expired_card_updated"
        or receipt.profile != "opt_in_credential_form_expiry"
        or receipt.boundary != "actual_credential_form_expiry_job"
        or receipt.finished_s != 1920
        or [tick.at_s for tick in receipt.ticks] != [float(s) for s in range(0, 1921, 15)]
    ):
        raise RuntimeError("real expiry did not retire at the shared clock TTL boundary")
    if len(edits) != 1 or edits[0].message.message_id != row.posted_message_id:
        raise RuntimeError("expiry did not acknowledge exactly the originally posted card")
    edit = edits[0]
    if edit.message.observed_s != 1800 or EXPIRED_HEADLINE not in edit.message.payload_json:
        raise RuntimeError("accepted PATCH was not the actual timed expiry render")
    if edit.message.component_labels or '"custom_id"' in edit.message.payload_json:
        raise RuntimeError("expired card retained a credential button")
    if any(event.operation != "edit" for tick in receipt.ticks for event in tick.posts):
        raise RuntimeError("expiry created a replacement card or routine delivery")
    if (
        sum(
            tick.credential_form_expiry.acknowledged
            for tick in receipt.ticks
            if tick.credential_form_expiry
        )
        != 1
    ):
        raise RuntimeError("durable acknowledgement did not join actual accepted PATCH")
    actual = await expiry.harness.thread.fetch_message(int(row.posted_message_id or "0"))
    latest = expiry.harness.gateway.surviving_messages()
    if str(actual.id) != row.posted_message_id or len(latest) != 1:
        raise RuntimeError("latest readback was not the same surviving card")
    if EXPIRED_HEADLINE not in latest[0].payload_json:
        raise RuntimeError("latest card readback lost the acknowledged expiry state")


async def test_preexpiry_exact_boundary_and_repeat_are_real_db_states(
    expiry: ExpiryContext,
) -> None:
    before = await expiry.jobs.advance(1799, job="credential_form_expiry")
    row = await read_request(expiry)
    if row.used_at is not None or any(tick.posts for tick in before.ticks):
        raise RuntimeError("clock hook retired the form before its actual expires_at")
    exact = await expiry.jobs.advance(1, job="credential_form_expiry")
    row = await read_request(expiry)
    if row.outcome != "expired_card_updated" or row.used_at != row.expires_at:
        raise RuntimeError("form did not expire at the exact persisted TTL")
    if len([event for tick in exact.ticks for event in tick.posts]) != 1:
        raise RuntimeError("exact TTL lacked one actual edit acknowledgement")
    again = await expiry.jobs.advance(120, job="credential_form_expiry")
    if any(tick.posts for tick in again.ticks):
        raise RuntimeError("acknowledged form was edited again")


async def test_edit_refusal_stays_durable_pending_then_real_retry_ack(
    expiry: ExpiryContext,
) -> None:
    error = discord.Forbidden(cast(Any, SimpleNamespace(status=403, reason="Forbidden")), "offline")
    expiry.harness.gateway.fail_next("edit", error)
    receipt = await expiry.jobs.advance(1800, job="credential_form_expiry")
    row = await read_request(expiry)
    if row.outcome != "expired_card_pending" or any(tick.posts for tick in receipt.ticks):
        raise RuntimeError("failed PATCH was certified as an accepted card edit")
    retried = await expiry.jobs.advance(30, job="credential_form_expiry")
    row = await read_request(expiry)
    if row.outcome != "expired_card_updated":
        raise RuntimeError("durable pending expiry was not retried by the real job")
    edits = [event for tick in retried.ticks for event in tick.posts]
    if len(edits) != 1 or edits[0].operation != "edit":
        raise RuntimeError("expiry retry did not capture an accepted in-place PATCH")


@pytest.mark.parametrize("routing", ["main", "binds"])
async def test_foreign_binds_refused_before_clock_or_connection(
    expiry: ExpiryContext,
    routing: str,
) -> None:
    foreign = create_async_engine("postgresql+asyncpg://localhost/daimon_test")
    attempts: list[object] = []

    def no_connect(*args: Any, **kwargs: Any) -> None:
        attempts.append(foreign)
        raise RuntimeError("foreign database connection attempted")

    event.listen(foreign.sync_engine, "do_connect", no_connect)
    original = expiry.sessions.kw["bind"]
    if routing == "main":
        expiry.sessions.configure(bind=foreign)
    else:
        expiry.sessions.kw["binds"] = {object: foreign}
    try:
        with pytest.raises(ValueError, match="owned"):
            await expiry.jobs.advance(1920, job="credential_form_expiry")
    finally:
        expiry.sessions.kw.pop("binds", None)
        expiry.sessions.configure(bind=original)
        await foreign.dispose()
    if attempts or expiry.clock() != 0:
        raise RuntimeError("refused expiry advanced clock or connected a foreign database")


async def test_unset_binding_preserves_typed_refusal_without_time(expiry: ExpiryContext) -> None:
    unbound = ClockedRoutineJobs(
        sessionmaker=expiry.sessions,
        clock=expiry.clock,
        caps=NoCaps(),
        fire=no_fire,
        post=no_post,
        gateway=expiry.harness.gateway,
        bindings=(),
    )
    with pytest.raises(LongWaitUnavailable) as error:
        await unbound.advance(1920, job="credential_form_expiry")
    if error.value.code != "CREDENTIAL_FORM_EXPIRY_JOB_UNAVAILABLE" or expiry.clock() != 0:
        raise RuntimeError("unbound expiry contract changed")


@pytest.mark.parametrize("limit", [True, 0, -1, 101])
def test_expiry_binding_rejects_unbounded_batches(limit: int) -> None:
    async def edit(row: CredentialRequestRow) -> bool:
        raise RuntimeError("validation must not invoke edit")

    with pytest.raises(ValueError, match="batch"):
        CredentialFormExpiryBinding(tenant_id=uuid.uuid4(), edit=edit, limit=limit)


@pytest.mark.parametrize("seconds", [1800, 1920])
@pytest.mark.parametrize("raises_after_mutation", [False, True])
async def test_callbacks_reconfiguring_secondary_bind_are_readmitted_before_next_poll(
    expiry: ExpiryContext, seconds: int, raises_after_mutation: bool
) -> None:
    foreign = create_async_engine("postgresql+asyncpg://localhost/daimon_test")
    attempts: list[object] = []
    called: list[int] = []

    def no_connect(*args: Any, **kwargs: Any) -> None:
        attempts.append(foreign)
        raise RuntimeError("callback's foreign bind connected")

    event.listen(foreign.sync_engine, "do_connect", no_connect)

    async def edit(row: CredentialRequestRow) -> bool:
        # Valid real edit occurs, then the next poll must re-admit mutable routes.
        accepted = await edit_expired_posted_card(expiry.harness.client, row=row)
        called.append(1)
        expiry.sessions.kw["binds"] = {object: foreign}
        if raises_after_mutation:
            raise RuntimeError("callback failed after its actual edit and routing mutation")
        return accepted

    jobs = ClockedRoutineJobs(
        sessionmaker=expiry.sessions,
        clock=expiry.clock,
        caps=NoCaps(),
        fire=no_fire,
        post=no_post,
        gateway=expiry.harness.gateway,
        bindings=(),
        credential_form_expiry=CredentialFormExpiryBinding(
            tenant_id=expiry.row.tenant_id, edit=edit
        ),
    )
    try:
        with pytest.raises(ValueError, match="owned"):
            await jobs.advance(seconds, job="credential_form_expiry")
    finally:
        expiry.sessions.kw.pop("binds", None)
        await foreign.dispose()
    if attempts or called != [1]:
        raise RuntimeError("mutated secondary bind was not refused after actual callback")
    async with expiry.sessions() as session:
        stored = await requests.peek_credential_request(session, token=expiry.row.token)
    if stored is None or stored.outcome != "expired_card_pending":
        raise RuntimeError("unsafe callback mutation acknowledged the durable card")


async def test_retry_cursor_progresses_past_failed_card_without_starving_next(
    expiry: ExpiryContext,
) -> None:
    target = expiry.row.target_ma_agent_id
    name = expiry.row.target_name
    if target is None or name is None:
        raise RuntimeError("control request is missing its independently stated target")
    async with expiry.sessions() as session, session.begin():
        row = await requests.create_credential_request(
            session,
            token=mint_request_token(),
            kind="env",
            tenant_id=expiry.row.tenant_id,
            agent_id=expiry.row.agent_id,
            account_id=expiry.row.account_id,
            target="SECOND_OFFLINE_KEY",
            mcp_server_url=None,
            requester_platform_user_id="103",
            channel_id="102",
            expires_at=expiry.row.expires_at,
            idempotency_key=uuid.uuid4(),
            target_ma_agent_id=target,
            target_name=name,
            requested_work=None,
            responder_name="Daimon",
            platform="discord",
            parent_channel_id="101",
            origin_thread_id="102",
        )
    message = await expiry.harness.thread.send(
        view=build_card_view(card_for_request(row, state="requested")),
        allowed_mentions=discord.AllowedMentions.none(),
    )
    async with expiry.sessions() as session, session.begin():
        await requests.update_credential_request_message(
            session, token=row.token, posted_message_id=str(message.id)
        )
    blocked = min(expiry.row.token, row.token)

    async def edit(actual: CredentialRequestRow) -> bool:
        if actual.token == blocked:
            expiry.harness.gateway.fail_next(
                "edit",
                discord.Forbidden(
                    cast(Any, SimpleNamespace(status=403, reason="Forbidden")), "offline"
                ),
            )
        return await edit_expired_posted_card(expiry.harness.client, row=actual)

    jobs = ClockedRoutineJobs(
        sessionmaker=expiry.sessions,
        clock=expiry.clock,
        caps=NoCaps(),
        fire=no_fire,
        post=no_post,
        gateway=expiry.harness.gateway,
        bindings=(),
        credential_form_expiry=CredentialFormExpiryBinding(
            tenant_id=expiry.row.tenant_id, edit=edit, limit=1
        ),
    )
    receipt = await jobs.advance(1920, job="credential_form_expiry")
    async with expiry.sessions() as session:
        stored = [
            await requests.peek_credential_request(session, token=token)
            for token in (expiry.row.token, row.token)
        ]
    if any(value is None for value in stored):
        raise RuntimeError("expiry requests disappeared")
    for value in stored:
        if value is None:
            raise RuntimeError("request disappeared")
        expected = "expired_card_pending" if value.token == blocked else "expired_card_updated"
        if value.outcome != expected:
            raise RuntimeError("retry cursor starved the later card or certified a failed edit")
    edits = [event for tick in receipt.ticks for event in tick.posts]
    if len(edits) != 1 or edits[0].operation != "edit" or edits[0].message.observed_s != 1815:
        raise RuntimeError("later card lacked the actual next-tick edit acknowledgement")
    if "next_after_token" in repr(receipt) or blocked in repr(receipt):
        raise RuntimeError("opaque retry cursor leaked into component receipts")
