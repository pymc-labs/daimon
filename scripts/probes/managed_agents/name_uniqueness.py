"""Probe whether MA enforces uniqueness on agent/env/vault/skill names.

Creates two objects of each type with identical names and reports whether the
second POST succeeds. Cleans up everything it creates.

Decision this informs: can we keep MA-side names owner-agnostic in the
open-source fork's accounts/principals design, or must we prefix names with
account IDs to avoid collisions?

Run:
    uv run python scripts/probes/managed_agents/name_uniqueness.py
"""

from __future__ import annotations

import asyncio
import io
import os
import uuid
import zipfile

import httpx
from dotenv import load_dotenv

API_BASE = "https://api.anthropic.com/v1"
BETA_AGENTS = "managed-agents-2026-04-01"
BETA_SKILLS = "skills-2025-10-02"


def headers(json: bool = True, beta: str = BETA_AGENTS) -> dict[str, str]:
    h = {
        "x-api-key": os.environ["ANTHROPIC_API_KEY"],
        "anthropic-version": "2023-06-01",
        "anthropic-beta": beta,
    }
    if json:
        h["content-type"] = "application/json"
    return h


async def try_create(
    http: httpx.AsyncClient, label: str, path: str, body: dict
) -> tuple[int, dict | str, str | None]:
    r = await http.post(f"{API_BASE}{path}", headers=headers(), json=body)
    try:
        data = r.json()
    except Exception:
        data = r.text
    obj_id = data.get("id") if isinstance(data, dict) else None
    print(f"  [{label}] {r.status_code} id={obj_id}")
    if r.status_code >= 400:
        print(f"    body={data}")
    return r.status_code, data, obj_id


async def try_delete(http: httpx.AsyncClient, path: str) -> None:
    try:
        r = await http.delete(f"{API_BASE}{path}", headers=headers(json=False))
        print(f"  cleanup DELETE {path} -> {r.status_code}")
    except Exception as e:
        print(f"  cleanup DELETE {path} failed: {e}")


async def probe_agents(http: httpx.AsyncClient) -> None:
    print("\n== Agents: duplicate name ==")
    name = f"probe-dup-{uuid.uuid4().hex[:8]}"
    body = {
        "name": name,
        "model": {"id": "claude-haiku-4-5", "speed": "standard"},
        "system": "probe",
        "skills": [],
        "tools": [],
        "mcp_servers": [],
    }
    ids: list[str] = []
    try:
        for label in ("first", "second"):
            status, _, oid = await try_create(http, label, "/agents", body)
            if oid:
                ids.append(oid)
    finally:
        for oid in ids:
            await try_delete(http, f"/agents/{oid}")


async def probe_envs(http: httpx.AsyncClient) -> None:
    print("\n== Environments: duplicate name ==")
    name = f"probe-dup-{uuid.uuid4().hex[:8]}"
    body = {
        "name": name,
        "config": {"type": "cloud", "networking": {"type": "unrestricted"}},
    }
    ids: list[str] = []
    try:
        for label in ("first", "second"):
            status, _, oid = await try_create(http, label, "/environments", body)
            if oid:
                ids.append(oid)
    finally:
        for oid in ids:
            await try_delete(http, f"/environments/{oid}")


async def probe_vaults(http: httpx.AsyncClient) -> None:
    print("\n== Vaults: duplicate display_name ==")
    name = f"probe-dup-{uuid.uuid4().hex[:8]}"
    body = {"display_name": name}
    ids: list[str] = []
    try:
        for label in ("first", "second"):
            status, _, oid = await try_create(http, label, "/vaults", body)
            if oid:
                ids.append(oid)
    finally:
        for oid in ids:
            await try_delete(http, f"/vaults/{oid}")


async def probe_skills(http: httpx.AsyncClient) -> None:
    print("\n== Skills: duplicate display_title ==")
    name = f"probe-dup-{uuid.uuid4().hex[:8]}"
    # build a minimal zip containing <name>/SKILL.md
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            f"{name}/SKILL.md",
            f"---\nname: {name}\ndescription: probe\n---\n# probe\n",
        )
    zip_bytes = buf.getvalue()

    ids: list[str] = []
    try:
        for label in ("first", "second"):
            r = await http.post(
                f"{API_BASE}/skills",
                headers=headers(json=False, beta=BETA_SKILLS),
                data={"display_title": name},
                files=[("files[]", (f"{name}.zip", zip_bytes, "application/zip"))],
            )
            try:
                data = r.json()
            except Exception:
                data = r.text
            oid = data.get("id") if isinstance(data, dict) else None
            print(f"  [{label}] {r.status_code} id={oid}")
            if r.status_code >= 400:
                print(f"    body={data}")
            if oid:
                ids.append(oid)
    finally:
        for oid in ids:
            await try_delete(http, f"/skills/{oid}")


async def main() -> None:
    load_dotenv()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("ANTHROPIC_API_KEY not set")

    async with httpx.AsyncClient(timeout=30) as http:
        await probe_agents(http)
        await probe_envs(http)
        await probe_vaults(http)
        await probe_skills(http)


if __name__ == "__main__":
    asyncio.run(main())
