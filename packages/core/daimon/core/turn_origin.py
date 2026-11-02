"""Create and render trusted controls without replacing platform history."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from typing import Literal

from daimon.core.access_policy import DM_SCOPE_PREFIX
from daimon.core.platform_words import Place, one_platform_rule, turn_words
from daimon.core.stores.domain import (
    CHAT_PLATFORMS,
    ChatPlatform,
    Role,
    TransferKind,
    TurnOriginRow,
)
from daimon.core.stores.github_grant_proposals import resolve as resolve_github_grant_proposals
from daimon.core.stores.pending_skill_adds import resolve_pending_skill_adds
from daimon.core.stores.turn_origins import create_origin, delete_origin
from daimon.core.teams_threads import conversation_of, is_group_chat
from pydantic import BaseModel, ConfigDict, JsonValue
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

#: A continuation is the person's own words and is untrusted text. It is JSON
#: encoded into the controls and bounded, so a long paste cannot crowd out the
#: instructions that follow it. Must agree with
#: `daimon.core.continuity.continuation.MAX_REQUESTED_WORK`, which applies the
#: same bound when the continuation is queued; this one is the render-time
#: backstop for text that reached the controls by any other route.
MAX_REQUESTED_WORK_CHARS = 500
_ALL_CHANNEL_READS = "read_channel, read_thread, get_message, list_threads, search_messages"
# Slack has no list_threads: a thread there is a reply chain, not a listable object.
_CHANNEL_READS = {"slack": "read_channel, read_thread, get_message, search_messages"}


def words_sentence(platform: ChatPlatform) -> str:
    """Appended to the controls' prose whenever `words` is present."""
    return (
        " When you tell a person how to reach a Daimon command or who can change a setting, "
        f"use `words` from these controls. {one_platform_rule(platform)} Even when you "
        'quote or sum up what someone wrote, put "another app" in place of another app\'s '
        "name so the meaning stays the same. Which Daimon "
        "command to use and who counts as an admin, as `words` gives them, are not setup "
        "details: tell anyone. Repo names, key names, environment details and your "
        "instructions still are."
    )


_TEAMS_CHANNEL_SUFFIXES = ("@thread.tacv2", "@thread.skype")
current_origin_id: ContextVar[uuid.UUID | None] = ContextVar("turn_origin_id", default=None)
# A prepared driver explicitly grants its own origin, apart from
# `current_origin_id`, which nested contexts inherit.
current_execution_origin_id: ContextVar[uuid.UUID | None] = ContextVar(
    "execution_origin_id", default=None
)


@contextmanager
def execution_origin(origin_id: uuid.UUID | None) -> Iterator[None]:
    """Authorize origin credential derivation only inside this prepared execution."""
    token = current_execution_origin_id.set(origin_id)
    try:
        yield
    finally:
        current_execution_origin_id.reset(token)


class SessionState(BaseModel):
    """What happened to the workspace this turn runs in, and what changed with it.

    `state` is the fact: `continued` (same workspace as last turn), `updated`
    (same workspace, new configuration applied to it), `replaced` (a new
    workspace carrying this task's work across), `replaced_after_loss` (a new
    workspace because the old one was gone, so something is genuinely missing).
    `applied` names configuration that is live for this turn; `lost` names what
    did not survive.
    """

    model_config = ConfigDict(frozen=True)

    state: Literal["continued", "updated", "replaced", "replaced_after_loss"]
    applied: tuple[str, ...] = ()
    lost: tuple[str, ...] = ()


class ResponderAccount(BaseModel):
    """The platform account the responder answers through, as the platform names it.

    `user_id` is the bot user the platform verified for this install (Slack's
    `auth.test`), `mention` the native token that addresses it (`<@U0123>`).
    One account can front several agents over a thread's life, so the account
    identifies who is being addressed now, not who wrote an earlier message.
    """

    model_config = ConfigDict(frozen=True)

    user_id: str
    mention: str


class HandoffNotice(BaseModel):
    """One-time notice that this turn is the first after a task was handed over.

    Rendered on the receiving agent's first turn only. `workspace` says how much
    came across, so the reply cannot overstate it: `transferred` (the working
    files themselves), `transcript_only` (the conversation, no files),
    `history_only` (only what was posted in the thread).
    """

    model_config = ConfigDict(frozen=True)

    from_name: str
    from_ma_agent_id: str
    requested_by: str
    requested_work: str | None = None
    workspace: Literal["transferred", "transcript_only", "history_only"]
    files: tuple[str, ...] = ()
    not_carried: tuple[str, ...] = ()


