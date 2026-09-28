import pytest
from daimon.core.context_prompt import DEFAULT_FRAGMENTS, context_prompt
from daimon.core.specs import AgentSpec, dump_agent_spec


@pytest.mark.parametrize("origin", ["chat", "routine", "relay", "handoff"])
def test_each_origin_selects_its_fragment(origin):
    result = context_prompt(origin)
    if origin == "chat":
        assert result == ""
    else:
        assert DEFAULT_FRAGMENTS[origin] in result
        assert f'origin="{origin}"' in result
        for other, text in DEFAULT_FRAGMENTS.items():
            if other != origin and text:
                assert text not in result


@pytest.mark.parametrize("mode", ["replace", "extend"])
def test_spec_roundtrip_selects_override(mode):
    spec = AgentSpec.model_validate(
        {
            "name": "test",
            "model": "claude-sonnet-4-6",
            "system": "Base instructions.",
            "context_fragments": {"routine": {"mode": mode, "text": "Custom routine guidance."}},
        }
    )
    payload = dump_agent_spec(spec)
    assert "context_fragments" not in payload
    assert payload["system"].startswith("Base instructions.")
    fragment = context_prompt("routine", system=payload["system"])
    assert "Custom routine guidance." in fragment
    assert (DEFAULT_FRAGMENTS["routine"] in fragment) == (mode == "extend")
    assert context_prompt("chat", system=payload["system"]) == ""


def test_empty_override_and_manual_prompt_edits_are_safe():
    from daimon.core.context_prompt import ContextFragment, encode_fragments

    system = encode_fragments("Base", {"routine": ContextFragment(text="")})
    assert context_prompt("routine", system=system + "\nAdditional instructions") == ""
    assert context_prompt("chat", system=system) == ""
    damaged = system.split('{"routine"', 1)[0] + "invalid json"
    assert DEFAULT_FRAGMENTS["routine"] in context_prompt("routine", system=damaged)


def test_chat_can_opt_into_custom_guidance():
    from daimon.core.context_prompt import ContextFragment, encode_fragments

    system = encode_fragments("Base", {"chat": ContextFragment(text="Keep replies short.")})
    assert "Keep replies short." in context_prompt("chat", system=system)
    assert "Keep replies short." not in context_prompt("routine", system=system)
