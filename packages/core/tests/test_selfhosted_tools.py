"""Self-hosted tools preserve local skill bytes and caller-bound MCP identity."""

from __future__ import annotations

import base64
import dataclasses
import datetime as dt
import json
import uuid
import zipfile
from pathlib import Path
from typing import cast

import jwt
import pytest
from daimon.core.config import McpCredentialBinding, SelfHostedSettings, Settings
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.neutral_resources import skill_upload
from daimon.core.skills.ingest import SkillBundle, SkillIngestError, bundle_from_files
from daimon.core.stores.domain import ChannelSkillRow
from daimon.core.turn.selfhosted_tools import (
    DAIMON_MCP_AUTH_ENV,
    MAX_SKILL_BYTES,
    SkippedSkill,
    TurnTools,
    build_turn_tools,
    load_seeded_turn_skill_bundles,
    load_turn_skill_bundles,
    mint_turn_mcp_authorization,
)
from mux.contracts.ids import ModelRef, ResourceRef, Revision, Scope, SkillRef
from mux.contracts.ports import Agents
from mux.contracts.resources import Agent, AgentSpec, MCPConnection
from mux.drivers.openai.skills import bundle_digest
from mux.errors import ProviderError, ScopeViolation
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

TENANT = uuid.UUID("00000000-0000-0000-0000-000000000007")
NOW = dt.datetime(2026, 10, 20, tzinfo=dt.UTC)


def bundle(name: str, **files: bytes) -> SkillBundle:
    return bundle_from_files(
        {
            "SKILL.md": f"---\nname: {name}\ndescription: Use {name}.\n---\nRead the files.\n".encode(),
            **files,
        }
    )


def build(
    *,
    servers: tuple[MCPConnection, ...] = (),
    refs: tuple[SkillRef, ...] = (),
    platform_refs: tuple[SkillRef, ...] = (),
    rows: tuple[ChannelSkillRow, ...] = (),
    bundles: dict[tuple[str, str | None], SkillBundle] | None = None,
    bindings: tuple[McpCredentialBinding, ...] = (),
    single_tenant: bool = False,
    allowed_http_hosts: tuple[str, ...] = (),
) -> TurnTools:
    return build_turn_tools(
        daimon_mcp_url="https://mcp.example/mcp",
        tenant_id=TENANT,
        platform="discord",
        channel_id="channel",
        agent_names=("agent",),
        skill_bundles=bundles or {},
        mcp_servers=servers,
        mcp_credential_bindings=bindings,
        single_tenant=single_tenant,
        mcp_allowed_http_hosts=allowed_http_hosts,
        agent_skills=refs,
        platform_skills=platform_refs,
        channel_skills=rows,
    )


def row(id_: str, name: str, **changes: object) -> ChannelSkillRow:
    return ChannelSkillRow.model_validate(
        dict(
            tenant_id=TENANT,
            platform="discord",
            channel_id="channel",
            skill_id=id_,
            version="v1",
            name=name,
            owner_agent_name=None,
            added_by_account_id=None,
            added_at=NOW,
        )
        | changes
    )


def test_reserved_server_and_configured_servers_are_credential_free() -> None:
    tools = build(
        bindings=(
            McpCredentialBinding(
                name="HARNESS_MCP_CRED_PRIVATE_AUTH",
                origin="https://private.example",
                tenants=(TENANT,),
            ),
        ),
        servers=(
            MCPConnection(name="z-public", url="http://public.example/mcp"),
            MCPConnection(
                name="a-private",
                url="https://private.example/mcp",
                credential_ref="HARNESS_MCP_CRED_PRIVATE_AUTH",
            ),
        ),
    )
    assert [server.name for server in tools.mcp_servers] == ["daimon-mcp", "a-private", "z-public"]
    assert tools.mcp_servers[0].headers_env == {"Authorization": DAIMON_MCP_AUTH_ENV}
    assert tools.mcp_servers[1].headers_env == {"Authorization": "HARNESS_MCP_CRED_PRIVATE_AUTH"}
    assert tools.mcp_servers[2].headers_env == {}
    assert "Bearer" not in json.dumps([server.model_dump() for server in tools.mcp_servers])


@pytest.mark.parametrize(
    "server",
    [
        MCPConnection(name="daimon-mcp", url="https://evil.example/mcp"),
        MCPConnection(name="bad name", url="https://mcp.example/mcp"),
        MCPConnection(
            name="other", url="https://mcp.example/mcp", credential_ref=DAIMON_MCP_AUTH_ENV
        ),
        MCPConnection(name="other", url="https://mcp.example/mcp", credential_ref="Bearer secret"),
        MCPConnection(name="other", url="https://mcp.example/mcp", transport="sse"),
        MCPConnection(name="other", url="https://mcp.example/mcp", tool_policy={"ask": True}),
    ],
)
def test_unsupported_or_reserved_mcp_configuration_fails(server: MCPConnection) -> None:
    with pytest.raises(ValueError):
        build(servers=(server,))


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/socket",
        "https://u:p@example/mcp",
        "https://example/mcp?token=x",
        "https://example/mcp#secret",
        "https://example:99999/mcp",
        "https://example/\nmcp",
    ],
)
def test_credentials_and_invalid_mcp_urls_fail(url: str) -> None:
    with pytest.raises(ValueError):
        build(servers=(MCPConnection(name="external", url=url),))


