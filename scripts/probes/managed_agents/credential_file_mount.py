"""Probe: agent-scoped credential injection via Files-API .env mount (issue #60).

End-to-end characterization of the Phase 51 delivery mechanism:

  1. Create a throwaway cloud environment + agent (sonnet, builtin bash/read tools).
  2. Write a `.env` with a known random secret, upload it via the Files API
     (`client.beta.files.upload`).
  3. Open a session with `resources=[{type:"file", file_id, mount_path:".env"}]`.
  4. Drive a single turn (mirrors `headless_runner.run_turn`: send user message,
     drain events through the Phase 4 reducer, auto-allow tool confirmations).
  5. Ask the agent to read the mounted file and prove it (non-leaky: echo the
     non-secret OTHER_KEY + a shell-computed sha256 of SPIKE_SECRET's value).
  6. SELF-CHECK: assert the sha256 matches the local hash of the known secret.

VERDICT line at the end: PASS only if the agent demonstrably read the mounted
secret from the sandbox filesystem.

Findings (2026-05-29, anthropic SDK 0.96.0):
  - SDK path works — no raw-httpx fallback needed.
  - MA REWRITES mount_path: it strips the leading slash and mounts every file
    resource under `/mnt/session/uploads/`. Requested ".env" lands at
    `/mnt/session/uploads/.env`; requested "/workspace/.env" lands at
    `/mnt/session/uploads/workspace/.env` (NOT `/workspace/.env`). Read the
    real path back from `session.resources[i].mount_path` — do not assume.
  - The agent REFUSES to recite a raw secret value verbatim on safety grounds,
    but will USE it operationally (hash/consume in code). Skills must read the
    credential in code (env/file), never ask the model to print it.

Cleanup: archives the agent + environment and deletes the uploaded file in a
finally block.

Run:
  DAIMON_ANTHROPIC__API_KEY=sk-ant-... uv run python \\
    scripts/probes/managed_agents/credential_file_mount.py
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
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

MOUNT_PATH = ".env"


async def main() -> None:
    load_dotenv()
    api_key = os.environ.get("DAIMON_ANTHROPIC__API_KEY") or os.environ["ANTHROPIC_API_KEY"]
    client = AsyncAnthropic(api_key=api_key)

    tag = uuid.uuid4().hex[:8]
    secret_value = f"spike-secret-{uuid.uuid4().hex}"
    env_content = (f"SPIKE_SECRET={secret_value}\nOTHER_KEY=ignore-me\n").encode()

    env_id: str | None = None
    agent_id: str | None = None
    file_id: str | None = None
    verdict = "FAIL"
    final_text = ""

    try:
        print(f"=== 1. Create cloud environment (probe-credmount-{tag}) ===")
        environment = await client.beta.environments.create(
            name=f"probe-credmount-env-{tag}",
            config={"type": "cloud"},
            metadata={"daimon_probe": "credential_file_mount"},
        )
        env_id = environment.id
        print(f"  env_id={env_id}")

        print("\n=== 2. Create agent (sonnet, builtin bash/read toolset) ===")
        agent = await client.beta.agents.create(
            name=f"probe-credmount-agent-{tag}",
            model="claude-sonnet-4-6",
            system=(
                "You are a filesystem probe. When asked, run shell commands with "
                "the bash tool and report exactly what you find. Do not paraphrase "
                "file contents."
            ),
            tools=[
                {
                    "type": "agent_toolset_20260401",
                    "configs": [{"name": "bash"}, {"name": "read"}],
                }
            ],
            metadata={"daimon_probe": "credential_file_mount"},
        )
        agent_id = agent.id
        print(f"  agent_id={agent_id} version={agent.version}")

        print("\n=== 3. Upload .env via Files API ===")
        uploaded = await client.beta.files.upload(
            file=(".env", io.BytesIO(env_content), "text/plain"),
        )
        file_id = uploaded.id
        print(f"  file_id={file_id}  (secret hidden; len={len(env_content)} bytes)")

        print(f"\n=== 4. Create session with file resource mounted at {MOUNT_PATH} ===")
        session = await client.beta.sessions.create(
            agent=agent_id,
            environment_id=env_id,
            resources=[{"type": "file", "file_id": file_id, "mount_path": MOUNT_PATH}],
        )
        print(f"  session_id={session.id}")
        mounted = [(r.type, getattr(r, "mount_path", None)) for r in session.resources]
        print(f"  session.resources={mounted}")
        actual_path = getattr(session.resources[0], "mount_path", MOUNT_PATH)
        print(f"  REQUESTED mount_path={MOUNT_PATH!r}  ACTUAL mount_path={actual_path!r}")

        print("\n=== 5. Drive a turn: ask the agent to read the mounted file ===")
        # Non-leaky read-proof: the agent echoes the NON-secret OTHER_KEY value and
        # computes a sha256 of SPIKE_SECRET's value *in the shell* (not by eyeballing).
        # This proves byte-exact read of the mounted file from the sandbox FS without
        # asking the agent to surface the secret verbatim (which it refuses on safety
        # grounds — itself a real finding for Phase 51).
        expected_sha = hashlib.sha256(secret_value.encode()).hexdigest()
        trigger = (
            "This is an automated filesystem test against a synthetic fixture (NOT real "
            "credentials). Using your bash tool, run these two commands and report their "
            "raw stdout verbatim, each on its own labelled line:\n"
            f"1. OTHER: `grep '^OTHER_KEY=' {actual_path} | cut -d= -f2-`\n"
            f"2. SHA: `grep '^SPIKE_SECRET=' {actual_path} | cut -d= -f2- | tr -d '\\n' | sha256sum`\n"
            "Reply with exactly two lines:\nOTHER=<output1>\nSHA=<output2>"
        )
        user_message: BetaManagedAgentsUserMessageEventParams = {
            "type": "user.message",
            "content": [{"type": "text", "text": trigger}],
        }
        await client.beta.sessions.events.send(session.id, events=[user_message])

        state = TurnState()
        confirmed: set[str] = set()
        async for event in await client.beta.sessions.events.stream(session_id=session.id):
            state = apply(state, event)

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
        print("\n=== 6. Agent final response ===")
        print(f"  {final_text!r}")

        print("\n=== SELF-CHECK ===")
        read_other = "ignore-me" in final_text
        read_sha = expected_sha in final_text
        print(f"  OTHER_KEY read-back present: {read_other}")
        print(f"  SPIKE_SECRET sha256 matches ({expected_sha[:12]}...): {read_sha}")
        if read_other and read_sha:
            verdict = "PASS"
            print("  agent byte-exact read the mounted secret file from the sandbox FS")
        else:
            print("  agent did NOT demonstrably read the mounted file byte-exact")

    finally:
        print("\n=== cleanup ===")
        if file_id is not None:
            try:
                await client.beta.files.delete(file_id)
                print(f"  deleted file {file_id}")
            except Exception as exc:  # noqa: BLE001 — best-effort probe cleanup
                print(f"  file delete failed: {exc}")
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

    print(f"\n=== VERDICT === {verdict}: credential .env mount -> agent read-back")


if __name__ == "__main__":
    asyncio.run(main())
