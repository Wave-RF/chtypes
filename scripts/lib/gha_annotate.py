#!/usr/bin/env python3
"""gha_annotate.py — turn one line of text into a GitHub Actions check-run
ANNOTATION (chtypes#320).

WHY THIS EXISTS. A machine automating the artifact producer's ABI-revision
transition reads this SDK's `artifacts` job to confirm it actually tested the
candidate build. Measured 2026-09-30: an unauthenticated read of that job's
check run and its annotations returns 200; the same read of its job LOG
returns 403 ("Must have admin rights"). So a machine with no token can see
annotations but never a log line, which means a provenance fact that only
ever reached the log might as well not exist to it — and a provenance fact
that was supposed to appear and silently did not must never be
indistinguishable from a pass.

This module is the one place that:

  - escapes a message the way GitHub's own `::workflow-command::` parser
    requires (escape_data / escape_property, below — the order matters: `%`
    first, so escaping a literal `%0D` already in the text does not become
    `%250D`);
  - formats and (optionally) prints a `::notice`/`::warning` line, gated on
    `GITHUB_ACTIONS=true` so a local run or a selftest does not spam a
    developer's terminal with workflow commands nobody there will parse;
  - gives `unknown(which)` one fixed shape — `::warning
    title=chtypes artifact provenance::unknown — <which>` — so "a provenance
    line could not be emitted" always reads as unknown, never as a quiet pass
    (chtypes#320, point 3).

Used from two places, deliberately not from a third:

  - `scripts/lib/provenance.py` imports this directly (same directory,
    import-by-sibling — see that file) and calls `notice()` at the single
    place each `provenance: …` line is already produced, so the annotation's
    body is always that line's EXACT current text. This is preferred over a
    caller post-filtering a suite's stdout for `provenance:` lines, which
    risks hiding a real exit code behind the filter (see check-suite.sh's own
    `set -o pipefail` discipline) and duplicates a pattern a second file would
    have to keep in sync with every new line this one grows.
  - `.github/workflows/ci.yml`'s `artifacts` job calls this file's CLI
    directly, for the two facts that are not an existing `print()` call to
    extend: which CHANNEL this run tested (scripts/abi-channel.sh's own
    stdout is captured into `$GITHUB_OUTPUT` and must not carry a stray
    `::notice` line into that file), and the `unknown` case for a suite or
    the channel that did not run at all (a workflow-level fact, not
    something any Python process on that path can observe from inside
    itself).

    python3 scripts/lib/gha_annotate.py notice "<message>"
    python3 scripts/lib/gha_annotate.py warning "<message>"
    python3 scripts/lib/gha_annotate.py unknown "<which>"   # warning: unknown — <which>
    python3 scripts/lib/gha_annotate.py --selftest

Deliberately NOT done here: reading or writing `$GITHUB_OUTPUT` /
`$GITHUB_STEP_SUMMARY`, or deciding WHICH lines are provenance — that is
`scripts/lib/provenance.py`'s and `ci.yml`'s own call sites' job, kept exactly
as it already was. This file only ever turns a message the caller already
decided on into a correctly escaped line, and decides whether to print it at
all (`GITHUB_ACTIONS=true`, or an explicit override for a test).

Escaping reference: GitHub's own documented rule for workflow commands
(https://docs.github.com/en/actions/using-workflows/workflow-commands-for-github-actions
, "Setting a Debug Message" / the Problem Matchers escaping table) — DATA
(message/property VALUES) replaces `%`→`%25`, CR→`%0D`, LF→`%0A`; PROPERTY
NAMES AND VALUES (here: `title=…`) replace those three AND `:`→`%3A`,
`,`→`%2C`.
"""
from __future__ import annotations

import argparse
import os
import sys

TITLE = "chtypes artifact provenance"


def escape_data(s: str) -> str:
    """Escape a workflow-command DATA value (the part after the second
    `::`) — `%` first, so a literal `%0D`/`%0A` already in `s` is not
    re-escaped into `%250D`/`%250A`."""
    return s.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def escape_property(s: str) -> str:
    """Escape a workflow-command PROPERTY value (here: `title=…`) — the same
    three substitutions as `escape_data`, plus `:` and `,`, which would
    otherwise be read as the next property or the next key=value pair."""
    return escape_data(s).replace(":", "%3A").replace(",", "%2C")


