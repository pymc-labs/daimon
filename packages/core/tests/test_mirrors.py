"""Mirrors: which events a mirror copies, the rule refusals, and the Q&A posts."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from daimon.core.access_policy import AgentRule, ChannelRule, TenantAccessPolicy
from daimon.core.mirror_events import ActorRef, ConversationEventV1, SourceRef
from daimon.core.mirrors import MirrorRefusal, matching_mirrors, mirror_refusal, render_qa
from daimon.core.permissions import channel_permissions
from daimon.core.stores.domain import MirrorRow

_TENANT = uuid.uuid4()
_AGENT = uuid.uuid4()
_NOW = datetime(2026, 10, 1, tzinfo=UTC)
_HOME = "100"
_SEALED = "200"
_OPEN = "300"
_POLICY = TenantAccessPolicy(
    channel_rules={
        _HOME: ChannelRule(readers="own", writers="own"),
        _SEALED: ChannelRule(readers="inside"),
        "400": ChannelRule(writers="none"),
    },
    agent_rules={"home-bot": AgentRule(runs_in=(_HOME,))},
)


def _mirror(*, consent: bool = False, status: str = "active", **changes: object) -> MirrorRow:
    values: dict[str, object] = {
        "id": uuid.uuid4(),
        "tenant_id": _TENANT,
        "name": "m",
        "source_agent_id": _AGENT,
        "source_agent_name": "helper",
        "source_agent_names": ("helper",),
        "dest_tenant_id": _TENANT,
        "dest_platform": "discord",
        "dest_kind": "channel",
        "dest_id": _OPEN,
        "consent_basis": "attested" if consent else None,
        "status": status,
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    return MirrorRow.model_validate(values | changes)


def _event(
    *, channel_id: str | None = None, seal_ids: tuple[str, ...] = (), **changes: object
) -> ConversationEventV1:
    values: dict[str, object] = {
        "id": uuid.uuid4(),
        "surface": "agent_chat",
        "occurred_at": _NOW,
        "tenant_id": _TENANT,
        "agent_id": _AGENT,
        "source": SourceRef(channel_id=channel_id, seal_ids=seal_ids, session_key="sesn_abc123"),
        "actor": ActorRef(platform_user_id="9001", display_name="Ada"),
        "question": "What changed?",
        "answer": "Nothing.",
    }
    return ConversationEventV1.model_validate(values | changes)


def test_matching_takes_the_agents_active_mirrors_of_the_same_tenant_only() -> None:
    mine = _mirror(consent=True)
    others = [
        _mirror(consent=True, source_agent_id=uuid.uuid4()),
        _mirror(consent=True, tenant_id=uuid.uuid4()),
        _mirror(consent=True, status="paused"),
    ]

    assert matching_mirrors([mine, *others], _event()) == (mine,), (
        "another agent's, another tenant's or a paused mirror must not match"
    )


def test_a_personal_session_needs_recorded_consent_but_a_bound_one_does_not() -> None:
    plain = _mirror()

    assert matching_mirrors([plain], _event()) == (), "an unbound session needs consent"
    assert matching_mirrors([plain], _event(channel_id=_OPEN)) == (plain,), (
        "a channel-bound session is judged by the channel's rules alone"
    )


def _refusal(
    dest_id: str,
    *,
    names: tuple[str, ...] = ("helper",),
    event: ConversationEventV1 | None = None,
    parent: str | None = None,
) -> MirrorRefusal | None:
    dest = channel_permissions(_POLICY, channel_id=dest_id, parent_channel_id=parent)
    source = event.source if event is not None else None
    return mirror_refusal(_POLICY, agent_names=names, dest=dest, source=source)


def test_refusals_follow_the_channel_rules() -> None:
    assert _refusal("400") == "writers_none", "nothing posts where writers are none"
    assert _refusal(_OPEN, event=_event(channel_id=_SEALED, seal_ids=(_SEALED,))) == (
        "not_a_reader"
    ), "a sealed session's content must not leave its channel"
    assert _refusal(_HOME) == "own_agents_only", "an outside agent must not enter a home"
    assert _refusal(_OPEN, names=("home-bot",)) == "own_agents_only", (
        "a home agent's content must not leave its home"
    )


def test_a_home_agent_may_mirror_into_its_own_home_and_threads_in_it() -> None:
    """The primary use case: an agent's agent-chat Q&A into its own private channel."""
    assert _refusal(_HOME, names=("home-bot",), event=_event()) is None, (
        "a home's own agent keeps its content at home"
    )
    assert _refusal("555", names=("home-bot",), parent=_HOME) is None, (
        "a thread in the home counts as the home"
    )


def test_a_sealed_session_may_be_mirrored_inside_its_channel() -> None:
    event = _event(channel_id=_SEALED, seal_ids=(_SEALED,))

    assert _refusal("777", event=event, parent=_SEALED) is None, (
        "a thread inside the sealed channel is still inside it"
    )


def test_a_session_stays_under_its_stamped_seal_once_its_channel_reopens() -> None:
    """The source carries the session's stamped seal, not only the channel's seal now."""
    reopened = TenantAccessPolicy()
    dest = channel_permissions(reopened, channel_id=_OPEN)

    def refusal(source: SourceRef) -> MirrorRefusal | None:
        return mirror_refusal(reopened, agent_names=("helper",), dest=dest, source=source)

    assert refusal(SourceRef(channel_id="X", seal_ids=(), session_key="s")) is None, (
        "without its stamp the session would read as open"
    )
    assert refusal(SourceRef(channel_id="X", seal_ids=("X",), session_key="s")) == (
        "not_a_reader"
    ), "a session sealed when it ran stays sealed"


def test_a_session_stamped_inside_a_home_stays_there() -> None:
    event = _event(channel_id="900", seal_ids=(_HOME,))

    assert _refusal(_HOME, event=event) == "own_agents_only", (
        "only the home's own agents carry what was said in it"
    )
    assert _refusal(_HOME, names=("home-bot",), event=event) is None, (
        "the home's own agent may mirror it into the home"
    )


def test_render_redacts_clips_and_names_the_asker_without_their_id() -> None:
    secret = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789"
    event = _event(
        question=f"is {secret} valid?",
        answer="x" * 250,
        actor=ActorRef(platform_user_id="9001", display_name="Ada", is_external=True),
    )

    post = render_qa(event, agent_name="helper", max_chars=200)

    assert secret not in post.question, "secrets are redacted"
    assert "*Ada (external)*" in post.question, "an external asker is labelled"
    assert "9001" not in post.question + post.answer, "the platform id is never posted"
    assert post.answer.endswith("> … (50 more characters)"), post.answer


def test_render_quotes_every_line_and_closes_a_clipped_fence() -> None:
    event = _event(question="# Title\n@everyone", answer="```py\n" + "x\n" * 200)

    post = render_qa(event, agent_name="helper", max_chars=100)

    body = post.question.split("\n")[1:]
    assert body == ["> # Title", "> @everyone"], "the asker's text is quoted, not rendered"
    lines = post.answer.split("\n")[1:]
    assert all(line.startswith("> ") for line in lines), lines
    assert [line for line in lines if "```" in line] == ["> ```py", "> ```"], (
        "a fence cut by the clip is closed"
    )
    assert post.thread_name == "helper · abc123", "one thread per session, named by its tail"
