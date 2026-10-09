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
        return Normalizer().normalize(
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
    normalized = Normalizer().normalize(
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
    normalizer = Normalizer(runtime_ids=("file_A_generated_first", "file_Z_generated_second"))
    assert normalizer.normalize(
        {
            "path": "/v1/files/file_Z_generated_second/content",
            "reply": {"id": "file_A_generated_first"},
        }
    ) == {
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
    assert Normalizer().normalize(
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
    assert Normalizer().normalize(
        {"url": "https://offline/v1/memory_stores?agent_id=agent_runtime"}
    ) == {"url": "https://offline/v1/memory_stores?agent_id=<id:1>"}
    assert Normalizer().normalize(
        {"url": "https://offline/v1/sessions/sess_B?session_id=sess_A&account_id=sess_B"}
    ) == {"url": "https://offline/v1/sessions/<id:1>?session_id=<id:2>&account_id=sess_B"}


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
