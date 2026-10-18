"""Mocked permission tests for persona-owned Teams credential dialogs."""

from __future__ import annotations

import dataclasses
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

import httpx
import pytest
from cryptography.fernet import Fernet
from daimon.adapters.teams.credential_requests import SUBMIT
from daimon.core.config import McpSettings
from daimon.core.credential_requests import CredentialRequestKind, build_skill_repo_target
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.github_credentials import build_multifernet
from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
from daimon.core.posted_controls.teams_card import CREDENTIAL_DIALOG
from daimon.core.stores.credential_requests import (
    create_credential_request,
    peek_credential_request,
)
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.testing import build_fake_anthropic, ma_agent
from daimon.testing.ma import MARouter
from pydantic import HttpUrl, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    ADMIN_TEAM,
    CHANNEL_ID,
    CONVERSATION_ID,
    ENTRA_TENANT_ID,
    THREAD_ID,
    build_teams_runtime,
    make_invoke,
    post_activity,
    running_service,
)
from .personas import PERSONAS, OwnerGraphFake, Persona, PersonaTeamsApiFake

pytestmark = pytest.mark.usefixtures("entra_env", "stub_bot_token")
TENANT = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID)
AGENT = "agt_persona"
MCP_URL = "https://mcp.example.com/mcp"
Kind = Literal["env_member", "env_admin", "repo", "mcp_oauth"]


def _invoke(
    kind: Literal["open", "submit"], token: str, persona: Persona, *, channel: bool
) -> dict[str, object]:
    if kind == "open":
        data = {"msteams": {"type": "task/fetch"}, "dialog_id": CREDENTIAL_DIALOG, "token": token}
        payload = make_invoke("task/fetch", {"data": data}, user=persona.user_id)
    else:
        data = {"action": SUBMIT, "token": token, "secret": "synthetic-token"}
        payload = make_invoke("task/submit", {"data": data}, user=persona.user_id)
    if channel:
        payload["conversation"] = {
            "id": THREAD_ID,
            "conversationType": "channel",
            "tenantId": ENTRA_TENANT_ID,
        }
        payload["channelData"] = {
            "tenant": {"id": ENTRA_TENANT_ID},
            "channel": {"id": CHANNEL_ID},
            "team": {"id": "19:team@thread.tacv2"},
        }
    if persona.name == "external":
        sender = payload["from"]
        assert isinstance(sender, dict)
        sender["tenantId"] = persona.tenant_id
    return payload


@pytest.mark.parametrize(
    ("requester", "kind", "own_agent"),
    [
        pytest.param(requester, kind, False, id=f"{requester.name}-{kind}")
        for requester in PERSONAS
        for kind in ("env_member", "env_admin", "repo", "mcp_oauth")
    ]
    + [pytest.param(PERSONAS[1], "repo", True, id="member-own-repo")],
)
async def test_credential_request_persona_matrix(
    db_session_factory: async_sessionmaker[AsyncSession],
    requester: Persona,
    kind: Kind,
    own_agent: bool,
) -> None:
    """Mocked permission tests route each requester's own fetch and submit through ingress."""
    await provision_tenant(db_session_factory, platform="teams", workspace_id=ENTRA_TENANT_ID)
    channel = requester.name == "external"
    conversation = THREAD_ID if channel else CONVERSATION_ID
    agent_id = "agt_persona_own" if own_agent else AGENT
    agent_name = "private" if own_agent else "daimon"
    target = {
        "env_member": "API_KEY",
        "env_admin": "APP_MODE",
        "repo": build_skill_repo_target("https://github.com/o/r", "main", ""),
        "mcp_oauth": "linear",
    }[kind]
    request_kind: CredentialRequestKind = (
        "env" if kind.startswith("env_") else "repo" if kind == "repo" else "mcp_oauth"
    )
    async with db_session_factory.begin() as session:
        principal = await get_or_create_platform_principal(
            session, tenant_id=TENANT, platform="teams", external_id=requester.user_id
        )
        row = await create_credential_request(
            session,
            token=uuid.uuid4().hex,
            kind=request_kind,
            tenant_id=TENANT,
            agent_id=derive_agent_uuid(tenant_id=TENANT, ma_agent_id=agent_id),
            account_id=principal.account_id,
            target=target,
            mcp_server_url=MCP_URL if kind == "mcp_oauth" else None,
            requester_platform_user_id=requester.user_id,
            channel_id=conversation,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
            idempotency_key=uuid.uuid4(),
            target_ma_agent_id=agent_id,
            target_name=agent_name,
            requested_work="synthetic follow-up",
            platform="teams",
            origin_thread_id=conversation,
            posted_message_id="m-7",
        )
    router = MARouter()
    agent = ma_agent(id=agent_id, name=agent_name, tenant_id=TENANT)
    router.add_agent_list(agent)
    router.add_agent(agent)
    graph = OwnerGraphFake()
    fake = PersonaTeamsApiFake(active=requester)
    settings = build_teams_runtime(db_session_factory).settings.teams
    assert settings is not None
    settings = settings.model_copy(update={"admin_team_id": ADMIN_TEAM})
    runtime = build_teams_runtime(
        db_session_factory,
        teams=settings,
        anthropic=build_fake_anthropic(router.dispatch),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(graph)),
    )
    runtime.settings.mcp = McpSettings(
        public_url=HttpUrl("https://daimon.example.com/mcp"), jwt_secret=SecretStr("j" * 32)
    )
    runtime = dataclasses.replace(
        runtime,
        turn_deps=dataclasses.replace(
            runtime.turn_deps, fernet=build_multifernet((Fernet.generate_key().decode(),))
        ),
    )
    stranger = PERSONAS[1] if requester.name == "admin" else PERSONAS[0]
    async with running_service(runtime, fake) as service:
        wrong = await post_activity(service, _invoke("open", row.token, stranger, channel=channel))
        opened = await post_activity(
            service, _invoke("open", row.token, requester, channel=channel)
        )
        if kind != "mcp_oauth":
            submitted = await post_activity(
                service, _invoke("submit", row.token, requester, channel=channel)
            )
        else:
            submitted = None
        await service.turns.drain(5)
    async with db_session_factory() as session:
        stored = await peek_credential_request(session, token=row.token)
    assert stored is not None
    assert wrong["task"]["type"] == "message", wrong
    assert "someone else" in json.dumps(wrong).lower(), wrong
    if kind == "mcp_oauth":
        assert opened["task"]["type"] == "continue", opened
        assert "https://daimon.example.com/" in json.dumps(opened), opened
        assert stored.used_at is not None
        return
    permitted = kind == "env_member" or requester.name == "admin"
    if kind == "repo":
        permitted = requester.name == "admin"
    if permitted:
        assert opened["task"]["type"] == "continue", opened
        assert submitted is not None and "cannot be empty" not in json.dumps(submitted), submitted
        if kind.startswith("env_"):
            assert stored.used_at is not None and stored.outcome == "applied", stored
        else:
            assert stored.used_at is None and stored.outcome == "token_rejected", stored
    else:
        assert submitted is not None and "cannot be empty" not in json.dumps(submitted), submitted
        assert submitted["task"]["type"] == "message", submitted
        if kind == "repo":
            assert "needs an admin" in json.dumps(opened), opened
        assert stored.used_at is None