def test_duplicate_server_names_fail() -> None:
    server = MCPConnection(name="external", url="https://example/mcp")
    with pytest.raises(ValueError, match="unique"):
        build(servers=(server, server))


def test_agent_platform_and_channel_skills_keep_files_pins_and_order() -> None:
    a, z, c = (
        bundle("alpha", **{"scripts/run.py": b"print('hi')\n"}),
        bundle("zeta"),
        bundle("channel"),
    )
    bundles: dict[tuple[str, str | None], SkillBundle] = {
        ("own", "v2"): z,
        ("platform", None): a,
        ("extra", "v1"): c,
    }
    refs = (SkillRef(id="own", version="v2", digest=bundle_digest(skill_upload(z))),)
    rows = (
        row("extra", "channel"),
        row("private", "private", owner_agent_name="another"),
        row("own", "zeta"),
    )
    tools = build(refs=refs, platform_refs=(SkillRef(id="platform"),), rows=rows, bundles=bundles)
    assert [skill.name for skill in tools.skills] == ["alpha", "channel", "zeta"]
    assert tools.skills[0].files["scripts/run.py"] == "print('hi')\n"
    assert list(tools.skills[0].files) == ["SKILL.md", "scripts/run.py"]
    assert tools == build(
        refs=refs,
        platform_refs=(SkillRef(id="platform"),),
        rows=tuple(reversed(rows)),
        bundles=bundles,
    )


def test_channel_mount_clashes_and_oldest_wins() -> None:
    b = bundle("same")
    tools = build(
        rows=(row("later", "same", added_at=NOW + dt.timedelta(seconds=1)), row("first", "same")),
        bundles={("first", "v1"): b},
    )
    assert len(tools.skills) == 1
    assert tools.skills[0].name == "same"


@pytest.mark.parametrize(
    "changes", [{"tenant_id": uuid.uuid4()}, {"platform": "slack"}, {"channel_id": "other"}]
)
def test_foreign_channel_rows_are_refused(changes: dict[str, object]) -> None:
    with pytest.raises(ScopeViolation):
        build(rows=(row("foreign", "foreign", **changes),))


def test_pinned_version_never_falls_back_to_latest() -> None:
    with pytest.raises(SkillIngestError, match="no local bundle"):
        build(
            refs=(SkillRef(id="skill", version="v1"),), bundles={("skill", None): bundle("skill")}
        )


def test_digest_and_forged_preview_are_refused() -> None:
    b = bundle("skill")
    with pytest.raises(SkillIngestError, match="digest"):
        build(refs=(SkillRef(id="skill", digest="wrong"),), bundles={("skill", None): b})
    forged = dataclasses.replace(b, preview=b.preview.model_copy(update={"name": "forged"}))
    with pytest.raises(SkillIngestError, match="preview"):
        build(refs=(SkillRef(id="skill"),), bundles={("skill", None): forged})


def test_conflicting_agent_mounts_and_versions_fail() -> None:
    b = bundle("same")
    with pytest.raises(SkillIngestError, match="mount"):
        build(refs=(SkillRef(id="a"), SkillRef(id="b")), bundles={("a", None): b, ("b", None): b})
    with pytest.raises(SkillIngestError, match="pins"):
        build(refs=(SkillRef(id="a"), SkillRef(id="a", version="v2")), bundles={("a", None): b})


def test_binary_files_round_trip_and_digest_covers_every_byte() -> None:
    from mux.drivers.selfhosted.wire import skill_sha256

    source = bundle("skill", **{"image.png": b"\xff\x00\x80", "text.txt": b"UTF-8 text"})
    tools = build(refs=(SkillRef(id="skill"),), bundles={("skill", None): source})
    mounted = tools.skills[0]
    assert base64.b64decode(mounted.binary_files["image.png"]) == b"\xff\x00\x80"
    assert mounted.files["text.txt"] == "UTF-8 text"
    decoded = {path: content.encode() for path, content in mounted.files.items()}
    decoded["image.png"] = b"\xff\x00\x80"
    assert mounted.sha256 == skill_sha256("skill", decoded)


