"""Persistent state for the hub login proxies.

``OAuthProxy`` keeps dynamic client registrations, in-flight transactions,
authorization codes, and upstream tokens in an ``AsyncKeyValue``. Its default
is an on-disk store under the process home, which neither survives a redeploy
nor is shared between replicas. Both platforms therefore share one Postgres
table, each behind its own collection prefix, so a Discord client id can never
be looked up by the Slack proxy, and every value is encrypted with the
deployment's credential keys because upstream access tokens live here.

The store speaks asyncpg directly and owns its own pool; it cannot reuse the
SQLAlchemy engine. One store serves both platforms so the deployment holds one
pool, and the caller closes it at shutdown. ``auto_create`` is off because the
table belongs to Alembic (``0013_hub_oauth_kv``).
"""

from __future__ import annotations

from cryptography.fernet import MultiFernet
from daimon.core.stores.domain import Platform
from key_value.aio.protocols import AsyncKeyValue
from key_value.aio.stores.postgresql import PostgreSQLStore
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper
from key_value.aio.wrappers.prefix_collections import PrefixCollectionsWrapper

HUB_KV_TABLE = "hub_oauth_kv"


def asyncpg_dsn(sqlalchemy_url: str) -> str:
    """Drop the ``+asyncpg`` driver marker; asyncpg wants a plain postgresql:// DSN."""
    return sqlalchemy_url.replace("postgresql+asyncpg://", "postgresql://", 1)


def build_hub_kv_base(
    *, database_url: str, fernet: MultiFernet
) -> tuple[PostgreSQLStore, AsyncKeyValue]:
    """Return the pool-owning store and the encrypted view the platforms share.

    The store comes back alongside its wrapper because only it can close the
    asyncpg pool; the wrappers do not expose it.
    """
    store = PostgreSQLStore(url=database_url, table_name=HUB_KV_TABLE, auto_create=False)
    return store, FernetEncryptionWrapper(store, fernet=fernet, raise_on_decryption_error=False)


def hub_kv_for(base: AsyncKeyValue, *, platform: Platform) -> AsyncKeyValue:
    """Namespace ``base`` so one platform's collections cannot be read as another's."""
    return PrefixCollectionsWrapper(base, prefix=platform)
