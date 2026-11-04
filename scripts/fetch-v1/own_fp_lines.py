#!/usr/bin/env python3
r"""scripts/fetch-v1/own_fp_lines.py — pick the registry lines that have a build at THIS SDK's fingerprint.

    scripts/fetch-v1/own_fp_lines.py --base <registry base> [--need N] [--first] [--force L[,L...]] [--fingerprint HEX]
    scripts/fetch-v1/own_fp_lines.py --selftest

WHY. The dev channel's repository holds builds of more than one ABI
fingerprint, and a line's own tag names the newest build of ANY of them. When a
build at a newer fingerprint is published, the newest line can exist there only
at that other fingerprint, and a 2.0.0-dev CLI (which speaks its own
fingerprint) correctly answers CHTYPES_ARTIFACT_UNPUBLISHED for it
(docs/guides/fetch-v1.md §3, "An SDK ahead of the registry fails closed"). A
job that took "the registry's newest lines" from tags/list then failed for a
reason that is not a defect of any binding.

SELECTION. A line has a build at the SDK's fingerprint when the registry
serves its alias `<line>--fp-<fp>` (§3: beside every tag a dev publish writes,
the alias names the newest build of that tag at that fingerprint; a listing
never shows an alias, so each is asked for by name). The newest lines with an
alias are chosen, newest first. The fingerprint is derived from the generator
(`gen.py --major 2 --fingerprint`), never typed here.

REFUSALS. Fewer than --need lines (default 2) is an error that names the
fingerprint, the lines listed and the lines that were skipped: never a pass on
0 or 1. `--force` names lines to use instead of choosing; every forced line
must have a build at the fingerprint or the check fails. A channel with no
fingerprint (a production repository) has no alias step, so every listed line
counts; the library function takes `has_build=None` for it.

`--selftest` drives `select_lines` over a stand-in registry: the newest line at
another fingerprint is skipped; forcing it fails; one matching line fails.
"""

from __future__ import annotations

import re
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
USER_AGENT = "chtypes-ci-line-select/1"
LINE_RE = re.compile(r"[0-9]+\.[0-9]+")
FP_RE = re.compile(r"[0-9a-f]{64}")


class SelectionError(RuntimeError):
    """The registry cannot supply the lines the check needs at this fingerprint."""


def sort_lines(tags: list[str]) -> list[str]:
    """The two-part tags, newest first, numeric by component."""
    two = {t for t in tags if LINE_RE.fullmatch(t)}
    return sorted(two, key=lambda t: tuple(int(x) for x in t.split(".")), reverse=True)


def select_lines(
    tags: list[str],
    has_build: Callable[[str], bool] | None,
    need: int = 2,
    force: list[str] | None = None,
    fp: str = "",
) -> list[str]:
    """The newest `need`+ lines with a build at the SDK's fingerprint, newest
    first. `has_build(line)` says whether the registry serves the line's alias
    for the fingerprint; None means the channel has none (every line counts).
    `force` replaces the choice with the named lines, each of which must pass
    `has_build` too. Raises SelectionError, never returns fewer than `need`."""
    listed = sort_lines(tags)
    ok = (lambda _l: True) if has_build is None else has_build
    if force:
        bad = [ln for ln in force if ln not in listed or not ok(ln)]
        if bad:
            raise SelectionError(f"forced line(s) {bad} have no build at fingerprint {fp or '(none)'} (lines listed: {listed})")
        chosen = list(force)
    else:
        chosen = [ln for ln in listed if ok(ln)]
    skipped = [ln for ln in listed if ln not in chosen]
    if len(chosen) < need:
        raise SelectionError(
            f"only {len(chosen)} line(s) {chosen} have a build at fingerprint {fp or '(none)'}, the check needs {need}; "
            f"listed {listed}, without a build at this fingerprint: {skipped}"
        )
    return chosen


def own_fingerprint(major: int = 2) -> str:
    """This tree's fingerprint for ABI `major`, from the generator."""
    out = subprocess.run(
        [sys.executable, "-I", str(ROOT / "scripts" / "abi-v1" / "gen.py"), "--major", str(major), "--fingerprint"],
        capture_output=True, text=True, timeout=120, check=False,
    )
    fp = out.stdout.strip().removeprefix("sha256:")
    if out.returncode != 0 or not FP_RE.fullmatch(fp):
        raise SelectionError(f"gen.py --major {major} --fingerprint exited {out.returncode}: {out.stderr.strip()[-200:]!r}")
    return fp


