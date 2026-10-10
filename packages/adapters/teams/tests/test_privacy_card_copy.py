"""The Teams privacy preview uses the same words as the other adapters."""

# pyright: reportPrivateUsage=false

from daimon.adapters.teams.privacy_card import _will_happen
from daimon.core.privacy import PurgePreview, PurgePreviewRow


def test_delete_preview_uses_plain_rows_without_routine_id() -> None:
    empty = PurgePreviewRow(count=0, example=None)
    values = {name: empty for name in PurgePreview.model_fields}
    values.update(
        routines=PurgePreviewRow(count=2, example="routine-uuid"),
        user_configs=PurgePreviewRow(count=1, example=None),
        github_credentials=PurgePreviewRow(count=1, example="octocat"),
    )
    rows = _will_happen(PurgePreview(**values))
    assert "⏰ Cancel **2** routines" in rows
    assert "⚙ Remove your saved settings" in rows
    assert "🔑 Delete **1** saved GitHub key, for example the one for octocat" in rows
    assert "routine-uuid" not in "\n".join(rows)
