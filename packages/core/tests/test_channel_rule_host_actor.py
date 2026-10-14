"""A channel rule change through a native resource host must name the host's account.

`set_channel_rule` refuses, before any read, a resource host supplied without
`actor_account_id` or for any other tenant or account. Without a host the
existing path is unchanged (covered by the channel rule suites).
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from daimon.core.authz import Subject
from daimon.core.channel_rules import set_channel_rule
from daimon.core.neutral_resources import NeutralResourceHost
from daimon.core.scope import DeploymentDefault
from daimon.testing.isolated_sdk import build_async_anthropic, build_async_openai
from mux.contracts.ids import Scope
from mux.drivers.openai import OpenAIDriver
from mux.drivers.openai.transport import SDKTransport
from mux.drivers.openai.turn import MemoryRecoveryJournal
from mux.drivers.openai.usage import MemoryUsageRevisions
from mux.errors import ScopeViolation
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

TENANT = uuid.uuid4()
ACCOUNT = uuid.uuid4()


def _no_io(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"provider I/O before the scope check: {request.url}")


class _NeverOpened(async_sessionmaker[AsyncSession]):
    def __call__(self, **local_kw: object) -> AsyncSession:  # type: ignore[override]
        raise AssertionError("a database read before the scope check")

    def begin(self) -> Any:
        raise AssertionError("a database write before the scope check")


def _host(tenant_id: uuid.UUID, account_id: uuid.UUID) -> NeutralResourceHost:
    scope = Scope(
        tenant_id=str(tenant_id),
        account_id=str(account_id),
        principal_id="operator",
        authorization_id="operator-grant",
    )
    driver = OpenAIDriver(
        SDKTransport(build_async_openai(transport=httpx.MockTransport(_no_io))),
        account_scope_id="project",
        authorization=lambda authorized, _kind, _id: authorized == scope,
        journal=MemoryRecoveryJournal(),
        usage_revisions=MemoryUsageRevisions(),
    )
    return NeutralResourceHost(
        driver, _NeverOpened(), scope=scope, account_scope_id="project", operation_key="op"
    )


async def _set(host: NeutralResourceHost, actor: uuid.UUID | None) -> None:
    await set_channel_rule(
        build_async_anthropic(transport=httpx.MockTransport(_no_io)),
        _NeverOpened(),
        tenant_id=TENANT,
        platform="discord",
        channel_id="900000000000000002",
        readers="own",
        subject=Subject(is_admin=True),
        default=DeploymentDefault(),
        actor_account_id=actor,
        copy=True,
        copy_from="shared",
        resource_host=host,
    )


@pytest.mark.parametrize(
    ("host_account", "actor"),
    [
        pytest.param("foreign", None, id="actor_omitted_same_tenant_foreign_account"),
        pytest.param("own", None, id="actor_omitted_matching_host"),
        pytest.param("foreign", "own", id="actor_given_foreign_account_host"),
    ],
)
async def test_a_host_without_its_own_account_named_is_refused_before_any_read(
    host_account: str, actor: str | None
) -> None:
    account = ACCOUNT if host_account == "own" else uuid.uuid4()
    with pytest.raises(ScopeViolation):
        await _set(_host(TENANT, account), ACCOUNT if actor == "own" else None)


async def test_a_host_for_another_tenant_is_refused_before_any_read() -> None:
    with pytest.raises(ScopeViolation):
        await _set(_host(uuid.uuid4(), ACCOUNT), ACCOUNT)
