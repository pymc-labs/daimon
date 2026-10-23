"""A configured revision cannot acquire channel authority from an absent origin."""

import os
from dataclasses import replace

import pytest
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.prepare import bind_session_impl

from .test_configuration_grants import planned as planned
from .test_openai_deployment import application as application
from .test_provider_dispatch import ACCOUNT, TENANT, admission


@pytest.fixture
def origin_plan(db_preflight, planned):
    return planned


@pytest.mark.parametrize("origin", [None, "unknown", "channel_clear", ""])
def test_revision_requires_a_recognized_origin(origin):
    with pytest.raises(AdmissionDenied, match="backend_unsupported"):
        replace(admission(), backend_revision_origin=origin)


def test_no_revision_retains_the_unconfigured_default():
    unconfigured = replace(admission(), backend_revision=None, backend_revision_origin=None)
    assert unconfigured.backend_revision is None
    assert unconfigured.backend_revision_origin is None


@pytest.mark.parametrize("origin", [None, "unknown", "agent"])
def test_native_loader_independently_refuses_missing_or_unproven_origin(
    origin_plan, monkeypatch, origin
):
    selected, loader, _ = origin_plan
    # Agent backends are on by default; an agent origin is refused under the operator stop.
    selected = replace(selected, deps=replace(selected.deps, agent_backends=False))
    # Simulate a stale external producer that bypasses the constructor guard.
    object.__setattr__(selected.admission, "backend_revision_origin", origin)
    monkeypatch.setattr(
        os.environ,
        "get",
        lambda *args: (_ for _ in ()).throw(AssertionError("origin refusal read configuration")),
    )
    with pytest.raises(AdmissionDenied, match="backend_unsupported"):
        loader(selected)


@pytest.mark.parametrize("origin", [None, "unknown", "agent"])
async def test_native_preparation_refuses_origin_before_runtime_or_configuration(
    origin_plan, monkeypatch, origin
):
    selected, _, _ = origin_plan
    object.__setattr__(selected.admission, "backend_revision_origin", origin)
    monkeypatch.setattr(
        os.environ,
        "get",
        lambda *args: (_ for _ in ()).throw(AssertionError("origin refusal read configuration")),
    )
    with pytest.raises(AdmissionDenied, match="backend_unsupported"):
        await bind_session_impl(
            selected.deps,
            selected.admission,
            tenant_id=TENANT,
            platform="slack",
            external_user_id="caller",
            thread_id="thread",
            session_account_id=ACCOUNT,
            reuse_existing=True,
            deadline=selected.deadline,
            now=selected.now,
        )
