"""Synthetic identities and network-edge fakes for mocked permission tests."""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Awaitable, Callable
from typing import Literal

import httpx
from microsoft_teams.common.http.client import MiddlewareContext

from .conftest import (
    AAD_OBJECT_ID,
    ADMIN_TEAM,
    ENTRA_TENANT_ID,
    THREAD_ID,
    TeamsApiFake,
)

FOREIGN_TENANT = str(uuid.UUID(int=99))
MEMBER_ID = str(uuid.UUID(int=3))
GUEST_ID = str(uuid.UUID(int=4))
EXTERNAL_ID = str(uuid.UUID(int=5))
PersonaName = Literal["admin", "member", "guest", "external"]


@dataclasses.dataclass(frozen=True)
class Persona:
    """One synthetic sender; a foreign tenant stays visible on the activity."""

    name: PersonaName
    user_id: str
    tenant_id: str
    role: str


PERSONAS: tuple[Persona, ...] = (
    Persona("admin", AAD_OBJECT_ID, ENTRA_TENANT_ID, "user"),
    Persona("member", MEMBER_ID, ENTRA_TENANT_ID, "user"),
    Persona("guest", GUEST_ID, ENTRA_TENANT_ID, "guest"),
    Persona("external", EXTERNAL_ID, FOREIGN_TENANT, "user"),
)


def from_outside(invoke: dict[str, object]) -> dict[str, object]:
    """`invoke` by the external persona, on an answer in `THREAD_ID`."""
    sender = {"id": f"29:{EXTERNAL_ID}", "aadObjectId": EXTERNAL_ID, "tenantId": FOREIGN_TENANT}
    conversation = {"id": THREAD_ID, "conversationType": "channel", "tenantId": ENTRA_TENANT_ID}
    return invoke | {"from": sender, "conversation": conversation}


@dataclasses.dataclass
class PersonaTeamsApiFake(TeamsApiFake):
    """Answer real SDK member reads with each synthetic sender's roster facts."""

    active: Persona | None = None

    async def send(
        self,
        context: MiddlewareContext,
        next: Callable[[], Awaitable[httpx.Response]],
    ) -> httpx.Response:
        path = httpx.URL(context.url).path
        if context.method == "GET" and "/members/" in path:
            user_id = path.rsplit("/", 1)[-1]
            persona = None
            for candidate in PERSONAS:
                if candidate.user_id == user_id:
                    persona = candidate
                    break
            if persona is not None:
                return httpx.Response(
                    200,
                    json={
                        "id": f"29:{persona.user_id}",
                        "aadObjectId": persona.user_id,
                        "tenantId": persona.tenant_id,
                        "userRole": persona.role,
                    },
                    request=httpx.Request(context.method, context.url),
                )
        if context.method == "GET" and path.endswith("/members"):
            roster = [
                {
                    "id": f"29:{p.user_id}",
                    "aadObjectId": p.user_id,
                    "tenantId": p.tenant_id,
                    "userRole": p.role,
                }
                for p in PERSONAS
                if self.active is None or p == self.active
            ]
            roster.append({"id": "28:bot-client-id", "name": "daimon"})
            return httpx.Response(
                200, json=roster, request=httpx.Request(context.method, context.url)
            )
        return await super().send(context, next)


@dataclasses.dataclass
class OwnerGraphFake:
    """Serve Graph owner and member reads through the runtime's HTTP transport."""

    owners: set[str] = dataclasses.field(default_factory=lambda: {AAD_OBJECT_ID, GUEST_ID})
    requests: list[str] = dataclasses.field(default_factory=list[str])

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        if request.method == "GET" and request.url.path.endswith(f"/teams/{ADMIN_TEAM}/members"):
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "userId": p.user_id,
                            "roles": ["owner", "guest"] if p.name == "guest" else ["owner"],
                        }
                        for p in PERSONAS
                        if p.user_id in self.owners
                    ]
                },
                request=request,
            )
        if request.method == "GET" and request.url.path.endswith("/allMembers"):
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "userId": p.user_id,
                            "tenantId": p.tenant_id,
                            "roles": ["guest"] if p.name == "guest" else [],
                        }
                        for p in PERSONAS
                    ]
                },
                request=request,
            )
        return httpx.Response(404, request=request)
