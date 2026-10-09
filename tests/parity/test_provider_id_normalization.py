"""Runtime provider handles cannot destabilize the behavior oracle."""

from __future__ import annotations

import json
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest
from daimon.core.session_snapshot import SessionSnapshot, fingerprint_identity, fingerprint_mutable
from daimon.testing.effect_recorder import DB_TABLES, EffectRecorder, Normalizer, database_metadata
from daimon.testing.ma_transport import Json
from sqlalchemy.ext.asyncio import AsyncSession


def normalize_request(request: dict[str, Json]) -> Json:
    return Normalizer(provider_requests=(request,)).normalize(request)


@pytest.mark.parametrize(
    "prefix",
    (
        "memstore",
        "memver",
        "mem",
        "vlt",
        "vault",
        "env",
        "agent",
        "sesn",
        "sess",
        "session",
        "skill",
        "file",
        "sevt",
        "evt",
        "outc",
        "res",
        "ag",
        "ses",
        "toolu",
        "tu",
        "e",
        "m",
        "s",
    ),
)
def test_provider_handle_alias_is_shared_by_response_request_and_dedup_key(prefix: str) -> None:
    def capture(suffix: str) -> Json:
        identifier = f"{prefix}_{suffix}"
        return normalize_request(
            {
                "path": f"/v1/files/{identifier}/content",
                "reply": {"id": identifier},
                "request": {"file_id": identifier},
                "page": {"first_id": identifier, "last_id": identifier},
                "idempotency_key": f"turn:{identifier}:accepted",
                "text": f"Use {identifier} literally",
                "caller": {"id": identifier},
                "tenant_id": identifier,
                "account_id": identifier,
                "model_id": identifier,
            }
        )

    first = cast(dict[str, Json], capture("A"))
    second = cast(dict[str, Json], capture("B"))
    for key in ("path", "reply", "request", "page", "idempotency_key"):
        assert first[key] == second[key]
    assert first["reply"] == {"id": "<id:1>"}
    assert first["path"] == "/v1/files/<id:1>/content"
    assert first["idempotency_key"] == "turn:<id:1>:accepted"
    for key in ("text", "caller", "tenant_id", "account_id", "model_id"):
        assert first[key] != second[key], f"{key} is meaningful, not a provider handle"


def test_distinct_handles_and_meaningful_path_segments_remain_distinct() -> None:
    normalized = normalize_request(
        {
            "path": "/v1/files/file_one/content",
            "reply": {"id": "file_two"},
            "filename": "file_one",
            "memory_path": "/notes/file_recipe.txt",
        }
    )
    assert normalized == {
        "path": "/v1/files/<id:1>/content",
        "reply": {"id": "<id:2>"},
        "filename": "file_one",
        "memory_path": "/notes/file_recipe.txt",
    }


def test_aliases_follow_first_capture_appearance_not_generation_or_lexical_order() -> None:
    request: dict[str, Json] = {
        "path": "/v1/files/file_Z_generated_second/content",
        "reply": {"id": "file_A_generated_first"},
    }
    normalizer = Normalizer(
        runtime_ids=("file_A_generated_first", "file_Z_generated_second"),
        provider_requests=(request,),
    )
    assert normalizer.normalize(request) == {
        "path": "/v1/files/<id:1>/content",
        "reply": {"id": "<id:2>"},
    }
    assert normalizer.normalize(
        {"id": "memstore_generated_third", "memory_store_id": "memstore_generated_third"}
    ) == {"id": "<id:3>", "memory_store_id": "<id:3>"}


def test_encoded_sse_and_turn_controls_keep_payload_text_caller_and_timestamps() -> None:
    stamp = "2026-10-09T00:00:00Z"
    event = {
        "id": "sevt_random",
        "processed_at": stamp,
        "content": [{"text": "Literal memstore_random"}],
    }
    controls = {
        "responder": {"ma_agent_id": "agent_random", "name": "Literal agent_random"},
        "tenant_id": "memstore_literal_tenant",
    }
    normalized = cast(
        dict[str, Json],
        Normalizer().normalize(
            {
                "body": f"event: agent.message\ndata: {json.dumps(event)}\n\n",
                "text": f"<turn_controls>\n{json.dumps(controls)}\nKeep instructions exact.\n</turn_controls>",
                "event_id": "sevt_random",
            }
        ),
    )
    assert normalized["body"] == (
        'event: agent.message\ndata: {"id": "<id:1>", '
        f'"processed_at": "{stamp}", "content": [{{"text": "Literal memstore_random"}}]}}\n\n'
    )
    assert normalized["text"] == (
        '<turn_controls>\n{"responder": {"ma_agent_id": "<id:2>", '
        '"name": "Literal agent_random"}, "tenant_id": "memstore_literal_tenant"}\n'
        "Keep instructions exact.\n</turn_controls>"
    )
    assert normalized["event_id"] == "<id:1>"


