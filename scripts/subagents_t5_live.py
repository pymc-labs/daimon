# ruff: noqa: E501  (the two prompts are sent to the agent verbatim)
"""#179 T5 on the staging Managed Agents key, through Daimon OS's own code paths.

Two independent changes from one request (two helpers, two worktrees, two branches
pushed to a disposable origin), then the first is merged and the lead fixes the
conflict the second now has. Drives: the agent write gate (`mux_compat.create_agent`),
the session gate + budget + framing (`sessions.create_session`, `coordinator_system`),
the per-turn limit (`subagent_budget.raise_turn_budget`), the real turn driver
(`turn.driver.run_turn`), and helper billing (`usage_sweep.settle_session`, read-only).

Run against a staging Managed Agents key only (about 0.10 USD a run):

    ANTHROPIC_API_KEY=<staging key> T5_DATABASE_URL=<migrated local test DSN> \
        uv run python scripts/subagents_t5_live.py

The database is read for billing only (`record=None` settle, then a settle whose
recorder has no tenant and writes nothing). Never prints the key. Cleans up the
environment, agent and session it creates. Writes t5-*.json next to itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import os
import sys
import time
from decimal import Decimal
from pathlib import Path

from anthropic import AsyncAnthropic
from daimon.core.config import SubagentsSettings
from daimon.core.mux_backend import platform_scope
from daimon.core.mux_compat import create_agent
from daimon.core.pricing import MODEL_PRICING
from daimon.core.sessions import create_session
from daimon.core.subagent_budget import raise_turn_budget
from daimon.core.subagents import coordinator_system, subagents_override
from daimon.core.turn.driver import run_turn
from daimon.core.turn.posture import BillingExempt
from daimon.core.turn.state import TextBlock, ToolUseBlock
from daimon.core.usage_recording import record_turn_usage
from daimon.core.usage_sweep import settle_session
from daimon.testing.turn_fakes import RecordingLifecycle
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

OUT = Path(os.environ.get("T5_OUT", Path(__file__).parent))
LEAD_MODEL = "claude-sonnet-5-5"

SETUP = """Set up a disposable git origin and two parallel changes.
1. Create a bare repo /work/origin.git and a clone /work/main (git config user.name T5, user.email t5@example.com). On main, commit app.py containing exactly:
def greet():
    return "hi"
and push main to origin.
2. Then, IN PARALLEL, hand two independent changes to two helpers at the same time. Each helper creates its own worktree from /work/main and works only there:
 - helper A: `git -C /work/main worktree add /work/wt-upper -b feat-upper origin/main`; change greet to return "HI"; commit; push feat-upper to origin.
 - helper B: `git -C /work/main worktree add /work/wt-name -b feat-name origin/main`; change greet to take a name and return f"hi {name}"; commit; push feat-name to origin.
