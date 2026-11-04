"""Executable record: server skill updates publish to one target, which every provider reads.

Each agent's Managed Agents record holds its skill pins. OpenAI, Gemini and self-hosted
runs build their skills from that pinned version at turn start, so publishing a new
version there and repinning following agents moves every provider. The sync therefore
wires one target. Skills pinned on a fixed operator setting are not repinned. If a
provider ever keeps its own pins, add its target here rather than deleting this record.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import httpx
from cryptography.fernet import Fernet
from daimon.adapters.web.nodes.data_server import DataServerNode
from daimon.adapters.web.runtime import WebRuntime
from daimon.adapters.web.server_skills import sync_deps
from daimon.core.config import Settings, WebSettings
from daimon.core.github_credentials import build_multifernet
from daimon.core.server_skills.anthropic import ManagedAgentsTarget
from mux.testing.drivers.anthropic.ma import MARouter, build_fake_anthropic
from sqlalchemy.ext.asyncio import async_sessionmaker


async def test_server_skill_updates_publish_to_the_managed_agents_record_only() -> None:
    async with httpx.AsyncClient() as http:
        runtime = WebRuntime(
            settings=Settings(_env_file=None, database={"url": "postgresql+asyncpg://u:p@h/db"}),  # pyright: ignore[reportCallIssue, reportArgumentType]
            web=WebSettings(public_url="https://web.example.com"),  # pyright: ignore[reportCallIssue, reportArgumentType]
            sessionmaker=async_sessionmaker(),
            http_client=http,
            fernet=build_multifernet((Fernet.generate_key().decode(),)),
            anthropic=build_fake_anthropic(MARouter().dispatch),
        )
        node = SimpleNamespace(server=SimpleNamespace(name="data"))
        deps = sync_deps(runtime, cast(DataServerNode, node))
    assert [type(t) for t in deps.targets] == [ManagedAgentsTarget]
