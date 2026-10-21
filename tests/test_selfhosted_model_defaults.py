"""Default drift checks run in core CI without the Bicep toolchain."""


async def test_python_production_model_defaults_agree() -> None:
    import inspect
    from unittest.mock import Mock

    from daimon.adapters.cli.commands.selfhosted import selfhosted_init_command
    from mux.contracts.ids import Scope
    from mux.drivers.selfhosted import SelfHostedDriver
    from mux.profiles import backend_selection

    expected = "gpt-6.1-sol"
    assert inspect.signature(selfhosted_init_command).parameters["model"].default == expected
    assert backend_selection("selfhosted").model == expected
    # Inspect only the models port: no turn, transport or model request is made.
    driver = SelfHostedDriver(
        Mock(),
        account_scope_id="test",
        agents=Mock(),
        environments=Mock(),
        authorization=lambda *args: False,
    )
    scope = Scope(tenant_id="test", account_id="test", principal_id="test", authorization_id="test")
    assert [info.model.id for info in await driver.models.list(scope, "selfhosted")] == [expected]
