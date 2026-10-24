"""Nothing a person sees, or the model reads, on one chat platform names another.

People on one platform must never learn that Daimon runs on the others, so every
text shown on platform P, and every text the model receives on a P turn, may name
P and no other chat app. Two guards:

- renderers are called per platform and their output searched for a foreign name;
- the source is scanned: a prose string literal that names a chat app has to sit in
  a file allowed to name it (its own adapter, or a module that picks wording by
  platform), so a new shared string cannot slip a name in.
"""

from __future__ import annotations

import ast
import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import get_args

import pytest
import yaml
from daimon.core.access_policy import ChannelRule
from daimon.core.here_card import HereCard, render_here_card
from daimon.core.platform_words import Place, turn_words
from daimon.core.privacy import PurgePreview, PurgePreviewRow, summary_line
from daimon.core.routine_delivery import failure_notice_text, render_routine_controls
from daimon.core.stores.domain import ChatPlatform, Role, RoutineRow, TurnOriginRow
from daimon.core.thread_participation import (
    ParticipationMode,
    ParticipationScope,
    ResolvedParticipation,
    build_participation_explanation,
    build_participation_note,
    scope_label,
)
from daimon.core.turn import TerminationReason, render_termination_notice
from daimon.core.turn.errors import AdmissionDenialReason
from daimon.core.turn.notices import RefusalNouns, admission_refusal_text
from daimon.core.turn_origin import render_turn_origin

_APPS: tuple[ChatPlatform, ...] = get_args(ChatPlatform)
_REPO = Path(__file__).resolve().parents[2]


def _foreign(platform: ChatPlatform, text: str) -> list[str]:
    return [app for app in _APPS if app != platform and app in text.lower()]


def _assert_only_own(platform: ChatPlatform, text: str) -> None:
    assert _foreign(platform, text) == [], (
        f"{platform} text names {_foreign(platform, text)}: {text!r}"
    )


def _origin(platform: ChatPlatform, **overrides: object) -> TurnOriginRow:
    fields: dict[str, object] = {
        "id": uuid.UUID("11111111-1111-1111-1111-111111111111"),
        "tenant_id": uuid.UUID("22222222-2222-2222-2222-222222222222"),
        "account_id": uuid.UUID("33333333-3333-3333-3333-333333333333"),
        "platform": platform,
        "parent_channel_id": "C_PARENT",
        "thread_id": "T_THREAD",
        "responder_ma_agent_id": "agt_stats",
        "responder_name": "stats-bot",
        "configuration_target_ma_agent_id": None,
        "configuration_target_name": None,
        "role": Role.USER,
        "is_setup": False,
        "created_at": datetime(2026, 9, 13, tzinfo=UTC),
        "expires_at": datetime(2026, 9, 13, 2, tzinfo=UTC),
    }
    fields.update(overrides)
    return TurnOriginRow.model_validate(fields)


@pytest.mark.parametrize("platform", _APPS)
@pytest.mark.parametrize("place", ["channel", "dm"])
def test_a_turns_controls_name_only_its_own_platform(platform: ChatPlatform, place: Place) -> None:
    thread = {"dm": "D1:1" if platform == "slack" else "dm:1", "channel": "T_THREAD"}[place]
    channel = "D1" if place == "dm" else "C_PARENT"
    rendered = render_turn_origin(
        _origin(platform, parent_channel_id=channel, thread_id=thread),
        effective_is_admin=False,
        responder_handle="@daimon",
    )
    controls = json.loads(rendered.split("\n")[1])

    assert controls["platform"] == platform
    _assert_only_own(platform, rendered.replace(f'"platform": "{platform}"', ""))
    _assert_only_own(platform, json.dumps(turn_words(platform, place=place)))


_DESTINATIONS = {"discord": "1234", "slack": "C0123ABC", "teams": "19:abc123@thread.tacv2"}


