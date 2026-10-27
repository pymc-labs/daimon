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
    sign_identity,
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


def test_settings_parse_servers_and_require_a_key() -> None:
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
    with pytest.raises(ValidationError, match="SIGNING_KEY"):
        McpIdentitySettings.model_validate({"servers": servers})
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
