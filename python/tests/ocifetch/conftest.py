"""Which fetch contract this directory's tests run under.

The installed package speaks the ABI v2 dev channel (`chtypes._ocifetch._channel`).
Most tests here, and the fetch-v1 conformance cases (`test_conformance.py`) above
all, exercise the v1 fetch contract the dev channel narrows: the cases in
tests/fixtures/fetch-v1 are its specification, and every one names its own
fixture registry and the test key, sets locks, and reads schema-1 records and
abi-1 predicates. So each test here runs under `use_fetch_v1_for_tests`, and the
tests of the dev channel itself (`test_devchannel.py`) switch to it with
`use_dev_channel_for_tests` for their own duration.
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
