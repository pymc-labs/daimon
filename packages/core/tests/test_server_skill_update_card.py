"""The card for approving a server skill's update shows the update, read from the mirror."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from daimon.core.confirmation import ConfirmationPrompt, ServerSkillUpdateCard, prompt_for_tool_call
from daimon.core.stores import server_skills
from daimon.core.tool_safety import DAIMON_SERVER_NAME, ToolCall, ToolSafetyPolicy
from daimon.core.turn.approvals import interactive_decider, skill_update_lookup

CALL = ToolCall(
    tool_use_id="t1",
    server_name=DAIMON_SERVER_NAME,
    tool_name="approve_server_skill_update",
    input={"skill_id": "skill_1", "digest": "a" * 64, "origin_context_id": "ctx"},
)
TRUSTED = frozenset({DAIMON_SERVER_NAME})
UPDATE = ServerSkillUpdateCard(
    name="Deploy helper",
    version="1.0",
    new_version="1.1",
    changes={"added": ["scripts/run.sh"], "changed": ["SKILL.md"], "removed": []},
)


def _prompt(update: ServerSkillUpdateCard | None) -> ConfirmationPrompt:
    return prompt_for_tool_call(
        CALL,
        requester_platform_user_id="u1",
        now=datetime.now(UTC),
        trusted_servers=TRUSTED,
        server_skill_update=update,
    )


def test_prompt_when_the_update_is_known_then_it_names_the_skill_and_the_changes() -> None:
    prompt = _prompt(UPDATE)
    shown = "\n".join([prompt.title, prompt.consequence or "", *prompt.detail_lines])
    assert "Deploy helper" in prompt.title
    for fact in ("1.0 → 1.1", "Added: scripts/run.sh", "Changed: SKILL.md"):
        assert fact in shown
    assert "Removed" not in shown, "a kind with no paths adds no line"
    assert "a" * 64 not in shown, "the digest is not shown"
    assert "ctx" not in shown, "the origin id is not shown"


def test_prompt_when_many_paths_changed_then_it_counts_the_rest() -> None:
    many = ServerSkillUpdateCard(
        name="S", version=None, new_version="2", changes={"added": [f"f{i}" for i in range(8)]}
    )
    shown = "\n".join(_prompt(many).detail_lines)
    assert "f4 and 3 more" in shown
    assert "f5" not in shown
    assert "none → 2" in shown


def test_prompt_when_the_update_is_unknown_then_it_is_the_generic_card() -> None:
    prompt = _prompt(None)
    assert "Deploy helper" not in prompt.title + (prompt.consequence or "")
    assert prompt.title, "a card still shows"


def _mirror(**fields: object) -> object:
    from types import SimpleNamespace

    base = {
        "name": "Deploy helper",
        "version_label": "1.0",
        "pending_label": "1.1",
        "pending_digest": "a" * 64,
        "pending_changes": {"added": ["scripts/run.sh"]},
    }
    return SimpleNamespace(**{**base, **fields})


class _Factory:
    def __call__(self) -> "_Factory":
        return self

    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *_a: object) -> None:
        return None


@pytest.mark.parametrize(
    ("mirror", "found"),
    [
        (_mirror(), True),
        (_mirror(pending_digest="b" * 64), False),
        (_mirror(pending_digest=None), False),
        (None, False),
    ],
    ids=["match", "moved_on", "nothing_pending", "gone"],
)
async def test_lookup_reads_the_mirror_and_misses_unless_the_digest_is_pending(
    monkeypatch: pytest.MonkeyPatch, mirror: object, found: bool
) -> None:
    seen: dict[str, object] = {}

    async def mirror_of_skill(_s: object, **keys: object) -> object:
        seen.update(keys)
        return mirror

    monkeypatch.setattr(server_skills, "mirror_of_skill", mirror_of_skill)
    import uuid

    tenant = uuid.uuid4()
    card = await skill_update_lookup(_Factory(), tenant_id=tenant)(CALL)  # type: ignore[arg-type]
    assert seen == {"tenant_id": tenant, "skill_id": "skill_1"}
    assert (card is not None) == found
    if card is not None:
        assert (card.name, card.version, card.new_version) == ("Deploy helper", "1.0", "1.1")
        assert card.changes == {"added": ["scripts/run.sh"]}


async def test_lookup_when_the_call_lacks_a_skill_then_misses() -> None:
    bare = CALL.model_copy(update={"input": {}})
    assert await skill_update_lookup(_Factory(), tenant_id=__import__("uuid").uuid4())(bare) is None  # type: ignore[arg-type]


async def _decide(lookup: AsyncMock | None) -> tuple[bool, str | None, AsyncMock]:
    card = AsyncMock(return_value="denied")
    result = await interactive_decider(
        ToolSafetyPolicy(enabled=True),
        requester_platform_user_id="u1",
        confirm=card,
        trusted_servers=TRUSTED,
        confirmed_tools=frozenset({"approve_server_skill_update"}),
        skill_update_for=lookup,
    )(CALL)
    return result.allow, result.deny_message, card


async def test_decider_when_the_update_is_found_then_the_card_shows_it() -> None:
    lookup = AsyncMock(return_value=UPDATE)
    allow, _, card = await _decide(lookup)
    assert not allow
    lookup.assert_awaited_once_with(CALL)
    assert card.await_args is not None
    prompt: ConfirmationPrompt = card.await_args.args[0]
    assert "Deploy helper" in prompt.title


@pytest.mark.parametrize(
    "lookup",
    [AsyncMock(return_value=None), AsyncMock(side_effect=OSError), None],
    ids=["miss", "read_fails", "no_lookup"],
)
async def test_decider_when_the_update_cannot_be_loaded_then_it_denies_without_a_card(
    lookup: AsyncMock | None,
) -> None:
    allow, message, card = await _decide(lookup)
    assert not allow
    assert message == "Couldn't load this update. Check the skill again."
    card.assert_not_awaited()


def test_prompt_when_runnable_files_come_after_many_others_then_they_are_listed_first() -> None:
    update = ServerSkillUpdateCard(
        name="S",
        version="1",
        new_version="2",
        changes={
            "added": [
                *(f"docs/n{i}.md" for i in range(6)),
                "bin/run.sh",
                "tools/setup.py",
                "Makefile",
            ],
        },
    )
    shown = "\n".join(_prompt(update).detail_lines)
    line = next(row for row in shown.splitlines() if row.startswith("Added:"))
    for runnable in ("Makefile", "bin/run.sh", "tools/setup.py"):
        assert runnable in line
    assert "and 4 more" in line


def test_prompt_when_a_path_is_long_then_it_is_cut_in_the_middle_keeping_its_extension() -> None:
    path = "scripts/" + "very_long_directory_name/" * 6 + "install_everything.sh"
    update = ServerSkillUpdateCard(
        name="S", version="1", new_version="2", changes={"added": [path]}
    )
    line = next(row for row in _prompt(update).detail_lines if row.startswith("Added:"))
    shown = line.removeprefix("Added: ")
    assert len(shown) <= 60
    assert shown.startswith("scripts/") and shown.endswith("install_everything.sh")
    assert "…" in shown
