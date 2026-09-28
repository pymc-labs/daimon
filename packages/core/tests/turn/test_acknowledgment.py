from daimon.core.turn.lifecycle import acknowledge


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
    await acknowledge(adapter, "done")
    assert phases == ["accepted", "done"]
