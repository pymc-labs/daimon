"""Setup identity validation never substitutes a deleted target's namesake."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import httpx
import pytest
from daimon.core.errors import DaimonError
from daimon.core.setup_conversations import (
    build_setup_opener,
    get_setup_responder,
    resolve_setup_agents,
    setup_target_label,
    setup_thread_name,
    shared_keys_sentence,
)
from daimon.testing.ma import MARouter, build_fake_anthropic, list_response
from daimon.testing.ma_models import ma_agent


@pytest.mark.parametrize("missing_target", [False, True])
async def test_setup_resolves_builtin_without_parent_default_or_namesake_fallback(
    missing_target: bool,
) -> None:
    tenant_id = uuid.uuid4()
    now = datetime.now(UTC)
    daimon = ma_agent(
        id="ag_builtin",
        name="daimon",
        tenant_id=tenant_id,
        metadata={"daimon_managed": "true"},
        created_at=now,
    )
    specialist = ma_agent(
        id="ag_specialist", name="specialist", tenant_id=tenant_id, created_at=now
    )
    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda req, match: list_response(
            [daimon.model_dump(mode="json"), specialist.model_dump(mode="json")]
        ),
    )
    router.add(
        "GET",
        r"/v1/agents/ag_specialist",
        lambda req, match: httpx.Response(200, json=specialist.model_dump(mode="json")),
    )
    router.add(
        "GET",
        r"/v1/agents/ag_deleted",
        lambda req, match: httpx.Response(
            404, json={"type": "error", "error": {"type": "not_found_error", "message": "missing"}}
        ),
    )
    client = build_fake_anthropic(router.dispatch)
    if missing_target:
        with pytest.raises(DaimonError, match="no longer exists"):
            await resolve_setup_agents(client, tenant_id=tenant_id, target_ma_agent_id="ag_deleted")
    else:
        responder, target = await resolve_setup_agents(
            client, tenant_id=tenant_id, target_ma_agent_id="ag_specialist"
        )
        assert responder.id == "ag_builtin", "Daimon always responds to setup"
        assert target is not None and target.id == "ag_specialist", (
            "selected specialist remains target"
        )


async def test_missing_bound_responder_is_explicit_and_does_not_reconcile() -> None:
    client = build_fake_anthropic(
        lambda req: httpx.Response(
            404, json={"type": "error", "error": {"type": "not_found_error", "message": "missing"}}
        )
    )
    with pytest.raises(DaimonError, match="Daimon responder is missing"):
        await get_setup_responder(client, tenant_id=uuid.uuid4(), ma_agent_id="ag_deleted")


def test_setup_thread_name_names_the_target_when_one_is_chosen() -> None:
    assert setup_thread_name("writer") == "Managing writer", (
        "the thread name says which agent is being managed"
    )


def test_setup_thread_name_stands_in_for_an_unchosen_target() -> None:
    assert setup_thread_name(None) == "Managing agents", (
        "a conversation opened before a target is chosen still needs a readable name"
    )


def test_setup_thread_name_truncates_a_name_discord_would_reject() -> None:
    name = setup_thread_name("x" * 200)
    assert len(name) == 100, "Discord rejects a thread name over 100 characters outright"


def test_shared_keys_sentence_names_the_agent_whose_keys_are_shared() -> None:
    assert shared_keys_sentence("writer") == "Anyone who talks to writer can use these.", (
        "the sentence must say the keys follow the agent, not the person who added them"
    )


_MEMBER_ROLE_LINE = (
    "You can make your own agents and add keys or tools to any of them. "
    "Changes to an agent other people use, or to who answers in a channel, "
    "need a workspace admin. Ask anyway and I'll write the request for them."
)
_ADMIN_ROLE_LINE = (
    "You can change any agent and choose who answers in each channel. "
    "Daimon itself can't be edited; ask me to fork it and change the fork."
)


@pytest.mark.parametrize(
    ("target", "subject"),
    [
        (None, "Which agent is this about? You can also ask me to make a new one."),
        (
            "Researcher",
            "This thread is about Researcher. Tell me what to change, or name another agent.",
        ),
    ],
)
def test_opener_names_its_target_and_only_explains_how_to_reply(target, subject) -> None:
    assert build_setup_opener(
        target_display=target,
        bot_mention="<@bot>",
        is_admin=False,
        admin_noun="a workspace admin",
    ) == (f"{subject}\n\n{_MEMBER_ROLE_LINE}\n\nMention <@bot> to reply."), (
        "the opener says what the thread is about, what this person can do, and how to reply"
    )


def test_opener_tells_an_admin_what_only_an_admin_can_do() -> None:
    assert build_setup_opener(
        target_display="Researcher",
        bot_mention="<@bot>",
        is_admin=True,
        admin_noun="a workspace admin",
    ) == (
        "This thread is about Researcher. Tell me what to change, or name another agent."
        f"\n\n{_ADMIN_ROLE_LINE}\n\nMention <@bot> to reply."
    ), "an admin is told they can route channels, and that the starting agent is forked, not edited"


def test_opener_uses_the_platform_name_for_an_admin() -> None:
    opener = build_setup_opener(
        target_display=None,
        bot_mention="<@bot>",
        is_admin=False,
        admin_noun="someone with Manage Server",
    )
    assert "someone with Manage Server" in opener, (
        "each platform names the admin the way its own members know them"
    )


def test_roster_setup_label_names_the_target_without_changing_its_identity() -> None:
    assert setup_target_label("Researcher") == "⚙️ Manage Researcher", "name the action's target"
    assert setup_target_label(None) == "⚙️ Manage agents", "no target opens the chooser"
    label = setup_target_label("x" * 64)
    assert len(label) == 30 and label.endswith("…"), "long names fit the button with an ellipsis"
