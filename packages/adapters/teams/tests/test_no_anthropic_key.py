"""A release that holds no Anthropic key never sends a Teams message to Anthropic.

Such a release answers only where registration granted the selfhosted model. A
message anywhere else gets one refusal, and the operator one
`selfhosted.grant_missing` line with the IDs to grant. `setup` and `memory`,
which read their agents from Anthropic, say they are unavailable; every other
command answers as it does with a key. Each case runs the real HTTP service
with an Anthropic client that records any use.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import uuid
from collections import Counter
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, NoReturn, cast
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import structlog
from anthropic import AsyncAnthropic
from daimon.adapters.teams.app import TeamsApp
from daimon.adapters.teams.commands import NOT_ON_THIS_INSTALL
from daimon.adapters.teams.http_service import TeamsHttpService
from daimon.adapters.teams.privacy_card import CONFIRM_INPUT
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.core.access_policy import ChannelRule, TenantAccessPolicy
from daimon.core.config import SelfhostedBillingSettings, SupportSettings
from daimon.core.features.registry import FEATURES
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.settings_panel import open_op
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.identity import find_platform_principal, get_or_create_platform_principal
from daimon.core.stores.routines import create_routine, get_routine
from daimon.core.stores.selfhosted_resources import SelfHostedAgents, SelfHostedEnvironments
from daimon.core.stores.tenants import set_funding_mode
from daimon.core.turn.admission import AdmissionDenied, admit
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ModelRef, Scope
from mux.contracts.resources import AgentSpec, EnvironmentSpec
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    AAD_OBJECT_ID,
    CHANNEL_ID,
    CONVERSATION_ID,
    ENTRA_TENANT_ID,
    GROUP_CHAT_ID,
    THREAD_ID,
    USER_NAME,
    SentRequest,
    TeamsApiFake,
    build_teams_runtime,
    make_card_action,
    make_channel_activity,
    make_group_activity,
    make_invoke,
    make_message_activity,
    notice_text,
    patched_admission,
    post_activity,
    running_service,
    settle,
    teams_settings,
)

pytestmark = pytest.mark.usefixtures("entra_env", "stub_bot_token", "provisioned_tenant")

TENANT = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID)
# Admission's words for a conversation its model provider cannot answer.
REFUSAL = (
    "This agent's model provider isn't available here right now.\n\n"
    "Ask an admin to check which provider this agent uses."
)
Activity = Callable[..., dict[str, object]]
# How a message reaches Daimon in each kind of conversation, where the answer
# goes, and the conversation a grant names.
PLACES: dict[str, tuple[Activity, str, str]] = {
    "1:1 chat": (make_message_activity, CONVERSATION_ID, CONVERSATION_ID),
    "group chat": (make_group_activity, GROUP_CHAT_ID, GROUP_CHAT_ID),
    "channel": (make_channel_activity, THREAD_ID, CHANNEL_ID),
}


class NoAnthropic:
    """An Anthropic client that records every use: a release with no key makes none."""

    def __init__(self) -> None:
        self.used: list[str] = []

    def __getattr__(self, name: str) -> object:
        self.used.append(name)
        raise AssertionError(f"Anthropic.{name} used")


def keyless(
    db_factory: async_sessionmaker[AsyncSession],
    client: NoAnthropic,
    admins: tuple[str, ...] = (),
) -> TeamsRuntime:
    """The Teams host of a release that holds no Anthropic key."""
    runtime = build_teams_runtime(db_factory, anthropic=cast(AsyncAnthropic, client), admins=admins)
    runtime.settings.anthropic.api_key = None
    return runtime


def notice(request: SentRequest) -> str:
    """The words of one notice card sent: its one text block, in a red box for an error."""
    attachments = cast(list[dict[str, Any]], request.body["attachments"])
    [block] = attachments[0]["content"]["body"]
    if block["type"] == "Container":
        [block] = block["items"]
    return block["text"]


@pytest.mark.parametrize("place", ["1:1 chat", "channel"])
async def test_no_command_calls_anthropic_and_setup_and_memory_say_they_are_unavailable(
    db_session_factory: async_sessionmaker[AsyncSession],
    teams_api_fake: TeamsApiFake,
    place: str,
) -> None:
    client = NoAnthropic()
    runtime = keyless(db_session_factory, client)
    # Support on, so every command Teams can register is tried.
    runtime.settings.support = SupportSettings(teams_escalation_channel_id="19:ops@thread.tacv2")
    write = PLACES[place][0]
    answers: dict[str, list[SentRequest]] = {}
    with structlog.testing.capture_logs() as logs:
        async with running_service(runtime, teams_api_fake) as service:
            registered = service.turns._private._commands  # pyright: ignore[reportPrivateUsage]
            for name in registered:
                before = len(teams_api_fake.activity_requests)
                await post_activity(service, write(text=name, activity_id=f"{place}-{name}"))
                await settle(service.turns)
                answers[name] = teams_api_fake.activity_requests[before:]

    assert all(answers.values()), "every command answers"
    for name in ("setup", "memory"):
        [answer] = answers[name]
        assert notice(answer) == NOT_ON_THIS_INSTALL.format(what=f"`{name}`")
        if place == "channel":
            assert "isTargetedActivity=true" in answer.url, "for the sender, where it was typed"
    unavailable = Counter(
        log["what"] for log in logs if log["event"] == "teams.command.unavailable"
    )
    assert unavailable == {"`setup`": 1, "`memory`": 1}, "no other command is refused"
    assert client.used == []


@pytest.mark.parametrize("place", PLACES)
async def test_an_unlisted_conversation_gets_one_refusal_and_one_grant_missing_line(
    db_session_factory: async_sessionmaker[AsyncSession],
    teams_api_fake: TeamsApiFake,
    place: str,
) -> None:
    write, conversation, granted = PLACES[place]
    client = NoAnthropic()
    with structlog.testing.capture_logs() as logs:
        async with running_service(keyless(db_session_factory, client), teams_api_fake) as service:
            await post_activity(service, write(activity_id="ask-1"))
            await settle(service.turns)
    async with db_session_factory() as session:
        principal = await find_platform_principal(
            session, tenant_id=TENANT, platform="teams", external_id=AAD_OBJECT_ID
        )

    assert principal is not None
    assert [(httpx.URL(r.url).path, notice(r)) for r in teams_api_fake.activity_requests] == [
        (f"/test/v3/conversations/{conversation}/activities", REFUSAL)
    ]
    missing = [log for log in logs if log["event"] == "selfhosted.grant_missing"]
    assert [(log["platform"], log["channel_id"], log["account_id"]) for log in missing] == [
        ("teams", granted, str(principal.account_id))
    ]
    assert client.used == []


async def test_a_redelivered_message_is_refused_once(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    client = NoAnthropic()
    with structlog.testing.capture_logs() as logs:
        for _ in range(2):  # Bot Framework's retry reaches a host restarted meanwhile.
            runtime = keyless(db_session_factory, client)
            async with running_service(runtime, teams_api_fake) as service:
                await post_activity(service, make_message_activity(activity_id="again-1"))
                await settle(service.turns)

    assert [notice(r) for r in teams_api_fake.activity_requests] == [REFUSAL]
    assert [log["event"] for log in logs].count("selfhosted.grant_missing") == 1
    assert client.used == []


CHART = Path(__file__).resolve().parents[4] / "deploy/helm/daimon-selfhost"
MODEL = "gpt-5.4-mini"


def chart_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"chart_{name}", CHART / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextmanager
def selfhosted_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Selfhosted admission on, and the settings the selfhosted runtime reads."""
    monkeypatch.setenv("DAIMON_SELFHOSTED__ADMISSION_ENABLED", "true")
    tariff = [{"effective_from": "2026-01-01", "input": "1", "output": "4", "cache_read": "0.1"}]
    billing = SelfhostedBillingSettings.model_validate(
        {
            "allow_test_tariffs": True,
            "deployments": {MODEL: f"{MODEL}-TEST"},
            "models": {f"{MODEL}-TEST": tariff},
        }
    )
    settings = SimpleNamespace(teams=teams_settings(), billing=SimpleNamespace(selfhosted=billing))
    with patch("daimon.core.config.load_settings", lambda: settings):
        yield


