"""Thin clients for the two real platform validators.

Slack: `POST https://slack.com/api/blocks.validate`, a schema check that needs only a
bot token. It answers HTTP 200 with `error: ratelimited` (and no `Retry-After`) when
called faster than about once a second, so calls are paced and retried with backoff.

Discord has no validate endpoint. The check is to POST the message body to a scratch
channel as the bot and delete the message at once. `allowed_mentions.parse` is empty, so
nothing pings anyone. Discord allows 5 messages per second per channel; the client reads
the bucket headers and waits when it is spent.

Tokens come from the caller; this module never prints or logs them.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .registry import Built

SLACK_VALIDATE_URL = "https://slack.com/api/blocks.validate"
DISCORD_API = "https://discord.com/api/v10"
DISCORD_USER_AGENT = "DiscordBot (https://github.com/pymc-labs/daimon, platform-contract)"

SLACK_MIN_SECONDS_BETWEEN_CALLS = 1.0
SLACK_RATE_LIMIT_BACKOFF_SECONDS = (2.0, 4.0, 8.0, 16.0, 32.0)
DISCORD_MIN_SECONDS_BETWEEN_CALLS = 0.3
DISCORD_MAX_RATE_LIMIT_RETRIES = 5


@dataclass(frozen=True)
class Verdict:
    """What a platform said about one payload."""

    accepted: bool
    error: str = ""

    def __str__(self) -> str:
        return "accepted" if self.accepted else f"rejected: {self.error}"


@dataclass
class _Pacer:
    """Keeps a minimum gap between calls to one API."""

    min_seconds: float
    _last: float = field(default=0.0, init=False)

    def wait(self) -> None:
        pause = self._last + self.min_seconds - time.monotonic()
        if pause > 0:
            time.sleep(pause)
        self._last = time.monotonic()


def _slack_parameter(built: Built) -> dict[str, Any]:
    """The `blocks.validate` argument for a payload: `view`, `blocks` or `message`."""
    return {built.kind: built.payload}


def _slack_error(response: dict[str, Any]) -> str:
    """The error code plus each pointer and message the validator reported."""
    details: list[str] = []
    errors: list[Any] = response.get("errors") or []
    for item in errors:
        if isinstance(item, dict) and item.get("pointer"):  # pyright: ignore[reportUnknownMemberType]
            details.append(f"{item['pointer']}: {item.get('message', '')}")  # pyright: ignore[reportUnknownMemberType]
    return f"{response.get('error', 'unknown')} {'; '.join(details)}".strip()


class SlackValidator:
    def __init__(self, token: str) -> None:
        self._client = httpx.Client(headers={"Authorization": f"Bearer {token}"}, timeout=30.0)
        self._pacer = _Pacer(SLACK_MIN_SECONDS_BETWEEN_CALLS)

    def validate(self, built: Built) -> Verdict:
        response = self._post(_slack_parameter(built))
        if response.get("ok"):
            return Verdict(accepted=True)
        return Verdict(accepted=False, error=_slack_error(response))

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        response: dict[str, Any] = {}
        for backoff in (*SLACK_RATE_LIMIT_BACKOFF_SECONDS, None):
            self._pacer.wait()
            http = self._client.post(SLACK_VALIDATE_URL, json=body)
            http.raise_for_status()
            response = http.json()
            if response.get("error") != "ratelimited":
                return response
            if backoff is not None:
                time.sleep(backoff)
        return response

    def close(self) -> None:
        self._client.close()


class DiscordScratchChannel:
    """Posts bodies to one scratch channel as the QA bot and deletes what it posted."""

    def __init__(self, token: str, channel_id: str) -> None:
        self._channel = channel_id
        self._client = httpx.Client(
            headers={"Authorization": f"Bot {token}", "User-Agent": DISCORD_USER_AGENT},
            timeout=30.0,
        )
        self._pacer = _Pacer(DISCORD_MIN_SECONDS_BETWEEN_CALLS)

    def guild_id(self) -> str:
        """The guild that owns the scratch channel, so a mistyped id cannot post into a real server."""
        response = self._request("GET", f"/channels/{self._channel}")
        if response.status_code != 200:
            raise RuntimeError(f"cannot read scratch channel: HTTP {response.status_code}")
        return str(response.json().get("guild_id", ""))

    def validate(self, built: Built) -> Verdict:
        body: dict[str, Any] = {**built.payload, "allowed_mentions": {"parse": []}}
        response = self._request("POST", f"/channels/{self._channel}/messages", body)
        if response.status_code == 200:
            self._delete(response.json()["id"])
            return Verdict(accepted=True)
        return Verdict(accepted=False, error=self._error(response))

    def _delete(self, message_id: str) -> None:
        response = self._request("DELETE", f"/channels/{self._channel}/messages/{message_id}")
        if response.status_code != 204:
            raise RuntimeError(
                f"could not delete scratch message {message_id}: HTTP {response.status_code}"
            )

    def _request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> httpx.Response:
        for _ in range(DISCORD_MAX_RATE_LIMIT_RETRIES):
            self._pacer.wait()
            response = self._client.request(method, DISCORD_API + path, json=body)
            if response.status_code == 429:
                time.sleep(float(response.json().get("retry_after", 1.0)) + 0.1)
                continue
            self._wait_if_bucket_spent(response)
            return response
        raise RuntimeError(f"Discord kept rate limiting {method} {path}")

    @staticmethod
    def _wait_if_bucket_spent(response: httpx.Response) -> None:
        if response.headers.get("x-ratelimit-remaining") == "0":
            time.sleep(float(response.headers.get("x-ratelimit-reset-after", "1.0")) + 0.05)

    @staticmethod
    def _error(response: httpx.Response) -> str:
        try:
            body = response.json()
        except ValueError:
            return f"HTTP {response.status_code}"
        parts = [
            f"HTTP {response.status_code} {body.get('code', '')} {body.get('message', '')}".strip()
        ]
        for path, detail in _discord_error_leaves(body.get("errors", {})):
            parts.append(f"{path}: {detail}")
        return " | ".join(parts)

    def close(self) -> None:
        self._client.close()


def _discord_error_leaves(node: Any, path: str = "") -> list[tuple[str, str]]:
    """Flatten Discord's nested `errors` object to (json path, `CODE: message`) pairs."""
    if not isinstance(node, dict):
        return []
    fields: dict[str, Any] = node  # pyright: ignore[reportUnknownVariableType]
    if "_errors" in fields:
        issues: list[dict[str, Any]] = fields["_errors"]
        return [(path or "/", f"{i.get('code', '')}: {i.get('message', '')}") for i in issues]
    leaves: list[tuple[str, str]] = []
    for key, value in fields.items():
        leaves.extend(_discord_error_leaves(value, f"{path}/{key}"))
    return leaves
