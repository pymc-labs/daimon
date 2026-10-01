"""Tests for the names-only key projection and its `<keys>` element renderer."""

from __future__ import annotations

import uuid

from daimon.core.credential_env import assemble_env_bytes
from daimon.core.session_snapshot import hash_env_bytes
from daimon.core.stores.agent_files import list_agent_files, put_agent_file
from daimon.core.turn_keys import (
    list_mounted_key_names,
    list_turn_key_names,
    render_keys_element,
)
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession


async def test_list_turn_key_names_returns_ascending_by_key(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    agent_id = tenant.id  # any stable uuid works; the store keys on (tenant_id, agent_id)

    for key in ("ZED", "ALPHA", "mid"):
        await put_agent_file(
            db_session,
            tenant_id=tenant.id,
            agent_id=agent_id,
            key=key,
            content="secret-value",
            set_by_account_id=None,
        )

    names = await list_turn_key_names(db_session, tenant_id=tenant.id, agent_id=agent_id)

    assert names == ("ALPHA", "ZED", "mid"), (
        "names must come back in the store's ORDER BY key order, ascending by codepoint"
    )


async def test_list_turn_key_names_empty_for_agent_with_no_rows(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)

    names = await list_turn_key_names(db_session, tenant_id=tenant.id, agent_id=tenant.id)

    assert names == (), "an agent with no stored keys must return an empty tuple, not None"


async def test_list_turn_key_names_returns_a_plain_tuple_of_str(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    await put_agent_file(
        db_session,
        tenant_id=tenant.id,
        agent_id=tenant.id,
        key="TOGGL_TOKEN",
        content="v",
        set_by_account_id=None,
    )

    names = await list_turn_key_names(db_session, tenant_id=tenant.id, agent_id=tenant.id)

    assert isinstance(names, tuple), "the return type must be a tuple, not a list or row sequence"
    assert all(isinstance(n, str) for n in names), (
        "every element must be a plain str, with no field through which a value is reachable"
    )


async def test_list_turn_key_names_and_render_never_leak_a_stored_value(
    db_session: AsyncSession,
) -> None:
    """The whole point of this module: a stored value must never be reachable
    from the projection's repr or from the rendered element."""
    tenant = await make_tenant(db_session)
    sentinel_value = "sk-sentinel-do-not-leak-9f3a1c"
    await put_agent_file(
        db_session,
        tenant_id=tenant.id,
        agent_id=tenant.id,
        key="OPENAI_API_KEY",
        content=sentinel_value,
        set_by_account_id=None,
    )

    names = await list_turn_key_names(db_session, tenant_id=tenant.id, agent_id=tenant.id)
    rendered = render_keys_element(names)

    assert sentinel_value not in repr(names), (
        "a stored value must not be reachable from the projection's repr"
    )
    assert sentinel_value not in rendered, (
        "a stored value must not be reachable from the rendered <keys> element"
    )
    assert "OPENAI_API_KEY" in rendered, "the key name itself is the whole point of the element"


def test_render_keys_element_empty_sequence_is_empty_string() -> None:
    assert render_keys_element(()) == "", (
        "an agent with no keys must contribute no element at all, not an empty <keys/>"
    )


def test_render_keys_element_non_empty_renders_a_single_keys_element() -> None:
    rendered = render_keys_element(("ALPHA", "ZED"))

    assert rendered.startswith("<keys>") and rendered.endswith("</keys>"), (
        "a non-empty sequence must render as a single <keys>...</keys> element"
    )
    assert rendered.index("ALPHA") < rendered.index("ZED"), (
        "names must render in the order given, not re-sorted by the renderer"
    )
    assert "\n" not in rendered, "the element must be single-line"


def test_render_keys_element_escapes_xml_metacharacters() -> None:
    rendered = render_keys_element(('WEIRD"NAME<>&',))

    assert 'WEIRD"NAME<>&' not in rendered, (
        "a name with XML metacharacters must not appear raw in the rendered element"
    )
    assert rendered.count("<key ") == 1, (
        "an escaped metacharacter must not be interpreted as closing the element early"
    )


async def _mounted_hash(session: AsyncSession, *, tenant_id: uuid.UUID) -> str:
    """The hash a session freezes when it mounts this agent's current keys."""
    rows = await list_agent_files(session, tenant_id=tenant_id, agent_id=tenant_id)
    return hash_env_bytes(assemble_env_bytes(rows))


async def test_list_mounted_key_names_returns_names_when_hash_matches(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    for key in ("ALPHA", "ZED"):
        await put_agent_file(
            db_session,
            tenant_id=tenant.id,
            agent_id=tenant.id,
            key=key,
            content="secret-value",
            set_by_account_id=None,
        )
    env_sha256 = await _mounted_hash(db_session, tenant_id=tenant.id)

    names = await list_mounted_key_names(
        db_session, tenant_id=tenant.id, agent_id=tenant.id, env_sha256=env_sha256
    )

    assert names == ("ALPHA", "ZED"), (
        "a session whose mounted .env is these exact rows may be told their names"
    )


async def test_list_mounted_key_names_returns_empty_when_hash_is_stale(
    db_session: AsyncSession,
) -> None:
    """A key added after the session mounted its .env is not readable by that session."""
    tenant = await make_tenant(db_session)
    await put_agent_file(
        db_session,
        tenant_id=tenant.id,
        agent_id=tenant.id,
        key="ALPHA",
        content="secret-value",
        set_by_account_id=None,
    )
    frozen = await _mounted_hash(db_session, tenant_id=tenant.id)
    await put_agent_file(
        db_session,
        tenant_id=tenant.id,
        agent_id=tenant.id,
        key="ZED",
        content="added-after-the-mount",
        set_by_account_id=None,
    )

    names = await list_mounted_key_names(
        db_session, tenant_id=tenant.id, agent_id=tenant.id, env_sha256=frozen
    )

    assert names == (), (
        "naming today's keys to a session running an older .env would promise a key it cannot read"
    )


async def test_list_mounted_key_names_returns_empty_without_a_hash(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    await put_agent_file(
        db_session,
        tenant_id=tenant.id,
        agent_id=tenant.id,
        key="ALPHA",
        content="secret-value",
        set_by_account_id=None,
    )

    names = await list_mounted_key_names(
        db_session, tenant_id=tenant.id, agent_id=tenant.id, env_sha256=None
    )

    assert names == (), "a session that froze no hash mounted no .env, so it has nothing to name"


async def test_list_mounted_key_names_never_leaks_a_stored_value(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    sentinel_value = "sk-sentinel-do-not-leak-9f3a1c"
    await put_agent_file(
        db_session,
        tenant_id=tenant.id,
        agent_id=tenant.id,
        key="OPENAI_API_KEY",
        content=sentinel_value,
        set_by_account_id=None,
    )
    env_sha256 = await _mounted_hash(db_session, tenant_id=tenant.id)

    names = await list_mounted_key_names(
        db_session, tenant_id=tenant.id, agent_id=tenant.id, env_sha256=env_sha256
    )

    assert names == ("OPENAI_API_KEY",), "the matching session is told the key name"
    assert sentinel_value not in repr(names), (
        "a stored value must not be reachable from the projection's repr"
    )
    assert sentinel_value not in render_keys_element(names), (
        "a stored value must not be reachable from the rendered <keys> element"
    )


async def test_list_mounted_key_names_leaves_out_rows_the_mount_skipped(
    db_session: AsyncSession,
) -> None:
    """A legacy hard-denied row is not in the mounted .env, so it is never named."""
    from sqlalchemy import text

    tenant = await make_tenant(db_session)
    await put_agent_file(
        db_session,
        tenant_id=tenant.id,
        agent_id=tenant.id,
        key="ALPHA_TOKEN",
        content="secret-value",
        set_by_account_id=None,
    )
    # Written the way pre-policy code did, bypassing the store's name check.
    await db_session.execute(
        text(
            "INSERT INTO agent_files (tenant_id, agent_id, key, content)"
            " VALUES (:t, :a, 'TAR_OPTIONS', 'legacy')"
        ),
        {"t": tenant.id, "a": tenant.id},
    )
    env_sha256 = await _mounted_hash(db_session, tenant_id=tenant.id)

    names = await list_mounted_key_names(
        db_session, tenant_id=tenant.id, agent_id=tenant.id, env_sha256=env_sha256
    )

    assert names == ("ALPHA_TOKEN",), "a key the mount left out must not be promised"
