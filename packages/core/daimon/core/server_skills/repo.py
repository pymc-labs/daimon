"""Skills a GitHub repository holds, as a source for the mirror sync.

A mirror's `server` is `repo:<owner>/<name>@<branch>` and its `uri` is
`repo:///<folder>/SKILL.md`. Private repositories are read with the workspace's own GitHub
App access (a workspace-wide authorized repository); any other repository is read without a
token, so only a public one answers. A push to the branch resyncs through the existing push
queue, and an update that adds or changes a runnable file waits for approval like any mirror.
"""

from __future__ import annotations

import asyncio
import hashlib
import tempfile
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import httpx
import structlog
from daimon.core.github_app_auth import build_app_jwt, mint_installation_token
from daimon.core.github_rate_limit import GitHubRateLimitError
from daimon.core.mcp_skills.install import ServerSkill
from daimon.core.mcp_skills.manifest import (
    SkillEntry,
    SkillRejected,
    parse_entry,
    read_frontmatter,
    verify_file,
    verify_frontmatter,
)
from daimon.core.server_skills.sync import UpstreamGone, UpstreamUnreachable
from daimon.core.skill_sync.bundler import (
    DEFAULT_MAX_TARBALL_DECOMPRESSED_BYTES,
    extract_and_bundle,
)
from daimon.core.skill_sync.bundler import SkillEntry as BundledSkill
from daimon.core.skill_sync.fetcher import (
    GitHubAuthError,
    GitHubTarballFetcher,
    GitHubUnreachable,
    TarballTooLarge,
)
from daimon.core.skill_zip import MAX_FILES, MAX_UNCOMPRESSED_BYTES
from daimon.core.stores import github_app_installations
from daimon.core.stores.domain import ServerSkillRow
from daimon.core.stores.github_access import list_authorized_repos
from daimon.core.stores.github_installation_coverage import installation_covers_repo
from pydantic import JsonValue, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = ["REPO_PREFIX", "RepoUpstream", "parse_repo_server", "repo_server"]

log = structlog.get_logger(__name__)

REPO_PREFIX = "repo:"


def repo_server(repo: str, branch: str) -> str:
    """The `server` name of mirrors of `owner/name` at `branch`."""
    return f"{REPO_PREFIX}{repo}@{branch}"


def parse_repo_server(server: str) -> tuple[str, str] | None:
    """`(owner/name, branch)` of a repo mirror's server name, else None."""
    if not server.startswith(REPO_PREFIX):
        return None
    repo, sep, branch = server.removeprefix(REPO_PREFIX).partition("@")
    return (repo, branch) if sep and repo.count("/") == 1 and branch else None


def _read_tree(found: Sequence[BundledSkill]) -> dict[str, dict[str, bytes]]:
    """The files under each skill folder, by folder relative to the repository root.

    A folder over the provider's size or file limits is left out, so one huge folder can't
    hold the whole repository's memory. A root-level SKILL.md is not supported.
    """
    tree: dict[str, dict[str, bytes]] = {}
    for item in found:
        if not item.source_rel or item.skip_reason is not None:
            continue
        paths = sorted(p for p in item.skill_dir.rglob("*") if p.is_file())
        if len(paths) > MAX_FILES or sum(p.stat().st_size for p in paths) > MAX_UNCOMPRESSED_BYTES:
            log.warning("repo_skill.too_large", folder=item.source_rel)
            continue
        tree[item.source_rel] = {
            p.relative_to(item.skill_dir).as_posix(): p.read_bytes() for p in paths
        }
    return tree


def _entry_of(folder: str, files: dict[str, bytes]) -> SkillEntry | None:
    """The checked listing entry of one folder, or None when it is not a valid skill."""
    if "SKILL.md" not in files:
        return None
    base = f"repo:///{folder}"
    resources: list[JsonValue] = [
        {
            "uri": f"{base}/{path}",
            "digest": f"sha256:{hashlib.sha256(body).hexdigest()}",
            "size": len(body),
        }
        for path, body in sorted(files.items())
    ]
    try:
        frontmatter = read_frontmatter(files["SKILL.md"])
        raw: JsonValue = {
            "uri": f"{base}/SKILL.md",
            "frontmatter": frontmatter,  # pyright: ignore[reportAssignmentType]  # plain YAML
            "resources": resources,
        }
        return parse_entry(raw)
    except SkillRejected as err:
        log.info("repo_skill.skipped", folder=folder, reason=str(err))
        return None


