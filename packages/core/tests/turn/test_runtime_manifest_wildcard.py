"""A deployment-wide default row authorizes every channel/account for one
tenant without ever letting `tenant_id` or `profile` itself wildcard — see
`select_runtime_manifest` in `daimon.core.turn.runtime_manifest`."""

from __future__ import annotations

import json
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
