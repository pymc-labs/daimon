"""Temporary CLI codecs for neutral resource ports; no provider calls.

Remove SDK decoding after the M0 host migration (sprint FOLLOWUPS.md).
The caller supplies the tenant and IDs established by its existing lookup.
"""

from uuid import uuid4

from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaEnvironment, SkillRetrieveResponse
from daimon.core.mux_backend import managed_agents, resource_ref
from daimon.core.mux_compat import legacy_call, sdk_environment, sdk_skill
from mux.contracts.ids import Scope


async def retrieve_environment(
    client: AsyncAnthropic, environment_id: str, *, scope: Scope
) -> BetaEnvironment:
    backend = managed_agents(
        client, scope=scope, resources=frozenset({("environment", environment_id)})
    )
    return sdk_environment(
        await legacy_call(
            backend.environments.retrieve(
                scope, resource_ref(backend, "environment", environment_id, scope=scope)
            )
        )
    )


async def delete_environment(client: AsyncAnthropic, environment_id: str, *, scope: Scope) -> None:
    backend = managed_agents(
        client, scope=scope, resources=frozenset({("environment", environment_id)})
    )
    await legacy_call(
        backend.environments.delete(
            scope,
            resource_ref(backend, "environment", environment_id, scope=scope),
            key=str(uuid4()),
        )
    )


async def retrieve_skill(
    client: AsyncAnthropic, skill_id: str, *, scope: Scope
) -> SkillRetrieveResponse:
    backend = managed_agents(client, scope=scope, resources=frozenset({("skill", skill_id)}))
    record = await legacy_call(backend.skills.retrieve(scope, skill_id))
    return SkillRetrieveResponse.model_construct(
        **sdk_skill(record).model_dump(mode="json", exclude_unset=True)
    )
