"""A deployment-wide default row authorizes every channel/account for one
tenant without ever letting `tenant_id` or `profile` itself wildcard — see
`select_runtime_manifest` in `daimon.core.turn.runtime_manifest`."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.openai_deployment import OpenAIDeployment
from daimon.core.turn.runtime_manifest import (
    ProviderRuntimeConfigurationError,
    select_runtime_manifest,
    validate_configured_runtime_manifest,
)


def row(
    *,
    tenant_id: str = "tenant-a",
    platform: str = "slack",
    channel_id: str = "channel-1",
    account_id: str = "account-1",
    agent_id: str = "agent-default",
    environment_id: str = "environment-default",
) -> dict[str, object]:
    return {
        "profile": "openai.persistent_workspace",
        "tenant_id": tenant_id,
        "platform": platform,
        "channel_id": channel_id,
        "account_id": account_id,
        "config_digest": "digest",
        "project": "project",
        "agent_id": agent_id,
        "environment_id": environment_id,
        "api_key_env": "DAIMON_CHANNEL_OPENAI_KEY",
        "skill_ids": [],
    }


def fake_request(
    *, tenant_id: str, account_id: str, platform: str, channel_id: str
) -> SimpleNamespace:
    revision = SimpleNamespace(
        profile="openai.persistent_workspace",
        digest="digest",
        channel=SimpleNamespace(platform=platform, channel_id=channel_id),
    )
    return SimpleNamespace(
        admission=SimpleNamespace(backend_revision=revision, backend_revision_origin="channel"),
        scope=SimpleNamespace(tenant_id=tenant_id, account_id=account_id),
    )


def write_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rows: list[dict]) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(rows))
    monkeypatch.setenv("DAIMON_TURN__PROVIDER_RUNTIME_FILE", str(path))


def test_wildcard_default_authorizes_every_channel_and_account_for_its_tenant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_manifest(
        tmp_path,
        monkeypatch,
        [row(platform="*", channel_id="*", account_id="*")],
    )
    for platform, channel_id, account_id in (
        ("slack", "channel-1", "account-1"),
        ("discord", "channel-9", "account-9"),
    ):
        request = fake_request(
            tenant_id="tenant-a",
            account_id=account_id,
            platform=platform,
            channel_id=channel_id,
        )
        plan = select_runtime_manifest(request, OpenAIDeployment)
        assert plan.platform == platform
        assert plan.channel_id == channel_id
        assert plan.account_id == account_id
        assert plan.agent_id == "agent-default"


def test_exact_row_wins_over_a_coexisting_wildcard_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_manifest(
        tmp_path,
        monkeypatch,
        [
            row(platform="*", channel_id="*", account_id="*", agent_id="agent-default"),
            row(channel_id="channel-1", account_id="account-1", agent_id="agent-pinned"),
        ],
    )
    request = fake_request(
        tenant_id="tenant-a", account_id="account-1", platform="slack", channel_id="channel-1"
    )
    plan = select_runtime_manifest(request, OpenAIDeployment)
    assert plan.agent_id == "agent-pinned"


def test_wildcard_never_crosses_tenant_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_manifest(
        tmp_path,
        monkeypatch,
        [row(tenant_id="tenant-a", platform="*", channel_id="*", account_id="*")],
    )
    request = fake_request(
        tenant_id="tenant-b", account_id="account-1", platform="slack", channel_id="channel-1"
    )
    with pytest.raises(AdmissionDenied) as denied:
        select_runtime_manifest(request, OpenAIDeployment)
    assert denied.value.reason == "backend_unsupported"


def test_two_overlapping_wildcard_rows_refuse_as_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_manifest(
        tmp_path,
        monkeypatch,
        [
            row(platform="*", channel_id="*", account_id="*", agent_id="agent-one"),
            row(platform="*", channel_id="*", account_id="*", agent_id="agent-two"),
        ],
    )
    request = fake_request(
        tenant_id="tenant-a", account_id="account-1", platform="slack", channel_id="channel-1"
    )
    with pytest.raises(AdmissionDenied) as denied:
        select_runtime_manifest(request, OpenAIDeployment)
    assert denied.value.reason == "backend_unsupported"


def test_a_channel_row_for_every_account_wins_over_a_deployment_wide_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_manifest(
        tmp_path,
        monkeypatch,
        [
            row(platform="*", channel_id="*", account_id="*", agent_id="agent-default"),
            row(channel_id="channel-1", account_id="*", agent_id="agent-channel"),
        ],
    )
    pinned = fake_request(
        tenant_id="tenant-a", account_id="account-7", platform="slack", channel_id="channel-1"
    )
    assert select_runtime_manifest(pinned, OpenAIDeployment).agent_id == "agent-channel"
    other = fake_request(
        tenant_id="tenant-a", account_id="account-7", platform="slack", channel_id="channel-2"
    )
    assert select_runtime_manifest(other, OpenAIDeployment).agent_id == "agent-default"


def test_equally_specific_overlapping_wildcard_rows_still_refuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_manifest(
        tmp_path,
        monkeypatch,
        [
            row(channel_id="channel-1", account_id="*", agent_id="agent-channel"),
            row(channel_id="*", account_id="account-1", agent_id="agent-account"),
        ],
    )
    request = fake_request(
        tenant_id="tenant-a", account_id="account-1", platform="slack", channel_id="channel-1"
    )
    with pytest.raises(AdmissionDenied) as denied:
        select_runtime_manifest(request, OpenAIDeployment)
    assert denied.value.reason == "backend_unsupported"


def test_a_channel_row_and_an_account_row_refuse_instead_of_shadowing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_manifest(
        tmp_path,
        monkeypatch,
        [
            row(platform="slack", channel_id="channel-1", account_id="*", agent_id="agent-channel"),
            row(platform="*", channel_id="*", account_id="account-1", agent_id="agent-account"),
        ],
    )
    request = fake_request(
        tenant_id="tenant-a", account_id="account-1", platform="slack", channel_id="channel-1"
    )
    with pytest.raises(AdmissionDenied) as denied:
        select_runtime_manifest(request, OpenAIDeployment)
    assert denied.value.reason == "backend_unsupported"


def test_another_tenants_narrow_row_never_competes_with_this_tenants_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_manifest(
        tmp_path,
        monkeypatch,
        [
            row(tenant_id="tenant-b", channel_id="channel-1", account_id="*", agent_id="agent-b"),
            row(platform="*", channel_id="*", account_id="*", agent_id="agent-default"),
        ],
    )
    request = fake_request(
        tenant_id="tenant-a", account_id="account-1", platform="slack", channel_id="channel-1"
    )
    plan = select_runtime_manifest(request, OpenAIDeployment)
    assert (plan.agent_id, plan.tenant_id) == ("agent-default", "tenant-a")


@pytest.mark.parametrize("field", ["tenant_id", "profile"])
def test_wildcard_tenant_id_or_profile_refuses_at_startup_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    write_manifest(tmp_path, monkeypatch, [{**row(), field: "*"}])
    with pytest.raises(ProviderRuntimeConfigurationError) as refused:
        validate_configured_runtime_manifest()
    assert refused.value.reason == "wildcard_tenant_or_profile"


def test_channel_and_agent_rows_serve_only_their_own_turn_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from uuid import UUID

    from daimon.core.ma_identity import derive_agent_uuid

    tenant = UUID("00000000-0000-0000-0000-00000000000a")
    sourced = {
        **row(platform="*", channel_id="*", account_id="*", agent_id="agent-counterpart"),
        "source_agent_id": str(derive_agent_uuid(tenant_id=tenant, ma_agent_id="agent_src")),
        "source_agent_version": 2,
        "counterpart_catalog_id": "catalog",
        "counterpart_catalog_revision": 1,
    }
    write_manifest(
        tmp_path,
        monkeypatch,
        [row(platform="*", channel_id="*", account_id="*", agent_id="agent-fixed"), sourced],
    )
    request = fake_request(
        tenant_id="tenant-a", account_id="account-1", platform="slack", channel_id="channel-1"
    )
    assert select_runtime_manifest(request, OpenAIDeployment).agent_id == "agent-fixed"
    request.admission.backend_revision_origin = "agent"
    request.admission.agent = SimpleNamespace(id="agent_src", version=2)
    request.tenant_id, request.worker = tenant, None
    assert select_runtime_manifest(request, OpenAIDeployment).agent_id == "agent-counterpart"


def test_fleet_worker_rows_still_resolve_next_to_channel_and_template_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#fleet-test's worker rows (exact channel, '*' account, source daimon-fleet v1) win."""
    from uuid import UUID

    from daimon.core.ma_identity import derive_agent_uuid
    from daimon.core.turn.gemini_deployment import GeminiPlan
    from daimon.core.turn.runtime_manifest import validate_counterpart_request
    from mux.contracts.config import BackendConfig, ConfigRevision, resolve_default
    from mux.contracts.ids import ChannelRef

    tenant = "449f4dc5-5990-52c0-96e0-45a4ab3bf0e4"
    channel = "1562712749321093122"
    native_agent = "agent_01DiAnUE5dV3E2Pr3ZosHCTS"
    source = derive_agent_uuid(tenant_id=UUID(tenant), ma_agent_id=native_agent)
    grant = {"profiles": ["gemini.inline_reuse"], "thread_modes": ["per_caller"], "requires": {}}
    common = {
        "profile": "gemini.inline_reuse",
        "tenant_id": tenant,
        "project": "gen-lang-client",
        "api_key_env": "DAIMON_CHANNEL_GEMINI_KEY",
        "storage_backend": "postgres",
        "hold_usd": "0.25",
        "cap_usd": "0.50",
        "tools": [],
        "environment": {"name": "remote"},
    }
    worker = {
        **common,
        "platform": "discord",
        "channel_id": channel,
        "account_id": "*",
        "source_agent_id": str(source),
        "source_agent_version": 1,
        "worker_materialization": True,
        "configuration_grant": {**grant, "models": ["gemini-3.8-flash"]},
        "agent_name": "daimon-fleet",
    }
    star = {"platform": "*", "channel_id": "*", "account_id": "*"}
    models = {"configuration_grant": {**grant, "models": ["gemini-3.8-flash", "gemini-4-argon"]}}
    d3 = {**common, **star, **models, "agent_name": "daimon-gemini"}
    template = {**common, **star, **models, "agent_name": "t", "root_materialization": True}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps([worker, d3, template]))
    monkeypatch.setenv("DAIMON_TURN__PROVIDER_RUNTIME_FILE", str(path))
    revision = ConfigRevision.create(
        ChannelRef(tenant_id=tenant, platform="discord", channel_id=channel),
        3,
        resolve_default(
            BackendConfig(backend="gemini", profile="gemini.inline_reuse", model="gemini-3.8-flash")
        ),
    )
    account = "7c1e0e3a-84d9-4102-9164-40027e27be37"
    request = SimpleNamespace(
        admission=SimpleNamespace(
            backend_revision=revision,
            backend_revision_origin="agent",
            account_id=UUID(account),
            agent=SimpleNamespace(id=native_agent, version=1),
        ),
        scope=SimpleNamespace(tenant_id=tenant, account_id=account),
        deps=SimpleNamespace(channel_backends=True, agent_backends=True, turn_path="mux"),
        tenant_id=UUID(tenant),
        session_account_id=UUID(account),
        platform="discord",
        worker=object(),
        before_native=object(),
    )
    plan = select_runtime_manifest(request, GeminiPlan)
    assert plan.worker_materialization and plan.agent_name == "daimon-fleet"
    assert validate_counterpart_request(plan, request, worker_materialization=True) == revision