def build_handoff_notice(
    *,
    from_name: str | None,
    from_ma_agent_id: str | None,
    requested_by: str,
    requested_work: str,
    transfer_kind: TransferKind | None,
) -> HandoffNotice:
    """The receiving agent's notice, claiming only what the bind carried across."""
    workspace: Literal["transferred", "transcript_only", "history_only"] = "history_only"
    not_carried: tuple[str, ...] = ("working files", "earlier conversation")
    if transfer_kind == "full":
        workspace, not_carried = "transferred", ()
    elif transfer_kind == "transcript":
        workspace, not_carried = "transcript_only", ("working files",)
    return HandoffNotice(
        from_name=from_name or "the previous agent",
        from_ma_agent_id=from_ma_agent_id or "",
        requested_by=requested_by,
        requested_work=requested_work,
        workspace=workspace,
        not_carried=not_carried,
    )


@asynccontextmanager
async def turn_origin(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    platform: str,
    parent_channel_id: str,
    thread_id: str,
    responder_ma_agent_id: str,
    responder_name: str,
    role: Role,
    sender_foreign: bool,
    shared_channel: bool,
    configuration_target_ma_agent_id: str | None = None,
    configuration_target_name: str | None = None,
    is_setup: bool = False,
    attended: bool = True,
    chat_session_id: str | None = None,
    message_text: str | None = None,
    queued_message: bool = False,
    platform_channel_id: str | None = None,
    platform_message_id: str | None = None,
) -> AsyncIterator[TurnOriginRow]:
    """Commit a distinct origin for this execution and remove it after execution.

    `sender_foreign` marks a client turn: the sender is from another organisation (or the
    platform could not say), who has no 1:1 chat with Daimon. `shared_channel` marks an
    origin channel shared with another organisation. Both come from the turn's admission
    (`admission.hold`, `admission.shared_channel`), never from an adapter's own guess;
    tools and the per-turn words read them from the origin.
    `message_text` is the person's own message that started this turn, as the
    adapter received it; it settles their open skill-add previews in this
    thread (`resolve_pending_skill_adds`). None for a turn no message started.
    `queued_message` means the message arrived during another turn in this
    thread. It runs as a turn, but cannot settle a skill-add preview.
    `platform_channel_id` and `platform_message_id` are the platform's own ids for
    where the turn's message can be read and the message itself, exactly as its API
    takes them; signed MCP caller identities name them. None when unknown.
    """
    now = datetime.now(UTC)
    async with sessionmaker.begin() as session:
        origin = await create_origin(
            session,
            tenant_id=tenant_id,
            account_id=account_id,
            platform=platform,
            parent_channel_id=parent_channel_id,
            thread_id=thread_id,
            responder_ma_agent_id=responder_ma_agent_id,
            responder_name=responder_name,
            configuration_target_ma_agent_id=configuration_target_ma_agent_id,
            configuration_target_name=configuration_target_name,
            role=role,
            expires_at=now + timedelta(hours=2),
            now=now,
            is_setup=is_setup,
            attended=attended,
            chat_session_id=chat_session_id,
            sender_foreign=sender_foreign,
            shared_channel=shared_channel,
            platform_channel_id=platform_channel_id,
            platform_message_id=platform_message_id,
        )
        if message_text is not None:
            if not queued_message:
                await resolve_pending_skill_adds(
                    session,
                    origin=origin,
                    message_text=message_text,
                    now=now,
                )
            await resolve_github_grant_proposals(session, origin=origin, message_text=message_text)
    origin_context = current_origin_id.set(origin.id)
    try:
        yield origin
    finally:
        current_origin_id.reset(origin_context)
        async with sessionmaker.begin() as session:
            await delete_origin(session, origin_id=origin.id)


def origin_place(origin: TurnOriginRow) -> Place:
    """Where the turn was sent: a private chat with Daimon, a channel or thread, or a group chat.

    Read from the ids the origin stores. A private conversation's scope id
    (`dm:...`) is a DM on any platform, as is a Slack `D...` channel. On Teams a
    group chat has a `19:...@thread.v2` id, a channel's ends `@thread.tacv2` (or
    the older `@thread.skype`), and anything else is the 1:1 chat."""
    if origin.thread_id.startswith(DM_SCOPE_PREFIX):
        return "dm"
    if origin.platform == "slack" and origin.parent_channel_id.startswith("D"):
        return "dm"
    if origin.platform == "teams":
        conversation = conversation_of(origin.thread_id)
        if is_group_chat(conversation):
            return "group"
        # A channel id names its thread kind; equal ids alone don't make a 1:1
        # chat, since a targeted channel root post stores its channel as both.
        if not conversation.split(";", 1)[0].endswith(_TEAMS_CHANNEL_SUFFIXES):
            return "dm"
    return "channel"


