"""Opaque MA skill ids become bare, own-namespace display names — or stay ids."""

from __future__ import annotations

import uuid

from daimon.core.defaults.skills import resolve_custom_skill_titles
from daimon.testing.builders import skill_listed
from daimon.testing.ma_models import ma_agent
from mux.testing.drivers.anthropic.ma import MARouter, build_fake_anthropic, list_response


def _skill(*, id: str, display_title: str) -> dict[str, object]:
    return skill_listed(id=id, display_title=display_title).model_dump(mode="json")


async def test_resolve_custom_skill_titles_omits_another_tenants_skill() -> None:
    tenant_id = uuid.uuid4()
    other_tenant_id = uuid.uuid4()
    router = MARouter()
    router.add(
        "GET",
        r"/v1/skills",
        lambda _req, _m: list_response(
            [_skill(id="skill_theirs", display_title=f"{str(other_tenant_id)[:8]}-secret")]
        ),
    )
    agent = ma_agent(skills=[{"type": "custom", "skill_id": "skill_theirs", "version": "1"}])

    titles, _truncated = await resolve_custom_skill_titles(
        build_fake_anthropic(router.dispatch), agents=[agent], tenant_id=tenant_id
    )

    assert titles == {}, "a foreign tenant's title must never reach a rendered skill name"