def _worker_request(tenant, channel, native_agent, version, *, worker=True):
    from uuid import UUID

    from mux.contracts.config import BackendConfig, ConfigRevision, resolve_default
    from mux.contracts.ids import ChannelRef

    revision = ConfigRevision.create(
        ChannelRef(tenant_id=tenant, platform="discord", channel_id=channel),
        3,
        resolve_default(
            BackendConfig(backend="gemini", profile="gemini.inline_reuse", model="gemini-3.8-flash")
        ),
    )
    account = "7c1e0e3a-84d9-4102-9164-40027e27be37"
    return revision, SimpleNamespace(
        admission=SimpleNamespace(
            backend_revision=revision,
            backend_revision_origin="agent",
            account_id=UUID(account),
            agent=SimpleNamespace(id=native_agent, version=version),
        ),
        scope=SimpleNamespace(tenant_id=tenant, account_id=account),
        deps=SimpleNamespace(channel_backends=True, agent_backends=True, turn_path="mux"),
        tenant_id=UUID(tenant),
        session_account_id=UUID(account),
        platform="discord",
        worker=object() if worker else None,
        before_native=object() if worker else None,
    )


def _gemini_worker_row(tenant, **changes):
    row = {
        "profile": "gemini.inline_reuse",
        "tenant_id": tenant,
        "platform": "*",
        "channel_id": "*",
        "account_id": "*",
        "source_agent_id": "*",
        "source_agent_version": "*",
        "worker_materialization": True,
        "configuration_grant": {
            "profiles": ["gemini.inline_reuse"],
            "models": ["gemini-3.8-flash"],
            "thread_modes": ["per_caller"],
            "requires": {},
        },
        "project": "gen-lang-client",
        "api_key_env": "DAIMON_CHANNEL_GEMINI_KEY",
        "storage_backend": "postgres",
        "hold_usd": "0.25",
        "cap_usd": "0.50",
        "agent_name": "any-worker",
        "tools": [],
        "environment": {"name": "remote"},
    }
    return row | changes


