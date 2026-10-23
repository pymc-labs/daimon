"""Authenticated delegation CLI; the MCP server attests the caller's platform role.

A claimed actor id or local operator context is insufficient: mutations use
an authenticated platform-user MCP credential, checked against its registry.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

import typer

admins_app = typer.Typer(
    help="List, grant or revoke tenant-wide Daimon admins using your MCP login."
)


def _call(url: str, token: str, tool: str, arguments: dict[str, object]) -> None:
    async def run() -> None:
        from fastmcp import Client

        async with Client(url, auth=token) as client:
            result = await client.call_tool(tool, arguments)
            for block in result.content:
                if block.type == "text":
                    typer.echo(block.text)

    asyncio.run(run())


@admins_app.command("list")
def admins_list(
    url: Annotated[
        str, typer.Option(envvar="DAIMON_ADMINS_URL", help="MCP endpoint for this tenant.")
    ],
    token: Annotated[
        str,
        typer.Option(
            envvar="DAIMON_ADMINS_TOKEN", help="Your platform-user MCP credential.", hide_input=True
        ),
    ],
) -> None:
    _call(url, token, "list_admins", {})


@admins_app.command("grant")
def admins_grant(
    subject_id: str,
    url: Annotated[str, typer.Option(envvar="DAIMON_ADMINS_URL")],
    token: Annotated[str, typer.Option(envvar="DAIMON_ADMINS_TOKEN", hide_input=True)],
    role: Annotated[bool, typer.Option(help="Grant a Discord role instead of a user.")] = False,
) -> None:
    _call(
        url,
        token,
        "grant_admin",
        {"subject_id": subject_id, "subject_kind": "role" if role else "user"},
    )


@admins_app.command("revoke")
def admins_revoke(
    subject_id: str,
    url: Annotated[str, typer.Option(envvar="DAIMON_ADMINS_URL")],
    token: Annotated[str, typer.Option(envvar="DAIMON_ADMINS_TOKEN", hide_input=True)],
    role: Annotated[bool, typer.Option(help="Revoke a Discord role grant.")] = False,
) -> None:
    _call(
        url,
        token,
        "revoke_admin",
        {"subject_id": subject_id, "subject_kind": "role" if role else "user"},
    )
