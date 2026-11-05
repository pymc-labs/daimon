"""`/api/agents/{id}/keys` and `/coding-keys`: an agent's stored key names and coding keys.

A key's value goes in and never comes back, in an answer or the audit trail. A coding
key's token is in the answer to the request that made it, and nowhere after. A pasted
prompt carries a single-use code instead, which the MCP adapter trades for the key.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from daimon.adapters.web.agent_access import Editor, Viewer, actor_of, change_deps, edit_refusal
from daimon.adapters.web.agent_audit import recorded
from daimon.adapters.web.runtime import WebRuntime
from daimon.core import agent_keys, coding_key_connect, coding_keys
from daimon.core.agent_editing import AgentEditError
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, SecretStr

_NOT_SET_UP = "Coding keys aren't set up on this install."


class KeyDoc(BaseModel):
    key: str
    updated_at: datetime


class KeyBody(BaseModel):
    value: SecretStr


class Removed(BaseModel):
    removed: bool


class CodingKeyDoc(BaseModel):
    id: str
    creator: str
    created_at: datetime
    expires_at: datetime | None
    mine: bool
    client: str | None


class MintedKeyDoc(BaseModel):
    id: str
    token: str
    url: str
    expires_at: datetime


class ConnectPromptDoc(BaseModel):
    id: str
    name: str
    prompt: str


class ConnectBody(BaseModel):
    replaces: uuid.UUID | None = None


class ConnectBatchDoc(BaseModel):
    batch: str
    expires_at: datetime
    prompts: list[ConnectPromptDoc]


class ConnectStatusDoc(BaseModel):
    connected: bool
    client: str | None


def agent_keys_router(runtime: WebRuntime) -> APIRouter:
    router = APIRouter(prefix="/api/agents")

    @router.get("/{agent_id}/keys")
    async def list_keys(caller: Viewer, agent_id: str) -> list[KeyDoc]:  # pyright: ignore[reportUnusedFunction]
        try:
            rows = await agent_keys.list_key_names(
                change_deps(runtime).edit, actor=actor_of(caller), ma_agent_id=agent_id
            )
        except AgentEditError as exc:
            raise edit_refusal(exc) from exc
        return [KeyDoc(key=r.key, updated_at=r.updated_at) for r in rows]

    @router.put("/{agent_id}/keys/{key}")
    async def set_key(caller: Editor, agent_id: str, key: str, body: KeyBody) -> KeyDoc:  # pyright: ignore[reportUnusedFunction]
        try:
            row = await agent_keys.set_key(
                change_deps(runtime).edit,
                actor=actor_of(caller),
                ma_agent_id=agent_id,
                key=key,
                value=body.value.get_secret_value(),
                audited=recorded(runtime, caller, "Set agent key", key),
            )
        except AgentEditError as exc:
            raise edit_refusal(exc) from exc
        return KeyDoc(key=row.key, updated_at=row.updated_at)

    @router.delete("/{agent_id}/keys/{key}")
    async def remove_key(caller: Editor, agent_id: str, key: str) -> Removed:  # pyright: ignore[reportUnusedFunction]
        try:
            removed = await agent_keys.remove_key(
                change_deps(runtime).edit,
                actor=actor_of(caller),
                ma_agent_id=agent_id,
                key=key,
                audited=recorded(runtime, caller, "Removed agent key", key),
            )
        except AgentEditError as exc:
            raise edit_refusal(exc) from exc
        return Removed(removed=removed)

    @router.get("/{agent_id}/coding-keys")
    async def list_coding(caller: Viewer, agent_id: str) -> list[CodingKeyDoc]:  # pyright: ignore[reportUnusedFunction]
        try:
            rows = await coding_keys.list_agent_coding_keys(
                change_deps(runtime).edit,
                actor=actor_of(caller),
                ma_agent_id=agent_id,
                now=runtime.now(),
            )
        except AgentEditError as exc:
            raise edit_refusal(exc) from exc
        return [
            CodingKeyDoc(
                id=str(r.jti),
                creator=r.creator,
                created_at=r.created_at,
                expires_at=r.expires_at,
                mine=r.mine,
                client=r.client,
            )
            for r in rows
        ]

    @router.post("/{agent_id}/coding-keys")
    async def mint_coding(caller: Editor, agent_id: str) -> MintedKeyDoc:  # pyright: ignore[reportUnusedFunction]
        public_url = runtime.settings.mcp.public_url
        secret = runtime.settings.mcp.jwt_secret
        if public_url is None or secret is None:
            raise HTTPException(status_code=503, detail=_NOT_SET_UP)
        try:
            minted = await coding_keys.mint_coding_key(
                change_deps(runtime).edit,
                actor=actor_of(caller),
                ma_agent_id=agent_id,
                public_url=str(public_url),
                secret=secret.get_secret_value().encode(),
                now=runtime.now(),
                audited=recorded(runtime, caller, "Created coding key"),
            )
        except AgentEditError as exc:
            raise edit_refusal(exc) from exc
        return MintedKeyDoc(
            id=str(minted.jti),
            token=minted.token,
            url=minted.url,
            expires_at=minted.expires_at,
        )

    @router.post("/{agent_id}/coding-keys/connect")
    async def connect_coding(  # pyright: ignore[reportUnusedFunction]
        caller: Editor, agent_id: str, body: ConnectBody | None = None
    ) -> ConnectBatchDoc:
        app_root = runtime.settings.mcp.app_root_url
        if app_root is None:
            raise HTTPException(status_code=503, detail=_NOT_SET_UP)
        try:
            batch = await coding_key_connect.mint_connect_batch(
                change_deps(runtime).edit,
                actor=actor_of(caller),
                ma_agent_id=agent_id,
                claim_url=f"{app_root.rstrip('/')}/coding-keys/claim",
                now=runtime.now(),
                replaces=body.replaces if body is not None else None,
            )
        except AgentEditError as exc:
            raise edit_refusal(exc) from exc
        return ConnectBatchDoc(
            batch=str(batch.batch),
            expires_at=batch.expires_at,
            prompts=[
                ConnectPromptDoc(id=p.id, name=p.name, prompt=p.prompt) for p in batch.prompts
            ],
        )

    @router.get("/{agent_id}/coding-keys/connect/{batch}")
    async def connect_status(  # pyright: ignore[reportUnusedFunction]
        caller: Editor, agent_id: str, batch: uuid.UUID
    ) -> ConnectStatusDoc:
        try:
            status = await coding_key_connect.connect_status(
                change_deps(runtime).edit,
                actor=actor_of(caller),
                ma_agent_id=agent_id,
                batch=batch,
            )
        except AgentEditError as exc:
            raise edit_refusal(exc) from exc
        return ConnectStatusDoc(connected=status.connected, client=status.client)

    @router.delete("/{agent_id}/coding-keys/{key_id}", status_code=204)
    async def revoke_coding(caller: Editor, agent_id: str, key_id: uuid.UUID) -> None:  # pyright: ignore[reportUnusedFunction]
        try:
            await coding_keys.revoke_agent_coding_key(
                change_deps(runtime).edit,
                actor=actor_of(caller),
                ma_agent_id=agent_id,
                jti=key_id,
                now=runtime.now(),
                audited=recorded(runtime, caller, "Revoked coding key"),
            )
        except AgentEditError as exc:
            raise edit_refusal(exc) from exc

    return router
