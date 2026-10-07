"""The test-only seams open only while pytest is running a test (r6).

Each case runs in a child interpreter, so the parent pytest process's own state
cannot leak in: `_pytest` imported without PYTEST_CURRENT_TEST, and
PYTEST_CURRENT_TEST without `_pytest`, must both be refused; both together (what
a running test has) is the positive control.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

_PROBE = """
import sys
{imports}
from chtypes._ocifetch import _channel
try:
    restore = _channel.allow_overrides_for_tests()
except RuntimeError as e:
    print("REFUSED", e)
    sys.exit(0)
restore()
print("OPENED")
"""


def _run(*, import_pytest: bool, current_test: bool) -> str:
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST"}
    if current_test:
        env["PYTEST_CURRENT_TEST"] = "probe::case (call)"
    code = _PROBE.format(imports="import _pytest" if import_pytest else "")
    out = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


@pytest.mark.parametrize(
    ("import_pytest", "current_test", "want"),
    [
        (True, False, "REFUSED"),  # a production process that happens to import pytest
        (False, True, "REFUSED"),  # the variable exported by hand
        (False, False, "REFUSED"),  # an ordinary process
        (True, True, "OPENED"),  # what a running pytest test has: the positive control
    ],
)
def test_seam_opens_only_while_pytest_runs_a_test(
    import_pytest: bool, current_test: bool, want: str
) -> None:
    got = _run(import_pytest=import_pytest, current_test=current_test)
    assert got.startswith(want), got
    if want == "REFUSED":
        assert "rule r6" in got, got