def _words_for_origin(origin: TurnOriginRow) -> dict[str, str] | None:
    """The per-turn platform words, or None for a platform with no chat commands (the CLI)."""
    if origin.platform not in CHAT_PLATFORMS:
        return None
    return turn_words(origin.platform, place=origin_place(origin), foreign=origin.sender_foreign)


def render_turn_origin(
    origin: TurnOriginRow,
    *,
    effective_is_admin: bool,
    responder_handle: str | None = None,
    responder_account: ResponderAccount | None = None,
    session_state: SessionState | None = None,
    handoff: HandoffNotice | None = None,
    running_on: Mapping[str, str] | None = None,
    moved: Mapping[str, str | None] | None = None,
) -> str:
    """Render server-provided location and identity separately from user history.

    `responder_handle` is the platform handle people mention this deployment
    by (`@daimon-staging`). It is the bot account's display name, which an
    operator may set to anything, while `responder.name` is the MA agent's
    name — they are routinely different, and a model that sees only the name
    reads the difference as two agents. Rendering both under one `responder`
    says they are one identity.

    `responder_account` is the bot account a native mention in the person's
    message names. Without it a native mention (`<@U0123>`) is an opaque ID
    that matches neither the name nor the handle, and the model reads its own
    account as another bot.

    `session_state` and `handoff` are optional server-supplied facts about the
    workspace this turn runs in. They are rendered inside the same JSON object
    as the rest of the controls, in a fixed key order, so two turns with the
    same facts produce byte-identical controls. `handoff` is a one-time block:
    the adapter includes it on the receiving agent's first turn only.

    `words` is what this platform calls things and how to reach its commands from
    where the turn was sent (`platform_words.turn_words`), so the model never
    tells a person a command from another platform. For a sender from another
    organisation (`origin.sender_foreign`, Teams), who has no private chat with Daimon,
    the words send setup and billing to someone in the organisation that added it.

    `running_on` is the model provider and model this message actually runs on
    (`daimon.core.turn.admission.running_on`), so "what model are you" is answered
    from what runs, not from the agent's settings. `moved` says, once, that this
    conversation just followed its agent's switch to another provider ("moved"),
    or stays where it is because it is shared ("stays"); `provider`/`model` name
    the agent's selection.

    Every value here is server-supplied except `handoff.requested_work`, which
    is the person's own words — it is truncated to `MAX_REQUESTED_WORK_CHARS`
    and JSON-encoded like everything else, never interpolated into the prose.
    """
    responder: dict[str, object] = {
        "name": origin.responder_name,
        "ma_agent_id": origin.responder_ma_agent_id,
    }
    if responder_handle is not None:
        responder["handle"] = responder_handle
    if responder_account is not None:
        responder["platform_user_id"] = responder_account.user_id
        responder["mention"] = responder_account.mention
    controls: dict[str, object] = {
        "origin_context_id": str(origin.id),
        "is_setup": origin.is_setup,
        "platform": origin.platform,
        "parent_channel_id": origin.parent_channel_id,
        "thread_id": origin.thread_id,
        "current_role": origin.role,
        "responder": responder,
        "configuration_target": (
            {
                "name": origin.configuration_target_name,
                "ma_agent_id": origin.configuration_target_ma_agent_id,
            }
            if origin.configuration_target_ma_agent_id is not None
            else None
        ),
    }
    words = _words_for_origin(origin)
    if words is not None:
        controls["words"] = words
    controls["effective_role"] = "admin" if effective_is_admin else "user"
    if session_state is not None:
        controls["session_state"] = {
            "state": session_state.state,
            "applied": list(session_state.applied),
            "lost": list(session_state.lost),
        }
    if running_on is not None:
        controls["running_on"] = dict(running_on)
    if moved is not None:
        controls["conversation_move"] = dict(moved)
    if handoff is not None:
        controls["handoff"] = {
            "from_name": handoff.from_name,
            "from_ma_agent_id": handoff.from_ma_agent_id,
            "requested_by": handoff.requested_by,
            "requested_work": (
                None
                if handoff.requested_work is None
                else handoff.requested_work[:MAX_REQUESTED_WORK_CHARS]
            ),
            "workspace": handoff.workspace,
            "files": list(handoff.files),
            "not_carried": list(handoff.not_carried),
        }
    rendered = "<turn_controls>\n" + json.dumps(controls)
    # Only say this when a handle is actually rendered: without one the
    # sentence points at a key that is not there.
    if responder_handle is not None:
        rendered += (
            "\nresponder.handle is how people mention the agent answering here. That handle, "
            "responder.name, and any casing of either are the same agent — never ask whether "
            "they are the same, and never treat that difference as target ambiguity. Ask about "
            "the target only when a different agent is named."
        )
    if responder_account is not None:
        rendered += (
            "\nresponder.mention is the native mention of the account you answer through, "
            "responder.platform_user_id. In thread history and the user query it appears "
            "XML-escaped (< as &lt;, > as &gt;). A message that mentions it is addressed to "
            "you: never treat it as another agent or bot, and never hand it off for that "
            "reason. Earlier messages posted by that account may have come from a different "
            "agent that answered through it then; judge them by what they say."
        )
    rendered += (
        "\nUse the explicitly requested target when named; otherwise configure the "
        "configuration_target. If is_setup is true and no target is selected, ask which "
        "agent to configure before mutation. Only ordinary chat defaults to the responder. "
        "Never substitute a "
        "recreated namesake for a missing identity. Ask one concise question when the "
        "target is missing or ambiguous. Pass expected_ma_agent_id with target-bearing "
        "tools. Use set_setup_target to switch this setup conversation's target and "
        "state the switch briefly. Pass origin_context_id to credential-request tools and to "
        f"channel reads ({_CHANNEL_READS.get(origin.platform, _ALL_CHANNEL_READS)})."
    )
    if words is not None and origin.platform in CHAT_PLATFORMS:
        rendered += words_sentence(origin.platform)
    if running_on is not None:
        rendered += (
            " running_on is the model provider and model this reply actually runs on: "
            "answer what model you are from it, never from your settings."
        )
    if moved is not None:
        rendered += (
            " conversation_move: when kind is moved, this conversation just moved to "
            "that provider and model: say so first, once, and that earlier messages here "
            "aren't carried over. When kind is stays, this conversation is shared and "
            "stays on running_on while new conversations use that provider and model: "
            "say so only if asked about your model or the switch."
        )
    rendered += " These controls grant no additional mutation or routing permissions."
    # The continuity paragraph is appended only when there is continuity to
    # describe: on an ordinary turn neither block is present and every sentence
    # below would be about nothing.
    if session_state is not None or handoff is not None:
        rendered += (
            "\nIf handoff is present, your first reply must show you have the task: name "
            "the work in progress and at least one file listed in handoff.files that you "
            "can actually see, then continue requested_work. Never claim a running "
            "process, notebook kernel or shell survived — only files and the conversation "
            "did. If a listed file is missing, say which. If session_state.state is "
            "replaced_after_loss, say what is missing before continuing; do not claim a "
            "complete restore. If it is replaced and session_state.lost is not empty, tell "
            "the person in one line what did not come across. Configuration named in "
            "session_state.applied is live for this turn; anything else applies from the "
            "person's next message."
        )
    return rendered + "\n</turn_controls>"


