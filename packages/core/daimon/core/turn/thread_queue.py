"""In-process thread ownership and per-author drain, with adapter policies supplied as callbacks.

These views use the adapters' existing collections. Admission fences, tenant and
process slots, durable dedupe and turn execution remain with their callers.
"""

from collections.abc import Awaitable, Callable, Hashable
from time import monotonic

QUEUE_NOTICE_AFTER_S = 3.0 * 60.0
QUEUE_NOTICE = "Still working on the previous message — yours runs next."


class HeldThreads[Key: Hashable](set[Key]):
    """Thread ownership plus one queue notice per active turn.

    Reserve the notice synchronously before platform I/O so simultaneous
    mentions cannot post duplicates. Releasing ownership forgets all state.
    """

    def __init__(self, *, clock: Callable[[], float] = monotonic) -> None:
        super().__init__()
        self._clock = clock
        self._started: dict[Key, float] = {}
        self._notified: set[Key] = set()
        self._generations: dict[Key, object] = {}

    def start_turn(self, key: Key) -> None:
        self._started[key] = self._clock()
        self._generations[key] = object()
        self._notified.discard(key)

    def add(self, element: Key) -> None:
        if element not in self:
            self.start_turn(element)
        super().add(element)

    def discard(self, element: Key) -> None:
        super().discard(element)
        self._started.pop(element, None)
        self._generations.pop(element, None)
        self._notified.discard(element)

    def generation(self, key: Key) -> object | None:
        return self._generations.get(key)

    def is_current(self, key: Key, generation: object | None) -> bool:
        """Fence a notice delayed by platform I/O against release or a new turn."""
        return generation is not None and key in self and self.generation(key) is generation

    def should_notify(self, key: Key) -> bool:
        started = self._started.get(key)
        if (
            key not in self
            or started is None
            or key in self._notified
            or self._clock() - started < QUEUE_NOTICE_AFTER_S
        ):
            return False
        self._notified.add(key)
        return True


def group_by_author[Message, Author: Hashable](
    queued: list[Message],
    author: Callable[[Message], Author | None],
    *,
    separate: Callable[[Message], bool],
) -> list[list[Message]]:
    """Keep first-seen author order and arrival order within each author.

    A port can return None after reporting an authorless message to skip it.
    `separate` keeps a button request as its own turn, flushing ordinary groups
    on either side so a later mention cannot be folded ahead of that request.
    """
    result: list[list[Message]] = []
    groups: dict[Author, list[Message]] = {}
    for message in queued:
        if separate(message):
            # Button input is a distinct request, even when the same person
            # sends ordinary mentions on either side of it.
            result.extend(groups.values())
            groups.clear()
            result.append([message])
        elif (key := author(message)) is not None:
            groups.setdefault(key, []).append(message)
    result.extend(groups.values())
    return result


class ThreadQueue[Key: Hashable, Message]:
    def __init__(
        self,
        processing: set[Key],
        pending: dict[Key, list[Message]],
        *,
        start_turn: Callable[[Key], None],
    ) -> None:
        self.processing = processing
        self.pending = pending
        self._start_turn = start_turn

    def enqueue(self, key: Key, message: Message) -> None:
        """Append synchronously, before a port awaits its wait response."""
        self.pending.setdefault(key, []).append(message)

    def claim(self, key: Key) -> None:
        self.processing.add(key)

    async def drain[Turn](
        self,
        key: Key,
        *,
        compose: Callable[[list[Message]], list[Turn]],
        run: Callable[[Turn], Awaitable[None]],
        initial: list[Turn] | None = None,
    ) -> None:
        """Finish each popped batch before inspecting arrivals during its turns.

        The run callback owns the platform's error boundary and turn tail. An
        escaping error or cancellation leaves only later batches in pending.
        """
        turns = initial if initial is not None else compose(self.pending.pop(key, []))
        while turns:
            for turn in turns:
                # Each author batch starts a new turn while ownership stays held.
                self._start_turn(key)
                await run(turn)
            turns = compose(self.pending.pop(key, []))


def claim_dispatch[Key: Hashable, DispatchKey: Hashable, Request](
    processing: set[Key],
    key: Key,
    deferred: dict[DispatchKey, Request],
    dispatch_key: DispatchKey,
    request: Request,
    *,
    merge: Callable[[Request | None, Request], Request] | None = None,
    admit: Callable[[], bool] = lambda: True,
    claim_slot: bool = True,
) -> bool:
    """Remember a busy dispatch, otherwise check its port's cap and claim.

    No await may separate the busy/cap decisions from the claim. Teams merges
    regional URLs and wake caps, then claims both slots in its holding context;
    the other ports keep the last request and claim here.
    """
    if key in processing:
        deferred[dispatch_key] = (
            request if merge is None else merge(deferred.get(dispatch_key), request)
        )
        return False
    if not admit():
        return False
    if claim_slot:
        processing.add(key)
    return True


async def dispatch_and_drain(
    dispatch: Callable[[], Awaitable[None]],
    drain: Callable[[], Awaitable[None]],
    *,
    drain_on_error: bool = False,
) -> None:
    """Drain after success, or also after Exception for Discord.

    Cancellation bypasses the error drain, as in the original callers.
    """
    try:
        await dispatch()
    except Exception:
        if drain_on_error:
            await drain()
        raise
    await drain()


def release_thread[Key: Hashable, DispatchKey: Hashable, Request](
    processing: set[Key],
    key: Key,
    deferred: dict[DispatchKey, Request],
    *,
    dispatch_keys: Callable[[], list[DispatchKey]],
    draining: bool,
    resume: Callable[[DispatchKey, Request], object],
    on_release: Callable[[], object] = lambda: None,
) -> None:
    """Free ownership, then consume deferred requests and spawn their resumes.

    Even during shutdown requests are removed, with durable rows left for a
    later turn. Spawning stays synchronous so the owner's cleanup cannot wait.
    """
    processing.discard(key)
    on_release()
    for dispatch_key in dispatch_keys():
        request = deferred.pop(dispatch_key, None)
        if request is not None and not draining:
            resume(dispatch_key, request)
