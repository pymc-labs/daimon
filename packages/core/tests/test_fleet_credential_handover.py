"""CREDSHARE v1: a shared stored key reaches a native worker, at its approved version only.

(Connector logins and the per-worker vault are A-3.)
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from daimon.core import fleet_credentials
from daimon.core.stores.domain import BackendAgent
from daimon.core.turn.runtime_manifest import classify_source_obligations, worker_source_ceiling
from mux.contracts.ids import ModelRef
from mux.contracts.resources import AgentSpec, MCPConnection, ResourceBinding, ToolSpec

STORED = f"agent-mcp:{uuid.uuid4()}"


def credential(reference: str, target: str | None = None) -> ResourceBinding:
    return ResourceBinding(
        id=reference, kind="credential", credential_ref=reference, target_path=target
    )


def source() -> BackendAgent:
    tenant, agent = uuid.uuid4(), uuid.uuid4()
    env = f"agent-env:{tenant}:{agent}"
    return BackendAgent(
        tenant_id=tenant,
        agent_id=agent,
        name="daimon",
        configuration_digest="c",
        effective_spec=AgentSpec(
            name="daimon",
            model=ModelRef(provider="anthropic", id="claude-haiku-5-5"),
            mcp_servers=(
                MCPConnection(name="linear", url="https://linear.example.test/mcp"),
                MCPConnection(name="drive", url="https://drive.example.test/mcp"),
            ),
            tools=(
                ToolSpec(name="linear", kind="mcp_toolset"),
                ToolSpec(name="drive", kind="mcp_toolset"),
            ),
        ),
        resource_requirements=(
            credential(STORED),
            credential(f"{env}:SHARED_KEY", ".env"),
            credential(f"{env}:OTHER_KEY", ".env"),
        ),
    )


def test_the_ceiling_keeps_exactly_the_shared_logins_and_their_connector():
    agent = source()
    plain = worker_source_ceiling(agent)
    assert plain.resource_requirements == ()
    assert not plain.effective_spec.mcp_servers
    shared = frozenset({STORED, "env:SHARED_KEY"})
    ceiling = worker_source_ceiling(agent, shared=shared, shared_servers=frozenset({"linear"}))
    assert {server.name for server in ceiling.effective_spec.mcp_servers} == {"linear"}
    assert {tool.name for tool in ceiling.effective_spec.tools or ()} == {"linear"}
    kept = {fleet_credentials.login_key_of(binding) for binding in ceiling.resource_requirements}
    assert kept == shared
    # The unshared key and connector are still declared losses.
    assert {"files", "mcp"} <= set(ceiling.effective_spec.declared_drops)
    assert "OTHER_KEY" not in json.dumps(
        [binding.model_dump(mode="json") for binding in ceiling.resource_requirements]
    )


def test_shared_logins_are_proven_and_only_a_shared_ceiling_carries_them():
    """The classifier proves the frozen shared logins; what bounds a worker is that only
    a root ceiling built from that frozen set contains them (a worker's resources must
    be a subset of its root's, `resolve_worker_spec`)."""
    agent = source()
    shared = frozenset({STORED, "env:SHARED_KEY"})
    ceiling = worker_source_ceiling(agent, shared=shared, shared_servers=frozenset({"linear"}))
    missing = classify_source_obligations(
        agent,
        ceiling.effective_spec,
        environment=None,
        resources=ceiling.resource_requirements,
        worker_ceiling=True,
        shared=shared,
        shared_servers=frozenset({"linear"}),
    )
    assert not {"credentials", "mcp", "files"} & missing
    # Without the frozen decision the root's ceiling has none of them to hand out.
    unshared = worker_source_ceiling(agent)
    assert not unshared.resource_requirements and not unshared.effective_spec.mcp_servers
    # A share of one login never carries another.
    one = worker_source_ceiling(agent, shared=frozenset({"env:SHARED_KEY"}))
    assert {fleet_credentials.login_key_of(b) for b in one.resource_requirements} == {
        "env:SHARED_KEY"
    }
    assert not one.effective_spec.mcp_servers


@pytest.mark.parametrize("changed", [False, True])
async def test_a_workers_env_has_only_its_shared_keys_at_their_approved_version(
    monkeypatch, changed
):
    from daimon.core import credential_env
    from daimon.core.fleet_credentials import stored_key_version
    from mux.errors import ScopeViolation

    setter, when = uuid.uuid4(), datetime(2026, 10, 23, 9, tzinfo=UTC)
    rows = [
        type("Row", (), {"key": key, "last_set_by_account_id": setter, "updated_at": when})()
        for key in ("SHARED_KEY", "OTHER_KEY")
    ]
    uploaded: list[list[str]] = []

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    async def list_files(session, *, tenant_id, agent_id):
        return rows

    async def upload(anthropic, session_factory, *, rows, **kwargs):
        uploaded.append([row.key for row in rows])
        return "file_1"

    monkeypatch.setattr(credential_env, "list_agent_files", list_files)
    monkeypatch.setattr(credential_env, "upload_env_file", upload)
    monkeypatch.setattr(credential_env, "session_env_key", lambda key, **kwargs: True)
    approved = stored_key_version(setter, when if not changed else when - timedelta(days=1))
    call = credential_env.upload_env_and_mount(
        None,
        lambda: Session(),
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        keys={"SHARED_KEY": approved},
    )
    if changed:
        # Rotated since it was approved: the worker refuses, nothing is uploaded.
        with pytest.raises(ScopeViolation, match="changed since it was approved"):
            await call
        assert uploaded == []
    else:
        await call
        assert uploaded == [["SHARED_KEY"]]
