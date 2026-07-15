"""Probe whether DELETE /v1/agents/{id} hard-deletes an agent.

Tests:
1. Create an agent via POST
2. Verify it exists via GET
3. DELETE it via raw HTTP DELETE /v1/agents/{id}
4. Record the DELETE response status code and body
5. Try to GET it again — does it 404, or is it still there?
6. Try to LIST agents and check if the deleted agent appears
7. DELETE an already-archived agent (POST /agents/{id}/archive first)
8. Create a new agent with the same name after DELETE — is name reuse allowed?

Run:
    uv run python scripts/probes/managed_agents/agent_hard_delete.py
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import httpx
from dotenv import load_dotenv

API_BASE = "https://api.anthropic.com/v1"
BETA = "managed-agents-2026-04-01"


def headers(json_body: bool = True) -> dict[str, str]:
    h = {
        "x-api-key": os.environ["ANTHROPIC_API_KEY"],
        "anthropic-version": "2023-06-01",
        "anthropic-beta": BETA,
    }
    if json_body:
        h["content-type"] = "application/json"
    return h


def agent_body(name: str) -> dict:
    return {
        "name": name,
        "model": {"id": "claude-haiku-4-5", "speed": "standard"},
        "system": "probe agent — safe to delete",
        "skills": [],
        "tools": [],
        "mcp_servers": [],
    }


async def create_agent(http: httpx.AsyncClient, name: str) -> tuple[int, dict | str]:
    r = await http.post(f"{API_BASE}/agents", headers=headers(), json=agent_body(name))
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, r.text


async def get_agent(http: httpx.AsyncClient, agent_id: str) -> tuple[int, dict | str]:
    r = await http.get(f"{API_BASE}/agents/{agent_id}", headers=headers(json_body=False))
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, r.text


async def delete_agent(http: httpx.AsyncClient, agent_id: str) -> tuple[int, str | dict]:
    r = await http.delete(f"{API_BASE}/agents/{agent_id}", headers=headers(json_body=False))
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, r.text


async def archive_agent(http: httpx.AsyncClient, agent_id: str) -> tuple[int, dict | str]:
    r = await http.post(
        f"{API_BASE}/agents/{agent_id}/archive", headers=headers(), json={}
    )
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, r.text


async def list_agents(http: httpx.AsyncClient) -> tuple[int, dict | str]:
    r = await http.get(f"{API_BASE}/agents", headers=headers(json_body=False), params={"limit": 100})
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, r.text


def fmt(body: dict | str) -> str:
    if isinstance(body, dict):
        return json.dumps(body)
    return str(body)


async def probe_basic_delete(http: httpx.AsyncClient) -> None:
    prefix = f"probe-delete-{uuid.uuid4().hex[:8]}"
    print(f"\n== 1. Basic DELETE: create → verify → DELETE → check ==")
    print(f"   agent name: {prefix}")

    # Step 1: Create
    status, body = await create_agent(http, prefix)
    print(f"   POST /agents → {status}")
    if status >= 400:
        print(f"   ERROR creating agent: {fmt(body)}")
        return
    assert isinstance(body, dict)
    agent_id = body["id"]
    print(f"   created id={agent_id}")

    # Step 2: Verify it exists
    status, body = await get_agent(http, agent_id)
    print(f"\n   GET /agents/{agent_id} (before delete) → {status}")
    if status == 200 and isinstance(body, dict):
        print(f"   exists: name={body.get('name')!r} status={body.get('status')!r}")
    else:
        print(f"   unexpected: {fmt(body)}")

    # Step 3: DELETE it
    status, body = await delete_agent(http, agent_id)
    print(f"\n   DELETE /agents/{agent_id} → {status}")
    print(f"   response body: {fmt(body)}")

    # Step 4: GET after DELETE
    status, body = await get_agent(http, agent_id)
    print(f"\n   GET /agents/{agent_id} (after delete) → {status}")
    if status == 404:
        print(f"   HARD DELETE confirmed: 404 after DELETE")
    elif status == 200 and isinstance(body, dict):
        agent_status = body.get("status")
        print(f"   Still retrievable: status={agent_status!r}")
        if agent_status == "archived":
            print(f"   SOFT DELETE / ARCHIVE: DELETE maps to archive")
        else:
            print(f"   NO-OP or unknown behaviour: agent unchanged")
    else:
        print(f"   Unexpected response: {fmt(body)}")

    # Step 5: LIST and check if agent appears
    list_status, list_body = await list_agents(http)
    print(f"\n   GET /agents (list) → {list_status}")
    if list_status == 200 and isinstance(list_body, dict):
        ids = [a["id"] for a in list_body.get("data", [])]
        if agent_id in ids:
            print(f"   AGENT STILL IN LIST after DELETE")
        else:
            print(f"   Agent NOT in list after DELETE (expected for hard or archived-hidden)")
        # Check with archived filter if supported
        r2 = await http.get(
            f"{API_BASE}/agents",
            headers=headers(json_body=False),
            params={"limit": 100, "status": "archived"},
        )
        try:
            archived_body = r2.json()
        except Exception:
            archived_body = r2.text
        print(f"\n   GET /agents?status=archived → {r2.status_code}")
        if r2.status_code == 200 and isinstance(archived_body, dict):
            archived_ids = [a["id"] for a in archived_body.get("data", [])]
            if agent_id in archived_ids:
                print(f"   Agent FOUND in archived list → DELETE = archive")
            else:
                print(f"   Agent NOT in archived list → likely hard-deleted")
        else:
            print(f"   archived filter response: {fmt(archived_body)}")
    else:
        print(f"   list error: {fmt(list_body)}")

    return agent_id


async def probe_delete_archived(http: httpx.AsyncClient) -> None:
    prefix = f"probe-delete-arch-{uuid.uuid4().hex[:8]}"
    print(f"\n== 2. DELETE an already-archived agent ==")
    print(f"   agent name: {prefix}")

    status, body = await create_agent(http, prefix)
    print(f"   POST /agents → {status}")
    if status >= 400:
        print(f"   ERROR: {fmt(body)}")
        return
    assert isinstance(body, dict)
    agent_id = body["id"]
    print(f"   created id={agent_id}")

    # Archive it first
    status, body = await archive_agent(http, agent_id)
    print(f"   POST /agents/{agent_id}/archive → {status}")
    if isinstance(body, dict):
        print(f"   archived status={body.get('status')!r}")
    if status >= 400:
        print(f"   archive failed: {fmt(body)}")
        # Still try to clean up
        await delete_agent(http, agent_id)
        return

    # Now DELETE the archived agent
    status, body = await delete_agent(http, agent_id)
    print(f"\n   DELETE /agents/{agent_id} (archived) → {status}")
    print(f"   response body: {fmt(body)}")

    # Check if it's gone
    status, body = await get_agent(http, agent_id)
    print(f"\n   GET /agents/{agent_id} (after DELETE of archived) → {status}")
    if status == 404:
        print(f"   Gone (404) — DELETE removes archived agents too")
    elif status == 200 and isinstance(body, dict):
        print(f"   Still present: status={body.get('status')!r}")
    else:
        print(f"   Unexpected: {fmt(body)}")


async def probe_name_reuse_after_delete(http: httpx.AsyncClient) -> None:
    prefix = f"probe-delete-reuse-{uuid.uuid4().hex[:8]}"
    print(f"\n== 3. Name reuse after DELETE ==")
    print(f"   agent name: {prefix}")

    # Create first
    status, body = await create_agent(http, prefix)
    print(f"   POST /agents (first) → {status}")
    if status >= 400:
        print(f"   ERROR: {fmt(body)}")
        return
    assert isinstance(body, dict)
    first_id = body["id"]
    print(f"   first id={first_id}")

    # Delete it
    del_status, del_body = await delete_agent(http, first_id)
    print(f"   DELETE /agents/{first_id} → {del_status}")

    # Attempt to create another agent with the same name
    status, body = await create_agent(http, prefix)
    print(f"\n   POST /agents (same name, after DELETE) → {status}")
    if status == 200 or status == 201:
        assert isinstance(body, dict)
        second_id = body["id"]
        print(f"   Name reuse ALLOWED: new id={second_id}")
        # Clean up second agent
        await delete_agent(http, second_id)
    elif status == 409 or status == 422:
        print(f"   Name reuse BLOCKED: {fmt(body)}")
    else:
        print(f"   Unexpected status: {fmt(body)}")


async def main() -> None:
    load_dotenv()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("ANTHROPIC_API_KEY not set")

    async with httpx.AsyncClient(timeout=30) as http:
        await probe_basic_delete(http)
        await probe_delete_archived(http)
        await probe_name_reuse_after_delete(http)

    print("\n== Done ==")


if __name__ == "__main__":
    asyncio.run(main())
