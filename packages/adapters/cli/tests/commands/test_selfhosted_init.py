"""Postgres-only initialization is repeatable and never constructs a provider client."""

import asyncio
from pathlib import Path

import pytest
from daimon.adapters.cli.commands import selfhosted
from daimon.adapters.cli.main import app
from daimon.core.stores.selfhosted_resources import SelfHostedAgents
from daimon.testing.factories import make_tenant
from mux.contracts.ids import Scope
from mux.drivers.selfhosted.config import HarnessConfig
from sqlalchemy.ext.asyncio import async_sessionmaker
from typer.testing import CliRunner


async def test_init_reuses_matching_records_and_refuses_drift(db_session, db_session_factory):
    tenant = await make_tenant(db_session)
    await db_session.commit()
    kwargs = dict(
        tenant_id=tenant.id,
        name="daimon",
        model="gpt-6-luna",
        system="Help.",
        environment_name="default",
        harness=HarnessConfig(harness_url="http://localhost:8080", auth_ref="HARNESS_TOKEN"),
    )
    first = await selfhosted.initialize(db_session_factory, **kwargs)
    second = await selfhosted.initialize(db_session_factory, **kwargs)
    assert first == second
    assert first[0].startswith("sh_agent_") and first[1].startswith("sh_env_")
    with pytest.raises(ValueError, match="different configuration"):
        await selfhosted.initialize(db_session_factory, **{**kwargs, "model": "other-deployment"})
    scope = Scope(
        tenant_id=str(tenant.id),
        account_id="operator",
        principal_id="cli",
        authorization_id="cli-deployment-operator",
    )
    agent = await SelfHostedAgents(db_session_factory).resolve(scope, first[0])
    assert agent.spec.system == "Help."


@pytest.mark.parametrize(
    "transport,harness_url,insecure",
    [
        ("direct", "http://localhost:8080", False),
        ("direct", "http://harness:8080", True),
        ("foundry_invocations", "https://harness.example", False),
    ],
)
def test_cli_prints_only_ids_without_anthropic(
    db_nullpool_engine,
    db_clean,
    monkeypatch,
    tmp_path: Path,
    transport,
    harness_url,
    insecure,
):
    monkeypatch.delenv("DAIMON_ANTHROPIC__API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sessions = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)

    async def seed():
        async with sessions.begin() as db:
            return await make_tenant(db)

    tenant = asyncio.run(seed())
    monkeypatch.setattr(selfhosted, "build_engine", lambda url: db_nullpool_engine)
    system = tmp_path / "system.txt"
    system.write_text("Help.")
    args = [
        "selfhosted",
        "init",
        "--model",
        "gpt-6-luna",
        "--tenant-id",
        str(tenant.id),
        "--harness-url",
        harness_url,
        "--database-url",
        "postgresql+asyncpg://localhost/test",
        "--system-file",
        str(system),
        "--transport",
        transport,
    ]
    if insecure:
        args += ["--allow-insecure-http"]
    if transport == "foundry_invocations":
        args += [
            "--agent-name",
            "daimon",
            "--project-endpoint",
            "https://foundry.example/api/projects/test",
            "--auth-mode",
            "entra",
            "--auth-ref",
            "api://audience",
        ]
    runner = CliRunner()
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    ids = result.stdout.splitlines()
    assert len(ids) == 2 and ids[0].startswith("sh_agent_") and ids[1].startswith("sh_env_")
    repeated = runner.invoke(app, args)
    assert repeated.exit_code == 0, repeated.output
    assert repeated.stdout == result.stdout

    async def check():
        scope = Scope(
            tenant_id=str(tenant.id),
            account_id="operator",
            principal_id="cli",
            authorization_id="cli-deployment-operator",
        )
        agent = await SelfHostedAgents(sessions).resolve(scope, ids[0])
        assert agent.spec.system == "Help."

    asyncio.run(check())


async def test_init_can_replace_archived_and_deleted_names(db_session, db_session_factory):
    tenant = await make_tenant(db_session)
    await db_session.commit()
    kwargs = dict(
        tenant_id=tenant.id,
        name="daimon",
        model="gpt-6-luna",
        system=None,
        environment_name="default",
        harness=HarnessConfig(harness_url="https://harness.example", auth_ref="TOKEN"),
    )
    first = await selfhosted.initialize(db_session_factory, **kwargs)
    scope = Scope(
        tenant_id=str(tenant.id),
        account_id="operator",
        principal_id="cli",
        authorization_id="cli-deployment-operator",
    )
    agents = SelfHostedAgents(db_session_factory)
    agent = await agents.resolve(scope, first[0])
    await agents.archive(scope, agent.ref, key="archive")
    second = await selfhosted.initialize(db_session_factory, **kwargs)
    assert second[0] != first[0] and second[1] == first[1]
    assert await selfhosted.initialize(db_session_factory, **kwargs) == second
    agent = await agents.resolve(scope, second[0])
    await agents.delete(scope, agent.ref, key="delete")
    third = await selfhosted.initialize(db_session_factory, **kwargs)
    assert third[0] not in (first[0], second[0]) and third[1] == first[1]


async def test_init_reuses_existing_environment_with_implicit_schema_defaults(
    db_session, db_session_factory
):
    from daimon.core.stores.selfhosted_resources import SelfHostedEnvironments
    from mux.contracts.extensions import ExtensionConfig
    from mux.contracts.resources import EnvironmentSpec

    tenant = await make_tenant(db_session)
    await db_session.commit()
    scope = Scope(
        tenant_id=str(tenant.id),
        account_id="operator",
        principal_id="cli",
        authorization_id="cli-deployment-operator",
    )
    env = await SelfHostedEnvironments(db_session_factory).create(
        scope,
        EnvironmentSpec(
            name="default",
            execution="self_hosted",
            native_config=ExtensionConfig(
                namespace="selfhosted.environment",
                version=1,
                value={"harness_url": "https://harness.example", "auth_ref": "TOKEN"},
            ),
        ),
        key="existing",
    )
    initialized = await selfhosted.initialize(
        db_session_factory,
        tenant_id=tenant.id,
        name="daimon",
        model="gpt-6-luna",
        system=None,
        environment_name="default",
        harness=HarnessConfig(harness_url="https://harness.example", auth_ref="TOKEN"),
    )
    assert initialized[1] == env.ref.id


@pytest.mark.parametrize("option", ["--auth-ref", "--harness-url"])
def test_cli_invalid_config_does_not_echo_credentials(option):
    from uuid import uuid4

    secret = "sk-secret-marker-never-print"
    args = [
        "selfhosted",
        "init",
        "--model",
        "gpt-6-luna",
        "--tenant-id",
        str(uuid4()),
        "--database-url",
        "postgresql+asyncpg://localhost/test",
        "--harness-url",
        "https://harness.example",
    ]
    args += [option, secret if option == "--auth-ref" else f"https://user:{secret}@harness.example"]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 2
    assert "Invalid harness configuration" in result.output
    assert secret not in result.output
    assert result.stdout == ""


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql+asyncpg://user:DbPasswordMarker927",
        "://user:DbPasswordMarker927@host/database",
        "postgresql+asyncpg://user:DbPasswordMarker927@host:invalid/database",
        "unsupported+asyncpg://user:DbPasswordMarker927@host/database",
    ],
)
def test_main_invalid_database_url_never_echoes_password(monkeypatch, capsys, database_url):
    from daimon.adapters.cli.main import main

    monkeypatch.setattr(
        "sys.argv",
        [
            "daimon",
            "selfhosted",
            "init",
            "--model",
            "gpt-6-luna",
            "--tenant-id",
            "11111111-2222-3333-4444-555555555555",
            "--harness-url",
            "https://harness.example",
            "--database-url",
            database_url,
        ],
    )
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "database configuration invalid (scheme/host/port check)" in output.err
    assert "DbPasswordMarker927" not in output.err
    assert database_url not in output.err
    assert "Traceback" not in output.err


