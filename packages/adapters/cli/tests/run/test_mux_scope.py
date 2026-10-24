"""The opt-in CLI bridge uses the resolved operator identity, without legacy I/O."""

import sys
import uuid
from datetime import UTC, datetime, timedelta
from io import StringIO
from typing import Literal, cast
from uuid import UUID

import pytest
from daimon.adapters.cli import main as main_mod
from daimon.adapters.cli.run import command
from daimon.adapters.cli.run.command import SessionTenantMismatch
from daimon.adapters.cli.runtime import CliRuntime
from daimon.core.config import Settings, TurnSettings
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.domain import AccountRow, CliPrincipalRow, Role
from daimon.core.turn.posture import BillingExempt
from daimon.core.turn.state import TurnState
from daimon.testing.factories import make_tenant, make_thread_session
from mux.contracts.ids import Scope
from mux.errors import ScopeViolation
from mux.testing.drivers.anthropic.ma_transport import ScriptedReply, ScriptedTransport
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs
from typer.testing import CliRunner

from packages.adapters.cli.tests.run.conftest import cli_principal_count

TENANT = UUID(int=7)
ACCOUNT = UUID(int=8)
NOW = datetime(2026, 10, 9, tzinfo=UTC)


class Sessionmaker:
    """The real Postgres session factory, counting how often the command opens it."""

    def __init__(self, real: async_sessionmaker[AsyncSession]) -> None:
        self.real = real
        self.calls = 0

    def __call__(self) -> AsyncSession:
        self.calls += 1
        return self.real()


def install_identity(monkeypatch: pytest.MonkeyPatch, *, failure: str | None = None) -> None:
    async def discover(*args: object, **kwargs: object) -> UUID:
        return TENANT

    async def principal(*args: object, **kwargs: object) -> CliPrincipalRow:
        assert kwargs == {"tenant_id": TENANT, "os_user": "operator"}
        return CliPrincipalRow(
            id=UUID(int=9),
            tenant_id=UUID(int=10) if failure == "principal" else TENANT,
            account_id=ACCOUNT,
            os_user="operator",
            created_at=NOW,
        )

    async def account(_db: object, account_id: UUID) -> AccountRow | None:
        assert account_id == ACCOUNT
        if failure == "missing":
            return None
        return AccountRow(
            id=ACCOUNT,
            tenant_id=UUID(int=10) if failure == "account" else TENANT,
            role=Role.USER,
            created_at=NOW,
        )

    monkeypatch.setattr(command, "discover_tenant", discover)
    monkeypatch.setattr(command, "get_or_create_cli_principal", principal)
    monkeypatch.setattr(command, "get_account", account)


def runtime(transport: ScriptedTransport, sm: Sessionmaker) -> CliRuntime:
    return CliRuntime(
        settings=Settings.model_validate(
            {
                "database": {"url": "postgresql+asyncpg://localhost/offline"},
                "anthropic": {"api_key": "offline"},
                "cli": {"local_user": "operator"},
            }
        ),
        anthropic=transport.client(),
        sessionmaker=cast(async_sessionmaker[AsyncSession], sm),
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
    )


