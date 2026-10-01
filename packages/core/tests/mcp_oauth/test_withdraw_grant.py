"""A refused sign-in's grant cleanup is retried once and logged on failure."""

from types import SimpleNamespace

import anthropic as anthropic_pkg
import httpx
import pytest
import structlog.testing
from daimon.core.mcp_oauth.complete import _withdraw_grant


def _client(failures: int):
    calls: list[tuple[str, str]] = []

    async def delete(credential_id, *, vault_id):
        calls.append((credential_id, vault_id))
        if len(calls) <= failures:
            request = httpx.Request("DELETE", "https://api.anthropic.test")
            raise anthropic_pkg.APIConnectionError(request=request)

    client = SimpleNamespace(
        beta=SimpleNamespace(vaults=SimpleNamespace(credentials=SimpleNamespace(delete=delete)))
    )
    return client, calls


@pytest.mark.parametrize(("failures", "logged"), [(1, False), (2, True)])
async def test_withdraw_grant_retries_once_then_logs(failures, logged):
    client, calls = _client(failures)
    with structlog.testing.capture_logs() as logs:
        await _withdraw_grant(client, credential_id="cred-1", vault_id="vault-1")
    assert calls == [("cred-1", "vault-1")] * 2
    failed = [e for e in logs if e["event"] == "mcp_oauth.refused_grant_withdraw_failed"]
    assert bool(failed) is logged
    if logged:
        assert failed[0]["credential_id"] == "cred-1"
        assert failed[0]["vault_id"] == "vault-1"
