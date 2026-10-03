"""In-process thread ownership and per-author drain, with adapter policies supplied as callbacks.

These views use the adapters' existing collections. Admission fences, tenant and
process slots, durable dedupe and turn execution remain with their callers.
"""

from asyncio import CancelledError
from collections.abc import Awaitable, Callable, Hashable


def group_by_author[Message, Author: Hashable](
    queued: list[Message], author: Callable[[Message], Author | None]
) -> list[list[Message]]:
    """Keep first-seen author order and arrival order within each author.

    A port can return None after reporting an authorless message to skip it.
    """
    groups: dict[Author, list[Message]] = {}
    for message in queued:
        if (key := author(message)) is not None:
            groups.setdefault(key, []).append(message)
    return list(groups.values())


class ThreadQueue[Key: Hashable, Message]:
    def __init__(self, processing: set[Key], pending: dict[Key, list[Message]]) -> None:
        self.processing = processing
        self.pending = pending

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
        on_cancel: Callable[[list[Turn]], Awaitable[None]],
        initial: list[Turn] | None = None,
    ) -> None:
        """Finish each popped batch before inspecting arrivals during its turns.

        The run callback owns the platform's error boundary and turn tail. An
        escaping error leaves only later batches in pending. Cancellation
        reports unstarted groups from the popped batch before propagating;
        the interrupted turn is never retried. Later batches stay in pending
        for the owner's existing cleanup.
        """
        turns = initial if initial is not None else compose(self.pending.pop(key, []))
        while turns:
            for index, turn in enumerate(turns):
                try:
                    await run(turn)
                except CancelledError:
                    await on_cancel(turns[index + 1 :])
                    raise
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