def test_decoded_per_skill_and_turn_caps() -> None:
    with pytest.raises(SkillIngestError, match="decoded byte caps"):
        build(
            refs=(SkillRef(id="huge"),),
            bundles={("huge", None): bundle("huge", **{"data.bin": b"x" * MAX_SKILL_BYTES})},
        )
    refs = tuple(SkillRef(id=f"large-{i}") for i in range(4))
    bundles: dict[tuple[str, str | None], SkillBundle] = {}
    for ref in refs:
        manifest_size = bundle(ref.id).preview.total_bytes
        bundles[(ref.id, None)] = bundle(
            ref.id, **{"data.bin": b"x" * (MAX_SKILL_BYTES - manifest_size)}
        )
    assert len(build(refs=refs[:3], bundles=bundles).skills) == 3
    with pytest.raises(SkillIngestError, match="decoded byte caps"):
        build(refs=refs, bundles=bundles)


def test_archive_decompression_is_bounded_before_full_read(monkeypatch: pytest.MonkeyPatch) -> None:
    from daimon.core.skills.ingest import retained_archive_files

    source = bundle("skill", **{"bomb.txt": b"x" * 100_000})
    requested: list[int] = []
    original = zipfile.ZipExtFile.read

    def bounded_read(self: zipfile.ZipExtFile, n: int = -1) -> bytes:
        requested.append(n)
        assert 0 <= n <= 101, "archive data must never be read without the remaining budget"
        return original(self, n)

    monkeypatch.setattr(zipfile.ZipExtFile, "read", bounded_read)
    with pytest.raises(SkillIngestError):
        retained_archive_files(source.zip_bytes, max_uncompressed_bytes=100)
    assert requested


def test_selected_seeds_use_existing_bundle_pipeline(tmp_path: Path) -> None:
    skill = tmp_path / "seed"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: seed\ndescription: A seed.\n---\nUse this.\n")
    (skill / "notes.txt").write_text("exact bytes\n")
    (skill / "outside.txt").symlink_to(tmp_path / "private.txt")
    (tmp_path / "private.txt").write_text("do not include")
    refs = (SkillRef(id="seed"),)
    bundles = load_seeded_turn_skill_bundles(tmp_path, refs)
    tools = build(refs=refs, bundles=bundles)
    assert tools.skills[0].files["notes.txt"] == "exact bytes\n"
    assert "outside.txt" not in tools.skills[0].files
    assert bundles == load_seeded_turn_skill_bundles(tmp_path, refs)
    assert load_seeded_turn_skill_bundles(tmp_path, (SkillRef(id="seed", version="v1"),)) == {}


async def test_minted_token_is_registered_expiring_redacted_and_tenant_bound(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from daimon.core.stores.identity import get_or_create_platform_principal
    from daimon.core.stores.mcp_tokens import get_mcp_token
    from daimon.testing.factories import make_tenant

    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, platform="discord", workspace_id="selfhosted-test")
        principal = await get_or_create_platform_principal(
            session, tenant_id=tenant.id, platform="discord", external_id="caller"
        )
    scope = Scope(
        tenant_id=str(tenant.id),
        account_id=str(principal.account_id),
        principal_id="caller",
        authorization_id="admitted",
    )
    agent = ResourceRef(
        id="sh_agent_test",
        kind="agent",
        provider="selfhosted",
        tenant_id=scope.tenant_id,
        account_scope_id=scope.tenant_id,
    )

    class TenantAgents:
        async def retrieve(self, authorized: Scope, ref: ResourceRef) -> Agent:
            if ref.id != agent.id or authorized.tenant_id != scope.tenant_id:
                raise ProviderError("not_found", retryable=False)
            return Agent(
                ref=agent,
                revision=Revision(local=1),
                created_at=NOW,
                spec=AgentSpec(name="agent", model=ModelRef(provider="selfhosted", id="gpt-5")),
            )

    agents = cast(Agents, TenantAgents())
    header = await mint_turn_mcp_authorization(
        db_session_factory,
        scope=scope,
        agent=agent,
        agents=agents,
        deadline_s=300,
        secret=b"t" * 32,
        now=NOW,
    )
    claims = jwt.decode(
        header.header.get_secret_value().removeprefix("Bearer "),
        b"t" * 32,
        algorithms=["HS256"],
        options={"verify_exp": False},
    )
    assert claims["anthropic_disabled"] is True
    for deadline in (3540.001, 3600, 10**100, float("nan"), float("inf"), 0, -1):
        with pytest.raises(ValueError, match="deadline"):
            await mint_turn_mcp_authorization(
                db_session_factory,
                scope=scope,
                agent=agent,
                agents=agents,
                deadline_s=deadline,
                secret=b"t" * 32,
                now=NOW,
            )
    maximum = await mint_turn_mcp_authorization(
        db_session_factory,
        scope=scope,
        agent=agent,
        agents=agents,
        deadline_s=3540,
        secret=b"t" * 32,
        now=NOW,
    )
    assert maximum.expires_at == NOW + dt.timedelta(hours=1)
    token = header.header.get_secret_value().removeprefix("Bearer ")
    assert token not in repr(header)
    claims = jwt.decode(token, b"t" * 32, algorithms=["HS256"], options={"verify_exp": False})
    assert claims["sub"] == scope.account_id
    assert claims["chat_agent_id"] == str(
        derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=agent.id)
    )
    assert claims["exp"] - claims["iat"] == 360
    assert header.jti == uuid.UUID(claims["jti"])
    assert header.expires_at == NOW + dt.timedelta(seconds=360)
    assert "agent_id" not in claims and "internal" not in claims
    async with db_session_factory() as session:
        stored = await get_mcp_token(session, jti=uuid.UUID(claims["jti"]))
        assert stored is not None and stored.tenant_id == tenant.id
        assert stored.agent_id == claims["chat_agent_id"]
    from daimon.core.stores.mcp_tokens import revoke_mcp_token

    async with db_session_factory() as session, session.begin():
        revoked = await revoke_mcp_token(session, jti=header.jti, now=NOW)
        assert revoked is not None and revoked.revoked_at == NOW
    with pytest.raises(ProviderError):
        await mint_turn_mcp_authorization(
            db_session_factory,
            scope=scope,
            agent=agent.model_copy(update={"id": "sh_agent_missing"}),
            agents=agents,
            deadline_s=300,
            secret=b"t" * 32,
            now=NOW,
        )
    foreign = scope.model_copy(update={"tenant_id": str(TENANT)})
    foreign_agent = agent.model_copy(
        update={"tenant_id": str(TENANT), "account_scope_id": str(TENANT)}
    )
    with pytest.raises(ProviderError):
        await mint_turn_mcp_authorization(
            db_session_factory,
            scope=foreign,
            agent=foreign_agent,
            agents=agents,
            deadline_s=300,
            secret=b"t" * 32,
            now=NOW,
        )
    with pytest.raises(ScopeViolation, match="scope"):
        await mint_turn_mcp_authorization(
            db_session_factory,
            scope=scope,
            agent=foreign_agent,
            agents=agents,
            deadline_s=300,
            secret=b"t" * 32,
            now=NOW,
        )


