"""End-to-end skill sync pipeline: fetch repo -> discover -> sync to MA."""

from __future__ import annotations

import shutil
import uuid

import httpx
import structlog
from anthropic import AsyncAnthropic
from daimon.core.defaults.report import ResourceOutcome
from daimon.core.errors import DaimonError
from daimon.core.skills.discover import discover_skills
from daimon.core.skills.fetch import fetch_repo
from daimon.core.skills.sync import sync_skills

_log = structlog.get_logger(__name__)


async def run_skill_sync(
    client: AsyncAnthropic,
    http_client: httpx.AsyncClient,
    *,
    url: str,
    branch: str = "main",
    path: str = "",
    tenant_id: uuid.UUID,
    token: str | None = None,
    max_tarball_bytes: int = 50 * 1024 * 1024,
    max_tarball_decompressed_bytes: int = 200 * 1024 * 1024,
) -> list[ResourceOutcome]:
    """Fetch a GitHub repo, discover SKILL.md files, and sync them to MA.

    The full pipeline: ``fetch_repo`` -> path scoping + validation ->
    ``discover_skills`` -> ``sync_skills``. Temp directory is cleaned up
    in a ``finally`` block even on error.

    Skills are created/updated under tenant-prefixed display_titles
    (``{t8}-{name}``), so two tenants syncing the same-named skill get
    distinct MA resources.

    Args:
        client: Anthropic SDK client for MA API calls.
        http_client: httpx client for GitHub tarball download (injected per D-03).
        url: GitHub repo URL.
        branch: Git branch to fetch (default ``"main"``).
        path: Optional subdirectory to scope discovery to. Empty string means
              discover from repo root.
        tenant_id: Owning tenant — determines the canonical title prefix.
        max_tarball_bytes: Raw tarball size cap passed through to ``fetch_repo``
            (RATE-03, D-13). Defaults to the safe 50 MiB constant.
        max_tarball_decompressed_bytes: Decompressed size cap passed through to
            ``fetch_repo``. Defaults to the safe 200 MiB constant.

    Raises:
        DaimonError: If ``path`` escapes the repo root or does not exist.
    """
    result = await fetch_repo(
        http_client,
        url,
        branch=branch,
        token=token,
        max_tarball_bytes=max_tarball_bytes,
        max_tarball_decompressed_bytes=max_tarball_decompressed_bytes,
    )
    try:
        discover_root = result.path / path if path else result.path
        if path:
            try:
                discover_root.resolve().relative_to(result.path.resolve())
            except ValueError as exc:
                raise DaimonError(f"path {path!r} escapes the repository root") from exc
            if not discover_root.exists():
                raise DaimonError(f"path {path!r} not found in fetched repository")
        found = discover_skills(discover_root)
        return await sync_skills(client, found, tenant_id=tenant_id)
    finally:
        shutil.rmtree(result.cleanup_dir, ignore_errors=True)
