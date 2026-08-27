"""Stage two of the two-stage turn chokepoint (D-01): `bind_session()`.

A single core call performs thread-session find-or-create, the full
`create_session` kwarg assembly, the `thread_sessions` mapping write, and
usage-recorder binding — returning a frozen `PreparedTurn` whose recorder is
a non-public field. Adapters never see or construct billing wiring.

`fernet=deps.fernet` is unconditional here: this is the fix for SPEC Req
7(a), the historical Slack gap where `create_session` was called without a
`fernet` argument.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import functools
import uuid
from collections.abc import Callable
from dataclasses import dataclass

import structlog
from daimon.core.agent_mcp_credentials import sync_agent_mcp_credentials
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.pricing import MODEL_PRICING
from daimon.core.sessions import create_session
from daimon.core.stores.thread_sessions import create_thread_session, get_live_thread_session
from daimon.core.turn.admission import Admission
from daimon.core.turn.ceiling import ceiling_error, remaining_s, turn_deadline
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.posture import UsageRecorder
from daimon.core.usage_recording import record_turn_usage

log = structlog.get_logger(__name__)

__all__ = ["PreparedTurn", "bind_session"]


@dataclass(frozen=True)
class PreparedTurn:
    """Everything `run_prepared_turn` (06-05) needs to drive a turn.

    `_record` is intentionally underscore-prefixed and excluded from the
    public contract adapters consume — the recorder is reachable only
    through `run_prepared_turn`.
    """

    admission: Admission
    ma_session_id: str
    mapping_id: uuid.UUID | None
    watermark: str | None
    reused: bool
    session_account_id: uuid.UUID
    _record: UsageRecorder


async def create_fresh_session(
    deps: TurnDeps,
    admission: Admission,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    thread_id: str,
    session_account_id: uuid.UUID,
) -> tuple[str, uuid.UUID]:
    """Create a brand-new MA session and its `thread_sessions` mapping row.

    The single shared `create_session` call site for a fresh session. Both
    `bind_session`'s no-live-row path and 06-05's dead-session recovery cycle
    call this helper rather than each carrying their own `create_session`
    call -- a divergent second call site is exactly the bug shape this phase
    exists to kill.

    Returns `(ma_session_id, mapping_id)`.
    """
    agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=str(admission.agent.id))
    ma_session = await create_session(
        deps.anthropic,
        agent=admission.agent,
        environment=admission.environment,
        mcp_settings=deps.mcp,
        account_id=admission.account_id,
        tenant_id=tenant_id,
        agent_uuid=agent_uuid,
        session_factory=deps.sessionmaker,
        fernet=deps.fernet,
        github_fallback_pat=deps.github_fallback_pat,
        github_app_id=deps.github_app_id,
        github_app_private_key=deps.github_app_private_key,
    )
    ma_session_id = ma_session.id

    async with deps.sessionmaker() as session:
        row = await create_thread_session(
            session,
            tenant_id=tenant_id,
            platform=platform,
            thread_id=thread_id,
            account_id=session_account_id,
            ma_session_id=ma_session_id,
        )
        await session.commit()

    return ma_session_id, row.id


def bind_recorder(
    deps: TurnDeps,
    admission: Admission,
    *,
    tenant_id: uuid.UUID,
    external_user_id: str,
    ma_session_id: str,
) -> UsageRecorder:
    """Build the usage recorder bound to a specific session id.

    Factored as a module-level helper (not inlined in `bind_session`) so
    06-05's dead-session recovery cycle can re-invoke it against the NEW
    session id after a recreate, rather than reusing a stale binding.
    """
    return functools.partial(
        record_turn_usage,
        sessionmaker=deps.sessionmaker,
        platform_user_id=external_user_id,
        managed_session_id=ma_session_id,
        model_id=admission.agent.model.id,
        tenant_id=tenant_id,
        markup=deps.markup,
        pricing=MODEL_PRICING.get(admission.agent.model.id),
    )


async def bind_session(
    deps: TurnDeps,
    admission: Admission,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    external_user_id: str,
    thread_id: str,
    session_account_id: uuid.UUID,
    reuse_existing: bool,
    deadline: dt.datetime | None = None,
    now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
) -> PreparedTurn:
    """Find-or-create the MA session for this turn and bind its recorder.

    When `reuse_existing` is True, a live `thread_sessions` row for
    (tenant_id, platform, thread_id, session_account_id) is reused verbatim
    (no `create_session` call). Otherwise (no live row, or
    `reuse_existing=False` for Discord's channel-mention path) a fresh
    session is created via the single shared `create_session` call site,
    always passing `fernet=deps.fernet`, and a new `thread_sessions` mapping
    row is written.

    `deadline`/`now` bound this whole body against the per-turn ceiling
    (D-03): the MA `sessions.create` call and the mapping write, and on the
    reuse path `sync_agent_mcp_credentials`. This does NOT cover `admit()`,
    which runs before `bind_session` and is deliberately unbounded (D-04).
    `deadline=None` is fail-safe, not off -- it computes
    `turn_deadline(now=now())` so every caller (including one that never
    passes a deadline) is still ceiling-covered.

    Raises `TypeError` if `admission` is not a real `Admission` -- pyright's
    strict mode already rejects a mistyped caller at type-check time; this
    guard makes the same contract hold at runtime (the type-level chokepoint
    claim tested by 06-05's `test_bind_session_requires_an_admission_value`).
    This check runs BEFORE the ceiling wrap, so a mistyped caller still fails
    immediately with the same error rather than waiting out a timeout.
    """
    if not isinstance(admission, Admission):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise TypeError(
            f"bind_session requires an Admission instance, got {type(admission).__name__}"
        )

    effective_deadline = deadline if deadline is not None else turn_deadline(now=now())

    async def _bind() -> PreparedTurn:
        ma_session_id: str | None = None
        mapping_id: uuid.UUID | None = None
        watermark: str | None = None
        reused = False

        if reuse_existing:
            async with deps.sessionmaker() as session:
                existing = await get_live_thread_session(
                    session,
                    tenant_id=tenant_id,
                    platform=platform,
                    thread_id=thread_id,
                    account_id=session_account_id,
                )
            if existing is not None:
                ma_session_id = existing.ma_session_id
                mapping_id = existing.id
                watermark = existing.watermark_message_id
                reused = True
                # A reused session skips create_session, so it would never pick
                # up an external MCP credential added to the agent after it was
                # created — the caller would keep failing at MCP init until
                # their session happened to be recreated. The vault this
                # session already mounts is read at each turn's MCP init, so
                # writing into it here reaches this session on this turn.
                if (
                    deps.fernet is not None
                    and deps.mcp.public_url is not None
                    and deps.mcp.jwt_secret is not None
                ):
                    await sync_agent_mcp_credentials(
                        deps.anthropic,
                        sessionmaker=deps.sessionmaker,
                        fernet=deps.fernet,
                        tenant_id=tenant_id,
                        agent_id=derive_agent_uuid(
                            tenant_id=tenant_id, ma_agent_id=str(admission.agent.id)
                        ),
                        account_id=admission.account_id,
                        jwt_secret=deps.mcp.jwt_secret.get_secret_value().encode(),
                        public_url=str(deps.mcp.public_url),
                        now=dt.datetime.now(dt.UTC),
                    )

        if not reused:
            ma_session_id, mapping_id = await create_fresh_session(
                deps,
                admission,
                tenant_id=tenant_id,
                platform=platform,
                thread_id=thread_id,
                session_account_id=session_account_id,
            )
            watermark = None

        assert ma_session_id is not None, "ma_session_id must be resolved on every code path"

        record = bind_recorder(
            deps,
            admission,
            tenant_id=tenant_id,
            external_user_id=external_user_id,
            ma_session_id=ma_session_id,
        )

        return PreparedTurn(
            admission=admission,
            ma_session_id=ma_session_id,
            mapping_id=mapping_id,
            watermark=watermark,
            reused=reused,
            session_account_id=session_account_id,
            _record=record,
        )

    try:
        return await asyncio.wait_for(_bind(), timeout=remaining_s(effective_deadline, now=now()))
    except TimeoutError as err:
        log.error(
            "turn.ceiling_exceeded",
            phase="bind_session",
            tenant_id=str(tenant_id),
            platform=platform,
            thread_id=thread_id,
            deadline=effective_deadline.isoformat(),
        )
        raise ceiling_error() from err