These are two separate pull requests. When both report, show `git -C /work/main worktree list` and `git -C /work/main ls-remote origin`."""

MERGE = """The first pull request, feat-upper, was approved: merge origin/feat-upper into main in /work/main and push main to origin.
Then bring feat-name up to date with the new main in its own worktree /work/wt-name (rebase onto origin/main). It will conflict in app.py: resolve it so greet takes a name and returns f"HI {name}", finish the rebase and force-push feat-name.
Finally run, in this order, and show the output: `git -C /work/main fetch -q origin`, `git -C /work/main show origin/main:app.py`, `git -C /work/main show origin/feat-name:app.py`, `git -C /work/main log --oneline --graph --all`."""


def _bash_outputs(state) -> list[tuple[str, str]]:
    out = []
    for block in state.content:
        if isinstance(block, ToolUseBlock) and block.name == "bash":
            text = "".join(getattr(c, "text", "") for c in (block.result_content or []))
            out.append((str(block.input.get("command", "")), text))
    return out


async def _turn(client: AsyncAnthropic, session_id: str, text: str, scope, name: str):
    await raise_turn_budget(client, session_id, scope=scope)
    started = time.monotonic()
    state = await run_turn(
        anthropic=client,
        session_id=session_id,
        user_message=text,
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        billing=BillingExempt(reason="cli-operator-run"),
        render_interval_s=0.5,
        path="mux",  # production's turn path
        scope=scope,
    )
    elapsed = time.monotonic() - started
    record = {
        "turn": name,
        "seconds": round(elapsed),
        "termination": str(state.termination),
        "helpers": [(t.thread_id, t.status) for t in state.tasks],
        "final": next((b.text for b in reversed(state.content) if isinstance(b, TextBlock)), ""),
        "bash": _bash_outputs(state),
    }
    (OUT / f"t5-{name}.json").write_text(json.dumps(record, indent=1))
    print(f"[{name}] {record['termination']} in {record['seconds']}s, helpers={record['helpers']}")
    return state, record


async def main() -> int:
    client = AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"], max_retries=2)
    scope = platform_scope("subagents T5 staging run")
    created: dict[str, str] = {}
    try:
        env = await client.beta.environments.create(
            name="subagents-t5-20261031",
            config={"type": "cloud", "networking": {"type": "limited"}},
        )
        created["env"] = env.id
        with subagents_override(SubagentsSettings(enabled=True, turn_budget_usd=Decimal("1.5"))):
            agent = await create_agent(
                client,
                {
                    "name": "subagents-t5-lead",
                    "model": LEAD_MODEL,
                    "system": "You are a careful coding agent.",
                    "tools": [{"type": "agent_toolset_20260401"}],
                    "multiagent": {"type": "coordinator", "agents": [{"type": "self"}]},
                },
                scope=scope,
            )
            created["agent"] = agent.id
            session = await create_session(
                client,
                agent=agent,
                environment=env,
                subagents=True,
                system_override=coordinator_system(None, agent.system, agent.multiagent),
            )
            created["session"] = session.id
            print("agent", agent.id, "session", session.id)
            s1, r1 = await _turn(client, session.id, SETUP, scope, "setup")
            s2, r2 = await _turn(client, session.id, MERGE, scope, "merge")

            # Helper billing, read-only, against a migrated local database (no ledger writes).
            engine = create_async_engine(os.environ["T5_DATABASE_URL"])
            sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
            settle = await settle_session(
                client, sessionmaker, session.id, record=None, helpers=True
            )
            threads = [t async for t in client.beta.sessions.threads.list(session.id)]
            # Then a settle whose recorder writes nothing (no tenant) archives the idle helpers.
            noop = functools.partial(
                record_turn_usage,
                sessionmaker=sessionmaker,
                tenant_id=None,
                platform_user_id=None,
                managed_session_id=session.id,
                model_id=LEAD_MODEL,
                pricing=MODEL_PRICING.get(LEAD_MODEL),
            )
            await settle_session(client, sessionmaker, session.id, record=noop, helpers=True)
            after = [t async for t in client.beta.sessions.threads.list(session.id)]
            await engine.dispose()

        helpers = [t for t in threads if t.parent_thread_id is not None]
        spans = [(t.created_at, t.updated_at) for t in helpers]
        overlapped = any(
            a[0] < b[1] and b[0] < a[1] for i, a in enumerate(spans) for b in spans[i + 1 :]
        )
        bash1 = "\n".join(out for _cmd, out in r1["bash"])
        bash2 = "\n".join(out for _cmd, out in r2["bash"])
        verdicts = {
            "two_or_more_helpers_in_turn_1": len(r1["helpers"]) >= 2,
            "helpers_ran_at_the_same_time": overlapped,
            "two_worktrees": "/work/wt-upper" in bash1 and "/work/wt-name" in bash1,
            "two_branches_pushed": "refs/heads/feat-upper" in bash1
            and "refs/heads/feat-name" in bash1,
            "first_merged_into_main": 'return "HI"' in bash2,
            "conflict_resolved_on_second": 'f"HI {name}"' in bash2,
            "both_turns_completed": r1["termination"].endswith("COMPLETED")
            or r1["termination"] == "completed",
            "helper_calls_found_for_billing": len(settle.unbilled) > 0,
            "finished_helpers_archived": all(
                t.archived_at is not None for t in after if t.parent_thread_id is not None
            ),
        }
        summary = {
            "verdicts": verdicts,
            "pass": all(verdicts.values()),
            "turn_seconds": [r1["seconds"], r2["seconds"]],
            "terminations": [r1["termination"], r2["termination"]],
            "helpers": len(helpers),
            "unbilled_calls_seen_by_settle": len(settle.unbilled),
            "threads": [
                {
                    "parent": t.parent_thread_id is not None,
                    "model": t.agent.model.id,
                    "list_cost_cents": (t.model_dump(mode="json").get("usage") or {})
                    .get("list_cost", {})
                    .get("amount"),
                }
                for t in threads
            ],
        }
        (OUT / "t5-summary.json").write_text(json.dumps(summary, indent=1, default=str))
        print(json.dumps(summary, indent=1, default=str))
        return 0 if summary["pass"] else 1
    finally:
        if "session" in created:
            with contextlib.suppress(Exception):
                await client.beta.sessions.archive(created["session"])
        if "agent" in created:
            with contextlib.suppress(Exception):
                await client.beta.agents.archive(created["agent"])
        if "env" in created:
            with contextlib.suppress(Exception):
                await client.beta.environments.delete(created["env"])
        print("cleaned up", sorted(created))


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
