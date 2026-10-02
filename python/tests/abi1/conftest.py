"""Fixtures for the ABI v1 conformance suite (plan PLAN-sdk-v1-ffi section
5.3, F-Py's conformance script scripts/abi-v1/conformance/python.sh).

Every test here needs the stub libraries scripts/abi-v1/build-stubs.sh
builds; without `$CHTYPES_ABI1_STUBS` it skips, loudly, by name -- the same
"no registry, no run" convention python/tests/conftest.py already documents
for the fetch suite. `uv run pytest -q` with no stubs therefore shows every
case in this directory SKIPPED, individually, never a collection error and
never a silent zero-run.

With stubs present, `pytest_sessionfinish` below also writes
`$CHTYPES_ABI1_REPORT` (spec/abi-v1/schema/report.schema.json's shape) from
the pass/fail this session actually observed per case id -- the report is a
BY-PRODUCT of running the real suite, never a second, independently
maintained notion of "did it pass".
"""

from __future__ import annotations

import json
import os
import platform
import sys
from pathlib import Path
from typing import Any

import pytest

from chtypes._abi1 import _decls

REPO_ROOT = Path(__file__).resolve().parents[3]
CASES_PATH = REPO_ROOT / "tests" / "fixtures" / "abi-v1" / "cases.json"
SCRIPTS_ABI_V1 = REPO_ROOT / "scripts" / "abi-v1"

# scripts/abi-v1 is a generator-only tree (never shipped with the installed
# package), but its compute_cases_hash is the ONE place cases.json's hash is
# derived (scripts/abi-v1/parity.py); importing it here, rather than
# recomputing the formula by hand, is the "drive it from the same derivation"
# rule (this repository's working-rules memory) applied to this report too.
sys.path.insert(0, str(SCRIPTS_ABI_V1))
import parity as _parity  # noqa: E402


def _load_cases() -> dict:
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))


_CASES_DOC = _load_cases()
_CASES: list[dict] = _CASES_DOC["cases"]
_CASE_IDS: list[str] = [c["id"] for c in _CASES]


def cases_of_kind(*kinds: str) -> tuple[list[dict], list[str]]:
    """(cases, ids) for exactly the given kinds -- used by each test file's
    own `@pytest.mark.parametrize`, so every case in cases.json lands in
    EXACTLY ONE test item across the whole suite (never a second file
    skipping what the first one ran): a skip during a test's "call" phase is
    an exception like any other, and `pytest_runtest_makereport` below would
    otherwise have to special-case it to avoid overwriting a real result."""
    chosen = [c for c in _CASES if c["kind"] in kinds]
    return chosen, [c["id"] for c in chosen]


@pytest.fixture(scope="session")
def stubs_dir() -> Path:
    env = os.environ.get("CHTYPES_ABI1_STUBS")
    if not env:
        pytest.skip("CHTYPES_ABI1_STUBS not set (scripts/abi-v1/build-stubs.sh --out DIR)")
    d = Path(env)
    if not d.is_dir():
        pytest.skip(f"CHTYPES_ABI1_STUBS={env!r} is not a directory")
    return d


@pytest.fixture(scope="session")
def stubs_manifest(stubs_dir: Path) -> dict:
    manifest = stubs_dir / "stubs.json"
    if not manifest.is_file():
        pytest.skip(f"{manifest} is missing (build-stubs.sh did not finish?)")
    return json.loads(manifest.read_text(encoding="utf-8"))


def host_os_arch() -> str:
    os_name = {"Linux": "linux", "Darwin": "darwin"}.get(
        platform.system(), platform.system().lower()
    )
    arch = {"x86_64": "amd64", "amd64": "amd64", "arm64": "arm64", "aarch64": "arm64"}.get(
        platform.machine(), platform.machine().lower()
    )
    return f"{os_name}-{arch}"


# -------------------------------------------------------------- report writer

# case id -> (pass, detail). Populated by pytest_runtest_makereport's "call"
# phase for every test parametrized by `case`; a test that errored during
# setup (never reaching "call") is absent, which is itself surfaced as
# MISSING by scripts/abi-v1/parity.py rather than silently "passed".
_RESULTS: dict[str, tuple[bool, str]] = {}


def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo) -> None:
    if call.when != "call":
        return
    callspec = getattr(item, "callspec", None)
    if callspec is None or "case" not in callspec.params:
        return
    case_id = callspec.id
    if case_id not in _CASE_IDS:
        return
    if call.excinfo is None:
        _RESULTS[case_id] = (True, "")
    else:
        _RESULTS[case_id] = (False, str(call.excinfo.value)[:2000])


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:  # noqa: ARG001
    report_path = os.environ.get("CHTYPES_ABI1_REPORT")
    if not report_path or not _RESULTS:
        return
    results = [
        {"id": case_id, "pass": ok, **({"detail": detail} if detail else {})}
        for case_id, (ok, detail) in sorted(_RESULTS.items())
    ]
    doc = {
        "schema": 1,
        "binding": "python",
        "toolchain": os.environ.get("CHTYPES_ABI1_TOOLCHAIN", platform.python_version()),
        "os": host_os_arch(),
        "cases_sha256": _parity.compute_cases_hash(_CASES_DOC),
        "results": results,
    }
    Path(report_path).write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# --------------------------------------------------------------- arg builder


def build_args(api: Any, arg_specs: list[dict]) -> list[Any]:
    return [build_arg(api, a) for a in arg_specs]


def build_arg(api: Any, a: dict) -> Any:
    if "int" in a:
        return a["int"]
    if "bytes_hex" in a:
        return bytes.fromhex(a["bytes_hex"])
    if "null_handle" in a:
        return None
    if "handle" in a:
        spec = a["handle"]
        result = _decls.invoke_by_name(api, spec["fn"], build_args(api, spec.get("args", [])))
        assert result.status == "CHS_OK", (
            f"{spec['fn']}: building a handle recipe must succeed, got {result.status}"
        )
        handles = list(result.out_handles.values())
        assert len(handles) == 1, (
            f"{spec['fn']}: expected exactly one minted handle, got {result.out_handles}"
        )
        return handles[0]
    raise ValueError(f"build_arg: unrecognized case argument shape {a!r}")


def strip_ids(obj: Any) -> Any:
    """The stub's echo JSON tags every handle with a per-image serial `id`
    (`emit/stub.py`'s `chs_sb_handle_field`) that no case can predict
    (`tests/fixtures/abi-v1/cases.json`'s own docstring); strip it, anywhere
    it appears, before a structural comparison."""
    if isinstance(obj, dict):
        return {k: strip_ids(v) for k, v in obj.items() if k != "id"}
    if isinstance(obj, list):
        return [strip_ids(v) for v in obj]
    return obj
