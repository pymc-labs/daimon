"""Platform-neutral model of a posted control card and its final copy.

A posted control is the public card the agent drops in a channel when it needs
a private value: "🔑 Add TOGGL_TOKEN to research-bot", with a button that opens
a private form. The same card is later edited in place to show what happened.

Discord and Slack render that card with entirely different primitives (V2
components vs. Block Kit dicts), but the *words* and the state machine behind
them must not diverge — that divergence is exactly what the parity suite keeps
catching. So the card is built once here, as data, and each adapter only
decides how to draw it.

Four slots, always in this order:

1. `headline` — one emoji-prefixed line; the emoji is also the state marker.
2. `facts` — one fact per line, in the vocabulary of the setup design.
3. `buttons` — only ever populated in the `requested` state.
4. `footer` — the closing line, when there is one.

The `requested` footer names the requester and the expiry, and both are
platform-specific renderings (Discord wants `<@id>` and a relative timestamp,
Slack wants `<@id>` and a `<!date^…>` token). The card therefore carries
`FOOTER_TEMPLATE` with two placeholders plus the raw
`requester_platform_user_id` / `expires_at_unix`, and the renderer substitutes.

Pure module — no I/O, no clock, no randomness. Every function is a total
function of its arguments or raises `ValueError` on caller misuse. Nothing here
imports an adapter or a platform SDK.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Final, Literal, Self, cast

from daimon.core.continuity.messages import ConfigurationChange, render_change_confirmation
from daimon.core.credential_requests import build_custom_id, split_skill_repo_target
from daimon.core.env_file import env_shadow_phrase
from daimon.core.github_repo_auth import normalize_owner_repo
from daimon.core.stores.domain import CredentialRequestRow
from pydantic import BaseModel, ConfigDict, model_validator

__all__ = [
    "ALREADY_USED_MESSAGE",
    "CardButton",
    "CardKind",
    "CardState",
    "EXPIRED_HEADLINE",
    "FOOTER_TEMPLATE",
    "NO_LONGER_VALID_MESSAGE",
    "PostedCard",
    "RECEIVED_FOOTER",
    "REPLACED_HEADLINE",
    "RefusalReason",
    "WRONG_REQUESTER_MESSAGE",
    "build_posted_card",
    "card_for_request",
    "card_text",
    "classify_card_state",
    "expired_message",
]

CardState = Literal[
    "requested",
    "received",
    "applied",
    "partial",
    "refused",
    "expired",
    "superseded",
    "replaced",
]
#: `env_file` is a bulk upload of one .env file; the other four match the
#: single-value request kinds in `daimon.core.credential_requests`.
CardKind = Literal["env", "env_file", "mcp", "mcp_oauth", "repo", "skill_repo"]
RefusalReason = Literal[
    "admin_required",
    "replacement_admin_required",
    "env_file_invalid",
    #: Nothing was saved because the thing being configured is gone or
    #: misconfigured — the agent no longer resolves, or the request row lost
    #: the server it named. Deliberately not an error string: the person who
    #: filled in the form did nothing wrong, and asking again is the way out.
    "target_unavailable",
    #: The server answered the token with 401/403 before anything was stored.
    #: The way out is a different credential — or, for a server that signs
    #: people in through a browser, the OAuth card.
    "token_rejected",
    #: The person cancelled at the OAuth provider; nothing was connected.
    "sign_in_declined",
]

#: `requested` footer, with the two placeholders each renderer fills from
#: `requester_platform_user_id` and `expires_at_unix`.
FOOTER_TEMPLATE: Final[str] = "Only {requester} can open this form. Expires {expires}."
RECEIVED_FOOTER: Final[str] = "Received. Saving…"
EXPIRED_HEADLINE: Final[str] = "⌛ This form expired."
#: `replaced` is the one state nobody asked for: the requester asked again in
#: the same thread, so this form was retired unclicked. Its copy names no
#: agent and no target — the newer card below it already does, and two cards
#: naming one key is what made the pair confusing in the first place.
REPLACED_HEADLINE: Final[str] = "⌛ This form was replaced."
_REPLACED_FACT: Final[str] = "Use the newer form below."

# Refusals for a click that cannot be honoured at all. These are the ephemeral
# replies both adapters were spelling out separately; they live here so the two
# copies cannot drift. `expired_message` replaces the fourth one, which was a
# bare "this expired" with no way back.
NO_LONGER_VALID_MESSAGE: Final[str] = "This request is no longer valid — ask again."
WRONG_REQUESTER_MESSAGE: Final[str] = (
    "This request was for someone else — ask again in your own thread."
)
ALREADY_USED_MESSAGE: Final[str] = "This request was already used — ask again."

_BUTTON_LABEL: Final[dict[CardKind, str]] = {
    "env": "🔐 Enter it privately",
    "env_file": "🔐 Upload it privately",
    "mcp": "🔐 Enter the token privately",
    "mcp_oauth": "🔗 Connect my account",
    "repo": "🔐 Use my GitHub token",
    "skill_repo": "🔐 Use my GitHub token",
}

# Leading emoji → state, for a reader (parity driver, QA script) holding only
# the rendered headline. The mapping is deliberately lossy in two places: a
# `received` card repeats its `requested` headline verbatim (only the footer
# changes), and `superseded` shares ⚠️ with `partial`. `replaced` shares ⌛
# with `expired` but is recoverable, because its headline is one fixed string
# no other state writes. A caller that needs the exact state reads
# `PostedCard.state`; this is for after the round trip through a platform,
# where only text survives.
_STATE_BY_EMOJI: Final[dict[str, CardState]] = {
    "🔑": "requested",
    "🔌": "requested",
    "📦": "requested",
    "🧩": "requested",
    "✅": "applied",
    "⚠️": "partial",
    "⌛": "expired",
    "🛡️": "refused",
}


class CardButton(BaseModel):
    """One button on a posted card.

    A `primary` button opens the private form and carries the request token in
    its `custom_id`; a `link` button just sends the person to a URL.
    """

    model_config = ConfigDict(frozen=True)

    label: str
    style: Literal["primary", "link"]
    custom_id: str | None = None
    url: str | None = None

    @model_validator(mode="after")
    def _check_style_matches_target(self) -> Self:
        if self.style == "primary" and (self.custom_id is None or self.url is not None):
            raise ValueError("style='primary' needs a custom_id and no url")
        if self.style == "link" and (self.url is None or self.custom_id is not None):
            raise ValueError("style='link' needs a url and no custom_id")
        return self


class PostedCard(BaseModel):
    """The public card for one posted control, in one state."""

    model_config = ConfigDict(frozen=True)

    kind: CardKind
    state: CardState
    headline: str
    facts: tuple[str, ...] = ()
    buttons: tuple[CardButton, ...] = ()
    footer: str | None = None
    requester_platform_user_id: str | None = None
    expires_at_unix: int | None = None

    @model_validator(mode="after")
    def _check_requested_only_fields(self) -> Self:
        requested = self.state == "requested"
        if self.buttons and not requested:
            raise ValueError(f"state={self.state!r} must not carry buttons")
        if requested and not self.buttons:
            raise ValueError("state='requested' must carry a button")
        if (self.requester_platform_user_id is not None) != requested:
            raise ValueError("requester_platform_user_id belongs to state='requested' only")
        if (self.expires_at_unix is not None) != requested:
            raise ValueError("expires_at_unix belongs to state='requested' only")
        if self.footer is not None and self.state not in ("requested", "received"):
            raise ValueError(f"state={self.state!r} has no footer")
        return self


def card_text(card: PostedCard) -> str:
    """Render the card's headline and facts as plain lines, footer excluded.

    The footer is left out because its `requested` form still holds
    placeholders only a renderer can fill.
    """
    return "\n".join((card.headline, *card.facts))


def classify_card_state(headline: str) -> CardState | None:
    """Return the state a rendered headline announces, by its leading emoji.

    `None` when the line starts with no state emoji. The mapping is lossy: a
    `received` headline reads as `requested` and a `superseded` one as
    `partial`, because those pairs share their emoji by design. `replaced` is
    the one ⌛ collision that is not lossy — it writes one fixed headline, so
    matching that string exactly is checked before the emoji table.
    """
    if headline == REPLACED_HEADLINE:
        return "replaced"
    for emoji, state in _STATE_BY_EMOJI.items():
        if headline.startswith(f"{emoji} "):
            return state
    return None


def _requested_content(
    kind: CardKind,
    *,
    agent_name: str,
    target: str,
    repo_display: str,
    branch: str | None,
    mcp_server_url: str | None,
    skill_repo_isolated: bool,
) -> tuple[str, tuple[str, ...]]:
    if kind == "env":
        return (
            f"🔑 Add {target} to {agent_name}",
            (
                f"Anyone who talks to {agent_name} can use it.",
                "The value is not shown in chat.",
            ),
        )
    if kind == "env_file":
        return (
            f"🔑 Add keys to {agent_name} from a .env file",
            (
                "One KEY=VALUE per line.",
                "Daimon stores the keys, not a retained copy of your uploaded file.",
                f"Anyone who talks to {agent_name} can use them.",
            ),
        )
    if kind == "mcp":
        if mcp_server_url is None:
            raise ValueError("kind='mcp' requires mcp_server_url")
        return (
            f"🔌 Connect {agent_name} to {target}",
            (
                f"{mcp_server_url} needs a token.",
                f"Anyone who talks to {agent_name} can use this connection.",
            ),
        )
    if kind == "mcp_oauth":
        if mcp_server_url is None:
            raise ValueError("kind='mcp_oauth' requires mcp_server_url")
        return (
            f"🔌 Connect {agent_name} to {target} with your account",
            (
                f"{mcp_server_url} signs you in through your browser.",
                "Only you can use your connection; others connect their own.",
            ),
        )
    if branch is None:
        raise ValueError(f"kind={kind!r} requires branch")
    branch_line = f"Branch `{branch}`."
    token_line = f"The token stays yours; {agent_name} uses it whenever it needs GitHub."
    if kind == "repo":
        return (
            f"📦 Give {agent_name} access to {repo_display}",
            (
                branch_line,
                f"This repo is private, so {agent_name} needs a way in.",
                token_line,
            ),
        )
    isolated_lines = (
        (f"Skill repo only — {agent_name}'s working repo does not change.",)
        if skill_repo_isolated
        else ()
    )
    return (
        f"🧩 Add skills to {agent_name} from {repo_display}",
        (*isolated_lines, branch_line, token_line),
    )


def _outcome_content(state: CardState, outcome: ConfigurationChange | None) -> tuple[str, ...]:
    if outcome is None:
        raise ValueError(f"state={state!r} requires outcome")
    if outcome.availability == "ready_now":
        # A card is edited after the save, before the agent's next turn; it can
        # never truthfully promise the agent already has the value in hand.
        raise ValueError("a posted card cannot claim availability='ready_now'")
    lines = render_change_confirmation(outcome).split("\n")
    marker = "✅" if state == "applied" else "⚠️"
    return (f"{marker} {lines[0]}", *lines[1:])


def _expired_fact(
    kind: CardKind, *, agent_name: str, responder_name: str, target: str, repo_display: str
) -> str:
    if kind == "env":
        phrase = f"add {target} to {agent_name}"
    elif kind == "env_file":
        phrase = f"add keys to {agent_name} from a file"
    elif kind in ("mcp", "mcp_oauth"):
        phrase = f"connect {agent_name} to {target}"
    elif kind == "repo":
        phrase = f"give {agent_name} access to {repo_display}"
    else:
        phrase = f"add skills to {agent_name} from {repo_display}"
    return f"Ask {responder_name} to {phrase} again."


def expired_message(
    *,
    kind: CardKind,
    agent_name: str,
    responder_name: str,
    target: str,
    repo: str | None = None,
) -> str:
    """Return the expired copy: what happened, and the way to ask again.

    Identical to the text of the `expired` card, so the ephemeral a late
    clicker gets says exactly what the card next to it now says.
    """
    return "\n".join(
        (
            EXPIRED_HEADLINE,
            _expired_fact(
                kind,
                agent_name=agent_name,
                responder_name=responder_name,
                target=target,
                repo_display=repo or target,
            ),
        )
    )


def _refusal_content(
    refusal: RefusalReason,
    *,
    agent_name: str,
    responder_name: str,
    target: str,
    repo_display: str,
    refusal_lines: Sequence[str],
    replaces: str | None = None,
) -> tuple[str, ...]:
    if refusal == "admin_required":
        return (
            f"🛡️ {agent_name}'s working repo was not changed.",
            f"An admin can ask {responder_name} to give {agent_name} access to {repo_display}.",
            "Nothing was saved.",
        )
    if refusal == "replacement_admin_required" and replaces is not None:
        return (
            f"🛡️ {target} was not added for {agent_name}.",
            f"Adding {target} {env_shadow_phrase(target, replaces)}. An admin can ask "
            f"{responder_name} to replace {replaces} on {agent_name}.",
            f"The existing {replaces} is unchanged.",
        )
    if refusal == "replacement_admin_required":
        return (
            f"🛡️ {target} was not replaced for {agent_name}.",
            f"An admin can ask {responder_name} to replace {target} on {agent_name}.",
            "The existing key is unchanged.",
        )
    if refusal == "target_unavailable":
        return (
            f"🛡️ Nothing was saved for {agent_name}.",
            f"{agent_name} is not available right now. Ask {responder_name} again.",
        )
    if refusal == "sign_in_declined":
        return (
            f"🛡️ Nothing was connected for {agent_name}.",
            f"Sign-in was cancelled. Ask {responder_name} to connect {target} again "
            "whenever you want to.",
        )
    if refusal == "token_rejected":
        return (
            f"🛡️ {target} did not accept that token for {agent_name}.",
            "Nothing was saved.",
            f"If {target} signs people in through a browser, ask {responder_name} to "
            "connect it with your account instead.",
        )
    if not refusal_lines:
        raise ValueError("refusal='env_file_invalid' requires refusal_lines")
    return (f"🛡️ No keys were saved for {agent_name}.", *refusal_lines)


def _superseded_content(*, agent_name: str, responder_name: str, target: str) -> tuple[str, ...]:
    return (
        f"⚠️ {target} was not replaced for {agent_name}.",
        "Someone else changed it while this form was open.",
        "The current value is unchanged.",
        f"Ask {responder_name} to replace {target} on {agent_name} again.",
    )


def build_posted_card(
    *,
    kind: CardKind,
    state: CardState,
    agent_name: str,
    responder_name: str,
    target: str,
    requester_platform_user_id: str,
    expires_at: datetime,
    token: str,
    mcp_server_url: str | None = None,
    repo: str | None = None,
    branch: str | None = None,
    skill_repo_isolated: bool = False,
    outcome: ConfigurationChange | None = None,
    refusal: RefusalReason | None = None,
    refusal_lines: Sequence[str] = (),
    replaces: str | None = None,
) -> PostedCard:
    """Build the card for one posted control in one state.

    `target` is the per-kind subject the request row already carries: the key
    name for `env`, the server name for `mcp`, `owner/repo` for the two repo
    kinds (`repo` overrides it for display when the row holds a full URL), and
    nothing meaningful for `env_file`. `replaces` names the held key an `env`
    alias would have retargeted, for a `replacement_admin_required` refusal.

    Raises `ValueError` for a combination that cannot exist: an outcome state
    without an `outcome`, a `refused` state without a `refusal`, an `mcp`
    request without its URL, a repo request without a branch, or an outcome
    whose availability is `ready_now`.
    """
    if (outcome is not None) != (state in ("applied", "partial")):
        raise ValueError("outcome belongs to state='applied' or state='partial' only")
    if (refusal is not None) != (state == "refused"):
        raise ValueError("refusal belongs to state='refused' only")
    if refusal_lines and refusal != "env_file_invalid":
        raise ValueError("refusal_lines belong to refusal='env_file_invalid' only")

    repo_display = repo or target

    if state in ("requested", "received"):
        headline, facts = _requested_content(
            kind,
            agent_name=agent_name,
            target=target,
            repo_display=repo_display,
            branch=branch,
            mcp_server_url=mcp_server_url,
            skill_repo_isolated=skill_repo_isolated,
        )
        if state == "received":
            return PostedCard(
                kind=kind, state=state, headline=headline, facts=facts, footer=RECEIVED_FOOTER
            )
        if expires_at.tzinfo is None:
            raise ValueError("expires_at must be timezone-aware")
        button = CardButton(
            label=_BUTTON_LABEL[kind], style="primary", custom_id=build_custom_id(token)
        )
        return PostedCard(
            kind=kind,
            state=state,
            headline=headline,
            facts=facts,
            buttons=(button,),
            footer=FOOTER_TEMPLATE,
            requester_platform_user_id=requester_platform_user_id,
            expires_at_unix=int(expires_at.timestamp()),
        )

    if state in ("applied", "partial"):
        lines = _outcome_content(state, outcome)
    elif state == "expired":
        lines = (
            EXPIRED_HEADLINE,
            _expired_fact(
                kind,
                agent_name=agent_name,
                responder_name=responder_name,
                target=target,
                repo_display=repo_display,
            ),
        )
    elif state == "replaced":
        lines = (REPLACED_HEADLINE, _REPLACED_FACT)
    elif state == "refused":
        if refusal is None:
            raise ValueError("state='refused' requires refusal")
        lines = _refusal_content(
            refusal,
            agent_name=agent_name,
            responder_name=responder_name,
            target=target,
            repo_display=repo_display,
            refusal_lines=refusal_lines,
            replaces=replaces,
        )
    else:
        lines = _superseded_content(
            agent_name=agent_name, responder_name=responder_name, target=target
        )

    return PostedCard(kind=kind, state=state, headline=lines[0], facts=tuple(lines[1:]))


def card_for_request(
    row: CredentialRequestRow,
    *,
    state: CardState,
    outcome: ConfigurationChange | None = None,
    refusal: RefusalReason | None = None,
    refusal_lines: Sequence[str] = (),
    replaces: str | None = None,
) -> PostedCard:
    """The card for one request row in `state`, rebuilt from the row alone.

    Edits run in another process long after the post, so the durable row is
    the only input. The repo kinds pack the branch into `target`.
    """
    repo = branch = None
    if row.kind in ("repo", "skill_repo"):
        repo_url, branch, _path = split_skill_repo_target(row.target)
        repo = normalize_owner_repo(repo_url)
    return build_posted_card(
        kind=cast("CardKind", row.kind),
        state=state,
        agent_name=row.target_name or "the agent",
        responder_name=row.responder_name or "Daimon",
        target=row.target,
        requester_platform_user_id=row.requester_platform_user_id,
        expires_at=row.expires_at,
        token=row.token,
        mcp_server_url=row.mcp_server_url,
        repo=repo,
        branch=branch,
        outcome=outcome,
        refusal=refusal,
        refusal_lines=refusal_lines,
        replaces=replaces,
    )
