"""``python -m chtypes`` and the ``chtypes`` console script — docs/fetch.md §6.

    chtypes fetch <line>... [--all] [--platform <os-arch>] [--dest <dir>]
                            [--tag <t> | --url <base>] [--lock <file>] [--frozen]
                            [--force] [--offline]
    chtypes verify [--dest <dir>]        re-hash every installed line against its manifest
    chtypes list   [--dest <dir>]        what is installed, and what the release offers
    chtypes where                        the registry directory fetch would write to

Exit codes: 0 ok · 1 verification failed · 2 usage · 3 source unreachable ·
4 not published for this platform/line. Progress goes to stderr; ``fetch``
prints the installed directory alone on stdout, one per line.
"""

from __future__ import annotations

import argparse
import sys
import warnings
from collections.abc import Sequence
from pathlib import Path

from ._manifest import Manifest
from .errors import (
    CODE_ARTIFACT_UNPUBLISHED,
    CODE_SOURCE_UNREACHABLE,
    ArtifactError,
    ChtypesError,
    UnsignedArtifactWarning,
)
from .fetch import DEFAULT_ARTIFACTS_URL, DEFAULT_TAG, PLATFORMS, ReleaseEntry

__all__ = ["main"]

EXIT_OK = 0
EXIT_VERIFY_FAILED = 1
EXIT_USAGE = 2
EXIT_UNREACHABLE = 3
EXIT_UNPUBLISHED = 4

_EXIT_FOR_CODE = {
    CODE_SOURCE_UNREACHABLE: EXIT_UNREACHABLE,
    CODE_ARTIFACT_UNPUBLISHED: EXIT_UNPUBLISHED,
}


def _say(line: str) -> None:
    sys.stderr.write(line + "\n")
    sys.stderr.flush()


class _Parser(argparse.ArgumentParser):
    """argparse exits 2 on usage errors already; this keeps the message shape ours."""

    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        _say(f"chtypes: {message}")
        raise SystemExit(EXIT_USAGE)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="chtypes",
        description="Fetch, verify and locate chtypes artifacts (docs/fetch.md).",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.required = True

    def dest_option(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--dest",
            metavar="<dir>",
            help="the registry directory (default: $CHTYPES_REGISTRY, else the per-user cache)",
        )

    fetch = sub.add_parser(
        "fetch",
        help="install one or more ClickHouse lines, verified",
        description="Install ClickHouse lines through the verification chain (docs/fetch.md §3).",
    )
    fetch.add_argument(
        "lines",
        nargs="*",
        metavar="<line>",
        help="a minor line (25.8) or an exact patch (25.8.28.1-lts, a hard requirement)",
    )
    fetch.add_argument("--all", action="store_true", help="every line the release publishes")
    fetch.add_argument(
        "--platform",
        metavar="<os-arch>",
        choices=PLATFORMS,
        help=f"one of {', '.join(PLATFORMS)} (default: this host)",
    )
    dest_option(fetch)
    where_from = fetch.add_mutually_exclusive_group()
    where_from.add_argument(
        "--tag",
        metavar="<t>",
        help=f"a release tag on the artifacts host (default: the rolling '{DEFAULT_TAG}')",
    )
    where_from.add_argument(
        "--url",
        metavar="<base>",
        help=f"any base: an http(s) URL, a file:// URL or a directory "
        f"(default: $CHTYPES_ARTIFACTS_URL or {DEFAULT_ARTIFACTS_URL}, plus the tag)",
    )
    fetch.add_argument(
        "--lock", metavar="<file>", help="record what was installed in this lock file (§5)"
    )
    fetch.add_argument(
        "--frozen",
        action="store_true",
        help="refuse anything the lock file does not pin (default lock: chtypes.lock)",
    )
    fetch.add_argument("--force", action="store_true", help="re-download an installed line")
    fetch.add_argument(
        "--offline",
        action="store_true",
        help="never touch the source: installed and verified, or CHTYPES_SOURCE_UNREACHABLE",
    )

    verify = sub.add_parser(
        "verify",
        help="re-hash every installed line against its manifest",
        description="Re-hash every installed line's library against its own manifest.json.",
    )
    dest_option(verify)

    listing = sub.add_parser(
        "list",
        help="what is installed, and what the release offers",
        description="What is installed in the registry, and what the release offers for it.",
    )
    dest_option(listing)
    listing.add_argument("--platform", metavar="<os-arch>", choices=PLATFORMS)
    lfrom = listing.add_mutually_exclusive_group()
    lfrom.add_argument("--tag", metavar="<t>")
    lfrom.add_argument("--url", metavar="<base>")

    where = sub.add_parser(
        "where",
        help="the registry directory fetch would write to",
        description="Print the registry directory fetch would write to (docs/fetch.md §1).",
    )
    where.add_argument("--platform", metavar="<os-arch>", choices=PLATFORMS)
    return parser


