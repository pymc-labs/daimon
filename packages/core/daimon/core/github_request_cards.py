"""GitHub request wording for requester-bound Discord and ephemeral Slack cards."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from daimon.core.stores.github_access_requests import AccessRequest


@dataclass(frozen=True)
class RequestCard:
    text: str
    primary: str | None
    secondary: tuple[str, ...] = ()


def requester_card(
    request: AccessRequest,
    *,
    connected_names: tuple[str, ...],
    asker_is_admin: bool,
    needs_more_ability: bool = False,
    ability: Literal["read", "write"],
) -> RequestCard:
    """A name is shown only after a caller proved it is connected here."""
    if request.status == "waiting_github":
        return RequestCard("Waiting for GitHub confirmation.", None, ("Cancel request",))
    if not asker_is_admin:
        if request.admin_notified_at is None:
            return RequestCard(
                "Admin unavailable. Ask an admin to open `/github`.",
                None,
                ("Cancel request",),
            )
        return RequestCard(
            "GitHub access pending. An admin has been asked. This request continues "
            "when access is ready.",
            None,
            ("Cancel request",),
        )
    name = ", ".join(connected_names) if connected_names else "a repo that isn't connected yet"
    if needs_more_ability and connected_names:
        title = f"Let {request.agent_name} open issues and pull requests in {name}?"
        action = "Allow"
    else:
        title = f"Let {request.agent_name} use {name}?"
        action = (
            ("Add repos" if len(connected_names) > 1 else "Add repo")
            if connected_names
            else "Connect and add"
        )
    if not connected_names:
        title += " (not connected yet)"
    level = "Read only" if ability == "read" else "Read and write"
    detail = f"Can: {level}."
    if not connected_names:
        detail = "GitHub will ask someone who manages it to confirm.\n" + detail
    return RequestCard(f"{title}\n{detail}", action, ("Cancel request",))


def admin_card(
    request: AccessRequest,
    *,
    connected_names: tuple[str, ...],
    channel_label: str,
    requester_label: str,
    ability: Literal["read", "write"],
) -> RequestCard:
    """An unconnected name stays hidden until the reader proves GitHub visibility."""
    if request.status == "waiting_github":
        return RequestCard("Waiting for GitHub confirmation.", None, ("Hide for me",))
    where = f"in {channel_label}" if channel_label else "in a direct message"
    heading = (
        f"{request.agent_name} needs GitHub access to continue {requester_label}'s request {where}."
    )
    names = ", ".join(connected_names) if connected_names else "a repo that isn't connected yet"
    level = "Read only" if ability == "read" else "Read and write"
    action = (
        ("Add repos" if len(connected_names) > 1 else "Add repo")
        if connected_names
        else "Connect and add"
    )
    return RequestCard(
        f"{heading}\n{names}\nCan: {level}",
        action,
        ("Decline", "Hide for me"),
    )
