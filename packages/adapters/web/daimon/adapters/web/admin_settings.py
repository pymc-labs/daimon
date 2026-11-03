"""`/api/admin/settings`: the workspace's modules, web chat agent, access and providers."""

from __future__ import annotations

import uuid
from typing import Any, Final, Literal

from daimon.adapters.web.access import is_admin
from daimon.adapters.web.admin_common import WorkspaceAdmin, listed_agents
from daimon.adapters.web.audit import audit
from daimon.adapters.web.caller import WebCaller
from daimon.adapters.web.runtime import WebRuntime
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
from daimon.core.stores import tenants
from daimon.core.stores import web_workspace_settings as workspace_settings
from daimon.core.stores.access_policy import PolicyBusyError
from daimon.core.workspace_settings import SettingRefused
from fastapi import APIRouter, HTTPException
from mux.contracts.ids import Provider
from pydantic import BaseModel

_SWITCHABLE: tuple[
    Literal["chat", "agents", "channels", "routines", "credits", "integrations"], ...
] = (
    "chat",
    "agents",
    "channels",
    "routines",
    "credits",
    "integrations",
)
_PROVIDERS: Final[tuple[tuple[Provider, str], ...]] = (
    ("anthropic", "Anthropic"),
    ("openai", "OpenAI"),
    ("gemini", "Google"),
    ("selfhosted", "Self-hosted"),
)
_PROVIDER_LABEL: Final = dict(_PROVIDERS)
_BUSY = "Settings are being changed. Try again."


class InvokersChange(BaseModel):
    """Who may start turns: everyone, or exactly these people."""

    everyone: bool = False
    people: list[uuid.UUID] = []


class SettingsChange(BaseModel):
    modules: (
        dict[Literal["chat", "agents", "channels", "routines", "credits", "integrations"], bool]
        | None
    ) = None
    default_agent: str | None = None
    invokers: InvokersChange | None = None
    dm_enabled: bool | None = None
    dm_memory_read_only: bool | None = None
    plain_text_mentions: bool | None = None
    providers: list[Provider] | Literal["default"] | None = None


def _on(value: bool) -> str:
    return "On" if value else "Off"


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
            tenant = await tenants.get_tenant(db, tenant_id)
            access = await access_settings.load_access(db, tenant_id=tenant_id)
            invokers = await access_settings.load_invoker_people(db, tenant_id=tenant_id)
        platform = tenant.platform if tenant else ""
        has_dms = platform in access_settings.DM_PLATFORMS
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
            "access": {
                "people": (
                    [{"id": str(p.id), "name": p.display_name} for p in invokers.people]
                    if invokers.limited
                    else None
                ),
                "others": invokers.others,
                "dm_enabled": access.dm_enabled if has_dms else None,
                "dm_memory_read_only": access.dm_memory_read_only if has_dms else None,
                "plain_text_mentions": (
                    access.plain_text_mentions
                    if platform in access_settings.MENTION_PLATFORMS
                    else None
                ),
            },
            "providers": await providers_json(tenant_id),
        }

    @router.get("/settings")
    async def settings(caller: WorkspaceAdmin) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        return await settings_json(caller)

    @router.patch("/settings")
    async def change_settings(body: SettingsChange, caller: WorkspaceAdmin) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        tenant_id, now = caller.person.tenant_id, runtime.now()
        account_id = caller.person.account_id
        subject = Subject(is_admin=is_admin(caller.role), platform_user_id=str(account_id))
        try:
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
                    changes.append(("Web chat agent", agent.name))
                await workspace_settings.save_settings(
                    db,
                    tenant_id=tenant_id,
                    modules_off=sorted(modules_off),
                    default_agent=default,
                    now=now,
                )
                tenant = await tenants.get_tenant(db, tenant_id)
                platform = tenant.platform if tenant else ""
                if body.invokers is not None:
                    people = None if body.invokers.everyone else body.invokers.people
                    named = await access_settings.set_invoker_people(
                        db, tenant_id=tenant_id, subject=subject, person_ids=people
                    )
                    changes.append(
                        (
                            "Who can use the agents",
                            f"{len(named.people)} people" if named.limited else "Everyone",
                        )
                    )
                if body.dm_enabled is not None:
                    await access_settings.set_dms(
                        db,
                        tenant_id=tenant_id,
                        subject=subject,
                        platform=platform,
                        enabled=body.dm_enabled,
                    )
                    changes.append(("Direct messages", _on(body.dm_enabled)))
                if body.dm_memory_read_only is not None:
                    await access_settings.set_dm_memory_read_only(
                        db, tenant_id=tenant_id, subject=subject, enabled=body.dm_memory_read_only
                    )
                    changes.append(
                        ("Read-only memory in direct messages", _on(body.dm_memory_read_only))
                    )
                if body.plain_text_mentions is not None:
                    await access_settings.set_plain_text_mentions(
                        db,
                        tenant_id=tenant_id,
                        subject=subject,
                        platform=platform,
                        enabled=body.plain_text_mentions,
                    )
                    changes.append(("Typed mentions", _on(body.plain_text_mentions)))
                for target, detail in changes:
                    await audit(
                        db,
                        tenant_id=tenant_id,
                        actor_account_id=account_id,
                        action="Changed setting",
                        target=target,
                        detail=detail,
                        at=now,
                    )
        except SettingRefused as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except PolicyBusyError as exc:
            raise HTTPException(status_code=409, detail=_BUSY) from exc
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
