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
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, cast

import structlog
from daimon.core.fleet_credentials import LoginCandidate, may_share_for_run

if TYPE_CHECKING:
    from daimon.core.neutral_resources import WorkerPermissions
    from daimon.core.turn.deps import TurnDeps
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

#: How long a run's first spawn waits for the requester's answer.
LOGIN_WAIT_S = 60.0
_POLL_S = 1.0
_DIGEST_PREFIX = 12
_RUNNING = ("running", "collecting")

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
    "share_not_allowed",
    "bad_choice",
    "rate_limited",
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
        "agent_name": next((name for name in parent.agent.names if name), None),
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
) -> LoginCard | None:
    """Past its deadline with no decision: record no_answer for every row left.

    Returns the final card (for "No answer, so workers ran without: …"), or None
    when there was nothing to expire (already decided, not yet due).
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
        decided = _decided(question, final=True)
        await fleet.write_login_state(
            session, tenant_id=tenant_id, run_id=run_id, question=question, decision=decided
        )
    log.info("fleet.login_question_expired", run_id=str(run_id))
    return login_card(tenant_id, run_id, question, decided, running=True)


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
class LoginCardRow:
    index: int
    label: str
    # share | skip | share_once | no_answer, or None while unanswered.
    choice: str | None


@dataclass(frozen=True)
class LoginCard:
    """Everything a chat card shows, re-read from the database on every render.

    Login names (labels) only: never a value, a key, or who set it.
    """

    tenant_id: uuid.UUID
    run_id: uuid.UUID
    platform: str | None
    channel_id: str | None
    thread_id: str | None
    requester_platform_user_id: str | None
    rows: tuple[LoginCardRow, ...]
    # The logins the workers run without regardless, with the reason.
    without: tuple[str, ...]
    expires_at: datetime
    digest: str
    state: Literal["open", "decided", "expired", "run_ended"]
    card_message_id: str | None
    card_settled: bool

    @property
    def final(self) -> bool:
        return self.state != "open"


def login_card(
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    question: Mapping[str, Any],
    decision: Mapping[str, str] | None,
    *,
    running: bool,
) -> LoginCard:
    choices = cast(dict[str, str], question.get("choices") or {})
    rows = tuple(
        LoginCardRow(
            index=index,
            label=str(row.get("label", "")),
            choice=choices.get(str(row.get("key"))) or (decision or {}).get(str(row.get("key"))),
        )
        for index, row in enumerate(cast(list[dict[str, Any]], question.get("rows") or []))
    )
    state: Literal["open", "decided", "expired", "run_ended"] = (
        "expired"
        if question.get("state") == "expired"
        else "decided"
        if decision is not None
        else "run_ended"
        if not running
        else "open"
    )
    return LoginCard(
        tenant_id=tenant_id,
        run_id=run_id,
        platform=question.get("platform"),
        channel_id=question.get("channel_id"),
        thread_id=question.get("thread_id"),
        requester_platform_user_id=question.get("requester_platform_user_id"),
        rows=rows,
        without=tuple(str(line) for line in question.get("without") or ()),
        expires_at=_expires(question),
        digest=str(question.get("digest", "")),
        state=state,
        card_message_id=question.get("card_message_id"),
        card_settled=bool(question.get("card_settled")),
    )


def final_state_text(card: LoginCard) -> str:
    """The card's last words, the same in every chat."""
    shared = [row.label for row in card.rows if row.choice in ("share", "share_once")]
    unanswered = [row.label for row in card.rows if row.choice in (None, "no_answer")]
    skipped = [row.label for row in card.rows if row.choice == "skip"]
    lines: list[str] = []
    if shared:
        lines.append("Shared with workers: " + ", ".join(shared))
    if card.state == "run_ended" and unanswered:
        lines.append("The run ended before an answer; workers didn't get: " + ", ".join(unanswered))
    elif unanswered:
        lines.append("No answer, so workers ran without: " + ", ".join(unanswered))
    if skipped or card.without:
        lines.append("Workers run without: " + ", ".join([*skipped, *card.without]))
    return "\n".join(lines)


