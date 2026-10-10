"""Boot backfill uses real MA SDK requests against the shared HTTP fake."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
from daimon.core.agent_guidance import CREDENTIAL_GUIDANCE_BLOCK, apply_credential_guidance
from daimon.core.defaults.backfill_guidance import backfill_credential_guidance
from daimon.testing.ma import MARouter, build_fake_anthropic, json_body, list_response
from daimon.testing.ma_models import ma_agent

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