TENANT_A = "449f4dc5-5990-52c0-96e0-45a4ab3bf0e4"


def test_a_worker_template_serves_any_agent_version_and_channel_of_its_tenant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from daimon.core.turn.gemini_deployment import GeminiPlan
    from daimon.core.turn.runtime_manifest import validate_counterpart_request

    write_manifest(tmp_path, monkeypatch, [_gemini_worker_row(TENANT_A)])
    for channel, native_agent, version in (
        ("1562712749321093122", "agent_root", 1),
        ("999000111", "agent_root", 7),  # the agent was edited: a new live version
        # A worker running AS another granted agent of the tenant (agent-as-worker).
        ("999000111", "agent_other", 2),
    ):
        revision, request = _worker_request(TENANT_A, channel, native_agent, version)
        plan = select_runtime_manifest(request, GeminiPlan)
        assert plan.worker_materialization and plan.source_agent_id is None
        assert (plan.channel_id, plan.account_id) == (channel, request.scope.account_id)
        assert validate_counterpart_request(plan, request, worker_materialization=True) == revision


def test_a_worker_template_never_serves_another_tenant_or_a_channel_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from daimon.core.turn.gemini_deployment import GeminiPlan

    write_manifest(tmp_path, monkeypatch, [_gemini_worker_row(TENANT_A)])
    _, foreign = _worker_request("00000000-0000-0000-0000-00000000000b", "c", "agent_root", 1)
    with pytest.raises(AdmissionDenied):
        select_runtime_manifest(foreign, GeminiPlan)
    _, channel_turn = _worker_request(TENANT_A, "c", "agent_root", 1, worker=False)
    channel_turn.admission.backend_revision_origin = "channel"
    with pytest.raises(AdmissionDenied):
        select_runtime_manifest(channel_turn, GeminiPlan)


