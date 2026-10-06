"""Which fetch contract this directory's tests run under.

The CLI a user runs speaks the ABI v2 dev channel (`chtypes._ocifetch._channel`).
`test_main.py` exercises the command line over the v1 fetch contract the dev
channel narrows (a fixture registry, the test key, locks), so each test here runs
under `use_fetch_v1_for_tests`. `test_devchannel.py` switches each of its tests
to the dev channel, and runs the real CLI in a child process, which no seam
reaches, to prove a user's process speaks it.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from chtypes._ocifetch import _channel


@pytest.fixture(autouse=True)
def _fetch_v1_contract() -> Iterator[None]:
    restore = _channel.use_fetch_v1_for_tests()
    try:
        yield
    finally:
        restore()
