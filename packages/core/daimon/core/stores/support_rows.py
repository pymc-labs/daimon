"""Rows for support limits, menus, channels and requests (see `daimon.core.support`)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from daimon.core.support import (
    DEFAULT_MENU,
    Balance,
    ChargedTo,
    MenuSpec,
    OptionSnapshot,
    PriceBounds,
    RequestStatus,
    SupportOption,
    SupportScope,
    SupportWindow,
    may_submit,
)
from daimon.core.windows import WindowState
from pydantic import BaseModel, ConfigDict


class _Row(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)


class SupportMenuRow(_Row):
    """A tenant's option menu; `revision` rises on every set."""

    id: uuid.UUID
    tenant_id: uuid.UUID
    key: str
    name: str
    revision: int
    options: tuple[SupportOption, ...]
    set_by_account_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime

    @property
    def spec(self) -> MenuSpec:
        return MenuSpec(name=self.name, options=self.options)


class SupportMenuPlaceRow(_Row):
    """The menu one place uses."""

    tenant_id: uuid.UUID
    scope: SupportScope
    scope_id: str
    menu_id: uuid.UUID
    set_by_account_id: uuid.UUID | None
    updated_at: datetime


class SupportLimitRow(_Row):
    """A place's credits, or one person's own limit there (`platform_user_id` set)."""

    id: uuid.UUID
    tenant_id: uuid.UUID
    scope: SupportScope
    scope_id: str
    platform_user_id: str | None
    shared: Decimal | None
    each_person: Decimal | None
    window: SupportWindow
    starts_at: datetime | None
    ends_at: datetime | None
    revision: int
    set_by_account_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class SupportChannelRow(_Row):
    """A workspace's own request channel."""

    tenant_id: uuid.UUID
    platform: str
    channel_id: str
    slack_team_id: str | None
    teams_team_id: str | None
    teams_service_url: str | None
    revision: int
    set_by_account_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class SupportEscalationRow(_Row):
    """One human-support request; the ledger fields default to a legacy row charged 1."""

    id: uuid.UUID
    tenant_id: uuid.UUID
    account_id: uuid.UUID | None
    platform: str
    platform_user_id: str
    channel_id: str
    message_id: str
    ma_session_id: str | None
    note: str
    delivered_at: datetime | None
    created_at: datetime
    charged_to: ChargedTo = "default"
    limit_id: uuid.UUID | None = None
    person_limit_id: uuid.UUID | None = None
    option_key: str | None = None
    option: OptionSnapshot | None = None
    answers: dict[str, str] | None = None
    hold: Decimal = Decimal(1)
    charge: Decimal | None = Decimal(1)
    status: RequestStatus = "settled"
    settled_at: datetime | None = None
    settled_by: str | None = None
    settled_by_account_id: uuid.UUID | None = None
    support_channel_id: str | None = None
    support_post_id: str | None = None

    @property
    def counted(self) -> Decimal:
        """What this row spends now: its charge once settled, its hold while pending."""
        return self.hold if self.charge is None else self.charge

    @property
    def bounds(self) -> PriceBounds:
        """The terms it was requested on; a legacy row is the default option."""
        if self.option is not None:
            return self.option.bounds
        default = DEFAULT_MENU.options[0]
        return OptionSnapshot(key=default.key, label=default.label, price=default.price).bounds


@dataclass(frozen=True)
class SupportSetup:
    """What a workspace set up for Ask a person, as the support requirements read it:
    the platform of its own channel, and whether any place has a limit."""

    own_platform: str | None = None
    place_limits: bool = False


@dataclass(frozen=True)
class LimitView:
    """The ledger, menu and balance one person asks under in one place (never cached).

    `limit` None is the default ledger, `person` the person's own limit that
    replaces the cap, `menu` None the built-in menu. `exempt` is a tenant
    admin: recorded, charged nowhere. `state` is the stricter of both rows'.
    """

    limit: SupportLimitRow | None
    person: SupportLimitRow | None
    menu: SupportMenuRow | None
    balance: Balance
    state: WindowState
    exempt: bool = False

    @property
    def spec(self) -> MenuSpec:
        return DEFAULT_MENU if self.menu is None else self.menu.spec

    @property
    def period(self) -> SupportLimitRow | None:
        """The row whose period the person's balance reads in."""
        return self.person or self.limit

    @property
    def charged_to(self) -> ChargedTo:
        if self.exempt:
            return "exempt"
        return "default" if self.limit is None else "limit"

    @property
    def active(self) -> bool:
        return self.state == "active"

    @property
    def available(self) -> Decimal | None:
        """What a request may hold now: 0 outside the dates, None unbounded (or exempt)."""
        if not self.active:
            return Decimal(0)
        return None if self.exempt else self.balance.available

    def affordable(self, bounds: PriceBounds) -> bool:
        """Whether an option on these terms can be submitted now."""
        return self.active and may_submit(bounds, self.available)


__all__ = [
    "LimitView",
    "SupportChannelRow",
    "SupportEscalationRow",
    "SupportLimitRow",
    "SupportMenuPlaceRow",
    "SupportMenuRow",
    "SupportSetup",
]