def format_command(level: str, message: str, title: str = TITLE) -> str:
    """The exact `::<level> title=<escaped title>::<escaped message>` line —
    never built ad hoc at a call site, so every emitter in this repository
    escapes the same way."""
    if level not in ("notice", "warning", "error"):
        raise ValueError(f"not a workflow-command annotation level: {level!r}")
    return f"::{level} title={escape_property(title)}::{escape_data(message)}"


def emit(level: str, message: str, *, title: str = TITLE, file=None, enabled: bool | None = None) -> bool:
    """Print `format_command(level, message, title)` to `file` and return
    True, unless `enabled` (or, when it is None, `GITHUB_ACTIONS == "true"`)
    says this is not a GitHub Actions run — in which case this prints
    nothing and returns False.

    `file` defaults to `None`, resolved to `sys.stdout` HERE rather than in
    the signature, the same reason `scripts/lib/provenance.py`'s own
    `report()` gives for the identical pattern: a default bound at
    def-time would not see a later `contextlib.redirect_stdout`."""
    if file is None:
        file = sys.stdout
    if enabled is None:
        enabled = os.environ.get("GITHUB_ACTIONS") == "true"
    if not enabled:
        return False
    print(format_command(level, message, title=title), file=file)
    return True


def notice(message: str, *, title: str = TITLE, file=None, enabled: bool | None = None) -> bool:
    return emit("notice", message, title=title, file=file, enabled=enabled)


def warning(message: str, *, title: str = TITLE, file=None, enabled: bool | None = None) -> bool:
    return emit("warning", message, title=title, file=file, enabled=enabled)


def unknown(which: str, *, title: str = TITLE, file=None, enabled: bool | None = None) -> bool:
    """chtypes#320, point 3: a provenance fact that could not be determined
    — a suite that never ran, a channel that could not be decided — reads as
    `unknown — <which>`, one fixed shape, never silence and never dressed up
    as a `notice`."""
    return warning(f"unknown — {which}", title=title, file=file, enabled=enabled)


