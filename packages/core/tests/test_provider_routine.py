"""`at_fire_place`: a provider routine fire is held and sealed where it posts, as on Claude."""

from __future__ import annotations

import uuid

from daimon.core.authz import AgentRef, Place, Surface, build_subject
from daimon.core.provider_routine import at_fire_place
from daimon.core.rule_views import RoutineOrigin
from daimon.core.scope import ResolvedConfig
from daimon.core.turn.admission import Admission, AdmissionGrant
from daimon.core.turn_hold import TurnHold
from daimon.testing.ma_models import ma_agent, ma_environment

TENANT = uuid.UUID(int=7)


def _admitted() -> Admission:
    """What admission gives a fire into the channel: no hold, the channel's seal only."""
    return Admission(
        account_id=uuid.UUID(int=8),
        agent=ma_agent(id="ag_routine", name="routine-agent", tenant_id=TENANT),
        environment=ma_environment(id="env_1", name="default", tenant_id=TENANT),
        config=ResolvedConfig(agent_name="routine-agent", environment_name="default"),
        origin_channel_id="channel",
        origin_seal_ids=frozenset({"channel"}),
        grant=AdmissionGrant(
            tenant_id=TENANT,
            subject=build_subject(is_admin=True, platform_user_id="owner"),
            surface=Surface.ROUTINE,
            turn_place=Place(channel_id="channel"),
            agent=AgentRef.of("routine-agent"),
            run_place=Place(channel_id="channel"),
            channel_id="channel",
            thread_id=None,
            is_dm=False,
        ),
    )


def test_a_fire_takes_the_schedulers_hold_and_its_threads_own_seal() -> None:
    """The thread it posts to and that thread's seal join; the hold drops admin standing."""
    hold = TurnHold("discord", "channel", client=False)
    origin = RoutineOrigin(
        channel_id="channel",
        thread_id="thread",
        seal_ids=frozenset({"thread"}),
        private_dm_id="routine:x",
    )
    placed = at_fire_place(_admitted(), hold=hold, origin_place=origin)
    assert placed.hold == hold and placed.grant is not None and placed.grant.hold == hold, placed
    assert not placed.grant.subject.is_admin, "a held fire has no admin standing"
    assert placed.origin_thread_id == "thread", "stamped in the thread it posts to"
    assert placed.origin_seal_ids == {"channel", "thread"}, "the thread's own seal joins"
    assert placed.memory_read_only, "a routine's memory is read-only, as on Claude"


def test_an_unplaced_fire_keeps_its_admission_but_reads_memory_only() -> None:
    """A fire with no destination nor hold changes nothing but its memory access."""
    admitted = _admitted()
    placed = at_fire_place(admitted, hold=None, origin_place=None)
    assert placed.hold is None and placed.origin_thread_id is None, placed
    assert placed.origin_seal_ids == admitted.origin_seal_ids and placed.memory_read_only
