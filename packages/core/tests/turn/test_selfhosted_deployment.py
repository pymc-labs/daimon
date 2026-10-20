"""Exact deployment grants and pre-HTTP billing refusal for self-hosted turns."""

import json
from types import SimpleNamespace

import httpx
import pytest
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.selfhosted_deployment import (
    SelfHostedDeployment,
    authorized_deployments,
    build_selfhosted_runtime,
)
from pydantic import ValidationError

from .test_selfhosted_host import REVISION, SCOPE


def manifest():
    return dict(
        profile="selfhosted.loop",
        tenant_id=SCOPE.tenant_id,
        platform="slack",
        channel_id=REVISION.channel.channel_id,
        account_id=SCOPE.account_id,
        config_digest=REVISION.content_digest(),
        agent_id="sh_agent_test",
        environment_id="sh_env_test",
        infrastructure_usd="0",
    )


def facts():
    class NoDependencies:
        def __getattr__(self, name):
            raise AssertionError("preflight must refuse before touching runtime dependencies")

    return SimpleNamespace(
        admission=SimpleNamespace(
            backend_revision=REVISION,
            agent=SimpleNamespace(id="sh_agent_test"),
            environment=SimpleNamespace(id="sh_env_test"),
        ),
        scope=SCOPE,
        platform="slack",
        thread_id="thread",
        deps=NoDependencies(),
    )


@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    monkeypatch.setenv("DAIMON_SELFHOSTED__ADMISSION_ENABLED", "true")


def test_infrastructure_cost_is_required_and_nonnegative():
    row = manifest()
    row.pop("infrastructure_usd")
    with pytest.raises(ValidationError, match="infrastructure_usd"):
        SelfHostedDeployment.model_validate(row)
    with pytest.raises(ValidationError, match="infrastructure_usd"):
        SelfHostedDeployment.model_validate({**row, "infrastructure_usd": "-1"})
    assert SelfHostedDeployment.model_validate(manifest()).infrastructure_usd == 0


def test_manifest_is_tenant_scoped_and_credential_free(tmp_path, monkeypatch):
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps([manifest()]))
    monkeypatch.setenv("DAIMON_TURN__PROVIDER_RUNTIME_FILE", str(path))
    assert len(authorized_deployments(SCOPE.tenant_id)) == 1
    assert authorized_deployments("foreign") == ()
    path.write_text(json.dumps([{**manifest(), "api_key": "not-an-accepted-field"}]))
    assert authorized_deployments(SCOPE.tenant_id) == ()


def test_missing_grant_refuses_before_transport_or_resource_dependencies(monkeypatch):
    monkeypatch.delenv("DAIMON_TURN__PROVIDER_RUNTIME_FILE", raising=False)
    with pytest.raises(AdmissionDenied, match="backend_unsupported"):
        build_selfhosted_runtime(facts())


