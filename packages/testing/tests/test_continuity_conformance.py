"""Actual provider driver reads behind controlled host hooks; no live verdict."""

import os
from datetime import timedelta
from typing import Literal

import httpx
import pytest
from daimon.testing.conformance.gemini import GeminiScript
from daimon.testing.conformance.runner import ConformanceFailure
from daimon.testing.continuity_conformance import (
    HistoryExpectations,
    LossProbe,
    ProbeClock,
    collect_history_evidence,
    collect_loss_evidence,
)
from daimon.testing.drivers.gemini.fake import MemoryStorage
from daimon.testing.drivers.openai.conformance import SCOPE, OpenAIScriptedTransport
from daimon.testing.isolated_sdk import AmbientSDKConfigurationError, build_async_openai
from mux.contracts.ports import ManagedAgents
from mux.drivers.gemini import GeminiManagedAgents
from mux.drivers.openai import OpenAIDriver
from mux.drivers.openai.transport import SDKTransport
from mux.drivers.openai.turn import MemoryRecoveryJournal
from mux.drivers.openai.usage import MemoryUsageRevisions
from mux.errors import ScopeViolation


@pytest.mark.parametrize("kind", ["expiry", "unexpected_loss"])
async def test_gemini_loss_uses_real_clock_read_and_upstream_missing_workspace(
    monkeypatch: pytest.MonkeyPatch, kind: Literal["expiry", "unexpected_loss"]
) -> None:
    script = GeminiScript()
    scenario = await script.arrange("C06")
    async with script.storage.transaction() as records:
        expiry = records.sessions[scenario.session.ref.id].workspace_expires_at
    assert expiry is not None
    clock = ProbeClock(expiry - timedelta(hours=1))
    monkeypatch.setattr("mux.drivers.gemini.core.now", clock)

    async def lose():
        del script.saved["root"]

    evidence = await collect_loss_evidence(
        LossProbe(
            clock,
            expiry,
            scenario.scope,
            scenario.session,
            script.ma,
            lambda: script.mutation_count,
            lose,
        ),
        kind,
    )
    assert evidence.kind == kind
    assert evidence.binding_id == scenario.session.binding.id
    assert evidence.source == "controlled_host_fault"
    assert evidence.writes_after == evidence.writes_before
    assert (evidence.observed_at >= expiry) == (kind == "expiry")
    assert script.mutation_count == 1


@pytest.mark.parametrize(
    "mutant", ["wrong_root", "wrong_message", "wrong_call", "same_driver", "lost_history"]
)
@pytest.mark.parametrize("provider", ["gemini", "openai"])
async def test_history_real_driver_restart_and_independent_identity_mutants(
    provider: str, mutant: str
) -> None:
    await history_case(provider, mutant)


@pytest.mark.parametrize("provider", ["gemini", "openai"])
async def test_history_real_driver_restart(provider: str) -> None:
    await history_case(provider, None)


async def history_case(provider: str, mutant: str | None) -> None:
    if provider == "gemini":
        script = GeminiScript()
        scenario = await script.arrange("C05")
        driver = script.ma

        async def restart() -> ManagedAgents:
            if mutant == "same_driver":
                return driver
            storage = MemoryStorage() if mutant == "lost_history" else script.storage
            return GeminiManagedAgents(
                script, storage=storage, state_store=script.store, account_scope_id="project"
            )

        expectations = HistoryExpectations("root", "step:0", "call")
        sdk = None
    else:
        script = OpenAIScriptedTransport()
        sdk = build_async_openai(
            api_key="offline-fixture",
            base_url="https://openai.invalid/v1",
            transport=httpx.MockTransport(script.handle),
        )
        journal = MemoryRecoveryJournal()
        usage = MemoryUsageRevisions()

        def construct(journal: MemoryRecoveryJournal) -> OpenAIDriver:
            return OpenAIDriver(
                SDKTransport(sdk),
                account_scope_id="project",
                journal=journal,
                usage_revisions=usage,
                authorization=lambda scope, kind, id_: scope == SCOPE,
            )

        driver = construct(journal)
        script.attach(driver, sdk)
        scenario = await script.arrange("C05")

        async def restart() -> ManagedAgents:
            if mutant == "same_driver":
                return driver
            new_driver = construct(MemoryRecoveryJournal() if mutant == "lost_history" else journal)
            script.attach(new_driver, sdk)
            return new_driver

        expectations = HistoryExpectations("root", "item", "call")
    if mutant == "wrong_root":
        expectations = HistoryExpectations(
            "foreign-root", expectations.saved_message_item_id, "call"
        )
    elif mutant == "wrong_message":
        expectations = HistoryExpectations("root", "foreign-item", "call")
    elif mutant == "wrong_call":
        expectations = HistoryExpectations(
            "root", expectations.saved_message_item_id, "foreign-call"
        )
    try:
        if mutant:
            with pytest.raises((ConformanceFailure, ScopeViolation)):
                await collect_history_evidence(
                    driver, scenario.scope, scenario.session.ref, expectations, restart=restart
                )
        else:
            evidence = await collect_history_evidence(
                driver, scenario.scope, scenario.session.ref, expectations, restart=restart
            )
            assert evidence.saved == evidence.restarted
            assert evidence.gaps
    finally:
        if sdk is not None:
            await sdk.close()


