"""The ABI v1 loader, Python half (plan PLAN-sdk-v1-ffi section 3.2/3.3).

Decoupled from the fetch PRs on purpose (plan section 3.1): `open()` takes a
small, loader-owned `predicate` mapping rather than the fetch module's
`Resolved` type, so this lane needs no fetch binding PR to merge first. Wave
C adds the three-line `Resolved -> predicate` adapter where the registry is
wired up; `predicate` is passed through exactly as that seam promises.

Every `chs_*` spelling in this module is forbidden by
scripts/abi-v1/check-no-hand-decls.py (it is not a generated file), so this
calls ONLY python/src/chtypes/_abi1/_decls.py's non-`chs_`-prefixed surface:
`resolve_abi_version()`, `resolve_all()`, and the `Api` object they return.

Steps 1-6 run in the order plan section 3.2 gives (with step 6's full
resolve-all sweep run right after step 3, BEFORE step 4's
`chs_build_info()` call -- step 4 needs that symbol already resolved, and
`tests/fixtures/abi-v1/cases.json`'s `missing-chs_build_info` and
`missing-chs_clickhouse_version` stub variants both expect the step 6
`missing_symbol:<name>` reason, not a step 4 one, which is only possible if
the full symbol sweep happens before chs_build_info is ever called). Step 7
calls the image's once-per-image zone setup (empty zone = UTC unless the
caller supplies one) and then its default settings.
"""

from __future__ import annotations

import ctypes
import json
import os
import platform
import re
import warnings
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from . import _decls, _errmap, _errors

ALLOW_UNVERIFIED_ENV = "CHTYPES_ALLOW_UNVERIFIED_LIBRARY"


@dataclass(frozen=True)
class LoadResult:
    """A library that passed every loader check: its resolved `Api` and the
    parsed `chs_build_info()` document."""

    api: _decls.Api
    build_info: dict
    path: str
    raw: bytes = b""


def _refuse(reason: str, path: str, want: object = None, got: object = None) -> None:
    cls = _errmap.loader_error_class(reason)
    raise cls(reason=reason, path=path, want=want, got=got)


def _glibc_version() -> tuple[int, int] | None:
    """Resolve `gnu_get_libc_version` dynamically from the running process:
    `ctypes.CDLL(None)` is the process image's own global symbols (glibc's,
    on a glibc system). Returns None on musl, or any system without the
    symbol -- the loader's own "no_glibc" refusal."""
    try:
        libc = ctypes.CDLL(None)
    except OSError:
        return None
    fn = getattr(libc, "gnu_get_libc_version", None)
    if fn is None:
        return None
    fn.restype = ctypes.c_char_p
    fn.argtypes = []
    raw = fn()
    if not raw:
        return None
    m = re.match(rb"^(\d+)\.(\d+)", raw)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def _check_glibc(path: str, predicate: Mapping[str, object]) -> None:
    """Loader step 1: Linux only (darwin: skip, plan section 3.2), and BEFORE
    dlopen -- this never touches the library file itself."""
    if platform.system() != "Linux":
        return
    floor = predicate.get("glibc_floor")
    if floor is None:
        _refuse("predicate_malformed", path, want="glibc_floor on a linux predicate", got=None)
    version = _glibc_version()
    if version is None:
        _refuse("no_glibc", path)
    want = tuple(int(x) for x in str(floor).split("."))
    if version < want:
        _refuse("glibc_floor", path, want=floor, got=".".join(map(str, version)))


def _dlopen(path: str) -> ctypes.CDLL:
    """Loader step 2: `RTLD_NOW | RTLD_LOCAL` (the F-Py dispatch's exact
    requirement) -- strict binding, so an unresolved symbol fails HERE, not
    on first use (the "unbound" stub variant proves this)."""
    try:
        return ctypes.CDLL(path, mode=os.RTLD_NOW | os.RTLD_LOCAL)
    except OSError as e:
        raise _errmap.loader_error_class("dlopen")(reason="dlopen", path=path, got=str(e)) from e


def _parse_build_info(raw: bytes, path: str) -> dict:
    """Parse `chs_build_info()` strictly (loader step 4): ASCII only,
    duplicate keys refused, `schema == 1`, the fields loader step 5 will
    cross-check present. This repository's scripts/abi-v1/jcs.py enforces the
    identical restriction for the ABI description itself, but that module is
    a generator-only tool (not shipped with the installed package), so this
    is its own small, independent implementation of the same rule."""
    if not raw.isascii():
        _refuse("build_info_malformed", path, got="non-ASCII byte in chs_build_info()")

    def _no_dup_pairs(pairs: list[tuple[str, object]]) -> dict:
        out: dict = {}
        for k, v in pairs:
            if k in out:
                raise ValueError(f"duplicate key {k!r}")
            out[k] = v
        return out

    try:
        info = json.loads(raw.decode("ascii"), object_pairs_hook=_no_dup_pairs)
    except (ValueError, UnicodeDecodeError) as e:
        _refuse("build_info_malformed", path, got=str(e))
    if not isinstance(info, dict):
        _refuse("build_info_malformed", path, got="chs_build_info() is not a JSON object")
    if info.get("schema") != 1:
        _refuse("build_info_malformed", path, got=f"schema={info.get('schema')!r}, want 1")
    required = (
        "abi",
        "abi_fingerprint",
        "clickhouse_version",
        "channel",
        "build",
        "os",
        "arch",
        "inputs_sha256",
        "core_commit",
    )
    missing = [k for k in required if k not in info]
    if missing:
        _refuse("build_info_malformed", path, got=f"missing field(s): {missing}")
    return info


