"""Slack's model choice for one thread: a message shortcut, a select, one Confirm.

Slash commands carry no thread, so the thread level is a shortcut on a message in the
thread. The select lists the agent's catalog with prices; a pick that changes provider shows
what changes and asks once. The same card is posted in the thread when an agent edit leaves
the thread's consent stale (`daimon.core.thread_model_reask`). The shared flow is
`daimon.core.thread_model_flow`; the person, channel and thread come from Slack's signed
envelope, and every click is checked again.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any, Final, cast

import structlog
from daimon.adapters.slack.admin import resolve_is_admin
from daimon.adapters.slack.blockkit import notice_attachments, notice_fields
from daimon.adapters.slack.interactions import resolve_web_client
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.admin_actions import DelegatedAction
from daimon.core.errors import DaimonError
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.thread_model_flow import (
    NOTHING_CHANGED,
    ThreadConfirm,
    ThreadOption,
    build_thread_pick,
    confirm_thread_pick,
    pick_thread_model,
    thread_options,
)
from daimon.core.thread_model_reask import ThreadReask, ThreadReaskNotifier
from daimon.core.thread_models import may_choose_thread_model
from daimon.core.turn.deps import TurnDeps
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.webhook.async_client import AsyncWebhookClient
from sqlalchemy.exc import SQLAlchemyError

log = structlog.get_logger()

SHORTCUT_CALLBACK_ID: Final = "thread_model"
PICK_ACTION_ID: Final = "thread_model_pick"
CONFIRM_ACTION_ID: Final = "thread_model_confirm"
CANCEL_ACTION_ID: Final = "thread_model_cancel"
THREAD_MODEL_ACTION_IDS: Final = frozenset({PICK_ACTION_ID, CONFIRM_ACTION_ID, CANCEL_ACTION_ID})

_BLOCK_PREFIX: Final = "thread_model:"
_TITLE: Final = "Model for this conversation"
_PICK_PROMPT: Final = "Pick a model."
_PICK_PLACEHOLDER: Final = "Model"
_NOT_ALLOWED: Final = "You can't change the model here. Ask an admin or an owner."
_NOT_IN_THREAD: Final = "Open this on a message in a conversation with Daimon."
_FAILED: Final = "That didn't work. Try again."
_CONFIRM_PROMPT: Final = "Confirm to switch this conversation."


def _text(text: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text[:3000]}}


def _select_blocks(options: tuple[ThreadOption, ...], conversation_id: str) -> list[dict[str, Any]]:
    select: dict[str, Any] = {
        "type": "static_select",
        "action_id": PICK_ACTION_ID,
        "placeholder": {"type": "plain_text", "text": _PICK_PLACEHOLDER},
        "options": [
            {"text": {"type": "plain_text", "text": o.label[:75]}, "value": o.value[:75]}
            for o in options
        ],
    }
    return [
        _text(f"*{_TITLE}*\n{_PICK_PROMPT}"),
        {
            "type": "actions",
            "block_id": f"{_BLOCK_PREFIX}{conversation_id}"[:255],
            "elements": [select],
        },
    ]


def _confirm_value(confirm: ThreadConfirm, conversation_id: str) -> str:
    return json.dumps(
        {"v": confirm.value, "d": confirm.digest, "r": confirm.revision, "c": conversation_id},
        separators=(",", ":"),
    )


def _read_confirm(raw: str) -> tuple[ThreadConfirm, str] | None:
    try:
        data = cast("dict[str, Any]", json.loads(raw))
        return ThreadConfirm(str(data["v"]), str(data["d"]), int(data["r"])), str(data["c"])
    except (ValueError, KeyError, TypeError):
        return None


def confirm_blocks(
    lines: tuple[str, ...], confirm: ThreadConfirm, conversation_id: str
) -> list[dict[str, Any]]:
    """What a pick changes, with Confirm and Cancel."""
    buttons: list[dict[str, Any]] = [
        {
            "type": "button",
            "action_id": CONFIRM_ACTION_ID,
            "style": "primary",
            "text": {"type": "plain_text", "text": "Confirm"},
            "value": _confirm_value(confirm, conversation_id),
        },
        {
            "type": "button",
            "action_id": CANCEL_ACTION_ID,
            "text": {"type": "plain_text", "text": "Cancel"},
            "value": "cancel",
        },
    ]
    return [
        _text("\n".join((f"*{_TITLE}*", *lines, _CONFIRM_PROMPT))),
        {"type": "actions", "block_id": f"{_BLOCK_PREFIX}confirm", "elements": buttons},
    ]


def _ids(payload: dict[str, Any]) -> tuple[str, str, str]:
    """Workspace, person and channel, from the signed envelope."""
    team, user, channel = (
        str(cast("dict[str, Any]", payload.get(part) or {}).get("id") or "")
        for part in ("team", "user", "channel")
    )
    return team, user, channel


async def _allowed(
    runtime: SlackRuntime,
    client: AsyncWebClient,
    *,
    team_id: str,
    user_id: str,
    channel_id: str,
    conversation_id: str,
) -> bool:
    admin = await resolve_is_admin(
        client, user_id=user_id, host_team_id=team_id, action=DelegatedAction.MODEL
    )
    return await may_choose_thread_model(
        runtime.sessionmaker,
        runtime.turn_deps.membership_resolver,
        tenant_id=derive_tenant_uuid(platform="slack", workspace_id=team_id),
        platform="slack",
        user_id=user_id,
        is_admin=admin,
        channel_id=channel_id,
        thread_id=conversation_id,
    )


async def _agent_admin(client: AsyncWebClient, *, team_id: str, user_id: str) -> bool:
    return await resolve_is_admin(
        client, user_id=user_id, host_team_id=team_id, action=DelegatedAction.AGENT_SETTINGS
    )


async def _answer(
    response_url: str, text: str, *, blocks: list[dict[str, Any]] | None = None
) -> None:
    """Replace the card with one line, or with new blocks."""
    fields: dict[str, Any] = {"replace_original": True, "parse": "none"}
    if blocks is not None:
        fields |= {"text": _TITLE, "blocks": blocks}
    else:
        fields |= {"text": "", "blocks": [], "attachments": notice_attachments(text, neutral=True)}
    await AsyncWebhookClient(response_url).send_dict(fields)


async def handle_thread_model_shortcut(runtime: SlackRuntime, payload: dict[str, Any]) -> None:
    """The shortcut on a message: the select, shown to its caller only in that thread."""
    team_id, user_id, channel_id = _ids(payload)
    message = cast("dict[str, Any]", payload.get("message") or {})
    thread_ts = str(message.get("thread_ts") or message.get("ts") or "")
    client = await resolve_web_client(runtime, team_id=team_id)
    if client is None or not (user_id and channel_id and thread_ts):
        return
    try:
        if not await _allowed(
            runtime,
            client,
            team_id=team_id,
            user_id=user_id,
            channel_id=channel_id,
            conversation_id=thread_ts,
        ):
            fields: dict[str, Any] = notice_fields(_NOT_ALLOWED, neutral=True)
        else:
            pick = await build_thread_pick(
                turn_deps=runtime.turn_deps,
                sessionmaker=runtime.sessionmaker,
                anthropic=runtime.anthropic,
                settings=runtime.settings,
                default=runtime.deployment_default,
                tenant_id=derive_tenant_uuid(platform="slack", workspace_id=team_id),
                platform="slack",
                user_id=user_id,
                admin=await _agent_admin(client, team_id=team_id, user_id=user_id),
                channel_id=channel_id,
                conversation_id=thread_ts,
            )
            options = await thread_options(runtime.settings, pick)
            fields = {"text": _TITLE, "blocks": _select_blocks(options, thread_ts)}
        await client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
            channel=channel_id, user=user_id, thread_ts=thread_ts, parse="none", **fields
        )
    except (SlackApiError, DaimonError, SQLAlchemyError) as exc:
        log.warning("slack.thread_model.shortcut_failed", team_id=team_id, exc_info=exc)


async def handle_thread_model_pick(runtime: SlackRuntime, payload: dict[str, Any]) -> None:
    """A pick from the select: kept when it needs no consent, else previewed for one Confirm."""
    team_id, user_id, channel_id = _ids(payload)
    element = cast("list[dict[str, Any]]", payload.get("actions") or [{}])[0]
    choice = str(cast("dict[str, Any]", element.get("selected_option") or {}).get("value") or "")
    block_id = str(element.get("block_id") or "")
    conversation_id = (
        block_id.removeprefix(_BLOCK_PREFIX) if block_id.startswith(_BLOCK_PREFIX) else ""
    )
    response_url = str(payload.get("response_url") or "")
    if not (team_id and user_id and channel_id and choice and conversation_id and response_url):
        return
    client = await resolve_web_client(runtime, team_id=team_id)
    if client is None:
        return
    try:
        if not await _allowed(
            runtime,
            client,
            team_id=team_id,
            user_id=user_id,
            channel_id=channel_id,
            conversation_id=conversation_id,
        ):
            await _answer(response_url, _NOT_ALLOWED)
            return
        admin = await _agent_admin(client, team_id=team_id, user_id=user_id)
        tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
        pick = await build_thread_pick(
            turn_deps=runtime.turn_deps,
            sessionmaker=runtime.sessionmaker,
            anthropic=runtime.anthropic,
            settings=runtime.settings,
            default=runtime.deployment_default,
            tenant_id=tenant_id,
            platform="slack",
            user_id=user_id,
            admin=admin,
            channel_id=channel_id,
            conversation_id=conversation_id,
        )
        result = await pick_thread_model(
            sessionmaker=runtime.sessionmaker,
            settings=runtime.settings,
            pick=pick,
            tenant_id=tenant_id,
            platform="slack",
            conversation_id=conversation_id,
            choice=choice,
            admin=admin,
        )
        if result.confirm is None:
            shown = (*result.lines, NOTHING_CHANGED) if result.lines else (result.text,)
            await _answer(response_url, "\n".join(shown))
            return
        await _answer(
            response_url,
            "",
            blocks=confirm_blocks(result.lines, result.confirm, conversation_id),
        )
    except (SlackApiError, DaimonError, SQLAlchemyError) as exc:
        log.warning("slack.thread_model.pick_failed", team_id=team_id, exc_info=exc)
        await _answer(response_url, _FAILED)


async def handle_thread_model_confirm(runtime: SlackRuntime, payload: dict[str, Any]) -> None:
    """Confirm: the pick is kept only if it still matches what was shown."""
    team_id, user_id, channel_id = _ids(payload)
    element = cast("list[dict[str, Any]]", payload.get("actions") or [{}])[0]
    read = _read_confirm(str(element.get("value") or ""))
    response_url = str(payload.get("response_url") or "")
    if read is None or not (team_id and user_id and channel_id and response_url):
        return
    confirm, conversation_id = read
    client = await resolve_web_client(runtime, team_id=team_id)
    if client is None:
        return
    try:
        if not await _allowed(
            runtime,
            client,
            team_id=team_id,
            user_id=user_id,
            channel_id=channel_id,
            conversation_id=conversation_id,
        ):
            await _answer(response_url, f"{_NOT_ALLOWED} {NOTHING_CHANGED}")
            return
        admin = await _agent_admin(client, team_id=team_id, user_id=user_id)
        pick = await build_thread_pick(
            turn_deps=runtime.turn_deps,
            sessionmaker=runtime.sessionmaker,
            anthropic=runtime.anthropic,
            settings=runtime.settings,
            default=runtime.deployment_default,
            tenant_id=derive_tenant_uuid(platform="slack", workspace_id=team_id),
            platform="slack",
            user_id=user_id,
            admin=admin,
            channel_id=channel_id,
            conversation_id=conversation_id,
        )
        if pick is None:
            await _answer(response_url, f"No agent answers here yet. {NOTHING_CHANGED}")
            return
        text = await confirm_thread_pick(
            pick, confirm, conversation_id=conversation_id, admin=admin
        )
        await _answer(response_url, text)
    except (SlackApiError, DaimonError, SQLAlchemyError) as exc:
        log.warning("slack.thread_model.confirm_failed", team_id=team_id, exc_info=exc)
        await _answer(response_url, f"{_FAILED} {NOTHING_CHANGED}")


async def handle_thread_model_cancel(runtime: SlackRuntime, payload: dict[str, Any]) -> None:
    """Cancel: the card becomes one line."""
    response_url = str(payload.get("response_url") or "")
    if response_url:
        await _answer(response_url, NOTHING_CHANGED)


def slack_thread_reask_notifier(runtime: SlackRuntime) -> ThreadReaskNotifier:
    """Post the preview card in the thread; a failed post raises so the next message asks again."""

    async def send(ask: ThreadReask) -> None:
        client = await resolve_web_client(runtime, team_id=ask.workspace_id)
        if client is None:
            raise RuntimeError("no slack client for the workspace")
        if ask.confirm is None:
            blocks = [_text("\n".join((f"*{_TITLE}*", *ask.lines)))]
        else:
            blocks = confirm_blocks(ask.lines, ask.confirm, ask.conversation_id)
        fields: dict[str, Any] = notice_fields(_TITLE, neutral=True, blocks=blocks)
        if ask.thread_id:
            fields["thread_ts"] = ask.thread_id
        await client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]
            channel=ask.channel_id, text=_TITLE, parse="none", **fields
        )

    return send


def with_thread_reask_notifier(runtime: SlackRuntime) -> SlackRuntime:
    """`runtime` whose turns post the card; a stand-in test runtime comes back unchanged."""
    real = cast(object, runtime)
    deps = cast(object, runtime.turn_deps)
    if not isinstance(real, SlackRuntime) or not isinstance(deps, TurnDeps):
        return runtime
    return replace(
        real,
        turn_deps=replace(deps, thread_reask_notifier=slack_thread_reask_notifier(real)),
    )
