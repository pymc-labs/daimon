"""Tests for privacy_panel/submit.py.

Covers:
- evaluate_delete_submission with mismatched username: proceed=False, errors response,
  DB rows remain intact (no purge triggered)
- evaluate_delete_submission with matching username: proceed=True, update response,
  account_id and view_id populated
- run_purge_and_update: account rows deleted from DB; views.update called with the
  post-delete counts view
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import MagicMock

import httpx
import yarl
from daimon.adapters.slack.privacy_panel.submit import (
    evaluate_delete_submission,
    run_purge_and_update,
)
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core._models import Account, PlatformPrincipal
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from daimon.testing.ma import MARouter, build_fake_anthropic, list_response
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# ---------------------------------------------------------------------------
# Payload helpers
# ---------------------------------------------------------------------------


def _make_view_submission_payload(
    *,
    account_id: uuid.UUID,
    expected_name: str,
    typed_name: str,
    view_id: str = "V_TEST",
) -> dict[str, Any]:
    """Build a minimal view_submission payload for the privacy delete modal."""
    private_metadata = json.dumps(
        {
            "account_id": str(account_id),
            "user_name": expected_name,
            "view_id": view_id,
        },
        separators=(",", ":"),
    )
    return {
        "type": "view_submission",
        "view": {
            "id": view_id,
            "callback_id": "privacy_delete",
            "private_metadata": private_metadata,
            "state": {
                "values": {
                    "confirm_name_block": {
                        "confirm_name": {
                            "type": "plain_text_input",
                            "value": typed_name,
                        }
                    }
                }
            },
        },
        "user": {"id": "U_TEST_SUBMIT", "username": expected_name},
        "team": {"id": "T_TEST_SUBMIT"},
    }


# ---------------------------------------------------------------------------
# Tests for evaluate_delete_submission (pure)
# ---------------------------------------------------------------------------


async def test_evaluate_delete_submission_mismatch_returns_errors_and_proceed_false(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Mismatched typed name → errors response, proceed=False, no purge (rows intact)."""
    tenant = await make_tenant(db_session, platform="slack", workspace_id="T_SUB_01")
    account = await make_account(db_session, tenant=tenant)
    await make_platform_principal(
        db_session,
        platform="slack",
        external_id="U_SUB_01",
        tenant=tenant,
        account=account,
    )
    await db_session.commit()

    payload = _make_view_submission_payload(
        account_id=account.id,
        expected_name="alice",
        typed_name="bob",  # mismatch
        view_id="V_SUB_01",
    )

    decision = evaluate_delete_submission(payload)

    assert decision.proceed is False, "mismatch should return proceed=False"
    assert decision.account_id is None, "mismatch should return account_id=None"
    assert decision.view_id is None, "mismatch should return view_id=None"
    assert decision.response_payload.get("response_action") == "errors", (
        "mismatch should return response_action=errors"
    )
    assert "confirm_name_block" in (decision.response_payload.get("errors") or {}), (
        "errors dict should contain the input block_id"
    )

    # Verify DB rows are intact — evaluate_delete_submission is pure, so no purge.
    pp_count = (
        await db_session.execute(
            select(func.count()).select_from(PlatformPrincipal)  # type: ignore[arg-type]
        )
    ).scalar_one()
    assert pp_count > 0, "platform principal must still exist after a mismatch (no purge)"

    account_count = (
        await db_session.execute(
            select(func.count()).select_from(Account)  # type: ignore[arg-type]
        )
    ).scalar_one()
    assert account_count > 0, "account row must still exist after a mismatch (no purge)"


async def test_evaluate_delete_submission_match_returns_update_and_proceed_true() -> None:
    """Matching typed name → update response, proceed=True, account_id + view_id set."""
    account_id = uuid.uuid4()
    payload = _make_view_submission_payload(
        account_id=account_id,
        expected_name="alice",
        typed_name="alice",  # match
        view_id="V_SUB_MATCH",
    )

    decision = evaluate_delete_submission(payload)

    assert decision.proceed is True, "match should return proceed=True"
    assert decision.account_id == account_id, "match should return the parsed account_id"
    assert decision.view_id is not None, "match should return a view_id"
    assert decision.response_payload.get("response_action") == "update", (
        "match should return response_action=update"
    )
    # The update payload should contain a 'view' (the "Deleting..." view).
    assert "view" in decision.response_payload, (
        "update response must include a view dict (the Deleting… view)"
    )


