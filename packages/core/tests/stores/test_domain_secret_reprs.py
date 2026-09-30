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
