"""J2 offline E2E: a copy whose preparation fails is never told its provider isn't set up,
and the real cause is logged with fixed labels.

Smoke dce44708 J2 (request 01M5Q5PSNMF4SMTPNWC0NE96AX, fresh-eyes FINDINGS cause 3): a
fresh Gemini copy's first turn was refused at `read_preparation_counterpart`'s catch-all.
The person read "runs on Gemini, which isn't set up here yet" while Gemini was set up,
and the log replaced every inner cause with "redacted", so nobody could tell what failed.
A failure a retry can clear says busy; any other unproven one says Daimon couldn't get it
ready. The log names the class, our frame and the SQLSTATE, never the error's text.
"""

import json
from datetime import UTC, datetime

import pytest
from daimon.core.turn.admission import admit_impl
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.notices import RefusalNouns, admission_refusal_text
from daimon.core.turn_hold import RequesterOrg
from sqlalchemy.exc import OperationalError
from structlog.testing import capture_logs

from .test_backend_selection import (
    initial_backend as initial_backend,
)
from .test_backend_selection import (
    preference_for,
)
from .test_backend_selection import (
    selector_host as selector_host,
)
from .test_provider_dispatch import TENANT

NOUNS = RefusalNouns(scope="server", admin="a server admin", billing="`/billing`")
PRIVATE = "PRIVATE-credential-value https://private.invalid"


class _LockNotAvailable(Exception):
    """A driver error as asyncpg raises it: the SQLSTATE on the error."""

    sqlstate = "55P03"


def _lock_timeout() -> BaseException:
    return OperationalError(f"UPDATE ... {PRIVATE}", None, _LockNotAvailable(PRIVATE))


def _unknown() -> BaseException:
    return KeyError(PRIVATE)


def _session_busy() -> BaseException:
    from daimon.core.session_fence_retry import FenceUnavailable
    from daimon.core.turn.errors import SessionBusyError

    busy = SessionBusyError(pending_reasons=(PRIVATE,), retry_after=datetime.now(UTC))
    busy.__cause__ = FenceUnavailable(PRIVATE)
    return busy


def _connection_lost() -> BaseException:
    from daimon.core.errors import TurnError

    return TurnError(kind="connection_lost", message=PRIVATE)


@pytest.mark.parametrize("initial_backend", [None], indirect=True)
@pytest.mark.parametrize(
    ("failure", "cause", "labels"),
    [
        (_lock_timeout, "preparation_busy", {"class": "sqlalchemy.exc.OperationalError"}),
        (lambda: TimeoutError(PRIVATE), "preparation_busy", {"type": "unlisted"}),
        (_unknown, "preparation_failed", {"class": "builtins.KeyError"}),
        # The live J2 failure (fresh-eyes FINDINGS): the session was still busy.
        (_session_busy, "preparation_busy", {"class": "daimon.core.turn.errors.SessionBusyError"}),
        (_connection_lost, "preparation_busy", {"class": "daimon.core.errors.TurnError"}),
    ],
)
async def test_j2_a_preparation_that_fails_says_busy_or_failed_never_not_set_up(
    selector_host, monkeypatch, failure, cause, labels
):
    from daimon.core.turn import runtime_manifest

    host = selector_host
    service, agent, actor = preference_for(host)
    await service.set_backend(agent, "gemini", 0, actor)

    async def failing(_request):
        raise failure()

    monkeypatch.setattr(runtime_manifest, "prepare_source", failing)
    with (
        capture_logs() as logs,
        pytest.raises(AdmissionDenied, match="backend_unsupported") as refused,
    ):
        await admit_impl(
            host.deps,
            tenant_id=TENANT,
            requester_org=RequesterOrg.HOST,
            platform="slack",
            external_user_id="caller",
            channel_id="channel",
            thread_id="new",
            now=datetime.now(UTC),
            origin="chat",
        )
    assert (refused.value.cause, refused.value.provider) == (cause, "gemini")
    said = admission_refusal_text(
        "backend_unsupported",
        NOUNS,
        agent_name="owner-flash",
        cause=refused.value.cause,
        provider=refused.value.provider,
    )
    assert "isn't set up" not in said
    if cause == "preparation_busy":
        assert said == (
            "Daimon is busy right now and couldn't get owner-flash ready on Gemini.\n\n"
            "Try again in a minute."
        )
    else:
        assert said.startswith("Daimon couldn't get owner-flash ready on Gemini.\n\n")
    (named,) = [e for e in logs if e["event"] == "turn.preparation_counterpart_error"]
    assert named["transient"] is (cause == "preparation_busy")
    (first,) = named["causes"][:1]
    assert labels.items() <= first.items(), first
    if failure is _lock_timeout:
        assert first["sqlstate"] == "55P03"
    assert PRIVATE not in json.dumps(logs), "fixed labels only, never the error's text"
    assert host.wire.requests == [] and host.created == []