async def test_evaluate_delete_submission_empty_typed_name_returns_errors() -> None:
    """Empty typed name is always a mismatch → errors response."""
    account_id = uuid.uuid4()
    payload = _make_view_submission_payload(
        account_id=account_id,
        expected_name="alice",
        typed_name="",  # empty
        view_id="V_EMPTY",
    )

    decision = evaluate_delete_submission(payload)

    assert decision.proceed is False, "empty typed name should return proceed=False"
    assert decision.response_payload.get("response_action") == "errors", (
        "empty typed name should return errors response"
    )


# ---------------------------------------------------------------------------
# Tests for run_purge_and_update (async, I/O)
# ---------------------------------------------------------------------------


async def test_run_purge_and_update_deletes_account_rows_and_calls_views_update(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """run_purge_and_update purges the account from DB and calls views.update."""
    tenant = await make_tenant(db_session, platform="slack", workspace_id="T_SUB_02")
    account = await make_account(db_session, tenant=tenant)
    await make_platform_principal(
        db_session,
        platform="slack",
        external_id="U_SUB_02",
        tenant=tenant,
        account=account,
    )
    await db_session.commit()

    # Fake Anthropic that returns an empty agents list (no MA sessions to delete).
    router = MARouter()

    def handle_agents_list(request: httpx.Request, match: Any) -> httpx.Response:  # noqa: ANN401
        return list_response([])

    router.add("GET", r"/v1/agents", handle_agents_list)
    fake_anthropic = build_fake_anthropic(router.dispatch)

    settings = MagicMock()
    settings.crypto.keys = (SecretStr("placeholder"),)
    runtime = SlackRuntime(
        settings=settings,
        anthropic=fake_anthropic,
        sessionmaker=db_session_factory,
        http_client=MagicMock(spec=httpx.AsyncClient),
    )

    await run_purge_and_update(
        runtime,
        fake_slack_web_client.client,
        account_id=account.id,
        tenant_id=tenant.id,
        platform_user_id="U_SUB_02",
        view_id="V_SUB_02_PURGE",
    )

    # DB: platform principal and account should be gone after purge.
    pp_count = (
        await db_session.execute(
            select(func.count()).select_from(PlatformPrincipal)  # type: ignore[arg-type]
        )
    ).scalar_one()
    assert pp_count == 0, "run_purge_and_update must delete the platform principal row"

    account_count = (
        await db_session.execute(
            select(func.count()).select_from(Account)  # type: ignore[arg-type]
        )
    ).scalar_one()
    assert account_count == 0, "run_purge_and_update must delete the account row"

    # views.update must have been called (post-delete status view).
    views_update_key = ("POST", yarl.URL("https://slack.com/api/views.update"))
    assert views_update_key in fake_slack_web_client.mock.requests, (
        "run_purge_and_update must call views.update with the post-delete view"
    )


async def test_run_purge_and_update_aborts_when_account_does_not_match_submitter(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """The account_id in private_metadata must be re-verified against the
    authenticated submitter. A mismatch (metadata pointing at someone else's
    account) must NOT purge — the destructive key is anchored to identity."""
    tenant = await make_tenant(db_session, platform="slack", workspace_id="T_SUB_03")
    account = await make_account(db_session, tenant=tenant)
    await make_platform_principal(
        db_session,
        platform="slack",
        external_id="U_SUB_03",
        tenant=tenant,
        account=account,
    )
    await db_session.commit()

    settings = MagicMock()
    settings.crypto.keys = (SecretStr("placeholder"),)
    runtime = SlackRuntime(
        settings=settings,
        anthropic=build_fake_anthropic(MARouter().dispatch),
        sessionmaker=db_session_factory,
        http_client=MagicMock(spec=httpx.AsyncClient),
    )

    # Submitter U_SUB_03 resolves to `account`, but metadata claims a foreign id.
    foreign_account_id = uuid.uuid4()
    await run_purge_and_update(
        runtime,
        fake_slack_web_client.client,
        account_id=foreign_account_id,
        tenant_id=tenant.id,
        platform_user_id="U_SUB_03",
        view_id="V_SUB_03_ABORT",
    )

    account_count = (
        await db_session.execute(
            select(func.count()).select_from(Account)  # type: ignore[arg-type]
        )
    ).scalar_one()
    assert account_count == 1, (
        "purge must be refused when the metadata account_id does not match the "
        "account resolved from the authenticated submitter"
    )
