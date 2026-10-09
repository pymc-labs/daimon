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

from anthropic import AsyncAnthropic, omit
from daimon.core.defaults.ma_index import list_skills_strict


async def export_platform(client: AsyncAnthropic, destination: Path) -> None:
    """Export current agents/environments, custom skill versions and memory contents.

    Publish atomically without overwriting an existing export. An API failure
    removes the partial archive and propagates; a success manifest is written
    only after every paginator and download completes.
    """
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

            async for agent in client.beta.agents.list():
                # List responses may be summaries; retrieve the full current definition.
                full = await client.beta.agents.retrieve(agent.id)
                write_json(f"agents/{len(checksums)}.json", full.model_dump(mode="json"))
            async for environment in client.beta.environments.list():
                full_env = await client.beta.environments.retrieve(environment.id)
                write_json(f"environments/{len(checksums)}.json", full_env.model_dump(mode="json"))
            for skill in await list_skills_strict(client):
                if skill.source != "custom":
                    continue
                prefix = f"skills/{len(checksums)}"
                write_json(f"{prefix}/skill.json", skill.model_dump(mode="json"))
                async for version in client.beta.skills.versions.list(skill.id):
                    version_prefix = f"{prefix}/{len(checksums)}"
                    write_json(f"{version_prefix}/version.json", version.model_dump(mode="json"))
                    content = await client.beta.skills.versions.download(
                        version.id, skill_id=skill.id, extra_headers={"anthropic-beta": omit}
                    )
                    try:
                        write(f"{version_prefix}/content.zip", await content.read())
                    finally:
                        await content.close()
            async for store in client.beta.memory_stores.list():
                prefix = f"memory-stores/{len(checksums)}"
                write_json(f"{prefix}/store.json", store.model_dump(mode="json"))
                async for memory in client.beta.memory_stores.memories.list(
                    store.id, path_prefix="/"
                ):
                    if memory.type != "memory":
                        continue
                    full_memory = await client.beta.memory_stores.memories.retrieve(
                        memory.id, memory_store_id=store.id, view="full"
                    )
                    write_json(
                        f"{prefix}/{len(checksums)}.json", full_memory.model_dump(mode="json")
                    )
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
