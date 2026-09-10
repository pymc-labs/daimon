"""Discord-specific message gating -- pure pre-DB checks."""

from __future__ import annotations

from collections.abc import Collection


def should_process_message(
    *,
    author_is_bot: bool,
    author_id: str,
    bot_mentioned: bool,
    guild_id: str | None,
    self_user_id: str | None = None,
    qa_bot_user_ids: Collection[str] = (),
) -> bool:
    """Pre-DB gate checks for on_message. Returns True if message passes all non-DB filters."""
    if author_is_bot and not _is_allowed_bot_author(
        author_id=author_id,
        self_user_id=self_user_id,
        qa_bot_user_ids=qa_bot_user_ids,
    ):
        return False
    if not bot_mentioned:
        return False
    return guild_id is not None


def _is_allowed_bot_author(
    *,
    author_id: str,
    self_user_id: str | None,
    qa_bot_user_ids: Collection[str],
) -> bool:
    """Whether a bot-authored mention may start a turn.

    Bots are rejected by default. A deployment may allow-list specific bots --
    automated QA drivers -- so a harness can exercise the mention path
    end-to-end without a human. Discord forbids bots from invoking application
    commands or component interactions, so a mention is the only turn trigger
    reachable by automation at all.

    Several ids are allowed because the admin-gated tools need a caller who
    holds Manage Server and the refusal paths need one who does not; a single
    driver cannot be both, and toggling a live bot's permissions mid-run makes
    the two outcomes indistinguishable from a flaky gate.

    daimon's own id is never honored, even if listed: its answers mention the
    caller, so allow-listing itself would have every turn trigger the next one
    without bound.
    """
    if self_user_id is not None and author_id == self_user_id:
        return False
    return author_id in qa_bot_user_ids


def is_auto_respond_candidate(
    *,
    available: bool,
    author_is_bot: bool,
    bot_mentioned: bool,
    in_thread: bool,
    guild_id: str | None,
) -> bool:
    """Pre-DB gate for organic thread participation: may this unmentioned message be screened?

    Only human-authored, unmentioned messages inside a guild thread qualify,
    and only while the deployment leaves the feature available (its mode is
    not `disabled`). Bots never qualify, allow-listed QA bots included: the
    mention path is the only automation entry point.
    """
    if not available or author_is_bot or bot_mentioned:
        return False
    return in_thread and guild_id is not None
