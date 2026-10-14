"""C6 product entrypoints over actual SDK mutations and owned Postgres readback."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import uuid
import zipfile
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from daimon.core.config import Settings
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.skills.add import add_agent_skill
from daimon.core.skills.ingest import bundle_from_files, bundle_from_upload
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.scoped_config_read import list_propagations_for_tenant
from daimon.testing.admin_bindings import AdminBindings, provision_admin_run
from daimon.testing.anthropic_admin import AgentFixture, AnthropicAdminBindings, MemberAgentEdit
from daimon.testing.db import require_owned_sessionmaker
from daimon.testing.ma import FakeMAState, MARouter, list_response, make_fake_ma_handler
from daimon.testing.ma_transport import ScriptedTransport
from mux.contracts.ids import Scope
from mux.errors import ScopeViolation
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

pytest_plugins = ["daimon.testing.db"]
WORKSPACE, CHANNEL = "123456789012345678", "223456789012345678"
UNBOUND = """# Templated by the runner ({nonce}); see PROPOSED-KINDS.md P2.
name: qa-unbound-c6
model: claude-haiku-4-5-20251001
description: qa-original
system: QA fixture agent. Never bound to a channel. Archived at teardown.
"""
SOURCE = """name: qa-base
model: claude-haiku-4-5-20251001
description: source-original
system: QA fixture source agent.
"""
BUNDLE = bundle_from_files(
    {
        "SKILL.md": b"---\nname: qa-echo-skill\ndescription: Echo\n---\nEcho QA.\n",
        "reference.md": b"Preserve this reference byte-for-byte.\n",
    }
)
FROZEN = Path(__file__).resolve().parents[3] / "tests/judge/fixtures/admin_operations.json"


def frozen(scenario_id: str) -> dict[str, Any]:
    return next(
        row
        for row in json.loads(FROZEN.read_text())["scenarios"]
        if row["scenario_id"] == scenario_id
    )


class Server:
    """State changes only through actual SDK requests, not authored row injection."""

    def __init__(self) -> None:
        self.state = FakeMAState()
        self.agent_handler = make_fake_ma_handler(self.state)
        self.skills: list[dict[str, Any]] = []
        self.uploaded: dict[str, bytes] = {}
        self.router = MARouter()
        self.router.add("GET", r"/v1/skills", lambda req, m: list_response(self.skills))
        self.router.add("POST", r"/v1/skills", self.upload)
        self.router.add(
            "GET",
            r"/v1/skills/(?P<id>[^/]+)/versions",
            lambda req, m: httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "version_1",
                            "version": "1",
                            "skill_id": m["id"],
                            "type": "skill_version",
                            "created_at": "2026-10-11T00:00:00Z",
                            "name": BUNDLE.preview.name,
                            "directory": BUNDLE.preview.name,
                            "description": BUNDLE.preview.description,
                        }
                    ],
                    "next_page": None,
                },
            ),
        )
        self.router.add(
            "GET",
            r"/v1/skills/(?P<id>[^/]+)/versions/version_1/content",
            lambda req, m: httpx.Response(200, content=self.uploaded[m["id"]]),
        )
        self.router.add("POST", r"/v1/agents/(?P<id>[^/]+)/archive", self.archive)
        for method in ["GET", "POST"]:
            self.router.add(method, r"/v1/agents(?:/[^/]+)?", self.agents)

    def agents(self, request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        response = self.agent_handler(request)
        if request.method == "POST" and response.status_code == 200:
            # The shared CRUD fake echoes request references. Native responses
            # resolve unpinned custom skills to their uploaded concrete version.
            body, row = json.loads(request.content), response.json()
            if request.url.path == "/v1/agents":
                row["description"] = body.get("description")
            for skill in row.get("skills", []):
                if skill["type"] == "custom" and skill.get("version") is None:
                    uploaded = next(s for s in self.skills if s["id"] == skill["skill_id"])
                    skill["version"] = uploaded["latest_version"]
            self.state.agents[row["id"]] = row
            self.state.agent_versions[row["id"]][row["version"]] = dict(row)
            return httpx.Response(200, json=row)
        return response

    def upload(self, request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        from email.parser import BytesParser
        from email.policy import default

        message = BytesParser(policy=default).parsebytes(
            b"Content-Type: "
            + request.headers["content-type"].encode()
            + b"\r\n\r\n"
            + request.content
        )
        title = None
        files: dict[str, bytes] = {}
        for part in message.iter_parts():
            if part.get_param("name", header="content-disposition") == "display_title":
                title = part.get_payload(decode=True).decode()
            elif part.get_filename() is not None:
                body = part.get_payload(decode=True)
                assert isinstance(body, bytes)
                files[part.get_filename()] = body
        assert isinstance(title, str) and files
        buffer = io.BytesIO()
        if len(files) == 1 and next(iter(files)).endswith(".zip"):
            # SDK receives the actual bundle archive as one multipart file.
            buffer.write(next(iter(files.values())))
        else:
            with zipfile.ZipFile(buffer, "w") as archive:
                for name, body in sorted(files.items()):
                    archive.writestr(zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0)), body)
        sid = f"skill_{len(self.skills)}"
        row = {
            "id": sid,
            "source": "custom",
            "display_title": title,
            "latest_version": "1",
            "created_at": "2026-10-11T00:00:00Z",
        }
        self.skills.append(row)
        self.uploaded[sid] = buffer.getvalue()
        return httpx.Response(200, json={**row, "type": "skill", "updated_at": row["created_at"]})

    def archive(self, request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        row = self.state.agents[match["id"]]
        row["archived_at"] = "2026-10-11T00:00:00Z"
        # Actual list excludes archived rows just as provider include_archived=false.
        self.state.agents.pop(match["id"])
        return httpx.Response(200, json=row)


@dataclass
class Case:
    binding: AdminBindings
    edge: AnthropicAdminBindings
    member: Scope
    server: Server

    async def create(self, name: str = "qa-base") -> None:
        label = "fixtures/qa-base.yaml" if name == "qa-base" else "fixtures/qa-unbound-agent.yaml"
        receipt = await self.binding.execute(
            {
                "do": "admin",
                "tool": "cli",
                "args": f"daimon agents --guild {{guild_id}} create {label}",
            },
            {},
        )
        assert receipt.status == "executed"


@pytest.fixture
async def case(db_engine: AsyncEngine, db_clean: None, tmp_path: Path) -> AsyncIterator[Case]:
    from daimon.adapters.cli.runtime import CliRuntime

    sessions = async_sessionmaker(db_engine, expire_on_commit=False)
    try:
        require_owned_sessionmaker(sessions)
    except ValueError:
        if (
            db_engine.url.database == "daimon_test"
            and db_engine.url.host in ("localhost", "127.0.0.1")
            and not db_engine.url.query
            and not (os.environ.get("GITHUB_ACTIONS") == "true" and os.environ.get("CI") == "true")
        ):
            pytest.skip("C6 proofs require owned lane DB or registered isolated CI schema")
        raise
    server = Server()
    binding = await provision_admin_run(
        sessions,
        workspace_id=WORKSPACE,
        backend="anthropic",
        channels=frozenset({CHANNEL}),
        trace=ScriptedTransport(router=server.router),
    )
    async with sessions() as session, session.begin():
        member = await get_or_create_platform_principal(
            session,
            tenant_id=uuid.UUID(binding.scope.tenant_id),
            platform="discord",
            external_id="423456789012345678",
        )
    scope = Scope(
        tenant_id=binding.scope.tenant_id,
        account_id=str(member.account_id),
        principal_id=str(member.id),
        authorization_id="qa-c6-member",
    )
    settings = Settings(
        _env_file=None,
        _env_prefix="N6_C6_UNUSED_",
        database={"url": "postgresql+asyncpg://offline:fixture@localhost/daimon_test_nc_n6"},
        anthropic={"api_key": "offline-fixture"},
        cli={"local_user": "qa-c6"},
    )
    runtime = CliRuntime(
        settings, binding.anthropic, sessions, binding.default, new_resolver_cache()
    )
    fixtures = {}
    for label, content in [
        ("fixtures/qa-unbound-agent.yaml", UNBOUND),
        ("fixtures/qa-base.yaml", SOURCE),
    ]:
        path = tmp_path / Path(label).name
        path.write_text(content)
        fixtures[label] = AgentFixture(path, hashlib.sha256(path.read_bytes()).hexdigest())
    edge = AnthropicAdminBindings(binding, runtime, tmp_path, fixtures)
    binding.resource_edge = edge
    try:
        yield Case(binding, edge, scope, server)
    finally:
        from daimon.core.agent_identity import cancel_pending_agent_faces

        cancel_pending_agent_faces()
        await binding.close()


async def test_d5_real_member_edit_and_independent_revision_readback(case: Case) -> None:
    row = frozen("QA-D5-UNBOUND-AGENT-EDIT-REFUSED")
    created = await case.binding.execute(row["operations"][0]["source"], {})
    assert created.status == "executed"
    before = await case.edge.readback_cli(
        "daimon agents --guild {guild_id} get qa-unbound-{nonce}", {"nonce": "c6"}
    )
    assert before.payload["description"] == "qa-original"
    start = len(case.binding.trace.requests)
    receipt = await case.edge.attempt_member_edit(MemberAgentEdit(case.member, "qa-unbound-c6"))
    # Main #505 (bfaffd2a) now refuses unbound-agent mutations. Observe the
    # actual member path; neither an identity refusal nor an operator edit
    # establishes this denial, and it is not a whole-scenario PASS.
    assert receipt.observations["before"] == before.model_dump(mode="json")
    after = await case.edge.read_agent("qa-unbound-c6")
    assert receipt.observations["after"] == after.model_dump(mode="json")
    assert receipt.observations["unchanged"] == (before.payload == after.payload)
    assert receipt.observations["revision_unchanged"] == (before.version == after.version)
    assert any(
        r.method == "GET" and r.path.endswith(before.native_id)
        for r in case.binding.trace.requests[start:]
    )
    assert "Which current agent" not in str(receipt.observations["errors"])
    updates = [
        r.json()
        for r in case.binding.trace.requests[start:]
        if r.method == "POST" and r.path == f"/v1/agents/{before.native_id}"
    ]
    assert updates == []
    case.binding.trace.assert_consumed()
    assert receipt.status == "refused" and receipt.code == "ADMIN_MEMBER_EDIT_REFUSED"
    expected_error = (
        "'qa-unbound-c6' is not limited by a channel rule exclusively to channels this caller "
        "administers, so an admin must change its setup. Tell the caller to ask a workspace "
        "or server admin to make the requested change to 'qa-unbound-c6'; carry the specific "
        "action from the conversation into that handoff. Do not retry. An admin can provision "
        "a channel-restricted agent that you administer."
    )
    assert receipt.observations["errors"] == [
        {"operation": operation, "error": expected_error}
        for operation in ("update_agent", "attach_mcp_server")
    ]
    assert (before.version, after.version) == (1, 1)
    assert before.payload == after.payload and before.cli_json == after.cli_json
    assert receipt.observations["unchanged"] is True
    assert receipt.observations["revision_unchanged"] is True
    print("D5 actual member edit: refused; revision 1 -> 1; unchanged=true; zero native POSTs")
    row = frozen("QA-D5-UNBOUND-AGENT-EDIT-REFUSED")
    archived = await case.binding.execute(row["operations"][1]["source"], {"nonce": "c6"})
    assert archived.status == "executed" and archived.observations["archived"] is True


async def test_d14_literal_cli_fork_preserves_own_skill_and_model(case: Case) -> None:
    await case.create()
    source = await find_agent_by_daimon_tag(
        case.binding.anthropic, tenant_id=uuid.UUID(case.binding.scope.tenant_id), name="qa-base"
    )
    assert source is not None

    async def authorized(agent: object) -> None:
        pass

    await add_agent_skill(
        case.binding.anthropic,
        case.binding.sessions,
        tenant_id=uuid.UUID(case.binding.scope.tenant_id),
        agent=source,
        agent_name="qa-base",
        bundle=BUNDLE,
        origin="explicit C6 fixture",
        added_by_account_id=uuid.UUID(case.binding.scope.account_id),
        recheck=authorized,
    )
    row = frozen("QA-D14-FORK-KEEPS-SKILLS")
    receipt = await case.binding.execute(
        row["operations"][0]["source"],
        {"channel_id": CHANNEL, "env.qa_base_with_own_skill": "qa-base"},
    )
    assert receipt.status == "executed" and receipt.observations["source_unchanged"] is True
    assert receipt.observations["readers"] == "own"
    copied = await case.edge.readback_cli(
        "daimon agents --guild {guild_id} get {fork.name}", receipt.values
    )
    original = await case.edge.read_agent("qa-base")
    assert copied.payload["model"] == original.payload["model"]
    own, clone = original.payload["skills"], copied.payload["skills"]
    assert isinstance(own, list) and isinstance(clone, list) and len(own) == len(clone) == 1
    assert isinstance(own[0], dict) and isinstance(clone[0], dict)
    sid, copied_sid = str(own[0]["skill_id"]), str(clone[0]["skill_id"])
    assert sid != copied_sid and own[0]["version"] == clone[0]["version"] == "1"
    original_bundle = bundle_from_upload(case.server.uploaded[sid], filename="source.zip")
    copied_bundle = bundle_from_upload(case.server.uploaded[copied_sid], filename="copy.zip")
    assert original_bundle.zip_bytes == copied_bundle.zip_bytes == BUNDLE.zip_bytes
    print("D14 actual CLI fork: skill bytes/model preserved; custom skill identity reminted")
    result = await case.binding.execute(row["operations"][1]["source"], {"channel_id": CHANNEL})
    assert result.status == "executed" and result.observations["archived"] == [copied.native_id]
    async with case.binding.sessions() as session:
        _, rows = await list_propagations_for_tenant(
            session, tenant_id=uuid.UUID(case.binding.scope.tenant_id)
        )
    assert not any(r.channel_id == CHANNEL for r in rows)


@pytest.mark.parametrize("kind", ["tenant", "account", "legacy", "platform", "empty-auth", "shape"])
async def test_member_probe_rejects_foreign_or_unbound_scope_before_sdk(
    case: Case, kind: str
) -> None:
    member = case.member
    if kind == "tenant":
        member = member.model_copy(update={"tenant_id": str(uuid.uuid4())})
    elif kind == "account":
        member = member.model_copy(update={"account_id": str(uuid.uuid4())})
    elif kind == "legacy":
        member = Scope.legacy_host_authorized(call_site="refused")
    elif kind == "platform":
        member = Scope.platform(reason="refused")
    elif kind == "empty-auth":
        member = member.model_copy(update={"authorization_id": ""})
    command = MemberAgentEdit(
        member, "qa-unbound-c6", description="changed" if kind == "shape" else "qa probe"
    )
    start = tuple(case.binding.trace.requests)
    with pytest.raises(ScopeViolation):
        await case.edge.attempt_member_edit(command)
    assert tuple(case.binding.trace.requests) == start


async def test_fixture_manifest_hash_change_refuses_before_sdk(case: Case) -> None:
    fixture = case.edge.fixtures["fixtures/qa-unbound-agent.yaml"]
    fixture.path.write_text(UNBOUND + "description: changed\n")
    start = tuple(case.binding.trace.requests)
    with pytest.raises(ScopeViolation, match="manifest"):
        await case.create("qa-unbound-c6")
    assert tuple(case.binding.trace.requests) == start


async def test_foreign_cli_destination_and_unconfirmed_archive_refuse(case: Case) -> None:
    start = tuple(case.binding.trace.requests)
    with pytest.raises(ScopeViolation):
        await case.binding.execute(
            {
                "do": "admin",
                "tool": "cli",
                "args": "daimon agents --guild 923456789012345678 get qa-unbound-c6",
            },
            {},
        )
    pending = await case.binding.execute(
        {
            "do": "admin",
            "tool": "cli",
            "args": "daimon agents --guild {guild_id} archive qa-unbound-c6",
        },
        {},
    )
    assert pending.status == "pending" and tuple(case.binding.trace.requests) == start


@pytest.mark.parametrize("kind", ["principal", "admin", "foreign-member"])
async def test_member_probe_uses_real_owned_member_identity(case: Case, kind: str) -> None:
    member = case.member
    if kind == "principal":
        member = member.model_copy(update={"principal_id": str(uuid.uuid4())})
    elif kind == "admin":
        member = member.model_copy(update={"account_id": case.binding.scope.account_id})
    else:
        other = await provision_admin_run(
            case.binding.sessions,
            workspace_id="923456789012345678",
            backend="anthropic",
            channels=frozenset({"823456789012345678"}),
            trace=ScriptedTransport(router=Server().router),
        )
        try:
            async with case.binding.sessions() as session, session.begin():
                principal = await get_or_create_platform_principal(
                    session,
                    tenant_id=uuid.UUID(other.scope.tenant_id),
                    platform="discord",
                    external_id="423456789012345678",
                )
            member = member.model_copy(
                update={"account_id": str(principal.account_id), "principal_id": str(principal.id)}
            )
        finally:
            await other.close()
    before = tuple(case.binding.trace.requests)
    with pytest.raises(ScopeViolation):
        await case.edge.attempt_member_edit(MemberAgentEdit(member, "qa-unbound-c6"))
    assert tuple(case.binding.trace.requests) == before


async def test_readback_refuses_revision_race_instead_of_cached_unchanged(case: Case) -> None:
    from daimon.core.errors import StoreError
    from daimon.core.mux_backend import resource_scope
    from daimon.core.mux_compat import update_agent
    from daimon.testing.ma_transport import ScriptedReply

    await case.create("qa-unbound-c6")
    before = await case.edge.read_agent("qa-unbound-c6")
    # Real CLI listing independently selects v1. A real SDK update produces v2;
    # replay the already-observed v1 list to model interleaved reads, not a row edit.
    old_list = {
        "data": [before.payload],
        "has_more": False,
        "first_id": before.native_id,
        "last_id": before.native_id,
    }
    await update_agent(
        case.binding.anthropic,
        before.native_id,
        scope=resource_scope(
            tenant_id=case.binding.scope.tenant_id, account_id=case.binding.scope.account_id
        ),
        version=before.version,
        payload={"description": "concurrent update"},
    )
    case.binding.trace.queue(
        ScriptedReply("GET", "/v1/agents", httpx.Response(200, json=old_list)),
        ScriptedReply("GET", "/v1/agents", httpx.Response(200, json=old_list)),
    )
    with pytest.raises(StoreError, match="changed between"):
        await case.edge.read_agent("qa-unbound-c6")
    case.binding.trace.assert_consumed()


@pytest.mark.parametrize(
    "name", ["ANTHROPIC_WEBHOOK_SIGNING_KEY", "ANTHROPIC_CUSTOM_HEADERS", "OPENAI_API_KEY"]
)
async def test_late_sdk_environment_refuses_before_io(
    case: Case, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    before = tuple(case.binding.trace.requests)
    monkeypatch.setenv(name, "not-a-real-secret")
    with pytest.raises(ValueError, match="ambient") as error:
        await case.edge.read_agent("qa-unbound-c6")
    assert "not-a-real-secret" not in str(error.value)
    assert tuple(case.binding.trace.requests) == before


@pytest.mark.parametrize("kind", ["client", "sessions", "default", "public-mcp"])
async def test_runtime_collaborators_must_match_owned_run(case: Case, kind: str) -> None:
    import dataclasses

    from daimon.core.scope import DeploymentDefault

    runtime = case.edge.runtime
    if kind == "client":
        from daimon.testing.ma import build_stub_anthropic

        client = build_stub_anthropic()
        runtime = dataclasses.replace(runtime, anthropic=client)
    elif kind == "sessions":
        runtime = dataclasses.replace(runtime, sessionmaker=async_sessionmaker())
    elif kind == "default":
        runtime = dataclasses.replace(
            runtime,
            deployment_default=DeploymentDefault(agent_name="foreign", environment_name="foreign"),
        )
    else:
        settings = runtime.settings.model_copy(deep=True)
        settings.mcp.public_url = "https://operator.invalid"
        runtime = dataclasses.replace(runtime, settings=settings)
    edge = dataclasses.replace(case.edge, runtime=runtime)
    before = tuple(case.binding.trace.requests)
    try:
        with pytest.raises(ScopeViolation, match="collaborators"):
            await edge.read_agent("qa-unbound-c6")
        assert tuple(case.binding.trace.requests) == before
    finally:
        if kind == "client":
            await runtime.anthropic.close()


async def test_c6_missing_fixture_and_other_backends_remain_typed_pending(case: Case) -> None:
    before = tuple(case.binding.trace.requests)
    pending = await case.binding.execute(
        {
            "do": "admin",
            "tool": "cli",
            "args": "daimon agents --guild {guild_id} create fixtures/ungranted-conversion.yaml",
        },
        {},
    )
    assert pending.code == "ADMIN_AGENT_SPEC_FIXTURE_UNBOUND"
    case.binding.backend = "openai"
    pending = await case.binding.execute(
        {
            "do": "admin",
            "tool": "cli",
            "args": "daimon agents --guild {guild_id} get qa-unbound-c6",
        },
        {},
    )
    assert pending.status == "pending" and pending.code == "ADMIN_ANTHROPIC_C6_ONLY"
    assert tuple(case.binding.trace.requests) == before
