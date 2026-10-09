"""GitHub grant cleanup retains SDK requests, ordering and operator output."""

# Exercise the actual operator command without a live provider or database.
# pyright: reportPrivateUsage=false
import asyncio
from collections.abc import Coroutine
from datetime import UTC, datetime
from io import StringIO
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from anthropic import APIStatusError, AsyncAnthropic
from daimon.adapters.cli.commands import github as command
from daimon.core.github_app_session import archive_app_vault
from daimon.core.mux_compat import archive_session
from daimon.core.session_snapshot import SessionSnapshot
from daimon.core.stores.domain import AccountIdentityRow, Role, ThreadSessionRow
from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport
from mux.contracts.ids import Scope
from pydantic import SecretStr
from rich.console import Console

TENANT = UUID("00000000-0000-0000-0000-000000000007")
ADMIN = UUID("00000000-0000-0000-0000-000000000008")
AGENT = UUID("00000000-0000-0000-0000-000000000009")
OWNER = UUID("00000000-0000-0000-0000-000000000010")
NOW = datetime(2026, 10, 9, tzinfo=UTC)


class Transaction:
    async def __aenter__(self) -> object:
        return self

    async def __aexit__(self, *_args: object) -> None:
        pass


class Sessionmaker:
    def __call__(self) -> Transaction:
        return Transaction()

    def begin(self) -> Transaction:
        return Transaction()


class Engine:
    disposed: bool = False

    async def dispose(self) -> None:
        self.disposed = True


class TrackingTransport(ScriptedTransport):
    def __init__(self, effects: list[str]) -> None:
        super().__init__()
        self.effects = effects

    def dispatch(self, request: httpx.Request) -> httpx.Response:
        self.effects.append(request.url.path)
        return super().dispatch(request)


def live_rows() -> list[ThreadSessionRow]:
    snapshot = SessionSnapshot(
        ma_agent_id="agent1",
        model_id="model",
        system_sha256=None,
        skills_sha256="skills",
        environment_id="env1",
        github_mode="app",
        repo_urls=("https://github.com/example/repo",),
        repo_url=None,
        repo_branch=None,
        memory_store_id=None,
        vault_id="vault1",
        tools_sha256="tools",
        mcp_servers_sha256="servers",
        env_sha256=None,
        agent_version=1,
        agent_name="agent",
    )
    return [
        ThreadSessionRow(
            id=UUID(int=index),
            tenant_id=TENANT,
            platform="cli",
            thread_id=f"thread{index}",
            account_id=OWNER,
            ma_session_id="session1",
            watermark_message_id=None,
            status="live",
            created_at=NOW,
            updated_at=NOW,
            effective_config=snapshot,
        )
        for index in (1, 2)
    ]