def _source(native_agent):
    from uuid import UUID

    from daimon.core.ma_identity import derive_agent_uuid

    return str(derive_agent_uuid(tenant_id=UUID(TENANT_A), ma_agent_id=native_agent))


def test_a_pinned_worker_row_serves_only_its_own_agent_and_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from daimon.core.turn.gemini_deployment import GeminiPlan
    from daimon.core.turn.runtime_manifest import validate_counterpart_request

    channel = "1562712749321093122"
    pinned = _gemini_worker_row(
        TENANT_A,
        platform="discord",
        channel_id=channel,
        source_agent_id=_source("agent_a"),
        source_agent_version=1,
        agent_name="pinned",
    )
    write_manifest(tmp_path, monkeypatch, [_gemini_worker_row(TENANT_A), pinned])
    for here, native_agent, version, expected in (
        (channel, "agent_a", 1, "pinned"),
        # Another agent's worker in that channel falls through to the template.
        (channel, "agent_b", 1, "any-worker"),
        # So does the pinned agent once edited, and the pinned agent elsewhere.
        (channel, "agent_a", 2, "any-worker"),
        ("999000111", "agent_a", 1, "any-worker"),
    ):
        revision, request = _worker_request(TENANT_A, here, native_agent, version)
        plan = select_runtime_manifest(request, GeminiPlan)
        assert plan.agent_name == expected, (here, native_agent, version)
        assert validate_counterpart_request(plan, request, worker_materialization=True) == revision


