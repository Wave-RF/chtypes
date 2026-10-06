"""The process setup (docs/reference/bindings-v1.md section 6): the image zone and
the default settings, chosen once, before traffic.

`setup` records; it does not load. The first open records the empty setup if
`setup` was never called. The record latches once an image completes loader
step 7 (`chs_initialize`, then `chs_set_defaults` when there are defaults), and
from then on `setup` succeeds only with exactly the setup in effect. A step 7
failure before any image has completed it clears the record, so a corrected
setup can be recorded and the next open runs step 7 with it. Every image is set
up once, at loader step 7, by `library.open_image` and `open_unverified`, under
this module's lock: the setup guard.
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


def _record(state: SetupState) -> SetupState:
    """Record `state`, or confirm it equals the one in effect. Holds LOCK."""
    global _state
    if _state is None:
        _state = state
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


def settle(completed: bool) -> None:
    """How loader step 7 ended, reported by the loader under LOCK. A success
    latches the setup in effect. A failure before any image has completed step
    7 clears the record, so `setup` accepts a corrected setup; once the setup
    has latched, a failure changes nothing (the library's own process-once
    rule answers a different zone on an image that already has one)."""
    global _state, _latched
    with LOCK:
        if completed:
            _latched = True
        elif not _latched:
            _state = None


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
    once an image completes loader step 7; if step 7 fails before that, the
    record is cleared and a corrected setup is accepted. Call it first.
    """
    if timezone is not None and not isinstance(timezone, str):
        raise TypeError(f"timezone must be str, not {type(timezone).__name__}")
    mapping: dict[str, str] = {}
    if defaults:
        string_map_json(defaults, "defaults")  # the type check, once
        mapping = dict(defaults)
    with LOCK:
        _record(SetupState(timezone or "", mapping))


def _reset_for_tests() -> None:
    """Forget the recorded setup and the latch. Test-only: images already loaded
    keep theirs."""
    global _state, _latched
    with LOCK:
        _state = None
        _latched = False
