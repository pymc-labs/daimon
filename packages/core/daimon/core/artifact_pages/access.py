"""Who may open a page, what each role may do with it, and its settings' hard rules.

Roles follow Google Drive: the owner and workspace admins do everything; editors
republish and change settings, and change sharing unless `editors_can_share` is off;
only the owner and admins delete, transfer or turn editor sharing off. An editor may
always leave. While editors may not share, a page seen outside the workspace keeps its
data, events, data scale and collection rules too: those decide what outsiders may do.
A public page's viewers are anonymous, so it cannot take their data writes, cannot both
read data and send events (an event could talk an agent into writing what the sender reads
back), and cannot ask (no person to run the turn as).
"""

from __future__ import annotations

import hmac
import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Literal

from daimon.core.artifact_pages.documents import check_rules
from daimon.core.artifact_pages.model import ArtifactError, Limits
from daimon.core.stores.domain import (
    ArtifactAccess,
    ArtifactRow,
    CollectionRule,
    DataAccess,
    WebPersonKind,
)
from pydantic import BaseModel, ConfigDict, Field

Role = Literal["owner", "admin", "editor", "viewer", "none"]
Sender = Literal["member", "visitor", "anonymous"]
_MANAGERS: Final[frozenset[Role]] = frozenset({"owner", "admin", "editor"})
DEFAULT_PUBLIC_DAYS: Final = 30
MAX_EXPIRY_DAYS: Final = 3650
ASK_OWNER: Final = "Ask the owner or an admin."


@dataclass(frozen=True)
class Viewer:
    """Who opened a page: a person of the page's own workspace, or anonymous
    (`account_id` None). `link` is the secret they gave, if any. `kind` is "visitor" for an
    outsider too (`standing`), who is no admin."""

    account_id: uuid.UUID | None = None
    person_id: uuid.UUID | None = None
    name: str | None = None
    kind: WebPersonKind | None = None
    is_admin: bool = False
    link: str | None = None

    @property
    def is_visitor(self) -> bool:
        return self.kind == "visitor"


class PageSettings(BaseModel):
    """Who may open a page and what it may do; the same for every viewer."""

    model_config = ConfigDict(frozen=True)

    access: ArtifactAccess = "workspace"
    people: tuple[uuid.UUID, ...] = ()
    editors: tuple[uuid.UUID, ...] = ()
    editors_can_share: bool = True
    data: DataAccess = "off"
    events: bool = False
    ask: bool = False
    data_scale: int = 1
    expires_at: datetime | None = None
    collection_rules: dict[str, CollectionRule] = Field(default_factory=dict[str, CollectionRule])