def install_host(
    monkeypatch: pytest.MonkeyPatch,
    transport: ScriptedTransport,
    effects: list[str],
    *,
    account: AccountIdentityRow | None,
) -> tuple[list[asyncio.Task[None]], StringIO, Engine]:
    tasks: list[asyncio.Task[None]] = []
    output = StringIO()
    engine = Engine()
    sessionmaker = Sessionmaker()

    async def lookup_admin(*args: object, **kwargs: object) -> UUID:
        assert kwargs["tenant_id"] == TENANT
        return ADMIN

    async def lookup_account(*args: object, **kwargs: object) -> AccountIdentityRow | None:
        assert kwargs["account_id"] == ADMIN
        return account

    async def deactivate(*args: object, **kwargs: object) -> None:
        assert kwargs["tenant_id"] == TENANT
        assert kwargs["agent_id"] == AGENT
        assert kwargs["changed_by_account_id"] == ADMIN

    async def mode(*args: object, **kwargs: object) -> str:
        return "app"

    async def sessions(*args: object, **kwargs: object) -> list[ThreadSessionRow]:
        assert kwargs["tenant_id"] == TENANT
        assert kwargs["agent_id"] == AGENT
        return live_rows()

    async def revoke(*args: object, **kwargs: object) -> None:
        effects.append(f"revoke:{kwargs['session_id']}")

    async def mark(*args: object, **kwargs: object) -> None:
        effects.append(f"dead:{kwargs['id']}")

    def run(coro: Coroutine[object, object, None], *, console: Console) -> None:
        tasks.append(asyncio.create_task(coro))

    def console_factory(**kwargs: object) -> Console:
        return Console(file=output, color_system=None)

    def engine_factory(url: object) -> Engine:
        return engine

    def session_factory(db_engine: object) -> Sessionmaker:
        return sessionmaker

    def encryption_factory(keys: object) -> object:
        return object()

    def client_factory(**kwargs: object) -> AsyncAnthropic:
        return transport.client()

    monkeypatch.setattr(
        command,
        "load_settings",
        lambda: SimpleNamespace(
            database=SimpleNamespace(url="postgresql://offline-test"),
            crypto=SimpleNamespace(keys=[SecretStr("offline-test")]),
            anthropic=SimpleNamespace(
                api_key=SecretStr("offline-test"), base_url="https://api.anthropic.com"
            ),
        ),
    )
    monkeypatch.setattr(command, "Console", console_factory)
    monkeypatch.setattr(command, "build_engine", engine_factory)
    monkeypatch.setattr(command, "build_session_factory", session_factory)
    monkeypatch.setattr(command, "build_multifernet", encryption_factory)
    monkeypatch.setattr(command, "AsyncAnthropic", client_factory)
    monkeypatch.setattr(command, "cli_account_id", lookup_admin)
    monkeypatch.setattr(command, "get_account_with_tenant", lookup_account)
    monkeypatch.setattr(command, "activate_agent", deactivate)
    monkeypatch.setattr(command, "deactivate_agent", deactivate)
    monkeypatch.setattr(command, "get_agent_mode", mode)
    monkeypatch.setattr(command, "list_live_sessions_for_agent", sessions)
    monkeypatch.setattr(command, "revoke_session_tokens", revoke)
    monkeypatch.setattr(command, "mark_dead", mark)
    monkeypatch.setattr(command, "run_cli", run)
    return tasks, output, engine


@pytest.mark.parametrize("action", ["activate", "deactivate"])
@pytest.mark.parametrize(
    ("status", "message"),
    [
        (200, ""),
        (400, "already archived"),
        (404, "gone"),
        (409, "conflict"),
        (403, "denied"),
        (400, "invalid vault"),
    ],
)
async def test_grant_cleanup_callsite_preserves_requests_errors_and_order(
    monkeypatch: pytest.MonkeyPatch, action: str, status: int, message: str
) -> None:
    before_effects: list[str] = []
    after_effects: list[str] = []
    before = TrackingTransport(before_effects)
    after = TrackingTransport(after_effects)
    response = (
        {} if status == 200 else {"error": {"type": "invalid_request_error", "message": message}}
    )
    for transport in (before, after):
        transport.queue(
            ScriptedReply("POST", "/v1/sessions/session1/archive", httpx.Response(200, json={})),
            ScriptedReply(
                "POST", "/v1/vaults/vault1/archive", httpx.Response(status, json=response)
            ),
        )
    expected_error: APIStatusError | None = None
    async with before.client() as old:
        await old.beta.sessions.archive("session1")
        before_effects.append("revoke:session1")
        try:
            await old.beta.vaults.archive("vault1")
        except APIStatusError as exc:
            if status not in (404, 409) and not (status == 400 and "already archived" in str(exc)):
                expected_error = exc
        if expected_error is None:
            before_effects.extend(f"dead:{row.id}" for row in live_rows())
    tasks, output, engine = install_host(
        monkeypatch,
        after,
        after_effects,
        account=AccountIdentityRow(
            account_id=ADMIN,
            tenant_id=TENANT,
            role=Role.ADMIN,
            platform="cli",
            external_id="local",
            platform_user_id="operator",
        ),
    )
    scopes: list[tuple[str, Scope]] = []

    async def session_archive(client: AsyncAnthropic, session_id: str, *, scope: Scope) -> None:
        scopes.append(("session", scope))
        await archive_session(client, session_id, scope=scope)

    async def vault_archive(
        client: AsyncAnthropic, *, vault_id: str, scope: Scope | None = None
    ) -> None:
        assert scope is not None
        scopes.append(("vault", scope))
        await archive_app_vault(client, vault_id=vault_id, scope=scope)

    monkeypatch.setattr(command, "archive_session", session_archive)
    monkeypatch.setattr(command, "archive_app_vault", vault_archive)
    command._run_grant_command(TENANT, action, AGENT)
    assert len(tasks) == 1
    if expected_error is None:
        await tasks[0]
    else:
        with pytest.raises(type(expected_error)) as actual:
            await tasks[0]
        assert str(actual.value) == str(expected_error)
    for transport in (before, after):
        transport.assert_consumed()
    assert [r.to_dict() for r in after.requests] == [r.to_dict() for r in before.requests]
    assert [r.body for r in after.requests] == [r.body for r in before.requests]
    assert after_effects == before_effects
    assert [kind for kind, _ in scopes] == ["session", "vault"]
    assert scopes[0][1] is scopes[1][1]
    assert scopes[0][1].tenant_id == str(TENANT)
    assert scopes[0][1].account_id == str(ADMIN)
    assert not scopes[0][1].is_platform
    assert not scopes[0][1].is_legacy_host_authorized
    assert output.getvalue() == (
        "app mode active\n" if action == "activate" else "legacy mode active\n"
    )
    assert engine.disposed


