"""Process-scoped collaborators captured by tool-registration closures.

FastMCP registers top-level functions, so tools can't take DB / client as
positional args. Closures over an McpRuntime are the injection point;
AuthIdentity flows separately via ctx.get_state.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from anthropic import AsyncAnthropic
from cryptography.fernet import MultiFernet
from daimon.core.artifacts import ArtifactStore
from daimon.core.config import Settings
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import DeploymentDefault
from google import genai
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@dataclass(frozen=True)
class McpRuntime:
    session_factory: async_sessionmaker[AsyncSession]
    client: AsyncAnthropic
    settings: Settings
    deployment_default: DeploymentDefault
    gemini_client: genai.Client | None = None
    notebook_rate_limiter: RateLimiter | None = None
    bundle_rate_limiter: RateLimiter | None = None
    fernet: MultiFernet | None = field(default=None)
    artifact_store: ArtifactStore | None = field(default=None)
