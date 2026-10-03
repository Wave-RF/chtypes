"""``python -m chtypes`` and the ``chtypes`` console script, over the v1 fetch layer
(docs/guides/fetch-v1.md).

    chtypes fetch <spelling>... | --all  [--lock <file>] [--frozen] [--offline]
    chtypes verify                       re-verify every installed build
    chtypes list   [--offline]           what is installed, and what the registry publishes
    chtypes where                        the v1 cache root

A spelling is `26.8`, `26.8.15` or `26.8.15.10`. Exit statuses come from the
`errors` table of spec/fetch-v1/constants.json (generated into the fetch layer
as `ERROR_EXIT_CODES`); a usage error exits 2 and success exits 0. Progress and
diagnostics go to stderr; `fetch` prints one installed library path per line on
stdout.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Sequence

from . import _ocifetch as fetch_layer
from ._ocifetch import _constants as C
from ._ocifetch._ensure import _translate_transport_error, detect_host_platform
from ._ocifetch._errors import ArtifactCorruptError, FetchError
from ._ocifetch._http import FetchPolicy, RetryPolicy, TransportError, fetch_from_bases
from ._ocifetch._layout import resolve_cache_root
from ._ocifetch._lock import load_lock
from .errors import ChtypesError
from .registry import FetchOptions

__all__ = ["main"]

EXIT_OK = 0
EXIT_USAGE = 2
# A failure that carries no code from the table (a lock file that cannot be
# read, an interrupted run): the status the table gives its verification codes.
_EXIT_FALLBACK = C.ERROR_EXIT_CODES["CHTYPES_ARTIFACT_CORRUPT"]
_EXIT_INTERRUPTED = 130


def _say(line: str) -> None:
    sys.stderr.write(line + "\n")
    sys.stderr.flush()


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        _say(f"chtypes: {message}")
        raise SystemExit(EXIT_USAGE)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="chtypes",
        description="Fetch, verify and locate chtypes artifacts (docs/guides/fetch-v1.md).",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.required = True

    fetch = sub.add_parser(
        "fetch",
        help="install one or more versions, verified",
        description="Resolve, verify and install versions (docs/guides/fetch-v1.md sections 3-6).",
    )
    fetch.add_argument(
        "spellings",
        nargs="*",
        metavar="<spelling>",
        help="26.8, 26.8.15 or 26.8.15.10 (no v prefix, no channel suffix)",
    )
    fetch.add_argument(
        "--all",
        action="store_true",
        help="every line the registry publishes (with --frozen: every request the lock pins)",
    )
    fetch.add_argument(
        "--lock",
        metavar="<file>",
        help=f"write the lock file (default name {C.LOCK_DEFAULT_FILE}); with --frozen, read it",
    )
    fetch.add_argument(
        "--frozen",
        action="store_true",
        help="fetch only what the lock pins, by digest, with no tag or referrer lookups",
    )
    fetch.add_argument(
        "--offline",
        action="store_true",
        help="never touch a source: installed and verified, or CHTYPES_ARTIFACT_MISSING",
    )

    sub.add_parser(
        "verify",
        help="re-verify the installed cache",
        description="Re-hash every installed library against its own verified record.",
    )

    listing = sub.add_parser(
        "list",
        help="what is installed, and what the registry publishes",
        description="What is installed, and the version tags the registry publishes.",
    )
    listing.add_argument(
        "--offline", action="store_true", help="list only what is installed; no network"
    )

    sub.add_parser("where", help="the v1 cache root", description="Print the v1 cache root.")
    return parser


def _options(args: argparse.Namespace, *, platform: str | None = None):
    lock_path = None
    lock_write = False
    if getattr(args, "frozen", False):
        lock_path = args.lock or C.LOCK_DEFAULT_FILE
    elif getattr(args, "lock", None):
        lock_path = args.lock
        lock_write = True
    fetch = FetchOptions(
        offline=getattr(args, "offline", False),
        frozen=getattr(args, "frozen", False),
        lock_path=lock_path,
        lock_write=lock_write,
    )
    return fetch._to_options(platform)


_LINE_SPELLING = re.compile(r"^[0-9]+\.[0-9]+$")


def _published_tags(options) -> list[str]:
    """The registry's `tags/list`, kept to the tags that are v1 version spellings
    (`spelling.regex`): the referrers fallback tags (`sha256-<hex>`) and anything
    else a repository carries are not versions."""
    try:
        resp = fetch_from_bases(
            options.resolved_bases(),
            "/tags/list",
            mode="tag",
            accept=("application/json",),
            max_bytes=C.TAGS_LIST_MAX_BYTES,
            policy=FetchPolicy(token=options.token),
            retry=RetryPolicy(),
        )
    except TransportError as exc:
        raise _translate_transport_error(exc) from exc
    try:
        tags = json.loads(resp.body.decode("utf-8")).get("tags") or []
    except (ValueError, AttributeError) as exc:
        raise ArtifactCorruptError(f"chtypes: tags/list was not a tag list ({exc})") from exc
    keep = [t for t in tags if isinstance(t, str) and re.fullmatch(C.SPELLING_REGEX, t)]
    return sorted(set(keep), key=lambda t: tuple(int(p) for p in t.split(".")))


def _cmd_fetch(args: argparse.Namespace) -> int:
    if args.all and args.spellings:
        raise ValueError("chtypes: give spellings or --all, not both")
    if not args.all and not args.spellings:
        raise ValueError("chtypes: name at least one version, or pass --all")
    platform = detect_host_platform()
    options = _options(args, platform=platform)
    if args.all:
        if args.frozen:
            lock = load_lock(options.lock_path)
            spellings = list(lock.requests)
        else:
            spellings = [t for t in _published_tags(options) if _LINE_SPELLING.match(t)]
            if not spellings:
                _say("chtypes: the registry publishes no lines")
    else:
        spellings = list(args.spellings)
    requests = [fetch_layer.Request(s) for s in spellings]  # spelling errors: usage, before any I/O
    for request in requests:
        resolved = fetch_layer.ensure(request, options)
        for warning in resolved.warnings:
            _say(f"chtypes: warning: {warning}")
        state = "already installed" if resolved.already_installed else "installed"
        _say(f"chtypes: {request.spelling} -> {resolved.version} ({resolved.platform}) {state}")
        sys.stdout.write(f"{resolved.library_path}\n")
    sys.stdout.flush()
    return EXIT_OK


def _cmd_verify(_args: argparse.Namespace) -> int:
    results = fetch_layer.verify_installed(FetchOptions()._to_options())
    if not results:
        _say(f"chtypes: nothing installed under {resolve_cache_root()}")
        return EXIT_OK
    failed = 0
    for r in results:
        status = "ok" if r.ok else "FAILED"
        sys.stdout.write(f"{r.version:<14} {r.platform:<13} {status:<7} {r.dir}\n")
        if not r.ok:
            failed += 1
            _say(f"  {r.detail}")
    sys.stdout.flush()
    if failed:
        _say(f"chtypes: {failed} of {len(results)} installed build(s) FAILED verification")
        return C.ERROR_EXIT_CODES["CHTYPES_ARTIFACT_CORRUPT"]
    _say(f"chtypes: {len(results)} installed build(s) verified")
    return EXIT_OK


def _cmd_list(args: argparse.Namespace) -> int:
    options = FetchOptions(offline=args.offline)._to_options()
    installed = fetch_layer.list_installed(options)
    sys.stdout.write(f"installed ({resolve_cache_root()}):\n")
    if not installed:
        sys.stdout.write("  (nothing)\n")
    for r in sorted(installed, key=lambda r: (r.version, r.platform)):
        sys.stdout.write(f"  {r.version:<14} {r.platform:<13} {r.library_path}\n")
    if not args.offline:
        tags = _published_tags(options)
        sys.stdout.write("published:\n")
        if not tags:
            sys.stdout.write("  (nothing)\n")
        for tag in tags:
            sys.stdout.write(f"  {tag}\n")
    sys.stdout.flush()
    return EXIT_OK


def _cmd_where(_args: argparse.Namespace) -> int:
    sys.stdout.write(f"{resolve_cache_root()}\n")
    return EXIT_OK


_COMMANDS = {"fetch": _cmd_fetch, "verify": _cmd_verify, "list": _cmd_list, "where": _cmd_where}


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI; returns the exit status."""
    try:
        args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    except SystemExit as exc:  # argparse: a usage error (2) or --help (0)
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    try:
        return _COMMANDS[args.command](args)
    except ValueError as exc:  # a bad spelling or option combination
        _say(str(exc))
        return EXIT_USAGE
    except (FetchError, ChtypesError) as exc:
        code = getattr(exc, "code", "")
        _say(str(exc))
        if code:
            _say(f"chtypes: {code}")
        return C.ERROR_EXIT_CODES.get(code, _EXIT_FALLBACK)
    except OSError as exc:
        _say(f"chtypes: {exc}")
        return _EXIT_FALLBACK
    except KeyboardInterrupt:
        _say("chtypes: interrupted")
        return _EXIT_INTERRUPTED


if __name__ == "__main__":
    sys.exit(main())
