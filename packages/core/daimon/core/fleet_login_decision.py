"""A fleet run's one decision about which stored logins its workers get (PR B).

The root freezes the candidates (`WorkerPermissions.login_candidates`). At the run's
first spawn, a candidate without a saved share that still holds is asked about: the
spawn opens the run's login question (the chat adapter posts it as a card), and waits
at most `LOGIN_WAIT_S` for the requester's answer. No worker is dispatched before the
decision or the timeout; a timeout records "no answer" (= skip) for every row left.
The decision is written once, from null, and never widened. Every reader of the
frozen root then hands a worker only decision ∩ candidates (`apply_run_decision`);
the approved row version still pins the hand-over itself.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, cast

import structlog
from daimon.core.fleet_credentials import LoginCandidate, may_share_for_run

if TYPE_CHECKING:
    from daimon.core.neutral_resources import WorkerPermissions
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

#: How long a run's first spawn waits for the requester's answer.
LOGIN_WAIT_S = 60.0
_POLL_S = 1.0

#: A button: share for this run (and save "share"), skip (and save "skip"), or share
#: this time only (and save "ask"). Saving happens only for the owner or an admin.
LoginChoice = Literal["share", "skip", "share_once"]
_SAVED: dict[str, Literal["share", "skip", "ask"]] = {
    "share": "share",
    "skip": "skip",
    "share_once": "ask",
}

Refusal = Literal[
    "no_question",
    "already_decided",
    "run_ended",
    "stale_card",
    "not_requester",
    "expired",
    "unknown_row",
    "share_not_allowed",
]


def pending_candidates(parent: WorkerPermissions) -> tuple[LoginCandidate, ...]:
    """The candidates the run still has to ask about (no saved share that holds)."""
    return tuple(c for c in parent.login_candidates if not c.preapproved)


def spawn_waits_for_logins(parent: WorkerPermissions) -> bool:
    """Whether a spawn must ask and wait (≤ `LOGIN_WAIT_S`) before dispatching.

    Never while sharing isn't live: there is no card to answer, so a wait would only
    delay every worker; the fleet card already says the workers run without them.
    """
    from daimon.core.turn.runtime_manifest import LOGIN_SHARING_LIVE

    return LOGIN_SHARING_LIVE and bool(pending_candidates(parent))


def apply_run_decision(
    parent: WorkerPermissions, decision: Mapping[str, str] | None
) -> WorkerPermissions:
    """The frozen root as its workers see it: decision ∩ the frozen candidates.

    Undecided shares only the saved shares that still held at freeze; a key the run
    never froze as a candidate is never shared, whatever the decision says.
    """
    candidates = parent.login_candidates
    if not candidates:
        return parent
    answers = decision or {}
    shared = frozenset(c.key for c in candidates if c.preapproved or answers.get(c.key) == "share")
    lines = tuple(
        f"{c.label} (no answer, so not shared)"
        if answers.get(c.key) == "no_answer"
        else f"{c.label} (skipped for this run)"
        for c in candidates
        if c.key not in shared and decision is not None
    )
    return replace(
        parent,
        shared_logins=shared,
        shared_versions=tuple(
            (key, version) for key, version in parent.shared_versions if key in shared
        ),
        workers_run_without=(*parent.workers_run_without, *lines),
    )


def question_digest(run_id: uuid.UUID, rows: list[dict[str, Any]]) -> str:
    """Binds a card to the exact rows it was posted for (names and versions)."""
    body = json.dumps(
        [str(run_id), [[row["key"], row["label"], row["version"]] for row in rows]],
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode()).hexdigest()


def new_question(parent: WorkerPermissions, *, now: datetime) -> dict[str, Any]:
    rows = [
        {
            "key": c.key,
            "label": c.label,
            "version": c.version,
            "installed_by": c.installed_by,
            "set_by": c.set_by,
        }
        for c in pending_candidates(parent)
    ]
    facts = parent.fleet
    return {
        "agent_id": str(parent.agent_id) if parent.agent_id else None,
        "requester_platform_user_id": parent.subject.platform_user_id,
        "platform": facts.root.channel.platform,
        "channel_id": facts.root.channel.channel_id,
        "thread_id": facts.root.thread_id,
        "rows": rows,
        # The logins the workers run without regardless (for the card's last lines).
        "without": list(parent.workers_run_without),
        # Never past the run's own deadline (the run's limits are its deadline).
        "expires_at": min(
            now + timedelta(seconds=LOGIN_WAIT_S), parent.limits.deadline
        ).isoformat(),
        "digest": question_digest(facts.run_id, rows),
        "choices": {},
        "card_message_id": None,
        "state": "open",
    }


def _expires(question: Mapping[str, Any]) -> datetime:
    try:
        return datetime.fromisoformat(str(question["expires_at"]))
    except (KeyError, ValueError):
        return datetime.min.replace(tzinfo=UTC)  # malformed reads as expired


def _decided(question: Mapping[str, Any], *, final: bool) -> dict[str, str] | None:
    """The decision once every row is answered (or, when `final`, the rest no_answer)."""
    choices = cast(dict[str, str], question.get("choices") or {})
    keys = [str(row["key"]) for row in question.get("rows", ())]
    if not final and not all(key in choices for key in keys):
        return None
    return {
        key: "share"
        if choices.get(key) in ("share", "share_once")
        else "skip"
        if key in choices
        else "no_answer"
        for key in keys
    }


async def expire_question(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    now: datetime,
) -> dict[str, Any] | None:
    """Past its deadline with no decision: record no_answer for every row left.

    Returns the final question (for the card's "No answer, so workers ran without"),
    or None when there was nothing to expire.
    """
    from daimon.core.stores import fleet

    async with sessionmaker.begin() as session:
        state = await fleet.lock_login_state(session, tenant_id=tenant_id, run_id=run_id)
        if state is None:
            return None
        _, _, question, decision = state
        if question is None or decision is not None or now < _expires(question):
            return None
        question["state"] = "expired"
        await fleet.write_login_state(
            session,
            tenant_id=tenant_id,
            run_id=run_id,
            question=question,
            decision=_decided(question, final=True),
        )
    log.info("fleet.login_question_expired", run_id=str(run_id))
    return question


async def await_run_decision(
    sessionmaker: async_sessionmaker[AsyncSession],
    parent: WorkerPermissions,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
) -> dict[str, str] | None:
    """Before a run's spawn: return its login decision, asking and waiting if needed.

    Nothing to ask returns at once. Otherwise the run's one question is opened (parallel
    spawns share it) and polled from the database (the answer may land in another
    process) until decided or `LOGIN_WAIT_S` passes, which records no_answer. A cancelled
    spawn stops waiting and dispatches nothing; so does a run that ends meanwhile
    (None, and the caller's re-read of the root refuses it).
    """
    from daimon.core.stores import fleet

    if not pending_candidates(parent):
        return None
    tenant_id, run_id = uuid.UUID(parent.fleet.scope.tenant_id), parent.fleet.run_id
    decision = await fleet.run_login_decision(sessionmaker, tenant_id=tenant_id, run_id=run_id)
    if decision is not None:
        return decision
    question = await fleet.open_login_question(
        sessionmaker,
        tenant_id=tenant_id,
        run_id=run_id,
        requester_account_id=parent.fleet.account_id,
        question=new_question(parent, now=now()),
    )
    if question is not None:
        expires = _expires(question)
        while (left := (expires - now()).total_seconds()) > 0:
            await sleep(min(_POLL_S, left))
            running, decision = await fleet.run_login_poll(
                sessionmaker, tenant_id=tenant_id, run_id=run_id
            )
            if decision is not None:
                return decision
            if not running:
                return None  # the run ended (stopped, deadline): nothing to dispatch
        await expire_question(sessionmaker, tenant_id=tenant_id, run_id=run_id, now=now())
    return await fleet.run_login_decision(sessionmaker, tenant_id=tenant_id, run_id=run_id)


@dataclass(frozen=True)
class LoginAnswer:
    """What a click did: the question as it now stands, or why it changed nothing."""

    question: dict[str, Any] | None
    refused: Refusal | None = None
    decided: bool = False
    saved: bool = False


async def choose_login(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    digest: str,
    actor_account_id: uuid.UUID,
    actor_platform_user_id: str | None,
    is_admin: bool,
    row: int | Literal["all"],
    choice: LoginChoice,
    now: datetime,
    save: Callable[[Mapping[str, Literal["share", "skip", "ask"]]], Awaitable[object]]
    | None = None,
) -> LoginAnswer:
    """One click on the run's login card, re-checked here at click time.

    Only the run's requester, on the card for these exact rows, before the deadline
    and before the run is decided. "share" needs `may_share_for_run` for every row it
    covers (a member may share only a key they set themselves). "all" answers every
    row not yet answered. Every click is audited as the CLICKER, refusals included
    with their reason, by login key (never a value). When every row has an answer,
    the run is decided (once, from null). An owner's or admin's click is also saved
    to the agent config through `save` (`set_credential_sharing`); a member's applies
    to this run only.
    """
    from daimon.core.stores import agent_owners, fleet, security_audit

    async with sessionmaker.begin() as session:
        state = await fleet.lock_login_state(session, tenant_id=tenant_id, run_id=run_id)
        question = state[2] if state is not None else None
        agent_id = question.get("agent_id") if question is not None else None
        role = "admin" if is_admin else "member"

        async def audit(
            outcome: Literal["allowed", "denied"], keys: Sequence[str], reason: str
        ) -> None:
            for key in keys or ["-"]:
                await security_audit.append_event(
                    session,
                    tenant_id=tenant_id,
                    account_id=actor_account_id,
                    agent_id=uuid.UUID(agent_id) if agent_id else None,
                    platform=question.get("platform") if question is not None else None,
                    platform_user_id=actor_platform_user_id,
                    tool_name="fleet_logins",
                    operation=choice,
                    outcome=outcome,
                    reason=f"{reason}; login={key}; role={role}; run={run_id}; row={row}",
                )

        async def refuse(reason: Refusal, keys: Sequence[str] = ()) -> LoginAnswer:
            await audit("denied", keys, f"refused={reason}")
            return LoginAnswer(question, reason)

        if state is None or question is None:
            return await refuse("no_question")
        requester, status, _, decision = state
        refused: Refusal | None = (
            "already_decided"
            if decision is not None
            else "run_ended"
            if status not in ("running", "collecting")
            else "stale_card"
            if question.get("digest") != digest
            else "not_requester"
            if actor_account_id != requester
            else "expired"
            if now >= _expires(question)
            else None
        )
        if refused is not None:
            return await refuse(refused)
        rows = cast(list[dict[str, Any]], question.get("rows") or [])
        choices = cast(dict[str, str], question.setdefault("choices", {}))
        if row == "all":
            targets = [r for r in rows if r["key"] not in choices]
        elif 0 <= row < len(rows):
            targets = [rows[row]]
        else:
            return await refuse("unknown_row")
        keys = [str(target["key"]) for target in targets]
        owner = (
            await agent_owners.owner_of(session, tenant_id, uuid.UUID(agent_id))
            if agent_id
            else None
        )
        is_owner = owner is not None and owner == actor_account_id
        role = "admin" if is_admin else "owner" if is_owner else "member"
        if choice != "skip":
            for target in targets:
                candidate = LoginCandidate(
                    key=target["key"],
                    label=target["label"],
                    version=target["version"],
                    preapproved=False,
                    installed_by=target["installed_by"],
                    set_by=target.get("set_by"),
                )
                if not may_share_for_run(
                    candidate,
                    actor_account_id=actor_account_id,
                    is_admin=is_admin,
                    is_owner=is_owner,
                ):
                    return await refuse("share_not_allowed", [str(target["key"])])
        for key in keys:
            choices[key] = choice
        await audit("allowed", keys, "applied")
        decided = _decided(question, final=False)
        if decided is not None:
            question["state"] = "decided"
        await fleet.write_login_state(
            session, tenant_id=tenant_id, run_id=run_id, question=question, decision=decided
        )
    saved = False
    if save is not None and (is_owner or is_admin) and keys:
        try:
            await save({key: _SAVED[choice] for key in keys})
            saved = True
        except Exception as error:  # the run's decision stands; only the saved default failed
            log.warning("fleet.login_save_failed", error_type=type(error).__name__)
    return LoginAnswer(question, decided=decided is not None, saved=saved)
