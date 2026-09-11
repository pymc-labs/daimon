"""Tests for the names-only key projection and its `<keys>` element renderer."""

from __future__ import annotations

from daimon.core.stores.agent_files import put_agent_file
from daimon.core.turn_keys import list_turn_key_names, render_keys_element
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession


async def test_list_turn_key_names_returns_ascending_by_key(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    agent_id = tenant.id  # any stable uuid works; the store keys on (tenant_id, agent_id)

    for key in ("ZED", "ALPHA", "mid"):
        await put_agent_file(
            db_session, tenant_id=tenant.id, agent_id=agent_id, key=key, content="secret-value"
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
        db_session, tenant_id=tenant.id, agent_id=tenant.id, key="TOGGL_TOKEN", content="v"
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
