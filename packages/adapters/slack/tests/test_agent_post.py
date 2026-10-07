"""Slack message headers and narrow pre-reinstall fallback."""

from __future__ import annotations

from typing import Any

import pytest
import yarl
from daimon.adapters.slack.agent_post import _NO_CUSTOMIZE_SCOPE, post_as_agent
from daimon.core.agent_identity import AgentIdentity
from daimon.core.slack_customize_scope import missing_customize_scope
from slack_sdk.errors import SlackApiError

from .conftest import CHAT_OK_PAYLOAD

_POST_URL = yarl.URL("https://slack.com/api/chat.postMessage")


@pytest.mark.asyncio
async def test_agent_header_and_builtin_app_header(fake_slack_web_client: Any) -> None:
    fake = fake_slack_web_client
    identity = AgentIdentity("Ada", "https://example.test/ada.png", False)
    await post_as_agent(fake.client, identity, channel="C_TEST", text="answer")
    await post_as_agent(
        fake.client, AgentIdentity("Daimon", None, True), channel="C_TEST", text="setup"
    )
    requests = fake.mock.requests[("POST", _POST_URL)]
    assert requests[0].kwargs["json"]["username"] == "Ada"
    assert requests[0].kwargs["json"]["icon_url"] == identity.avatar_url
    assert "username" not in requests[1].kwargs["json"]


@pytest.mark.asyncio
async def test_only_customize_missing_scope_retries_and_remembers_token(
    fake_slack_web_client: Any,
) -> None:
    fake = fake_slack_web_client
    fake.mock.clear()
    _NO_CUSTOMIZE_SCOPE.clear()
    fake.mock.post(
        str(_POST_URL),
        payload={"ok": False, "error": "missing_scope", "needed": "chat:write.customize"},
    )
    fake.mock.post(str(_POST_URL), payload=CHAT_OK_PAYLOAD, repeat=True)
    identity = AgentIdentity("Ada", None, False)
    await post_as_agent(fake.client, identity, channel="C_TEST", text="first")
    await post_as_agent(fake.client, identity, channel="C_TEST", text="second")
    requests = fake.mock.requests[("POST", _POST_URL)]
    assert requests[0].kwargs["json"]["username"] == "Ada"
    assert all("username" not in request.kwargs["json"] for request in requests[1:])


@pytest.mark.asyncio
async def test_other_missing_scope_is_not_retried(fake_slack_web_client: Any) -> None:
    fake = fake_slack_web_client
    fake.mock.clear()
    _NO_CUSTOMIZE_SCOPE.clear()
    fake.mock.post(
        str(_POST_URL), payload={"ok": False, "error": "missing_scope", "needed": "chat:write"}
    )
    with pytest.raises(SlackApiError):
        await post_as_agent(
            fake.client, AgentIdentity("Ada", None, False), channel="C_TEST", text="x"
        )
    assert len(fake.mock.requests[("POST", _POST_URL)]) == 1


def test_missing_scope_cache_expires_and_can_be_cleared(monkeypatch: pytest.MonkeyPatch) -> None:
    from daimon.core import slack_customize_scope as cache

    cache._NO_CUSTOMIZE_SCOPE.clear()  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(cache.time, "monotonic", lambda: 100.0)
    cache.remember_missing_customize_scope("xoxb-test")
    assert missing_customize_scope("xoxb-test")
    monkeypatch.setattr(cache.time, "monotonic", lambda: 1000.0)
    assert not missing_customize_scope("xoxb-test")
    cache.remember_missing_customize_scope("xoxb-test")
    cache.clear_missing_customize_scope("xoxb-test")
    assert not missing_customize_scope("xoxb-test")
