"""Rows for support plans, catalogs, allowances and requests (see `daimon.core.support_plans`)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from daimon.core.support_plans import (
    DEFAULT_CATALOG,
    DEFAULT_REVISION,
    Balance,
    ChargedTo,
    OptionSnapshot,
    PlanScope,
    PriceBounds,
    RequestStatus,
    SupportCatalogSpec,
    SupportOption,
    may_submit,
)
from daimon.core.windows import BudgetWindow, WindowState
from pydantic import BaseModel, ConfigDict


class _Row(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)


class SupportCatalogRow(_Row):
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
    def spec(self) -> SupportCatalogSpec:
        return SupportCatalogSpec(name=self.name, options=self.options)


class SupportPlanRow(_Row):
    """Credits for a workspace, an agent or a channel; `catalog_id` None is the default menu."""

    id: uuid.UUID
    tenant_id: uuid.UUID
    platform: str
    scope: PlanScope
    scope_id: str
    catalog_id: uuid.UUID | None
    pool: Decimal | None
    per_person: Decimal | None
    window: BudgetWindow
    starts_at: datetime | None
    ends_at: datetime | None
    set_by_account_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class SupportAllowanceRow(_Row):
    """One person's own cap under a plan, or under the default plan when `plan_id` is None."""

    id: uuid.UUID
    tenant_id: uuid.UUID
    plan_id: uuid.UUID | None
    platform_user_id: str
    credits: Decimal
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
    plan_id: uuid.UUID | None = None
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
        default = DEFAULT_CATALOG.options[0]
        return OptionSnapshot(key=default.key, label=default.label, price=default.price).bounds


@dataclass(frozen=True)
class PlanView:
    """The plan one person asks under in one place, read for one decision (never cached).

    `plan` None is the implicit default plan and `catalog` None the default
    menu. `exempt` is a tenant admin (decision 5): recorded, charged nowhere.
    """

    plan: SupportPlanRow | None
    catalog: SupportCatalogRow | None
    balance: Balance
    state: WindowState
    exempt: bool = False

    @property
    def spec(self) -> SupportCatalogSpec:
        return DEFAULT_CATALOG if self.catalog is None else self.catalog.spec

    @property
    def revision(self) -> int:
        """What a menu carries and a submit compares; the default menu is 0."""
        return DEFAULT_REVISION if self.catalog is None else self.catalog.revision

    @property
    def charged_to(self) -> ChargedTo:
        if self.exempt:
            return "exempt"
        return "default" if self.plan is None else "plan"

    @property
    def active(self) -> bool:
        return self.state == "active"

    @property
    def available(self) -> Decimal | None:
        """What a request may hold now: 0 outside the window, None unbounded (or exempt)."""
        if not self.active:
            return Decimal(0)
        return None if self.exempt else self.balance.available

    def affordable(self, bounds: PriceBounds) -> bool:
        """Whether an option on these terms can be submitted now."""
        return self.active and may_submit(bounds, self.available)


__all__ = [
    "PlanView",
    "SupportAllowanceRow",
    "SupportCatalogRow",
    "SupportEscalationRow",
    "SupportPlanRow",
]