def _gemini_root_template(tenant, **changes):
    row = {
        key: value
        for key, value in _gemini_worker_row(tenant).items()
        if key not in ("source_agent_id", "source_agent_version", "worker_materialization")
    }
    return (
        row | {"root_materialization": True, "agent_name": "any-root", "hold_usd": "0.40"} | changes
    )


def test_root_and_worker_templates_coexist_in_one_tenant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from daimon.core.turn.gemini_deployment import GeminiPlan
    from daimon.core.turn.runtime_manifest import manifest_hold_usd

    write_manifest(
        tmp_path, monkeypatch, [_gemini_root_template(TENANT_A), _gemini_worker_row(TENANT_A)]
    )
    revision, worker = _worker_request(TENANT_A, "999000111", "agent_a", 3)
    _, root = _worker_request(TENANT_A, "999000111", "agent_a", 3, worker=False)
    assert select_runtime_manifest(root, GeminiPlan).agent_name == "any-root"
    assert select_runtime_manifest(worker, GeminiPlan).agent_name == "any-worker"
    account = worker.scope.account_id
    assert manifest_hold_usd(revision, tenant_id=TENANT_A, account_id=account) == Decimal("0.25")


def test_a_pinned_worker_hold_applies_only_to_its_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from daimon.core.turn.runtime_manifest import manifest_hold_usd

    channel = "1562712749321093122"
    pinned = _gemini_worker_row(
        TENANT_A,
        platform="discord",
        channel_id=channel,
        source_agent_id=_source("agent_a"),
        source_agent_version=1,
        hold_usd="0.49",
    )
    write_manifest(tmp_path, monkeypatch, [_gemini_worker_row(TENANT_A), pinned])
    revision, request = _worker_request(TENANT_A, channel, "agent_a", 1)

    def hold(source, version=None):
        return manifest_hold_usd(
            revision,
            tenant_id=TENANT_A,
            account_id=request.scope.account_id,
            source_agent_id=source,
            source_agent_version=version,
        )

    assert hold(_source("agent_a"), 1) == Decimal("0.49")
    assert hold(_source("agent_a").upper(), 1) == Decimal("0.49"), "pins compare as UUIDs"
    assert hold(_source("agent_b"), 1) == Decimal("0.25")
    # A stale pin (v1) never sizes a worker of the edited agent (v2): the template does.
    assert hold(_source("agent_a"), 2) == Decimal("0.25")
    assert hold(None) == Decimal("0.25")


@pytest.mark.parametrize("field", ["source_agent_id", "source_agent_version"])
def test_any_source_is_only_a_worker_template(field) -> None:
    from daimon.core.turn.gemini_deployment import GeminiPlan
    from daimon.core.turn.openai_deployment import OpenAIDeployment
    from pydantic import ValidationError

    row = _gemini_root_template(TENANT_A)
    with pytest.raises(ValidationError, match="only a worker template"):
        GeminiPlan.model_validate(row | {field: "*"})
    openai = {
        "profile": "openai.conversation_only",
        "tenant_id": TENANT_A,
        "platform": "*",
        "channel_id": "*",
        "account_id": "*",
        "configuration_grant": {
            "profiles": ["openai.conversation_only"],
            "models": ["gpt-6-luna"],
            "thread_modes": ["per_caller"],
            "requires": {},
        },
        "project": "p",
        "agent_id": "a",
        "api_key_env": "DAIMON_CHANNEL_OPENAI_KEY",
        field: "*",
    }
    with pytest.raises(ValidationError, match="only a worker template"):
        OpenAIDeployment.model_validate(openai)


@pytest.mark.parametrize("half", [{"source_agent_id": None}, {"source_agent_version": None}])
def test_a_worker_row_pins_both_or_neither_source_field(half) -> None:
    from daimon.core.turn.gemini_deployment import GeminiPlan
    from pydantic import ValidationError

    row = _gemini_worker_row(
        TENANT_A, source_agent_id="00000000-0000-0000-0000-0000000000f1", source_agent_version=1
    )
    with pytest.raises(ValidationError):
        GeminiPlan.model_validate(row | half)