async def register(
    sf: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    grants: dict[str, list[str]],
) -> None:
    """`register` with `TEAMS_GRANTS`, and the runtime grants it writes mounted."""
    environ = {
        "SELFHOSTED_MODEL": MODEL,
        "DAIMON_TEAMS__TENANT_ID": ENTRA_TENANT_ID,
        "TEAMS_GRANTS": json.dumps(grants),
    }
    registration = await chart_script("provision").provision(sf, environ)
    # What `daimon selfhosted init` creates for the Teams tenant.
    async with sf.begin() as session:
        operator = await get_or_create_platform_principal(
            session, tenant_id=TENANT, platform="teams", external_id="operator"
        )
        await set_funding_mode(session, tenant_id=TENANT, funding_mode="operator_funded")
    scope = Scope(
        tenant_id=str(TENANT),
        account_id=str(operator.account_id),
        principal_id="cli",
        authorization_id="init",
    )
    agent = await SelfHostedAgents(sf).create(
        scope, AgentSpec(name="daimon", model=ModelRef(provider="selfhosted", id=MODEL)), key="a"
    )
    harness = {"harness_url": "https://harness.example", "auth_mode": "bearer_env", "auth_ref": "X"}
    environment = await SelfHostedEnvironments(sf).create(
        scope,
        EnvironmentSpec(
            name="default",
            execution="self_hosted",
            native_config=ExtensionConfig(
                namespace="selfhosted.environment", version=1, value=harness
            ),
        ),
        key="e",
    )
    ids = {
        registration["tenant_id"]: ["sh_agent_cli", "sh_env_cli"],
        str(TENANT): [agent.ref.id, environment.ref.id],
    }
    mounted = tmp_path / "runtime.json"
    mounted.write_text(json.dumps(chart_script("runtime_grants").rows(registration, ids)))
    monkeypatch.setenv("DAIMON_TURN__PROVIDER_RUNTIME_FILE", str(mounted))


