"""redeem_promo_code: admin-only, redeems through core and explains the result."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.promo_codes import (
    _redeem_promo_code_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.promo_codes import build_promo_code_terms, hash_promo_code, normalize_promo_code
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import promo_codes as promo_store
from daimon.core.stores import tenant_ledger
from daimon.core.stores.domain import Role
from daimon.testing.factories import make_account, make_tenant
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

Factory = async_sessionmaker[AsyncSession]


def _runtime(factory: Factory) -> McpRuntime:
    return McpRuntime(
        session_factory=factory,
        client=MagicMock(spec=AsyncAnthropic),  # type: ignore[arg-type]
        settings=MagicMock(),  # type: ignore[arg-type]
        deployment_default=DeploymentDefault(),
    )


async def _setup(session: AsyncSession, *, is_admin: bool = True) -> AuthIdentity:
    tenant = await make_tenant(session)
    account = await make_account(session, tenant=tenant)
    return AuthIdentity(
        account_id=account.id,
        tenant_id=tenant.id,
        role=Role.ADMIN if is_admin else Role.USER,
        is_admin=is_admin,
    )


async def _code(session: AsyncSession, code: str, **kwargs: object) -> None:
    terms = build_promo_code_terms(amount_usd=Decimal("12.5"), **kwargs)  # type: ignore[arg-type]
    await promo_store.insert_promo_code(
        session, code_hash=hash_promo_code(normalize_promo_code(code)), terms=terms
    )


async def test_admin_redeems_credit(db_session: AsyncSession, db_session_factory: Factory) -> None:
    auth = await _setup(db_session)
    await _code(db_session, "WELCOME-2026", timed=False)
    result = await _redeem_promo_code_impl(_runtime(db_session_factory), auth, "welcome 2026")
    assert result.redeemed and result.kind == "credit"
    assert (result.amount_usd, result.balance_usd) == ("12.50", "12.50")
    assert result.message == "Redeemed $12.50 of credit."
    assert await tenant_ledger.get_balance(db_session, tenant_id=auth.tenant_id) == Decimal("12.5")
    [row] = await promo_store.list_promo_codes(db_session)
    [redemption] = await promo_store.list_redemptions(db_session, promo_code_id=row.id)
    assert redemption.redeemed_by_account_id == auth.account_id


async def test_timed_credit_that_starts_later_names_its_window(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    auth = await _setup(db_session)
    start = datetime.now(UTC).replace(microsecond=0) + timedelta(days=1)
    await _code(
        db_session,
        "TIMED-2026",
        timed=True,
        credit_starts_at=start,
        credit_ends_at=start + timedelta(days=2),
    )
    result = await _redeem_promo_code_impl(_runtime(db_session_factory), auth, "TIMED-2026")
    assert result.redeemed and result.kind == "timed" and result.balance_usd == "0.00"
    assert result.credit_starts_at == start
    assert f"from {start:%Y-%m-%d %H:%M} UTC until" in result.message


async def test_refusal_is_a_result_not_an_error(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    auth = await _setup(db_session)
    result = await _redeem_promo_code_impl(_runtime(db_session_factory), auth, "NOPE-NOPE")
    assert (result.redeemed, result.refusal) == (False, "invalid")
    assert result.message == "That code is not valid. Check it and try again."


async def test_non_admin_is_refused_before_redeeming(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    auth = await _setup(db_session, is_admin=False)
    await _code(db_session, "WELCOME-2026", timed=False)
    with pytest.raises(ToolError, match="requires a workspace or server admin"):
        await _redeem_promo_code_impl(_runtime(db_session_factory), auth, "WELCOME-2026")
    assert await tenant_ledger.get_balance(db_session, tenant_id=auth.tenant_id) == 0
