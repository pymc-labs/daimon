"""Reader-3 (person) and reader-2 (model, `ToolError`) continuity copy.

Every string here is final copy shown to the person in Discord/Slack/CLI, or
`ToolError` text read by the model deciding what to say next — never both at
once (adapter copy, tool-error text and person-facing text each have one reader).
Strings are
plain fact-per-line prose: markup-free, no session ids, no internal jargon.
Adapters add emoji/bold on top; this module never does.

Pure module — no I/O, no clock, no randomness. Every `render_*` function is a
total function of its arguments (or raises `ValueError` on caller misuse, e.g.
an availability that needs a `count` the caller did not supply).

Lines are joined with ``"\\n"`` and never end in a trailing newline.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Final, Literal

from daimon.core.env_file import MEMBER_SECRET_SUFFIX_HINT, EnvProblem, EnvRejection
from pydantic import BaseModel, ConfigDict

__all__ = [
    "ChangeAvailability",
    "ChangeKind",
    "ConfigurationChange",
    "FORBIDDEN_TOKENS",
    "TransferKind",
    "UnsavedWorkChoice",
    "render_change_confirmation",
    "render_current_work_must_finish",
    "render_env_import_applied",
    "render_env_import_rejected",
    "render_fresh_start",
    "render_handoff_acknowledged",
    "render_preparation_failed",
    "render_replacement_summary",
    "render_responder_changed_without_handoff",
    "render_timer_seed",
    "render_timer_target_changed",
    "render_wake_target_changed",
    "render_unexpected_loss",
    "render_unsaved_work_question",
]

ChangeKind = Literal[
    "key",
    "keys_bulk",
    "key_removed",
    "model",
    "instructions",
    "skill",
    "skill_removed",
    "skills_bulk",
    "mcp",
    "mcp_removed",
    "repo",
    "environment",
]
ChangeAvailability = Literal["saved", "ready_now", "next_message", "preparation_failed"]
TransferKind = Literal["full", "transcript", "history"]
UnsavedWorkChoice = Literal["copy", "leave"]

#: Words that never belong in reader-3 or reader-2 copy (case-insensitive
#: substring check). Exported so parity/sweep tests elsewhere can reuse it.
FORBIDDEN_TOKENS: frozenset[str] = frozenset(
    {"session", "revision", "modal", "credential", "env var", "restart"}
)


class ConfigurationChange(BaseModel):
    """One configuration change to confirm to the person who made it."""

    model_config = ConfigDict(frozen=True)

    target_name: str
    kind: ChangeKind
    availability: ChangeAvailability
    detail: str | None = None
    repo: str | None = None
    branch: str | None = None
    count: int | None = None
    copied_file_count: int | None = None
    unsaved_work: UnsavedWorkChoice | None = None


def _render_saved_lines(first_line: str, target_name: str, availability: ChangeAvailability) -> str:
    anyone_line = f"Anyone who talks to {target_name} can use it."
    if availability in ("saved", "preparation_failed"):
        lines = [first_line, anyone_line]
    elif availability == "ready_now":
        lines = [first_line, f"{target_name} can use it now.", anyone_line]
    else:
        lines = [first_line, f"{target_name} can use it from your next message here.", anyone_line]
    return "\n".join(lines)


def _render_key_saved(target_name: str, key: str | None, availability: ChangeAvailability) -> str:
    if key is None:
        raise ValueError("kind='key' requires detail (the key name)")
    return _render_saved_lines(f"{key} saved for {target_name}.", target_name, availability)


def _render_keys_bulk_saved(
    target_name: str, detail: str | None, count: int | None, availability: ChangeAvailability
) -> str:
    if detail is not None:
        raise ValueError("kind='keys_bulk' does not use detail; it names no single key")
    if count is None:
        raise ValueError("kind='keys_bulk' requires count")
    if count < 1:
        raise ValueError("kind='keys_bulk' count must be >= 1")
    if count == 1:
        first_line = f"1 key saved for {target_name}."
    else:
        first_line = f"{count} keys saved for {target_name}."
    return _render_saved_lines(first_line, target_name, availability)


def _render_key_removed(target_name: str, key: str | None) -> str:
    if key is None:
        raise ValueError("kind='key_removed' requires detail (the key name)")
    return "\n".join(
        [
            f"{key} removed from {target_name}.",
            "It stops being supplied from your next message here.",
            "Work already running with it is not stopped, and it is not cancelled at the service.",
        ]
    )


def _render_model_changed(target_name: str, detail: str | None) -> str:
    if detail is not None:
        raise ValueError("kind='model' does not use detail")
    return "\n".join(
        [
            f"{target_name} will use the new model starting with your next message.",
            "Your task, decisions and working files stay as they are.",
        ]
    )


def _render_instructions_updated(target_name: str, detail: str | None) -> str:
    if detail is not None:
        raise ValueError("kind='instructions' does not use detail")
    return "\n".join(
        [
            f"{target_name}'s instructions are updated.",
            "It uses them from your next message here.",
        ]
    )


def _render_skill_added(target_name: str, skill: str | None) -> str:
    if skill is None:
        raise ValueError("kind='skill' requires detail (the skill name)")
    return "\n".join(
        [
            f"{target_name} has the {skill} skill.",
            "It can use it from your next message here.",
        ]
    )


def _render_skill_removed(target_name: str, skill: str | None) -> str:
    if skill is None:
        raise ValueError("kind='skill_removed' requires detail (the skill name)")
    return "\n".join(
        [
            f"{target_name} no longer has the {skill} skill.",
            "The change applies from your next message here.",
        ]
    )


def _render_skills_bulk_added(change: ConfigurationChange) -> str:
    if change.detail is not None:
        raise ValueError("kind='skills_bulk' does not use detail; it names no single skill")
    if change.count is None:
        raise ValueError("kind='skills_bulk' requires count")
    if change.count < 1:
        raise ValueError("kind='skills_bulk' count must be >= 1")
    if change.repo is None:
        raise ValueError("kind='skills_bulk' requires repo (where the skills came from)")
    target_name = change.target_name
    if change.availability == "preparation_failed":
        return "\n".join(
            [
                f"Your GitHub token is saved for {target_name}.",
                "The skills did not import.",
                f"Ask me to add skills from {change.repo} again to retry.",
            ]
        )
    noun = "skill" if change.count == 1 else "skills"
    pronoun = "it" if change.count == 1 else "them"
    return "\n".join(
        [
            f"{change.count} {noun} added to {target_name} from {change.repo}.",
            f"It can use {pronoun} from your next message here.",
        ]
    )


def _render_mcp_connected(
    target_name: str, service: str | None, availability: ChangeAvailability
) -> str:
    if service is None:
        raise ValueError("kind='mcp' requires detail (the service name)")
    if availability == "preparation_failed":
        return "\n".join(
            [
                f"{service} token saved for {target_name}.",
                "The connection did not finish, so its tools are not available yet.",
                f"Ask me to connect {service} again to retry.",
            ]
        )
    return "\n".join(
        [
            f"{target_name} is connected to {service}.",
            "Its tools are available from your next message here.",
        ]
    )


def _render_mcp_removed(target_name: str, service: str | None) -> str:
    if service is None:
        raise ValueError("kind='mcp_removed' requires detail (the service name)")
    return "\n".join(
        [
            f"{target_name} is no longer connected to {service}.",
            "The change applies from your next message here.",
        ]
    )


def _render_environment_changed(target_name: str, env: str | None) -> str:
    if env is None:
        raise ValueError("kind='environment' requires detail (the environment name)")
    return "\n".join(
        [
            f"{target_name} runs in the {env} environment from your next message here.",
            "Your conversation, decisions and working files come with you.",
            "Anything still running stops. I cannot carry a running process or notebook "
            "kernel across.",
        ]
    )


def _render_repo_changed(change: ConfigurationChange) -> str:
    target_name = change.target_name
    if change.detail is not None:
        raise ValueError("kind='repo' does not use detail; use the repo/branch fields instead")
    if (change.repo is None) != (change.branch is None):
        raise ValueError("kind='repo' requires repo and branch together, or neither (token-only)")
    if change.repo is None:
        if change.unsaved_work is not None or change.copied_file_count is not None:
            raise ValueError(
                "kind='repo' token-only change (no repo pinned) must not set "
                "unsaved_work or copied_file_count"
            )
        return "\n".join(
            [
                f"Your GitHub token is saved for {target_name}.",
                "No repo is pinned yet.",
                f"{target_name}'s GitHub connection uses it from your next message here.",
            ]
        )
    switch_line = f"{target_name} now works in {change.repo} on {change.branch}."
    switches_line = "It switches to that checkout from your next message here."
    if change.unsaved_work == "copy":
        if change.copied_file_count is None:
            raise ValueError("unsaved_work='copy' requires copied_file_count")
        n = change.copied_file_count
        return "\n".join(
            [
                switch_line,
                f"I copied {n} changed files into your working files first; nothing was "
                "committed or pushed.",
                switches_line,
            ]
        )
    if change.unsaved_work == "leave":
        return "\n".join(
            [
                switch_line,
                "The uncommitted changes stay in the old checkout and do not come across.",
                switches_line,
            ]
        )
    if change.copied_file_count is not None:
        raise ValueError("copied_file_count is only valid with unsaved_work='copy'")
    return "\n".join(
        [
            switch_line,
            switches_line,
            "Your conversation and working files come with you.",
        ]
    )


def render_change_confirmation(change: ConfigurationChange) -> str:
    """Render the person-facing confirmation for one configuration change."""
    kind = change.kind
    if kind == "key":
        return _render_key_saved(change.target_name, change.detail, change.availability)
    if kind == "keys_bulk":
        return _render_keys_bulk_saved(
            change.target_name, change.detail, change.count, change.availability
        )
    if kind == "key_removed":
        return _render_key_removed(change.target_name, change.detail)
    if kind == "model":
        return _render_model_changed(change.target_name, change.detail)
    if kind == "instructions":
        return _render_instructions_updated(change.target_name, change.detail)
    if kind == "skill":
        return _render_skill_added(change.target_name, change.detail)
    if kind == "skill_removed":
        return _render_skill_removed(change.target_name, change.detail)
    if kind == "skills_bulk":
        return _render_skills_bulk_added(change)
    if kind == "mcp":
        return _render_mcp_connected(change.target_name, change.detail, change.availability)
    if kind == "mcp_removed":
        return _render_mcp_removed(change.target_name, change.detail)
    if kind == "repo":
        return _render_repo_changed(change)
    if kind == "environment":
        return _render_environment_changed(change.target_name, change.detail)
    raise ValueError(f"unknown ChangeKind: {kind!r}")


#: Per-line reason for a rejected `.env` upload, one phrase per rejection kind.
#: ``{name}`` is filled only where the problem carries a name, which the parser
#: sets only for a syntactically valid identifier — including a reserved or
#: member-refused one, whose NAME is safe to show; a value is never available
#: to these templates. The member refusal lists `MEMBER_SECRET_SUFFIX_HINT`,
#: the same suffixes the policy accepts.
_ENV_LINE_REASONS: Final[dict[EnvRejection, str]] = {
    "syntax": "I could not read this line.",
    "bad_name": "the name here is not usable as a key name.",
    "reserved_name": "{name} is reserved: it changes how the agent's tools run.",
    "not_credential_name": (
        "{name} is not a secret name a member can add. Use a name ending in "
        + MEMBER_SECRET_SUFFIX_HINT
        + ". An admin can add identity, account, region, path and URL names."
    ),
    "duplicate_name": "{name} is set more than once.",
    "alias_pair": (
        "{name} is read by the same tool as another key in this file; keep only one of them."
    ),
    "value_too_large": "{name} is too long.",
    "too_many_entries": "{name} is past the number of keys I can take at once.",
    "file_too_large": "this line could not be read.",
    "not_utf8": "this line could not be read.",
    "empty": "this line could not be read.",
}

#: Whole-file reason, used when the rejection points at no particular line.
_ENV_FILE_REASONS: Final[dict[EnvRejection, str]] = {
    "file_too_large": "The file is too big to read.",
    "not_utf8": "The file is not plain text.",
    "empty": "There are no keys in the file.",
    "syntax": "I could not read the file.",
    "bad_name": "The names in the file are not usable as key names.",
    "reserved_name": "The file sets a reserved name that changes how the agent's tools run.",
    "not_credential_name": (
        "A name in the file is not a secret name a member can add. Member keys must "
        "end in "
        + MEMBER_SECRET_SUFFIX_HINT
        + "; an admin can add identity, account, region, path and URL names."
    ),
    "duplicate_name": "The same name is set more than once.",
    "alias_pair": "The file sets two names one tool reads as the same credential.",
    "value_too_large": "One of the keys is too long.",
    "too_many_entries": "There are more keys in the file than I can take at once.",
}

_ENV_PROBLEM_LINES_SHOWN: Final[int] = 3
_ENV_NAMES_SHOWN: Final[int] = 8


def render_env_import_rejected(
    rejection: EnvRejection, problems: Sequence[EnvProblem], *, target_name: str
) -> str:
    """Tell the person their uploaded file was rejected whole, and why.

    `problems` carries line numbers and — only where the parser knew it — key
    names. No value ever reaches this copy; the problem type has no field for
    one.
    """
    lines = [f"No keys were saved for {target_name}."]
    if problems:
        reason = _ENV_LINE_REASONS[rejection]
        for problem in problems[:_ENV_PROBLEM_LINES_SHOWN]:
            lines.append(f"line {problem.line}: {reason.format(name=problem.name)}")
        remaining = len(problems) - _ENV_PROBLEM_LINES_SHOWN
        if remaining > 0:
            lines.append(f"…and {remaining} more.")
    else:
        lines.append(_ENV_FILE_REASONS[rejection])
    lines.append("Nothing was changed. Upload a corrected file.")
    return "\n".join(lines)


def _render_env_names(names: Sequence[str]) -> str:
    shown = ", ".join(names[:_ENV_NAMES_SHOWN])
    remaining = len(names) - _ENV_NAMES_SHOWN
    if remaining > 0:
        return f"{shown}, … and {remaining} more."
    return f"{shown}."


def render_env_import_applied(
    *, target_name: str, added: int, replaced: int, names: Sequence[str]
) -> str:
    """Confirm an accepted `.env` upload: how many keys landed, and which."""
    if added < 0 or replaced < 0:
        raise ValueError("added and replaced must be >= 0")
    if not names:
        raise ValueError("render_env_import_applied requires the names that were saved")
    added_noun = "key" if added == 1 else "keys"
    return "\n".join(
        [
            f"{added} {added_noun} added and {replaced} replaced for {target_name}.",
            _render_env_names(names),
            f"Anyone who talks to {target_name} can use them.",
        ]
    )


def render_unsaved_work_question(repo: str) -> str:
    """Ask the person whether to copy or leave uncommitted changes before a repo switch."""
    return "\n".join(
        [
            f"There are uncommitted changes in {repo}.",
            "I can copy them into your working files before switching, or leave them where "
            "they are.",
            "Nothing is committed or pushed either way.",
            "Which would you like?",
        ]
    )


def render_handoff_acknowledged(
    *, target_name: str, from_name: str, channel: str, requested_work: str | None
) -> str:
    """Confirm a task handoff to `target_name`, taking over from `from_name`."""
    lines = [
        f"{target_name} takes over this task from your next message here.",
        "Your conversation, decisions and working files come with it.",
        f"{target_name} uses its own keys, connections and memory, not {from_name}'s.",
        f"Who answers in {channel} is unchanged.",
    ]
    if requested_work is not None:
        # The person's own words may already end a sentence; never double the stop.
        lines.append(f"It will pick up with: {requested_work.rstrip().rstrip('.!?')}.")
    return "\n".join(lines)


def render_fresh_start(target_name: str) -> str:
    """Confirm a fresh-start reset for `target_name`, leaving unfinished work behind."""
    return "\n".join(
        [
            "Starting fresh from your next message here.",
            "Leaves behind: this task's working files and unfinished work.",
            f"Keeps: everything already posted here, and {target_name}'s saved memory, "
            "keys and connections.",
            "Nothing is removed until the new workspace is ready.",
        ]
    )


def render_preparation_failed(target_name: str) -> str:
    """Tell the person a turn was not started because `target_name` could not be prepared."""
    return "\n".join(
        [
            f"I could not get {target_name} ready with the latest setup, so I have not "
            "started this message.",
            "What was saved is still saved.",
            "Your task, decisions and working files are unchanged.",
            "Ask again to retry.",
        ]
    )


def render_unexpected_loss(transfer_kind: Literal["transcript", "history"]) -> str:
    """Tell the person their workspace was lost and describe what was recovered."""
    if transfer_kind == "transcript":
        recovered_line = "I have this conversation and the files that were saved to your task."
    else:
        recovered_line = "I have what was posted here, but not the earlier conversation."
    return "\n".join(
        [
            "I lost the workspace this task was running in and started a new one.",
            recovered_line,
            "Anything unsaved in the old workspace is gone, and nothing that was running "
            "came across.",
            "Tell me what to re-check and I'll go from there.",
        ]
    )


def render_current_work_must_finish(target_name: str, *, handoff: bool) -> str:
    """Tell the person the in-flight message finishes before their change/handoff applies."""
    if handoff:
        return "\n".join(
            [
                f"{target_name} takes over from your next message here.",
                "The message I'm working on now finishes with me.",
            ]
        )
    return "\n".join(
        [
            f"{target_name} is still working on the previous message here.",
            "Your change is saved and it picks it up on the next message, not that one.",
        ]
    )


def render_responder_changed_without_handoff(
    *, new_responder: str, owner: str, channel: str
) -> str:
    """Tell the person a new responder answers here, but the task still belongs to `owner`."""
    return "\n".join(
        [
            f"{new_responder} now answers in {channel}, but this conversation's work "
            f"belongs to {owner}.",
            f'Say "have {new_responder} take over this task" and I\'ll move the conversation and '
            "working files across.",
            f"Or start a new conversation to begin fresh with {new_responder}.",
        ]
    )


def render_replacement_summary(transfer_kind: TransferKind, lost: Sequence[str]) -> str:
    """Summarize what a planned replacement session carried across."""
    if transfer_kind == "full":
        base = "Your conversation, decisions and working files came across."
    elif transfer_kind == "transcript":
        base = (
            "Your conversation and decisions came across; the working files could not be saved "
            "from the old workspace."
        )
    else:
        base = "Only what was posted here came across."
    lines = [base]
    if lost:
        lines.append("Not carried: " + ", ".join(lost) + ".")
    return "\n".join(lines)


def render_timer_seed(note: str, *, set_at: datetime) -> str:
    """The user message a fired timer's turn runs with. Model-facing, not person-facing."""
    return (
        f"[timer] You set this timer in this conversation at "
        f"{set_at.astimezone(UTC):%Y-%m-%d %H:%M} UTC and it has fired. This turn was "
        f"started by the timer, not by a new message; act on your note and reply "
        f"here.\n\n{note}"
    )


def render_wake_target_changed(target_name: str, current_name: str) -> str:
    """Tell the person queued work did not run because another agent answers here now."""
    return "\n".join(
        [
            f"Work here was queued for {target_name}, but {current_name} answers here now.",
            "It did not run.",
            "Ask again and the current agent will pick it up.",
        ]
    )


def render_timer_target_changed(target_name: str, current_name: str) -> str:
    """Tell the person a timer did not run because another agent answers here now."""
    return "\n".join(
        [
            f"A reminder here was set with {target_name}, but {current_name} answers here now.",
            "It did not run.",
            "Ask again and I'll set it with the current agent.",
        ]
    )