@asynccontextmanager
async def release(
    sf: async_sessionmaker[AsyncSession],
    fake: TeamsApiFake,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    grants: dict[str, list[str]],
) -> AsyncIterator[TeamsHttpService]:
    """The Teams host of a release whose `register` granted `grants`."""
    with selfhosted_settings(monkeypatch):
        await register(sf, tmp_path, monkeypatch, grants)
        runtime = build_teams_runtime(sf)
        deps = dataclasses.replace(runtime.turn_deps, channel_backends=True)
        async with running_service(dataclasses.replace(runtime, turn_deps=deps), fake) as host:
            yield host


async def outcome(host: TeamsHttpService, payload: dict[str, object]) -> str:
    """Post `payload` and stop once admission decides: served, or refused and why."""
    decided: list[str] = []

    async def admit_then_stop(*args: Any, **kwargs: Any) -> NoReturn:
        try:
            await admit(*args, **kwargs)
        except AdmissionDenied as refused:
            decided.append(f"refused: {refused.reason}")
            raise
        decided.append("served")
        raise AdmissionDenied(reason="backend_unsupported")  # stop before any card

    # A conversation no grant names runs on Anthropic, whose agent is faked.
    with patch("daimon.adapters.teams.app.admit", admit_then_stop), patched_admission():
        await post_activity(host, payload)
        await settle(host.turns)
    [decision] = decided
    return decision


@pytest.mark.parametrize(
    ("key", "unlisted"), [("held", "served"), ("absent", "refused: backend_unsupported")]
)
async def test_the_key_changes_only_what_an_unlisted_conversation_gets(
    db_session_factory: async_sessionmaker[AsyncSession],
    teams_api_fake: TeamsApiFake,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    key: str,
    unlisted: str,
) -> None:
    listed = "19:listed@thread.v2"
    grants = {listed: [AAD_OBJECT_ID]}
    async with release(db_session_factory, teams_api_fake, tmp_path, monkeypatch, grants) as host:
        if key == "absent":
            host.runtime.settings.anthropic.api_key = None
        outcomes = {
            "listed": await outcome(
                host, make_group_activity(conversation_id=listed, activity_id="listed-1")
            ),
            "unlisted": await outcome(
                host,
                make_group_activity(conversation_id="19:other@thread.v2", activity_id="other-1"),
            ),
        }

    assert outcomes == {"listed": "served", "unlisted": unlisted}