def _cmd_fetch(args: argparse.Namespace) -> int:
    from .fetch import fetch_lines

    installed = fetch_lines(
        args.lines,
        all_lines=args.all,
        dest=args.dest,
        platform=args.platform,
        url=args.url,
        tag=args.tag,
        lock=args.lock,
        frozen=args.frozen,
        force=args.force,
        offline=args.offline,
        progress=_say,
    )
    for directory in installed:
        sys.stdout.write(f"{directory}\n")
    sys.stdout.flush()
    return EXIT_OK


def _cmd_verify(args: argparse.Namespace) -> int:
    from .fetch import fetch_destination, verify_registry

    registry = fetch_destination(args.dest)
    report = verify_registry(registry)
    if not report:
        _say(f"chtypes: nothing installed under {registry}")
        return EXIT_OK
    failed = 0
    for minor, directory, error in report:
        if error is None:
            sys.stdout.write(f"{minor:<8} ok       {directory}\n")
        else:
            failed += 1
            sys.stdout.write(f"{minor:<8} FAILED   {directory}\n")
            _say(f"  {error}")
    _goldens_line(Path(registry))
    sys.stdout.flush()
    if failed:
        _say(f"chtypes: {failed} of {len(report)} installed line(s) FAILED verification")
        return EXIT_VERIFY_FAILED
    _say(f"chtypes: {len(report)} installed line(s) verified under {registry}")
    return EXIT_OK


def _cmd_list(args: argparse.Namespace) -> int:
    from .fetch import Fetcher, fetch_destination, host_platform, installed_lines

    platform = args.platform or host_platform()
    registry = fetch_destination(args.dest, platform=platform)
    have = installed_lines(registry)
    sys.stdout.write(f"installed ({registry}):\n")
    if not have:
        sys.stdout.write("  (nothing)\n")
    for minor, (directory, manifest) in have.items():
        sys.stdout.write(f"  {minor:<8} {manifest.clickhouse_version:<18} {directory}\n")
    sys.stdout.flush()

    fetcher = Fetcher(dest=registry, platform=platform, url=args.url, tag=args.tag, progress=_say)
    release = fetcher.release()
    offered = release.offered(platform)
    signed = f"signed by key {release.signed_by}" if release.signed_by else "UNSIGNED"
    sys.stdout.write(f"release ({release.source}, {signed}) offers for {platform}:\n")
    if not offered:
        sys.stdout.write("  (nothing)\n")
    for entry in offered:
        state = "installed" if _installed(have, entry) else "not installed"
        sys.stdout.write(
            f"  {entry.minor:<8} {entry.clickhouse_version:<18} b{entry.build_number:<3} "
            f"{entry.file}  [{state}]\n"
        )
    sys.stdout.flush()
    return EXIT_OK


def _installed(have: dict[str, tuple[object, Manifest]], entry: ReleaseEntry) -> bool:
    got = have.get(entry.minor)
    return got is not None and got[1].clickhouse_version == entry.clickhouse_version


def _goldens_line(registry: Path) -> None:
    """The served golden set sits beside the artifacts, so "where is my registry"
    and "is my registry sound" are both moments someone wants to know whether it
    is there — a missing one is why the golden tests skip."""
    from .fetch import GOLDENS_ASSET

    path = Path(registry) / GOLDENS_ASSET
    try:
        size = path.stat().st_size
    except OSError:
        sys.stdout.write(f"{path}  (golden set: not fetched — the golden tests will skip)\n")
    else:
        sys.stdout.write(f"{path}  (golden set, {size} bytes)\n")


def _cmd_where(args: argparse.Namespace) -> int:
    from .fetch import fetch_destination

    dest = fetch_destination(platform=args.platform)
    sys.stdout.write(f"{dest}\n")
    _goldens_line(Path(dest))
    return EXIT_OK


_COMMANDS = {
    "fetch": _cmd_fetch,
    "verify": _cmd_verify,
    "list": _cmd_list,
    "where": _cmd_where,
}


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI; returns the exit code (docs/fetch.md §6)."""
    parser = build_parser()
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    # The library's loud warning reaches stderr through the progress channel
    # here; the `warnings` copy would print it a second time.
    warnings.simplefilter("ignore", UnsignedArtifactWarning)
    try:
        return _COMMANDS[args.command](args)
    except ValueError as exc:  # a bad option combination or spelling: usage
        _say(str(exc))
        return EXIT_USAGE
    except ArtifactError as exc:
        _say(str(exc))
        _say(f"chtypes: {exc.code}")
        return _EXIT_FOR_CODE.get(exc.code, EXIT_VERIFY_FAILED)
    except ChtypesError as exc:
        _say(str(exc))
        return EXIT_VERIFY_FAILED
    except KeyboardInterrupt:
        _say("chtypes: interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