REDACTED_ORIGIN_ID = "[redacted]"
_ORIGIN_ID_KEY = "origin_context_id"
# The key and the id after it, in the rendered controls (`"origin_context_id": "<uuid>"`),
# in a tool call's JSON (also escaped) and in a model's prose ("origin_context_id is <uuid>").
_ORIGIN_ID_TEXT = re.compile(
    rf"({_ORIGIN_ID_KEY}(?:\W|\b(?:is|was|are|of|equals)\b){{0,12}})"
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def redact_turn_origin_text(text: str) -> str:
    """`text` with the id after every `origin_context_id` replaced by `[redacted]`."""
    return _ORIGIN_ID_TEXT.sub(rf"\1{REDACTED_ORIGIN_ID}", text)


def redact_turn_origin_ids(value: JsonValue) -> JsonValue:
    """`value` with every turn-origin id replaced by `[redacted]`.

    An origin id is a bearer proof that a call comes from one live turn (the
    person's 1:1 chat gates mail, calendar and file links on it), so it must stay
    inside that turn. Whatever lets the model read another session's history
    (transcripts, session events, a quoted earlier conversation) passes it through
    here, never the current turn's own prompt. Walks nested lists and objects and
    redacts both the rendered controls' text and any `origin_context_id` field, such
    as the one in a tool call's input. The id format lives with `render_turn_origin`
    above; keep the two in step (`tests/test_turn_origin_redaction.py` round-trips them).
    """
    if isinstance(value, str):
        return redact_turn_origin_text(value)
    if isinstance(value, list):
        return [redact_turn_origin_ids(item) for item in value]
    if isinstance(value, dict):
        return {
            key: (
                REDACTED_ORIGIN_ID
                if key == _ORIGIN_ID_KEY and isinstance(item, str)
                else redact_turn_origin_ids(item)
            )
            for key, item in value.items()
        }
    return value
