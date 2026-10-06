"""The process setup (docs/reference/bindings-v1.md section 6): the image zone and
the default settings, chosen once, before traffic.

`setup` records; it does not load. The first open records the empty setup if
`setup` was never called. The record latches once an image completes loader
step 7 (`chs_initialize`, then `chs_set_defaults` when there are defaults), and
from then on `setup` succeeds only with exactly the setup in effect. Until then,
any open that fails, whatever failed, clears the record (`open_failed`), so a
corrected setup can be recorded. Every image is set up once, at loader step 7,
by `library.open_image` and `open_unverified`, under this module's lock: the
setup guard, which every load, latch and clear holds.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from ._input import Settings, string_map_json
from .errors import misuse

__all__ = ["setup"]

LOCK = threading.RLock()


@dataclass(frozen=True)
class SetupState:
    zone: str
    defaults: dict[str, str]

    def describe(self) -> str:
        return f"timezone={self.zone!r}, defaults={self.defaults!r}"

    def zone_bytes(self) -> bytes:
        return self.zone.encode("utf-8")

    def defaults_json(self) -> bytes:
        return string_map_json(self.defaults, "defaults") or b""


_state: SetupState | None = None
# Whether any image has completed loader step 7 under the record.
_latched = False
# Counts the records `setup` made and the records failed opens cleared. An open
# reads it when it begins, and clears the record on failure only if it is
# unchanged: a failed open never clears a setup recorded after it began.
_generation = 0


def _record(state: SetupState, *, by_setup: bool = False) -> SetupState:
    """Record `state`, or confirm it equals the one in effect. Holds LOCK."""
    global _state, _generation
    if _state is None:
        _state = state
        if by_setup:
            _generation += 1
        return state
    if _state != state:
        raise misuse(
            f"setup is already in effect ({_state.describe()}) and cannot be changed to "
            f"({state.describe()}); call setup() once, before the first open"
        )
    return _state


def effective() -> SetupState:
    """The setup in effect, recording the empty setup if none was recorded.
    Called by an open, with LOCK held."""
    return _record(_state if _state is not None else SetupState("", {}))


def generation() -> int:
    """Read when an open begins, for `open_failed`."""
    with LOCK:
        return _generation


def latch() -> None:
    """An image completed loader step 7: from then on the setup in effect
    stands. Called by an open with LOCK held, right after the load."""
    global _latched
    with LOCK:
        _latched = True


def open_failed(began: int) -> None:
    """Settle an open that failed, whatever failed. While no image has completed
    step 7 it clears the record, so `setup` accepts a corrected setup, unless a
    setup was recorded after the open began (`began` is `generation()` then).
    Once the setup has latched it changes nothing: the library's own
    process-once rule answers a different zone on an image that already has one.
    LOCK serializes it with every load, so it never lands between another
    open's commit and its latch."""
    global _state, _generation
    with LOCK:
        if _latched or _generation != began:
            return
        _state = None
        _generation += 1


def setup(*, timezone: str | None = None, defaults: Settings | None = None) -> None:
    """Set the image time zone and the default settings, once per process.

    `timezone` is the image zone ClickHouse's own `DateLUT` loads; it governs
    compiled types (a bare `DateTime` column's zone, MATERIALIZED, PARTITION BY
    and TTL). Left out, the library's default, `UTC`. `defaults` is the default
    settings every call starts from. The per-call zone is the `session_timezone`
    argument of a call, not this.

    Called again with the same zone spelling, byte for byte, and the same
    defaults, this is a no-op. Called with a different zone or different
    defaults it is a `UsageError` naming both, and the first setup stands. If it
    is never called, the first open records the empty setup. The setup latches
    once an image completes loader step 7. Until then, any open that fails,
    whatever failed (the fetch, the signature, an incompatible artifact, a
    missing symbol or step 7), clears the record: the next `setup` is accepted,
    and an open with no `setup` after the failure records the empty setup. Call
    it first.
    """
    if timezone is not None and not isinstance(timezone, str):
        raise TypeError(f"timezone must be str, not {type(timezone).__name__}")
    mapping: dict[str, str] = {}
    if defaults:
        string_map_json(defaults, "defaults")  # the type check, once
        mapping = dict(defaults)
    with LOCK:
        _record(SetupState(timezone or "", mapping), by_setup=True)


def _reset_for_tests() -> None:
    """Forget the recorded setup and the latch. Test-only: images already loaded
    keep theirs."""
    global _state, _latched
    with LOCK:
        _state = None
        _latched = False
