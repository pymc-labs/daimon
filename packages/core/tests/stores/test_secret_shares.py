"""A shared secret is encrypted, revealed once to its recipient only, then cleared."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from cryptography.fernet import Fernet
from daimon.core._models import SecretShare
from daimon.core.secret_shares import MAX_LIVE_PER_SENDER
from daimon.core.stores import secret_shares as store
from daimon.testing.factories import make_tenant
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

NOW = datetime(2026, 11, 21, 12, tzinfo=UTC)


async def _share(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    sender: str = "1",
    recipient: str = "2",
    now: datetime = NOW,
) -> store.ShareView | None:
    return await store.create_share(
        session,
        tenant_id=tenant_id,
        platform="discord",
        sender=sender,
        recipient=recipient,
        label="Prod DB",
        value="s3cret",
        now=now,
    )


async def _reveal(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    share: store.ShareView,
    recipient: str = "2",
    now: datetime = NOW,
) -> store.Reveal:
    return await store.reveal_share(
        session,
        tenant_id=tenant_id,
        share_id=share.id,
        platform="discord",
        recipient=recipient,
        now=now,
    )


async def test_the_value_is_ciphertext_and_shown_once_to_the_recipient_only(
    db_session: AsyncSession,
) -> None:
    key = Fernet.generate_key()
    db_session.info["crypto_keys"] = (key.decode(),)
    tenant = await make_tenant(db_session)
    share = await _share(db_session, tenant.id)
    assert share is not None
    stored = await db_session.scalar(select(SecretShare.content).where(SecretShare.id == share.id))
    assert stored is not None and b"s3cret" not in stored
    assert Fernet(key).decrypt(stored) == b"s3cret"

    other = await _reveal(db_session, tenant.id, share, recipient="3")
    assert (other.value, other.refusal) == (None, "not_recipient")
    first = await _reveal(db_session, tenant.id, share)
    assert (first.value, first.refusal) == ("s3cret", None)
    second = await _reveal(db_session, tenant.id, share)
    assert (second.value, second.refusal) == (None, "revealed")
    row = await db_session.get(SecretShare, share.id)
    assert row is not None and row.content is None and row.revealed_at == NOW, "kept as the record"


async def test_no_deployment_key_stores_nothing(db_session: AsyncSession) -> None:
    db_session.info["crypto_keys"] = ()
    db_session.info["crypto_allow_plaintext"] = True
    tenant = await make_tenant(db_session)
    with pytest.raises(store.SecretShareEncryptionRequiredError):
        await _share(db_session, tenant.id)
    assert await db_session.scalar(select(SecretShare.id)) is None


async def test_the_sweep_clears_lapsed_values_with_no_new_share(db_session: AsyncSession) -> None:
    db_session.info["crypto_keys"] = (Fernet.generate_key().decode(),)
    tenant = await make_tenant(db_session)
    shares = [await _share(db_session, tenant.id) for _ in range(MAX_LIVE_PER_SENDER)]
    assert await _share(db_session, tenant.id) is None, "the most live shares per sender"
    later = NOW + timedelta(days=1)
    assert await store.clear_lapsed_shares(db_session, now=NOW) == 0
    assert await store.clear_lapsed_shares(db_session, now=later) == MAX_LIVE_PER_SENDER
    assert all(c is None for c in (await db_session.scalars(select(SecretShare.content))).all())
    assert shares[0] is not None
    lapsed = await _reveal(db_session, tenant.id, shares[0], now=later)
    assert (lapsed.value, lapsed.refusal) == (None, "expired")
    assert await _share(db_session, tenant.id, now=later) is not None, "the slot is free"


async def test_a_reveal_needs_the_shares_own_tenant(db_session: AsyncSession) -> None:
    db_session.info["crypto_keys"] = (Fernet.generate_key().decode(),)
    tenant, other = await make_tenant(db_session), await make_tenant(db_session)
    share = await _share(db_session, tenant.id)
    assert share is not None
    foreign = await _reveal(db_session, other.id, share)
    assert (foreign.value, foreign.refusal) == (None, "gone")


async def test_a_held_share_reveals_only_after_its_sender_confirms(
    db_session: AsyncSession,
) -> None:
    db_session.info["crypto_keys"] = (Fernet.generate_key().decode(),)
    tenant = await make_tenant(db_session)
    share = await store.create_share(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        sender="1",
        recipient="2",
        label="Prod DB",
        value="s3cret",
        now=NOW,
        awaiting_sender=True,
    )
    assert share is not None
    assert (await _reveal(db_session, tenant.id, share)).refusal == "gone"
    kw: dict[str, Any] = {
        "tenant_id": tenant.id,
        "share_id": share.id,
        "platform": "discord",
        "now": NOW,
    }
    assert (await store.confirm_share(db_session, sender="2", **kw)).refusal == "not_sender"
    assert (await store.confirm_share(db_session, sender="1", **kw)).refusal is None
    assert (await store.confirm_share(db_session, sender="1", **kw)).refusal == "gone"
    assert (await _reveal(db_session, tenant.id, share)).value == "s3cret"


async def test_two_clicks_at_once_reveal_the_value_once(
    db_committing_engine: AsyncEngine, db_clean: None
) -> None:
    sessions = async_sessionmaker(
        db_committing_engine,
        expire_on_commit=False,
        info={"crypto_keys": (Fernet.generate_key().decode(),)},
    )
    async with sessions.begin() as session:
        tenant = await make_tenant(session)
        share = await _share(session, tenant.id)
    assert share is not None

    async def click() -> store.Reveal:
        async with sessions.begin() as session:
            return await _reveal(session, tenant.id, share)

    results = await asyncio.gather(click(), click())
    assert sorted(str(r.value) for r in results) == ["None", "s3cret"]


async def test_shares_at_once_stop_at_the_cap(
    db_committing_engine: AsyncEngine, db_clean: None
) -> None:
    sessions = async_sessionmaker(
        db_committing_engine,
        expire_on_commit=False,
        info={"crypto_keys": (Fernet.generate_key().decode(),)},
    )
    async with sessions.begin() as session:
        tenant = await make_tenant(session)
        for _ in range(MAX_LIVE_PER_SENDER - 1):
            await _share(session, tenant.id)

    async def share() -> store.ShareView | None:
        async with sessions.begin() as session:
            return await _share(session, tenant.id)

    results = await asyncio.gather(share(), share())
    assert sum(r is not None for r in results) == 1


async def test_erasure_takes_shares_the_person_sent_or_received(db_session: AsyncSession) -> None:
    db_session.info["crypto_keys"] = (Fernet.generate_key().decode(),)
    tenant = await make_tenant(db_session)
    await _share(db_session, tenant.id, sender="1", recipient="2")
    await _share(db_session, tenant.id, sender="2", recipient="3")
    await _share(db_session, tenant.id, sender="3", recipient="4")
    kw = {"platform": "discord", "platform_user_id": "2"}
    assert await store.count_shares_for_platform_user(db_session, tenant_id=tenant.id, **kw) == 2
    assert await store.delete_shares_for_platform_user(db_session, tenant_id=tenant.id, **kw) == 2
    assert await store.count_shares_for_platform_user(db_session, tenant_id=tenant.id, **kw) == 0