def test_skill_count_cap_and_identical_reference_deduplication() -> None:
    from daimon.core.constants import AGENT_SKILL_CAP

    refs = tuple(SkillRef(id=f"s-{i}") for i in range(AGENT_SKILL_CAP + 1))
    bundles: dict[tuple[str, str | None], SkillBundle] = {
        (ref.id, None): bundle(ref.id) for ref in refs
    }
    accepted = build(refs=refs[:-1], bundles=bundles)
    assert len(accepted.skills) == AGENT_SKILL_CAP
    assert (
        build(refs=(refs[0], refs[0]), bundles=bundles).skills
        == build(refs=(refs[0],), bundles=bundles).skills
    )
    with pytest.raises(SkillIngestError, match="count cap"):
        build(refs=refs, bundles=bundles)


@pytest.mark.parametrize(
    "url,env",
    [
        ("http://evil.example/mcp", "AZURE_OPENAI_API_KEY"),
        ("https://evil.example/mcp", "HARNESS_TOKEN"),
        ("https://x.example/mcp", "IDENTITY_HEADER"),
        ("http://169.254.169.254/metadata", "AZURE_CLIENT_SECRET"),
        ("http://evil.example/mcp", "HARNESS_MCP_CRED_NOTES"),
    ],
)
def test_platform_secret_exfiltration_is_refused(url: str, env: str) -> None:
    with pytest.raises(ValueError):
        build(servers=(MCPConnection(name="evil", url=url, credential_ref=env),))


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]"])
def test_loopback_credentials_are_supported_and_urls_normalized(host: str) -> None:
    server = MCPConnection(
        name="local", url=f"http://{host}:8080/mcp/", credential_ref="HARNESS_MCP_CRED_LOCAL"
    )
    with pytest.raises(ValueError, match="unbound"):
        build(servers=(server,))
    public = server.model_copy(update={"credential_ref": None})
    assert build(servers=(public,)).mcp_servers[1].url == f"http://{host}:8080/mcp"


def test_daimon_url_requires_tls_and_cannot_be_aliased() -> None:
    with pytest.raises(ValueError, match="reserved"):
        build(servers=(MCPConnection(name="alias", url="HTTPS://MCP.EXAMPLE:443/mcp/"),))
    for url in ("http://remote.example/mcp", "https://user:pass@example/mcp"):
        with pytest.raises(ValueError):
            build_turn_tools(
                daimon_mcp_url=url,
                tenant_id=TENANT,
                platform="discord",
                channel_id="channel",
                agent_names=(),
                skill_bundles={},
            )
    result = build_turn_tools(
        daimon_mcp_url="https://MCP.EXAMPLE:443/mcp/",
        tenant_id=TENANT,
        platform="discord",
        channel_id="channel",
        agent_names=(),
        skill_bundles={},
    )
    assert result.mcp_servers[0].url == "https://mcp.example/mcp"


