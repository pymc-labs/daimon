"""A shared secret is encrypted, revealed once to its recipient only, then cleared."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from daimon.core._models import SecretShare
from daimon.core.secret_shares import MAX_LIVE_PER_SENDER
from daimon.core.stores import secret_shares as store
from daimon.testing.factories import make_tenant
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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
    session: AsyncSession, share_id: uuid.UUID, recipient: str = "2", now: datetime = NOW
) -> store.Reveal:
    return await store.reveal_share(
        session, share_id=share_id, platform="discord", recipient=recipient, now=now
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

    other = await _reveal(db_session, share.id, recipient="3")
    assert (other.value, other.refusal) == (None, "not_recipient")
    first = await _reveal(db_session, share.id)
    assert (first.value, first.refusal) == ("s3cret", None)
    second = await _reveal(db_session, share.id)
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


async def test_a_lapsed_share_is_cleared_and_frees_the_senders_slot(
    db_session: AsyncSession,
) -> None:
    db_session.info["crypto_keys"] = (Fernet.generate_key().decode(),)
    tenant = await make_tenant(db_session)
    shares = [await _share(db_session, tenant.id) for _ in range(MAX_LIVE_PER_SENDER)]
    assert await _share(db_session, tenant.id) is None, "the most live shares per sender"
    later = NOW + timedelta(days=1)
    assert shares[0] is not None
    lapsed = await _reveal(db_session, shares[0].id, now=later)
    assert (lapsed.value, lapsed.refusal) == (None, "expired")
    assert await _share(db_session, tenant.id, now=later) is not None
    contents = (await db_session.scalars(select(SecretShare.content))).all()
    assert sum(c is not None for c in contents) == 1, "every lapsed value is cleared"


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
