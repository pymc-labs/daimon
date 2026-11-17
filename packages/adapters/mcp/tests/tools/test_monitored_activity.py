"""Monitored activity tools: the admin gate, private-only transcripts, and the audit row."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from daimon.adapters.mcp.tools import monitored_activity as tool
from daimon.core import monitoring_view as view
from daimon.core.stores import web_people as people_store
from daimon.core.stores.domain import MonitoredConversationRow, MonitoredEventRow
from daimon.core.web_admin import WebAdmin
from daimon.core.web_roles_rules import Role
from fastmcp.exceptions import ToolError

from .parity_fakes import fake_runtime, make_auth

NOW = datetime(2030, 1, 15, 12, tzinfo=UTC)
PERSON = SimpleNamespace(id=uuid.uuid4(), account_id=uuid.uuid4(), display_name="Pat Doe")


class Harness:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.role: Role = "admin"
        self.private = "shown"
        self.audits: list[tuple[str, str, str | None]] = []
        self.agents = {"agt_1": "Ada"}
        self.runtime = fake_runtime()
        self.auth = make_auth()
        self.conversation = view.conversation_id(
            MonitoredConversationRow(
                platform="web",
                account_id=PERSON.account_id,
                conversation="w1",
                day="2030-01-15",
                questions=2,
                tools=1,
                first_at=NOW,
                last_at=NOW,
                cursor_id=uuid.uuid4(),
            )
        )

        async def admin_of(_rt: Any, auth: Any) -> WebAdmin:
            return WebAdmin(tenant_id=auth.tenant_id, account_id=auth.account_id, role=self.role)

        async def names(*_a: Any, **_k: Any) -> dict[str, str]:
            return self.agents

        async def details(*_a: Any, **_k: Any) -> str:
            return self.private

        async def people(*_a: Any, **_k: Any) -> list[Any]:
            return [PERSON]

        async def state(*_a: Any, **_k: Any) -> view.AgentMonitoring:
            return view.AgentMonitoring(
                direct_messages=cast(Any, SimpleNamespace(on=True, since=NOW)),
                coding_tools=cast(Any, SimpleNamespace(on=False, since=None)),
            )

        async def conversations(*_a: Any, **_k: Any) -> list[MonitoredConversationRow]:
            return [
                MonitoredConversationRow(
                    platform="web",
                    account_id=PERSON.account_id,
                    conversation="w1",
                    day="2030-01-15",
                    questions=2,
                    tools=1,
                    first_at=NOW,
                    last_at=NOW,
                    cursor_id=uuid.uuid4(),
                )
            ]

        async def transcript(*_a: Any, **_k: Any) -> list[MonitoredEventRow]:
            return [
                MonitoredEventRow(
                    id=uuid.uuid4(),
                    tenant_id=uuid.uuid4(),
                    agent_id=uuid.uuid4(),
                    platform="web",
                    account_id=PERSON.account_id,
                    conversation="w1",
                    turn_ref="t1",
                    kind="question",
                    content="How do I deploy?",
                    tool_name=None,
                    outcome=None,
                    created_at=NOW,
                )
            ]

        def audit_to(_auth: Any, action: str, detail: str | None = None) -> Any:
            async def record(_db: Any, name: str) -> None:
                self.audits.append((action, name, detail))

            return record

        monkeypatch.setattr(tool, "web_admin_of", admin_of)
        monkeypatch.setattr(tool, "agent_names", names)
        monkeypatch.setattr(tool, "private_details", details)
        monkeypatch.setattr(people_store, "list_people", people)
        monkeypatch.setattr(view, "agent_monitoring", state)
        monkeypatch.setattr(view, "list_conversations", conversations)
        monkeypatch.setattr(view, "get_transcript", transcript)
        monkeypatch.setattr(tool, "audit_to", audit_to)

    async def listed(self, **over: Any) -> tool.MonitoredActivity:
        args: dict[str, Any] = dict(
            agent_id="agt_1",
            where=None,
            person_id=None,
            first=None,
            last=None,
            after=None,
            limit=50,
            origin_context_id=None,
        )
        return await tool._list_impl(self.runtime, self.auth, **{**args, **over})  # pyright: ignore[reportPrivateUsage]

    async def opened(self, **over: Any) -> tool.MonitoredTranscript:
        args: dict[str, Any] = dict(
            agent_id="agt_1",
            conversation_id=self.conversation,
            after=None,
            origin_context_id=None,
        )
        return await tool._transcript_impl(self.runtime, self.auth, **{**args, **over})  # pyright: ignore[reportPrivateUsage]


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Harness:
    return Harness(monkeypatch)


async def test_list_when_admin_names_the_person_and_audits_the_read(harness: Harness) -> None:
    """A list read in a private place names people, reports each switch, and is audited."""
    result = await harness.listed()

    assert [(c.person, c.where, c.questions, c.tool_calls) for c in result.conversations] == [
        ("Pat Doe", "web", 2, 1)
    ], result
    assert (result.direct_messages_on_since, result.coding_tools_on_since) == (NOW, None), result
    assert harness.audits == [(view.AUDIT_LIST, "Ada", None)], "one audit row per list read"


async def test_list_when_shared_place_hides_names(harness: Harness) -> None:
    """Outside a private place a person reads `Person 1`, with no id."""
    harness.private = "direct_message"

    result = await harness.listed()

    assert [(c.person, c.person_id) for c in result.conversations] == [("Person 1", None)], result
    assert result.private_details == "direct_message", result


async def test_list_when_not_admin_is_refused_without_audit(harness: Harness) -> None:
    """A role without workspace management reads nothing and leaves no audit row."""
    harness.role = "member"

    with pytest.raises(ToolError, match="for admins"):
        await harness.listed()

    assert harness.audits == [], "a refused read is not audited"


async def test_list_when_agent_unknown_is_refused(harness: Harness) -> None:
    """An agent that is not here is not found."""
    with pytest.raises(ToolError, match="agent was not found"):
        await harness.listed(agent_id="agt_other")

    assert harness.audits == [], "no audit row for an unknown agent"


async def test_list_when_later_page_is_not_audited_again(harness: Harness) -> None:
    """Loading more is the same read: only the first page writes an audit row."""
    first = await harness.listed()
    assert first.next_page is None, first

    with pytest.raises(ToolError, match="not a page"):
        await harness.listed(after="junk")

    assert len(harness.audits) == 1, harness.audits


async def test_transcript_when_private_returns_events_and_audits_with_person(
    harness: Harness,
) -> None:
    """A private reader gets the events and the audit row names whose conversation."""
    result = await harness.opened()

    assert [(e.kind, e.content) for e in result.events] == [("question", "How do I deploy?")], (
        result
    )
    assert harness.audits == [(view.AUDIT_OPEN, "Ada", "Pat Doe")], harness.audits


async def test_transcript_when_in_a_channel_is_refused_without_audit(harness: Harness) -> None:
    """What people said never goes into a shared channel."""
    harness.private = "direct_message"

    with pytest.raises(ToolError, match="direct message"):
        await harness.opened()

    assert harness.audits == [], "a refused read is not audited"


async def test_transcript_when_handle_is_not_ours_is_refused(harness: Harness) -> None:
    """A made-up conversation id is refused before anything is read."""
    with pytest.raises(ToolError, match="not one list_monitored_activity gave"):
        await harness.opened(conversation_id="bogus")

    assert harness.audits == [], "no audit row for a bad handle"
