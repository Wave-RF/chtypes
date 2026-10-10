"""The process setup (docs/reference/bindings-v1.md section 6): the image zone and
the default settings, chosen once, before traffic.

`setup` records; it does not load. The first open records the empty setup if
`setup` was never called. The record latches once an image completes loader
step 7 (`chs_initialize`, then `chs_set_defaults` when there are defaults), and
from then on `setup` succeeds only with exactly the setup in effect. Until then,
an open that attempted a load and failed unlocks the record (`open_failed`): it
stays, so a retry runs under it, and a different setup may replace it. Every
image is set up once, at loader step 7, by `library.open_image` and
`open_unverified`, under this module's lock: the setup guard, which every load,
latch and unlock holds.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from ._input import Settings, string_map_json
from .errors import _misuse as misuse

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
# Whether an open that attempted a load failed since the record was last set or
# committed, with nothing latched: a different `setup` then replaces the record
# instead of being refused.
_replaceable = False
# Counts the records `setup` made or replaced. An open reads it when its attempt
# begins, and unlocks the record on failure only if it is unchanged: a failed
# open never unlocks a setup recorded after it began.
_generation = 0


def _misuse(current: SetupState, wanted: SetupState) -> Exception:
    return misuse(
        f"setup is already in effect ({current.describe()}) and cannot be changed to "
        f"({wanted.describe()}); call setup() once, before the first open"
    )


def effective() -> SetupState:
    """The setup in effect, recording the empty setup if none was recorded.
    Called by an open, with LOCK held. The load that follows claims the record,
    so it is locked again: no `setup` replaces it under a load."""
    global _state, _replaceable
    if _state is None:
        _state = SetupState("", {})
    _replaceable = False
    return _state


def generation() -> int:
    """Read when an open's attempt begins, for `open_failed`."""
    with LOCK:
        return _generation


def latch() -> None:
    """An image completed loader step 7: from then on the setup in effect
    stands. Called by an open with LOCK held, right after the load."""
    global _latched, _replaceable
    with LOCK:
        _latched, _replaceable = True, False


def open_failed(began: int) -> None:
    """Settle an open that attempted a load and failed, whatever failed. While
    no image has completed step 7 it unlocks the record: the record stays, so a
    retry runs under it, and a different `setup` may replace it. It leaves alone
    a setup recorded or replaced after the open began (`began` is
    `generation()` then), and once the setup has latched it changes nothing:
    the library's own process-once rule answers a different zone on an image
    that already has one. LOCK serializes it with every load, so it never lands
    between another open's commit and its latch."""
    global _replaceable
    with LOCK:
        if _latched or _state is None or _generation != began:
            return
        _replaceable = True


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
    once an image completes loader step 7. Until then, an open that attempted a
    load and failed, whatever failed (the fetch, the signature, an incompatible
    artifact, a missing symbol or step 7), keeps the record but makes it
    replaceable: a retry with no new `setup` runs under the recorded setup, and
    a different `setup` replaces it. A refused version spelling or an unverified
    open without the caller's opt-in fails before any load is attempted, and
    unlocks nothing. Call it first.
    """
    if timezone is not None and not isinstance(timezone, str):
        raise TypeError(f"timezone must be str, not {type(timezone).__name__}")
    mapping: dict[str, str] = {}
    if defaults:
        string_map_json(defaults, "defaults")  # the type check, once
        mapping = dict(defaults)
    wanted = SetupState(timezone or "", mapping)
    global _state, _replaceable, _generation
    with LOCK:
        if _state == wanted:
            return
        if _state is not None and (_latched or not _replaceable):
            raise _misuse(_state, wanted)
        # A first record, or a replacement after a failed open: either way the
        # new record is locked until the next failed open.
        _state, _replaceable = wanted, False
        _generation += 1


def _reset_for_tests() -> None:
    """Forget the recorded setup and the latch. Test-only: images already loaded
    keep theirs."""
    global _state, _latched, _replaceable
    with LOCK:
        _state = None
        _latched = False
        _replaceable = False