class PageChange(BaseModel):
    """A partial change (web PATCH, MCP republish): only the fields set apply, and
    `expires_at=None` set explicitly means never."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str | None = None
    access: ArtifactAccess | None = None
    people: tuple[uuid.UUID, ...] | None = None
    editors: tuple[uuid.UUID, ...] | None = None
    editors_can_share: bool | None = None
    data: DataAccess | None = None
    events: bool | None = None
    ask: bool | None = None
    data_scale: int | None = None
    expires_at: datetime | None = None
    collection_rules: dict[str, CollectionRule] | None = None


def settings_of(artifact: ArtifactRow) -> PageSettings:
    return PageSettings.model_validate(artifact.model_dump(include=set(PageSettings.model_fields)))


def apply_change(
    settings: PageSettings, change: PageChange, *, owner_account_id: uuid.UUID
) -> PageSettings:
    """The settings after `change`. People and editors are deduplicated; the owner is
    dropped from both, being more than either."""
    values = settings.model_dump()
    values.update(
        {k: getattr(change, k) for k in change.model_fields_set if k in PageSettings.model_fields}
    )
    for key in ("people", "editors"):
        values[key] = tuple(sorted(set(values[key]) - {owner_account_id}, key=str))
    return PageSettings.model_validate(values)


def check_settings(settings: PageSettings) -> None:
    check_rules(settings.collection_rules)
    if not 1 <= settings.data_scale <= Limits.MAX_DATA_SCALE:
        raise ArtifactError("invalid", f"Data space is 1-{Limits.MAX_DATA_SCALE} times.")
    if len(settings.people) > Limits.PEOPLE:
        raise ArtifactError("invalid", f"At most {Limits.PEOPLE} people per artifact.")
    if len(settings.editors) > Limits.EDITORS:
        raise ArtifactError("invalid", f"At most {Limits.EDITORS} editors per artifact.")
    if settings.access != "public":
        return
    if settings.data == "write":
        raise ArtifactError("invalid", "A public page cannot write its data; set data to read.")
    if settings.data == "read" and settings.events:
        raise ArtifactError("invalid", "A public page can read its data or send events, not both.")
    if settings.ask:
        raise ArtifactError("invalid", "A public page cannot ask the agent.")


def check_change(
    current: PageSettings,
    new: PageSettings,
    *,
    role: Role,
    now: datetime,
    actor: uuid.UUID | None = None,
    outside: bool = False,
) -> None:
    """Whether `role` (the account `actor`) may move a page from `current` to `new`; then
    its hard rules. `outside`: people outside the workspace can read the page now."""
    if not can_edit(role):
        raise ArtifactError("forbidden", ASK_OWNER)
    sharing = ["access", "people", "editors", "expires_at"]
    if outside:
        sharing += ["data", "events", "data_scale", "collection_rules"]
    leaving = tuple(e for e in current.editors if e != actor)
    changed = [k for k in sharing if getattr(current, k) != getattr(new, k)]
    if changed == ["editors"] and new.editors == leaving:
        changed = []
    if changed and not can_share(current, role):
        raise ArtifactError("forbidden", ASK_OWNER)
    if current.editors_can_share != new.editors_can_share and not can_delete(role):
        raise ArtifactError("forbidden", ASK_OWNER)
    expiry, limit = new.expires_at, now + timedelta(days=MAX_EXPIRY_DAYS)
    if expiry is not None and expiry != current.expires_at and not now < expiry <= limit:
        raise ArtifactError("invalid", f"An expiry is up to {MAX_EXPIRY_DAYS} days away.")
    check_settings(new)


def resolve_expiry(
    access: ArtifactAccess, days: int | None, *, current: datetime | None, now: datetime
) -> datetime | None:
    """`days`: None keeps `current`, 0 means never. A page going public with no expiry
    gets 30 days, so a forgotten link dies."""
    if days is None:
        if access == "public" and current is None:
            return now + timedelta(days=DEFAULT_PUBLIC_DAYS)
        return current
    if not 0 <= days <= MAX_EXPIRY_DAYS:
        raise ArtifactError("invalid", f"Expiry is 0 (never) to {MAX_EXPIRY_DAYS} days.")
    return now + timedelta(days=days) if days else None


def is_expired(artifact: ArtifactRow, now: datetime) -> bool:
    return artifact.expires_at is not None and artifact.expires_at <= now


def role_of(
    artifact: ArtifactRow, account_id: uuid.UUID | None, *, is_admin: bool, visitor: bool
) -> Role:
    """The account's role on the page, admission aside: `viewer` means listed in `people`.
    A visitor (`standing`) manages nothing: a role stored for them reads as the plain
    `viewer` of a page that names them, and comes back if they are a member again."""
    if account_id is None:
        return "none"
    if visitor:
        return "viewer" if account_id in readers(artifact) else "none"
    if account_id == artifact.owner_account_id:
        return "owner"
    if is_admin:
        return "admin"
    if account_id in artifact.editors:
        return "editor"
    return "viewer" if account_id in artifact.people else "none"


def role_for(artifact: ArtifactRow, viewer: Viewer) -> Role:
    return role_of(artifact, viewer.account_id, is_admin=viewer.is_admin, visitor=viewer.is_visitor)


def admit(artifact: ArtifactRow, viewer: Viewer, now: datetime) -> Role:
    """The viewer's role if they may open the page now, else `none`. The owner, editors
    and admins always may; on a public page anyone else needs its link."""
    if is_expired(artifact, now):
        return "none"
    role = role_for(artifact, viewer)
    if role in _MANAGERS:
        return role
    if artifact.access == "public":
        return "viewer" if is_link(artifact, viewer.link) else "none"
    if artifact.access == "workspace" and viewer.kind == "member":
        return "viewer"
    return role


def is_link(artifact: ArtifactRow, given: str | None) -> bool:
    expected = artifact.link_secret
    if expected is None or given is None:
        return False
    return hmac.compare_digest(expected.encode(), given.encode())


def can_edit(role: Role) -> bool:
    return role in _MANAGERS


def can_share(artifact: ArtifactRow | PageSettings, role: Role) -> bool:
    """Change access, people, editors, expiry or the link."""
    return role in ("owner", "admin") or (role == "editor" and artifact.editors_can_share)


def can_delete(role: Role) -> bool:
    """Delete, transfer, or turn editor sharing on or off."""
    return role in ("owner", "admin")


def can_use_with_tools(artifact: ArtifactRow, role: Role) -> bool:
    """Tools read and write a page's data and watch its events: anyone in the workspace
    on a workspace page; only its owner, editors and admins on any other."""
    return artifact.access == "workspace" or role in _MANAGERS


def shown_as(artifact: ArtifactRow, viewer: Viewer, role: Role) -> Viewer:
    """Who the page's events name: a public page's viewers stay anonymous, signed in or not."""
    return viewer if artifact.access != "public" or can_edit(role) else Viewer()


def sender_of(viewer: Viewer) -> Sender:
    if viewer.account_id is None:
        return "anonymous"
    return "visitor" if viewer.kind == "visitor" else "member"


def readers(artifact: ArtifactRow) -> tuple[uuid.UUID, ...]:
    """Everyone named on the page: its owner, editors and people."""
    return (artifact.owner_account_id, *artifact.editors, *artifact.people)


def visible_outside_workspace(artifact: ArtifactRow, visitor_ids: Collection[uuid.UUID]) -> bool:
    """Whether people outside the workspace can read the page: an agent writing to it
    writes for them. `visitor_ids` are the visitors among its `readers`."""
    return artifact.access == "public" or any(i in visitor_ids for i in readers(artifact))
