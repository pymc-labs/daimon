from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from mux.conformance.fixtures import FIXTURES
from mux.conformance.runner import ConformanceFailure
from mux.contracts.ids import ResourceRef, Scope
from mux.contracts.receipts import DeletionReceipt
from mux.drivers.openai import OpenAIDriver
from mux.drivers.openai.artifacts import OpenAIArtifacts
from mux.drivers.openai.conformance import REF, SCOPE, OpenAIScriptedTransport
from mux.drivers.openai.sessions import OpenAISessions
from mux.drivers.openai.transport import SDKTransport
from mux.drivers.openai.turn import MemoryRecoveryJournal
from mux.drivers.openai.usage import MemoryUsageRevisions
from mux.errors import ProviderError
from openai import AsyncOpenAI


class Script(OpenAIScriptedTransport):
    async def delete_shared_vault(self) -> None:
        assert self._sdk is not None
        await SDKTransport(self._sdk).request("DELETE", "/vaults/shared-vault")


@pytest.fixture
async def ports() -> AsyncIterator[tuple[OpenAIDriver, Script]]:
    script = Script()
    sdk = AsyncOpenAI(
        api_key="offline-fixture",
        base_url="https://openai.invalid/v1",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(script.handle)),
    )
    driver = OpenAIDriver(
        SDKTransport(sdk),
        account_scope_id="project",
        authorization=lambda scope, kind, id_: scope == SCOPE,
        journal=MemoryRecoveryJournal(),
        usage_revisions=MemoryUsageRevisions(),
    )
    script.attach(driver, sdk)
    try:
        yield driver, script
    finally:
        await script.aclose()


@pytest.mark.asyncio
async def test_c09_real_resource_ports_and_native_deletion(
    ports: tuple[OpenAIDriver, Script],
) -> None:
    driver, script = ports
    result = await FIXTURES["C09"](driver, None, script)
    assert result.status == "pass"
    assert script.deleted_resources == (REF,)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutant,diagnostic",
    [
        ("bytes", "binary checksum"),
        ("truncation", "truncated, duplicated or corrupted"),
        ("retention", "exact seeded shared resources"),
        ("vault_delete", "provider must not actually delete shared resources"),
    ],
)
async def test_c09_broken_driver_variants_fail_contract_checks(
    ports: tuple[OpenAIDriver, Script],
    monkeypatch: pytest.MonkeyPatch,
    mutant: str,
    diagnostic: str,
) -> None:
    driver, script = ports
    if mutant in ("bytes", "truncation"):
        original = OpenAIArtifacts.download

        async def broken(
            self: OpenAIArtifacts, scope: Scope, ref: ResourceRef
        ) -> AsyncIterator[bytes]:
            try:
                async for chunk in original(self, scope, ref):
                    yield b"x" * len(chunk) if mutant == "bytes" else chunk
            except ProviderError:
                if mutant != "truncation":
                    raise

        monkeypatch.setattr(OpenAIArtifacts, "download", broken)
    else:
        original_delete = OpenAISessions.delete

        async def deleted(
            self: OpenAISessions, scope: Scope, ref: ResourceRef, *, key: str
        ) -> DeletionReceipt:
            value = await original_delete(self, scope, ref, key=key)
            if mutant == "retention":
                return value.model_copy(update={"retained": ()})
            await script.delete_shared_vault()
            return value

        monkeypatch.setattr(OpenAISessions, "delete", deleted)
    with pytest.raises(ConformanceFailure, match=diagnostic):
        await FIXTURES["C09"](driver, None, script)
