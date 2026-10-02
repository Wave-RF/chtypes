"""Runs every handshake/echo/status case in tests/fixtures/abi-v1/cases.json
(spec/abi-v1/schema/cases.schema.json) through the generated invoke-by-name
dispatcher (chtypes._abi1._decls.invoke_by_name), against the "ok" stub
library, opened through the real hand-written loader (chtypes._abi1._loader)
-- not a hand-built ctypes.CDLL -- so this suite exercises the loader and the
dispatcher together, the same path a real artifact will take once core
serves a v1 candidate (plan section 5.3). Loader-kind cases are
python/tests/abi1/test_loader.py's.
"""

from __future__ import annotations

import json

import pytest

from chtypes._abi1 import _decls, _loader

from .conftest import build_args, cases_of_kind, strip_ids, stub_path

_CASES, _IDS = cases_of_kind("handshake", "echo", "status")


@pytest.fixture(scope="session")
def ok_api(stubs_dir, stubs_manifest):
    entry = stubs_manifest["variants"]["ok"]
    result = _loader.open(stub_path(stubs_dir, "ok"), entry["predicate"])
    return result.api


def _run_handshake(api, case: dict) -> None:
    result = _decls.invoke_by_name(api, case["fn"], [])
    expect = case["expect"]
    if "int" in expect:
        assert result.int_value == expect["int"], (
            f"{case['id']}: want int {expect['int']!r}, got {result}"
        )
    elif "contains" in expect:
        assert (
            result.bytes_value is not None and expect["contains"].encode() in result.bytes_value
        ), f"{case['id']}: {expect['contains']!r} not found in {result.bytes_value!r}"
    elif "non_empty" in expect:
        assert result.bytes_value, (
            f"{case['id']}: expected a non-empty value, got {result.bytes_value!r}"
        )
    else:
        raise AssertionError(f"{case['id']}: unrecognized handshake expect shape {expect!r}")


def _check_error(case: dict, result, expect_error: dict) -> None:
    assert result.error is not None, f"{case['id']}: expected an error, got none"
    assert result.error.ch_code == expect_error["ch_code"], case["id"]
    assert result.error.ch_name == expect_error["ch_name"].encode(), case["id"]
    assert result.error.message == expect_error["message"].encode(), case["id"]
    assert result.error.column == expect_error["column"].encode(), case["id"]


def _run_echo_or_status(api, case: dict) -> None:
    args = build_args(api, case["args"])
    result = _decls.invoke_by_name(api, case["fn"], args)
    expect = case["expect"]
    assert result.status == expect["status"], (
        f"{case['id']}: want status {expect['status']}, got {result.status}"
    )
    if "error" in expect:
        _check_error(case, result, expect["error"])
    if "outputs" in expect:
        for name, want_echo in expect["outputs"].items():
            raw = result.outputs.get(name)
            assert raw is not None, f"{case['id']}: no output {name!r} in {result.outputs}"
            got = strip_ids(json.loads(raw))
            assert got == want_echo, (
                f"{case['id']}: output {name!r}: want {want_echo!r}, got {got!r}"
            )


@pytest.mark.parametrize("case", _CASES, ids=_IDS)
def test_case(case: dict, ok_api) -> None:
    kind = case["kind"]
    if kind == "handshake":
        _run_handshake(ok_api, case)
    elif kind in ("echo", "status"):
        _run_echo_or_status(ok_api, case)
    else:
        raise AssertionError(f"{case['id']}: unrecognized case kind {kind!r}")
