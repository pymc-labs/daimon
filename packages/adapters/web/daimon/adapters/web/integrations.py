"""`/api/integrations/*`: the chat install, GitHub, MCP servers and API tokens, for admins.

Everything read here comes from `daimon.core.integration_views`; tokens go through
`daimon.core.panel_operator_tokens`, the same service the chat panels call. No answer holds a
credential: an MCP server's address is shown without its query or sign-in, and a minted API
token appears once, in the answer to the request that made it.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime
from typing import Annotated, Literal
from urllib.parse import urlsplit

from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.adapters.web.admin_common import (
    listed_agents,
    names_by_account,
    person_ids_by_account,
)
from daimon.adapters.web.audit import audit
from daimon.adapters.web.caller import WebCaller, requires
from daimon.adapters.web.runtime import WebRuntime
from daimon.core import integration_views as views
from daimon.core.authz import Subject
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME
from daimon.core.integration_views import AgentServers, IntegrationsRefused
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_server_url import canonical_mcp_url
from daimon.core.operator_tokens import OperatorTokenError
from daimon.core.panel_audit import PanelOp, PanelOutcome, record_panel_write
from daimon.core.panel_operator_tokens import (
    LABEL_MAX_CHARS,
    PANEL_SCOPES,
    PANEL_TTL_DAYS,
    OperatorTokenNotAllowed,
    list_panel_operator_tokens,
    mint_panel_operator_token,
    revoke_panel_operator_token,
)
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

Viewer = Annotated[WebCaller, Depends(requires("integrations.view"))]
Manager = Annotated[WebCaller, Depends(requires("integrations.manage"))]

NO_TOKEN = "This API token isn't here."
NOT_SET_UP = "API tokens aren't set up on this install."
BAD_SCOPES = "Pick at least one permission from the list."
UNAVAILABLE = "Agents are not available right now."

_SCOPE_LABELS: dict[str, str] = {
    "tenant:read": "Read the workspace summary",
    "channels:write": "Change channel settings",
    "agents:archive": "Archive channel copies",
    "promo:redeem": "Redeem promo codes",
}
_ERROR_WORDS = 80


class Team(BaseModel):
    id: str
    name: str | None


class Install(BaseModel):
    platform: str
    name: str
    status: Literal["connected", "pending", "failed", "archived", "disconnected"]
    problem: bool
    teams: list[Team]


class Workspaces(BaseModel):
    workspaces: list[Install]


class GithubRepo(BaseModel):
    name: str
    access: Literal["read", "write"]
    shared: bool
    status: Literal["active", "suspended", "revoked"]
    working: bool


class GithubAgent(BaseModel):
    id: str
    name: str
    repos: list[GithubRepo]


class GithubInstall(BaseModel):
    id: str
    account: str
    selection: str | None
    repos: int
    state: Literal["active", "suspended", "removed"]
    refreshed_at: str | None


class GithubRequest(BaseModel):
    id: str
    agent: str
    agent_id: str | None
    repos: list[str]
    access: Literal["read", "write"]
    requester: str
    requester_id: str | None
    created_at: str
    expires_at: str
    waiting_github: bool


class GithubTrouble(BaseModel):
    kind: Literal["push", "installation"]
    subject: str
    attempts: int
    error: str
    at: str


class Github(BaseModel):
    configured: bool
    installations: list[GithubInstall]
    agents: list[GithubAgent]
    requests: list[GithubRequest]
    trouble: list[GithubTrouble]


class McpAgent(BaseModel):
    id: str
    name: str
    shared: bool
    signed_in: int


class McpServer(BaseModel):
    id: str
    name: str
    url: str
    agents: list[McpAgent]
    signed_in: int


class Mcp(BaseModel):
    servers: list[McpServer]


class ExpiredServer(BaseModel):
    id: str
    expired: int
    people: list[str]


class McpExpiry(BaseModel):
    complete: bool
    servers: list[ExpiredServer]


class Scope(BaseModel):
    id: str
    label: str


class Token(BaseModel):
    id: str
    label: str | None
    scopes: list[str]
    created_at: str
    expires_at: str | None


class Tokens(BaseModel):
    configured: bool
    scopes: list[Scope]
    days: int
    tokens: list[Token]


class MintBody(BaseModel):
    scopes: list[str] = Field(min_length=1, max_length=len(PANEL_SCOPES))
    label: str | None = Field(default=None, max_length=LABEL_MAX_CHARS)


class Minted(BaseModel):
    token: str
    id: str
    scopes: list[str]
    expires_at: str


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment is not None else None


def _agent_name(agent: BetaManagedAgentsAgent) -> str:
    return str(agent.metadata.get(MA_METADATA_KEY_NAME) or agent.name)


def _server_id(url: str) -> str:
    return hashlib.sha256(canonical_mcp_url(url).encode()).hexdigest()[:12]


def _shown_url(url: str) -> str:
    """The server's address without a sign-in or query, which can carry a key."""
    parts = urlsplit(url.strip())
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{host}{port}{parts.path}".rstrip("/")


