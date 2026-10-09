"""Main #494 workspace downloads retain their exact offline SDK requests."""

import httpx
import pytest
from anthropic import APIStatusError, omit
from daimon.core.defaults.ma_index import SkillVersionNotFoundError, download_skill_version
from daimon.core.mux_backend import resource_scope
from daimon.core.mux_compat import legacy_call, legacy_iter
from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport
from mux.contracts.ids import Scope
from mux.drivers.anthropic.resources._authorization import ResourceAuthorization
from mux.drivers.anthropic.resources.platform_export import AnthropicPlatformExport
from mux.drivers.anthropic.resources.skills import AnthropicSkillVersions
from mux.errors import ScopeViolation


async def main_download(client, *, skill_id, version):
    # Unchanged main #494 algorithm: lazy lookup, native ID, omitted beta header.
    version_id = None
    async for row in client.beta.skills.versions.list(skill_id=skill_id):
        if row.version == version:
            version_id = row.id
            break
    if version_id is None:
        raise SkillVersionNotFoundError(f"{skill_id} has no version {version}.")
    content = await client.beta.skills.versions.download(
        version_id, skill_id=skill_id, extra_headers={"anthropic-beta": omit}
    )
    try:
        return await content.read()
    finally:
        await content.close()


def script(case):
    transport = ScriptedTransport()
    path = "/v1/skills/skill1/versions"
    target = {"id": "skill_version_target", "version": "123"}
    filler = {"id": "skill_version_old", "version": "122"}
    first = [target] if case == "early" else [filler]
    cursor = "second" if case in {"early", "second"} else None
    if case == "empty":
        first, cursor = [], "unused"
    transport.queue(
        ScriptedReply(
            "GET",
            path,
            httpx.Response(200, json={"data": first, "next_page": cursor}),
            query=(("beta", "true"),),
        )
    )
    if case == "second":
        transport.queue(
            ScriptedReply(
                "GET",
                path,
                httpx.Response(200, json={"data": [target], "next_page": "unused"}),
                query=(("beta", "true"), ("page", "second")),
            )
        )
    if case in {"early", "second"}:
        transport.queue(
            ScriptedReply(
                "GET",
                path + "/skill_version_target/content",
                httpx.Response(200, content=b"zip"),
                query=(("beta", "true"),),
            )
        )
    return transport


@pytest.mark.parametrize("case", ["early", "second", "missing", "empty"])
async def test_fork_lookup_matches_main_and_stops_at_first_match(case):
    before, after = script(case), script(case)
    async with before.client() as old, after.client() as new:
        if case in {"missing", "empty"}:
            with pytest.raises(SkillVersionNotFoundError) as expected:
                await main_download(old, skill_id="skill1", version="123")
            with pytest.raises(SkillVersionNotFoundError) as actual:
                await download_skill_version(
                    new, skill_id="skill1", version="123", scope=resource_scope(tenant_id="tenant")
                )
            assert str(actual.value) == str(expected.value)
        else:
            assert (
                await main_download(old, skill_id="skill1", version="123")
                == (
                    await download_skill_version(
                        new,
                        skill_id="skill1",
                        version="123",
                        scope=resource_scope(tenant_id="tenant"),
                    )
                )
                == b"zip"
            )
            assert "anthropic-beta" not in dict(after.requests[-1].protocol_headers)
    before.assert_consumed()
    after.assert_consumed()
    assert after.requests == before.requests


@pytest.mark.parametrize("status", [200, 403])
async def test_export_uses_known_native_id_without_an_extra_lookup(status):
    before, after = ScriptedTransport(), ScriptedTransport()

    def response():
        if status == 200:
            return httpx.Response(status, content=b"zip")
        return httpx.Response(
            status, json={"error": {"type": "permission_error", "message": "denied"}}
        )

    path = "/v1/skills/skill1/versions/skill_version_target/content"
    for transport in (before, after):
        transport.queue(ScriptedReply("GET", path, response(), query=(("beta", "true"),)))
    scope = Scope.platform(reason="recovery export", authorization_id="platform-export")
    async with before.client() as old, after.client() as new:

        async def direct():
            content = await old.beta.skills.versions.download(
                "skill_version_target", skill_id="skill1", extra_headers={"anthropic-beta": omit}
            )
            try:
                return await content.read()
            finally:
                await content.close()

        if status == 403:
            with pytest.raises(APIStatusError) as expected:
                await direct()
            with pytest.raises(APIStatusError) as actual:
                await legacy_call(
                    AnthropicPlatformExport(new).download_skill_version_id(
                        scope, "skill1", "skill_version_target"
                    )
                )
            assert actual.value.status_code == expected.value.status_code
        else:
            assert await direct() == await legacy_call(
                AnthropicPlatformExport(new).download_skill_version_id(
                    scope, "skill1", "skill_version_target"
                )
            )
    before.assert_consumed()
    after.assert_consumed()
    assert after.requests == before.requests
    assert len(after.requests) == 1


@pytest.mark.parametrize("operation", ["walk", "download"])
async def test_native_id_operations_reject_foreign_scope_before_io(operation):
    transport = ScriptedTransport()
    scope = resource_scope(tenant_id="tenant")
    async with transport.client() as client:
        port = AnthropicSkillVersions(
            client, ResourceAuthorization(scope, frozenset({("skill", "skill1")}))
        )
        foreign = resource_scope(tenant_id="foreign")
        with pytest.raises(ScopeViolation):
            iterator = (
                port.walk_native(foreign, "skill1")
                if operation == "walk"
                else port.download_by_id(foreign, "skill1", "version")
            )
            await anext(legacy_iter(iterator))
    assert transport.requests == []