@pytest.mark.parametrize("corrupt", [False, True])
async def test_gemini_snapshot_bytes_across_two_real_port_turns(corrupt: bool) -> None:
    import io
    import tarfile

    from daimon.testing.continuity_conformance import collect_snapshot_bytes
    from mux.contracts.actions import UserMessage
    from mux.contracts.events import TextPart
    from mux.contracts.ids import PageRequest

    script = GeminiScript()
    scenario = await script.arrange("C10")
    for i in range(3):
        native = script.native(f"native-root-{i}")
        script.responses.append(native)
        script.streams[f"native-root-{i}"] = [
            {"event_type": "interaction.completed", "event_id": f"end-{i}", "interaction": native}
        ]
    await script.ma.events.send(
        scenario.scope,
        scenario.session.ref,
        (UserMessage(content=(TextPart(text="bootstrap"),)),),
        key="bootstrap",
    )
    body = io.BytesIO()
    data = bytes(range(255 if corrupt else 256))
    with tarfile.open(fileobj=body, mode="w:gz") as archive:
        member = tarfile.TarInfo("seed.bin")
        member.size = len(data)
        archive.addfile(member, io.BytesIO(data))
    script.snapshots["workspace"] = body.getvalue()
    artifacts = await script.ma.artifacts.list(
        scenario.scope, scenario.session.ref, page=PageRequest()
    )
    if corrupt:
        with pytest.raises(ConformanceFailure, match="binary bytes changed"):
            await collect_snapshot_bytes(
                script.ma,
                scenario.scope,
                scenario.session,
                artifacts.data[0].ref,
                key_prefix="persistence",
            )
    else:
        samples = await collect_snapshot_bytes(
            script.ma,
            scenario.scope,
            scenario.session,
            artifacts.data[0].ref,
            key_prefix="persistence",
        )
        assert samples == (data,) * 3
        assert script.mutation_count == 3  # native workspace bootstrap, then the two probe inputs
        assert len(script.snapshot_reads) == 4  # list plus three downloads


@pytest.mark.parametrize("kind", ["expiry", "unexpected_loss"])
async def test_openai_actual_sdk_missing_session_and_unproved_expiry_remains_refused(
    kind: Literal["expiry", "unexpected_loss"],
) -> None:
    from datetime import UTC, datetime

    script = OpenAIScriptedTransport()
    lost = False

    async def handle(request: httpx.Request) -> httpx.Response:
        if lost and request.url.path == "/v1/agents/sessions/session":
            return httpx.Response(
                404, json={"error": {"type": "not_found", "message": "owned session missing"}}
            )
        return await script.handle(request)

    sdk = build_async_openai(
        api_key="offline-fixture",
        base_url="https://openai.invalid/v1",
        transport=httpx.MockTransport(handle),
    )
    driver = OpenAIDriver(
        SDKTransport(sdk),
        account_scope_id="project",
        journal=MemoryRecoveryJournal(),
        usage_revisions=MemoryUsageRevisions(),
        authorization=lambda scope, kind, id_: scope == SCOPE,
    )
    script.attach(driver, sdk)
    try:
        scenario = await script.arrange("C10")
        clock = ProbeClock(datetime(2026, 10, 10, tzinfo=UTC))

        async def lose():
            nonlocal lost
            lost = True

        probe = LossProbe(
            clock,
            clock() + timedelta(days=7),
            scenario.scope,
            scenario.session,
            driver,
            lambda: script.mutation_count,
            lose,
        )
        if kind == "expiry":
            # OpenAI has no proven native clock-expiry seam; a host clock alone
            # cannot invent native expiry, so this remains a provider-owner gap.
            with pytest.raises(ValueError, match="silently continued"):
                await collect_loss_evidence(probe, kind)
        else:
            evidence = await collect_loss_evidence(probe, kind)
            assert evidence.evidence == ("provider session is missing",)
            assert evidence.writes_after == evidence.writes_before == 0
    finally:
        await sdk.close()


def test_probe_clock_rejects_naive_and_backwards_time():
    from datetime import UTC, datetime

    with pytest.raises(ValueError, match="aware"):
        ProbeClock(datetime(2026, 10, 10))
    clock = ProbeClock(datetime(2026, 10, 10, tzinfo=UTC))
    with pytest.raises(ValueError, match="backwards"):
        clock.advance_to(clock() - timedelta(seconds=1))


@pytest.mark.parametrize("case", ["history", "loss"])
@pytest.mark.parametrize("name", ["OPENAI_PROJECT_ID", "OPENAI_ORG_ID"])
async def test_native_hook_sdk_refuses_ambient_configuration(
    monkeypatch: pytest.MonkeyPatch, case: str, name: str
) -> None:
    # A fresh fake mapping prevents operator value/credential reads.
    monkeypatch.setattr(os, "environ", {name: "offline-ambient-sentinel"})
    with pytest.raises(AmbientSDKConfigurationError):
        if case == "history":
            await history_case("openai", None)
        else:
            await test_openai_actual_sdk_missing_session_and_unproved_expiry_remains_refused(
                "unexpected_loss"
            )
