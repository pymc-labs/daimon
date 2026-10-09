"""Closed privileged input schema and exact native batch ordering."""

from __future__ import annotations

import pickle

import pytest
from daimon.testing.ma_transport import ScriptedTransport
from mux.contracts.actions import NativeInput, UserMessage
from mux.contracts.events import TextPart
from mux.contracts.extensions import ExtensionConfig
from mux.drivers.anthropic import AnthropicManagedAgents
from mux.drivers.anthropic.actions import SystemMessageConfig, translate_inputs
from mux.errors import ExtensionVersionError, UnsupportedCapability
from pydantic import JsonValue, ValidationError


def _system(*, version: int = 1, **extra: JsonValue) -> NativeInput:
    return NativeInput(
        extension=ExtensionConfig(
            namespace="anthropic.session_system_message",
            version=version,
            value={"content": [{"type": "text", "text": "host framing"}], **extra},
        )
    )


def _user() -> UserMessage:
    return UserMessage(content=(TextPart(text="user input"),))


def test_privileged_system_config_roundtrips_json_and_pickle() -> None:
    config = SystemMessageConfig(content=(TextPart(text="host framing"),))
    assert SystemMessageConfig.model_validate_json(config.model_dump_json()) == config
    assert pickle.loads(pickle.dumps(config)) == config
    assert translate_inputs((_user(), _system())) == [
        {"type": "user.message", "content": [{"type": "text", "text": "user input"}]},
        {"type": "system.message", "content": [{"type": "text", "text": "host framing"}]},
    ]


async def test_factory_advertises_closed_system_message_schema_without_io() -> None:
    transport = ScriptedTransport()
    async with transport.client() as client:
        profile = AnthropicManagedAgents(client).capabilities()
        offered = [
            extension.version
            for extension in profile.extensions
            if extension.namespace == "anthropic.session_system_message"
        ]
    assert offered == [1]
    assert transport.requests == []


@pytest.mark.parametrize("case", ["first", "not_final", "duplicate", "reused_object"])
def test_system_message_requires_one_final_event_following_user_input(case: str) -> None:
    system = _system()
    batches = {
        "first": (system, _user()),
        "not_final": (_user(), system, _user()),
        "duplicate": (_user(), system, _system()),
        "reused_object": (_user(), system, _user(), system),
    }
    with pytest.raises(ValueError):
        translate_inputs(batches[case])


def test_unknown_native_schema_and_version_are_rejected() -> None:
    with pytest.raises(ExtensionVersionError):
        translate_inputs((_user(), _system(version=2)))
    unknown = NativeInput(extension=ExtensionConfig(namespace="anthropic.arbitrary", version=1))
    with pytest.raises(UnsupportedCapability):
        translate_inputs((unknown,))


def test_system_schema_rejects_sdk_kwargs_and_non_text_content() -> None:
    with pytest.raises(ValidationError):
        translate_inputs((_user(), _system(timeout=30)))
    image = NativeInput(
        extension=ExtensionConfig(
            namespace="anthropic.session_system_message",
            version=1,
            value={"content": [{"type": "image", "data": "payload"}]},
        )
    )
    with pytest.raises(ValidationError):
        translate_inputs((_user(), image))
