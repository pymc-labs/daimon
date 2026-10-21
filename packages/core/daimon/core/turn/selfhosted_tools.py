"""Prepare harness tools from scoped records; credentials travel as transport headers."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import math
import re
import socket
import uuid
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from anthropic.types.beta import BetaManagedAgentsCustomSkill
from daimon.core.channel_skills import applicable_skills, skill_room
from daimon.core.config import McpCredentialBinding
from daimon.core.constants import AGENT_SKILL_CAP
from daimon.core.defaults.mcp_merge import get_reserved_mcp_rejection
from daimon.core.mcp_server_url import canonical_mcp_url
from daimon.core.session_snapshot import MaSkill
from daimon.core.skills.ingest import (
    SkillBundle,
    SkillIngestError,
    bundle_from_retained_archive,
    retained_archive_files,
)
from daimon.core.stores.domain import ChannelSkillRow
from daimon.core.stores.neutral_catalog import PostgresNeutralCatalog
from mux.contracts.ids import ResourceRef, Scope, SkillRef
from mux.contracts.ports import Agents
from mux.contracts.resources import Agent, MCPConnection, SkillUpload, SkillUploadFile
from mux.drivers.selfhosted.wire import SKILL_MAX_BYTES as MAX_SKILL_BYTES
from mux.drivers.selfhosted.wire import TURN_SKILLS_MAX_BYTES as MAX_TURN_SKILL_BYTES
from mux.drivers.selfhosted.wire import InlineSkill, MCPServer, skill_sha256
from mux.errors import ScopeViolation
from mux.profiles.anthropic import is_builtin_skill
from mux.state.operations import request_digest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

DAIMON_MCP_SERVER = "daimon-mcp"
DAIMON_MCP_AUTH_ENV = "DAIMON_MCP_AUTHORIZATION"
DAIMON_MCP_AUTH_HEADER = "X-Daimon-MCP-Authorization"
_CREDENTIAL_ENV = re.compile(r"HARNESS_MCP_CRED_[A-Za-z0-9_]+")
_SERVER_NAME = re.compile(r"[A-Za-z0-9_.-]+")


@dataclass(frozen=True)
class SkippedSkill:
    name: str
    reason: str


@dataclass(frozen=True)
class TurnTools:
    """Credential-free wire fields and notices the host displays once per thread."""

    mcp_servers: tuple[MCPServer, ...]
    skills: tuple[InlineSkill, ...]
    skipped: tuple[SkippedSkill, ...] = ()


@dataclass(frozen=True)
class MintedMcpAuthorization:
    header: SecretStr = field(repr=False)
    jti: uuid.UUID
    expires_at: datetime


@dataclass(frozen=True)
class _SkillHolder:
    skills: Sequence[MaSkill]


def _mcp_url(url: str, *, credential: bool = False, allowed_http_hosts: Sequence[str] = ()) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(char.isspace() or ord(char) < 32 for char in url)
    ):
        raise ValueError("MCP URL must be HTTP without credentials, query or fragment")
    _ = parsed.port
    loopback = parsed.hostname.lower() == "localhost"
    with suppress(ValueError):
        loopback |= ipaddress.ip_address(parsed.hostname).is_loopback
    if (
        credential
        and parsed.scheme != "https"
        and not loopback
        and parsed.hostname.lower() not in allowed_http_hosts
    ):
        raise ValueError("Credential-bearing MCP connections require HTTPS or loopback")
    return canonical_mcp_url(url)


def _check_http_destination(url: str, *, allowed_hosts: Sequence[str]) -> None:
    """Classify literals and internal names without DNS in the pure builder.

    The harness network boundary must also prevent DNS rebinding and redirects
    into private networks. An operator exception authorizes an exact hostname.
    """
    parsed = urlsplit(url)
    if parsed.scheme != "http":
        return
    host = (parsed.hostname or "").lower().rstrip(".")
    if host in {value.lower().rstrip(".") for value in allowed_hosts}:
        return
    address = None
    with suppress(ValueError):
        address = ipaddress.ip_address(host)
    if address is None:
        with suppress(OSError):
            address = ipaddress.IPv4Address(socket.inet_aton(host))
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if host == "localhost" or (address is not None and address.is_loopback):
        return
    internal = (
        not address.is_global
        if address is not None
        else "." not in host
        or host.endswith((".internal", ".local", ".localhost", ".svc.cluster.local"))
        or (".internal." in host and host.endswith(".azurecontainerapps.io"))
        or host in {"metadata.google.internal", "metadata.azure.internal"}
    )
    if internal:
        raise ValueError("Private HTTP MCP destination requires an operator allowed host")


def _inline_skill(
    ref: SkillRef,
    bundle: SkillBundle,
    *,
    remaining_bytes: int,
    digest_kind: Literal["content", "zip"],
) -> InlineSkill:
    cap = min(MAX_SKILL_BYTES, remaining_bytes)
    if bundle.preview.total_bytes > cap:
        raise SkillIngestError(
            "Harness skills exceed decoded byte caps (8 MiB/skill, 24 MiB/turn)."
        )
    digest = hashlib.sha256(bundle.zip_bytes).hexdigest()
    if digest != bundle.preview.content_hash:
        raise SkillIngestError("An admitted skill bundle has a different content digest.")
    checked = bundle_from_retained_archive(bundle.zip_bytes, max_uncompressed_bytes=cap)
    if checked.preview != bundle.preview:
        raise SkillIngestError("An admitted skill bundle differs from its preview.")
    files = retained_archive_files(checked.zip_bytes, max_uncompressed_bytes=cap)
    if ref.digest is not None:
        # Native mux content pins cover canonical SkillUpload, independently of
        # ZIP packaging and the harness digest (which includes the mount name).
        canonical = SkillUpload(
            files=tuple(
                SkillUploadFile(path=path, content=body) for path, body in sorted(files.items())
            )
        )
        expected = (
            request_digest(canonical.model_dump(mode="json"))
            if digest_kind == "content"
            else digest
        )
        if ref.digest != expected:
            raise SkillIngestError("An admitted skill bundle has a different content digest.")
    text: dict[str, str] = {}
    binary: dict[str, str] = {}
    for path, body in sorted(files.items()):
        try:
            text[path] = body.decode("utf-8")
        except UnicodeDecodeError:
            binary[path] = base64.b64encode(body).decode("ascii")
    return InlineSkill(
        name=checked.preview.name,
        files=text,
        binary_files=binary,
        sha256=skill_sha256(checked.preview.name, files),
    )


def build_turn_tools(
    *,
    daimon_mcp_url: str,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str,
    agent_names: Sequence[str],
    skill_bundles: Mapping[tuple[str, str | None], SkillBundle],
    mcp_servers: Sequence[MCPConnection] = (),
    mcp_credential_bindings: Sequence[McpCredentialBinding] = (),
    single_tenant: bool = False,
    mcp_allowed_http_hosts: Sequence[str] = (),
    skill_digest_kind: Literal["content", "zip"] = "content",
    agent_skills: Sequence[SkillRef] = (),
    platform_skills: Sequence[SkillRef] = (),
    channel_skills: Sequence[ChannelSkillRow] = (),
) -> TurnTools:
    """Pure preparation from authorized records; exact pins never fall back to latest.

    Missing agent custom bundles fail closed. Provider built-ins and unavailable
    pinned channel bundles are reported as skips. Platform/channel additions use
    the shared ownership, mount and count rules. Archive corruption/digest/cap
    failures refuse the turn rather than quietly changing requested content.
    """
    daimon_mcp_url = _mcp_url(
        daimon_mcp_url, credential=True, allowed_http_hosts=mcp_allowed_http_hosts
    )
    servers = [
        MCPServer(
            name=DAIMON_MCP_SERVER,
            url=daimon_mcp_url,
            headers_env={"Authorization": DAIMON_MCP_AUTH_ENV},
            allow_insecure_http=(
                urlsplit(daimon_mcp_url).scheme == "http"
                and urlsplit(daimon_mcp_url).hostname in mcp_allowed_http_hosts
            ),
        )
    ]
    bindings = {binding.name: binding for binding in mcp_credential_bindings}
    if len(bindings) != len(mcp_credential_bindings):
        raise ValueError("MCP credential binding names must be unique")
    if not single_tenant and any(binding.tenants == ("*",) for binding in bindings.values()):
        raise ValueError("Wildcard MCP credential bindings require a single-tenant installation")
    names = {DAIMON_MCP_SERVER}
    for connection in sorted(mcp_servers, key=lambda item: item.name):
        url = _mcp_url(connection.url, credential=connection.credential_ref is not None)
        if connection.credential_ref is None:
            _check_http_destination(url, allowed_hosts=mcp_allowed_http_hosts)
        if get_reserved_mcp_rejection(
            server_name=connection.name, url=url, public_url=daimon_mcp_url
        ):
            raise ValueError("MCP server name or URL collides with reserved Daimon connection")
        if not _SERVER_NAME.fullmatch(connection.name) or connection.name in names:
            raise ValueError("MCP server names must be unique")
        if connection.transport != "streamable_http" or connection.tool_policy:
            raise ValueError("Harness MCP requires streamable HTTP without tool policies")
        headers: dict[str, str] = {}
        if connection.credential_ref is not None:
            if not _CREDENTIAL_ENV.fullmatch(connection.credential_ref):
                raise ValueError(
                    "MCP credentials must name a HARNESS_MCP_CRED_ environment variable"
                )
            binding = bindings.get(connection.credential_ref)
            parsed = urlsplit(url)
            origin = f"{parsed.scheme}://{parsed.netloc}"
            if binding is None or binding.origin != origin:
                raise ValueError("MCP credential is unbound or has a different origin")
            if tenant_id not in binding.tenants and binding.tenants != ("*",):
                raise ScopeViolation(connection.name, "MCP credential belongs to another tenant")
            headers["Authorization"] = connection.credential_ref
        names.add(connection.name)
        servers.append(MCPServer(name=connection.name, url=url, headers_env=headers))

    skills: list[InlineSkill] = []
    held: dict[str, SkillRef] = {}
    mounts: set[str] = set()
    skipped: list[SkippedSkill] = []
    consumed = 0

    def add(ref: SkillRef) -> None:
        nonlocal consumed
        if is_builtin_skill(ref):
            skipped.append(SkippedSkill(ref.id, "provider_builtin"))
            return
        previous = held.get(ref.id)
        if previous is not None:
            if previous != ref:
                raise SkillIngestError("An admitted skill has conflicting version pins.")
            return
        bundle = skill_bundles.get((ref.id, ref.version))
        if bundle is None:
            raise SkillIngestError("An admitted skill version has no local bundle.")
        skill = _inline_skill(
            ref,
            bundle,
            remaining_bytes=MAX_TURN_SKILL_BYTES - consumed,
            digest_kind=skill_digest_kind,
        )
        if skill.name in mounts:
            raise SkillIngestError("Admitted skills have conflicting mount names.")
        if len(skills) >= AGENT_SKILL_CAP:
            raise SkillIngestError("The admitted agent exceeds the skill count cap.")
        held[ref.id] = ref
        mounts.add(skill.name)
        consumed += bundle.preview.total_bytes
        skills.append(skill)

    def holder() -> _SkillHolder:
        return _SkillHolder(
            tuple(
                BetaManagedAgentsCustomSkill(
                    type="custom", skill_id=ref.id, version=ref.version or "latest"
                )
                for ref in held.values()
            )
        )

    for ref in agent_skills:
        add(ref)
    for ref in platform_skills:
        if not is_builtin_skill(ref) and ref.id not in held and not skill_room(holder()):
            skipped.append(SkippedSkill(ref.id, "skill_cap"))
        else:
            add(ref)
    rows = sorted(channel_skills, key=lambda row: (row.added_at, row.skill_id, row.version))
    for row in rows:
        if (row.tenant_id, row.platform, row.channel_id) != (tenant_id, platform, channel_id):
            raise ScopeViolation(row.skill_id, "channel skill is outside the admitted channel")
    # Use final mount names. Channel pins for held IDs cannot replace an
    # agent's selected version or its mount name.
    bodies = {id_: skill_bundles[(id_, ref.version)].preview.name for id_, ref in held.items()}
    named_rows = [
        row.model_copy(update={"name": skill_bundles[(row.skill_id, row.version)].preview.name})
        if (row.skill_id, row.version) in skill_bundles
        else row
        for row in rows
    ]
    candidates = applicable_skills(
        named_rows, agent=holder(), agent_names=tuple(agent_names), bodies=bodies
    )
    for extra in candidates:
        if (extra.skill_id, extra.version) not in skill_bundles:
            skipped.append(SkippedSkill(extra.skill_id, "pinned_bundle_unavailable"))
        elif not skill_room(holder()):
            skipped.append(SkippedSkill(extra.skill_id, "skill_cap"))
        else:
            add(SkillRef(id=extra.skill_id, version=extra.version, source="custom"))
    return TurnTools(
        tuple(servers),
        tuple(sorted(skills, key=lambda skill: skill.name)),
        tuple(sorted(set(skipped), key=lambda item: (item.name, item.reason))),
    )


async def mint_turn_mcp_authorization(
    sessions: async_sessionmaker[AsyncSession],
    *,
    scope: Scope,
    agent: ResourceRef,
    agents: Agents,
    secret: bytes,
    deadline_s: float,
    now: datetime | None = None,
    slack_turn_context_id: uuid.UUID | None = None,
) -> MintedMcpAuthorization:
    """Register a live agent's token; the host revokes jti in its turn-finally path."""
    from daimon.core.ma_identity import derive_agent_uuid
    from daimon.core.mcp_auth import CHAT_TOKEN_TTL, mint_chat_mcp_token
    from daimon.core.stores.accounts import get_account_with_tenant

    if (
        scope.is_platform
        or scope.is_legacy_host_authorized
        or not scope.principal_id.strip()
        or not scope.authorization_id.strip()
        or agent.provider != "selfhosted"
        or agent.kind != "agent"
        or not agent.id.startswith("sh_agent_")
        or agent.tenant_id != scope.tenant_id
        or agent.account_scope_id != scope.tenant_id
    ):
        raise ScopeViolation(agent.id, "explicit admitted self-hosted agent scope required")
    if not math.isfinite(deadline_s) or deadline_s <= 0:
        raise ValueError("turn deadline must be positive and finite")
    if deadline_s + 60 > CHAT_TOKEN_TTL.total_seconds():
        raise ValueError("turn deadline plus grace exceeds chat token TTL")
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    live = await agents.retrieve(scope, agent)
    if (
        live.lifecycle != "active"
        or live.ref.id != agent.id
        or live.ref.tenant_id != scope.tenant_id
        or live.ref.provider != "selfhosted"
        or live.ref.kind != "agent"
        or live.ref.account_scope_id != scope.tenant_id
    ):
        raise ScopeViolation(agent.id, "MCP credential requires a live tenant agent")
    tenant_id, account_id = uuid.UUID(scope.tenant_id), uuid.UUID(scope.account_id)
    async with sessions() as session, session.begin():
        identity = await get_account_with_tenant(session, account_id=account_id)
        if identity is None or identity.tenant_id != tenant_id:
            raise ScopeViolation(agent.id, "MCP caller is outside the admitted tenant")
        token, jti, expires_at = await mint_chat_mcp_token(
            session,
            account_id=account_id,
            agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=agent.id),
            secret=secret,
            now=now,
            ttl=timedelta(seconds=deadline_s + 60),
            anthropic_disabled=True,
            slack_turn_context_id=slack_turn_context_id,
        )
    return MintedMcpAuthorization(SecretStr(f"Bearer {token}"), jti, expires_at)


