"""A pinned skill version downloads by its id; "latest" is the newest version."""

from collections import deque

import httpx
import pytest
from mux.contracts.ids import Scope, SkillRef
from mux.drivers.anthropic import AnthropicManagedAgents, SkillVersions
from mux.errors import ProviderError
from mux.testing.drivers.anthropic.ma_transport import ScriptedReply, ScriptedTransport

SCOPE = Scope.platform(reason="test skill download")


def version(number: str, created_at: str) -> dict[str, object]:
    return {
        "id": f"ver_{number}",
        "skill_id": "s",
        "version": number,
        "type": "skill_version",
        "name": "s",
        "description": "",
        "directory": "s",
        "created_at": created_at,
    }


# Version 3 is newer, though its text sorts first: the times are compared, not the text.
ROWS = [version("1", "2026-03-01T01:00:00+05:00"), version("3", "2026-02-28T21:00:00Z")]


def transport(*replies: ScriptedReply) -> ScriptedTransport:
    listed = ScriptedReply(
        "GET", "/v1/skills/s/versions", httpx.Response(200, json={"data": ROWS, "next_page": None})
    )
    return ScriptedTransport(deque((listed, *replies)))


async def download(sdk: ScriptedTransport, pinned: str) -> bytes:
    async with sdk.client() as client:
        port = AnthropicManagedAgents(client).extension(
            SkillVersions, namespace="anthropic.skills_versions", version=1
        )
        ref = SkillRef(id="s", source="custom", version=pinned)
        return b"".join([chunk async for chunk in port.download(SCOPE, ref)])


async def test_latest_downloads_the_newest_version() -> None:
    sdk = transport(
        ScriptedReply(
            "GET", "/v1/skills/s/versions/ver_3/content", httpx.Response(200, content=b"ZIP")
        )
    )
    assert await download(sdk, "latest") == b"ZIP"
    assert not sdk.violations, sdk.violations


async def test_a_missing_version_is_not_found() -> None:
    with pytest.raises(ProviderError) as raised:
        await download(transport(), "2")
    assert raised.value.category == "not_found", "a version the skill lacks is not found"
