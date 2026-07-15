"""Probe: can the OPERATOR retrieve a file the AGENT created in its sandbox?

This is the Approach-2 feasibility test for the publish_blog payload-limit fix
(spike 033). The candidate "clean" fix is: the agent writes the notebook / data
to a file in its MA sandbox, passes a *file_id* to an MCP tool, and the MCP
server (operator's API key) downloads the bytes server-to-server — never through
the model token stream.

That only works if MA surfaces agent-created sandbox files to the operator. The
SDK *has* the machinery for the Messages-API code-execution tool
(`bash_code_execution_output.file_id`, `beta.files.download`, `BetaContainer`),
but it is unproven whether an MA *session* with `agent_toolset_20260401` exposes
agent-created files as downloadable file_ids. This probe answers that empirically.

Method:
  1. Create a throwaway cloud env (unrestricted) + agent (bash toolset).
  2. Open a session (NO mounted resources — we want the agent to CREATE a file).
  3. Drive one turn: agent writes a deterministic 64 KiB file to /tmp and reports
     its shell-computed sha256 (026 lesson: shell sha256, never model eyeball).
  4. OPERATOR-SIDE EGRESS ATTEMPTS (the actual test):
       a. scan every raw session event for any `file_id` / `container` reference
       b. diff `beta.files.list()` before vs after the turn
       c. for any file_id discovered (events or files.list), `beta.files.download`
          and compare bytes to the locally-known expected content
  5. SELF-CHECK: PASS (Approach 2 viable) iff the operator downloads bytes whose
     sha256 matches the deterministic fixture. Otherwise FAIL (Approach 2 dead —
     commit to Approach 1, the capability-URL + agent-curl side channel).

Run:
  DAIMON_ANTHROPIC__API_KEY=sk-ant-... uv run python \\
    scripts/probes/managed_agents/files_api_egress.py
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import uuid

from anthropic import AsyncAnthropic
from anthropic.types.beta.sessions.beta_managed_agents_session_error_event import (
    BetaManagedAgentsSessionErrorEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_session_status_idle_event import (
    BetaManagedAgentsSessionStatusIdleEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_user_message_event_params import (
    BetaManagedAgentsUserMessageEventParams,
)
from anthropic.types.beta.sessions.beta_managed_agents_user_tool_confirmation_event_params import (
    BetaManagedAgentsUserToolConfirmationEventParams,
)
from dotenv import load_dotenv

from daimon.core.turn.reducers import apply
from daimon.core.turn.state import TurnState, extract_final_response

# Deterministic 64 KiB fixture the agent will write. The operator knows these
# exact bytes, so any downloaded candidate can be byte-compared without trusting
# the agent's self-reported hash.
FIXTURE = b"D033" * 16384  # 65536 bytes
EXPECTED_SHA = hashlib.sha256(FIXTURE).hexdigest()
SANDBOX_PATH = "/tmp/egress_probe.bin"

FILE_ID_RE = re.compile(r'"file_id"\s*:\s*"([^"]+)"')
CONTAINER_ID_RE = re.compile(r'"container"\s*:\s*\{[^}]*"id"\s*:\s*"([^"]+)"')


async def _download_bytes(client: AsyncAnthropic, file_id: str) -> bytes | None:
    """Best-effort download of a file_id's bytes via the Files API."""
    try:
        resp = await client.beta.files.download(file_id)
        # The SDK returns a streamable response; .read() yields the raw bytes.
        read = getattr(resp, "read", None)
        if read is not None:
            data = read()
            return await data if asyncio.iscoroutine(data) else data
        return bytes(resp)  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001 — probe: any failure means "not retrievable this way"
        print(f"    download({file_id}) failed: {exc}")
        return None