def load_seeded_turn_skill_bundles(
    skills_root: Path, refs: Sequence[SkillRef]
) -> dict[tuple[str, str | None], SkillBundle]:
    """Synchronous filesystem shell; async hosts use load_turn_skill_bundles.

    Missing/pinned/built-in refs are left for the builder's explicit resolution
    policy. A local seed never substitutes for a retained pinned version.
    """
    from daimon.core.defaults.loader import load_skill_paths, load_skill_spec
    from daimon.core.skill_zip import canonical_zip_bytes

    paths = {path.name: path for path in load_skill_paths(skills_root)}
    bundles: dict[tuple[str, str | None], SkillBundle] = {}
    for ref in sorted(refs, key=lambda ref: (ref.id, ref.version or "")):
        if is_builtin_skill(ref) or ref.version is not None or ref.id not in paths:
            continue
        path = paths[ref.id]
        if path.is_symlink() or (path / "SKILL.md").is_symlink():
            raise SkillIngestError("A seeded skill must not be a symlink.")
        spec, _ = load_skill_spec(path)
        bundles[(ref.id, None)] = bundle_from_retained_archive(
            canonical_zip_bytes(
                path, arcname_prefix=spec.name, max_uncompressed_bytes=MAX_SKILL_BYTES
            ),
            max_uncompressed_bytes=MAX_SKILL_BYTES,
        )
    return bundles


