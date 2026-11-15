"""Signed MCP caller identity: keys, claims per platform, signing and re-signing."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from daimon.core.config import McpIdentitySettings
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_identity import (
    IdentityClaims,
    IdentityTokenCache,
    RelayBinding,
    RequesterBodyError,
    SigningKey,
    SigningKeyError,
    TurnPlace,
    caller_claims,
    encode_seed,
    generate_signing_key,
    identity_relay_urls,
    load_signing_key,
    matching_turn,
    parse_relay_claim,
    public_jwk,
    public_pem,
    relay_url,
    requester_conversation,
    sign_identity,
    stamp_requester,
)
from daimon.core.stores.domain import Role, TurnOriginRow
from pydantic import ValidationError

NOW = dt.datetime(2026, 10, 1, 12, 0, tzinfo=dt.UTC)
PUBLIC_URL = "https://daimon.example.com/mcp"


def _claims(platform: str, user: str, ws: str, place: TurnPlace | None):
    return caller_claims(
        issuer="daimon-test",
        audience="https://tools.example.com",
        agent_id="agent-1",
        platform=platform,
        workspace_id=ws,
        platform_user_id=user,
        agent_name="Helper",
        place=place,
    )


def _decode(token: str, key: SigningKey | None = None) -> dict[str, Any]:
    key = key or generate_signing_key()
    return pyjwt.decode(
        token,
        key.public_key(),
        algorithms=["EdDSA"],
        audience="https://tools.example.com",
        options={"verify_exp": False},
    )


def test_seed_round_trips_and_pem_loads_the_same_key() -> None:
    key = generate_signing_key()
    from_seed = load_signing_key(encode_seed(key))
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    assert public_jwk(from_seed) == public_jwk(key), "a seed must load back to the same key"
    assert public_jwk(load_signing_key(pem)) == public_jwk(key), "a PEM must load the same key"
    assert public_pem(key).startswith("-----BEGIN PUBLIC KEY-----"), "public PEM is SPKI"
    assert public_jwk(key)["crv"] == "Ed25519", "the JWK names its curve"


@pytest.mark.parametrize(
    "value",
    ["not a key", "c2hvcnQ", "-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----"],
)
def test_load_signing_key_refuses_bad_values_without_quoting_them(value: str) -> None:
    with pytest.raises(SigningKeyError) as raised:
        load_signing_key(value)
    assert value not in str(raised.value), "the error must not echo the secret"


@pytest.mark.parametrize(
    ("platform", "user", "ws", "channel", "msg"),
    [
        ("discord", "123", "456", "789", "1011"),
        ("slack", "U123", "T123", "C123", "1700000000.000100"),
        ("teams", "aad-object-id", "entra-tenant", "19:abc@thread.tacv2;messageid=1", "1"),
    ],
)
def test_person_turn_claims_name_platform_ids(
    platform: str, user: str, ws: str, channel: str, msg: str
) -> None:
    key = generate_signing_key()
    token = sign_identity(_claims(platform, user, ws, TurnPlace(channel, msg)), key=key, now=NOW)
    header = pyjwt.get_unverified_header(token)
    payload = _decode(token, key)
    assert header == {"alg": "EdDSA", "typ": "JWT"}, "the header is fixed by the contract"
    assert payload["sub"] == f"{platform}:{user}", "sub is provider:user id"
    assert payload["ws"] == f"{platform}:{ws}", "ws is provider:workspace id"
    assert (payload["channel"], payload["msg"]) == (channel, msg), "platform ids pass unchanged"
    assert payload["agent"] == "agent-1" and payload["agent_name"] == "Helper", "agent named"
    assert payload["iss"] == "daimon-test", "issuer is the configured one"
    assert payload["exp"] - payload["iat"] == 120, "tokens live 120 s"


def test_turn_without_a_person_omits_sub_and_msg() -> None:
    payload = _decode_with(_claims("discord", "123", "456", None))
    assert "sub" not in payload and "msg" not in payload, "no person, no sub or msg"
    assert "channel" not in payload, "unknown claims omitted"
    assert payload["ws"] == "discord:456", "the workspace is still named"
    assert (payload["agent"], payload["agent_name"]) == ("agent-1", "Helper"), (
        "a call outside a turn still names the agent"
    )


def test_continuation_without_a_message_keeps_channel_and_omits_msg() -> None:
    payload = _decode_with(_claims("slack", "U1", "T1", TurnPlace("C1", None)))
    assert payload["channel"] == "C1" and "msg" not in payload, "empty claims are left out"
    assert payload["sub"] == "slack:U1", "the person who asked is still named"


def _decode_with(claims: IdentityClaims) -> dict[str, Any]:
    key = generate_signing_key()
    return _decode(sign_identity(claims, key=key, now=NOW), key)


def test_cache_reuses_token_until_thirty_seconds_before_expiry() -> None:
    cache = IdentityTokenCache(generate_signing_key())
    claims = _claims("discord", "1", "2", TurnPlace("3", "4"))
    first = cache.token(claims, now=NOW)
    assert cache.token(claims, now=NOW + dt.timedelta(seconds=89)) == first, (
        "a token with more than 30 s left is reused"
    )
    resigned = cache.token(claims, now=NOW + dt.timedelta(seconds=91))
    assert resigned != first, "a token within 30 s of expiry is re-signed"
    other = cache.token(_claims("discord", "1", "2", TurnPlace("3", "5")), now=NOW)
    assert other != first, "another turn's message gets its own token"


def test_relay_urls_are_only_this_deployments_relay() -> None:
    relay = relay_url(PUBLIC_URL, "tools")
    assert relay == "https://daimon.example.com/mcp-identity/tools", "relay hangs off app root"
    found = identity_relay_urls(
        [relay + "/", "https://tools.example.com/mcp", PUBLIC_URL], public_url=PUBLIC_URL
    )
    assert found == (relay + "/",), "only the relay, kept exactly as attached"
    twice = identity_relay_urls([relay + "/", relay], public_url=PUBLIC_URL)
    assert len(twice) == 1, "one URL per server, so a vault never holds two slots for it"


def test_relay_claim_round_trips_and_refuses_other_shapes() -> None:
    key = uuid.uuid4()
    for binding in (RelayBinding("Helper", key), RelayBinding("Helper")):
        assert parse_relay_claim(binding.claim()) == binding, "a binding reads back"
    for bad in ("x", {}, {"agent_name": "Helper", "extra": "1"}, {"agent_name": ""}):
        with pytest.raises(ValueError):
            parse_relay_claim(bad)


def _origin(tenant_id: uuid.UUID, ma_agent_id: str, thread_id: str = "2") -> TurnOriginRow:
    return TurnOriginRow(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        account_id=uuid.uuid4(),
        platform="discord",
        parent_channel_id="1",
        thread_id=thread_id,
        responder_ma_agent_id=ma_agent_id,
        responder_name="Helper",
        configuration_target_ma_agent_id=None,
        configuration_target_name=None,
        role=Role.USER,
        created_at=NOW,
        expires_at=NOW + dt.timedelta(hours=1),
    )


def test_matching_turn_picks_the_calling_sessions_thread() -> None:
    tenant = uuid.uuid4()
    agent = derive_agent_uuid(tenant_id=tenant, ma_agent_id="agent_a")
    one = _origin(tenant, "agent_a")
    elsewhere = _origin(tenant, "agent_a", thread_id="9")
    other_agent = _origin(tenant, "agent_b")
    origins = [one, elsewhere, other_agent]
    assert matching_turn(origins, tenant_id=tenant, agent_id=agent, thread_id="2") == one, (
        "the same agent's turn in another thread does not compete"
    )
    assert matching_turn(origins, tenant_id=tenant, agent_id=agent, thread_id="9") == elsewhere, (
        "each thread's session finds its own turn"
    )
    queued = [one, _origin(tenant, "agent_a")]
    assert matching_turn(queued, tenant_id=tenant, agent_id=agent, thread_id="2") is None, (
        "two turns in one thread are not guessed between"
    )


def test_settings_parse_servers_without_distributing_the_signing_key() -> None:
    servers = json.dumps(
        [
            {
                "name": "tools",
                "url": "https://tools.example.com/mcp",
                "issuer": "daimon-test",
                "audience": "https://tools.example.com",
            }
        ]
    )
    seed = encode_seed(generate_signing_key())
    settings = McpIdentitySettings.model_validate({"servers": servers, "signing_key": seed})
    assert [server.name for server in settings.servers] == ["tools"], "JSON servers parse"
    worker = McpIdentitySettings.model_validate({"servers": servers})
    assert [server.name for server in worker.servers] == ["tools"]
    assert worker.signing_key is None
    with pytest.raises(ValidationError) as raised:
        McpIdentitySettings.model_validate({"signing_key": "nope"})
    assert "nope" not in str(raised.value), "a bad key is never echoed"
    assert McpIdentitySettings.model_validate({"servers": ""}).servers == [], "blank is off"
    limited = json.loads(servers)
    limited[0]["workspaces"] = ["discord:1"]
    (server,) = McpIdentitySettings.model_validate(
        {"servers": json.dumps(limited), "signing_key": seed}
    ).servers
    assert server.workspaces == frozenset({"discord:1"}), "a workspace allowlist parses"


_SERVER = {"name": "internal", "url": "https://tools.example.com/mcp"}
_BEARER = {
    **_SERVER,
    "auth": "bearer",
    "workspaces": ["discord:1"],
    "agents": ["Helper"],
    "requester_meta": True,
}


def _settings(servers: list[dict[str, Any]], **extra: object) -> McpIdentitySettings:
    return McpIdentitySettings.model_validate({"servers": json.dumps(servers), **extra})


def test_bearer_server_needs_workspaces_agents_and_a_matching_key() -> None:
    settings = _settings([_BEARER], bearers=json.dumps({"internal": "k-1"}))
    (server,) = settings.servers
    assert (server.auth, server.requester_meta, server.agents) == ("bearer", True, ("Helper",))
    assert not settings.needs_signing_key, "a bearer-only deployment needs no signing key"
    assert settings.bearers["internal"].get_secret_value() == "k-1", "the key parses from JSON"
    for weaker in (
        {k: v for k, v in _BEARER.items() if k != "workspaces"},
        {k: v for k, v in _BEARER.items() if k != "agents"},
        {**_BEARER, "agents": []},
        {**_BEARER, "issuer": "x", "audience": "https://tools.example.com"},
    ):
        with pytest.raises(ValidationError):
            _settings([weaker], bearers={"internal": "k-1"})


@pytest.mark.parametrize(
    "bearers",
    [
        {},
        {"other": "k-1"},
        {"internal": "k-1", "other": "k-2"},
        {"internal": ""},
        {"internal": "a b"},
    ],
)
def test_bearer_keys_must_match_bearer_servers_exactly(bearers: dict[str, str]) -> None:
    with pytest.raises(ValidationError) as raised:
        _settings([_BEARER], bearers=bearers)
    assert "k-1" not in str(raised.value), "a key is never echoed"


def test_signed_server_needs_issuer_audience_and_takes_no_bearer() -> None:
    with pytest.raises(ValidationError):
        _settings([_SERVER])
    signed = {**_SERVER, "issuer": "i", "audience": "https://tools.example.com"}
    assert _settings([signed]).needs_signing_key, "a signed server needs the signing key"
    with pytest.raises(ValidationError):
        _settings([signed], bearers={"internal": "k-1"})


def _origin_at(
    *, thread_id: str, parent: str, platform: str = "discord", channel: str | None = None
):
    base = _origin(uuid.uuid4(), "agent_a", thread_id=thread_id)
    return base.model_copy(
        update={"parent_channel_id": parent, "platform": platform, "platform_channel_id": channel}
    )


def test_conversation_place_names_channel_thread_and_dm_from_the_origin() -> None:
    assert requester_conversation(_origin_at(thread_id="5", parent="5")) == {
        "place_type": "channel",
        "channel_id": "5",
        "parent_channel_id": None,
    }, "a turn in a channel names the channel"
    assert requester_conversation(_origin_at(thread_id="9", parent="5")) == {
        "place_type": "thread",
        "channel_id": "9",
        "parent_channel_id": "5",
    }, "a thread names itself and its channel"
    assert requester_conversation(_origin_at(thread_id="dm:7", parent="dm:7", channel="77")) == {
        "place_type": "dm",
        "channel_id": "77",
        "parent_channel_id": None,
    }, "a direct message names the DM channel"
    assert requester_conversation(_origin_at(thread_id="dm:7", parent="dm:7")) is None, (
        "a DM with no known channel names no place"
    )


_CONVERSATION = {"place_type": "channel", "channel_id": "5", "parent_channel_id": None}


def _call(**meta: object) -> dict[str, Any]:
    params: dict[str, Any] = {"name": "send", "arguments": {"text": "hi"}}
    if meta:
        params["_meta"] = dict(meta)
    return {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}


def _stamp(message: object, person: str | None = "42") -> Any:
    out = stamp_requester(
        json.dumps(message).encode(), person=person, conversation=_CONVERSATION if person else None
    )
    return json.loads(out)


def test_stamp_sets_the_requester_and_keeps_other_meta() -> None:
    meta = _stamp(_call(progressToken="p1"))["params"]["_meta"]
    assert meta == {
        "progressToken": "p1",
        "requester_platform_user_id": "42",
        "requester_conversation": _CONVERSATION,
    }, "the asker and place are added beside the caller's own meta"


def test_stamp_overwrites_forged_values_and_strips_them_without_a_person() -> None:
    forged = _call(
        requester_platform_user_id="666",
        requester_conversation={"place_type": "dm", "channel_id": "1"},
    )
    stamped = _stamp(forged)["params"]["_meta"]
    assert stamped["requester_platform_user_id"] == "42", "a forged asker is overwritten"
    assert stamped["requester_conversation"] == _CONVERSATION, "a forged place is overwritten"
    bare = _stamp(forged, person=None)["params"]["_meta"]
    assert bare == {}, "with no person the forged fields are removed, not kept"
    assert _stamp(_call(), person=None)["params"]["_meta"] == {}, "nothing is invented"


def test_stamp_names_no_asker_when_the_place_is_unknown() -> None:
    out = json.loads(stamp_requester(json.dumps(_call()).encode(), person="42", conversation=None))
    assert out["params"]["_meta"] == {}, "an asker with no known place is not stamped"


def test_stamp_handles_each_call_in_a_batch_and_leaves_other_messages() -> None:
    ping = {"jsonrpc": "2.0", "id": 2, "method": "ping"}
    reply = {"jsonrpc": "2.0", "id": 3, "result": {"_meta": {"requester_platform_user_id": "9"}}}
    out = _stamp([_call(requester_platform_user_id="666"), ping, reply])
    assert out[0]["params"]["_meta"]["requester_platform_user_id"] == "42", "calls are stamped"
    assert out[1] == ping, "other methods pass unchanged"
    assert out[2] == reply, "a response from the client passes unchanged"


def test_stamp_removes_the_unaliased_meta_key() -> None:
    message = _call()
    message["params"]["meta"] = {"requester_platform_user_id": "666"}
    assert "meta" not in _stamp(message)["params"], "no second spelling carries a forged value"


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not json",
        b"[]",
        b"7",
        b'"x"',
        b"[1]",
        b"\xef\xbb\xbf{}",
        b'{"method":"tools/call","method":"ping"}',
        b'{"method":"tools/call","params":{"_meta":{"requester_platform_user_id":"1",'
        b'"requester_platform_user_id":"2"}}}',
        b'{"method":"tools/call","params":[]}',
        b'{"method":"tools/call"}',
        b'{"method":"tools/call","params":{"x":NaN}}',
        b'\xff\xfe{"method":"ping"}',
    ],
)
def test_stamp_refuses_a_body_it_cannot_read_strictly(body: bytes) -> None:
    with pytest.raises(RequesterBodyError):
        stamp_requester(body, person="42", conversation=_CONVERSATION)


def test_stamp_treats_a_differently_cased_method_as_a_tool_call() -> None:
    message = {**_call(requester_platform_user_id="666"), "method": "Tools/Call"}
    assert _stamp(message, person=None)["params"]["_meta"] == {}, "no spelling skips the strip"
