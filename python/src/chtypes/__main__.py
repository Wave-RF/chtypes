"""``python -m chtypes`` and the ``chtypes`` console script, over the v1 fetch layer
(docs/guides/fetch-v1.md).

    chtypes fetch <spelling>... | --all  [--platform <os-arch>] [--cache <dir>] [--lock <file>]
                                         [--frozen] [--offline] [--update] [--strict]
    chtypes verify [--cache <dir>] [--strict]   re-verify every installed build
    chtypes list   [--cache <dir>] [--offline] [--strict]
    chtypes where  [--cache <dir>] [--strict]   the cache root

A spelling is `26.8`, `26.8.15` or `26.8.15.10`. Exit statuses come from the
`errors` table of spec/fetch-v1/constants.json (generated into the fetch layer
as `ERROR_EXIT_CODES`); a usage error exits 2 and success exits 0. Progress and
diagnostics go to stderr; `fetch` prints one installed library path per line on
stdout.

2.0.0-dev: UNSTABLE, staging only, not for production. This CLI speaks the ABI
v2 dev channel (spec/abi-v2/docs.md, rules r5 and r6): it fetches only from
https://registry-staging.wavehouse.dev/chtypes/v2-dev and trusts only the
staging key; CHTYPES_ARTIFACTS_URL, CHTYPES_TRUSTED_KEYS and
CHTYPES_ALLOW_UNSIGNED are ignored, each with one warning; --lock, --frozen and
--update are refused before any network call; its default cache is
${XDG_CACHE_HOME:-~/.cache}/chtypes/v2-dev, and an explicit one (--cache,
CHTYPES_CACHE) is used through its v2-dev subroot.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Sequence

from . import __version__ as chtypes_version
from . import _ocifetch as fetch_layer
from ._ocifetch import _channel
from ._ocifetch import _constants as C
from ._ocifetch._ensure import _translate_transport_error, detect_host_platform, missing_notes
from ._ocifetch._errors import ArtifactCorruptError, ArtifactMissingError, FetchError
from ._ocifetch._faults import probe_roots
from ._ocifetch._http import FetchPolicy, RetryPolicy, TransportError, fetch_from_bases
from ._ocifetch._layout import resolve_cache_root, search_roots
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


def _display_version() -> str:
    """The package version; an uninstalled or dev build is 0.0.0-dev."""
    if chtypes_version.startswith("0.0.0+"):
        return "0.0.0-dev"
    return chtypes_version


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="chtypes",
        description="Fetch, verify and locate chtypes artifacts (docs/guides/fetch-v1.md).",
        epilog=(
            "2.0.0-dev: UNSTABLE, staging only, not for production. Fetches only from the staging "
            "dev channel and trusts only its key; --lock, --frozen and --update are refused; "
            "CHTYPES_ARTIFACTS_URL, CHTYPES_TRUSTED_KEYS and CHTYPES_ALLOW_UNSIGNED are ignored "
            "(spec/abi-v2/docs.md, rule r6)."
        ),
    )
    parser.add_argument("--version", action="version", version=f"chtypes {_display_version()}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.required = True

    def cache_option(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--cache", metavar="<dir>", help="the cache directory (default: CHTYPES_CACHE)"
        )
        p.add_argument(
            "--strict",
            action="store_true",
            help="a cache that cannot be read is CHTYPES_CACHE_UNUSABLE, never not-installed "
            "(default: CHTYPES_CACHE_STRICT=1)",
        )

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
        "--platform", metavar="<os-arch>", help="one of the platform keys (default: this host)"
    )
    cache_option(fetch)
    fetch.add_argument(
        "--update",
        action="store_true",
        help="re-resolve every locked request and rewrite the lock (requires --lock)",
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

    verify = sub.add_parser(
        "verify",
        help="re-verify the installed cache",
        description="Re-hash every installed library against its own verified record.",
    )
    cache_option(verify)

    listing = sub.add_parser(
        "list",
        help="what is installed, and what the registry publishes",
        description="What is installed, and the version tags the registry publishes.",
    )
    cache_option(listing)
    listing.add_argument(
        "--offline", action="store_true", help="list only what is installed; no network"
    )

    where = sub.add_parser("where", help="the cache root", description="Print the cache root.")
    cache_option(where)
    return parser


def _options(args: argparse.Namespace, *, platform: str | None = None):
    lock_path = None
    lock_write = False
    if getattr(args, "frozen", False):
        lock_path = args.lock or C.LOCK_DEFAULT_FILE
    elif getattr(args, "update", False):
        lock_path = args.lock
    elif getattr(args, "lock", None):
        lock_path = args.lock
        lock_write = True
    fetch = FetchOptions(
        cache_dir=getattr(args, "cache", None),
        strict_cache=True if getattr(args, "strict", False) else None,
        offline=getattr(args, "offline", False),
        frozen=getattr(args, "frozen", False),
        lock_path=lock_path,
        lock_write=lock_write,
        update=getattr(args, "update", False),
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
    # A dev SDK pins nothing: the refusal comes before anything else, the
    # network above all (rule r6).
    if (args.lock or args.frozen or args.update) and not _channel.active().pinnable:
        raise ValueError(f"chtypes: {_channel.PINNING_REFUSED}")
    if args.all and args.spellings:
        raise ValueError("chtypes: give spellings or --all, not both")
    if not args.all and not args.spellings and not args.update:
        raise ValueError("chtypes: name at least one version, or pass --all")
    if args.update and not args.lock:
        raise ValueError("chtypes: --update requires --lock")
    if args.update and (args.frozen or args.offline):
        raise ValueError("chtypes: --update cannot be combined with --frozen or --offline")
    platform = args.platform or detect_host_platform()
    if platform not in {p["key"] for p in C.PLATFORMS}:
        raise ValueError(f"chtypes: unknown platform {platform!r}")
    options = _options(args, platform=platform)
    if args.update and not args.spellings and not args.all:
        spellings = list(load_lock(options.lock_path).requests)
        args.spellings = spellings
    if args.all:
        if args.frozen or args.update:
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
        sys.stdout.write(f"{resolved.dir}\n")
    sys.stdout.flush()
    return EXIT_OK


def _say_notes(options) -> None:
    """What the cache says about itself in the default mode: a warning per root
    or entry it could not read, and the 0.x hint."""
    for note in missing_notes(options):
        _say(f"chtypes: {note}")


def _cmd_verify(args: argparse.Namespace) -> int:
    options = _options(args)
    results = fetch_layer.verify_installed(options)
    if not results:
        # An empty pass must never look like a good one (public issue #486):
        # say that nothing was verified, and why when the cache says why. In
        # strict mode it is a failure, so a mounted cache can be health-checked.
        root = resolve_cache_root(args.cache)
        _say(f"chtypes: verified 0 builds under {root}")
        _say_notes(options)
        if options.resolved_strict():
            raise ArtifactMissingError(
                f"chtypes: no build is installed under {root}, and strict mode needs one"
            )
        return EXIT_OK
    _say_notes(options)
    failed = 0
    for r in results:
        if not r.ok:
            failed += 1
            _say(f"chtypes: FAILED {r.version} {r.platform} {r.dir}: {r.detail}")
    if failed:
        _say(f"chtypes: {failed} of {len(results)} installed build(s) FAILED verification")
        return C.ERROR_EXIT_CODES["CHTYPES_ARTIFACT_CORRUPT"]
    return EXIT_OK


def _cmd_list(args: argparse.Namespace) -> int:
    options = _options(args)
    installed = fetch_layer.list_installed(options)
    for r in sorted(installed, key=lambda r: (r.version, r.platform)):
        sys.stdout.write(f"installed {r.version} {r.platform} {r.dir}\n")
    sys.stdout.flush()
    _say_notes(options)
    if not args.offline:
        for tag in _published_tags(options):
            sys.stdout.write(f"published {tag} support unknown\n")
    sys.stdout.flush()
    return EXIT_OK


def _cmd_where(args: argparse.Namespace) -> int:
    options = _options(args)
    if options.resolved_strict():
        # Strict mode checks the root before naming it.
        probe_roots(search_roots(options.cache_dir, options.system_dirs), strict=True)
    sys.stdout.write(f"{resolve_cache_root(args.cache)}\n")
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
