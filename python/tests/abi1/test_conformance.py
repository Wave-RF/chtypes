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

import base64
import json
import threading

import pytest

from chtypes._abi1 import _decls, _loader

from .conftest import build_args, cases_of_kind, strip_ids, stub_path

_CASES, _IDS = cases_of_kind("handshake", "echo", "status", "lifecycle", "concurrent", "document")


@pytest.fixture(scope="session")
def ok_api(stubs_dir, stubs_manifest):
    entry = stubs_manifest["variants"]["ok"]
    result = _loader.open(stub_path(stubs_dir, entry), entry["predicate"])
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


def _check_result(case: dict, result, expect: dict) -> None:
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
    # A "document" case: the output decodes as STRICT UTF-8 (no replacement)
    # and equals the expected document exactly, every member and no more.
    for name, want_doc in expect.get("documents", {}).items():
        raw = result.outputs.get(name)
        assert raw is not None, f"{case['id']}: no output {name!r} in {result.outputs}"
        got = json.loads(raw.decode("utf-8", errors="strict"))
        assert got == want_doc, f"{case['id']}: document {name!r}: want {want_doc!r}, got {got!r}"


def _decode_document_case(case: dict, result) -> None:
    """Hand the REAL round-2 document the stub produced to the public decoder: the
    raw bytes (FF 00 80 and NUL included) must round-trip exactly."""
    from chtypes import _decode

    if case["fn"] == "chs_preview_row":
        row = _decode.decode_row_document(result.outputs["out"])
        (col,) = row.columns
        want = case["expect"]["documents"]["out"]["cols"][0]
        raw = bytes.fromhex(case["args"][2]["bytes_hex"])[3:]  # after the `!D:` prefix
        assert col.column == b"s" and col.is_stored is True
        assert col.value == raw and base64.b64decode(want["value_b64"]) == raw
        assert col.text == (
            base64.b64decode(want["stored_b64"])
            if "stored_b64" in want
            else want["stored"].encode()
        )
    elif case["fn"] == "chs_discover_columns":
        disc = _decode.decode_discovery(result.outputs["out"])
        want = case["expect"]["documents"]["out"]
        assert disc.columns[0].name == base64.b64decode(want["columns"][0]["name_b64"])
        assert disc.columns_sql == base64.b64decode(want["columns_sql_b64"])
        assert disc.columns[0].declaration == disc.columns_sql


def _run_echo_or_status(api, case: dict) -> None:
    args = build_args(api, case["args"])
    result = _decls.invoke_by_name(api, case["fn"], args)
    _check_result(case, result, case["expect"])
    if case["kind"] == "document":
        _decode_document_case(case, result)


def _live(api) -> dict:
    result = _decls.invoke_by_name(api, _LIVE_FN, [])
    assert result.status == "CHS_OK", f"live-handle read failed: {result.status}"
    return json.loads(result.outputs["out"])


_LIVE_FN = "chs_live_handles"


def _run_lifecycle(api, case: dict) -> None:
    base = _live(api)
    refs: dict = {}
    try:
        for i, step in enumerate(case["steps"]):
            where = f"{case['id']} step {i}"
            if "let" in step:
                call = step["call"]
                result = _decls.invoke_by_name(api, call["fn"], build_args(api, call["args"], refs))
                assert result.status == "CHS_OK", f"{where}: {call['fn']} gave {result.status}"
                handles = list(result.out_handles.values())
                assert len(handles) == 1, f"{where}: want one minted handle, got {handles}"
                refs[step["let"]] = handles[0]
            elif "call" in step:
                call = step["call"]
                result = _decls.invoke_by_name(api, call["fn"], build_args(api, call["args"], refs))
                _check_result(case, result, step["expect"])
            elif "free" in step:
                refs.pop(step["free"]).close()
            elif "live_delta" in step:
                now = _live(api)
                for kind, delta in step["live_delta"].items():
                    got = now.get(kind, 0) - base.get(kind, 0)
                    assert got == delta, f"{where}: live {kind} moved {got}, want {delta}"
            else:
                raise AssertionError(f"{where}: unrecognized step {step!r}")
        assert not refs, f"{case['id']}: handles left bound: {sorted(refs)}"
    finally:
        for h in refs.values():
            h.close()


def _run_concurrent(api, case: dict) -> None:
    args = build_args(api, case["args"])
    errors: list[str] = []
    barrier = threading.Barrier(case["threads"])

    def worker() -> None:
        try:
            barrier.wait()
            for _ in range(case["calls"]):
                result = _decls.invoke_by_name(api, case["fn"], args)
                _check_result(case, result, case["expect"])
        except BaseException as e:  # noqa: BLE001 - reported to the main thread
            errors.append(repr(e))

    threads = [threading.Thread(target=worker) for _ in range(case["threads"])]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for a in args:
        if hasattr(a, "close"):
            a.close()
    assert not errors, f"{case['id']}: {errors[:3]}"


@pytest.mark.parametrize("case", _CASES, ids=_IDS)
def test_case(case: dict, ok_api) -> None:
    kind = case["kind"]
    if kind == "handshake":
        _run_handshake(ok_api, case)
    elif kind in ("echo", "status", "document"):
        _run_echo_or_status(ok_api, case)
    elif kind == "lifecycle":
        _run_lifecycle(ok_api, case)
    elif kind == "concurrent":
        _run_concurrent(ok_api, case)
    else:
        raise AssertionError(f"{case['id']}: unrecognized case kind {kind!r}")
