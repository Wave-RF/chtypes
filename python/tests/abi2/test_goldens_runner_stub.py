"""The goldens runner, proven against the stub library before any real artifact exists.

`tests/goldens_v1/test_runner.py` is the Python binding's goldens runner
(docs/guides/goldens-v1.md section 2). Here it is driven, through its documented
stub override, against the `ok` stub library, over a small goldens document the
stub can answer (`tests/goldens_v1/data/stub-goldens.json`: a document-mode row, a
refusal, and a row with non-UTF-8 bytes in a second setup, so the per-setup
process split runs twice). The shared comparator then judges the report: it must
PASS, and a report with one planted wrong byte must FAIL.

Like every test in this directory it skips loudly by name without
`$CHTYPES_ABI2_STUBS`, and runs in the `v1-abi-conformance` leg, which runs this
directory.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from .conftest import REPO_ROOT, stub_path

RUNNER = REPO_ROOT / "python" / "tests" / "goldens_v1" / "test_runner.py"
STUB_DOCUMENT = REPO_ROOT / "python" / "tests" / "goldens_v1" / "data" / "stub-goldens.json"
COMPARE = REPO_ROOT / "scripts" / "goldens-v1" / "compare.py"


def _runner_module():
    spec = importlib.util.spec_from_file_location("chtypes_goldens_runner", RUNNER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _stub_document(stubs_manifest: dict, tmp_path: Path) -> Path:
    """`STUB_DOCUMENT` with its `abi` and `abi_fingerprint` taken from the "ok"
    stub's predicate in stubs.json (rendered from the description the stub is
    built from), so an UNSTABLE description's moving fingerprint never needs a
    hand edit here; the comparator then checks it against the identity the
    loaded stub reports."""
    predicate = stubs_manifest["variants"]["ok"]["predicate"]
    doc = json.loads(STUB_DOCUMENT.read_bytes())
    doc["abi"], doc["abi_fingerprint"] = predicate["abi"], predicate["abi_fingerprint"]
    path = tmp_path / "stub-goldens.json"
    path.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    return path


def _compare(document: Path, report: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(COMPARE), "--goldens", str(document), "--report", str(report)],
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_the_runner_over_the_stub_passes_the_comparator_and_a_planted_byte_fails(
    stubs_dir: Path, stubs_manifest: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "CHTYPES_GOLDENS_LIBRARY", stub_path(stubs_dir, stubs_manifest["variants"]["ok"])
    )
    document_in = _stub_document(stubs_manifest, tmp_path)
    monkeypatch.setenv("CHTYPES_GOLDENS_DOCUMENT_IN", str(document_in))
    monkeypatch.setenv("CHTYPES_GOLDENS_REPORT", str(tmp_path / "report.json"))
    monkeypatch.setenv("CHTYPES_GOLDENS_DOCUMENT", str(tmp_path / "document.json"))
    for name in (
        "CHTYPES_GOLDENS_REGISTRY_BASE",
        "CHTYPES_GOLDENS_VERSION",
        "CHTYPES_GOLDENS_PLATFORM",
    ):
        monkeypatch.delenv(name, raising=False)

    _runner_module().test_goldens_v1_runner(tmp_path)

    # The document written out is the exact bytes that went in.
    assert (tmp_path / "document.json").read_bytes() == document_in.read_bytes()
    report = tmp_path / "report.json"
    ok = _compare(document_in, report)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "PASS" in ok.stdout

    planted = json.loads(report.read_text(encoding="utf-8"))
    case = next(c for c in planted["cases"] if c["id"] == "stub-row-text")
    document = bytearray(base64.b64decode(case["document_b64"]))
    document[document.index(b"hello")] ^= 1  # one wrong byte in the exact output
    case["document_b64"] = base64.b64encode(bytes(document)).decode("ascii")
    bad = tmp_path / "planted.json"
    bad.write_text(json.dumps(planted), encoding="utf-8")
    refused = _compare(document_in, bad)
    assert refused.returncode != 0, refused.stdout
    assert "stub-row-text" in refused.stdout
