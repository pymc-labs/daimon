from daimon.core.turn.lifecycle import acknowledge
from structlog.testing import capture_logs


async def test_existing_adapter_without_hook_is_safe():
    await acknowledge(object(), "accepted")
    await acknowledge(object(), "done")


async def test_signals_and_delivery_failure_are_best_effort():
    phases = []

    class Adapter:
        async def on_acknowledgment(self, phase):
            phases.append(phase)
            if phase == "done":
                raise OSError("reaction permission denied")

    adapter = Adapter()
    await acknowledge(adapter, "accepted")
    with capture_logs() as logs:
        await acknowledge(adapter, "done")
    assert logs == [
        {
            "phase": "done",
            "error_type": "OSError",
            "event": "turn.acknowledgment_failed",
            "log_level": "debug",
        }
    ]
    assert phases == ["accepted", "done"]
