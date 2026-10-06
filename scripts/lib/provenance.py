#!/usr/bin/env python3
"""provenance.py — say which ABI this binding was built against, before a
suite runs.

WHY THIS EXISTS. A green run proves a suite passed against *something*, and
the log has to say what. Under ABI v1 there is no registry directory to
describe: a library is fetched over OCI, verified, and identified when it is
loaded, by the `chs_build_info` document the loader reads (and cross-checks
against `include/chtypes.h`'s `CHS_ABI_FINGERPRINT`). What this script can state
before any library is loaded is the identity of the ABI the binding is
compiled against, read from the header itself and never hard-coded here, so a
header change moves what this prints without this file being touched:

    provenance.py --header <path/to/chtypes.h>
    provenance.py --selftest

It prints one line, `provenance: this binding speaks ABI v<N>, fingerprint
<sha256:...> (<header path>)`, and a second one saying that the loaded
library's own `build_info` is what names the artifact. Both go out as
`::notice` workflow-command annotations too whenever `GITHUB_ACTIONS=true`
(`scripts/lib/gha_annotate.py`), because a machine that cannot read a job's log
anonymously can still read its annotations. A header that cannot say what ABI
it declares is reported as an `unknown` annotation and raises, so a missing
fact is never silence.

The v0 form of this script walked a registry's per-line `manifest.json` files
and warned on an `abi_revision` mismatch; that registry layout and that
revision number are gone with v0.
"""
from __future__ import annotations

import argparse
import io
import re
import sys
import tempfile
from pathlib import Path

import gha_annotate

ABI_VERSION_RE = re.compile(r"^\s*#\s*define\s+CHS_ABI_VERSION\s+(\d+)\b", re.MULTILINE)
ABI_FINGERPRINT_RE = re.compile(r'^\s*#\s*define\s+CHS_ABI_FINGERPRINT\s+"(sha256:[0-9a-f]{64})"', re.MULTILINE)


def read_header_identity(header_path: str) -> tuple[int, str]:
    """The (CHS_ABI_VERSION, CHS_ABI_FINGERPRINT) a header declares."""
    text = Path(header_path).read_text(encoding="utf-8")
    version = ABI_VERSION_RE.search(text)
    if not version:
        raise ValueError(f"{header_path} defines no CHS_ABI_VERSION")
    fingerprint = ABI_FINGERPRINT_RE.search(text)
    if not fingerprint:
        raise ValueError(f"{header_path} defines no CHS_ABI_FINGERPRINT")
    return int(version.group(1)), fingerprint.group(1)


def emit_line(line: str, out) -> None:
    """Print one provenance line to `out` and, at this single place every such
    line is produced, also hand it to `gha_annotate.notice()`, which emits it
    verbatim as an annotation when `GITHUB_ACTIONS=true` and does nothing
    otherwise. `out` is passed through as the annotation's own `file=`, so a
    selftest that redirects `out` captures both in the same place."""
    print(line, file=out)
    gha_annotate.notice(line, file=out)


def report(header_path: str, out=None) -> None:
    # Resolved here, not in the signature: a default of `out=sys.stdout` binds
    # the stream object once, at definition time, and `redirect_stdout` would
    # silently have no effect on it.
    if out is None:
        out = sys.stdout
    try:
        version, fingerprint = read_header_identity(header_path)
    except (OSError, ValueError) as e:
        gha_annotate.unknown(f"this binding's ABI identity ({header_path}: {e})", file=out)
        raise
    emit_line(f"provenance: this binding speaks ABI v{version}, fingerprint {fingerprint} ({header_path})", out)
    emit_line(
        "provenance: the loaded library's own chs_build_info names the artifact; "
        "the loader cross-checks its abi_fingerprint against the one above",
        out,
    )


def selftest() -> int:
    failures: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            failures.append(msg)

    fp_a = "sha256:" + "a" * 64
    fp_b = "sha256:" + "b" * 64

    def header_text(version: int, fingerprint: str) -> str:
        return f'#define CHS_ABI_VERSION {version}\n#define CHS_ABI_FINGERPRINT "{fingerprint}"\n'

    def run(header: Path) -> str:
        buf = io.StringIO()
        report(str(header), buf)
        return buf.getvalue()

    with tempfile.TemporaryDirectory() as td:
        header = Path(td) / "chtypes.h"

        # The printer reads the header, so a rewritten header moves what it
        # prints: a version that hard-codes either value fails here.
        header.write_text(header_text(1, fp_a))
        out = run(header)
        check(f"speaks ABI v1, fingerprint {fp_a}" in out, f"the identity line is missing or wrong:\n{out}")
        check("chs_build_info" in out, "the second line, naming chs_build_info, is missing")
        header.write_text(header_text(2, fp_b))
        out = run(header)
        check(f"speaks ABI v2, fingerprint {fp_b}" in out, f"a rewritten header did not move the line:\n{out}")
        check(fp_a not in out, "the old fingerprint survived a header rewrite")

        # Under GITHUB_ACTIONS the same text is also an annotation, verbatim.
        buf = io.StringIO()
        gha_annotate.notice("probe", file=buf, enabled=True)
        check(buf.getvalue().startswith("::notice"), "gha_annotate.notice(enabled=True) did not emit a notice")

        # A header with no identity must raise, and say unknown, never print a
        # plausible line.
        for label, text in (
            ("no CHS_ABI_VERSION", f'#define CHS_ABI_FINGERPRINT "{fp_a}"\n'),
            ("no CHS_ABI_FINGERPRINT", "#define CHS_ABI_VERSION 1\n"),
            ("a malformed fingerprint", '#define CHS_ABI_VERSION 1\n#define CHS_ABI_FINGERPRINT "sha256:xyz"\n'),
            ("the v0 revision macro only", "#define CHS_ABI_REVISION 5\n"),
        ):
            header.write_text(text)
            buf = io.StringIO()
            raised = False
            try:
                report(str(header), buf)
            except ValueError:
                raised = True
            check(raised, f"report() on a header with {label} did not raise ValueError")
            check("speaks ABI" not in buf.getvalue(), f"report() printed an identity for a header with {label}")

        missing = Path(td) / "absent.h"
        raised = False
        try:
            report(str(missing), io.StringIO())
        except OSError:
            raised = True
        check(raised, "report() on a missing header did not raise OSError")

    if failures:
        for f in failures:
            print(f"SELFTEST FAILED: {f}", file=sys.stderr)
        return 1
    print("provenance: selftest ok — the identity line follows the header, and a header without one raises")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--header", help="path to include/chtypes.h")
    ap.add_argument("--selftest", action="store_true", help="run this script's own correctness gate")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    if not args.header:
        ap.error("--header is required outside --selftest")
    report(args.header)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
