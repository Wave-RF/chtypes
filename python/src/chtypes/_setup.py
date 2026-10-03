"""The process setup (docs/reference/bindings-v1.md section 6): the image zone and
the default settings, chosen once, before traffic.

`setup` records; it does not load. The first open commits whatever is recorded
(the empty setup if `setup` was never called), and from then on `setup` succeeds
only with exactly the setup in effect. Every image is set up once, at loader
step 7, by `_image`, under this module's lock: the setup guard.
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
    """The setup in effect, committing the empty setup if none was recorded.
    Called by an open, with LOCK held."""
    return _record(_state if _state is not None else SetupState("", {}))


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
    is never called, the first open records the empty setup. Call it first.
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
    """Forget the recorded setup. Test-only: images already loaded keep theirs."""
    global _state
    with LOCK:
        _state = None