def test_encoded_json_changes_only_id_tokens_preserving_wire_format_and_literal_duplicates() -> (
    None
):
    raw = (
        'event: agent.message\r\ndata: {"id" : "sevt_random", '
        '"number":1e2,"text":"\\u00e9 sevt_random", '
        '"caller":{"id":"sevt_random"}}\r\n\r\n'
    )
    assert Normalizer().normalize({"body": raw}) == {
        "body": raw.replace('"id" : "sevt_random"', '"id" : "<id:1>"')
    }


def test_query_ids_and_error_references_share_aliases_without_changing_keys_or_prose() -> None:
    assert normalize_request(
        {
            "path": "/v1/sessions/sess_B",
            "query": [["session_id", "sess_A"], ["session_id", "sess_B"], ["scope", "session"]],
            "error": {"type": "not_found_error", "message": "No such session: sess_A"},
            "message": "Literal sess_A",
        }
    ) == {
        "path": "/v1/sessions/<id:1>",
        "query": [["session_id", "<id:2>"], ["session_id", "<id:1>"], ["scope", "session"]],
        "error": {"type": "not_found_error", "message": "No such session: <id:2>"},
        "message": "Literal sess_A",
    }


def test_collection_names_and_url_query_keys_are_literal() -> None:
    assert normalize_request(
        {"url": "https://offline/v1/memory_stores?agent_id=agent_runtime"}
    ) == {"url": "https://offline/v1/memory_stores?agent_id=<id:1>"}
    assert normalize_request(
        {"url": "https://offline/v1/sessions/sess_B?session_id=sess_A&account_id=sess_B"}
    ) == {"url": "https://offline/v1/sessions/<id:1>?session_id=<id:2>&account_id=sess_B"}


@pytest.mark.parametrize("platform", ("anthropic", "ma_http"))
@pytest.mark.parametrize(
    ("field", "template"),
    (
        ("path", "/notes/{handle}"),
        ("path", "/v1/files/{handle}/content"),
        ("url", "https://github.com/org/{handle}"),
        ("url", "https://service.test/v1/files/{handle}/content?file_id={handle}"),
    ),
)
def test_full_recorder_keeps_literal_resource_paths_and_repo_urls_sensitive(
    platform: str, field: str, template: str
) -> None:
    def capture(handle: str) -> dict[str, Json]:
        recorder = EffectRecorder()
        recorder.record(
            platform,
            "request",
            {
                "method": "POST",
                "path": "/v1/sessions/sess_fixed/resources",
                "body": {field: template.format(handle=handle)},
            },
            result={"id": handle},
        )
        return json.loads(recorder.transcript())

    first, second = capture("file_alpha"), capture("file_beta")
    assert first != second, "literal content changes must remain visible when provider IDs rotate"
    first_effect = cast(list[dict[str, Json]], first["effects"])[0]
    second_effect = cast(list[dict[str, Json]], second["effects"])[0]
    assert first_effect["result"] == second_effect["result"] == {"id": "<id:2>"}
    first_payload = cast(dict[str, Json], first_effect["payload"])
    second_payload = cast(dict[str, Json], second_effect["payload"])
    assert first_payload["path"] == second_payload["path"] == "/v1/sessions/<id:1>/resources"
    assert first_payload["body"] == {field: template.format(handle="file_alpha")}
    assert second_payload["body"] == {field: template.format(handle="file_beta")}


@pytest.mark.parametrize("with_response", (False, True))
def test_full_recorder_configured_mcp_urls_never_become_provider_routes(
    with_response: bool,
) -> None:
    def capture(handle: str) -> str:
        recorder = EffectRecorder()
        recorder.record(
            "ma_http",
            "request",
            {
                "method": "POST",
                "path": "/v1/agents/agent_fixed",
                "body": {
                    "version": 1,
                    "mcp_servers": [
                        {"name": "configured", "url": f"https://service.test/v1/tools/{handle}"}
                    ],
                },
            },
            result={"id": "agent_fixed"} if with_response else None,
        )
        return recorder.transcript()

    first, second = capture("agent_alpha"), capture("agent_beta")
    assert first != second
    assert "https://service.test/v1/tools/agent_alpha" in first
    assert "https://service.test/v1/tools/agent_beta" in second


def test_unmarked_paths_urls_and_configured_queries_are_literal() -> None:
    value: dict[str, Json] = {
        "path": "/v1/files/file_alpha/content",
        "url": "https://service.test/v1/agents/agent_alpha",
        "query": [["file_id", "file_alpha"]],
        "reply": {"id": "file_alpha"},
    }
    normalizer = Normalizer()
    normalized = cast(dict[str, Json], normalizer.normalize(value))
    assert normalized["path"] == value["path"]
    assert normalized["url"] == value["url"]
    assert normalized["query"] == value["query"]
    assert normalizer.provider_ids == {"file_alpha"}
    assert normalizer.ids == {"file_alpha": "<id:1>"}


