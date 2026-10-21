"""The real mux driver and harness communicate over a loopback HTTP socket."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import httpx
from mux.contracts.actions import UserMessage
from mux.contracts.events import TextPart
from mux.contracts.ids import ModelRef, ResourceRef, Revision, Scope
from mux.contracts.ports import Agents, Environments
from mux.contracts.resources import Agent, AgentSpec, Environment, EnvironmentSpec, SessionSpec
from mux.drivers.selfhosted import SelfHostedDriver
from mux.drivers.selfhosted.transport import DirectTransport

SCOPE = Scope(
    tenant_id="tenant", account_id="account", principal_id="person", authorization_id="grant"
)


def resource_ref(kind: str) -> ResourceRef:
    return ResourceRef(
        id=kind,
        kind=kind,
        provider="selfhosted",
        account_scope_id="tenant",
        tenant_id=SCOPE.tenant_id,
        account_id=None,
    )


class ResourceStore:
    def __init__(self, value):
        self.value = value

    async def retrieve(self, scope, ref):
        assert scope == SCOPE
        assert ref == self.value.ref
        return self.value


async def driver_session(client: httpx.AsyncClient, *, tools=None, mcp_servers=None):
    agents = ResourceStore(
        Agent(
            ref=resource_ref("agent"),
            revision=Revision(local=1),
            created_at=datetime.now(UTC),
            spec=AgentSpec(
                name="interop",
                system="test instructions",
                tools=tools,
                mcp_servers=mcp_servers,
                model=ModelRef(provider="selfhosted", id="gpt-6-luna"),
            ),
        )
    )
    environments = ResourceStore(
        Environment(
            ref=resource_ref("environment"),
            revision=Revision(local=1),
            created_at=datetime.now(UTC),
            spec=EnvironmentSpec(name="interop", execution="self_hosted"),
        )
    )
    driver = SelfHostedDriver(
        DirectTransport(client, base_url=str(client.base_url), token="test-token"),
        account_scope_id="tenant",
        agents=cast(Agents, agents),
        environments=cast(Environments, environments),
        authorization=lambda scope, kind, id_: scope == SCOPE,
        deadline_s=10,
    )
    session = await driver.sessions.create(
        SCOPE,
        SessionSpec(
            agent=agents.value.ref,
            agent_revision=Revision(local=1),
            environment=environments.value.ref,
            config_revision=1,
        ),
        key="create",
    )
    return driver, session


def message(text: str) -> UserMessage:
    return UserMessage(content=(TextPart(text=text),))


def workspace(home: Path, session_id: str) -> Path:
    return home / "sessions" / hashlib.sha256(session_id.encode()).hexdigest()[:32] / "work"


async def collect(driver, session, *, after=None):
    async with asyncio.timeout(10):
        return [
            event
            async for event in await driver.events.open_stream(
                SCOPE,
                session.ref,
                after=after,
                previews=True,
            )
        ]