def selftest() -> int:
    import io

    failures: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            failures.append(msg)

    # -- escaping: the three DATA escapes, and the order (% before \r / \n,
    #    so a literal "%0D" in the input is not re-escaped into "%250D"). --
    check(
        escape_data("100% done\r\nnext") == "100%25 done%0D%0Anext",
        f"escape_data did not escape %, CR and LF as documented: {escape_data('100% done' + chr(13) + chr(10) + 'next')!r}",
    )
    check(
        escape_data("already %0D escaped") == "already %250D escaped",
        "escape_data re-escaped a literal %0D instead of escaping the %% first — "
        f"got {escape_data('already %0D escaped')!r}",
    )
    check(escape_data("plain text, no specials") == "plain text, no specials", "escape_data touched a plain string")

    # -- escaping: PROPERTY values additionally escape : and , ------------
    check(
        escape_property("abi7-candidate: a, b") == "abi7-candidate%3A a%2C b",
        f"escape_property did not escape : and , : {escape_property('abi7-candidate: a, b')!r}",
    )
    check(
        escape_property("100%\r\n") == "100%25%0D%0A",
        "escape_property dropped the three DATA escapes it is supposed to inherit",
    )
    # escape_data must NOT touch : or , — proves the two functions are
    # genuinely different, not escape_property aliased onto both roles.
    check(escape_data("a:b,c") == "a:b,c", "escape_data escaped : or , — that is escape_property's job, not DATA's")

    # -- format_command: the exact shape, title included -------------------
    check(
        format_command("notice", "channel: served") == "::notice title=chtypes artifact provenance::channel: served",
        f"format_command built an unexpected line: {format_command('notice', 'channel: served')!r}",
    )
    check(
        format_command("warning", "unknown — python suite", title="x:y")
        == "::warning title=x%3Ay::unknown — python suite",
        "format_command did not escape a title containing a property-reserved character",
    )
    try:
        format_command("debug", "nope")
        failures.append("format_command accepted an unsupported level ('debug') instead of raising")
    except ValueError:
        pass

    # -- emit/notice/warning: the enabled gate, both directions ------------
    buf = io.StringIO()
    printed = notice("should not appear", file=buf, enabled=False)
    check(printed is False, "notice(enabled=False) reported True")
    check(buf.getvalue() == "", f"notice(enabled=False) printed something: {buf.getvalue()!r}")

    buf = io.StringIO()
    printed = notice("should appear", file=buf, enabled=True)
    check(printed is True, "notice(enabled=True) reported False")
    check(
        buf.getvalue() == "::notice title=chtypes artifact provenance::should appear\n",
        f"notice(enabled=True) printed the wrong line: {buf.getvalue()!r}",
    )

    buf = io.StringIO()
    warning("careful", file=buf, enabled=True)
    check(
        buf.getvalue() == "::warning title=chtypes artifact provenance::careful\n",
        f"warning(enabled=True) printed the wrong line: {buf.getvalue()!r}",
    )

    # -- the env-var default, both ways, saved and restored ----------------
    saved = os.environ.get("GITHUB_ACTIONS")
    try:
        os.environ.pop("GITHUB_ACTIONS", None)
        buf = io.StringIO()
        check(notice("x", file=buf) is False, "notice() with no GITHUB_ACTIONS env var and no override still emitted")
        os.environ["GITHUB_ACTIONS"] = "false"
        buf = io.StringIO()
        check(notice("x", file=buf) is False, "notice() with GITHUB_ACTIONS=false still emitted")
        os.environ["GITHUB_ACTIONS"] = "true"
        buf = io.StringIO()
        check(notice("x", file=buf) is True, "notice() with GITHUB_ACTIONS=true (env default) did not emit")
        check(buf.getvalue() == "::notice title=chtypes artifact provenance::x\n", "env-driven notice() printed the wrong line")
    finally:
        if saved is None:
            os.environ.pop("GITHUB_ACTIONS", None)
        else:
            os.environ["GITHUB_ACTIONS"] = saved

    # -- unknown(): the one fixed shape, point 3 of chtypes#320 -------------
    buf = io.StringIO()
    unknown("the channel (scripts/abi-channel.sh did not complete)", file=buf, enabled=True)
    check(
        buf.getvalue()
        == "::warning title=chtypes artifact provenance::unknown — the channel (scripts/abi-channel.sh did not complete)\n",
        f"unknown() did not print the fixed 'unknown — <which>' shape: {buf.getvalue()!r}",
    )
    # unknown() must escape its `which` the same as any other message —
    # proven with a `which` carrying a newline, the shape a free-text suite
    # name interpolated from elsewhere could in principle carry.
    buf = io.StringIO()
    unknown("python suite\n(see job summary)", file=buf, enabled=True)
    check(
        buf.getvalue() == "::warning title=chtypes artifact provenance::unknown — python suite%0A(see job summary)\n",
        f"unknown() did not escape its own message: {buf.getvalue()!r}",
    )

    if failures:
        for f in failures:
            print(f"SELFTEST FAILED: {f}", file=sys.stderr)
        return 1
    print(
        "gha_annotate.py: selftest ok — escape_data/escape_property escape %, CR, LF (and, for properties, : and ,) "
        "in the documented order, format_command rejects an unsupported level, emit/notice/warning/unknown respect "
        "an explicit enabled= override and the GITHUB_ACTIONS env var default, and unknown() always prints the "
        "fixed 'unknown — <which>' shape with its own message escaped (chtypes#320)"
    )
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("level", nargs="?", choices=["notice", "warning", "unknown"], help="annotation kind to emit")
    ap.add_argument("message", nargs="?", help="the annotation body ('which' for 'unknown')")
    ap.add_argument("--title", default=TITLE, help=f"annotation title (default: {TITLE!r})")
    ap.add_argument("--selftest", action="store_true", help="run this script's own correctness gate")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    if args.level is None or args.message is None:
        ap.error("a level (notice|warning|unknown) and a message are required outside --selftest")

    if args.level == "unknown":
        unknown(args.message, title=args.title)
    else:
        emit(args.level, args.message, title=args.title)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
