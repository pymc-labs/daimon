"""Shared `CliRuntime` builder for the CLI adapter tests."""

from __future__ import annotations

from typing import cast

from anthropic import AsyncAnthropic
from daimon.adapters.cli.runtime import CliRuntime
from daimon.core.config import Settings
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.scope import DeploymentDefault
from daimon.testing import MARouter, build_fake_anthropic, build_stub_anthropic
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class _FakeCli:
    local_user = "testuser"


class FakeCliSettings:
    """The narrowest settings stand-in the CLI commands read: `cli.local_user`.

    Tests whose commands read more (`mcp`, `github`, `defaults_root`, ...)
    define their own stand-in and pass it as `settings=`.
    """

    cli = _FakeCli()


def build_cli_runtime(
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    anthropic: AsyncAnthropic | None = None,
    router: MARouter | None = None,
    settings: object | None = None,
) -> CliRuntime:
    """A real `CliRuntime` over the test DB and a transport-level fake MA.

    Pass a ready `anthropic` client or a `router` (wrapped in a MockTransport-
    backed `AsyncAnthropic`); with neither, the client is a no-op 200 stub.
    `settings` is a duck-typed stand-in for the fields the command under test
    reads (see `FakeCliSettings`); it is cast, never validated.
    """
    if anthropic is not None and router is not None:
        raise ValueError("pass anthropic= or router=, not both")
    if anthropic is None:
        anthropic = (
            build_fake_anthropic(router.dispatch) if router is not None else build_stub_anthropic()
        )
    return CliRuntime(
        settings=cast(Settings, settings if settings is not None else FakeCliSettings()),
        anthropic=anthropic,
        sessionmaker=db_session_factory,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
    )