@pytest.mark.parametrize("platform", _APPS)
def test_a_routine_runs_with_controls_naming_only_its_destinations_platform(
    platform: ChatPlatform,
) -> None:
    now = datetime(2026, 9, 13, tzinfo=UTC)
    row = RoutineRow.model_validate(
        {
            "id": uuid.uuid4(),
            "tenant_id": uuid.uuid4(),
            "created_by_user_id": "U1",
            "agent_id": "ag",
            "agent_name": "daimon",
            "cron_expr": "0 9 * * 1",
            "timezone": "UTC",
            "trigger_message": "go",
            "enabled": True,
            "next_fire_at": None,
            "last_fired_at": None,
            "last_error": None,
            "last_result_tail": None,
            "delivery_payload": None,
            "destination_kind": "channel",
            "destination_id": _DESTINATIONS[platform],
            "created_at": now,
            "updated_at": now,
        }
    )
    text = render_routine_controls(row, platform=platform)

    _assert_only_own(platform, text.replace(f'"platform": "{platform}"', ""))


@pytest.mark.parametrize("platform", _APPS)
@pytest.mark.parametrize("place", ["channel", "dm"])
def test_refusals_and_notices_name_only_their_own_platform(
    platform: ChatPlatform, place: Place
) -> None:
    nouns = RefusalNouns.for_platform(platform, place=place)
    for reason in get_args(AdmissionDenialReason):
        for in_dm in (False, True):
            _assert_only_own(platform, admission_refusal_text(reason, nouns, in_dm=in_dm))
    for reason in TerminationReason:
        _assert_only_own(platform, failure_notice_text(reason, platform=platform))
        for in_dm in (False, True):
            notice = render_termination_notice(
                reason,
                in_dm=in_dm,
                setup_command=nouns.setup,
                billing_command=nouns.billing,
                setup_verb=nouns.verb,
            )
            if notice is not None:
                _assert_only_own(platform, notice.plain_text())


@pytest.mark.parametrize("platform", _APPS)
@pytest.mark.parametrize("state", ["no_view", "no_replies", "no_agent", "blocked", "ok"])
def test_the_here_card_names_only_its_own_platform(platform: ChatPlatform, state: str) -> None:
    card = HereCard(
        platform=platform,
        channel_rule=ChannelRule(),
        who_may_answer="",
        reads_kept_inside=False,
        agent_name=None if state == "no_agent" else "ResearchBot",
        tier="channel",
        bot_can_view=state != "no_view",
        effective_writers="none" if state == "no_replies" else "any",
        agent_can_answer_here=state != "blocked",
        effective_readers="any",
        text="",
    )
    shown = render_here_card(card)

    _assert_only_own(platform, f"{shown.title} {shown.subline}")


def _full_preview() -> PurgePreview:
    row = PurgePreviewRow(count=2, example=None)
    return PurgePreview(**{name: row for name in PurgePreview.model_fields})


def test_the_privacy_summary_names_no_chat_platform() -> None:
    text = summary_line(_full_preview())

    assert [app for app in _APPS if app in text.lower()] == []


@pytest.mark.parametrize("platform", _APPS)
def test_participation_texts_name_the_platforms_own_word_for_the_workspace(
    platform: ChatPlatform,
) -> None:
    """The workspace tier reads as the platform's own space: an organisation on Teams."""
    space = {"discord": "server", "slack": "workspace", "teams": "organisation"}[platform]
    effective = ResolvedParticipation(ParticipationMode.DISABLED, "workspace")
    texts = [
        build_participation_explanation(effective, thread_id=None, platform=platform),
        build_participation_note(
            scope=ParticipationScope.WORKSPACE,
            scope_id=None,
            requested=ParticipationMode.ON,
            effective=effective,
            platform=platform,
        ),
        scope_label(ParticipationScope.WORKSPACE, None, platform),
    ]
    for text in texts:
        assert space in text, text
        if platform != "slack":
            assert "workspace" not in text, text
        _assert_only_own(platform, text)


# --- the source ---------------------------------------------------------------------