@pytest.mark.parametrize(("key", "judged"), [("held", 1), ("absent", 0)])
async def test_an_unaddressed_reply_is_judged_only_with_an_anthropic_key(
    db_session_factory: async_sessionmaker[AsyncSession],
    teams_api_fake: TeamsApiFake,
    key: str,
    judged: int,
) -> None:
    """Participation's classifier is an Anthropic model, so without a key nothing is judged."""
    runtime = build_teams_runtime(db_session_factory)
    if key == "absent":
        runtime.settings.anthropic.api_key = None
    reply = make_channel_activity(text="thanks all", mention_bot=False, activity_id="reply-1")
    with patch.object(TeamsApp, "_observe", new_callable=AsyncMock) as observe:
        async with running_service(runtime, teams_api_fake) as service:
            await post_activity(service, reply)
            await settle(service.turns)

    assert observe.await_count == judged


def said(response: dict[str, Any]) -> str:
    """The one line an invoke answers with: a toast, a dialog's notice or its message."""
    if "task" not in response:
        return response["value"]
    if response["task"]["type"] == "message":
        return response["task"]["value"]
    return notice_text(response)


def dialog(kind: str, data: dict[str, object]) -> dict[str, object]:
    """A dialog opened (`fetch`) or sent (`submit`) from the 1:1 chat."""
    return make_invoke(f"task/{kind}", {"data": data})


# What a person presses that reads agents from Anthropic, on an old card or a panel a
# command shows, and what its notice names.
READS_ANTHROPIC: dict[str, tuple[dict[str, object], str]] = {
    "a setup card's button": (
        make_card_action("agent_setup", "details", agent="daimon"),
        "`setup`",
    ),
    "New agent": (dialog("fetch", {"dialog_id": "agent_create"}), "`setup`"),
    "New agent, sent": (dialog("submit", {"action": "agent_create", "name": "scout"}), "`setup`"),
    "Use from your coding tools": (
        dialog("fetch", {"dialog_id": "agent_coding_tools", "agent": "daimon"}),
        "`setup`",
    ),
    "a coding key, minted": (
        dialog("submit", {"action": "agent_coding_tools", "op": "mint", "agent": "daimon"}),
        "`setup`",
    ),
    "Add a skill": (
        dialog("fetch", {"dialog_id": "agent_add_skill", "agent": "daimon"}),
        "`setup`",
    ),
    "Add a skill, sent": (
        dialog("submit", {"action": "agent_add_skill", "agent": "daimon"}),
        "`setup`",
    ),
    "Channel settings": (dialog("fetch", {"dialog_id": "channel_settings"}), "`setup`"),
    "Channel settings, sent": (dialog("submit", {"action": "channel_settings"}), "`setup`"),
    "New routine": (dialog("fetch", {"dialog_id": "routine_create"}), "Creating a routine"),
    "New routine, sent": (dialog("submit", {"action": "routine_create"}), "Creating a routine"),
    "Switch to an agent": (
        make_card_action("thread_hand_over", "hand_over", agent="agent_1", offer="Hand over?"),
        "Switching agents",
    ),
    "a credential request": (
        dialog("fetch", {"dialog_id": "credential_request", "token": "t"}),
        "Answering credential requests",
    ),
    "a credential request, sent": (
        dialog("submit", {"action": "credential_submit", "token": "t"}),
        "Answering credential requests",
    ),
}
# Setup buttons that read no agent keep working: ending a setup conversation,
# revoking a coding key and operator tokens.
READS_NO_AGENT: dict[str, dict[str, object]] = {
    "End setup": make_card_action("agent_setup", "end", thread=f"{CONVERSATION_ID};setup=1"),
    "a coding key, revoked": dialog(
        "submit", {"action": "agent_coding_tools", "op": "revoke", "jti": str(uuid.uuid4())}
    ),
    "Operator tokens": dialog("fetch", {"dialog_id": "operator_tokens"}),
}


@pytest.mark.parametrize("pressed", READS_ANTHROPIC)
async def test_a_button_that_reads_agents_from_anthropic_answers_with_one_notice(
    db_session_factory: async_sessionmaker[AsyncSession],
    teams_api_fake: TeamsApiFake,
    pressed: str,
) -> None:
    payload, what = READS_ANTHROPIC[pressed]
    client = NoAnthropic()
    with structlog.testing.capture_logs() as logs:
        async with running_service(keyless(db_session_factory, client), teams_api_fake) as service:
            response = await post_activity(service, payload)

    assert said(response) == NOT_ON_THIS_INSTALL.format(what=what)
    assert [log["what"] for log in logs if log["event"] == "teams.command.unavailable"] == [what]
    assert client.used == []


