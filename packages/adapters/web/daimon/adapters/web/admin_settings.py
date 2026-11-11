"""`/api/admin/settings`: the workspace's web chat agent, who may use the agents, and providers.

The switches for web modules, direct messages, read-only DM memory and typed mentions are
features; the feature list is their one place."""

from __future__ import annotations

import uuid
from typing import Any, Final, Literal

from daimon.adapters.web.access import is_admin
from daimon.adapters.web.admin_common import WorkspaceAdmin, admin_of, listed_agents, refused
from daimon.adapters.web.audit import audit
from daimon.adapters.web.caller import WebCaller
from daimon.adapters.web.runtime import WebRuntime
from daimon.core import web_admin
from daimon.core import workspace_settings as access_settings
from daimon.core.agent_configuration import configuration_refusal
from daimon.core.authz import Subject
from daimon.core.channel_backend import (
    AgentBackendRefused,
    AgentBackendService,
    BackendActor,
    make_backend_service,
)
from daimon.core.provider_keys import default_enabled_backends
from daimon.core.stores import web_workspace_settings as workspace_settings
from daimon.core.stores.access_policy import PolicyBusyError
from daimon.core.web_admin import WebAdminError
from fastapi import APIRouter, HTTPException
from mux.contracts.ids import Provider
from mux.profiles import PROVIDER_LABELS
from pydantic import BaseModel

_PROVIDERS: Final[tuple[tuple[Provider, str], ...]] = tuple(PROVIDER_LABELS.items())
_PROVIDER_LABEL: Final = PROVIDER_LABELS
_BUSY = "Settings are being changed. Try again."


class InvokersChange(BaseModel):
    """Who may start turns: everyone, or exactly these people."""

    everyone: bool = False
    people: list[uuid.UUID] = []


class SettingsChange(BaseModel):
    default_agent: str | None = None
    invokers: InvokersChange | None = None
    providers: list[Provider] | Literal["default"] | None = None


def settings_router(runtime: WebRuntime) -> APIRouter:
    router = APIRouter(prefix="/api/admin")

    def backends() -> AgentBackendService:
        return make_backend_service(runtime.sessionmaker, runtime.settings.turn)

    async def providers_json(tenant_id: uuid.UUID) -> dict[str, Any] | None:
        service = backends()
        if not service.enabled:
            return None
        policy = await service.tenant_policy(tenant_id)
        keyed = {*default_enabled_backends(), *policy.enabled_backends}
        return {
            "offered": [{"id": p, "label": label} for p, label in _PROVIDERS if p in keyed],
            "allowed": list(policy.enabled_backends),
            "followed": not policy.explicit,
        }

    async def settings_json(caller: WebCaller) -> dict[str, Any]:
        tenant_id = caller.person.tenant_id
        async with runtime.sessionmaker() as db:
            chosen = await workspace_settings.get_settings(db, tenant_id=tenant_id)
            invokers = await access_settings.load_invoker_people(db, tenant_id=tenant_id)
        default = chosen.default_agent
        if default is None and runtime.turns is not None:
            name = runtime.turns.deps.deployment_default.agent_name
            default = next(
                (a.id for a in await listed_agents(runtime, tenant_id) if a.name == name), None
            )
        return {
            "default_agent": default or "",
            "access": {
                "people": (
                    [{"id": str(p.id), "name": p.display_name} for p in invokers.people]
                    if invokers.limited
                    else None
                ),
                "others": invokers.others,
            },
            "providers": await providers_json(tenant_id),
        }

    @router.get("/settings")
    async def settings(caller: WorkspaceAdmin) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        return await settings_json(caller)

    @router.patch("/settings")
    async def change_settings(body: SettingsChange, caller: WorkspaceAdmin) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        tenant_id = caller.person.tenant_id
        subject = Subject(
            is_admin=is_admin(caller.role), platform_user_id=str(caller.person.account_id)
        )
        agents = {a.id: a.name for a in await listed_agents(runtime, tenant_id)}
        invokers = body.invokers
        try:
            await web_admin.change_settings(
                runtime.sessionmaker,
                admin_of(caller),
                runtime.now(),
                agents=agents,
                default_agent=body.default_agent,
                everyone_invokes=None if invokers is None else invokers.everyone,
                invoker_people=[] if invokers is None else invokers.people,
            )
        except WebAdminError as error:
            raise refused(error) from error
        if body.providers is not None:
            await set_providers(caller, subject, body.providers)
        return await settings_json(caller)

    async def set_providers(
        caller: WebCaller, subject: Subject, wanted: list[Provider] | Literal["default"]
    ) -> None:
        """The provider list, through the service that audits it as a security event.

        The service runs its own transaction, so the web audit row follows it.
        """
        tenant_id, account_id = caller.person.tenant_id, caller.person.account_id
        service = backends()
        if not service.enabled:
            raise HTTPException(status_code=422, detail=configuration_refusal("feature_disabled"))
        current = await service.tenant_policy(tenant_id)
        listed = None if wanted == "default" else tuple(dict.fromkeys(wanted))
        if listed == (current.enabled_backends if current.explicit else None):
            return
        actor = BackendActor(tenant_id=tenant_id, account_id=account_id, subject=subject)
        try:
            await service.set_tenant_allowlist(tenant_id, listed, current.revision, actor)
        except PolicyBusyError as exc:
            raise HTTPException(status_code=409, detail=_BUSY) from exc
        except AgentBackendRefused as exc:
            raise HTTPException(status_code=422, detail=configuration_refusal(exc.reason)) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail="Anthropic stays on. Nothing changed."
            ) from exc
        detail = (
            "Use the default providers"
            if listed is None
            else ", ".join(_PROVIDER_LABEL[p] for p in listed)
        )
        async with runtime.sessionmaker.begin() as db:
            await audit(
                db,
                tenant_id=tenant_id,
                actor_account_id=account_id,
                action="Changed setting",
                target="Providers",
                detail=detail,
                at=runtime.now(),
            )

    return router
