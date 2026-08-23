"""Wire contract for the chat-initiated credential-request button.

This module lives in `daimon.core`, not in either adapter, because the two
processes that need it cannot import each other: the MCP server (Cloud Run)
mints the button and posts it to a channel, while the Discord bot (worker VM)
later receives the click and must decode the same `custom_id`. Import-linter's
independence contract forbids `daimon.adapters.mcp` from importing
`daimon.adapters.discord` (or vice versa), so the shared encode/decode logic
has to sit one level up, in core.

Discord caps `custom_id` at 100 characters, which rules out encoding the
target fields (kind, agent, key/server name, requester, expiry) directly in
the id — an MCP server name alone can blow that budget. Instead the
`custom_id` carries only an opaque, single-use token; the actual target
fields live in the `credential_requests` DB row keyed by that token (see
`daimon.core.stores.credential_requests`). The button's *label* is what
names the human-readable target, via `build_button_label`.

`CUSTOM_ID_PATTERN` MUST fullmatch every minted custom_id: discord.py's
`DynamicItem` dispatch matches incoming custom_ids with `pattern.fullmatch`,
not a prefix search, so a template that merely starts with the right prefix
would never dispatch (or could over-match unrelated ids).

Each kind puts something different in `target`: for `env` it is the
environment variable's key name, for `mcp` it is the MCP server name, and
for `repo` it is the repo URL. A `repo` request's branch is collected in the
modal at click time, not stored on the row — `credential_requests` has no
branch column, and this module adds none.
"""

from __future__ import annotations

import datetime as dt
import re
import secrets
from typing import Final, Literal

CUSTOM_ID_PREFIX: Final[str] = "ztc:"

# secrets.token_urlsafe(16) yields a 22-character URL-safe string (~128 bits
# of entropy) — the resulting custom_id ("ztc:" + 22 chars = 26 chars) sits
# comfortably under Discord's 100-character custom_id cap.
TOKEN_BYTES: Final[int] = 16

CUSTOM_ID_TEMPLATE: Final[str] = r"ztc:(?P<token>[A-Za-z0-9_-]{16,64})"
CUSTOM_ID_PATTERN: Final[re.Pattern[str]] = re.compile(CUSTOM_ID_TEMPLATE)

CredentialRequestKind = Literal["env", "mcp", "repo", "skill_repo"]

# TTL bounds the replay window for a never-clicked button sitting in channel
# scrollback; combined with the store's single-use consume, this is the full
# replay defense.
DEFAULT_TTL: Final[dt.timedelta] = dt.timedelta(minutes=30)

# Discord's documented Button.label limit.
MAX_BUTTON_LABEL_CHARS: Final[int] = 80

# Slack's documented button-element text limit (plain_text, 75 characters).
MAX_SLACK_BUTTON_LABEL_CHARS: Final[int] = 75

# Slack routes block_actions by action_id, and a button element carries the
# opaque request token in its `value` field instead of the id itself — the
# Slack counterpart of Discord's `custom_id` encoding above. Both processes
# (the MCP server posts the button, the Slack bot dispatches the click) read
# this one constant, for the same divergent-copy reason the custom_id
# builders live here.
SLACK_ACTION_ID: Final[str] = "credential_request"

# The full label prefix for each kind. "env" and "mcp" reproduce the
# pre-repo-kind wording byte-for-byte. "repo" cannot reuse the "Add {X}
# credential: " interpolation — a repo binding is not a credential.
# "skill_repo" is deliberately worded as an import, not a binding: it shares
# "repo"'s PAT store but writes NO agent_repo_binding row, so a label saying
# "bind" would promise a checkout the user never asked for.
_KIND_LABEL_PREFIX: Final[dict[CredentialRequestKind, str]] = {
    "env": "Add env credential: ",
    "mcp": "Add MCP credential: ",
    "repo": "Bind repo: ",
    "skill_repo": "Import skills from: ",
}


def build_skill_repo_target(url: str, branch: str, path: str) -> str:
    """Pack a skill-sync target into one `target` column value.

    `URL[@branch][#path]` — the same grammar the CLI's `--repo` argument
    already accepts, reused so the round trip needs no schema change and no
    second parser. `branch`/`path` have to survive the button click because
    the modal re-runs the sync on submit, and a click carries nothing but
    the request row.
    """
    packed = url
    if branch:
        packed = f"{packed}@{branch}"
    if path:
        packed = f"{packed}#{path}"
    return packed


def split_skill_repo_target(target: str) -> tuple[str, str, str]:
    """Inverse of `build_skill_repo_target`. Returns `(url, branch, path)`.

    Defaults match `sync_skills`' own defaults: branch "main", path "".
    Split on "#" first — a branch name cannot contain "#", but a path can
    contain "@", so splitting on "@" first would mangle it.
    """
    rest, _, path = target.partition("#")
    url, _, branch = rest.partition("@")
    return url, branch or "main", path


def mint_request_token() -> str:
    """Return a fresh, URL-safe, single-use token. Two calls never collide."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def build_custom_id(token: str) -> str:
    """Return the wire `custom_id` for a minted token."""
    return f"{CUSTOM_ID_PREFIX}{token}"


def build_button_label(
    kind: CredentialRequestKind,
    target: str,
    *,
    max_chars: int = MAX_BUTTON_LABEL_CHARS,
) -> str:
    """Return the human-readable button label naming the exact target.

    Truncates `target` (with a trailing "…") when it would overflow the
    platform's button label limit — the label, not the custom_id, is what
    names the exact target, so it must fit on its own. Discord callers use
    the 80-character default; Slack callers pass
    `MAX_SLACK_BUTTON_LABEL_CHARS` (75).
    """
    prefix = _KIND_LABEL_PREFIX[kind]
    available = max_chars - len(prefix)
    if len(target) <= available:
        return f"{prefix}{target}"
    truncated = target[: available - 1] + "…"
    return f"{prefix}{truncated}"