def test_platform_and_channel_skills_trim_to_remaining_room() -> None:
    from daimon.core.constants import AGENT_SKILL_CAP

    refs = tuple(SkillRef(id=f"own-{i}") for i in range(AGENT_SKILL_CAP - 1))
    extra = (SkillRef(id="platform-first"), SkillRef(id="platform-last"))
    bundles: dict[tuple[str, str | None], SkillBundle] = {
        (ref.id, None): bundle(ref.id) for ref in (*refs, *extra)
    }
    bundles[("channel", "v1")] = bundle("channel")
    tools = build(
        refs=refs, platform_refs=extra, rows=(row("channel", "channel"),), bundles=bundles
    )
    assert len(tools.skills) == AGENT_SKILL_CAP
    assert {skip.name for skip in tools.skipped} == {"platform-last", "channel"}
    assert all(skip.reason == "skill_cap" for skip in tools.skipped)


def test_unavailable_channel_pin_is_skipped_without_using_latest() -> None:
    tools = build(rows=(row("pin", "pin"),), bundles={("pin", None): bundle("pin")})
    assert tools.skills == ()
    assert tools.skipped == (SkippedSkill("pin", "pinned_bundle_unavailable"),)


async def test_real_shipped_daimon_agent_builds_with_expected_builtin_skips() -> None:
    from daimon.core.defaults.loader import load_agent_specs

    defaults = Path(__file__).resolve().parents[3] / "defaults"
    agent = next(spec for spec in load_agent_specs(defaults / "agents") if spec.name == "daimon")
    own = tuple(SkillRef(id=ref.skill_id) for ref in agent.skills if ref.platforms is None)
    teams = tuple(
        SkillRef(id=ref.skill_id, source=ref.type)
        for ref in agent.skills
        if ref.platforms and "teams" in ref.platforms
    )
    scope = Scope(
        tenant_id=str(TENANT),
        account_id=str(uuid.uuid4()),
        principal_id="caller",
        authorization_id="admitted",
    )
    bundles = await load_turn_skill_bundles(defaults / "skills", (*own, *teams), scope=scope)
    # The production async host must keep packaging/decompression off its loop.
    import asyncio

    tools = await asyncio.to_thread(build, refs=own, platform_refs=teams, bundles=bundles)
    assert {skip.name for skip in tools.skipped} == {"docx", "pptx", "xlsx"}
    assert all(skip.reason == "provider_builtin" for skip in tools.skipped)
    assert {skill.name for skill in tools.skills} == {
        ref.id for ref in (*own, *teams) if ref.id not in {"docx", "pptx", "xlsx"}
    }
    assert "marimo_notebooks" in {skill.name for skill in tools.skills}
    art = next(skill for skill in tools.skills if skill.name == "pymc-artifact-style")
    assert art.binary_files, "the real artwork/font assets must reach the harness"
    for skill in tools.skills:
        assert skill.sha256 and "SKILL.md" in skill.files


async def test_retained_channel_pin_loads_exact_authorized_catalog_bytes(
    db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    from daimon.core.stores.neutral_catalog import PostgresNeutralCatalog
    from daimon.testing.factories import make_account, make_tenant

    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session)
        account = await make_account(session, tenant=tenant)
        other = await make_account(session, tenant=tenant)
    scope = Scope(
        tenant_id=str(tenant.id),
        account_id=str(account.id),
        principal_id="caller",
        authorization_id="admitted",
    )
    catalog = PostgresNeutralCatalog(
        db_session_factory, provider="openai", account_scope_id="workspace"
    )
    first = bundle("retained", **{"asset.png": b"\xff\x00"})
    second = bundle("retained", **{"asset.png": b"\xff\x01"})
    for version, source in (("v1", first), ("v2", second)):
        await catalog.put_skill(
            scope,
            skill_id="uploaded",
            version=version,
            agent_name="agent",
            display_title="retained",
            bundle=source,
        )
    refs = (
        SkillRef(id="uploaded", version="v1", digest=bundle_digest(skill_upload(first))),
        SkillRef(id="missing", version="v1"),
    )
    loaded = await load_turn_skill_bundles(tmp_path, refs, scope=scope, catalog=catalog)
    assert loaded == {("uploaded", "v1"): first}
    assert build(refs=refs[:1], bundles=loaded).skills[0].name == "retained"
    tools = build_turn_tools(
        daimon_mcp_url="https://mcp.example/mcp",
        tenant_id=tenant.id,
        platform="discord",
        channel_id="channel",
        agent_names=("agent",),
        skill_bundles=loaded,
        channel_skills=(row("uploaded", "retained", tenant_id=tenant.id),),
    )
    assert base64.b64decode(tools.skills[0].binary_files["asset.png"]) == b"\xff\x00"
    assert (
        await load_turn_skill_bundles(
            tmp_path,
            refs,
            scope=scope.model_copy(update={"account_id": str(other.id)}),
            catalog=catalog,
        )
        == {}
    )


