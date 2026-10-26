"""Run: uv run python -m packages.core.tests.context_provider_demo (no network)."""

from __future__ import annotations

import asyncio
import json

from daimon.core.context_provider import ContextProviders

from .context_card_stub import FixtureProvider
from .test_context_provider import FIXTURE, TENANT
from .turn.test_context_provider import drive


async def main() -> None:
    owner = ContextProviders(provider=FixtureProvider(FIXTURE, tenant_id=str(TENANT)))
    for title, session in (
        ("FIRST TURN WITH CARDS", "s1"),
        ("SECOND TURN DEDUPED", "s1"),
        ("RESET SESSION", "replacement"),
    ):
        batch = await drive(owner, session_id=session)
        print(title)
        print(json.dumps(batch, indent=2))
    print("NO PROVIDER CONFIGURED")

    async def disabled(request):
        return None

    print(json.dumps(await drive(owner, load=disabled), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