async def load_turn_skill_bundles(
    skills_root: Path,
    refs: Sequence[SkillRef],
    *,
    scope: Scope,
    catalog: PostgresNeutralCatalog | None = None,
    platform: str | None = None,
    channel_id: str | None = None,
    agent_names: Sequence[str] = (),
    channel_skills: Sequence[ChannelSkillRow] = (),
    agent: Agent | None = None,
) -> dict[tuple[str, str | None], SkillBundle]:
    """Load local seeds off-loop and exact retained versions from an authorized catalog.

    Catalog provenance (including its provider workspace) is owned by admission;
    no provider API is called and missing pinned versions stay missing.
    """
    bundles = await asyncio.to_thread(load_seeded_turn_skill_bundles, skills_root, refs)
    if catalog is not None:
        for ref in refs:
            if ref.version is not None:
                stored = (
                    await catalog.get_agent_skill(scope, agent, ref)
                    if agent is not None and ref in (agent.spec.skills or ())
                    else await catalog.get_skill(scope, ref.id, ref.version)
                )
                if stored is not None:
                    bundles[(ref.id, ref.version)] = stored.bundle
    if channel_skills:
        if platform is None or channel_id is None:
            raise ValueError("Channel skill loading requires an admitted platform and channel")
        for row in channel_skills:
            if (str(row.tenant_id), row.platform, row.channel_id) != (
                scope.tenant_id,
                platform,
                channel_id,
            ):
                raise ScopeViolation(row.skill_id, "channel skill is outside the admitted channel")
            if catalog is not None:
                stored = await catalog.get_channel_skill(
                    scope,
                    row.skill_id,
                    row.version,
                    platform=platform,
                    channel_id=channel_id,
                    agent_names=tuple(agent_names),
                )
                if stored is not None:
                    bundles.setdefault((row.skill_id, row.version), stored.bundle)
    return bundles
