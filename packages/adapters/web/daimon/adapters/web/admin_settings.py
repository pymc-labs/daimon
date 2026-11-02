"""`/api/admin/settings`: the workspace's modules and default agent."""

from __future__ import annotations

from typing import Any, Literal

from daimon.adapters.web.admin_common import WorkspaceAdmin, listed_agents
from daimon.adapters.web.audit import audit
from daimon.adapters.web.caller import WebCaller
from daimon.adapters.web.runtime import WebRuntime
from daimon.core.stores import web_workspace_settings as workspace_settings
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

_SWITCHABLE: tuple[Literal["chat", "agents", "credits"], ...] = ("chat", "agents", "credits")


class SettingsChange(BaseModel):
    modules: dict[Literal["chat", "agents", "credits"], bool] | None = None
    default_agent: str | None = None


def settings_router(runtime: WebRuntime) -> APIRouter:
    router = APIRouter(prefix="/api/admin")

    async def settings_json(caller: WebCaller) -> dict[str, Any]:
        tenant_id = caller.person.tenant_id
        async with runtime.sessionmaker() as db:
            chosen = await workspace_settings.get_settings(db, tenant_id=tenant_id)
        default = chosen.default_agent
        if default is None and runtime.turns is not None:
            name = runtime.turns.deps.deployment_default.agent_name
            default = next(
                (a.id for a in await listed_agents(runtime, tenant_id) if a.name == name), None
            )
        offered = set(runtime.web.modules)
        return {
            "modules": {m: m in offered and m not in chosen.modules_off for m in _SWITCHABLE},
            "default_agent": default or "",
        }

    @router.get("/settings")
    async def settings(caller: WorkspaceAdmin) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        return await settings_json(caller)

    @router.patch("/settings")
    async def change_settings(body: SettingsChange, caller: WorkspaceAdmin) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        tenant_id, now = caller.person.tenant_id, runtime.now()
        async with runtime.sessionmaker.begin() as db:
            chosen = await workspace_settings.get_settings(db, tenant_id=tenant_id)
            modules_off = set(chosen.modules_off)
            changes: list[tuple[str, str]] = []
            for module, on in (body.modules or {}).items():
                if on == (module in modules_off):
                    (modules_off.discard if on else modules_off.add)(module)
                    changes.append(("Modules", f"{module.title()} {'on' if on else 'off'}"))
            default = chosen.default_agent
            if body.default_agent is not None and body.default_agent != default:
                agent = next(
                    (
                        a
                        for a in await listed_agents(runtime, tenant_id)
                        if a.id == body.default_agent
                    ),
                    None,
                )
                if agent is None:
                    raise HTTPException(
                        status_code=422, detail="Pick an agent from this workspace."
                    )
                default = agent.id
                changes.append(("Default agent", agent.name))
            await workspace_settings.save_settings(
                db,
                tenant_id=tenant_id,
                modules_off=sorted(modules_off),
                default_agent=default,
                now=now,
            )
            for target, detail in changes:
                await audit(
                    db,
                    tenant_id=tenant_id,
                    actor_account_id=caller.person.account_id,
                    action="Changed setting",
                    target=target,
                    detail=detail,
                    at=now,
                )
        return await settings_json(caller)

    return router
