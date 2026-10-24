"""Operator grants admit only bounded settings, without credential or SDK I/O."""

import copy
import os
from dataclasses import replace

import pytest
from daimon.core.channel_backend import check_backend
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.gemini_deployment import load_gemini_deployment
from daimon.core.turn.openai_deployment import load_deployment
from mux.contracts.config import BackendConfig, ConfigRevision, resolve_default

from .test_gemini_deployment import entry as gemini_entry
from .test_openai_deployment import application as application
from .test_openai_deployment import entry as openai_entry
from .test_openai_deployment import manifest


@pytest.fixture(params=["openai", "gemini"])
def planned(request, application, tmp_path, monkeypatch):
    selected = application
    loader = load_deployment
    row = openai_entry()
    if request.param == "gemini":
        revision = ConfigRevision.create(
            application.admission.backend_revision.channel,
            2,
            resolve_default(
                BackendConfig(
                    backend="gemini", profile="gemini.inline_reuse", model="gemini-3.8-flash"
                )
            ),
        )
        selected = replace(
            application,
            admission=replace(
                application.admission, backend_revision=revision, backend=check_backend(revision)
            ),
        )
        loader = load_gemini_deployment
        row = gemini_entry(selected, tmp_path)
    revision = selected.admission.backend_revision
    row["config_digest"] = "prior-operator-digest"
    row["configuration_grant"] = {
        "profiles": [revision.profile],
        "models": [revision.model],
        "thread_modes": [revision.thread_mode],
        "requires": {},
    }
    original = os.environ.get

    def no_key_read(name, default=None):
        if name == row["api_key_env"]:
            raise AssertionError("loader inspected a credential")
        return original(name, default)

    monkeypatch.setattr(os.environ, "get", no_key_read)
    return selected, loader, row


async def test_actual_loaders_accept_changed_digest_within_explicit_bounds(
    planned, monkeypatch, tmp_path
):
    selected, loader, row = planned
    manifest(monkeypatch, tmp_path / "runtime.json", [row])
    plan = loader(selected)
    assert plan.configuration_grant is not None
    assert (
        plan.tenant_id == selected.scope.tenant_id and plan.account_id == selected.scope.account_id
    )
    assert plan.config_digest == "prior-operator-digest"


@pytest.mark.parametrize(
    "field,value",
    [
        ("profiles", ["ungranted.profile"]),
        ("models", ["ungranted-model"]),
        ("thread_modes", ["shared"]),
        ("requires", {"multiagent": {"level": "required"}}),
        ("wildcard", True),
        ("models", []),
    ],
)
async def test_actual_loaders_refuse_each_out_of_bounds_or_open_grant(
    planned, monkeypatch, tmp_path, field, value
):
    selected, loader, row = planned
    row = copy.deepcopy(row)
    row["configuration_grant"][field] = value
    manifest(monkeypatch, tmp_path / "runtime.json", [row])
    with pytest.raises(AdmissionDenied):
        loader(selected)


@pytest.mark.parametrize("field", ["tenant_id", "account_id", "platform", "channel_id"])
async def test_actual_loaders_keep_every_scope_filter_with_bounded_grant(
    planned, monkeypatch, tmp_path, field
):
    selected, loader, row = planned
    row[field] = "foreign"
    manifest(monkeypatch, tmp_path / "runtime.json", [row])
    with pytest.raises(AdmissionDenied):
        loader(selected)


async def test_legacy_digest_and_ambiguous_rows_remain_closed(planned, monkeypatch, tmp_path):
    selected, loader, row = planned
    legacy = copy.deepcopy(row)
    legacy.pop("configuration_grant")
    manifest(monkeypatch, tmp_path / "runtime.json", [legacy])
    with pytest.raises(AdmissionDenied):
        loader(selected)
    legacy["config_digest"] = selected.admission.backend_revision.digest
    manifest(monkeypatch, tmp_path / "runtime.json", [legacy])
    assert loader(selected).configuration_grant is None
    manifest(monkeypatch, tmp_path / "runtime.json", [row, row])
    with pytest.raises(AdmissionDenied):
        loader(selected)


async def test_actual_loaders_refuse_unproven_agent_counterpart_before_manifest_or_key_read(
    planned, monkeypatch
):
    selected, loader, _row = planned
    # Preserve the actual fixture admission, changing only the trusted selection origin.
    origin = replace(selected.admission, backend_revision_origin="agent")
    # Agent backends are on by default; this is the operator stop.
    selected = replace(
        selected, admission=origin, deps=replace(selected.deps, agent_backends=False)
    )
    monkeypatch.setattr(
        os.environ,
        "get",
        lambda *args: (_ for _ in ()).throw(AssertionError("unproven origin read configuration")),
    )
    with pytest.raises(AdmissionDenied):
        loader(selected)
