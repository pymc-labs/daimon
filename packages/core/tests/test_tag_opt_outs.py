"""Never-tag opt-outs: storage by account, lookup by platform user, purge and privacy summary."""

from __future__ import annotations

import uuid

import pytest
from daimon.core.outbound.mentions import Platform
from daimon.core.outbound.opt_out import tag_opt_outs, untag_opted_out
from daimon.core.privacy import collect_purge_preview, held_lines
from daimon.core.purge import purge_account
from daimon.core.stores import platform_names as names_store
from daimon.core.stores import tag_opt_outs as store
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def test_opted_out_user_ids_when_person_has_two_identities_then_both_match(
    db_session: AsyncSession,
) -> None:
    """The choice belongs to the account, so every linked identity of the person is covered."""
    tenant = await make_tenant(db_session, platform="slack", workspace_id="T_OPT_1")
    account = await make_account(db_session, tenant=tenant)
    await make_platform_principal(
        db_session, platform="slack", external_id="U_A1", tenant=tenant, account=account
    )
    await make_platform_principal(
        db_session, platform="slack", external_id="U_A2", tenant=tenant, account=account
    )
    other = await make_account(db_session, tenant=tenant)
    await make_platform_principal(
        db_session, platform="slack", external_id="U_B", tenant=tenant, account=other
    )
    await store.set_opt_out(db_session, account_id=account.id, tenant_id=tenant.id)
    await store.set_opt_out(db_session, account_id=account.id, tenant_id=tenant.id)

    found = await store.opted_out_user_ids(
        db_session,
        tenant_id=tenant.id,
        platform="slack",
        user_ids=["U_A1", "U_A2", "U_B", "U_NONE"],
    )

    assert found == frozenset({"U_A1", "U_A2"}), "both identities of the person, nobody else"
    assert await store.count_for_account(db_session, account_id=account.id) == 1, (
        "set is idempotent"
    )


async def test_clear_opt_out_when_set_then_person_can_be_tagged_again(
    db_session: AsyncSession,
) -> None:
    """Unsetting removes the row and reports whether there was one."""
    tenant = await make_tenant(db_session, workspace_id="G_OPT_2")
    account = await make_account(db_session, tenant=tenant)
    await make_platform_principal(
        db_session, platform="discord", external_id="42", tenant=tenant, account=account
    )
    await store.set_opt_out(db_session, account_id=account.id, tenant_id=tenant.id)

    assert await store.clear_opt_out(db_session, account_id=account.id) is True, "was set"
    assert await store.clear_opt_out(db_session, account_id=account.id) is False, "already clear"
    assert not await store.is_opted_out(db_session, account_id=account.id), "tags allowed again"


@pytest.mark.parametrize(
    ("platform", "user_id", "text", "expected"),
    [
        ("discord", "42", "hi <@42> and <@!43>", "hi @Ann and <@!43>"),
        ("slack", "U42", "hi <@U42> and <@U43>", "hi @Ann and <@U43>"),
        ("teams", "T42", "hi <at>Ann</at>", "hi <at>Ann</at>"),
    ],
)
async def test_untag_opted_out_when_person_opted_out_then_plain_name_on_every_platform(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    platform: Platform,
    user_id: str,
    text: str,
    expected: str,
) -> None:
    """Adapter-authored text loses the ping of an opted-out person; Teams text carries none."""
    tenant = await make_tenant(db_session, platform=platform, workspace_id=f"W_{platform}")
    account = await make_account(db_session, tenant=tenant)
    await make_platform_principal(
        db_session, platform=platform, external_id=user_id, tenant=tenant, account=account
    )
    await names_store.upsert_user_names(
        db_session,
        tenant_id=tenant.id,
        platform=platform,
        names={user_id: names_store.KnownName(display_name="Ann")},
    )
    await store.set_opt_out(db_session, account_id=account.id, tenant_id=tenant.id)
    await db_session.commit()

    skip = await tag_opt_outs(
        db_session_factory, tenant_id=tenant.id, platform=platform, user_ids=[user_id]
    )
    out = await untag_opted_out(
        db_session_factory,
        tenant_id=tenant.id,
        platform=platform,
        text=text,
    )

    assert skip == frozenset({user_id}), f"{platform}: the person is found by platform id"
    assert out == expected, f"{platform}: mention becomes a plain name"


async def test_tag_opt_outs_when_no_sessionmaker_then_nobody_is_opted_out() -> None:
    """Without a database the lookup is empty rather than failing a post."""
    assert (
        await tag_opt_outs(None, tenant_id=uuid.uuid4(), platform="discord", user_ids=["1"])
        == frozenset()
    )


async def test_purge_account_when_opted_out_then_setting_is_erased_and_counted(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Erasure removes the never-tag choice; the privacy summary lists it beforehand."""
    tenant = await make_tenant(db_session, workspace_id="G_OPT_3")
    account = await make_account(db_session, tenant=tenant)
    await make_platform_principal(
        db_session, platform="discord", external_id="77", tenant=tenant, account=account
    )
    await store.set_opt_out(db_session, account_id=account.id, tenant_id=tenant.id)
    await db_session.commit()

    preview = await collect_purge_preview(sm=db_session_factory, account_id=account.id)
    report = await purge_account(sm=db_session_factory, account_id=account.id)

    assert preview.tag_opt_outs.count == 1, "the summary counts the setting"
    assert "1 never-tag setting" in held_lines(preview), "the privacy panel lists it"
    assert report.db.tag_opt_outs == 1, "the purge reports it"
    assert not await store.is_opted_out(db_session, account_id=account.id), "the row is gone"
