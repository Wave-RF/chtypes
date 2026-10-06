#!/usr/bin/env python3
"""release-pep440.py: the Python release's version, from its tag, by PEP 440.

    uv run --no-project --with packaging==26.3 python3 scripts/release-pep440.py map <tag-version> <pyproject-version> [--prerelease]
    uv run --no-project --with packaging==26.3 python3 scripts/release-pep440.py --selftest

A Python release tag carries the channel's spelling (`python/v2.0.0-dev.0`,
scripts/release-channel.sh), and PyPI carries the PEP 440 one (`2.0.0.dev0`).
The two are the same version, but not the same string, so the release
workflow compares them the way every installer does: through
`packaging.version`, never by string equality. `map` prints the PEP 440
version and refuses, naming the rule, when:

  * either side is not a PEP 440 version;
  * the tag's version and pyproject.toml's are different versions;
  * pyproject.toml does not spell the version canonically. The wheel's file
    name, the installed metadata, the User-Agent and PyPI all carry the
    canonical form (`2.0.0.dev0`), and the binding's own User-Agent test
    compares it with the manifest's string, so a `2.0.0-dev.0` there would
    build a package whose version reads differently in every place;
  * `--prerelease` (passed while the channel is a pre-release channel) is
    given and the version is not a pre-release. A pre-release is what pip and
    uv never pick without `--pre` or an exact pin, so it is the property that
    keeps a dev build from being installed by default.

`packaging` is the library pip and uv's own specifier rules follow; it is not
in the standard library, so this runs under `uv run --with packaging`.
--selftest plants each refusal and requires it to fire.
"""
from __future__ import annotations

import sys

from packaging.version import InvalidVersion, Version


def pep440(tag_version: str, manifest: str, prerelease: bool) -> str:
    """The PEP 440 version both name, or ValueError naming the rule broken."""
    try:
        tag = Version(tag_version)
    except InvalidVersion:
        raise ValueError(f"the tag's version {tag_version!r} is not a PEP 440 version") from None
    try:
        declared = Version(manifest)
    except InvalidVersion:
        raise ValueError(f"pyproject.toml's version {manifest!r} is not a PEP 440 version") from None
    if tag != declared:
        raise ValueError(f"tag python/v{tag_version} is PEP 440 {tag}, but pyproject.toml says {declared}")
    if str(declared) != manifest:
        raise ValueError(f"pyproject.toml spells the version {manifest!r}; spell it canonically, {str(declared)!r}, as the wheel and PyPI will")
    if prerelease and not declared.is_prerelease:
        raise ValueError(f"{declared} is not a PEP 440 pre-release, so a plain install would pick it; the channel releases only pre-releases (#511)")
    return str(declared)


def selftest() -> int:
    n = 0
    good = [
        (("2.0.0-dev.0", "2.0.0.dev0", True), "2.0.0.dev0"),
        (("2.0.0-dev.17", "2.0.0.dev17", True), "2.0.0.dev17"),
        (("1.1.0", "1.1.0", False), "1.1.0"),
    ]
    for args, want in good:
        got = pep440(*args)
        if got != want:
            print(f"selftest FAIL: {args}: wanted {want!r}, got {got!r}", file=sys.stderr)
            return 1
        print(f"selftest ok: {args} -> {got!r}")
        n += 1
    bad = [
        (("2.0.0-dev.0", "2.0.0.dev1", True), "pyproject.toml says 2.0.0.dev1"),
        (("2.0.0-dev.0", "2.0.0", True), "pyproject.toml says 2.0.0"),
        (("2.0.0-dev.0", "2.0.0-dev.0", True), "spell it canonically, '2.0.0.dev0'"),
        (("2.0.0-dev.0", "2.0.0dev0", True), "spell it canonically, '2.0.0.dev0'"),
        (("2.0.0-dev.0", "v2.0.0.dev0", True), "spell it canonically, '2.0.0.dev0'"),
        (("2.0.0", "2.0.0", True), "is not a PEP 440 pre-release"),
        (("2.0.0-dev.0", "2.0.0.dev0+local", True), "pyproject.toml says 2.0.0.dev0+local"),
        (("2.0.0-dev.x", "2.0.0.dev0", True), "the tag's version '2.0.0-dev.x' is not a PEP 440 version"),
        (("2.0.0-dev.0", "two", True), "pyproject.toml's version 'two' is not a PEP 440 version"),
    ]
    for args, phrase in bad:
        try:
            got = pep440(*args)
        except ValueError as e:
            if phrase not in str(e):
                print(f"selftest FAIL: {args}: refused, but without {phrase!r}: {e}", file=sys.stderr)
                return 1
            print(f"selftest ok: {args} refused ({phrase!r})")
            n += 1
            continue
        print(f"selftest FAIL: {args}: was not refused (got {got!r})", file=sys.stderr)
        return 1
    print(f"selftest: {n} cases, every refusal fired and every good input passed")
    return 0


def main(argv: list[str]) -> int:
    if argv == ["--selftest"]:
        return selftest()
    if len(argv) in (3, 4) and argv[0] == "map" and (len(argv) == 3 or argv[3] == "--prerelease"):
        try:
            print(pep440(argv[1], argv[2], len(argv) == 4))
        except ValueError as e:
            print(f"::error::release-pep440: {e}", file=sys.stderr)
            return 1
        return 0
    print(__doc__.split("\n\n")[1], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
