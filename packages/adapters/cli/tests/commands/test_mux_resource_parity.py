"""CLI extraction fidelity at the real SDK transport, without live requests."""

from contextlib import asynccontextmanager
from email.parser import BytesParser
from email.policy import default
from io import StringIO
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from anthropic import APIStatusError
from daimon.adapters.cli import mux_compat
from daimon.adapters.cli.commands import environments, skills_backfill
from daimon.core.mux_backend import resource_scope
from daimon.core.mux_compat import (
    archive_agent,
    archive_environment,
    create_skill,
    list_skill_versions,
    retrieve_agent,
    update_agent,
    update_environment,
)
from daimon.testing.ma_models import ma_agent, ma_environment
from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport
from rich.console import Console

TENANT = UUID("00000000-0000-0000-0000-000000000007")
SCOPE = resource_scope(tenant_id=str(TENANT))
SKILL = {
    "id": "skill1",
    "type": "skill",
    "source": "custom",
    "display_title": "legacy",
    "created_at": "2026-01-01T00:00:00Z",
    "updated_at": "2026-01-01T00:00:00Z",
    "latest_version": "1",
}
VERSION = {
    "skill_id": "skill1",
    "version": "1",
    "name": "sample",
    "description": "sample",
    "created_at": "2026-01-01T00:00:00Z",
}


def scripts(*replies):
    transports = [ScriptedTransport(), ScriptedTransport()]
    for transport in transports:
        transport.queue(*replies)
    return transports


def assert_equal(before, after):
    for transport in (before, after):
        transport.assert_consumed()
    assert [r.to_dict() for r in after.requests] == [r.to_dict() for r in before.requests]


@pytest.mark.parametrize("kind", ["agent", "environment", "skill"])
async def test_cli_native_record_retrieval_is_identical(kind):
    record = {
        "agent": ma_agent(id="agent1", tenant_id=TENANT).model_dump(mode="json"),
        "environment": ma_environment(id="environment1", tenant_id=TENANT).model_dump(mode="json"),
        "skill": SKILL,
    }[kind]
    before, after = scripts(
        ScriptedReply("GET", f"/v1/{kind}s/{kind}1", httpx.Response(200, json=record))
    )
    async with before.client() as old, after.client() as new:
        expected = await getattr(old.beta, f"{kind}s").retrieve(f"{kind}1")
        call = {
            "agent": retrieve_agent,
            "environment": mux_compat.retrieve_environment,
            "skill": mux_compat.retrieve_skill,
        }[kind]
        actual = await call(new, f"{kind}1", scope=SCOPE)
    assert_equal(before, after)
    assert actual.model_dump(mode="json") == expected.model_dump(mode="json")


@pytest.mark.parametrize("resource", ["agent", "environment"])
async def test_cli_archive_request_is_identical(resource):
    before, after = scripts(
        ScriptedReply("POST", f"/v1/{resource}s/id/archive", httpx.Response(200, json={}))
    )
    async with before.client() as old, after.client() as new:
        await getattr(old.beta, f"{resource}s").archive("id")
        call = archive_agent if resource == "agent" else archive_environment
        await call(new, "id", scope=SCOPE)
    assert_equal(before, after)


@pytest.mark.parametrize(
    "payload",
    [
        {
            "skills": [
                {"type": "custom", "skill_id": "new"},
                {"type": "anthropic", "skill_id": "xlsx", "version": "latest"},
            ]
        },
        {
            "tools": [
                {
                    "type": "agent_toolset_20260401",
                    "configs": [{"name": "bash", "enabled": True}],
                    "default_config": {"permission_policy": {"type": "always_allow"}},
                }
            ]
        },
        {
            "name": "renamed",
            "metadata": {
                "daimon_tenant": str(TENANT),
                "daimon_account": "guild",
                "daimon_reader_of": "original",
            },
        },
    ],
)
async def test_cli_agent_backfill_and_rekey_payloads_are_identical(payload):
    record = ma_agent(id="agent1", tenant_id=TENANT, version=4).model_dump(mode="json")
    before, after = scripts(
        ScriptedReply("POST", "/v1/agents/agent1", httpx.Response(200, json=record))
    )
    async with before.client() as old, after.client() as new:
        expected = await old.beta.agents.update("agent1", version=3, **payload)
        actual = await update_agent(new, "agent1", version=3, payload=payload, scope=SCOPE)
    assert_equal(before, after)
    assert actual.model_dump(mode="json") == expected.model_dump(mode="json")


@pytest.mark.parametrize("status", [200, 409])
async def test_cli_environment_delete_keeps_exception_type(status):
    record = (
        {"error": {"type": "conflict_error", "message": "environment in use"}}
        if status == 409
        else {}
    )
    before, after = scripts(
        ScriptedReply("DELETE", "/v1/environments/env1", httpx.Response(status, json=record))
    )
    async with before.client() as old, after.client() as new:
        if status == 200:
            await old.beta.environments.delete("env1")
            await mux_compat.delete_environment(new, "env1", scope=SCOPE)
        else:
            with pytest.raises(APIStatusError) as expected:
                await old.beta.environments.delete("env1")
            with pytest.raises(type(expected.value)) as actual:
                await mux_compat.delete_environment(new, "env1", scope=SCOPE)
            assert str(actual.value) == str(expected.value)
    assert_equal(before, after)


async def test_cli_environment_update_keeps_explicit_empties():
    record = ma_environment(id="env1", tenant_id=TENANT).model_dump(mode="json")
    payload = {
        "name": "renamed",
        "description": "",
        "metadata": {},
        "config": {"type": "cloud", "packages": {"pip": []}},
    }
    before, after = scripts(
        ScriptedReply("POST", "/v1/environments/env1", httpx.Response(200, json=record))
    )
    async with before.client() as old, after.client() as new:
        expected = await old.beta.environments.update("env1", **payload)
        actual = await update_environment(new, "env1", payload, scope=SCOPE)
    assert_equal(before, after)
    assert actual.model_dump(mode="json") == expected.model_dump(mode="json")


