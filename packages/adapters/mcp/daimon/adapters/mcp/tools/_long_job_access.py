"""One owner/admin act decision, with an independent source-content decision."""

from dataclasses import dataclass

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._session_access import source_metadata_readable
from daimon.adapters.mcp.tools.watches import fresh_admin
from daimon.core.defaults.metadata import MA_METADATA_KEY_ACCOUNT
from daimon.core.long_job_models import LongJob, LongJobRedacted
from daimon.core.session_seal import origin_stamp


@dataclass(frozen=True)
class LongJobCaller:
    runtime: McpRuntime
    auth: AuthIdentity
    is_admin: bool
    origin_context_id: str | None

    @classmethod
    async def load(
        cls, runtime: McpRuntime, auth: AuthIdentity, origin_context_id: str | None
    ) -> "LongJobCaller":
        # Native human admin attestation is fetched once per request, never per row.
        return cls(runtime, auth, await fresh_admin(runtime, auth), origin_context_id)


@dataclass(frozen=True)
class LongJobAccess:
    act: bool
    read: bool

    def view(self, job: LongJob) -> LongJob | LongJobRedacted:
        if not self.act:
            raise ValueError("A job view requires act authority")
        if self.read:
            return job
        return LongJobRedacted(
            id=job.id,
            state=job.state,
            owner_account_id=job.owner_account_id,
            created_at=job.created_at,
            deadline_at=job.deadline_at,
        )


async def long_job_access(caller: LongJobCaller, job: LongJob | None) -> LongJobAccess:
    """Tenant first; owner/current human admin may act; source policy alone grants read."""
    act = (
        job is not None
        and caller.auth.tenant_id == job.tenant_id
        and (caller.auth.account_id == job.owner_account_id or caller.is_admin)
    )
    if not act or job is None:
        return LongJobAccess(act=False, read=False)
    metadata = {
        **origin_stamp(channel_id=job.parent_channel_id, thread_id=job.thread_id),
        **job.source_metadata,
        MA_METADATA_KEY_ACCOUNT: str(job.owner_account_id),
    }
    try:
        readable = await source_metadata_readable(
            caller.runtime, caller.auth, metadata, origin_context_id=caller.origin_context_id
        )
    except Exception:
        # A failed source proof redacts reads; it must never prevent stopping work.
        readable = False
    return LongJobAccess(act=True, read=readable)