def test_unpriced_deployment_has_zero_harness_http_calls(tmp_path, monkeypatch):
    from daimon.core.config import SelfhostedBillingSettings

    path = tmp_path / "runtime.json"
    path.write_text(json.dumps([manifest()]))
    monkeypatch.setenv("DAIMON_TURN__PROVIDER_RUNTIME_FILE", str(path))
    monkeypatch.setattr(
        "daimon.core.config.load_settings",
        lambda: SimpleNamespace(billing=SimpleNamespace(selfhosted=SelfhostedBillingSettings())),
    )
    calls = []

    async def forbidden_http(self, *args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("unpriced deployments must never contact the harness")

    monkeypatch.setattr(httpx.AsyncClient, "request", forbidden_http)
    with pytest.raises(AdmissionDenied, match="backend_unsupported"):
        build_selfhosted_runtime(facts())
    assert calls == []


def test_disabled_operator_switch_refuses_before_resource_or_transport_dependencies(monkeypatch):
    monkeypatch.setenv("DAIMON_SELFHOSTED__ADMISSION_ENABLED", "false")
    with pytest.raises(AdmissionDenied, match="backend_unsupported"):
        build_selfhosted_runtime(facts())


def test_installed_tariffs_load_without_database_or_provider_credentials(tmp_path, monkeypatch):
    from daimon.core.turn.runtimes import channel_runtime_prices
    from daimon.core.turn.selfhosted_deployment import register_runtime

    monkeypatch.chdir(tmp_path)
    for name in (
        "DAIMON_DATABASE__URL",
        "DAIMON_ANTHROPIC__API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "AZURE_OPENAI_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(
        "DAIMON_BILLING__SELFHOSTED",
        json.dumps(
            {
                "deployments": {"offline-deployment": "offline-model"},
                "models": {
                    "offline-model": [
                        {
                            "effective_from": "2026-01-01",
                            "input": "2",
                            "output": "10",
                            "cache_read": "0.5",
                        }
                    ]
                },
            }
        ),
    )
    register_runtime()
    prices = channel_runtime_prices()
    assert prices[("selfhosted", "offline-deployment")].input == 2
    assert ("selfhosted", "unknown-model") not in prices


@pytest.mark.parametrize(
    "auth_ref,url,tenant",
    [
        ("DAIMON_MCP__JWT_SECRET", "https://attacker.example", SCOPE.tenant_id),
        ("DAIMON_SELFHOSTED__HARNESS_TOKEN_TEST", "https://attacker.example", SCOPE.tenant_id),
        ("DAIMON_SELFHOSTED__HARNESS_TOKEN_TEST", "https://harness", "foreign"),
    ],
)
def test_harness_binding_refuses_secret_exfiltration_before_resolution(
    monkeypatch, auth_ref, url, tenant
):
    from daimon.core.config import HarnessAuthBinding, SelfHostedSettings
    from daimon.core.turn.selfhosted_deployment import validate_harness_auth
    from mux.drivers.selfhosted.config import HarnessConfig

    settings = SelfHostedSettings(
        harness_auth_bindings=(
            HarnessAuthBinding(
                name="DAIMON_SELFHOSTED__HARNESS_TOKEN_TEST",
                origin="https://harness",
                tenants=(SCOPE.tenant_id,),
            ),
        )
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("must not resolve secrets or contact HTTP")

    monkeypatch.setattr(httpx.AsyncClient, "request", forbidden)
    monkeypatch.setattr("daimon.core.turn.selfhosted_deployment.os.environ.get", forbidden)
    with pytest.raises(AdmissionDenied):
        validate_harness_auth(
            HarnessConfig(harness_url=url, auth_mode="bearer_env", auth_ref=auth_ref),
            settings,
            tenant,
        )


def test_host_policy_keeps_stricter_agent_and_operator_rules():
    from daimon.core.tool_safety import ToolSafetyPolicy
    from daimon.core.turn.selfhosted_deployment import host_tool_policy, merge_tool_policy

    host = host_tool_policy(
        ToolSafetyPolicy(enabled=True, denied=("reports/delete_report",)),
        ("reports",),
        asks_before_publishing=False,
        rule_tools=frozenset(),
    )
    merged = merge_tool_policy(
        {"reports/delete_report": "auto", "reports/read_report": "deny"}, host
    )
    assert merged["reports/delete_report"] == "deny"
    assert merged["reports/read_report"] == "deny"
    assert merged["reports/*"] == "ask"
    assert merged["daimon-mcp/add_skill"] == "ask"
    assert (
        merge_tool_policy({"reports/write": "auto"}, {"reports/*": "deny"})["reports/write"]
        == "deny"
    )


async def test_entra_refresh_failure_is_retryable_and_credential_is_reused(monkeypatch):
    import time

    from azure.core.credentials import AccessToken
    from daimon.core.turn.selfhosted_deployment import EntraTokenProvider
    from mux.errors import ProviderError

    created, requests, closed = [], [], []

    class Credential:
        def __init__(self):
            created.append(self)

        async def get_token(self, scope):
            requests.append(scope)
            if len(requests) > 1:
                raise RuntimeError("expired identity provider unavailable")
            return AccessToken("offline", int(time.time()) + 30)

        async def close(self):
            closed.append(self)

    monkeypatch.setattr("azure.identity.aio.DefaultAzureCredential", Credential)
    provider = EntraTokenProvider("https://harness")
    assert await provider() == "offline"
    with pytest.raises(ProviderError) as error:
        await provider()
    assert error.value.retryable and error.value.category == "transient_network"
    assert requests == ["https://harness/.default"] * 2
    await provider.close()
    assert created == closed and len(created) == 1


@pytest.mark.parametrize(
    "audience,url,tenant",
    [
        ("https://management.azure.com", "https://attacker.example", SCOPE.tenant_id),
        ("https://unbound.example", "https://harness", SCOPE.tenant_id),
        ("https://management.azure.com", "https://harness", "foreign"),
    ],
)
def test_entra_binding_checks_origin_audience_and_tenant_before_minting(
    monkeypatch, audience, url, tenant
):
    from daimon.core.config import HarnessAuthBinding, SelfHostedSettings
    from daimon.core.turn.selfhosted_deployment import validate_harness_auth
    from mux.drivers.selfhosted.config import HarnessConfig

    settings = SelfHostedSettings(
        harness_auth_bindings=(
            HarnessAuthBinding(
                auth_mode="entra",
                name="https://management.azure.com",
                origin="https://harness",
                tenants=(SCOPE.tenant_id,),
            ),
        )
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("unbound destinations must not resolve credentials or send HTTP")

    monkeypatch.setattr("azure.identity.aio.DefaultAzureCredential", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "request", forbidden)
    with pytest.raises(AdmissionDenied):
        validate_harness_auth(
            HarnessConfig(harness_url=url, auth_mode="entra", auth_ref=audience), settings, tenant
        )