#: Files whose strings may name a platform other than their own, and which. Each picks
#: its wording from the caller's platform (so a name appears only on that platform), or
#: is operator-facing, or is the prompt source the Teams variant is cut from.
_MAY_NAME: dict[str, frozenset[str]] = {
    "packages/adapters/mcp/daimon/adapters/mcp/hub/slack_provider.py": frozenset({"slack"}),
    "packages/adapters/mcp/daimon/adapters/mcp/oauth_mcp.py": frozenset(_APPS),
    "packages/adapters/mcp/daimon/adapters/mcp/oauth_microsoft.py": frozenset({"teams"}),
    "packages/adapters/mcp/daimon/adapters/mcp/oauth_slack.py": frozenset({"slack"}),
    "packages/adapters/mcp/daimon/adapters/mcp/tools/_channel_target.py": frozenset({"slack"}),
    "packages/adapters/mcp/daimon/adapters/mcp/tools/credential_requests.py": frozenset({"teams"}),
    "packages/adapters/mcp/daimon/adapters/mcp/tools/direct_messages.py": frozenset(
        {"discord", "slack"}
    ),
    # Explicit platform availability is required before registration or billing.
    # Platform-specific start tests lock this refusal to avoid charging without a wake.
    "packages/adapters/mcp/daimon/adapters/mcp/tools/long_jobs.py": frozenset({"discord"}),
    "packages/adapters/mcp/daimon/adapters/mcp/tools/routines.py": frozenset({"slack"}),
    "packages/adapters/mcp/daimon/adapters/mcp/tools/skill_uploads.py": frozenset(_APPS),
    "packages/adapters/scheduler/daimon/adapters/scheduler/settings.py": frozenset({"discord"}),
    "packages/core/daimon/core/agent_guidance.py": frozenset(_APPS),
    "packages/core/daimon/core/platform_system.py": frozenset(_APPS),
    "packages/core/daimon/core/platform_words.py": frozenset(_APPS),
    "packages/core/daimon/core/microsoft_identity.py": frozenset({"teams"}),
    "packages/core/daimon/core/native_admins.py": frozenset({"discord"}),
    "packages/core/daimon/core/routine_delivery.py": frozenset(_APPS),
    "packages/core/daimon/core/skills/add.py": frozenset({"teams"}),
}
#: Other efforts' open work: their strings are listed in the W11 report, not guarded yet.
_PENDING = (
    "packages/adapters/mcp/daimon/adapters/mcp/oauth_github.py",
    "packages/adapters/mcp/daimon/adapters/mcp/oauth_github_personal.py",
)
#: Where a person or the model meets text: the adapters, core and the MCP server. Operator
#: settings, stores, migrations, the CLI and test support are not.
_SCANNED = (
    "packages/core/daimon/core",
    "packages/adapters/mcp/daimon/adapters/mcp",
    "packages/adapters/scheduler/daimon",
    "packages/adapters/discord/daimon",
    "packages/adapters/slack/daimon",
    "packages/adapters/teams/daimon",
)
# The per-platform tool packages speak for their one platform; `microsoft` is Teams-only.
_SKIPPED_PARTS = (
    "/stores/",
    "/alembic/",
    "/tools/discord/",
    "/tools/slack/",
    "/tools/teams/",
    "/tools/microsoft/",
)
_SKIPPED_FILES = ("core/config.py", "core/_models.py")
_OWN_ADAPTER = {
    "packages/adapters/discord/": "discord",
    "packages/adapters/slack/": "slack",
    "packages/adapters/teams/": "teams",
}
_LOG_CALLS = frozenset({"debug", "info", "warning", "error", "exception", "critical", "log"})
_NAME = re.compile(r"discord|slack|teams", re.IGNORECASE)
_APP_WORD = re.compile(r"Discord|Slack|Teams")


