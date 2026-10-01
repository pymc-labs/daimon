"""Row models that carry secrets keep them out of repr (logs, error-tracker frames)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from daimon.core.stores.domain import AgentFileRow


def test_agent_file_row_repr_hides_the_decrypted_value() -> None:
    now = datetime.now(UTC)
    row = AgentFileRow(
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        key="CRM_TOKEN",
        content="canary-secret-value-91c2",
        created_at=now,
        updated_at=now,
    )

    assert "canary-secret-value-91c2" not in repr(row)
    assert "canary-secret-value-91c2" not in str(row)
    assert "CRM_TOKEN" in repr(row)
    assert row.content == "canary-secret-value-91c2"


def test_credential_request_and_oauth_flow_reprs_hide_their_secrets() -> None:
    from daimon.core.stores.domain import CredentialRequestRow, McpOAuthFlowRow

    for model, secret_fields in (
        (CredentialRequestRow, ("token",)),
        (McpOAuthFlowRow, ("request_token", "state", "code_verifier")),
    ):
        for field in secret_fields:
            assert model.model_fields[field].repr is False, f"{model.__name__}.{field}"


def test_env_entry_repr_hides_the_value() -> None:
    from daimon.core.env_file import EnvEntry

    entry = EnvEntry(name="CRM_TOKEN", value="canary-env-value-5d1e", line=3)

    assert "canary-env-value-5d1e" not in repr(entry)
    assert "CRM_TOKEN" in repr(entry)
