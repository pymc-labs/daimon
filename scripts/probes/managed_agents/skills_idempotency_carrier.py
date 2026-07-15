"""Probe: what MA-side carrier (if any) can support skill idempotency?

L13 bug: defaults apply uploads a new skill version on every run, even when
SKILL.md content is unchanged. Skills can't use the `metadata.daimon_spec_hash`
trick agents/envs use because `client.beta.skills.list/retrieve` returns no
metadata column.

This probe answers: is there ANYTHING MA returns that we can use to detect
"no change since last upload" without persisting a hash locally? Specifically:

1. skills.list row shape — what's `latest_version` (counter? hash? id?)
2. skills.retrieve shape — same fields, plus anything else
3. skills.versions.list shape — per-version metadata (digest? size? hash?)
4. skills.versions.retrieve shape — anything content-derived?

Read-only. Picks one daimon-system: skill and inspects.
"""

from __future__ import annotations

import asyncio
import os

from anthropic import AsyncAnthropic


def dump(label: str, obj: object) -> None:
    print(f"\n== {label} ==")
    if hasattr(obj, "model_dump"):
        for k, v in obj.model_dump(mode="json").items():  # type: ignore[attr-defined]
            print(f"  {k}: {v!r}")
    else:
        print(f"  {obj!r}")


async def main() -> None:
    api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ["DAIMON_ANTHROPIC__API_KEY"]
    client = AsyncAnthropic(api_key=api_key)

    # Find a daimon-system skill to inspect (prefer cli-auth since that's the duplicate)
    target = None
    candidates: list[object] = []
    print("== skills.list — ALL rows (looking for non-anthropic source) ==")
    async for sk in client.beta.skills.list(limit=100):
        dt = getattr(sk, "display_title", None)
        src = getattr(sk, "source", None)
        print(
            f"  id={sk.id!r}  source={src!r}  display_title={dt!r}  "
            f"latest_version={getattr(sk, 'latest_version', '<MISSING>')!r}  "
            f"created_at={getattr(sk, 'created_at', '<MISSING>')!r}  "
            f"updated_at={getattr(sk, 'updated_at', '<MISSING>')!r}"
        )
        if src != "anthropic":
            candidates.append(sk)
            if dt and "cli-auth" in dt and target is None:
                target = sk

    if target is None and candidates:
        target = candidates[0]  # type: ignore[assignment]
    if target is None:
        print("no daimon-system: skills found, aborting")
        return

    print(f"\n=== target: {target.id} ({target.display_title!r}) ===")  # type: ignore[attr-defined]

    # 1. skills.retrieve full shape
    retrieved = await client.beta.skills.retrieve(target.id)  # type: ignore[attr-defined]
    dump("skills.retrieve()", retrieved)

    # 2. skills.versions.list — full per-version detail
    print("\n== skills.versions.list ==")
    versions: list[object] = []
    async for v in client.beta.skills.versions.list(target.id):  # type: ignore[attr-defined]
        versions.append(v)
    print(f"  total versions: {len(versions)}")
    for i, v in enumerate(versions[:3], 1):
        dump(f"version [{i}]", v)

    # 3. skills.versions.retrieve on the newest
    if versions:
        newest = versions[0]
        # try common id field names
        vid = getattr(newest, "id", None) or getattr(newest, "version", None)
        if vid:
            try:
                vr = await client.beta.skills.versions.retrieve(
                    target.id, vid  # type: ignore[attr-defined,arg-type]
                )
                dump("skills.versions.retrieve(newest)", vr)
            except Exception as e:
                print(f"\n  versions.retrieve failed: {type(e).__name__}: {e}")


if __name__ == "__main__":
    asyncio.run(main())
