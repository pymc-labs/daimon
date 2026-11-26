"""The agent's keys on an OpenAI or Gemini reply: the `.env` a Claude session mounts.

The same rows, bytes and hash (`assemble_env_bytes`, `hash_env_bytes`), placed in the
provider's sandbox at `PATH`: OpenAI mounts files only under `/workspace`. Where a
Claude session mounts none (`agent_env_file` Off, no keys), or an admin turned
`provider_keys_file` off, or the reply is a self-helper's copy, there is none.
Logs name only the hash, never a key or its value.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from daimon.core.credential_env import assemble_env_bytes, session_env_key
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.self_helpers import is_self_helper_thread
from daimon.core.session_snapshot import hash_env_bytes
from daimon.core.stores.agent_files import list_agent_files
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

if TYPE_CHECKING:
    from daimon.core.turn.prepare import ProviderPreparationRequest

#: Where an OpenAI or Gemini sandbox holds the keys file.
PATH = "/workspace/.daimon/.env"


@dataclass(frozen=True)
class EnvFile:
    body: bytes = field(repr=False)
    sha256: str


async def agent_env_file(
    sessionmaker: async_sessionmaker[AsyncSession], *, tenant_id: uuid.UUID, agent_id: uuid.UUID
) -> EnvFile | None:
    """The `.env` a session mounts for this agent, or None for an agent with no keys.

    The tenant-scoped rows `upload_env_and_mount` reads, less the GitHub tokens an
    App session leaves out.
    """
    async with sessionmaker() as session:
        rows = await list_agent_files(session, tenant_id=tenant_id, agent_id=agent_id)
    rows = [row for row in rows if session_env_key(row.key, exclude_github=True)]
    if not rows:
        return None
    body = assemble_env_bytes(rows)
    return EnvFile(body=body, sha256=hash_env_bytes(body))


async def turn_env_file(request: ProviderPreparationRequest) -> EnvFile | None:
    """The keys file this provider reply's sandbox holds, or None for none."""
    from daimon.core.features.snapshot import deployment_on_everywhere

    settings = request.deps.settings
    if (
        not request.admission.toolset.env_file
        or is_self_helper_thread(request.thread_id)
        or (
            settings is not None
            and not deployment_on_everywhere(
                settings, "provider_keys_file", request.answers_on or (request.platform,)
            )
        )
    ):
        return None
    tenant_id = request.tenant_id
    agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=request.admission.agent.id)
    return await agent_env_file(request.deps.sessionmaker, tenant_id=tenant_id, agent_id=agent_id)
