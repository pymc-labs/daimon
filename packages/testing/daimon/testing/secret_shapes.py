"""Every credential shape the redaction has had to handle, as canary renderers.

Shared by the all-sinks redaction tests: each shape renders a unique canary
inside realistic surrounding text, and every sink (Sentry, structlog JSON and
CLI chains, stdlib/uvicorn/fastmcp log filters, CLI error output) must emit
none of it.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from urllib.parse import quote

Render = Callable[[str], str]


def new_canary() -> str:
    """A canary holding a hyphen, one of the characters that ends naive matches."""
    return "hard-" + uuid.uuid4().hex[:12]


def plain(canary: str) -> str:
    """The canary without punctuation, for alphanumeric-only token formats."""
    return canary.replace("-", "")


def _encoded(text: str, levels: int = 1) -> str:
    for _ in range(levels):
        text = quote(text, safe="")
    return text


TEXT_SHAPES: dict[str, Render] = {
    # name=value pairs, every value terminator
    "env-pair": lambda c: f"boot failed: SLACK_BOT_TOKEN={c}",
    "env-pair-quoted-space": lambda c: f'API_TOKEN="hello {c}"',
    "env-pair-escaped-quote": lambda c: f'API_TOKEN="ab\\"{c}"',
    "env-pair-punctuation": lambda c: f"API_TOKEN=ab:{c};x",
    "env-pair-spaced": lambda c: f"password = {c}",
    "keys-suffix": lambda c: f"DAIMON_CRYPTO__KEYS={c}",
    # structured text
    "json": lambda c: json.dumps({"api_key": c}),
    "json-nested": lambda c: json.dumps({"detail": {"headers": {"Authorization": c}}}),
    "json-in-text": lambda c: f"HTTP 400: {json.dumps({'error': 'bad', 'refresh_token': c})}",
    "json-escaped": lambda c: f'body: {{\\"token\\": \\"{c}\\"}}',
    "dict-repr": lambda c: repr({"access_token": c}),
    "dict-repr-nested": lambda c: repr({"nested": [{"client_secret": c}]}),
    # CLI flags and argv
    "flag-eq": lambda c: f"run failed: --password={c}",
    "flag-space": lambda c: f"cli --api-key {c} exited 1",
    "argv-list": lambda c: (
        f"Command '['git', '--password', '{c}']' returned non-zero exit status 1."
    ),
    # headers and URL credentials
    "authorization-bearer": lambda c: f"Authorization: Bearer {c}",
    "authorization-basic": lambda c: f"header Authorization: Basic {c}",
    "url-userinfo": lambda c: f"connect postgresql://daimon:{c}@db:5432/daimon failed",
    "pydantic-input": lambda c: f"1 validation error: input_value='{c}', input_type=str",
    # provider token formats
    "slack-token": lambda c: f"slack said xoxb-1234567890-{c}",
    "slack-app-token": lambda c: f"socket xapp-1-{c}ABCDEFGH",
    "github-token": lambda c: f"github ghp_{plain(c)}abcdefghijklmnop",
    "anthropic-key": lambda c: f"key sk-ant-api03-{c}abcdefghijkl",
    "jwt": lambda c: f"jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIx{plain(c)}.c2lnbmF0dXJlLXZhbHVl",
    # OAuth
    "oauth-query": lambda c: f"GET https://h/oauth/callback?code={c}&state={c}x",
    "oauth-relative-target": lambda c: f'"GET /oauth/mcp/callback?code={c}&state=s HTTP/1.1" 500',
    "oauth-json": lambda c: f'oauth failed "code": "{c}"',
    "bare-query-key": lambda c: f"redirect to https://h/cb?{c}",
    "form-encoded": lambda c: f"body a=1%26client_secret%3D{c}%26b=2",
    # capability paths, raw and percent-encoded
    "upload-path": lambda c: f"PUT https://h/uploads/{plain(c)}Ab3Cd4Ef5 failed",
    "slack-file-path": lambda c: f"GET /slack/file/eyJ0ZWFtIjoiVDEifQ.{plain(c)}c2lnbmF0dXJl ok",
    "upload-path-encoded": lambda c: f"Referer: {_encoded(f'https://h/uploads/{c}')}",
    "slack-file-path-encoded-twice": lambda c: f"see {_encoded(f'/slack/file/x.{c}', 2)}",
    "upload-path-partly-encoded": lambda c: f"x /%75ploads/{c} y",
    "discord-webhook": lambda c: (
        f"POST https://discord.com/api/webhooks/123456789/{c} returned 404"
    ),
}

FIELD_SHAPES: dict[str, Callable[[str], dict[str, object]]] = {
    "secret-named-field": lambda c: {"api_key": c},
    "secret-named-field-token": lambda c: {"bot_token": c},
    "nested-header": lambda c: {"diagnostic": {"headers": {"Authorization": c}}},
    "list-of-dicts": lambda c: {"items": [{"client_secret": c}]},
    "text-in-field": lambda c: {"detail": f"token={c}"},
}