def _http(url: str, accept: str, method: str = "GET") -> tuple[int, bytes]:
    req = urllib.request.Request(url, method=method, headers={"Accept": accept, "User-Agent": USER_AGENT})
    last: Exception | None = None
    import time

    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return 404, b""
            last = e
            if e.code < 500 and e.code != 429:
                break
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
        time.sleep(2**attempt)
    raise SelectionError(f"{method} {url}: {last}")


def registry_api(base: str) -> str:
    scheme, _, rest = base.partition("://")
    host, _, repo = rest.partition("/")
    if scheme != "https" or not host or not repo:
        raise SelectionError(f"not an https registry base with a repository path: {base!r}")
    return f"{scheme}://{host}/v2/{repo}"


def registry_tags(base: str) -> list[str]:
    import json

    _, body = _http(f"{registry_api(base)}/tags/list", "application/json")
    return list(json.loads(body.decode("utf-8")).get("tags") or [])


def registry_has_build(base: str, line: str, fp: str) -> bool:
    status, _ = _http(
        f"{registry_api(base)}/manifests/{line}--fp-{fp}", "application/vnd.oci.image.index.v1+json", method="HEAD"
    )
    return status == 200


def _selftest() -> int:
    fp = "ab" * 32
    other = "cd" * 32
    # 26.10 exists only at another fingerprint, as on the staging dev channel.
    aliases = {("26.9", fp), ("26.8", fp), ("26.3", fp), ("26.10", other)}
    tags = ["26.10", "26.10.2", "26.10.2.103", "26.3", "26.3.38", "26.8", "26.9", f"26.9--fp-{fp}", "sha256-00", "junk"]

    def has(line: str) -> bool:
        return (line, fp) in aliases

    got = select_lines(tags, has, need=2, fp=fp)
    assert got == ["26.9", "26.8", "26.3"], got
    print(f"own_fp_lines: selftest: the newest line (26.10) has only another fingerprint's alias; chosen {got}")

    def must_fail(what: str, fn: Callable[[], object], needle: str) -> None:
        try:
            fn()
        except SelectionError as e:
            assert needle in str(e), (what, str(e))
            print(f"own_fp_lines: selftest: {what}: FAILED as required: {e}")
            return
        raise AssertionError(f"{what}: passed, must fail")

    must_fail("forcing the line without a build at the fingerprint", lambda: select_lines(tags, has, 2, ["26.10", "26.9"], fp), "26.10")
    must_fail("forcing a line the registry does not list", lambda: select_lines(tags, has, 2, ["26.99", "26.9"], fp), "26.99")
    one = {("26.8", fp), ("26.10", other), ("26.9", other)}
    must_fail("one matching line", lambda: select_lines(tags, lambda ln: (ln, fp) in one, 2, fp=fp), "only 1")
    must_fail("no matching line", lambda: select_lines(tags, lambda _l: False, 2, fp=fp), "only 0")
    must_fail("an empty listing", lambda: select_lines([], has, 2, fp=fp), "only 0")
    # A channel with no fingerprint has no alias step: every listed line counts.
    assert select_lines(tags, None, 2) == ["26.10", "26.9", "26.8", "26.3"]
    # Behavior for lines that DO have the fingerprint is unchanged: numeric order, not lexical.
    allfp = {(ln, fp) for ln in ("26.10", "26.9", "26.3")}
    assert select_lines(tags, lambda ln: (ln, fp) in allfp, 2, fp=fp) == ["26.10", "26.9", "26.3"]
    assert select_lines(tags, has, 1, ["26.8"], fp) == ["26.8"], "a forced line that has a build is used"
    assert FP_RE.fullmatch(own_fingerprint()), "the generator's fingerprint"
    print("own_fp_lines: selftest ok")
    return 0


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        return _selftest()
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", required=True)
    ap.add_argument("--need", type=int, default=2)
    ap.add_argument("--first", action="store_true", help="print only the newest chosen line")
    ap.add_argument("--force", default="")
    ap.add_argument("--fingerprint", default="", help="default: derived from the generator (--major 2)")
    ap.add_argument("--major", type=int, default=2)
    args = ap.parse_args(argv)
    try:
        fp = args.fingerprint or own_fingerprint(args.major)
        if not FP_RE.fullmatch(fp):
            raise SelectionError(f"not a fingerprint (64 lowercase hex): {fp!r}")
        force = [x for x in args.force.split(",") if x]
        lines = select_lines(registry_tags(args.base), lambda ln: registry_has_build(args.base, ln, fp), args.need, force, fp)
    except SelectionError as e:
        print(f"own_fp_lines: ERROR: {e}", file=sys.stderr)
        return 1
    print(f"own_fp_lines: registry {args.base}, fingerprint {fp}: lines {lines}", file=sys.stderr)
    print(lines[0] if args.first else "\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
