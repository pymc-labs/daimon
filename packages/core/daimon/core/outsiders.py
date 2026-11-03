"""Visibility facts supplied by adapters, separate from channel reading rules."""

from pydantic import BaseModel, ConfigDict

OUTSIDERS_WARNING = (
    "⚠ People outside your organisation can view this channel. It is not confidential."
)
INTERNAL_ROLES_HINT = "Name your internal roles to check this"
OUTSIDER_CHECK_SKIPPED = "Outsider check skipped; outside access could not be checked."


class OutsiderVisibility(BaseModel):
    model_config = ConfigDict(frozen=True)

    outsiders_can_view: bool | None = None
    note: str | None = None

    def lines(self, *, readers: str) -> tuple[str, ...]:
        if readers == "own":
            return ()
        return (
            *((OUTSIDERS_WARNING,) if self.outsiders_can_view else ()),
            *((self.note,) if self.note else ()),
        )
