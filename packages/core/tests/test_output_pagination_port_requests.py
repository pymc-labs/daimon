"""Main #833 pagination through the scoped output seam at the SDK boundary."""

import httpx
import pytest
from anthropic import APIStatusError
from daimon.core.mux_backend import managed_agents, resource_ref, resource_scope
from daimon.core.output_ports_compat import list_output_records
from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport
from mux.contracts.ids import ResourceRef
from mux.drivers.anthropic.outputs import Outputs
from mux.errors import ScopeViolation

SCOPE = resource_scope(tenant_id="tenant-a", account_id="account-a")
MA_BETA = "managed-agents-2026-04-01"


@pytest.mark.parametrize("second_status", [200, 400, 404, 429, 500])
async def test_all_output_pages_match_main_requests_and_late_sdk_errors(
    second_status: int,
) -> None:
    old, new = ScriptedTransport(), ScriptedTransport()
    for transport in (old, new):
        transport.queue(
            ScriptedReply(
                "GET",
                "/v1/files",
                httpx.Response(
                    200,
                    json={
                        "data": [{"id": "file_a", "downloadable": False}],
                        "first_id": "file_a",
                        "last_id": "file_a",
                        "has_more": True,
                    },
                ),
            ),
            ScriptedReply(
                "GET",
                "/v1/files",
                httpx.Response(
                    second_status,
                    json=(
                        {
                            "data": [
                                {
                                    "id": "file_b",
                                    "downloadable": True,
                                    "filename": "result.txt",
                                    "mime_type": "text/plain",
                                    "size_bytes": 3,
                                }
                            ],
                            "has_more": False,
                        }
                        if second_status == 200
                        else {"error": {"type": "api_error", "message": "late page failed"}}
                    ),
                ),
            ),
        )
    async with old.client() as legacy, new.client() as client:
        if second_status == 200:
            originals = [
                item
                async for item in legacy.beta.files.list(
                    scope_id="sess_output", betas=[MA_BETA], limit=1000
                )
            ]
            result = await list_output_records(client, "sess_output", scope=SCOPE, all_pages=True)
            assert [item.model_dump(mode="json", exclude_unset=True) for item in result] == [
                item.model_dump(mode="json", exclude_unset=True) for item in originals
            ]
            assert [item.id for item in result] == ["file_a", "file_b"]
        else:
            with pytest.raises(APIStatusError) as original_error:
                async for _ in legacy.beta.files.list(
                    scope_id="sess_output", betas=[MA_BETA], limit=1000
                ):
                    pass
            with pytest.raises(type(original_error.value)) as migrated_error:
                await list_output_records(client, "sess_output", scope=SCOPE, all_pages=True)
            assert str(migrated_error.value) == str(original_error.value)
            assert migrated_error.value.status_code == second_status
    old.assert_consumed()
    new.assert_consumed()
    assert old.requests == new.requests
    assert len(new.requests) == 2
    assert dict(new.requests[0].query) == {
        "beta": "true",
        "limit": "1000",
        "scope_id": "sess_output",
    }
    assert dict(new.requests[1].query) == {
        "beta": "true",
        "limit": "1000",
        "scope_id": "sess_output",
        "after_id": "file_a",
    }
    assert all(
        dict(r.protocol_headers)["anthropic-beta"] == f"{MA_BETA},files-api-2025-04-14"
        for r in new.requests
    )


@pytest.mark.parametrize("foreign", [False, True])
async def test_all_output_pages_require_authorized_scope_before_any_io(foreign: bool) -> None:
    transport = ScriptedTransport()
    async with transport.client() as client:
        backend = managed_agents(
            client,
            scope=SCOPE,
            resources=frozenset({("session", "foreign")}) if foreign else frozenset(),
        )
        port = backend.extension(Outputs, namespace="anthropic.outputs", version=1)
        ref = (
            ResourceRef(
                id="foreign",
                kind="session",
                provider="anthropic",
                account_scope_id=backend.account_scope_id,
                tenant_id="tenant-b",
                account_id=SCOPE.account_id,
            )
            if foreign
            else resource_ref(backend, "session", "ungranted", scope=SCOPE)
        )
        with pytest.raises(ScopeViolation):
            await port.list_outputs(SCOPE, ref, all_pages=True)
    assert transport.requests == []
