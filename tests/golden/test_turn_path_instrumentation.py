"""Child-only probe for the real oracle's mocked run-turn recorder."""

from decimal import Decimal
from unittest.mock import AsyncMock


async def test_controls_are_forwarded_without_erasing_semantic_kwargs() -> None:
    turn = AsyncMock(name="run_turn", return_value=None)
    backend, scope, session_ref = object(), object(), object()
    await turn(
        path="mux",
        backend=backend,
        scope=scope,
        session_ref=session_ref,
        session_id="sess_probe",
        user_message="Literal /notes/file_alpha",
        model_id="caller-model",
        price=Decimal("0.0000001234"),
        continuity="history",
    )
    # Filtering is a recorder policy; the actual caller still receives all DI.
    assert turn.call_args.kwargs["backend"] is backend
    assert turn.call_args.kwargs["scope"] is scope
    assert turn.call_args.kwargs["session_ref"] is session_ref
    assert turn.call_args.kwargs["path"] == "mux"
