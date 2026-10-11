"""Real submit + real loopback rejection + scoped PostgreSQL readback."""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import hashlib
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import httpx
import pytest
from daimon.adapters.discord import credential_modals
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.stores.domain import CredentialRequestRow
from daimon.testing.db import require_owned_sessionmaker
from daimon.testing.discord_credential_conformance import (
    SOURCE_ASSERTION,
    SOURCE_SHA256,
    LoopbackRejectingProbe,
    RejectedTokenReceipt,
    _LoopbackTransport,  # pyright: ignore[reportPrivateUsage]
    read_used_at,
    rejected_token_submit,
)
from daimon.testing.ma import build_stub_anthropic
from pydantic import HttpUrl
from sqlalchemy import Column, MetaData, String, Table
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from packages.adapters.discord.tests import test_credential_modals as fixtures

ORIGIN = "https://reject.qa.example/mcp"
WRONG_TOKEN = "wrong-token-123"


@dataclasses.dataclass
class OwnedSubmit:
    runtime: DiscordRuntime
    row: CredentialRequestRow
    probe: LoopbackRejectingProbe
    creds: list[dict[str, Any]]
    updates: list[dict[str, Any]]


@pytest.fixture
async def owned_submit(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[OwnedSubmit]:
    try:
        require_owned_sessionmaker(db_session_factory)
    except ValueError as exc:
        pytest.skip(f"offline hook requires isolated test DB: {exc}")
    row = await fixtures._seed_mcp_request(  # pyright: ignore[reportPrivateUsage]
        db_session_factory, mcp_server_url=ORIGIN, with_origin=True
    )
    creds: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []
    client = build_stub_anthropic(
        fixtures._vault_handler(  # pyright: ignore[reportPrivateUsage]
            "vlt_c2", "owned fixture", creds, tenant_id=str(row.tenant_id), agent_updates=updates
        )
    )
    async with LoopbackRejectingProbe(ORIGIN) as probe:
        runtime = dataclasses.replace(
            fixtures._runtime(  # pyright: ignore[reportPrivateUsage]
                sessionmaker=db_session_factory,
                anthropic=client,
                public_url=HttpUrl("https://mcp.example.com/mcp"),
                jwt_secret="x" * 32,
            ),
            mcp_token_probe=probe,
        )
        yield OwnedSubmit(runtime, row, probe, creds, updates)
    await client.close()


async def execute(owned: OwnedSubmit) -> RejectedTokenReceipt:
    interaction = fixtures._card_interaction()  # pyright: ignore[reportPrivateUsage]
    interaction.user = MagicMock(spec=discord.Member)
    interaction.user.id = int(owned.row.requester_platform_user_id)
    interaction.user.guild_permissions.administrator = True
    interaction.user.guild_permissions.manage_guild = False
    interaction.guild.owner_id = 999
    return await rejected_token_submit(
        runtime=owned.runtime,
        row=owned.row,
        interaction=interaction,
        token_value=WRONG_TOKEN,
        probe=owned.probe,
    )


async def test_actual_rejected_submit_observes_retry_release(
    owned_submit: OwnedSubmit,
) -> None:
    receipt = await execute(owned_submit)
    assert receipt.before.unused and receipt.before.used_at is None
    assert not receipt.at_probe.unused and receipt.at_probe.used_at is not None
    assert receipt.after.unused and receipt.after.used_at is None
    assert receipt.after.outcome == "token_rejected"
    assert receipt.unused_assertion is True, "observed product retry release"
    assert receipt.probe_status == owned_submit.probe.results[0].status_code == 403
    assert len(owned_submit.probe.posts) == 1
    assert owned_submit.probe.posts[0].method == "initialize"
    assert (
        owned_submit.probe.posts[0].token_sha256 == hashlib.sha256(WRONG_TOKEN.encode()).hexdigest()
    )
    assert owned_submit.creds == owned_submit.updates == []
    encoded = receipt.model_dump_json()
    print("C2_REAL_RECEIPT=" + encoded)
    assert WRONG_TOKEN not in encoded and owned_submit.row.token not in encoded
    assert RejectedTokenReceipt.model_validate_json(encoded) == receipt


async def test_mutation_skip_real_consume_changes_actual_assertion_to_true(
    owned_submit: OwnedSubmit, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def leave_unused(*args: object, **kwargs: object) -> CredentialRequestRow:
        return owned_submit.row

    monkeypatch.setattr(credential_modals, "consume_credential_submit", leave_unused)
    receipt = await execute(owned_submit)
    assert receipt.probe_status == 403
    assert receipt.at_probe.used_at is receipt.after.used_at is None
    assert receipt.unused_assertion is True, "hook observes the unconsumed row"


async def test_mutation_remove_real_release_changes_actual_assertion_to_false(
    owned_submit: OwnedSubmit, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        credential_modals.credential_requests,
        "release_failed_credential_request",
        AsyncMock(return_value=None),
    )
    receipt = await execute(owned_submit)
    assert receipt.before.unused and receipt.before.used_at is None
    assert receipt.probe_status == 403
    assert len(owned_submit.probe.posts) == 1
    assert receipt.at_probe.used_at is not None
    assert receipt.after.used_at == receipt.at_probe.used_at
    assert receipt.after.outcome == "token_rejected"
    assert receipt.unused_assertion is False
    assert owned_submit.creds == owned_submit.updates == []


async def test_mutation_remove_real_submit_cannot_certify_a_verdict(
    owned_submit: OwnedSubmit, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(credential_modals.McpCredentialModal, "on_submit", AsyncMock())
    with pytest.raises(ValueError, match="did not reach"):
        await execute(owned_submit)
    assert owned_submit.probe.posts == []
    assert owned_submit.probe.sample is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("token", "' OR 1=1 --"),
        ("tenant_id", uuid.UUID(int=1)),
        ("account_id", uuid.UUID(int=2)),
        ("agent_id", uuid.UUID(int=3)),
        ("requester_platform_user_id", "foreign-requester"),
        ("mcp_server_url", "https://foreign.qa.example/mcp"),
        ("target_ma_agent_id", "foreign-agent"),
    ],
)
async def test_parameterized_readback_refuses_unowned_rows(
    owned_submit: OwnedSubmit, field: str, value: object
) -> None:
    foreign = owned_submit.row.model_copy(update={field: value})
    with pytest.raises(ValueError, match="genuinely scoped"):
        await read_used_at(owned_submit.runtime.sessionmaker, foreign)
    assert (await read_used_at(owned_submit.runtime.sessionmaker, owned_submit.row)).unused


async def test_concurrent_submit_cannot_share_one_probe(
    owned_submit: OwnedSubmit, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    original = credential_modals.McpCredentialModal.on_submit

    async def held(self: credential_modals.McpCredentialModal, interaction: Any) -> None:
        entered.set()
        await release.wait()
        await original(self, interaction)

    monkeypatch.setattr(credential_modals.McpCredentialModal, "on_submit", held)
    first = asyncio.create_task(execute(owned_submit))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        with pytest.raises(ValueError, match="fresh owned"):
            await execute(owned_submit)
    finally:
        release.set()
        await first
    assert len(owned_submit.probe.posts) == 1


async def test_unbound_http_request_is_refused_without_connecting() -> None:
    transport = _LoopbackTransport(ORIGIN, "http://127.0.0.1:1/mcp")
    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        with pytest.raises(ValueError, match="unbound"):
            await client.post("https://foreign.qa.example/mcp")
        with pytest.raises(ValueError, match="unbound"):
            await client.get(ORIGIN)


async def test_guard_rejects_routed_unowned_bind_before_connection(
    owned_submit: OwnedSubmit,
) -> None:
    engine = create_async_engine("postgresql+asyncpg://unused:unused@localhost:1/not_a_test")
    route = Table("credential_requests", MetaData(), Column("token", String, primary_key=True))
    factory = async_sessionmaker(
        owned_submit.runtime.sessionmaker.kw["bind"], binds={route: engine}
    )
    try:
        with pytest.raises(ValueError):
            await read_used_at(factory, owned_submit.row)
    finally:
        await engine.dispose()


def test_binding_pin_keeps_original_predicate_and_real_primary_key() -> None:
    assert SOURCE_ASSERTION == (
        "select used_at is null from credential_requests where id = '{headless.request_id}'"
    )
    assert SOURCE_SHA256 == "5a62a3ded200f694869a1e5c9b436587f7844cc09743272d58d6b9ed72ff72fd"
    source = Path(__file__).resolve().parents[4] / "packages/core/daimon/core/_models.py"
    tree = ast.parse(source.read_text())
    model = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "CredentialRequest"
    )
    fields = {
        node.target.id: node
        for node in model.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }
    assert "id" not in fields
    token = fields["token"].value
    assert isinstance(token, ast.Call)
    assert any(
        arg.arg == "primary_key" and isinstance(arg.value, ast.Constant) and arg.value.value is True
        for arg in token.keywords
    )