@pytest.mark.parametrize("pressed", READS_NO_AGENT)
async def test_a_setup_button_that_reads_no_agent_keeps_working(
    db_session_factory: async_sessionmaker[AsyncSession],
    teams_api_fake: TeamsApiFake,
    pressed: str,
) -> None:
    client = NoAnthropic()
    with structlog.testing.capture_logs() as logs:
        async with running_service(keyless(db_session_factory, client), teams_api_fake) as service:
            response = await post_activity(service, READS_NO_AGENT[pressed])

    assert "isn't available on this Daimon install" not in json.dumps(response)
    assert [log for log in logs if log["event"] == "teams.command.unavailable"] == []
    assert client.used == []


async def test_help_offers_no_command_that_needs_anthropic(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    client = NoAnthropic()
    async with running_service(keyless(db_session_factory, client), teams_api_fake) as service:
        await post_activity(service, make_message_activity(text="help", activity_id="help-1"))
        await settle(service.turns)

    [card] = teams_api_fake.activity_requests
    listed = [
        fact["title"]
        for block in card.body["attachments"][0]["content"]["body"]
        for fact in block.get("facts", [])
    ]
    assert "help" in listed and "setup" not in listed and "memory" not in listed
    assert client.used == []


async def test_settings_offers_no_agent_without_an_anthropic_key(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    """An admin's feature screen leaves out the agent picker, which Anthropic would list."""
    client = NoAnthropic()
    runtime = keyless(db_session_factory, client, admins=(AAD_OBJECT_ID,))
    press = make_card_action(
        "settings", open_op("chat", "progress_plans"), channel=CHANNEL_ID, thread=""
    )
    async with running_service(runtime, teams_api_fake) as service:
        screen = json.dumps(await post_activity(service, press))

    assert FEATURES["progress_plans"].name in screen
    assert "for an agent" not in screen
    assert client.used == []


async def test_deleting_your_data_reaches_no_anthropic_without_its_key(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    async with db_session_factory.begin() as session:
        principal = await get_or_create_platform_principal(
            session, tenant_id=TENANT, platform="teams", external_id=AAD_OBJECT_ID
        )
    confirm = make_card_action(
        "privacy",
        "confirm_delete",
        account=str(principal.account_id),
        **{CONFIRM_INPUT: USER_NAME},
    )
    client = NoAnthropic()
    async with running_service(keyless(db_session_factory, client), teams_api_fake) as service:
        await post_activity(service, confirm)
        await service.turns.drain(timeout=10)  # The purge is a tracked task.
    async with db_session_factory() as session:
        gone = await find_platform_principal(
            session, tenant_id=TENANT, platform="teams", external_id=AAD_OBJECT_ID
        )

    [edit] = [r for r in teams_api_fake.activity_requests if r.method == "PUT"]
    assert "Account deleted" in json.dumps(edit.body)
    assert gone is None
    assert client.used == []


async def test_deleting_a_routine_reaches_no_anthropic_without_its_key(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    """A channel kept to its own agents makes the delete read the agents' names."""
    async with db_session_factory.begin() as session:
        await set_access_policy(
            session,
            tenant_id=TENANT,
            policy=TenantAccessPolicy(
                channel_rules={CHANNEL_ID: ChannelRule(readers="own", writers="own")}
            ),
        )
        routine = await create_routine(
            session,
            tenant_id=TENANT,
            created_by_user_id=AAD_OBJECT_ID,
            agent_id="agent_1",
            agent_name="daimon",
            cron_expr="0 9 * * *",
            timezone_="UTC",
            trigger_message="digest",
            next_fire_at=datetime(2030, 1, 1, tzinfo=UTC),
        )
    client = NoAnthropic()
    delete = make_card_action("routines", "confirm_delete", routine=str(routine.id))
    async with running_service(keyless(db_session_factory, client), teams_api_fake) as service:
        done = json.dumps(await post_activity(service, delete))
    async with db_session_factory() as session:
        gone = await get_routine(session, routine.id, tenant_id=TENANT)

    assert "Routine deleted" in done
    assert gone is None
    assert client.used == []
