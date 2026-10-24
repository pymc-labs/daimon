"""Queue notices reserve one reply per held turn without reading a real clock."""

from daimon.core.turn.thread_queue import QUEUE_NOTICE_AFTER_S, HeldThreads


def test_notice_threshold_deduplication_and_turn_reset() -> None:
    now = [0.0]
    threads = HeldThreads[str](clock=lambda: now[0])
    threads.add("thread")
    now[0] = QUEUE_NOTICE_AFTER_S - 1
    assert not threads.should_notify("thread")
    now[0] += 1
    assert threads.should_notify("thread")
    assert not threads.should_notify("thread")
    assert not threads.should_notify("another-thread")
    threads.start_turn("thread")
    assert not threads.should_notify("thread")
    now[0] += QUEUE_NOTICE_AFTER_S
    assert threads.should_notify("thread")
    threads.discard("thread")
    assert not threads.should_notify("thread")
    threads.add("thread")
    assert not threads.should_notify("thread")


async def test_each_drained_turn_gets_a_fresh_notice_budget() -> None:
    from daimon.core.turn.thread_queue import ThreadQueue

    now = [0.0]
    threads = HeldThreads[str](clock=lambda: now[0])
    threads.add("thread")
    queue = ThreadQueue[str, str](threads, {}, start_turn=threads.start_turn)
    noticed: list[str] = []

    async def run(message: str) -> None:
        assert not threads.should_notify("thread")
        now[0] += QUEUE_NOTICE_AFTER_S
        if threads.should_notify("thread"):
            noticed.append(message)
        threads.add("thread")  # repeated ownership claims cannot reset deduplication
        assert not threads.should_notify("thread")

    await queue.drain("thread", compose=lambda messages: messages, run=run, initial=["one", "two"])
    assert noticed == ["one", "two"]


def test_notice_generation_changes_even_when_clock_does_not() -> None:
    threads = HeldThreads[str](clock=lambda: 0.0)
    threads.add("thread")
    first = threads.generation("thread")
    assert threads.is_current("thread", first)
    threads.start_turn("thread")
    assert not threads.is_current("thread", first)
    second = threads.generation("thread")
    threads.discard("thread")
    assert not threads.is_current("thread", second)
    threads.add("thread")
    assert not threads.is_current("thread", second)