async def main() -> None:
    load_dotenv()
    api_key = os.environ.get("DAIMON_ANTHROPIC__API_KEY") or os.environ["ANTHROPIC_API_KEY"]
    client = AsyncAnthropic(api_key=api_key)

    tag = uuid.uuid4().hex[:8]
    env_id: str | None = None
    agent_id: str | None = None
    verdict = "FAIL"

    try:
        print(f"=== expected fixture: {len(FIXTURE)} bytes  sha256={EXPECTED_SHA[:16]}... ===")

        print("\n=== 0. Snapshot files.list() BEFORE the turn ===")
        before_ids: set[str] = set()
        try:
            async for f in client.beta.files.list():
                before_ids.add(f.id)
            print(f"  {len(before_ids)} pre-existing files in the workspace")
        except Exception as exc:  # noqa: BLE001
            print(f"  files.list() failed (may be unsupported): {exc}")

        print(f"\n=== 1. Create cloud environment (probe-egress-{tag}) ===")
        environment = await client.beta.environments.create(
            name=f"probe-egress-env-{tag}",
            config={"type": "cloud"},
            metadata={"daimon_probe": "files_api_egress"},
        )
        env_id = environment.id
        print(f"  env_id={env_id}")

        print("\n=== 2. Create agent (sonnet, bash toolset) ===")
        agent = await client.beta.agents.create(
            name=f"probe-egress-agent-{tag}",
            model="claude-sonnet-4-6",
            system=(
                "You are a filesystem probe. When asked, run the exact shell "
                "commands given with the bash tool and report their raw stdout."
            ),
            tools=[{"type": "agent_toolset_20260401", "configs": [{"name": "bash"}]}],
            metadata={"daimon_probe": "files_api_egress"},
        )
        agent_id = agent.id
        print(f"  agent_id={agent_id} version={agent.version}")

        print("\n=== 3. Create session (no mounted resources) ===")
        session = await client.beta.sessions.create(agent=agent_id, environment_id=env_id)
        print(f"  session_id={session.id}")

        print("\n=== 4. Drive a turn: agent writes a 64 KiB file, reports its sha256 ===")
        trigger = (
            "Automated filesystem test. Using your bash tool, run these commands "
            "and report the raw stdout of the last one on a line labelled SHA=:\n"
            f"1. `python3 -c \"open('{SANDBOX_PATH}','wb').write(b'D033'*16384)\"`\n"
            f"2. `sha256sum {SANDBOX_PATH}`\n"
            "Reply with exactly one line: SHA=<sha256sum output>"
        )
        user_message: BetaManagedAgentsUserMessageEventParams = {
            "type": "user.message",
            "content": [{"type": "text", "text": trigger}],
        }
        await client.beta.sessions.events.send(session.id, events=[user_message])

        state = TurnState()
        confirmed: set[str] = set()
        raw_dump_parts: list[str] = []
        event_types: dict[str, int] = {}
        async for event in await client.beta.sessions.events.stream(session_id=session.id):
            state = apply(state, event)
            event_types[type(event).__name__] = event_types.get(type(event).__name__, 0) + 1
            try:
                raw_dump_parts.append(event.model_dump_json())  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                raw_dump_parts.append(repr(event))

            if isinstance(event, BetaManagedAgentsSessionErrorEvent):
                message = getattr(event.error, "message", None) or repr(event.error)
                raise RuntimeError(f"session.error: {message}")

            if isinstance(event, BetaManagedAgentsSessionStatusIdleEvent):
                if event.stop_reason.type == "requires_action":
                    fresh = [t for t in event.stop_reason.event_ids if t not in confirmed]
                    if fresh:
                        confirmed.update(fresh)
                        decisions: list[BetaManagedAgentsUserToolConfirmationEventParams] = [
                            {"type": "user.tool_confirmation", "result": "allow", "tool_use_id": t}
                            for t in fresh
                        ]
                        print(f"  auto-allowing {len(fresh)} tool call(s)")
                        await client.beta.sessions.events.send(session.id, events=decisions)
                    continue
                break

        final_text = extract_final_response(state.content)
        print(f"\n  event types seen: {event_types}")
        print(f"  agent reported: {final_text!r}")
        agent_reported_match = EXPECTED_SHA in final_text
        print(f"  agent's shell sha256 == expected fixture sha256: {agent_reported_match}")

        raw_dump = "\n".join(raw_dump_parts)

        print("\n=== 5a. Scan raw session events for file_id / container references ===")
        event_file_ids = set(FILE_ID_RE.findall(raw_dump))
        container_ids = set(CONTAINER_ID_RE.findall(raw_dump))
        has_container_word = "container" in raw_dump.lower()
        print(f"  file_id refs in events: {event_file_ids or '∅'}")
        print(f"  container ids in events: {container_ids or '∅'}")
        print(f"  any 'container' substring in event stream: {has_container_word}")

        print("\n=== 5b. Diff files.list() AFTER the turn ===")
        after_ids: set[str] = set()
        try:
            async for f in client.beta.files.list():
                after_ids.add(f.id)
        except Exception as exc:  # noqa: BLE001
            print(f"  files.list() failed: {exc}")
        new_from_list = after_ids - before_ids
        print(f"  new file_ids appearing in files.list(): {new_from_list or '∅'}")

        print("\n=== 5c. Attempt to download every discovered file_id; compare bytes ===")
        candidates = event_file_ids | container_ids | new_from_list
        downloaded_match = False
        if not candidates:
            print("  no candidate file_ids discovered by ANY operator-side path")
        for fid in candidates:
            data = await _download_bytes(client, fid)
            if data is None:
                continue
            got_sha = hashlib.sha256(data).hexdigest()
            match = got_sha == EXPECTED_SHA
            print(f"    {fid}: {len(data)} bytes  sha256={got_sha[:16]}...  match={match}")
            downloaded_match = downloaded_match or match

        print("\n=== SELF-CHECK ===")
        print(f"  agent created the file (its shell sha matches fixture): {agent_reported_match}")
        print(f"  OPERATOR downloaded byte-exact fixture server-side: {downloaded_match}")
        if downloaded_match:
            verdict = "PASS"
            print("  → Approach 2 VIABLE: operator can read agent-created sandbox files")
        else:
            verdict = "FAIL"
            print("  → Approach 2 DEAD: no server-side path retrieves the agent's file")
            if not agent_reported_match:
                print("    (note: agent may not have created the file — inspect final_text)")

    finally:
        print("\n=== cleanup ===")
        if agent_id is not None:
            try:
                await client.beta.agents.archive(agent_id)
                print(f"  archived agent {agent_id}")
            except Exception as exc:  # noqa: BLE001
                print(f"  agent archive failed: {exc}")
        if env_id is not None:
            try:
                await client.beta.environments.delete(env_id)
                print(f"  deleted env {env_id}")
            except Exception as exc:  # noqa: BLE001
                try:
                    await client.beta.environments.archive(env_id)
                    print(f"  archived env {env_id}")
                except Exception as exc2:  # noqa: BLE001
                    print(f"  env cleanup failed: {exc2}")

    print(f"\n=== VERDICT === {verdict}: operator retrieval of agent-created sandbox file")


if __name__ == "__main__":
    asyncio.run(main())