@dataclass(frozen=True)
class LoginAnswer:
    """What a click did: the card as it now stands, or why it changed nothing."""

    card: LoginCard | None
    refused: Refusal | None = None
    decided: bool = False
    saved: bool = False


async def choose_login(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    digest: str,
    actor_account_id: uuid.UUID | None,
    actor_platform_user_id: str | None,
    is_admin: bool,
    row: int | Literal["all"],
    choice: LoginChoice,
    now: datetime,
    save: Callable[
        [Mapping[str, Literal["share", "skip", "ask"]], Mapping[str, Any]], Awaitable[object]
    ]
    | None = None,
) -> LoginAnswer:
    """One click on the run's login card, re-checked here at click time.

    `actor_account_id` None is a clicker with no account here (refused, audited).
    `digest` may be the card's prefix of the question digest (at least 12 hex).
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
            card = (
                login_card(tenant_id, run_id, question, state[3], running=state[1] in _RUNNING)
                if state is not None and question is not None
                else None
            )
            return LoginAnswer(card, reason)

        if state is None or question is None:
            return await refuse("no_question")
        requester, status, _, decision = state
        refused: Refusal | None = (
            "already_decided"
            if decision is not None
            else "run_ended"
            if status not in _RUNNING
            else "stale_card"
            # A card's button may carry only a prefix (chat ids are short): >= 12 hex.
            if len(digest) < _DIGEST_PREFIX
            or not str(question.get("digest", "")).startswith(digest)
            else "not_requester"
            if actor_account_id != requester
            else "expired"
            if now >= _expires(question)
            else None
        )
        if refused is not None:
            return await refuse(refused)
        assert actor_account_id is not None  # it is the requester
        if row == "all" and choice != "skip":
            return await refuse("bad_choice")  # only "Skip all"
        rows = cast(list[dict[str, Any]], question.get("rows") or [])
        choices = cast(dict[str, str], question.setdefault("choices", {}))
        if row == "all":
            targets = [r for r in rows if r["key"] not in choices]
        elif 0 <= row < len(rows):
            targets = [rows[row]]
        else:
            return await refuse("stale_card")  # a row the stored question doesn't have
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
            await save({key: _SAVED[choice] for key in keys}, question)
            saved = True
        except Exception as error:  # the run's decision stands; only the saved default failed
            log.warning("fleet.login_save_failed", error_type=type(error).__name__)
    return LoginAnswer(
        login_card(tenant_id, run_id, question, decided, running=True),
        decided=decided is not None,
        saved=saved,
    )


#: Plain words for every refusal, for any chat's card (never a code).
REFUSAL_TEXT: dict[Refusal, str] = {
    "no_question": "This question is no longer open.",
    "already_decided": "This run already has its answer; it can't be changed now.",
    "run_ended": "This run has ended.",
    "stale_card": "This card is out of date. Use the newest one in this thread.",
    "not_requester": "Only the person who started this run can answer.",
    "expired": "Time ran out, so the workers run without these logins.",
    "bad_choice": "That button can't apply to every login at once; choose one by one.",
    "share_not_allowed": (
        "You can't share that login: only an admin (or its owner, for a login the owner "
        "set) can share it with workers. You can share only keys you set yourself."
    ),
    "rate_limited": "Too many tries on this card. Wait a minute, then use the newest card.",
}


@dataclass
class LoginClickLimiter:
    """Refused clicks per (run, clicker) in a sliding window, in this process.

    Past the cap a click is refused without touching the database, so a stale card
    clicked over and over can't flood the audit table. Applied clicks never count.
    """

    max_refusals: int = 5
    window_s: float = 60.0
    _refusals: dict[str, deque[float]] = field(default_factory=dict[str, deque[float]])

    def _bucket(self, key: str, now: float) -> deque[float]:
        bucket = self._refusals.setdefault(key, deque())
        while bucket and bucket[0] <= now - self.window_s:
            bucket.popleft()
        return bucket

    def limited(self, key: str, now: float) -> bool:
        return len(self._bucket(key, now)) >= self.max_refusals

    def refused(self, key: str, now: float) -> None:
        self._bucket(key, now).append(now)


async def answer_login_click(
    deps: TurnDeps,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    run_id: uuid.UUID,
    digest: str,
    row: int | Literal["all"],
    choice: LoginChoice,
    clicker_platform_user_id: str,
    live_admin: Callable[[], Awaitable[bool]],
    limiter: LoginClickLimiter,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> LoginAnswer:
    """A chat card's button press, end to end: the one call an adapter makes.

    The clicker is resolved to the account they already have here (never created
    on a click; none = refused as not the requester). Admin is asked live, at click
    time. Refused clicks are capped per (run, clicker) by `limiter`. An owner's or
    admin's answer is also saved to the agent's config as the clicker.
    """
    from daimon.core.stores import fleet

    key = f"{run_id}:{clicker_platform_user_id}"
    clock = now().timestamp()
    if limiter.limited(key, clock):
        log.warning("fleet.login_click_rate_limited", run_id=str(run_id))
        return LoginAnswer(None, "rate_limited")
    account = await fleet.account_for_platform_user(
        deps.sessionmaker,
        tenant_id=tenant_id,
        platform=platform,
        external_id=clicker_platform_user_id,
    )
    is_admin = account is not None and await live_admin()

    async def save(
        choices: Mapping[str, Literal["share", "skip", "ask"]], question: Mapping[str, Any]
    ) -> None:
        from daimon.core.authz import Subject
        from daimon.core.channel_backend import (
            AgentBackendService,
            BackendActor,
            load_backend_agent,
        )
        from daimon.core.turn.admission import capture_agent_source

        assert account is not None
        name = question.get("agent_name")
        if not isinstance(name, str):
            raise LookupError("agent_name")
        subject = Subject(is_admin=is_admin, platform_user_id=clicker_platform_user_id)
        agent = await load_backend_agent(
            deps.anthropic, deps.sessionmaker, tenant_id=tenant_id, agent_name=name
        )
        if question.get("agent_id") != str(agent.agent_id):
            raise LookupError("agent_replaced")  # a namesake is not this run's agent
        source = await capture_agent_source(deps, agent, account_id=account, subject=subject)
        await AgentBackendService(deps.sessionmaker, enabled=True).set_credential_sharing(
            agent,
            choices,
            source,
            BackendActor(tenant_id=tenant_id, account_id=account, subject=subject),
            requester_account_id=account,
            run_id=run_id,
        )

    answer = await choose_login(
        deps.sessionmaker,
        tenant_id=tenant_id,
        run_id=run_id,
        digest=digest,
        actor_account_id=account,
        actor_platform_user_id=clicker_platform_user_id,
        is_admin=is_admin,
        row=row,
        choice=choice,
        now=now(),
        save=save,
    )
    if answer.refused is not None:
        limiter.refused(key, clock)
    return answer


async def login_cards(
    sessionmaker: async_sessionmaker[AsyncSession], *, platform: str
) -> tuple[LoginCard, ...]:
    """Every card a chat adapter still has work on: open ones (to post, or expire at
    `expires_at`) and final ones whose posted card isn't settled yet (after a restart)."""
    from daimon.core.stores import fleet

    return tuple(
        login_card(tenant_id, run_id, question, decision, running=status in _RUNNING)
        for tenant_id, run_id, status, question, decision in await fleet.login_questions(
            sessionmaker, platform=platform
        )
    )


async def settle_login_card(
    sessionmaker: async_sessionmaker[AsyncSession], *, tenant_id: uuid.UUID, run_id: uuid.UUID
) -> bool:
    """The posted card now shows its final state: stop listing it."""
    from daimon.core.stores import fleet

    return await fleet.settle_login_card(sessionmaker, tenant_id=tenant_id, run_id=run_id)
