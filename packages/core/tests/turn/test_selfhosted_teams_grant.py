"""The AKS chart's registration grants Teams turns exactly as it grants the CLI operator.

`deploy/helm/daimon-selfhost/scripts/provision.py` writes the records and
`runtime_grants.py` turns them into `runtime.json` rows and the harness auth
binding's tenants, as `register.sh` does. A turn is then checked through the
real `build_selfhosted_runtime` and `validate_harness_auth`, reading the
channel configuration the way admission does (`current_backend`).
"""

from __future__ import annotations

import importlib.util
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from daimon.core.channel_backend import channel_ref, current_backend
from daimon.core.config import HarnessAuthBinding, SelfhostedBillingSettings, SelfHostedSettings
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.mux_backend import resource_scope
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.identity import find_platform_principal
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.selfhosted_deployment import (
    build_selfhosted_runtime,
    validate_harness_auth,
)
from mux.drivers.selfhosted.config import HarnessConfig
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

SCRIPTS = Path(__file__).resolve().parents[4] / "deploy/helm/daimon-selfhost/scripts"
MODEL = "gpt-5.4-mini"
ENTRA_TENANT = "33333333-3333-4333-8333-333333333333"
CHANNEL = "19:pilot@thread.tacv2"
OTHER_CHANNEL = "19:elsewhere@thread.tacv2"
PERSON = "0f4e2c1a-9b7d-4e5f-8a6b-1c2d3e4f5a6b"
OTHER_PERSON = "7a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
HARNESS = "https://daimon-teams-daimon-harness:8088"
TOKEN = "DAIMON_SELFHOSTED__HARNESS_TOKEN_PRIMARY"

pytestmark = pytest.mark.asyncio


def script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"chart_{name}", SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def environ(grants: dict[str, list[str]]) -> dict[str, str]:
    return {
        "SELFHOSTED_MODEL": MODEL,
        "DAIMON_TEAMS__TENANT_ID": ENTRA_TENANT,
        "TEAMS_GRANTS": json.dumps(grants),
    }


def ids_for(registration: dict[str, Any]) -> dict[str, list[str]]:
    """What `daimon selfhosted init` prints per tenant (agent, environment)."""
    tenants = script("runtime_grants").tenants(registration)
    return {tenant: [f"sh_agent_{n}", f"sh_env_{n}"] for n, tenant in enumerate(tenants)}


