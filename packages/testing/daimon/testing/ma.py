"""Daimon-specific Managed Agents fakes: tenant-stamped resolver routes and the live-key gate.

The transport fakes (`MARouter`, `build_fake_anthropic`, `make_fake_ma_handler`, ...) live
in `mux.testing.drivers.anthropic.ma`; this module adds only what depends on Daimon's
tenant metadata or its test environment variables.
"""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import Callable

import httpx
import pytest
from anthropic.types.beta import BetaEnvironment, BetaManagedAgentsAgent
from daimon.testing.ma_models import ma_agent, ma_environment
from mux.testing.drivers.anthropic.ma import MARouter
from mux.testing.drivers.anthropic.ma_models import ma_session

# ---------------------------------------------------------------------------
# Live-API contract-test helper
# ---------------------------------------------------------------------------


def require_api_key() -> str:
    """Read DAIMON_TEST_ANTHROPIC_API_KEY from env, skip if missing.

    Shared by contract-test conftests (`-m contract`, env-gated, never gate
    CI) that need a real AsyncAnthropic client backed by a live API key.
    """
    key = os.environ.get("DAIMON_TEST_ANTHROPIC_API_KEY")
    if not key:
        pytest.skip("DAIMON_TEST_ANTHROPIC_API_KEY not set — contract tests skipped")
    return key


def make_agent_env_echo_handler(
    *, tenant_id: uuid.UUID | str | None = None, with_session: bool = True
) -> Callable[[httpx.Request], httpx.Response]:
    """Answer agent / environment (and, by default, session) retrieves for
    whatever id was asked, built on the canonical `ma_*` shapes.

    Covers the endpoints a thread turn hits when it creates an MA session
    (`GET /v1/agents/{id}`, `GET /v1/environments/{id}`) and the session
    read a mapping row without a recorded configuration triggers
    (`GET /v1/sessions/{id}`, unless `with_session=False`). Any other
    request fails loudly with an AssertionError naming it.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        m = re.fullmatch(r"/v1/agents/(?P<id>[^/]+)", path)
        if m and request.method == "GET":
            agent = ma_agent(id=m.group("id"), tenant_id=tenant_id)
            return httpx.Response(200, json=agent.model_dump(mode="json"))
        m = re.fullmatch(r"/v1/environments/(?P<id>[^/]+)", path)
        if m and request.method == "GET":
            environment = ma_environment(id=m.group("id"), tenant_id=tenant_id)
            return httpx.Response(200, json=environment.model_dump(mode="json"))
        m = re.fullmatch(r"/v1/sessions/(?P<id>[^/]+)", path)
        if with_session and m and request.method == "GET":
            session = ma_session(id=m.group("id"))
            return httpx.Response(200, json=session.model_dump(mode="json"))
        raise AssertionError(f"make_agent_env_echo_handler: unhandled {request.method} {path}")

    return handler


def resolved_agent_env_router(
    agent: BetaManagedAgentsAgent | None = None,
    environment: BetaEnvironment | None = None,
    *,
    tenant_id: uuid.UUID | str | None = None,
    router: MARouter | None = None,
) -> MARouter:
    """A router whose list + retrieve routes resolve one agent and one
    environment: what admission needs to pick a responder for `tenant_id`.

    Defaults to `ma_agent(tenant_id=...)` / `ma_environment(tenant_id=...)`
    so the resolver's tag lookup (`daimon_tenant` + `daimon_name`) finds
    them. Retrieve routes are exact-id (`MARouter.add_agent` /
    `add_environment`): a retrieve of any other id fails loudly. Pass
    `router=` to add these routes to an existing router.
    """
    resolved_agent = agent if agent is not None else ma_agent(tenant_id=tenant_id)
    resolved_environment = (
        environment if environment is not None else ma_environment(tenant_id=tenant_id)
    )
    target = router if router is not None else MARouter()
    target.add_agent_list(resolved_agent)
    target.add_agent(resolved_agent)
    target.add_environment_list(resolved_environment)
    target.add_environment(resolved_environment)
    return target
