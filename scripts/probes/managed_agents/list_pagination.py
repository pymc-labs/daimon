"""Probe: does MA list pagination actually work past 100 rows?

2026-04-22 initial probe noted next_page returns null even when more rows exist.

2026-06-10 re-probe verdict (against live org with 31 skills / 29 agents):
  - skills.list: BROKEN — with limit=5 and 31 rows, first page has next_page=None
    and has_more=False; async-for with limit=5 stops after 5 items (of 31); no
    cursor is ever returned at any page boundary.  The brokenness is NOT a ceiling
    at some large N — next_page is NEVER populated for skills, regardless of row
    count.  Small-limit methodology (limit=5, org at 31) is conclusive: no seeding
    to >100 is needed.
  - agents.list: OK — paginates correctly under identical conditions; limit=5
    returns next_page, async-for yields all 29 agents.
  - environments.list: NOT_STRESSED at time of run (too few rows).

  Implication: skills.list is broken at the API level.  _SKILLS_PAGE_LIMIT=100
  (the API max) is the absolute org-wide visibility window; the ceiling machinery
  in D-13 is REQUIRED, not insurance.  Re-run this probe after any anthropic SDK
  upgrade to detect if Anthropic fixes pagination — a transition from BROKEN to OK
  in the skills row means the ceiling hard-fail can be downgraded to insurance.

This probe checks:
  1. Seeding enough agents to exceed one page (limit=5 in our small-page test).
  2. For agents.list: testing async-for auto-pagination with limit=5.
  3. For agents.list: testing explicit next_page cursor following.
  4. For skills.list and environments.list: testing with whatever rows exist
     (seeding those is expensive — skills require zip uploads; envs are rarer).
     The small-limit methodology (limit=5) is conclusive for skills — 31 existing
     rows is sufficient stress; no seeding to >100 is needed.
  5. Printing a table and a clear VERDICT line per kind: BROKEN/OK/NOT_STRESSED.
     BROKEN(cap=N) means next_page was never returned at any boundary; the org's
     visible window is at most N (the requested limit).

Cleanup: all probe-created agents are archived in a finally block.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass

from anthropic import AsyncAnthropic
from anthropic.pagination import AsyncPageCursor
from dotenv import load_dotenv

# How many agents to seed so we can test multi-page (limit=5 → need >5 agents).
# We'll create SEED_COUNT agents tagged with PROBE_TAG in metadata so we can
# identify and archive them at cleanup.
SEED_COUNT = 12
PROBE_TAG = "probe:list_pagination:2026-04-23"
SMALL_LIMIT = 5


@dataclass
class PaginationResult:
    kind: str
    total_rows_seeded: int  # 0 = used existing workspace state
    first_page_size: int
    first_page_has_more: bool | None
    first_page_next_page: str | None  # raw cursor value, None if absent
    async_for_total: int  # items yielded by bare async-for (limit=SMALL_LIMIT)
    explicit_cursor_total: int  # items via manual cursor following (limit=SMALL_LIMIT)
    async_for_limit100_total: int  # items yielded by async-for with limit=100
    expected_at_least: int  # minimum we expect (rows we seeded + pre-existing baseline)


def _pagination_works(r: PaginationResult) -> str:
    """Single-character verdict per kind."""
    # Pagination works if async_for reaches at least expected_at_least rows.
    if r.async_for_total >= r.expected_at_least:
        return "OK"
    # Check if it's capped at first_page_size (ceiling hit).
    if r.async_for_total == r.first_page_size:
        return f"BROKEN(cap={r.first_page_size})"
    return f"PARTIAL(got={r.async_for_total},expected>={r.expected_at_least})"


async def count_existing(client: AsyncAnthropic, kind: str) -> int:
    """Count rows currently visible for a resource kind."""
    n = 0
    if kind == "agents":
        async for _ in client.beta.agents.list(include_archived=False, limit=100):
            n += 1
    elif kind == "environments":
        async for _ in client.beta.environments.list(include_archived=False, limit=100):
            n += 1
    elif kind == "skills":
        async for _ in client.beta.skills.list(limit=100):
            n += 1
    return n


async def probe_agents(client: AsyncAnthropic, existing: int) -> PaginationResult:
    """Probe agents.list with seeded data so we know we have > SMALL_LIMIT rows."""
    seeded_ids: list[str] = []

    try:
        # Seed SEED_COUNT agents.
        print(f"  seeding {SEED_COUNT} probe agents…")
        for i in range(SEED_COUNT):
            ag = await client.beta.agents.create(
                model="claude-haiku-4-5",
                name=f"probe-pagination-{i:03d}",
                metadata={"probe": PROBE_TAG},
            )
            seeded_ids.append(ag.id)
        print(f"  seeded {len(seeded_ids)} agents OK")

        total_expected = existing + SEED_COUNT

        # ── first page raw ──────────────────────────────────────────────────
        # await paginator yields the first AsyncPageCursor object directly.
        first_page: AsyncPageCursor[object] = await client.beta.agents.list(  # type: ignore[assignment]
            include_archived=False, limit=SMALL_LIMIT
        )
        first_page_size = len(first_page.data)  # type: ignore[attr-defined]
        first_page_next_page: str | None = getattr(first_page, "next_page", None)
        first_page_has_more: bool | None = getattr(first_page, "has_more", None)

        # ── async-for total (small limit) ───────────────────────────────────
        async_for_total = 0
        async for _ in client.beta.agents.list(include_archived=False, limit=SMALL_LIMIT):
            async_for_total += 1

        # ── explicit cursor following ───────────────────────────────────────
        explicit_total = 0
        cursor: str | None = None
        pages_visited = 0
        while True:
            if cursor is None:
                pg: AsyncPageCursor[object] = await client.beta.agents.list(  # type: ignore[assignment]
                    include_archived=False, limit=SMALL_LIMIT
                )
            else:
                pg = await client.beta.agents.list(  # type: ignore[assignment]
                    include_archived=False, limit=SMALL_LIMIT, page=cursor
                )
            pages_visited += 1
            pg_data = pg.data  # type: ignore[attr-defined]
            explicit_total += len(pg_data)
            next_cur: str | None = getattr(pg, "next_page", None)
            print(f"    explicit page {pages_visited}: size={len(pg_data)}, next_page={next_cur!r}")
            if not next_cur:
                break
            if next_cur == cursor:
                print("    WARNING: cursor did not advance — breaking to avoid loop")
                break
            cursor = next_cur

        # ── async-for with limit=100 ────────────────────────────────────────
        async_for_limit100 = 0
        async for _ in client.beta.agents.list(include_archived=False, limit=100):
            async_for_limit100 += 1

        return PaginationResult(
            kind="agents",
            total_rows_seeded=SEED_COUNT,
            first_page_size=first_page_size,
            first_page_has_more=first_page_has_more,
            first_page_next_page=first_page_next_page,
            async_for_total=async_for_total,
            explicit_cursor_total=explicit_total,
            async_for_limit100_total=async_for_limit100,
            expected_at_least=total_expected,
        )

    finally:
        print(f"  archiving {len(seeded_ids)} probe agents…")
        for aid in seeded_ids:
            try:
                await client.beta.agents.archive(aid)
            except Exception as e:
                print(f"    WARNING: failed to archive {aid}: {e}")
        print("  cleanup done")


async def probe_kind_no_seed(
    client: AsyncAnthropic,
    kind: str,
    existing: int,
) -> PaginationResult:
    """Probe environments or skills with only existing workspace data."""

    # ── first page raw ──────────────────────────────────────────────────────
    # await paginator yields the first AsyncPageCursor object directly.
    if kind == "environments":
        first_page: AsyncPageCursor[object] = await client.beta.environments.list(  # type: ignore[assignment]
            include_archived=False, limit=SMALL_LIMIT
        )
    else:  # skills
        first_page = await client.beta.skills.list(limit=SMALL_LIMIT)  # type: ignore[assignment]

    first_page_size = len(first_page.data)  # type: ignore[attr-defined]
    first_page_next_page: str | None = getattr(first_page, "next_page", None)
    first_page_has_more: bool | None = getattr(first_page, "has_more", None)

    # ── async-for total (small limit) ──────────────────────────────────────
    async_for_total = 0
    if kind == "environments":
        async for _ in client.beta.environments.list(include_archived=False, limit=SMALL_LIMIT):
            async_for_total += 1
    else:
        async for _ in client.beta.skills.list(limit=SMALL_LIMIT):
            async_for_total += 1

    # ── explicit cursor following ───────────────────────────────────────────
    explicit_total = 0
    cursor: str | None = None
    pages_visited = 0
    while True:
        if kind == "environments":
            if cursor is None:
                pg: AsyncPageCursor[object] = await client.beta.environments.list(  # type: ignore[assignment]
                    include_archived=False, limit=SMALL_LIMIT
                )
            else:
                pg = await client.beta.environments.list(  # type: ignore[assignment]
                    include_archived=False, limit=SMALL_LIMIT, page=cursor
                )
        else:
            if cursor is None:
                pg = await client.beta.skills.list(limit=SMALL_LIMIT)  # type: ignore[assignment]
            else:
                pg = await client.beta.skills.list(limit=SMALL_LIMIT, page=cursor)  # type: ignore[assignment]
        pages_visited += 1
        pg_data = pg.data  # type: ignore[attr-defined]
        explicit_total += len(pg_data)
        next_cur: str | None = getattr(pg, "next_page", None)
        print(f"    {kind} explicit page {pages_visited}: size={len(pg_data)}, next_page={next_cur!r}")
        if not next_cur:
            break
        if next_cur == cursor:
            print("    WARNING: cursor did not advance — breaking to avoid loop")
            break
        cursor = next_cur

    # ── async-for with limit=100 ────────────────────────────────────────────
    async_for_limit100 = 0
    if kind == "environments":
        async for _ in client.beta.environments.list(include_archived=False, limit=100):
            async_for_limit100 += 1
    else:
        async for _ in client.beta.skills.list(limit=100):
            async_for_limit100 += 1

    # For unseeded kinds, we just expect to see all existing rows via async-for.
    # If existing <= SMALL_LIMIT, pagination isn't stressed; note this.
    return PaginationResult(
        kind=kind,
        total_rows_seeded=0,
        first_page_size=first_page_size,
        first_page_has_more=first_page_has_more,
        first_page_next_page=first_page_next_page,
        async_for_total=async_for_total,
        explicit_cursor_total=explicit_total,
        async_for_limit100_total=async_for_limit100,
        expected_at_least=existing,
    )


def _print_table(results: list[PaginationResult]) -> None:
    print()
    print(
        f"{'KIND':<14} {'SEEDED':>6} {'FP_SIZE':>7} {'HAS_MORE':>9} {'NEXT_PAGE_PRESENT':>18} "
        f"{'ASYNC_FOR(L5)':>14} {'EXPLICIT(L5)':>13} {'ASYNC_FOR(L100)':>16} {'EXPECTED>=':>10}"
    )
    print("-" * 115)
    for r in results:
        np_present = "YES" if r.first_page_next_page else "NO"
        print(
            f"{r.kind:<14} {r.total_rows_seeded:>6} {r.first_page_size:>7} "
            f"{str(r.first_page_has_more):>9} {np_present:>18} "
            f"{r.async_for_total:>14} {r.explicit_cursor_total:>13} "
            f"{r.async_for_limit100_total:>16} {r.expected_at_least:>10}"
        )
    print()


def _verdict(results: list[PaginationResult]) -> str:
    statuses: list[tuple[str, str]] = []
    for r in results:
        # Only evaluate pagination stress if existing > SMALL_LIMIT.
        # If too few rows to stress, record as NOT_STRESSED.
        if r.expected_at_least <= SMALL_LIMIT and r.total_rows_seeded == 0:
            statuses.append((r.kind, "NOT_STRESSED"))
            continue
        v = _pagination_works(r)
        statuses.append((r.kind, v))

    works: list[str] = [k for k, v in statuses if v == "OK"]
    broken: list[str] = [k for k, v in statuses if v.startswith("BROKEN")]
    partial: list[str] = [k for k, v in statuses if v.startswith("PARTIAL")]
    not_stressed: list[str] = [k for k, v in statuses if v == "NOT_STRESSED"]

    details = "\n  ".join(f"{k}: {v}" for k, v in statuses)

    if broken and not works and not partial:
        return f"PAGINATION_BROKEN\n  {details}"
    if works and not broken and not partial:
        stressed: list[str] = [k for k, v in statuses if v == "OK"]
        if not_stressed:
            return (
                f"PAGINATION_WORKS (for stressed kinds: {stressed})\n"
                f"  NOT_STRESSED (too few rows to probe): {not_stressed}\n"
                f"  {details}"
            )
        return f"PAGINATION_WORKS\n  {details}"
    if broken or partial:
        return f"PAGINATION_PARTIAL\n  {details}"
    return f"INCONCLUSIVE\n  {details}"


async def main() -> None:
    load_dotenv()
    api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("DAIMON_ANTHROPIC__API_KEY")
    if not api_key:
        raise RuntimeError("Set ANTHROPIC_API_KEY or DAIMON_ANTHROPIC__API_KEY")

    client = AsyncAnthropic(api_key=api_key)
    results: list[PaginationResult] = []

    # ── Baseline counts ────────────────────────────────────────────────────
    print("== Counting existing workspace rows (limit=100 each) ==")
    existing_agents = await count_existing(client, "agents")
    existing_envs = await count_existing(client, "environments")
    existing_skills = await count_existing(client, "skills")
    print(
        f"  agents={existing_agents}  environments={existing_envs}  skills={existing_skills}"
    )

    # ── Agents: seed + probe ───────────────────────────────────────────────
    print(f"\n== agents.list — seeding {SEED_COUNT} probe agents, then probing ==")
    agents_result = await probe_agents(client, existing_agents)
    results.append(agents_result)

    # ── Environments: probe with existing data ─────────────────────────────
    print("\n== environments.list — probing existing workspace data (no seeding) ==")
    if existing_envs == 0:
        print("  WARNING: 0 environments found — cannot stress pagination")
    envs_result = await probe_kind_no_seed(client, "environments", existing_envs)
    results.append(envs_result)

    # ── Skills: probe with existing data ──────────────────────────────────
    print("\n== skills.list — probing existing workspace data (no seeding) ==")
    if existing_skills == 0:
        print("  WARNING: 0 skills found — cannot stress pagination")
    skills_result = await probe_kind_no_seed(client, "skills", existing_skills)
    results.append(skills_result)

    # ── Table ──────────────────────────────────────────────────────────────
    print("\n== RESULTS TABLE ==")
    print(f"  (small page limit used: {SMALL_LIMIT})")
    _print_table(results)

    # ── Verdict ───────────────────────────────────────────────────────────
    verdict = _verdict(results)
    print(f"VERDICT: {verdict}")


if __name__ == "__main__":
    asyncio.run(main())
