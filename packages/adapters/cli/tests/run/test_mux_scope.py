"""The opt-in CLI bridge uses the resolved operator identity, without legacy I/O."""

import sys
import uuid
from datetime import UTC, datetime, timedelta
from io import StringIO
from typing import Literal, cast
from uuid import UUID

import pytest
from daimon.adapters.cli.run import command
from daimon.adapters.cli.runtime import CliRuntime
from daimon.core.config import Settings, TurnSettings
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.domain import AccountRow, CliPrincipalRow, Role
from daimon.core.turn.posture import BillingExempt
from daimon.core.turn.state import TurnState
from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport
from mux.contracts.ids import Scope
from mux.errors import ScopeViolation
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

TENANT = UUID(int=7)
ACCOUNT = UUID(int=8)
NOW = datetime(2026, 10, 9, tzinfo=UTC)


class Transaction:
    async def __aenter__(self) -> "Transaction":
        return self

    async def __aexit__(self, *_args: object) -> None:
        pass

    async def commit(self) -> None:
        pass


class Sessionmaker:
    calls: int = 0

    def __call__(self) -> Transaction:
        self.calls += 1
        return Transaction()


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
) -> None:
    from daimon.testing.ma import send_events_response

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
        settings = TurnSettings(path=path)
        monkeypatch.setattr(command, "load_turn_settings", lambda settings=settings: settings)
        sm = Sessionmaker()
        rt = runtime(transport, sm)
        output = StringIO()
        monkeypatch.setattr(sys, "stdout", output)
        async with rt.anthropic:
            with capture_logs() as logs:
                result = await command.run_conversation_observed(
                    rt=rt, session_id="session1", user_message="hello", deadline=deadline
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
    failure: str,
) -> None:
    install_identity(monkeypatch, failure=failure)
    monkeypatch.setattr(command, "load_turn_settings", lambda: TurnSettings(path="mux"))
    transport = ScriptedTransport()
    rt = runtime(transport, Sessionmaker())
    async with rt.anthropic:
        with pytest.raises(ScopeViolation):
            await command.run_conversation_observed(
                rt=rt, session_id="session1", user_message="hello"
            )
    assert transport.requests == []


async def test_cli_mux_threads_the_real_scope_deadline_and_exempt_billing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_identity(monkeypatch)
    monkeypatch.setattr(command, "load_turn_settings", lambda: TurnSettings(path="mux"))
    captured: dict[str, object] = {}

    async def driver(**kwargs: object) -> TurnState:
        captured.update(kwargs)
        return TurnState()

    monkeypatch.setattr(command, "run_turn", driver)
    transport = ScriptedTransport()
    rt = runtime(transport, Sessionmaker())
    deadline = NOW + timedelta(minutes=20)
    async with rt.anthropic:
        assert (
            await command.run_conversation_observed(
                rt=rt, session_id="session1", user_message="hello", deadline=deadline
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