def _cross_check(info: Mapping[str, object], predicate: Mapping[str, object], path: str) -> None:
    """Loader step 5: `chs_build_info()` against the signed predicate, field
    by field, in the description's own order. The predicate is whatever the
    fetch layer returned, never re-encoded."""
    for build_info_field, predicate_field, _compare in _decls.CROSS_CHECK_FIELDS:
        bi_val = info.get(build_info_field)
        pred_val = predicate.get(predicate_field)
        if bi_val != pred_val:
            _refuse(f"build_info_mismatch:{build_info_field}", path, want=pred_val, got=bi_val)


def _open(
    path: str,
    predicate: Mapping[str, object],
    *,
    skip_step1: bool,
    skip_step5: bool,
    timezone: bytes = b"",
    defaults: bytes = b"",
    settle: Callable[[bool], None] | None = None,
) -> LoadResult:
    if not skip_step1:
        _check_glibc(path, predicate)

    lib = _dlopen(path)

    try:
        abi_version_fn = _decls.resolve_abi_version(lib)
    except _decls.MissingSymbol:
        _refuse("not_v1", path)
    version = abi_version_fn()
    if version != _decls.CHS_ABI_VERSION:
        _refuse("abi_version", path, want=_decls.CHS_ABI_VERSION, got=version)

    try:
        api = _decls.resolve_all(lib)
    except _decls.MissingSymbol as e:
        _refuse(f"missing_symbol:{e.name}", path, want=e.name, got=None)
        # _refuse always raises; the following line only satisfies a type
        # checker that cannot see that. (Never reached.)
        raise

    raw = api.build_info()
    if raw is None:
        _refuse("build_info_malformed", path, got="chs_build_info() returned NULL")
    info = _parse_build_info(raw, path)

    if info["abi_fingerprint"] != _decls.CHS_ABI_FINGERPRINT:
        _refuse(
            "fingerprint", path, want=_decls.CHS_ABI_FINGERPRINT, got=info.get("abi_fingerprint")
        )

    if not skip_step5:
        _cross_check(info, predicate, path)

    # Step 7: the once-per-image setup (docs/reference/bindings-v1.md section
    # 6): the image zone (empty = UTC), then the default settings when there
    # are any. After every handshake check and before any other call. A
    # failure is the library's own refusal, the call error its status maps to
    # (never a loader refusal reason). `settle`, when given, is told how step 7
    # ended: the public layer's setup guard latches on success and clears its
    # record on a failure before any image has completed step 7. Nothing here
    # remembers a failure, so the next open runs step 7 again.
    try:
        api.initialize(timezone)
        if defaults:
            api.set_defaults(defaults)
    except Exception:
        if settle is not None:
            settle(False)
        raise
    if settle is not None:
        settle(True)

    return LoadResult(api=api, build_info=info, path=path, raw=raw)


def recheck(result: LoadResult, path: str, predicate: Mapping[str, object]) -> None:
    """An image that is already open, met again through a new signed
    statement: loader steps 1, 4 and 5 are checked against that statement
    anyway (a mismatch refuses THIS request; the image stays open for the
    requests it did match). No dlopen and no step 7: both happened once, when
    the image first opened."""
    _check_glibc(path, predicate)
    _cross_check(result.build_info, predicate, path)


def open(
    path: str,
    predicate: Mapping[str, object],
    *,
    timezone: bytes = b"",
    defaults: bytes = b"",
    settle: Callable[[bool], None] | None = None,
) -> LoadResult:
    """Load and verify a v1 artifact at `path` against its (already verified
    elsewhere) signed `predicate`, then set the image zone (`timezone`, empty
    = UTC) and the default settings (`defaults`, a JSON object of string
    values, empty for none): step 7. Raises an artifact error naming the exact
    refusal on any loader failure, or the call error step 7's own status maps
    to. `settle`, when given, is told whether step 7 completed."""
    return _open(
        path,
        predicate,
        skip_step1=False,
        skip_step5=False,
        timezone=timezone,
        defaults=defaults,
        settle=settle,
    )


_WARNED_PATHS: set[str] = set()


def check_unverified_allowed(path: str, allow: bool) -> None:
    """An unverified open needs BOTH the caller's `allow` and the environment
    opt-in; either missing is a `UsageError`, raised before anything loads."""
    if not allow or os.environ.get(ALLOW_UNVERIFIED_ENV) != "1":
        raise _errors.misuse(
            f"open_unverified({path!r}) needs both allow=True and ${ALLOW_UNVERIFIED_ENV}=1"
        )


def open_unverified(
    path: str,
    *,
    predicate: Mapping[str, object] | None = None,
    allow: bool = False,
    timezone: bytes = b"",
    defaults: bytes = b"",
    settle: Callable[[bool], None] | None = None,
) -> LoadResult:
    """For core's local builds and the linked mode ONLY (plan section 3.1,
    Q-b): skips step 1 when no predicate is given, and always skips step 5.
    Needs BOTH `allow=True` and `$CHTYPES_ALLOW_UNVERIFIED_LIBRARY=1`; never
    a default, and warns loudly once per path."""
    check_unverified_allowed(path, allow)
    if path not in _WARNED_PATHS:
        _WARNED_PATHS.add(path)
        warnings.warn(
            f"chtypes: loading {path!r} UNVERIFIED (${ALLOW_UNVERIFIED_ENV}=1 is set); "
            "never use this outside a local build or core's own test suites",
            stacklevel=2,
        )
    return _open(
        path,
        predicate or {},
        skip_step1=predicate is None,
        skip_step5=True,
        timezone=timezone,
        defaults=defaults,
        settle=settle,
    )