@pytest.mark.parametrize("last", [None, ""])
async def test_cli_skill_version_walk_preserves_pagination(last):
    before, after = scripts(
        ScriptedReply(
            "GET",
            "/v1/skills/skill1/versions",
            httpx.Response(200, json={"data": [VERSION], "next_page": "next"}),
        ),
        ScriptedReply(
            "GET",
            "/v1/skills/skill1/versions",
            httpx.Response(200, json={"data": [{**VERSION, "version": "2"}], "next_page": last}),
        ),
    )
    async with before.client() as old, after.client() as new:
        expected = [
            v.model_dump(mode="json") async for v in old.beta.skills.versions.list("skill1")
        ]
        actual = [
            v.model_dump(mode="json") async for v in list_skill_versions(new, "skill1", scope=SCOPE)
        ]
    assert_equal(before, after)
    assert actual == expected


@asynccontextmanager
async def db_context():
    yield SimpleNamespace(begin=db_context)


def runtime(client):
    return SimpleNamespace(
        anthropic=client,
        sessionmaker=db_context,
        settings=SimpleNamespace(cli=SimpleNamespace(local_user="operator")),
    )


def authorize_environment_commands(monkeypatch):
    async def discover(_):
        return TENANT

    async def principal(*args, **kwargs):
        return None

    monkeypatch.setattr(environments, "discover_tenant", discover)
    monkeypatch.setattr(environments, "get_or_create_cli_principal", principal)


@pytest.mark.parametrize("description", [None, ""])
async def test_cli_environment_fork_callsite_preserves_null_description(monkeypatch, description):
    authorize_environment_commands(monkeypatch)
    record = ma_environment(id="env1", name="source", tenant_id=TENANT).model_dump(mode="json")

    record["description"] = description

    async def find(*args, **kwargs):
        return SimpleNamespace(id="env1")

    async def matches(*args, **kwargs):
        return []

    monkeypatch.setattr(environments, "find_environment_by_daimon_tag", find)
    monkeypatch.setattr(environments, "find_environments_by_daimon_tag", matches)
    before, after = scripts(
        ScriptedReply("GET", "/v1/environments/env1", httpx.Response(200, json=record)),
        ScriptedReply("POST", "/v1/environments", httpx.Response(200, json=record)),
    )
    async with before.client() as old, after.client() as new:
        source = await old.beta.environments.retrieve("env1")
        await old.beta.environments.create(
            name="copy",
            config=source.config.model_dump(mode="json"),
            description=description,
            metadata={"daimon_tenant": str(TENANT), "daimon_name": "copy"},
        )
        await environments.environments_fork(
            rt=runtime(new), console=Console(file=StringIO()), src="source", dst="copy"
        )
    assert_equal(before, after)


def multipart(request):
    headers = dict(request.protocol_headers)
    message = BytesParser(policy=default).parsebytes(
        b"Content-Type: " + headers["content-type"].encode() + b"\r\n\r\n" + request.body
    )
    return [
        (
            part.get_param("name", header="content-disposition"),
            part.get_filename(),
            part.get_content_type(),
            part.get_payload(decode=True),
        )
        for part in message.iter_parts()
    ]


async def test_cli_skill_upload_helper_preserves_multipart_bytes():
    before, after = scripts(ScriptedReply("POST", "/v1/skills", httpx.Response(200, json=SKILL)))
    data = b"PK\x03\x04exact zip contents\x00\xff"
    async with before.client() as old, after.client() as new:
        expected = await old.beta.skills.create(
            display_title="new", files=[("SKILL.zip", data, "application/zip")]
        )
        actual = await create_skill(new, display_title="new", data=data, scope=SCOPE)
    for transport in (before, after):
        transport.assert_consumed()
    assert multipart(after.requests[0]) == multipart(before.requests[0])
    assert actual.model_dump(mode="json") == expected.model_dump(mode="json")


async def test_cli_seeded_backfill_upload_callsite_preserves_multipart(monkeypatch, tmp_path):
    data = b"PK\x03\x04the actual seeded archive\x00\xff"
    archive = tmp_path / "generated.zip"
    archive.write_bytes(data)
    monkeypatch.setattr(skills_backfill, "build_skill_zip", lambda _: SimpleNamespace(path=archive))
    row = skills_backfill._BackfillRow(
        tenant_id=str(TENANT),
        agent_names="agent",
        skill_id="old",
        display_title="sample",
        classification="RECREATE_SEEDED",
        new_title=f"{TENANT}/sample",
        new_skill_id="",
    )
    before, after = scripts(ScriptedReply("POST", "/v1/skills", httpx.Response(200, json=SKILL)))
    async with before.client() as old, after.client() as new, httpx.AsyncClient() as http:
        with archive.open("rb") as fh:
            expected = await old.beta.skills.create(
                display_title=row.new_title, files=[("SKILL.zip", fh, "application/zip")]
            )
        actual = await skills_backfill._create_new_skill(
            client=new,
            sessionmaker=None,
            http_client=http,
            console=Console(file=StringIO()),
            row=row,
            tenant_id=TENANT,
        )
    for transport in (before, after):
        transport.assert_consumed()
    assert multipart(after.requests[0]) == multipart(before.requests[0])
    assert multipart(after.requests[0])[-1] == ("files[]", "SKILL.zip", "application/zip", data)
    assert actual == expected.id
    assert not archive.exists()
