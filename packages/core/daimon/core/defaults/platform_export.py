"""Read-only workspace recovery export; no model turns or platform mutations.

This is an operator archive, not a tenant-facing download. Preserve original
IDs and metadata for recovery mapping; never interpret remote names as paths.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from anthropic import AsyncAnthropic
from daimon.core.errors import SkillsListTruncatedError
from daimon.core.mux_backend import managed_agents, platform_scope
from daimon.core.mux_compat import legacy_call, legacy_iter
from mux.drivers.anthropic.resources.platform_export import PlatformExport
from pydantic import JsonValue


async def export_platform(client: AsyncAnthropic, destination: Path) -> None:
    """Export current agents/environments, custom skill versions and memory contents.

    Publish atomically without overwriting an existing export. An API failure
    removes the partial archive and propagates; a success manifest is written
    only after every paginator and download completes.
    """
    backend = managed_agents(client)
    native = backend.extension(PlatformExport, namespace="anthropic.platform_export", version=1)
    scope = platform_scope("defaults.platform_export", authorization_id="platform-export")
    checksums: dict[str, str] = {}
    fd, temporary = tempfile.mkstemp(prefix=".platform-export-", dir=destination.parent)
    os.close(fd)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:

            def write(name: str, content: bytes) -> None:
                archive.writestr(name, content)
                checksums[name] = hashlib.sha256(content).hexdigest()

            def write_json(name: str, value: object) -> None:
                write(name, json.dumps(value, ensure_ascii=False, indent=2).encode())

            async for agent in legacy_iter(native.agents(scope)):
                # List responses may be summaries; retrieve the full current definition.
                full = await legacy_call(native.agent(scope, str(agent["id"])))
                write_json(f"agents/{len(checksums)}.json", full)
            async for environment in legacy_iter(native.environments(scope)):
                full_env = await legacy_call(native.environment(scope, str(environment["id"])))
                write_json(f"environments/{len(checksums)}.json", full_env)
            skills: list[dict[str, JsonValue]] = []
            async for page in legacy_iter(native.skill_pages(scope, limit=1000)):
                skills.extend(page.data)
                if len(page.data) >= 1000 and not page.has_more:
                    raise SkillsListTruncatedError(
                        "skills.list returned a full page of 1000 rows — "
                        "the org skill view may be truncated; "
                        "create/delete decisions on this view are unsafe"
                    )
            for skill in skills:
                if skill["source"] != "custom":
                    continue
                prefix = f"skills/{len(checksums)}"
                write_json(f"{prefix}/skill.json", skill)
                async for version in legacy_iter(native.skill_versions(scope, str(skill["id"]))):
                    version_prefix = f"{prefix}/{len(checksums)}"
                    write_json(f"{version_prefix}/version.json", version)
                    content = await legacy_call(
                        native.download_skill_version_id(
                            scope, str(skill["id"]), str(version["id"])
                        )
                    )
                    write(f"{version_prefix}/content.zip", content)
            async for store in legacy_iter(native.memory_stores(scope)):
                prefix = f"memory-stores/{len(checksums)}"
                write_json(f"{prefix}/store.json", store)
                async for memory in legacy_iter(native.memories(scope, str(store["id"]))):
                    if memory["type"] != "memory":
                        continue
                    full_memory = await legacy_call(
                        native.memory(scope, str(store["id"]), str(memory["id"]))
                    )
                    write_json(f"{prefix}/{len(checksums)}.json", full_memory)
            archive.writestr(
                "manifest.json",
                json.dumps(
                    {
                        "format": "daimon-platform-export-v1",
                        "completed_at": datetime.now(UTC).isoformat(),
                        "sha256": checksums,
                        "excluded": [
                            "session transcripts and sandbox files",
                            "credential secrets and vault contents",
                            "historical agent/environment/memory versions",
                        ],
                        "replay": "manual ID remapping required; not a defaults tree",
                    },
                    indent=2,
                ),
            )
        # Same-filesystem hard link is atomic and fails if destination exists.
        os.link(temporary, destination)
    finally:
        os.unlink(temporary)