@pytest.mark.parametrize("origin", ["https://service.example", "HTTPS://SERVICE.EXAMPLE:443"])
def test_bound_credential_accepts_only_operator_origin_and_tenant(origin: str) -> None:
    binding = McpCredentialBinding(
        name="HARNESS_MCP_CRED_SERVICE", origin=origin, tenants=(TENANT,)
    )
    server = MCPConnection(
        name="service", url="https://service.example:443/mcp", credential_ref=binding.name
    )
    assert build(servers=(server,), bindings=(binding,)).mcp_servers[1].headers_env == {
        "Authorization": binding.name
    }
    with pytest.raises(ValueError, match="unbound"):
        build(servers=(server,))
    with pytest.raises(ValueError, match="origin"):
        build(
            servers=(server.model_copy(update={"url": "https://attacker.example/mcp"}),),
            bindings=(binding,),
        )
    with pytest.raises(ScopeViolation, match="tenant"):
        build(
            servers=(server,), bindings=(binding.model_copy(update={"tenants": (uuid.uuid4(),)}),)
        )
    with pytest.raises(ValueError, match="origin"):
        build(
            servers=(server.model_copy(update={"url": "https://service.example:444/mcp"}),),
            bindings=(binding,),
        )


def test_binding_json_setting_and_single_tenant_wildcard(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = [
        {
            "name": "HARNESS_MCP_CRED_SERVICE",
            "origin": "https://service.example",
            "tenants": [str(TENANT)],
        }
    ]
    monkeypatch.setenv("DAIMON_SELFHOSTED__MCP_CREDENTIAL_BINDINGS", json.dumps(raw))
    settings = Settings(database={"url": "postgresql+asyncpg://localhost/unused"})
    binding = settings.selfhosted.mcp_credential_bindings[0]
    assert binding.tenants == (TENANT,)
    server = MCPConnection(
        name="service", url="https://service.example/mcp", credential_ref=binding.name
    )
    single_tenant = McpCredentialBinding(name=binding.name, origin=binding.origin, tenants=("*",))
    with pytest.raises(ValueError, match="single-tenant"):
        build(servers=(server,), bindings=(single_tenant,))
    assert (
        build(servers=(server,), bindings=(single_tenant,), single_tenant=True).mcp_servers[1].name
        == "service"
    )
    with pytest.raises(ValueError, match="single-tenant"):
        SelfHostedSettings(mcp_credential_bindings=(single_tenant,))
    assert SelfHostedSettings(single_tenant=True, mcp_credential_bindings=(single_tenant,))
    with pytest.raises(ValueError, match="unique"):
        SelfHostedSettings(mcp_credential_bindings=(binding, binding))


@pytest.mark.parametrize(
    "origin",
    [
        "http://service.example",
        "https://user@service.example",
        "https://service.example/",
        "https://service.example?x=1",
        "https://service.example#x",
        "https://service.example:99999",
    ],
)
def test_invalid_credential_binding_origin_is_refused(origin: str) -> None:
    with pytest.raises(ValueError):
        McpCredentialBinding(name="HARNESS_MCP_CRED_SERVICE", origin=origin, tenants=(TENANT,))


def test_native_content_pin_matches_retained_bundle_not_zip() -> None:
    original = bundle("native", **{"asset.bin": b"\xff\x00", "notes.txt": b"retained"})
    from daimon.core.skills.ingest import bundle_from_retained_archive

    retained = bundle_from_retained_archive(original.zip_bytes)
    pin = bundle_digest(skill_upload(original))
    assert pin != retained.preview.content_hash
    refs = (SkillRef(id="native", version="v1", digest=pin),)
    assert build(refs=refs, bundles={("native", "v1"): retained}).skills[0].name == "native"
    changed = bundle("native", **{"asset.bin": b"\xff\x01", "notes.txt": b"retained"})
    with pytest.raises(SkillIngestError, match="digest"):
        build(refs=refs, bundles={("native", "v1"): changed})
    with pytest.raises(SkillIngestError, match="digest"):
        build(
            refs=(SkillRef(id="native", digest=retained.preview.content_hash),),
            bundles={("native", None): retained},
        )
    tools = build_turn_tools(
        daimon_mcp_url="https://mcp.example/mcp",
        tenant_id=TENANT,
        platform="discord",
        channel_id="channel",
        agent_names=(),
        skill_bundles={("native", None): retained},
        skill_digest_kind="zip",
        agent_skills=(SkillRef(id="native", digest=retained.preview.content_hash),),
    )
    assert tools.skills[0].name == "native"


def test_held_version_names_win_over_old_channel_pins() -> None:
    tools = build(
        refs=(SkillRef(id="own", version="v2"),),
        rows=(row("own", "old-name"), row("extra", "new-name")),
        bundles={
            ("own", "v2"): bundle("new-name"),
            ("own", "v1"): bundle("old-name"),
            ("extra", "v1"): bundle("new-name"),
        },
    )
    assert [skill.name for skill in tools.skills] == ["new-name"]


def test_channel_clashes_use_final_bundle_names() -> None:
    tools = build(
        refs=(SkillRef(id="own"),),
        rows=(row("extra", "stale-name"), row("next", "other-stale-name")),
        bundles={
            ("own", None): bundle("held-name"),
            ("extra", "v1"): bundle("held-name"),
            ("next", "v1"): bundle("next-name"),
        },
    )
    assert [skill.name for skill in tools.skills] == ["held-name", "next-name"]


@pytest.mark.parametrize("name", ["pdf", "docx", "pptx", "xlsx"])
def test_explicit_custom_source_overrides_builtin_id(name: str) -> None:
    tools = build(refs=(SkillRef(id=name, source="custom"),), bundles={(name, None): bundle(name)})
    assert [skill.name for skill in tools.skills] == [name]
    assert tools.skipped == ()
    assert build(refs=(SkillRef(id=name),)).skipped == (SkippedSkill(name, "provider_builtin"),)


@pytest.mark.parametrize("name", ["pdf", "docx"])
def test_channel_skills_are_explicitly_custom_even_with_builtin_ids(name: str) -> None:
    tools = build(rows=(row(name, name),), bundles={(name, "v1"): bundle(name)})
    assert [skill.name for skill in tools.skills] == [name]
    assert tools.skipped == ()


@pytest.mark.parametrize(
    "host",
    [
        "169.254.169.254",
        "169.254.170.2",
        "10.1.2.3",
        "172.16.0.1",
        "192.168.0.1",
        "100.100.100.200",
        "0.0.0.0",
        "[fe80::1]",
        "[fd00::1]",
        "[::ffff:169.254.169.254]",
        "2852039166",
        "0xa9fea9fe",
        "0251.0376.0251.0376",
        "10.1",
        "metadata.google.internal",
        "metadata",
        "mcp.internal",
        "mcp.svc.cluster.local",
        "mcp.local",
    ],
)
def test_credential_free_private_http_requires_operator_exception(host: str) -> None:
    server = MCPConnection(name="private", url=f"http://{host}/mcp")
    with pytest.raises(ValueError, match="allowed host"):
        build(servers=(server,))
    allowed = host.strip("[]")
    assert build(servers=(server,), allowed_http_hosts=(allowed,)).mcp_servers[1].name == "private"
    with pytest.raises(ValueError, match="allowed host"):
        build(servers=(server,), allowed_http_hosts=("different.example",))


async def test_channel_member_loads_only_persisted_exact_channel_grants(
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    from daimon.core.stores.channel_skills import add_channel_skill
    from daimon.core.stores.neutral_catalog import PostgresNeutralCatalog
    from daimon.testing.factories import make_account, make_tenant

    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session)
        uploader = await make_account(session, tenant=tenant)
        member = await make_account(session, tenant=tenant)
        foreign = await make_tenant(session)
        outsider = await make_account(session, tenant=foreign)
    scope = Scope(
        tenant_id=str(tenant.id),
        account_id=str(uploader.id),
        principal_id="uploader",
        authorization_id="admitted",
    )
    member_scope = scope.model_copy(update={"account_id": str(member.id), "principal_id": "member"})
    catalog = PostgresNeutralCatalog(
        db_session_factory, provider="openai", account_scope_id="workspace"
    )
    first, second = (
        bundle("shared", **{"data.bin": b"\xff\x00"}),
        bundle("shared", **{"data.bin": b"\xff\x01"}),
    )
    for version, source in (("v1", first), ("v2", second)):
        await catalog.put_skill(
            scope,
            skill_id="shared",
            version=version,
            agent_name="agent",
            display_title="shared",
            bundle=source,
        )
    async with db_session_factory() as session, session.begin():
        grant = await add_channel_skill(
            session,
            tenant_id=tenant.id,
            platform="discord",
            channel_id="channel",
            skill_id="shared",
            version="v1",
            name="shared",
            owner_agent_name="agent",
            actor_account_id=uploader.id,
        )
    assert await catalog.get_skill(member_scope, "shared", "v1") is None

    async def load(
        authorized: Scope,
        *,
        channel: str = "channel",
        names: tuple[str, ...] = ("agent",),
        rows: tuple[ChannelSkillRow, ...] = (grant,),
    ) -> dict[tuple[str, str | None], SkillBundle]:
        return await load_turn_skill_bundles(
            tmp_path,
            (),
            scope=authorized,
            catalog=catalog,
            platform="discord",
            channel_id=channel,
            agent_names=names,
            channel_skills=rows,
        )

    loaded = await load(member_scope)
    assert loaded == {("shared", "v1"): first}
    assert await load(scope) == loaded
    assert (
        await load_turn_skill_bundles(
            tmp_path, (SkillRef(id="shared", version="v1"),), scope=member_scope, catalog=catalog
        )
        == {}
    )
    tools = build_turn_tools(
        daimon_mcp_url="https://mcp.example/mcp",
        tenant_id=tenant.id,
        platform="discord",
        channel_id="channel",
        agent_names=("agent",),
        skill_bundles=loaded,
        channel_skills=(grant,),
    )
    assert base64.b64decode(tools.skills[0].binary_files["data.bin"]) == b"\xff\x00"
    assert await load(member_scope, names=("different",)) == {}
    assert (
        await load(
            member_scope, channel="other", rows=(grant.model_copy(update={"channel_id": "other"}),)
        )
        == {}
    )
    assert await load(member_scope, rows=(grant.model_copy(update={"version": "v2"}),)) == {}
    foreign_scope = member_scope.model_copy(
        update={"tenant_id": str(foreign.id), "account_id": str(outsider.id)}
    )
    with pytest.raises(ScopeViolation):
        await load(foreign_scope)
    assert (
        await load(foreign_scope, rows=(grant.model_copy(update={"tenant_id": foreign.id}),)) == {}
    )
    other_workspace = PostgresNeutralCatalog(
        db_session_factory, provider="openai", account_scope_id="other"
    )
    assert (
        await load_turn_skill_bundles(
            tmp_path,
            (),
            scope=member_scope,
            catalog=other_workspace,
            platform="discord",
            channel_id="channel",
            agent_names=("agent",),
            channel_skills=(grant,),
        )
        == {}
    )

    await catalog.put_skill(
        member_scope,
        skill_id="shared",
        version="v1",
        agent_name="agent",
        display_title="shared",
        bundle=second,
    )
    with pytest.raises(ScopeViolation, match="ambiguous"):
        await load(member_scope)


@pytest.mark.parametrize("name", ["pdf", "docx"])
def test_explicit_custom_builtin_named_seed_loads(tmp_path: Path, name: str) -> None:
    path = tmp_path / name
    path.mkdir()
    (path / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Custom skill.\n---\nCustom.")
    refs = (SkillRef(id=name, source="custom"),)
    assert (
        build(refs=refs, bundles=load_seeded_turn_skill_bundles(tmp_path, refs)).skills[0].name
        == name
    )


@pytest.mark.parametrize("retained", [False, True])
async def test_selfhosted_pins_need_explicit_retention_in_the_agent_tenant(
    db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path, retained: bool
) -> None:
    from daimon.core.anthropic_guard import without_anthropic
    from daimon.core.stores.neutral_catalog import PostgresNeutralCatalog
    from daimon.core.stores.selfhosted_resources import SelfHostedAgents
    from daimon.testing.factories import make_account, make_tenant

    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session)
        account = await make_account(session, tenant=tenant)
    scope = Scope(
        tenant_id=str(tenant.id),
        account_id=str(account.id),
        principal_id="caller",
        authorization_id="admitted",
    )
    catalog = PostgresNeutralCatalog(
        db_session_factory, provider="selfhosted", account_scope_id=scope.tenant_id
    )
    pin = SkillRef(id="retained", version="v1")
    source = bundle("retained", **{"asset.bin": b"\xff\x00"})
    with without_anthropic():
        if retained:
            # Explicit test seeding: this store API is not a production upload route.
            await catalog.put_skill(
                scope,
                skill_id=pin.id,
                version=pin.version,
                agent_name="agent",
                display_title="retained",
                bundle=source,
            )
        agent = await SelfHostedAgents(db_session_factory).create(
            scope,
            AgentSpec(
                name="agent", model=ModelRef(provider="selfhosted", id="gpt-5"), skills=(pin,)
            ),
            key="pinned-agent",
        )
        loaded = await load_turn_skill_bundles(
            tmp_path, (pin,), scope=scope, catalog=catalog, agent=agent
        )
        if retained:
            assert loaded == {(pin.id, pin.version): source}
            assert build(refs=(pin,), bundles=loaded).skills[0].name == "retained"
        else:
            assert loaded == {}
            with pytest.raises(SkillIngestError, match="no local bundle"):
                build(refs=(pin,), bundles=loaded)
        deployment_catalog = PostgresNeutralCatalog(
            db_session_factory, provider="selfhosted", account_scope_id="deployment-account"
        )
        with pytest.raises(ScopeViolation, match="admitted skill catalog"):
            await load_turn_skill_bundles(
                tmp_path, (pin,), scope=scope, catalog=deployment_catalog, agent=agent
            )
