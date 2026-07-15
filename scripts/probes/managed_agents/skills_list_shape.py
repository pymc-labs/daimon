"""Narrow probe: what does beta.skills.list() actually return, via the SDK?

Diagnoses defaults-apply bug 3: reconcile tries to CREATE
'daimon-system:brainstorming' even though a skill with that display_title
already exists on this API key. That implies `find_skill_by_display_title`
(core/defaults/ma_index.py:64) returns None when iterating
`client.beta.skills.list()`.

Two working theories, both answerable from the raw list response:

1. The SDK's `beta.skills.list()` paginates, and `async for` doesn't exhaust
   all pages by default for this endpoint — so the brainstorming skill lives
   on page 2+.
2. The existing skill has no `display_title` attribute, or the attribute
   name differs from what we assumed (e.g. `title`, `name`).

This probe iterates every skill the SDK returns, prints each one's id +
display_title + created_at + metadata shape, and counts total returned. No
filtering, no matching — just "show me everything you'll give me."
"""

from __future__ import annotations

import asyncio
import os

from anthropic import AsyncAnthropic
from dotenv import load_dotenv


async def main() -> None:
    load_dotenv()
    client = AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    # Try the SDK's paginator as we use it in find_skill_by_display_title.
    count = 0
    found_brainstorming = False
    print("== client.beta.skills.list() — every row ==")
    async for sk in client.beta.skills.list():
        count += 1
        dt = getattr(sk, "display_title", "<MISSING>")
        name = getattr(sk, "name", "<MISSING>")
        created = getattr(sk, "created_at", "<MISSING>")
        md = getattr(sk, "metadata", "<MISSING>")
        attrs = sorted(vars(sk).keys()) if hasattr(sk, "__dict__") else "<no __dict__>"
        print(
            f"  #{count}  id={sk.id!r}  display_title={dt!r}  name={name!r}  "
            f"created_at={created!r}  metadata={md!r}"
        )
        if count == 1:
            print(f"    ALL ATTRS: {attrs}")
        if dt == "daimon-system:brainstorming":
            found_brainstorming = True

    print(f"\ntotal skills returned: {count}")
    print(f"daimon-system:brainstorming present: {found_brainstorming}")

    # Try paginator-explicit shapes in case async-for default behavior differs.
    print("\n== explicit page fetch: client.beta.skills.list(limit=100) ==")
    explicit_count = 0
    async for sk in client.beta.skills.list(limit=100):
        explicit_count += 1
    print(f"total with limit=100: {explicit_count}")


if __name__ == "__main__":
    asyncio.run(main())