def _short(error: str) -> str:
    """A failure's kind, not its text: provider messages can name accounts or addresses."""
    return error.split(":", 1)[0].strip()[:_ERROR_WORDS]


def _refused(exc: IntegrationsRefused) -> HTTPException:
    return HTTPException(status_code=403, detail=str(exc))


def integrations_router(runtime: WebRuntime) -> APIRouter:
    router = APIRouter(prefix="/api/integrations")

    async def agents_of(caller: WebCaller) -> list[tuple[uuid.UUID, BetaManagedAgentsAgent]]:
        tenant_id = caller.person.tenant_id
        return [
            (derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=a.id), a)
            for a in await listed_agents(runtime, tenant_id)
        ]

    async def subject_of(db: AsyncSession, caller: WebCaller) -> Subject:
        return (await caller.principal(db)).subject

    @router.get("/workspaces")
    async def workspaces(caller: Viewer) -> Workspaces:  # pyright: ignore[reportUnusedFunction]
        async with runtime.sessionmaker() as db:
            try:
                install = await views.load_workspace_install(
                    db,
                    tenant_id=caller.person.tenant_id,
                    subject=await subject_of(db, caller),
                    workspace_name=caller.person.workspace_name,
                    now=runtime.now(),
                )
            except IntegrationsRefused as exc:
                raise _refused(exc) from exc
        if install is None:
            return Workspaces(workspaces=[])
        return Workspaces(
            workspaces=[
                Install(
                    platform=install.platform,
                    name=install.name,
                    status=install.status,
                    problem=install.problem,
                    teams=[Team(id=t.id, name=t.name) for t in install.teams],
                )
            ]
        )

    @router.get("/github")
    async def github(caller: Viewer) -> Github:  # pyright: ignore[reportUnusedFunction]
        tenant_id = caller.person.tenant_id
        agents = await agents_of(caller)
        named = {agent_uuid: _agent_name(a) for agent_uuid, a in agents}
        ma_ids = {agent_uuid: a.id for agent_uuid, a in agents}
        async with runtime.sessionmaker() as db:
            try:
                overview = await views.load_github_overview(
                    db,
                    tenant_id=tenant_id,
                    subject=await subject_of(db, caller),
                    agent_ids=list(named),
                    now=runtime.now(),
                )
            except IntegrationsRefused as exc:
                raise _refused(exc) from exc
            people = await names_by_account(db, tenant_id)
            person_ids = await person_ids_by_account(db, tenant_id)
        return Github(
            configured=runtime.settings.github_app.app_id is not None,
            installations=[
                GithubInstall(
                    id=str(i.installation_id),
                    account=i.account_login,
                    selection=i.repository_selection,
                    repos=i.repos,
                    state="removed"
                    if i.removed_at is not None
                    else "suspended"
                    if i.suspended_at is not None
                    else "active",
                    refreshed_at=_iso(i.refreshed_at),
                )
                for i in overview.installations
            ],
            agents=[
                GithubAgent(
                    id=ma_ids[a.agent_id],
                    name=named[a.agent_id],
                    repos=[
                        GithubRepo(
                            name=r.full_name,
                            access=r.access,
                            shared=r.scope == "shared",
                            status=r.status,
                            working=r.working,
                        )
                        for r in a.repos
                    ],
                )
                for a in overview.agents
                if a.repos
            ],
            requests=[
                GithubRequest(
                    id=str(r.id),
                    agent=named.get(r.agent_id, r.agent_name),
                    agent_id=ma_ids.get(r.agent_id),
                    repos=list(r.repo_names),
                    access=r.required_ability,
                    requester=people.get(r.requester_account_id, "Someone"),
                    requester_id=person_ids.get(r.requester_account_id),
                    created_at=r.created_at.isoformat(),
                    expires_at=r.expires_at.isoformat(),
                    waiting_github=r.status == "waiting_github",
                )
                for r in overview.requests
            ],
            trouble=[
                GithubTrouble(
                    kind=t.kind,
                    subject=t.subject,
                    attempts=t.attempts,
                    error=_short(t.error),
                    at=t.at.isoformat(),
                )
                for t in overview.trouble
            ],
        )

    def servers_of(agents: list[tuple[uuid.UUID, BetaManagedAgentsAgent]]) -> list[AgentServers]:
        return [
            AgentServers(
                agent_id=agent_uuid,
                name=_agent_name(a),
                servers=tuple((s.name, s.url) for s in a.mcp_servers),
            )
            for agent_uuid, a in agents
        ]

    @router.get("/mcp")
    async def mcp(caller: Viewer) -> Mcp:  # pyright: ignore[reportUnusedFunction]
        listed = await agents_of(caller)
        ma_ids = {agent_uuid: a.id for agent_uuid, a in listed}
        async with runtime.sessionmaker() as db:
            try:
                catalogue = await views.load_mcp_catalogue(
                    db,
                    tenant_id=caller.person.tenant_id,
                    subject=await subject_of(db, caller),
                    agents=servers_of(listed),
                )
            except IntegrationsRefused as exc:
                raise _refused(exc) from exc
        return Mcp(
            servers=[
                McpServer(
                    id=_server_id(s.url),
                    name=s.name,
                    url=_shown_url(s.url),
                    agents=[
                        McpAgent(
                            id=ma_ids[u.agent_id],
                            name=u.name,
                            shared=u.shared_token,
                            signed_in=u.signed_in,
                        )
                        for u in s.agents
                    ],
                    signed_in=s.signed_in,
                )
                for s in catalogue
            ]
        )

    @router.get("/mcp/expiry")
    async def expiry(caller: Viewer) -> McpExpiry:  # pyright: ignore[reportUnusedFunction]
        """Sign-ins that lapsed with nothing to renew them; asks the provider, so it is separate."""
        client = runtime.anthropic or (runtime.turns.deps.anthropic if runtime.turns else None)
        if client is None:
            raise HTTPException(status_code=503, detail=UNAVAILABLE)
        tenant_id = caller.person.tenant_id
        urls = {url for a in servers_of(await agents_of(caller)) for _, url in a.servers}
        async with runtime.sessionmaker() as db:
            try:
                lapsed, complete = await views.check_mcp_expiry(
                    client,
                    db,
                    tenant_id=tenant_id,
                    subject=await subject_of(db, caller),
                    server_urls=urls,
                    now=runtime.now(),
                )
            except IntegrationsRefused as exc:
                raise _refused(exc) from exc
            people = await names_by_account(db, tenant_id)
        by_server: dict[str, list[str]] = {}
        for item in lapsed:
            by_server.setdefault(item.server_url, []).append(people.get(item.account_id, "Someone"))
        return McpExpiry(
            complete=complete,
            servers=[
                ExpiredServer(id=_server_id(url), expired=len(names), people=sorted(set(names)))
                for url, names in by_server.items()
            ],
        )

    def configured() -> bool:
        return runtime.settings.mcp.jwt_secret is not None

    @router.get("/tokens")
    async def tokens(caller: Viewer) -> Tokens:  # pyright: ignore[reportUnusedFunction]
        async with runtime.sessionmaker() as db:
            try:
                views.require_admin(await subject_of(db, caller))
            except IntegrationsRefused as exc:
                raise _refused(exc) from exc
            rows = await list_panel_operator_tokens(
                db, tenant_id=caller.person.tenant_id, now=runtime.now()
            )
        return Tokens(
            configured=configured(),
            scopes=[Scope(id=s, label=_SCOPE_LABELS.get(s, s)) for s in PANEL_SCOPES],
            days=PANEL_TTL_DAYS,
            tokens=[
                Token(
                    id=str(r.jti),
                    label=r.label,
                    scopes=sorted(r.scopes),
                    created_at=r.created_at.isoformat(),
                    expires_at=_iso(r.expires_at),
                )
                for r in rows
            ],
        )

    async def record(
        caller: WebCaller,
        db: AsyncSession,
        *,
        op: PanelOp,
        outcome: PanelOutcome,
        reason: str,
        jti: uuid.UUID | None,
    ) -> None:
        principal = await caller.principal(db)
        await record_panel_write(
            runtime.sessionmaker,
            tenant_id=caller.person.tenant_id,
            platform=principal.platform,
            platform_user_id=principal.external_id,
            op=op,
            outcome=outcome,
            reason=reason,
            token_kind="operator",
            token_jti=jti,
        )

    @router.post("/tokens")
    async def mint(  # pyright: ignore[reportUnusedFunction]
        body: MintBody, caller: Manager, response: Response
    ) -> Minted:
        secret = runtime.settings.mcp.jwt_secret
        if secret is None:
            raise HTTPException(status_code=503, detail=NOT_SET_UP)
        tenant_id, now = caller.person.tenant_id, runtime.now()
        response.headers["Cache-Control"] = "no-store"
        try:
            async with runtime.sessionmaker.begin() as db:
                principal = await caller.principal(db)
                minted = await mint_panel_operator_token(
                    db,
                    subject=principal.subject,
                    tenant_id=tenant_id,
                    platform=principal.platform,
                    platform_user_id=principal.external_id,
                    scopes=body.scopes,
                    label=body.label,
                    secret=secret.get_secret_value().encode(),
                    now=now,
                )
                label = (body.label or "").strip()
                await audit(
                    db,
                    tenant_id=tenant_id,
                    actor_account_id=caller.person.account_id,
                    action="Created API token",
                    target="API token",
                    detail=f"{label or 'No label'}: {', '.join(sorted(minted.scopes))}",
                    at=now,
                )
        except OperatorTokenNotAllowed as exc:
            async with runtime.sessionmaker() as db:
                await record(
                    caller,
                    db,
                    op="operator_token_mint",
                    outcome="denied",
                    reason="needs_admin",
                    jti=None,
                )
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except OperatorTokenError as exc:
            async with runtime.sessionmaker() as db:
                await record(
                    caller,
                    db,
                    op="operator_token_mint",
                    outcome="denied",
                    reason="scopes",
                    jti=None,
                )
            raise HTTPException(status_code=422, detail=BAD_SCOPES) from exc
        async with runtime.sessionmaker() as db:
            await record(
                caller,
                db,
                op="operator_token_mint",
                outcome="allowed",
                reason="completed",
                jti=minted.jti,
            )
        return Minted(
            token=minted.token,
            id=str(minted.jti),
            scopes=sorted(minted.scopes),
            expires_at=minted.expires_at.isoformat(),
        )

    @router.delete("/tokens/{jti}", status_code=204)
    async def revoke(jti: uuid.UUID, caller: Manager) -> Response:  # pyright: ignore[reportUnusedFunction]
        tenant_id, now = caller.person.tenant_id, runtime.now()
        async with runtime.sessionmaker.begin() as db:
            try:
                revoked = await revoke_panel_operator_token(
                    db,
                    subject=await subject_of(db, caller),
                    tenant_id=tenant_id,
                    jti=jti,
                    now=now,
                )
            except OperatorTokenError as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc
            if revoked:
                await audit(
                    db,
                    tenant_id=tenant_id,
                    actor_account_id=caller.person.account_id,
                    action="Revoked API token",
                    target="API token",
                    detail=str(jti)[:8],
                    at=now,
                )
        async with runtime.sessionmaker() as db:
            await record(
                caller,
                db,
                op="operator_token_revoke",
                outcome="allowed" if revoked else "error",
                reason="completed" if revoked else "already_revoked",
                jti=jti,
            )
        if not revoked:
            raise HTTPException(status_code=404, detail=NO_TOKEN)
        return Response(status_code=204)

    return router