@pytest.mark.parametrize("error_type", [RuntimeError, TypeError, OSError])
def test_cli_unexpected_engine_construction_errors_do_not_echo_input(
    monkeypatch, capsys, error_type
):
    from daimon.adapters.cli.main import main

    def fail_engine(url):
        raise error_type(f"DbPasswordMarker927: {url}")

    monkeypatch.setattr(selfhosted, "build_engine", fail_engine)
    monkeypatch.setattr(
        "sys.argv",
        [
            "daimon",
            "selfhosted",
            "init",
            "--model",
            "gpt-6-luna",
            "--tenant-id",
            "11111111-2222-3333-4444-555555555555",
            "--harness-url",
            "https://harness.example",
            "--database-url",
            "postgresql+asyncpg://user:DbPasswordMarker927@host/database",
        ],
    )
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "database configuration invalid (scheme/host/port check)" in output.err
    assert "DbPasswordMarker927" not in output.err
    assert "Traceback" not in output.err


@pytest.mark.parametrize("failure", ["drift", "provider", "database", "file"])
def test_main_expected_initialization_failures_are_friendly(monkeypatch, capsys, failure):
    from daimon.adapters.cli.main import main
    from mux.errors import ProviderError
    from sqlalchemy.exc import SQLAlchemyError

    async def fail_initialize(*args, **kwargs):
        if failure == "drift":
            raise ValueError("DbPasswordMarker927")
        if failure == "provider":
            raise ProviderError("invalid_request", retryable=False)
        if failure == "database":
            raise SQLAlchemyError("DbPasswordMarker927")
        raise OSError("DbPasswordMarker927")

    monkeypatch.setattr(selfhosted, "initialize", fail_initialize)
    monkeypatch.setattr(
        "sys.argv",
        [
            "daimon",
            "selfhosted",
            "init",
            "--model",
            "gpt-6-luna",
            "--tenant-id",
            "11111111-2222-3333-4444-555555555555",
            "--harness-url",
            "https://harness.example",
            "--database-url",
            "postgresql+asyncpg://user:DbPasswordMarker927@host/database",
        ],
    )
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "Initialization refused" in output.err or "Initialization failed" in output.err
    assert "Traceback" not in output.err
    assert "DbPasswordMarker927" not in output.err


def test_production_model_default_is_sol_without_calling_a_model():
    import inspect

    assert (
        inspect.signature(selfhosted.selfhosted_init_command).parameters["model"].default
        == "gpt-6.1-sol"
    )
