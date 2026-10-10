"""The close guard: the one piece of per-object synchronization the public layer
keeps (docs/reference/bindings-v1.md section 3).

There is no lock around a call: every call on a compiled handle is safe to run
concurrently with any other. The guard exists only because `close` frees the
caller's reference, which must not happen under a call that is still inside the
object. Calls hold the guard shared; `close` refuses later calls at once, waits
for the ones already inside, and only then frees.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from types import TracebackType

from .errors import _misuse as misuse

__all__ = ["CloseGuard"]


class CloseGuard:
    __slots__ = ("_active", "_closed", "_cond", "_what")

    def __init__(self, what: str) -> None:
        self._what = what
        self._cond = threading.Condition(threading.Lock())
        self._active = 0
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def __enter__(self) -> CloseGuard:
        """Enter a call. A closed object is a `UsageError`, raised here, before
        any C call."""
        with self._cond:
            if self._closed:
                raise misuse(f"{self._what} is closed")
            self._active += 1
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        with self._cond:
            self._active -= 1
            if self._active == 0:
                self._cond.notify_all()

    def close(self, free: Callable[[], None]) -> None:
        """Refuse later calls, wait for in-flight ones, then run `free` once.
        Idempotent: a second close returns at once."""
        with self._cond:
            if self._closed:
                return
            self._closed = True
            while self._active:
                self._cond.wait()
        free()