def test_api_route_preserves_literal_secret_names_and_unknown_collection_paths() -> None:
    request: dict[str, Json] = {
        "url": "https://agent_alpha.test/v1/vaults/vlt_alpha/secrets/agent_alpha",
        "body": {"agent_id": "agent_alpha"},
    }
    assert normalize_request(request) == {
        "url": "https://agent_alpha.test/v1/vaults/<id:1>/secrets/agent_alpha",
        "body": {"agent_id": "<id:2>"},
    }
    request = {
        "path": "/v1/tools/agent_alpha",
        "body": {"agent_id": "agent_alpha"},
    }
    assert normalize_request(request) == {
        "path": "/v1/tools/agent_alpha",
        "body": {"agent_id": "<id:1>"},
    }


def test_full_recorder_standalone_transport_routes_and_configured_body_urls_differ() -> None:
    def capture(handle: str) -> dict[str, Json]:
        return json.loads(
            EffectRecorder().transcript(
                requests=[
                    {
                        "method": "POST",
                        "path": f"/v1/agents/{handle}",
                        "query": [["agent_id", handle]],
                        "body": {"url": f"https://service.test/v1/agents/{handle}"},
                    }
                ]
            )
        )

    first, second = capture("agent_alpha"), capture("agent_beta")
    assert first != second
    requests = cast(list[dict[str, Json]], first["requests"])
    assert requests[0]["path"] == "/v1/agents/<id:1>"
    assert requests[0]["query"] == [["agent_id", "<id:1>"]]
    assert requests[0]["body"] == {"url": "https://service.test/v1/agents/agent_alpha"}


def snapshot(memory: str) -> SessionSnapshot:
    return SessionSnapshot(
        ma_agent_id="agent_runtime",
        model_id="claude-sonnet-4-6",
        system_sha256="literal-system-digest",
        skills_sha256="literal-skills-digest",
        environment_id="env_runtime",
        repo_url=None,
        repo_branch=None,
        memory_store_id=memory,
        vault_id="vlt_runtime",
        tools_sha256="literal-tools-digest",
        mcp_servers_sha256="literal-mcp-digest",
        env_sha256=None,
        agent_version=1,
        agent_name="caller-configured-name",
    )


def capture_snapshot(config: SessionSnapshot, *, fingerprint: str | None = None) -> str:
    return EffectRecorder().transcript(
        database={
            "thread_sessions": [
                {
                    "effective_config": config.model_dump(mode="json"),
                    "identity_fingerprint": (
                        fingerprint if fingerprint is not None else fingerprint_identity(config)
                    ),
                    "mutable_fingerprint": fingerprint_mutable(config),
                }
            ]
        }
    )


def test_derived_fingerprints_use_the_same_aliases_and_keep_configuration_sensitivity() -> None:
    original = snapshot("memstore_random_A")
    rotated = snapshot("memstore_random_B")
    assert fingerprint_identity(original) != fingerprint_identity(rotated)
    first = capture_snapshot(original)
    assert first == capture_snapshot(rotated)
    row = json.loads(first)["database"]["thread_sessions"][0]
    recorded = SessionSnapshot.model_validate(row["effective_config"])
    assert row["identity_fingerprint"] == fingerprint_identity(recorded)
    assert row["mutable_fingerprint"] == fingerprint_mutable(recorded)
    for change in (
        {"memory_read_only": True},
        {"model_id": "claude-opus-4-6"},
        {"skills_sha256": "different-skills"},
        {"tools_sha256": "different-tools"},
        {"repo_url": "https://example.org/other-repo"},
    ):
        assert capture_snapshot(original.model_copy(update=change)) != first


def test_an_incorrect_stored_fingerprint_is_not_silently_repaired() -> None:
    with pytest.raises(ValueError, match="identity_fingerprint does not match"):
        capture_snapshot(snapshot("memstore_random"), fingerprint="incorrect-stored-digest")


async def test_provider_rotation_cannot_change_database_row_order_or_alias_assignment() -> None:
    async def capture(reverse: bool) -> str:
        rows: list[dict[str, object]] = []
        for index, model in enumerate(("model_A", "model_B")):
            memory = ("memstore_Z", "memstore_A")[index ^ reverse]
            config = snapshot(memory).model_copy(update={"model_id": model})
            row: dict[str, object] = {
                column.name: None
                for column in database_metadata().tables["thread_sessions"].columns
            }
            row.update(
                id=f"db_{index}",
                tenant_id="literal-tenant",
                effective_config=config.model_dump(mode="json"),
                identity_fingerprint=fingerprint_identity(config),
                mutable_fingerprint=fingerprint_mutable(config),
            )
            rows.append(row)
        results: list[Mock] = []
        for table in DB_TABLES:
            result = Mock()
            result.mappings.return_value = rows if table == "thread_sessions" else []
            results.append(result)
        session = cast(AsyncSession, AsyncMock(execute=AsyncMock(side_effect=results)))
        recorder = EffectRecorder()
        database = await recorder.database(session)
        ordered = cast(list[dict[str, object]], database["thread_sessions"])
        assert [
            cast(dict[str, object], row["effective_config"])["model_id"] for row in ordered
        ] == [
            "model_A",
            "model_B",
        ]
        return recorder.transcript(database=database)

    assert await capture(False) == await capture(True)