@dataclass
class RepoUpstream:
    """One repository branch, read for a workspace; the repository is fetched once per instance."""

    tenant_id: uuid.UUID
    repo: str
    branch: str
    sessionmaker: async_sessionmaker[AsyncSession]
    http: httpx.AsyncClient
    app_id: str | None
    app_private_key: SecretStr | None
    now: Callable[[], datetime]
    max_tarball_bytes: int = 50 * 1024 * 1024
    max_tarball_decompressed_bytes: int = DEFAULT_MAX_TARBALL_DECOMPRESSED_BYTES
    _tree: dict[str, dict[str, bytes]] | None = field(default=None, init=False, repr=False)

    async def _credential(self) -> str | None:
        """An App token when the workspace authorized this repository, else None (public)."""
        if self.app_id is None or self.app_private_key is None:
            return None
        async with self.sessionmaker() as session:
            authorized = next(
                (
                    r
                    for r in await list_authorized_repos(session, tenant_id=self.tenant_id)
                    if r.repo_full_name.casefold() == self.repo.casefold() and r.status == "active"
                ),
                None,
            )
            if authorized is None:
                return None
            installation = await github_app_installations.get(
                session, installation_id=authorized.installation_id
            )
        if not installation_covers_repo(installation, authorized.repo_full_name):
            return None
        return await mint_installation_token(
            self.http,
            jwt=build_app_jwt(
                self.app_private_key.get_secret_value(),
                self.app_id,
                now=int(self.now().timestamp()),
            ),
            installation_id=authorized.installation_id,
            repository=authorized.repo_full_name.split("/", 1)[1],
            permissions={"contents": "read"},
        )

    async def _load(self) -> dict[str, dict[str, bytes]]:
        if self._tree is not None:
            return self._tree
        try:
            tarball = await GitHubTarballFetcher(
                self.http, max_tarball_bytes=self.max_tarball_bytes
            ).fetch_tarball(credential=await self._credential(), url=self.repo, branch=self.branch)
            with tempfile.TemporaryDirectory() as tmp:
                found = await extract_and_bundle(
                    tarball_bytes=tarball,
                    extract_root=Path(tmp),
                    repo_name=self.repo.split("/", 1)[1],
                    split=True,
                    max_tarball_decompressed_bytes=self.max_tarball_decompressed_bytes,
                )
                self._tree = await asyncio.to_thread(_read_tree, found)
        except (GitHubAuthError, GitHubUnreachable) as err:
            raise UpstreamGone(type(err).__name__) from err
        except (TarballTooLarge, GitHubRateLimitError, httpx.HTTPError) as err:
            raise UpstreamUnreachable(type(err).__name__) from err
        return self._tree

    async def listing(self, *, tenant_id: uuid.UUID) -> Sequence[SkillEntry]:
        """Every valid skill folder at the branch head."""
        tree = await self._load()
        return [e for folder, files in sorted(tree.items()) if (e := _entry_of(folder, files))]

    async def entry(self, mirror: ServerSkillRow) -> SkillEntry | None:
        listed = await self.listing(tenant_id=mirror.tenant_id)
        return next((e for e in listed if e.uri == mirror.uri), None)

    async def fetch(self, mirror: ServerSkillRow) -> ServerSkill:
        return await self.fetch_uri(mirror.uri)

    async def fetch_uri(self, uri: str) -> ServerSkill:
        """Every file of the skill at `uri`, checked against its listing entry."""
        entry = next(
            (e for e in await self.listing(tenant_id=self.tenant_id) if e.uri == uri), None
        )
        if entry is None:
            raise UpstreamGone("the repository no longer holds it")
        files = (await self._load())[uri.removeprefix("repo:///").removesuffix("/SKILL.md")]
        try:
            for item in entry.files:
                verify_file(item, files[item.path])
            verify_frontmatter(entry, files["SKILL.md"])
        except SkillRejected as err:
            raise UpstreamUnreachable(str(err)) from err
        return ServerSkill(entry=entry, files=dict(files))
