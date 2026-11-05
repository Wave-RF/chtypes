"""`CancelToken`: the public cancel source, and the per-call arming of a C token.

A C token (`chs_cancel`) is per call and per image, and is never public. A call
given a `CancelToken` creates one, registers its fire with the source, names its
id in the call's options document, makes the call, unregisters (waiting for a
fire already in progress) and only then frees it: the C token is never fired
after it is freed. With no source, nothing here runs: no token is created and no
extra ABI call is made.
"""

from __future__ import annotations

import contextlib
import itertools
import threading
from collections.abc import Callable, Iterator

__all__ = ["CancelToken"]


class CancelToken:
    """A cancel source for one or many calls (`cancel=` on `Schema.row`,
    `Schema.rows`, `Schema.insert`, `Schema.parse_block`, `Filter.rows` and
    `Library.compile_table`).

    `cancel()` may be called from any thread, any number of times; the first
    call cancels every call that holds this token and is still running, and any
    call started with it afterwards. A canceled call raises `CallCanceledError`
    and answers nothing. A call that already passed its last check point returns
    its complete answer: a complete answer is never turned into a cancel.

    The calls release the GIL, so `cancel()` from another thread reaches a call
    while it runs. Ctrl-C (`KeyboardInterrupt`) cannot interrupt a call on the
    main thread: Python delivers the signal only when the call returns. To make
    a call interruptible, run it on a worker thread and cancel from the main
    thread, or with asyncio::

        tok = CancelToken()
        try:
            result = await asyncio.to_thread(schema.rows, Format.CSV, body, cancel=tok)
        except asyncio.CancelledError:
            tok.cancel()
            raise

    A token is one-shot: there is no reset; make a new one."""

    __slots__ = ("_canceled", "_inflight", "_lock", "_serial")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._canceled = False
        self._inflight: dict[int, Callable[[], None]] = {}
        self._serial = itertools.count()

    @property
    def canceled(self) -> bool:
        """Whether `cancel()` has been called."""
        return self._canceled

    def cancel(self) -> None:
        """Cancel every call that holds this token. Idempotent; any thread.

        The fires run under the token's lock, so a call that is returning waits
        for a fire in progress before it frees its C token. A fire never blocks."""
        with self._lock:
            if self._canceled:
                return
            self._canceled = True
            for fire in self._inflight.values():
                fire()

    def __repr__(self) -> str:
        return f"CancelToken(canceled={self._canceled})"

    def _register(self, fire: Callable[[], None]) -> int | None:
        """Register one call's fire. A token already canceled fires it at once
        (the call then answers `CHS_CANCELED` at entry) and keeps nothing."""
        with self._lock:
            if self._canceled:
                fire()
                return None
            key = next(self._serial)
            self._inflight[key] = fire
            return key

    def _unregister(self, key: int | None) -> None:
        """Drop one call's fire. Taking the lock waits for a `cancel()` that is
        firing it right now, so after this returns the fire can never run."""
        if key is None:
            return
        with self._lock:
            self._inflight.pop(key, None)


def check_source(cancel: object) -> CancelToken | None:
    """A `cancel=` argument: None or a `CancelToken`, checked before any call."""
    if cancel is None or isinstance(cancel, CancelToken):
        return cancel
    raise TypeError(f"cancel must be a CancelToken, not {type(cancel).__name__}")


@contextlib.contextmanager
def armed(api: object, source: CancelToken | None) -> Iterator[str | None]:
    """The call's cancel id while the C token is armed, or None with no source
    (and then no ABI call at all). Order: create, register the fire, the call
    (the body of the `with`), unregister (draining a fire in progress), free."""
    if source is None:
        yield None
        return
    handle, cid = api.cancel_create()  # type: ignore[attr-defined]
    try:
        key = source._register(lambda: api.cancel_fire(handle))  # type: ignore[attr-defined]
        try:
            yield cid.decode("ascii")
        finally:
            source._unregister(key)
    finally:
        handle.close()