@pytest.mark.parametrize("status", [400, 403, 404, 409, 500])
async def test_session_archive_error_keeps_type_and_stops_cleanup(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    before_effects: list[str] = []
    after_effects: list[str] = []
    before = TrackingTransport(before_effects)
    after = TrackingTransport(after_effects)
    for transport in (before, after):
        transport.queue(
            ScriptedReply(
                "POST",
                "/v1/sessions/session1/archive",
                httpx.Response(
                    status, json={"error": {"type": "invalid_request_error", "message": "failed"}}
                ),
            )
        )
    async with before.client() as old:
        with pytest.raises(APIStatusError) as expected:
            await old.beta.sessions.archive("session1")
    tasks, output, engine = install_host(
        monkeypatch,
        after,
        after_effects,
        account=AccountIdentityRow(
            account_id=ADMIN,
            tenant_id=TENANT,
            role=Role.ADMIN,
            platform="cli",
            external_id="local",
            platform_user_id="operator",
        ),
    )
    command._run_grant_command(TENANT, "deactivate", AGENT)
    with pytest.raises(type(expected.value)) as actual:
        await tasks[0]
    assert str(actual.value) == str(expected.value)
    for transport in (before, after):
        transport.assert_consumed()
    assert [r.to_dict() for r in after.requests] == [r.to_dict() for r in before.requests]
    assert after_effects == before_effects == ["/v1/sessions/session1/archive"]
    assert output.getvalue() == "legacy mode active\n"
    assert engine.disposed


@pytest.mark.parametrize("denial", ["missing", "foreign", "external", "member"])
async def test_grant_cleanup_keeps_admin_gate_before_provider_io(
    monkeypatch: pytest.MonkeyPatch, denial: str
) -> None:
    account = (
        None
        if denial == "missing"
        else AccountIdentityRow(
            account_id=ADMIN,
            tenant_id=AGENT if denial == "foreign" else TENANT,
            role=Role.USER if denial == "member" else Role.ADMIN,
            is_external=denial == "external",
            platform="cli",
            external_id="local",
            platform_user_id="operator",
        )
    )
    transport = ScriptedTransport()
    effects: list[str] = []
    tasks, output, engine = install_host(monkeypatch, transport, effects, account=account)
    command._run_grant_command(TENANT, "deactivate", AGENT)
    with pytest.raises(ValueError, match="current CLI user is not a workspace admin"):
        await tasks[0]
    transport.assert_consumed()
    assert not transport.requests
    assert not effects
    assert not output.getvalue()
    assert engine.disposed
