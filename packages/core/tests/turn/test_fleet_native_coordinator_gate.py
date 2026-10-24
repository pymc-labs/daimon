"""A GPT or Gemini root may coordinate a fleet: its native counterpart is validated,
not refused for existing (Astra review R02, latent-audit M07)."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from daimon.core.turn.runtime_manifest import _counterpart_admitted

SCOPE = SimpleNamespace(tenant_id="tenant-a")


def _admission(
    provider,
    model,
    *,
    tenant="tenant-a",
    agent_model=None,
    lifecycle="active",
    archived=None,
    revision=True,
):
    agent = SimpleNamespace(
        ref=SimpleNamespace(tenant_id=tenant),
        spec=SimpleNamespace(model=SimpleNamespace(provider=provider, id=agent_model or model)),
        lifecycle=lifecycle,
        archived_at=archived,
    )
    return SimpleNamespace(
        backend_counterpart=SimpleNamespace(agent=agent),
        backend_revision=SimpleNamespace(backend=provider, model=model) if revision else None,
    )


@pytest.mark.parametrize(
    ("provider", "model"), [("openai", "gpt-6-luna"), ("gemini", "gemini-3.8-flash")]
)
def test_the_admitted_counterpart_may_coordinate(provider, model):
    assert _counterpart_admitted(_admission(provider, model), SCOPE)


def test_a_root_without_a_counterpart_is_unaffected():
    assert _counterpart_admitted(
        SimpleNamespace(backend_counterpart=None, backend_revision=None), SCOPE
    )


@pytest.mark.parametrize(
    "fault",
    [
        {"tenant": "tenant-b"},
        {"agent_model": "gpt-6-astra"},
        {"lifecycle": "archived"},
        {"archived": datetime(2026, 10, 1, tzinfo=UTC)},
        {"revision": False},
    ],
)
def test_a_counterpart_that_isnt_the_admitted_one_refuses(fault):
    assert not _counterpart_admitted(_admission("openai", "gpt-6-luna", **fault), SCOPE)


def test_a_counterpart_on_another_provider_than_the_revision_refuses():
    admission = _admission("openai", "gpt-6-luna")
    admission.backend_revision.backend = "gemini"
    assert not _counterpart_admitted(admission, SCOPE)
