"""Who may read or change an agent from the web app, and the dependencies an edit needs."""

from __future__ import annotations

from typing import Annotated

from daimon.adapters.web.access import is_admin
from daimon.adapters.web.caller import WebCaller, requires
from daimon.adapters.web.channel_names import channel_lookup_of
from daimon.adapters.web.faces import avatar_base_url
from daimon.adapters.web.runtime import WebRuntime
from daimon.core.agent_changes import AgentChangeDeps
from daimon.core.agent_editing import AgentActor, AgentEditDeps, AgentEditError
from daimon.core.agent_view import AgentViewDeps, AgentViewer
from daimon.core.authz import Subject, Surface
from daimon.core.channel_backend import make_backend_service
from fastapi import Depends, HTTPException

Viewer = Annotated[WebCaller, Depends(requires("agents.view"))]
Editor = Annotated[WebCaller, Depends(requires("agents.edit"))]
Creator = Annotated[WebCaller, Depends(requires("agents.create"))]

_STATUS = {
    "forbidden": 403,
    "not_found": 404,
    "stale": 409,
    "conflict": 409,
    "taken": 409,
    "invalid": 422,
}


def edit_refusal(exc: AgentEditError) -> HTTPException:
    return HTTPException(status_code=_STATUS.get(exc.kind, 400), detail=str(exc))


def change_deps(runtime: WebRuntime) -> AgentChangeDeps:
    if runtime.anthropic is None:
        raise HTTPException(status_code=503, detail="Agents are not available here.")
    public_url = runtime.settings.mcp.public_url
    base = str(public_url) if public_url is not None else None
    return AgentChangeDeps(
        edit=AgentEditDeps(
            client=runtime.anthropic,
            session_factory=runtime.sessionmaker,
            public_url=base,
            denied_tools=runtime.settings.tool_safety.denied,
            resolver_cache=runtime.resolver_cache,
            deployment_default=runtime.deployment_default,
        ),
        view=AgentViewDeps(
            client=runtime.anthropic,
            session_factory=runtime.sessionmaker,
            markup=runtime.settings.billing.markup,
            avatar_base_url=avatar_base_url(runtime),
            deployment_default=runtime.deployment_default,
            skill_descriptions=runtime.skill_descriptions,
            mcp_tools=runtime.mcp_tool_lister,
            fernet=runtime.fernet,
            channel_lookup=channel_lookup_of(runtime),
        ),
        backends=make_backend_service(runtime.sessionmaker, runtime.settings.turn),
    )


def viewer_of(caller: WebCaller) -> AgentViewer:
    return AgentViewer(
        account_id=caller.person.account_id,
        is_admin=is_admin(caller.role),
        may_edit="agents.edit" in caller.capabilities,
    )


def actor_of(caller: WebCaller) -> AgentActor:
    account = caller.person.account_id
    return AgentActor(
        tenant_id=caller.person.tenant_id,
        account_id=account,
        subject=Subject(is_admin=is_admin(caller.role), platform_user_id=str(account)),
        surface=Surface.WEB,
    )
