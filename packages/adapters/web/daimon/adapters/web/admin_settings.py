"""`/api/admin/settings`: the workspace's modules, sign-in methods and default agent."""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from daimon.adapters.web.admin_common import WorkspaceAdmin, listed_agents
from daimon.adapters.web.app_probe import AppStatus
from daimon.adapters.web.apps import app_status, app_token
from daimon.adapters.web.audit import audit
from daimon.adapters.web.caller import WebCaller
from daimon.adapters.web.providers import build_providers
from daimon.adapters.web.runtime import WebRuntime
from daimon.core.config import EmbeddedAppSettings
from daimon.core.stores import web_workspace_settings as workspace_settings
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

_SWITCHABLE: tuple[Literal["chat", "agents", "credits"], ...] = ("chat", "agents", "credits")


class SettingsChange(BaseModel):
    modules: dict[Literal["chat", "agents", "credits"], bool] | None = None
    signin: dict[str, bool] | None = None
    default_agent: str | None = None


def settings_router(runtime: WebRuntime) -> APIRouter:
    router = APIRouter(prefix="/api/admin")

    def apps_json() -> list[dict[str, Any]]:
        return [
            {"key": a.key, "name": a.name, "icon": a.icon, "url": a.origin}
            for a in runtime.web.apps
        ]

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
            "signin": [
                {"id": p.id, "label": p.label, "enabled": p.id not in chosen.signin_off}
                | ({"icon": p.icon} if p.icon else {})
                for p in build_providers(runtime).values()
            ],
            "default_agent": default or "",
            "apps": apps_json(),
        }

    @router.get("/settings")
    async def settings(caller: WorkspaceAdmin) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        return await settings_json(caller)

    @router.get("/apps/status")
    async def apps_status(caller: WorkspaceAdmin) -> dict[str, AppStatus]:  # pyright: ignore[reportUnusedFunction]
        """Whether each embedded app answers, as the caller; reused for a short while."""
        person_id = caller.person.id

        async def one(app: EmbeddedAppSettings) -> AppStatus:
            async def probe() -> AppStatus:
                token = await app_token(runtime, caller, app)
                return await app_status(
                    runtime.http_client, app, token=token and token.access_token
                )

            return await runtime.app_statuses.get(person_id, app.key, probe)

        statuses = await asyncio.gather(*(one(a) for a in runtime.web.apps))
        return {a.key: s for a, s in zip(runtime.web.apps, statuses, strict=True)}

    @router.patch("/settings")
    async def change_settings(body: SettingsChange, caller: WorkspaceAdmin) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        tenant_id, now = caller.person.tenant_id, runtime.now()
        providers = build_providers(runtime)
        async with runtime.sessionmaker.begin() as db:
            chosen = await workspace_settings.get_settings(db, tenant_id=tenant_id)
            modules_off, signin_off = set(chosen.modules_off), set(chosen.signin_off)
            changes: list[tuple[str, str]] = []
            for module, on in (body.modules or {}).items():
                if on == (module in modules_off):
                    (modules_off.discard if on else modules_off.add)(module)
                    changes.append(("Modules", f"{module.title()} {'on' if on else 'off'}"))
            for method, on in (body.signin or {}).items():
                if method not in providers:
                    raise HTTPException(
                        status_code=422, detail="This way of signing in isn't offered."
                    )
                if on == (method in signin_off):
                    (signin_off.discard if on else signin_off.add)(method)
                    changes.append(
                        ("Sign-in", f"{providers[method].label} {'on' if on else 'off'}")
                    )
            if providers and set(providers) <= signin_off:
                raise HTTPException(status_code=422, detail="Keep at least one way to sign in.")
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
                signin_off=sorted(signin_off),
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
