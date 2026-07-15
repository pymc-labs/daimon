"""Probe: can we attribute, enumerate, and hard-delete MA sessions for GDPR?

Phase 57 (D-05) needs to know whether a user's `/privacy` delete can erase
their Anthropic-side conversation transcripts. daimon stores NO MA session ids
locally (sessions are created per-turn and discarded), and `core.sessions.
create_session` currently passes NO `metadata`. Open questions:

  Q1. Does `sessions.create` accept a `metadata` dict? (tag-going-forward)
  Q2. Does a retrieved session echo that metadata back?
  Q3. Does `sessions.list(agent_id=...)` echo metadata per row? `list` has NO
      metadata filter (only agent_id / created_at / archived), so enumerating
      "this user's sessions" means list-by-agent + CLIENT-SIDE metadata filter
      — which only works if list rows carry metadata. Make-or-break for D-05.
  Q4. Does `sessions.delete` hard-delete — retrieve 404/410 afterward and the
      row absent from list (incl. include_archived)? The session IS the
      transcript container, so a 404 means the transcript is gone.
  Q5. An untagged session (models EXISTING pre-Phase-57 transcripts): it lists,
      but with no daimon attribution — confirming existing transcripts are NOT
      individually reachable, i.e. deletion is forward-only.

Run (from repo root so .env + .venv resolve):
    uv run python <path>/scripts/probes/managed_agents/session_gdpr_deletion.py

Requires DAIMON_ANTHROPIC__API_KEY (or ANTHROPIC_API_KEY) with MA access.
Creates one throwaway agent + env + two sessions; cleans up in finally.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from dataclasses import dataclass, field

from anthropic import APIStatusError, AsyncAnthropic
from dotenv import load_dotenv

PROBE_TAG = "phase-57-session-gdpr-deletion"
ACCOUNT_ID = str(uuid.uuid4())  # stand-in for a daimon account_id
CLOUD_CONFIG: dict[str, object] = {
    "type": "cloud",
    "networking": {"type": "unrestricted"},
    "packages": {"apt": [], "cargo": [], "gem": [], "go": [], "npm": [], "pip": []},
}


@dataclass
class Findings:
    create_accepts_metadata: bool | None = None
    retrieve_echoes_metadata: bool | None = None
    list_echoes_metadata: bool | None = None
    delete_ok: bool | None = None
    retrieve_after_delete_status: int | None = None
    present_in_list_after_delete: bool | None = None
    present_in_list_archived_after_delete: bool | None = None
    untagged_has_no_attribution: bool | None = None
    notes: list[str] = field(default_factory=list[str])


async def _retrieve_status(client: AsyncAnthropic, sid: str) -> int:
    try:
        await client.beta.sessions.retrieve(sid)
        return 200
    except APIStatusError as e:
        return e.status_code


async def _list_ids(client: AsyncAnthropic, agent_id: str, *, archived: bool) -> set[str]:
    ids: set[str] = set()
    kwargs: dict[str, object] = {"agent_id": agent_id, "limit": 50}
    if archived:
        kwargs["include_archived"] = True
    async for s in client.beta.sessions.list(**kwargs):  # type: ignore[arg-type]
        ids.add(s.id)
    return ids


async def main() -> None:
    load_dotenv()
    key = os.environ.get("DAIMON_ANTHROPIC__API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise SystemExit("No DAIMON_ANTHROPIC__API_KEY / ANTHROPIC_API_KEY in env/.env")

    client = AsyncAnthropic(api_key=key)
    f = Findings()
    agent_id: str | None = None
    env_id: str | None = None
    try:
        agent = await client.beta.agents.create(
            model="claude-haiku-4-5",
            name=f"{PROBE_TAG}-{uuid.uuid4().hex[:8]}",
            system="Probe agent.",
            metadata={"daimon_probe": PROBE_TAG},
        )
        agent_id = agent.id
        env = await client.beta.environments.create(
            name=f"{PROBE_TAG}-env-{uuid.uuid4().hex[:8]}",
            config=CLOUD_CONFIG,  # type: ignore[arg-type]
            metadata={"daimon_probe": PROBE_TAG},
        )
        env_id = env.id
        agent_ref = {"type": "agent", "id": agent.id, "version": agent.version}
        print(f"agent={agent_id} v{agent.version} env={env_id} account_id(stub)={ACCOUNT_ID}")

        # Q1 — create WITH metadata
        md = {"daimon_account_id": ACCOUNT_ID, "daimon_probe": PROBE_TAG}
        try:
            tagged = await client.beta.sessions.create(
                agent=agent_ref,  # type: ignore[arg-type]
                environment_id=env_id,
                metadata=md,
            )
            f.create_accepts_metadata = True
        except APIStatusError as e:
            f.create_accepts_metadata = False
            f.notes.append(f"create+metadata rejected {e.status_code}: {str(e)[:160]}")
            tagged = await client.beta.sessions.create(
                agent=agent_ref,  # type: ignore[arg-type]
                environment_id=env_id,
            )
        print(f"[Q1] create(metadata=...) accepted -> {f.create_accepts_metadata}")

        # Q2 — retrieve echoes metadata?
        got = await client.beta.sessions.retrieve(tagged.id)
        f.retrieve_echoes_metadata = bool(got.metadata) and got.metadata.get("daimon_account_id") == ACCOUNT_ID
        print(f"[Q2] retrieve.metadata -> {json.dumps(got.metadata)}")

        # Q5 — untagged session (models pre-Phase-57 existing transcript)
        untagged = await client.beta.sessions.create(
            agent=agent_ref,  # type: ignore[arg-type]
            environment_id=env_id,
        )

        # Q3 — list by agent echoes metadata per row?
        tagged_md: dict[str, str] | None = None
        untagged_md: dict[str, str] | None = None
        async for s in client.beta.sessions.list(agent_id=agent_id, limit=50):
            if s.id == tagged.id:
                tagged_md = s.metadata
            elif s.id == untagged.id:
                untagged_md = s.metadata
        f.list_echoes_metadata = bool(tagged_md and tagged_md.get("daimon_account_id") == ACCOUNT_ID)
        f.untagged_has_no_attribution = not (untagged_md or {})
        print(f"[Q3] list tagged_row.metadata={json.dumps(tagged_md)}")
        print(f"[Q5] list untagged_row.metadata={json.dumps(untagged_md)}")

        # Q4 — hard delete then re-fetch
        try:
            await client.beta.sessions.delete(tagged.id)
            f.delete_ok = True
        except APIStatusError as e:
            f.delete_ok = False
            f.notes.append(f"delete failed {e.status_code}: {str(e)[:160]}")
        f.retrieve_after_delete_status = await _retrieve_status(client, tagged.id)
        f.present_in_list_after_delete = tagged.id in await _list_ids(client, agent_id, archived=False)
        f.present_in_list_archived_after_delete = tagged.id in await _list_ids(client, agent_id, archived=True)
        print(
            f"[Q4] delete_ok={f.delete_ok} retrieve_after={f.retrieve_after_delete_status} "
            f"in_list={f.present_in_list_after_delete} in_list_archived={f.present_in_list_archived_after_delete}"
        )
    finally:
        if agent_id:
            try:
                await client.beta.agents.archive(agent_id)
            except APIStatusError as e:
                print(f"cleanup: archive agent {e.status_code}")
        if env_id:
            try:
                await client.beta.environments.delete(env_id)
            except APIStatusError as e:
                print(f"cleanup: delete env {e.status_code}")

    hard_delete = bool(f.delete_ok) and f.retrieve_after_delete_status in (404, 410) and not f.present_in_list_after_delete
    tag_forward = bool(f.create_accepts_metadata and f.list_echoes_metadata)
    print("\n================ VERDICT (Phase 57 / D-05) ================")
    print(f" Q1 create accepts metadata ............ {f.create_accepts_metadata}")
    print(f" Q2 retrieve echoes metadata ........... {f.retrieve_echoes_metadata}")
    print(f" Q3 LIST echoes metadata (filterable) .. {f.list_echoes_metadata}")
    print(f" Q4 DELETE hard-deletes ................ {hard_delete} "
          f"(del_ok={f.delete_ok}, get={f.retrieve_after_delete_status}, "
          f"archived_list={f.present_in_list_archived_after_delete})")
    print(f" Q5 untagged session = no attribution .. {f.untagged_has_no_attribution}")
    print("-----------------------------------------------------------")
    print(f" TAG-FORWARD enumeration feasible ...... {tag_forward}")
    print(f" EXISTING transcripts reachable ........ {not f.untagged_has_no_attribution}")
    if f.notes:
        print(" notes: " + " | ".join(f.notes))
    print("===========================================================")


if __name__ == "__main__":
    asyncio.run(main())
