"""Wiring guard: Slack's built context names stored keys, never their values.

`daimon.core.turn_keys` defines the `<keys>` element contract and Slack's
`context.py` now wires `render_keys_element` into every builder
(`build_context_xml`, `build_delta_xml`) via a `key_names` parameter the
adapter fills from `list_mounted_key_names` after `bind_session`. This file
proves that on Slack, for a real built context:

  - passing key names renders a `<keys>` element naming exactly those keys,
  - no stored *value* ever appears, named or not,
  - passing no names renders no `<keys>` element at all,
  - a stale mounted `.env` (hash mismatch) also renders no `<keys>` element,
    because `list_mounted_key_names` itself returns no names in that case,
  - `context.py` does reference `daimon.core.turn_keys` — the tripwire for
    this wiring existing at all.

This file is the inverse of the guard that existed before this wiring
landed: that version asserted key *names* never appeared either. If you are
re-disabling this wiring, invert these assertions back rather than deleting
them.
"""

from __future__ import annotations

import re
from pathlib import Path

from aioresponses import aioresponses as AioResponsesMock
from daimon.adapters.slack.context import build_context_xml, build_delta_xml
from daimon.core.stores.agent_files import put_agent_file
from daimon.core.turn_keys import list_mounted_key_names
from daimon.testing.factories import make_tenant
from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy.ext.asyncio import AsyncSession

_REPLIES_PATTERN = re.compile(r"https://slack\.com/api/conversations\.replies.*")

_STORED_KEY_ONE = "OPENAI_API_KEY"
_STORED_VALUE_ONE = "sk-sentinel-slack-guard-7d2f0e"
_STORED_KEY_TWO = "TOGGL_TOKEN"
_STORED_VALUE_TWO = "toggl-sentinel-slack-guard-9a1c"
_STORED_NAMES = (_STORED_KEY_ONE, _STORED_KEY_TWO)


def _make_client() -> AsyncWebClient:
    return AsyncWebClient(token="xoxb-test")


async def _seed_stored_keys(session: AsyncSession) -> None:
    tenant = await make_tenant(session)
    for key, value in (
        (_STORED_KEY_ONE, _STORED_VALUE_ONE),
        (_STORED_KEY_TWO, _STORED_VALUE_TWO),
    ):
        await put_agent_file(
            session,
            tenant_id=tenant.id,
            agent_id=tenant.id,
            key=key,
            content=value,
            set_by_account_id=None,
        )


def _assert_keys_named_never_valued(xml: str, names: tuple[str, ...], *, builder_name: str) -> None:
    assert "<keys" in xml, f"{builder_name} must emit a <keys> element when names are passed"
    for name in names:
        assert name in xml, f"{builder_name} must name every passed key — missing {name!r}"
    for value in (_STORED_VALUE_ONE, _STORED_VALUE_TWO):
        assert value not in xml, (
            f"{builder_name} must never leak a stored key's value — names ride, values never do"
        )


def _assert_no_keys_element(xml: str, *, builder_name: str) -> None:
    assert "<keys" not in xml, (
        f"{builder_name} must not emit a <keys> element when no names are passed"
    )
    for value in (_STORED_VALUE_ONE, _STORED_VALUE_TWO):
        assert value not in xml, (
            f"{builder_name} must never leak a stored key's value, names or no names"
        )


async def test_build_context_xml_names_keys_never_values(db_session: AsyncSession) -> None:
    await _seed_stored_keys(db_session)
    with AioResponsesMock() as mock:
        mock.get(_REPLIES_PATTERN, payload={"ok": True, "messages": [], "has_more": False})
        client = _make_client()
        xml = await build_context_xml(
            client,
            channel="C1",
            thread_ts="100.0",
            user_query="hi",
            key_names=_STORED_NAMES,
        )

    _assert_keys_named_never_valued(xml, _STORED_NAMES, builder_name="build_context_xml")


async def test_build_delta_xml_names_keys_never_values(db_session: AsyncSession) -> None:
    await _seed_stored_keys(db_session)
    with AioResponsesMock() as mock:
        mock.get(_REPLIES_PATTERN, payload={"ok": True, "messages": [], "has_more": False})
        client = _make_client()
        xml = await build_delta_xml(
            client,
            channel="C1",
            thread_ts="100.0",
            watermark_ts="99.0",
            user_query="hi",
            key_names=_STORED_NAMES,
        )

    _assert_keys_named_never_valued(xml, _STORED_NAMES, builder_name="build_delta_xml")


async def test_build_context_xml_omits_keys_when_no_names_are_passed(
    db_session: AsyncSession,
) -> None:
    await _seed_stored_keys(db_session)
    with AioResponsesMock() as mock:
        mock.get(_REPLIES_PATTERN, payload={"ok": True, "messages": [], "has_more": False})
        client = _make_client()
        xml = await build_context_xml(client, channel="C1", thread_ts="100.0", user_query="hi")

    _assert_no_keys_element(xml, builder_name="build_context_xml")


async def test_build_context_xml_omits_keys_when_the_mounted_env_is_stale(
    db_session: AsyncSession,
) -> None:
    """`list_mounted_key_names` returns no names for a hash that doesn't match

    today's `agent_files` rows, so a builder fed its result renders nothing —
    a session running an older `.env` must never be told about keys it can't
    actually read.
    """
    tenant = await make_tenant(db_session)
    await put_agent_file(
        db_session,
        tenant_id=tenant.id,
        agent_id=tenant.id,
        key=_STORED_KEY_ONE,
        content=_STORED_VALUE_ONE,
        set_by_account_id=None,
    )

    key_names = await list_mounted_key_names(
        db_session,
        tenant_id=tenant.id,
        agent_id=tenant.id,
        env_sha256="0" * 64,  # deliberately mismatched — never a real hash
    )
    assert key_names == (), "a mismatched hash must yield no names to pass to the builder"

    with AioResponsesMock() as mock:
        mock.get(_REPLIES_PATTERN, payload={"ok": True, "messages": [], "has_more": False})
        client = _make_client()
        xml = await build_context_xml(
            client, channel="C1", thread_ts="100.0", user_query="hi", key_names=key_names
        )

    _assert_no_keys_element(xml, builder_name="build_context_xml")


def test_context_module_references_turn_keys() -> None:
    source = Path("packages/adapters/slack/daimon/adapters/slack/context.py").read_text(
        encoding="utf-8"
    )

    assert "turn_keys" in source, (
        "slack/context.py must reference daimon.core.turn_keys — this is the wiring's "
        "tripwire. If you are reading this because it failed, you unwired render_keys_element: "
        "do so deliberately, then invert the assertions in this file back to name-absence."
    )