def _docstring_ids(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                ids.add(id(first.value))
    return ids


def _in_log_call(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    while node in parents:
        node = parents[node]
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _LOG_CALLS
        ):
            return True
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            return False
    return False


def _named_apps(path: Path) -> set[str]:
    """The chat apps a file's prose string literals name (docstrings and logs aside)."""
    tree = ast.parse(path.read_text())
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    skip = _docstring_ids(tree)
    named: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if (
            id(node) in skip
            or isinstance(parents.get(node), ast.Expr)  # an attribute docstring
            or not _NAME.search(node.value)
            or _in_log_call(node, parents)
        ):
            continue
        # A bare lowercase token is a platform key, not prose.
        if not re.search(r"\s", node.value) and not _APP_WORD.fullmatch(node.value):
            continue
        # SQL and table names are not text anybody reads.
        if re.search(r"\b(SELECT|INSERT|UPDATE|DELETE)\b", node.value):
            continue
        named.update(app for app in _APPS if app in node.value.lower())
    return named


def _scanned_files() -> list[Path]:
    files: list[Path] = []
    for root in _SCANNED:
        for path in sorted((_REPO / root).rglob("*.py")):
            rel = path.relative_to(_REPO).as_posix()
            if (
                "/tests/" in rel
                or any(part in rel for part in _SKIPPED_PARTS)
                or rel.endswith(_SKIPPED_FILES)
                or rel in _PENDING
            ):
                continue
            files.append(path)
    return files


def _allowed_apps(rel: str) -> frozenset[str]:
    for prefix, own in _OWN_ADAPTER.items():
        if rel.startswith(prefix):
            return frozenset({own})
    return _MAY_NAME.get(rel, frozenset())


def test_a_string_names_a_chat_app_only_where_its_file_may() -> None:
    offences: list[str] = []
    for path in _scanned_files():
        rel = path.relative_to(_REPO).as_posix()
        extra = _named_apps(path) - _allowed_apps(rel)
        if extra:
            offences.append(f"{rel}: {sorted(extra)}")

    assert offences == [], (
        "a string shown to a person or the model names a chat app this file may not; word it "
        "neutrally or by the caller's platform (`words_for(platform)`):\n" + "\n".join(offences)
    )


def test_every_allowance_is_still_needed() -> None:
    """A file that no longer names an app must lose its allowance, so the list stays honest."""
    stale = [
        f"{rel}: {sorted(allowed - _named_apps(_REPO / rel))}"
        for rel, allowed in _MAY_NAME.items()
        if allowed - _named_apps(_REPO / rel)
    ]

    assert stale == [], "\n".join(stale)


# --- what a Teams session is given ----------------------------------------------------

_DEFAULTS = _REPO / "defaults"
_OTHER_APPS_FOR_TEAMS = re.compile(r"discord|slack", re.IGNORECASE)


def test_the_skills_a_teams_agent_is_seeded_with_name_no_other_chat_platform() -> None:
    """Every custom skill on the default agent that a Teams turn can load, whether it is
    on the agent for everyone or added on Teams alone."""
    spec = yaml.safe_load((_DEFAULTS / "agents" / "daimon.yaml").read_text())
    offences: list[str] = []
    checked = 0
    for ref in spec["skills"]:
        if ref["type"] != "custom" or ref.get("platforms") not in (None, ["teams"]):
            continue
        for path in sorted((_DEFAULTS / "skills" / ref["skill_id"]).rglob("*")):
            if not path.is_file():
                continue
            checked += 1
            if match := _OTHER_APPS_FOR_TEAMS.search(path.read_text(errors="ignore")):
                offences.append(f"{path.relative_to(_REPO)}: {match.group(0)}")

    assert checked > 10, "the seeded skills were not found"
    assert offences == []


@pytest.mark.parametrize("platform", _APPS)
def test_each_platforms_system_prompt_names_no_other_chat_platform(platform: ChatPlatform) -> None:
    from daimon.core.agent_guidance import apply_credential_guidance
    from daimon.core.platform_system import system_for_platform

    spec = yaml.safe_load((_DEFAULTS / "agents" / "daimon.yaml").read_text())
    variant = system_for_platform(apply_credential_guidance(spec["system"]), platform)

    assert variant is not None
    _assert_only_own(platform, variant)
