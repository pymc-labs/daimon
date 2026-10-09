"""Explicit native create nulls retain their SDK requests; neutral omissions stay omitted."""

from collections import deque

import httpx
import pytest
from daimon.core.mux_backend import resource_scope
from daimon.core.mux_compat import create_environment
from daimon.testing.ma_models import ma_environment
from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport
from mux.contracts.resources import EnvironmentSpec
from mux.drivers.anthropic.resources.environments import environment_payload
from mux.drivers.anthropic.schemas import EnvironmentConfig
from pydantic import ValidationError


@pytest.mark.parametrize("description", ["omitted", None, "source description"])
@pytest.mark.parametrize("config", ["omitted", None, {"type": "self_hosted"}])
async def test_environment_create_keeps_explicit_description_null_and_configuration(
    description, config
):
    payload = {"name": "fork"}
    if description != "omitted":
        payload["description"] = description
    if config != "omitted":
        payload["config"] = config
    response = ma_environment(id="environment", name="fork", tenant_id="tenant").model_dump(
        mode="json"
    )
    response["description"] = None
    old, new = [
        ScriptedTransport(
            deque([ScriptedReply("POST", "/v1/environments", httpx.Response(200, json=response))])
        )
        for _ in range(2)
    ]
    async with old.client() as before, new.client() as after:
        expected = await before.beta.environments.create(**payload)
        actual = await create_environment(after, payload, scope=resource_scope(tenant_id="tenant"))
    assert actual.id == expected.id
    assert actual.description is None
    assert [r.to_dict() for r in new.requests] == [r.to_dict() for r in old.requests]
    old.assert_consumed()
    new.assert_consumed()


def test_neutral_environment_description_none_stays_omitted_and_null_marker_is_closed():
    assert environment_payload(EnvironmentSpec(name="neutral", description=None)) == {
        "name": "neutral"
    }
    with pytest.raises(ValidationError):
        EnvironmentConfig.model_validate({"create_nulls": ["metadata"]})