async def test_cli_mux_scope_drives_real_turn_with_identical_legacy_requests(
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from mux.testing.drivers.anthropic.ma import send_events_response

    install_identity(monkeypatch)
    monkeypatch.setattr(uuid, "uuid4", lambda: UUID(int=1))
    recordings: list[ScriptedTransport] = []
    outputs: list[str] = []
    deadline = datetime.now(UTC) + timedelta(seconds=30)
    paths: tuple[Literal["legacy", "mux"], ...] = ("legacy", "mux")
    for path in paths:
        transport = ScriptedTransport()
        transport.queue(
            ScriptedReply.stream(
                "/v1/sessions/session1/events/stream",
                [
                    {
                        "id": "evt_idle",
                        "type": "session.status_idle",
                        "processed_at": NOW.isoformat(),
                        "stop_reason": {"type": "end_turn"},
                    }
                ],
            ),
            ScriptedReply("POST", "/v1/sessions/session1/events", send_events_response()),
        )
        sm = Sessionmaker(db_session_factory)
        rt = runtime(transport, sm)
        output = StringIO()
        monkeypatch.setattr(sys, "stdout", output)
        async with rt.anthropic:
            with capture_logs() as logs:
                result = await command.run_conversation_observed(
                    rt=rt,
                    session_id="session1",
                    user_message="hello",
                    path=path,
                    deadline=deadline,
                )
        assert result == 0
        assert [entry["reason"] for entry in logs if entry["event"] == "turn.billing_exempt"] == [
            "cli-operator-run"
        ]
        assert sm.calls == (1 if path == "mux" else 0)
        transport.assert_consumed()
        recordings.append(transport)
        outputs.append(output.getvalue())
    assert recordings[0].requests == recordings[1].requests
    assert outputs[0] == outputs[1]


@pytest.mark.parametrize("failure", ["missing", "principal", "account"])
async def test_cli_mux_wrong_or_missing_identity_fails_before_provider_io(
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory: async_sessionmaker[AsyncSession],
    failure: str,
) -> None:
    install_identity(monkeypatch, failure=failure)
    transport = ScriptedTransport()
    rt = runtime(transport, Sessionmaker(db_session_factory))
    async with rt.anthropic:
        with pytest.raises(ScopeViolation):
            await command.run_conversation_observed(
                rt=rt, session_id="session1", user_message="hello", path="mux"
            )
    assert transport.requests == []


async def test_cli_mux_threads_the_real_scope_deadline_and_exempt_billing(
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    install_identity(monkeypatch)
    captured: dict[str, object] = {}

    async def driver(**kwargs: object) -> TurnState:
        captured.update(kwargs)
        return TurnState()

    monkeypatch.setattr(command, "run_turn", driver)
    transport = ScriptedTransport()
    rt = runtime(transport, Sessionmaker(db_session_factory))
    deadline = NOW + timedelta(minutes=20)
    async with rt.anthropic:
        assert (
            await command.run_conversation_observed(
                rt=rt, session_id="session1", user_message="hello", path="mux", deadline=deadline
            )
            == 0
        )
    assert captured["scope"] == Scope(
        tenant_id=str(TENANT),
        account_id=str(ACCOUNT),
        principal_id="daimon",
        authorization_id="cli-operator-run",
    )
    assert captured["path"] == "mux"
    assert captured["deadline"] is deadline
    assert captured["billing"] == BillingExempt(reason="cli-operator-run")
    assert transport.requests == []


# Decision D2: on the mux path `daimon run` acts only inside the CLI's own tenant.
# A session another tenant owns is refused before any principal is minted; legacy
# keeps its raw-session behaviour; a session nothing records (what
# `daimon sessions create` makes) is allowed.

FOREIGN_WORKSPACE = "guild-foreign"


def idle_transport(session_id: str) -> ScriptedTransport:
    from mux.testing.drivers.anthropic.ma import send_events_response

    transport = ScriptedTransport()
    transport.queue(
        ScriptedReply.stream(
            f"/v1/sessions/{session_id}/events/stream",
            [
                {
                    "id": "evt_idle",
                    "type": "session.status_idle",
                    "processed_at": NOW.isoformat(),
                    "stop_reason": {"type": "end_turn"},
                }
            ],
        ),
        ScriptedReply("POST", f"/v1/sessions/{session_id}/events", send_events_response()),
    )
    return transport


async def foreign_thread_session(db: AsyncSession, session_id: str) -> UUID:
    tenant = await make_tenant(db, platform="discord", workspace_id=FOREIGN_WORKSPACE)
    await make_thread_session(db, tenant=tenant, ma_session_id=session_id)
    await db.commit()
    return tenant.id


async def foreign_journal_session(db: AsyncSession, session_id: str) -> UUID:
    tenant = await make_tenant(db, platform="discord", workspace_id=FOREIGN_WORKSPACE)
    await db.execute(
        text(
            "INSERT INTO provider_binding_slot (binding_id, tenant_id, platform, channel_id,"
            " thread_id, account_id, generation) VALUES ('binding-foreign', :t, 'discord',"
            " 'chan', 'thread', NULL, 1)"
        ),
        {"t": tenant.id},
    )
    await db.execute(
        text(
            "INSERT INTO journal_session (session_id, tenant_id, binding_id)"
            " VALUES (:s, :t, 'binding-foreign')"
        ),
        {"s": session_id, "t": tenant.id},
    )
    await db.commit()
    return tenant.id


@pytest.mark.parametrize("record", [foreign_thread_session, foreign_journal_session])
async def test_cli_mux_refuses_a_session_another_tenant_owns_pinned_mux(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    record: object,
) -> None:
    owner = await record(db_session, "session-foreign")  # type: ignore[operator]
    transport = ScriptedTransport()
    sm = Sessionmaker(db_session_factory)
    rt = runtime(transport, sm)
    async with rt.anthropic:
        with pytest.raises(SessionTenantMismatch) as refused:
            await command.run_conversation_observed(
                rt=rt, session_id="session-foreign", user_message="hello", path="mux"
            )
    message = str(refused.value)
    assert message == (
        f"session session-foreign belongs to tenant {owner}; "
        "run it from that tenant, not the local CLI tenant"
    )
    assert refused.value.owner_tenant_id == owner
    assert transport.requests == [], "refused before any provider I/O"
    assert sm.calls == 1
    # No CLI identity was minted anywhere, least of all in the other tenant.
    assert await cli_principal_count(db_session) == 0


@pytest.mark.parametrize("record", [foreign_thread_session, foreign_journal_session])
async def test_cli_legacy_still_runs_a_session_another_tenant_owns_pinned_legacy(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    record: object,
) -> None:
    await record(db_session, "session-foreign")  # type: ignore[operator]
    transport = idle_transport("session-foreign")
    sm = Sessionmaker(db_session_factory)
    rt = runtime(transport, sm)
    async with rt.anthropic:
        result = await command.run_conversation_observed(
            rt=rt,
            session_id="session-foreign",
            user_message="hello",
            path="legacy",
            deadline=datetime.now(UTC) + timedelta(seconds=30),
        )
    assert result == 0
    transport.assert_consumed()
    assert sm.calls == 0, "legacy never opens the database for the identity check"
    assert await cli_principal_count(db_session) == 0


async def test_cli_mux_runs_a_session_nothing_records_pinned_mux(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """`daimon sessions create` leaves no tenant stamp and no row; refusing it would
    refuse the session `daimon run` normally targets (docs/billing.md)."""
    transport = idle_transport("session-fresh")
    rt = runtime(transport, Sessionmaker(db_session_factory))
    async with rt.anthropic:
        result = await command.run_conversation_observed(
            rt=rt,
            session_id="session-fresh",
            user_message="hello",
            path="mux",
            deadline=datetime.now(UTC) + timedelta(seconds=30),
        )
    assert result == 0
    transport.assert_consumed()
    assert await cli_principal_count(db_session) == 1


async def test_cli_mux_runs_a_session_the_local_tenant_owns_pinned_mux(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from daimon.core.ma_identity import derive_tenant_uuid
    from daimon.core.stores.tenants import get_tenant

    local = derive_tenant_uuid(platform="cli", workspace_id="local")
    tenant = await get_tenant(db_session, local)
    assert tenant is not None
    await make_thread_session(db_session, tenant=tenant, platform="cli", ma_session_id="s-local")
    await db_session.commit()
    transport = idle_transport("s-local")
    rt = runtime(transport, Sessionmaker(db_session_factory))
    async with rt.anthropic:
        result = await command.run_conversation_observed(
            rt=rt,
            session_id="s-local",
            user_message="hello",
            path="mux",
            deadline=datetime.now(UTC) + timedelta(seconds=30),
        )
    assert result == 0
    transport.assert_consumed()


def test_command_resolves_the_path_once_and_reports_a_refusal_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The edge (`run_command`) reads the turn path from settings and hands it down;
    a tenant refusal exits 1 with the named message, not a traceback.

    The conversation itself runs on its own event loop here, so it is faked; the
    refusal it raises is proven against Postgres by the tests above.
    """
    from contextlib import asynccontextmanager

    owner = UUID(int=99)
    seen: list[str] = []

    @asynccontextmanager
    async def build_runtime(_settings: Settings):  # type: ignore[no-untyped-def]
        yield None

    async def conversation(_settings, _session, _message, **kwargs: object) -> int:
        seen.append(str(kwargs["path"]))
        raise SessionTenantMismatch("session-foreign", owner)

    settings = Settings.model_validate(
        {
            "database": {"url": "postgresql+asyncpg://localhost/offline"},
            "anthropic": {"api_key": "offline"},
        }
    )
    monkeypatch.setattr(command, "load_settings", lambda: settings)
    monkeypatch.setattr(command, "load_turn_settings", lambda: TurnSettings(path="mux"))
    monkeypatch.setattr(command, "build_runtime", build_runtime)
    monkeypatch.setattr(command, "_run_with_defaults", conversation)

    result = CliRunner().invoke(main_mod.app, ["run", "hi", "--session", "session-foreign"])

    assert seen == ["mux"]
    assert result.exit_code == 1
    assert f"session session-foreign belongs to tenant {owner}" in result.stderr
    assert "Traceback" not in result.stderr