async def teams_turn(
    sf: async_sessionmaker[AsyncSession],
    rows: list[dict[str, Any]],
    ids: dict[str, list[str]],
    *,
    channel_id: str,
    person: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Build the selfhosted runtime for one Teams message, as the turn path does."""
    runtime_file = tmp_path / "runtime.json"
    runtime_file.write_text(json.dumps(rows))
    monkeypatch.setenv("DAIMON_TURN__PROVIDER_RUNTIME_FILE", str(runtime_file))
    tenant_id = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT)
    async with sf() as session:
        revision = await current_backend(session, channel_ref(tenant_id, "teams", channel_id))
        principal = await find_platform_principal(
            session, tenant_id=tenant_id, platform="teams", external_id=person
        )
    account_id = principal.account_id if principal is not None else uuid.uuid4()
    agent_id, environment_id = ids[str(tenant_id)]
    now = datetime.now(UTC)
    return build_selfhosted_runtime(
        SimpleNamespace(  # pyright: ignore[reportArgumentType]
            admission=SimpleNamespace(
                backend_revision=revision,
                agent=SimpleNamespace(id=agent_id),
                environment=SimpleNamespace(id=environment_id),
            ),
            scope=resource_scope(
                tenant_id=str(tenant_id),
                account_id=str(account_id),
                authorization_id="admitted-turn",
            ),
            platform="teams",
            thread_id=channel_id,
            external_user_id=person,
            deps=SimpleNamespace(sessionmaker=sf),
            deadline=now + timedelta(minutes=5),
            now=lambda: now,
        )
    )


@pytest.fixture(autouse=True)
def selfhosted_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAIMON_SELFHOSTED__ADMISSION_ENABLED", "true")
    billing = SelfhostedBillingSettings.model_validate(
        {
            "allow_test_tariffs": True,
            "deployments": {MODEL: f"{MODEL}-TEST"},
            "models": {
                f"{MODEL}-TEST": [
                    {
                        "effective_from": "2026-01-01",
                        "input": "1",
                        "output": "4",
                        "cache_read": "0.1",
                    }
                ]
            },
        }
    )
    monkeypatch.setattr(
        "daimon.core.config.load_settings",
        lambda: SimpleNamespace(billing=SimpleNamespace(selfhosted=billing)),
    )


async def test_teams_grant_admits_only_the_granted_person_in_the_granted_channel(
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sf = db_session_factory
    registration = await script("provision").provision(
        sf, environ({CHANNEL + ";messageid=1700000000000": [PERSON.upper()]})
    )
    ids = ids_for(registration)
    rows = script("runtime_grants").rows(registration, ids)
    assert [(row["platform"], row["channel_id"]) for row in rows] == [
        ("cli", "selfhosted-operator"),
        ("teams", CHANNEL),
    ]

    granted = await teams_turn(
        sf, rows, ids, channel_id=CHANNEL, person=PERSON, tmp_path=tmp_path, monkeypatch=monkeypatch
    )
    assert granted.model == MODEL

    for channel_id, person in ((CHANNEL, OTHER_PERSON), (OTHER_CHANNEL, PERSON)):
        with pytest.raises(AdmissionDenied, match="backend_unsupported"):
            await teams_turn(
                sf,
                rows,
                ids,
                channel_id=channel_id,
                person=person,
                tmp_path=tmp_path,
                monkeypatch=monkeypatch,
            )

    teams_tenant = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT)
    async with sf() as session:
        policy = await load_access_policy(session, tenant_id=teams_tenant)
    assert "selfhosted" in policy.enabled_backends


async def test_harness_binding_lists_the_teams_tenant_only_when_granted(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    grants = script("runtime_grants")
    teams_tenant = str(derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT))
    cli_only = await script("provision").provision(db_session_factory, {"SELFHOSTED_MODEL": MODEL})
    with_teams = await script("provision").provision(
        db_session_factory, environ({CHANNEL: [PERSON]})
    )
    config = HarnessConfig(harness_url=HARNESS, auth_mode="bearer_env", auth_ref=TOKEN)

    def binding(registration: dict[str, Any]) -> SelfHostedSettings:
        return SelfHostedSettings(
            harness_auth_bindings=(
                HarnessAuthBinding(
                    name=TOKEN, origin=HARNESS, tenants=grants.tenants(registration)
                ),
            )
        )

    assert "teams" not in cli_only
    with pytest.raises(AdmissionDenied):
        validate_harness_auth(config, binding(cli_only), teams_tenant)
    validate_harness_auth(config, binding(with_teams), teams_tenant)
    validate_harness_auth(config, binding(with_teams), with_teams["tenant_id"])


async def test_rerunning_registration_keeps_every_grant_valid(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    provision = script("provision")
    first = await provision.provision(db_session_factory, environ({CHANNEL: [PERSON]}))
    again = await provision.provision(
        db_session_factory, environ({CHANNEL: [PERSON, OTHER_PERSON]})
    )
    assert again["config_digest"] == first["config_digest"]
    assert again["teams"]["grants"][0] == first["teams"]["grants"][0]
    assert len({grant["account_id"] for grant in again["teams"]["grants"]}) == 2


@pytest.mark.parametrize(
    "raw",
    [
        "[]",
        "{}",
        json.dumps({CHANNEL: []}),
        json.dumps({CHANNEL: ["not-an-entra-id"]}),
        json.dumps({";messageid=1": [PERSON]}),
    ],
)
async def test_malformed_teams_grants_stop_registration(raw: str) -> None:
    with pytest.raises(SystemExit):
        script("provision").parse_teams_grants(raw)


async def test_teams_grants_need_the_hosts_teams_tenant(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    with pytest.raises(SystemExit, match="DAIMON_TEAMS__TENANT_ID"):
        await script("provision").provision(
            db_session_factory,
            {"SELFHOSTED_MODEL": MODEL, "TEAMS_GRANTS": json.dumps({CHANNEL: [PERSON]})},
        )
