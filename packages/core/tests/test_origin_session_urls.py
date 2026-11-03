"""Identity vault slots stay on Daimon's endpoint and survive session re-gating."""

import uuid

import pytest
from daimon.core.mcp_server_url import is_session_mcp_url, session_mcp_url
from daimon.core.tool_safety import ToolSafetyPolicy, heal_reserved_server


@pytest.mark.parametrize("base", ["https://mcp.example/mcp", "https://mcp.example/mcp?mode=chat"])
def test_session_slot_survives_regating_but_never_trusts_another_endpoint(base: str) -> None:
    selected = session_mcp_url(base, str(uuid.uuid4()))
    assert is_session_mcp_url(selected, base)
    servers = [{"name": "daimon-mcp", "type": "url", "url": selected}]
    assert heal_reserved_server(ToolSafetyPolicy(enabled=True), servers, public_url=base) is None
    foreign = selected.replace("mcp.example", "foreign.example")
    assert not is_session_mcp_url(foreign, base)
    healed = heal_reserved_server(
        ToolSafetyPolicy(enabled=True),
        [{"name": "daimon-mcp", "type": "url", "url": foreign}],
        public_url=base,
    )
    assert healed is not None and healed[0]["url"] == base


@pytest.mark.parametrize(
    "query",
    [
        "daimon_session=bad",
        "daimon_session=",
        "daimon_session={id}&daimon_session={id}",
        "daimon_session={id}&extra=true",
    ],
)
def test_a_session_selector_does_not_trust_arbitrary_url_queries(query: str) -> None:
    base = "https://mcp.example/mcp"
    assert not is_session_mcp_url(f"{base}?{query.format(id=uuid.uuid4())}", base)
