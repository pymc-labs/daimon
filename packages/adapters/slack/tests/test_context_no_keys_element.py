"""Disabled-contract guard: Slack's built context carries no <keys> element.

`daimon.core.turn_keys` defines the `<keys>` element contract but nothing
wires it into any adapter yet (the reused-session refresh mechanism it would
depend on does not exist). This file proves that on Slack, for a real built
context, against an agent with stored keys:

  - no `<keys` element appears,
  - no stored value appears,
  - no stored key *name* appears either, since nothing injects them,
  - `context.py` carries no reference to the `turn_keys` module.

When a later phase wires `render_keys_element` into a builder deliberately,
every assertion here about key *names* being absent must be inverted (the
value-absence assertions must stay, unchanged). Finding this test red is the
signal that the wiring happened; go update the assertions here to match the
new, intended behaviour instead of deleting them.
"""

from __future__ import annotations

import re
from pathlib import Path

from aioresponses import aioresponses as AioResponsesMock
from daimon.adapters.slack.context import build_context_xml, build_delta_xml
from daimon.core.stores.agent_files import put_agent_file
from daimon.testing.factories import make_tenant
from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy.ext.asyncio import AsyncSession

_REPLIES_PATTERN = re.compile(r"https://slack\.com/api/conversations\.replies.*")

_STORED_KEY_ONE = "OPENAI_API_KEY"
_STORED_VALUE_ONE = "sk-sentinel-slack-guard-7d2f0e"
_STORED_KEY_TWO = "TOGGL_TOKEN"
_STORED_VALUE_TWO = "toggl-sentinel-slack-guard-9a1c"


def _make_client() -> AsyncWebClient:
    return AsyncWebClient(token="xoxb-test")


async def _seed_stored_keys(session: AsyncSession) -> None:
    tenant = await make_tenant(session)
    for key, value in (
        (_STORED_KEY_ONE, _STORED_VALUE_ONE),
        (_STORED_KEY_TWO, _STORED_VALUE_TWO),
    ):
        await put_agent_file(
            session, tenant_id=tenant.id, agent_id=tenant.id, key=key, content=value
        )


def _assert_no_keys_element(xml: str, *, builder_name: str) -> None:
    assert "<keys" not in xml, (
        f"{builder_name} must not emit a <keys> element — nothing wires "
        "render_keys_element into any adapter yet"
    )
    for value in (_STORED_VALUE_ONE, _STORED_VALUE_TWO):
        assert value not in xml, (
            f"{builder_name} must never leak a stored key's value, wiring or no wiring"
        )
    for name in (_STORED_KEY_ONE, _STORED_KEY_TWO):
        assert name not in xml, (
            f"{builder_name} must not mention a stored key's name today — nothing injects "
            "key names into a turn yet. This expectation inverts once a later phase wires "
            "render_keys_element into this builder; when it does, update this assertion to "
            f"require the name instead of forbidding it."
        )


async def test_build_context_xml_carries_no_keys_element(db_session: AsyncSession) -> None:
    await _seed_stored_keys(db_session)
    with AioResponsesMock() as mock:
        mock.get(_REPLIES_PATTERN, payload={"ok": True, "messages": [], "has_more": False})
        client = _make_client()
        xml = await build_context_xml(client, channel="C1", thread_ts="100.0", user_query="hi")

    _assert_no_keys_element(xml, builder_name="build_context_xml")


async def test_build_delta_xml_carries_no_keys_element(db_session: AsyncSession) -> None:
    await _seed_stored_keys(db_session)
    with AioResponsesMock() as mock:
        mock.get(_REPLIES_PATTERN, payload={"ok": True, "messages": [], "has_more": False})
        client = _make_client()
        xml = await build_delta_xml(
            client, channel="C1", thread_ts="100.0", watermark_ts="99.0", user_query="hi"
        )

    _assert_no_keys_element(xml, builder_name="build_delta_xml")


def test_context_module_does_not_reference_turn_keys() -> None:
    source = Path("packages/adapters/slack/daimon/adapters/slack/context.py").read_text(
        encoding="utf-8"
    )

    assert "turn_keys" not in source, (
        "slack/context.py must not reference daimon.core.turn_keys — the <keys> element "
        "contract is intentionally unwired. If you are reading this because it failed, you "
        "wired it: do so deliberately, then invert the name-absence assertions in "
        "test_context_no_keys_element.py to match the new, intended behaviour."
    )
