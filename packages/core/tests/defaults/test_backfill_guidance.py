"""Boot backfill uses real MA SDK requests against the shared HTTP fake."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from daimon.core.agent_guidance import CREDENTIAL_GUIDANCE_BLOCK, apply_credential_guidance
from daimon.core.defaults.backfill_guidance import backfill_credential_guidance
from daimon.testing.ma_models import ma_agent
from mux.testing.drivers.anthropic.ma import (
    MARouter,
    build_fake_anthropic,
    json_body,
    list_response,
)
from mux.testing.drivers.anthropic.ma_transport import ScriptedReply, ScriptedTransport
from structlog.testing import capture_logs

TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
OLD = "<!-- daimon:credential-guidance v1 -->\nOld GitHub token advice\n<!-- /daimon:credential-guidance -->\n\nMy own instructions"


def _agent(id: str, system: str, **kwargs: Any) -> dict[str, Any]:
    return ma_agent(id=id, name=id, tenant_id=TENANT, system=system, **kwargs).model_dump(
        mode="json"
    )


async def test_backfill_updates_only_system_and_is_idempotent() -> None:
    agents = [
        _agent("old", OLD, metadata={"daimon_spec_hash": "old-hash"}),
        _agent("missing", "My own instructions"),
        _agent("isolated", OLD, metadata={"daimon_isolated": "true"}),
        _agent("seeded", OLD, metadata={"daimon_managed": "true"}),
        ma_agent(
            id="seeded_legacy",
            name="daimon",
            tenant_id=TENANT,
            system=OLD,
            metadata={"daimon_account": str(TENANT)},
        ).model_dump(mode="json"),
        _agent("archived", OLD, archived_at=datetime(2026, 1, 2, tzinfo=UTC)),
    ]
    writes: list[tuple[str, dict[str, Any]]] = []
    router = MARouter()

    def listing(_req: httpx.Request, _match: object) -> httpx.Response:
        return list_response([agent for agent in agents if agent["archived_at"] is None])

    def update(req: httpx.Request, match: Any) -> httpx.Response:
        body = json_body(req)
        agent_id = match.group(1)
        writes.append((agent_id, body))
        agent = next(a for a in agents if a["id"] == agent_id)
        assert body == {"version": agent["version"], "system": body["system"]}
        agent["system"] = body["system"]
        agent["version"] += 1
        return httpx.Response(200, json=agent)

    router.add("GET", r"/v1/agents", listing)
    router.add("POST", r"/v1/agents/([^/]+)", update)
    client = build_fake_anthropic(router.dispatch)
    await backfill_credential_guidance(
        client,
        tenant_id=TENANT,
        seeded_agent_ids={"seeded"},
        seeded_agent_names={"seeded", "daimon"},
        seeded_account_id=TENANT,
    )
    assert [id for id, _ in writes] == ["old", "missing"]
    assert agents[0]["system"] == apply_credential_guidance(OLD)
    assert agents[1]["system"] == CREDENTIAL_GUIDANCE_BLOCK + "\n\nMy own instructions"
    assert agents[0]["metadata"]["daimon_spec_hash"] == "old-hash"
    assert all(agent["system"] == OLD for agent in agents[2:])

    await backfill_credential_guidance(
        client,
        tenant_id=TENANT,
        seeded_agent_ids={"seeded"},
        seeded_agent_names={"seeded", "daimon"},
        seeded_account_id=TENANT,
    )
    assert len(writes) == 2


async def test_backfill_continues_after_ma_failure() -> None:
    agents = [_agent("fails", OLD), _agent("works", OLD)]
    writes: list[str] = []
    router = MARouter()
    router.add("GET", r"/v1/agents", lambda _r, _m: list_response(agents))

    def update(req: httpx.Request, match: Any) -> httpx.Response:
        agent_id = match.group(1)
        writes.append(agent_id)
        if agent_id == "fails":
            return httpx.Response(
                409,
                json={
                    "type": "error",
                    "error": {
                        "type": "invalid_request_error",
                        "message": "Concurrent modification detected",
                    },
                },
            )
        agent = next(a for a in agents if a["id"] == agent_id)
        agent["system"] = json_body(req)["system"]
        return httpx.Response(200, json=agent)

    router.add("POST", r"/v1/agents/([^/]+)", update)
    await backfill_credential_guidance(
        build_fake_anthropic(router.dispatch),
        tenant_id=TENANT,
        seeded_agent_ids=set(),
        seeded_agent_names=set(),
        seeded_account_id=None,
    )
    assert writes[-1] == "works"
    assert all(agent_id == "fails" for agent_id in writes[:-1])
    assert agents[1]["system"] == apply_credential_guidance(OLD)


@pytest.mark.parametrize("status,partial", [(200, False), (409, False), (500, False), (200, True)])
async def test_backfill_port_preserves_actual_sdk_requests_and_conflict_attempts(
    status: int, partial: bool, tmp_path: Path
) -> None:
    owned = _agent("owned", OLD)
    foreign = ma_agent(id="foreign", name="foreign", tenant_id=uuid.uuid4(), system=OLD).model_dump(
        mode="json"
    )
    system = apply_credential_guidance(OLD)
    expected = {"version": owned["version"], "system": system}
    reply = (
        httpx.Response(
            200,
            json=(
                {"id": None, "metadata": None}
                if partial
                else {**owned, "system": system, "version": owned["version"] + 1}
            ),
        )
        if status == 200
        else httpx.Response(
            status,
            json={
                "type": "error",
                "error": {
                    "type": "invalid_request_error",
                    "message": "Concurrent modification detected",
                },
            },
        )
    )
    baseline = ScriptedTransport()
    migrated = ScriptedTransport()
    for trace in (baseline, migrated):
        trace.queue(
            ScriptedReply("GET", "/v1/agents", list_response([foreign, owned])),
            ScriptedReply(
                "POST", "/v1/agents/owned", reply, request_json=expected, check_json=True
            ),
        )
    async with baseline.client() as client:
        # Main #718's native request sequence, including its tenant-filtered read.
        agents = [
            agent
            async for agent in client.beta.agents.list(include_archived=False, limit=100)
            if agent.metadata.get("daimon_tenant") == str(TENANT)
        ]
        assert len(agents) == 1
        try:
            await client.beta.agents.update(agents[0].id, version=agents[0].version, system=system)
        except Exception:
            if status == 200:
                raise
    with capture_logs() as logs:
        async with migrated.client() as client:
            await backfill_credential_guidance(
                client,
                tenant_id=TENANT,
                seeded_agent_ids=set(),
                seeded_agent_names=set(),
                seeded_account_id=None,
            )
    summaries = [row for row in logs if row["event"] == "agents.guidance_backfill_done"]
    assert len(summaries) == 1
    assert summaries[0]["updated"] == (1 if status == 200 else 0)
    assert summaries[0]["failed"] == (0 if status == 200 else 1)
    baseline.assert_consumed()
    migrated.assert_consumed()
    assert migrated.requests == baseline.requests
    assert len(migrated.requests) == 2
    assert migrated.requests[-1].body == baseline.requests[-1].body
    assert migrated.requests[-1].json() == expected
    (tmp_path / f"guidance-port-{status}-{partial}-bytes.json").write_text(
        json.dumps(
            {
                "status": status,
                "partial_reply": partial,
                "byte_identical": True,
                "requests": [
                    {
                        "method": r.method,
                        "path": r.path,
                        "query": r.query,
                        "body_hex": r.body.hex(),
                        "protocol_headers": r.protocol_headers,
                    }
                    for r in migrated.requests
                ],
            },
            indent=2,
        )
        + "\n"
    )


@pytest.mark.parametrize("violation", ["foreign-scope", "unbound-resource"])
async def test_guidance_update_refuses_unbound_resource_before_io(violation: str) -> None:
    from daimon.core.mux_backend import resource_scope
    from mux.contracts.ids import Revision
    from mux.contracts.resources import AgentPatch
    from mux.drivers.anthropic.resources._authorization import ResourceAuthorization
    from mux.drivers.anthropic.resources.agents import update_agent_ignored
    from mux.errors import ScopeViolation

    admitted = resource_scope(tenant_id=str(TENANT))
    requested = (
        resource_scope(tenant_id=str(uuid.uuid4())) if violation == "foreign-scope" else admitted
    )
    authorization = ResourceAuthorization(
        admitted, frozenset({("agent", "other" if violation == "unbound-resource" else "owned")})
    )
    trace = ScriptedTransport()
    async with trace.client() as client:
        with pytest.raises(ScopeViolation):
            await update_agent_ignored(
                client,
                requested,
                "owned",
                AgentPatch(system="guidance"),
                expected=Revision(local=1, native="1"),
                authorization=authorization,
            )
    trace.assert_consumed()
    assert not trace.requests
