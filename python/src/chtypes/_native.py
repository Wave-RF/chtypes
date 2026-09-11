"""The ctypes layer: one dlopen'd artifact, every signature fully declared.

Three rules here are load-bearing, and each of them is the kind of silent
wrongness this repository exists to kill:

* **Every `argtypes` and `restype` is declared.** An undeclared `restype` on a
  pointer-returning function is treated as `int` and truncates the pointer on
  some platforms — a crash or, worse, a wrong answer.
* **Owned strings come back as `c_void_p`, never `c_char_p`.** ctypes converts a
  `c_char_p` result straight to `bytes` and throws the pointer away, which makes
  it impossible to call `chs_free` on it: every result document would leak.
  Borrowed `const char *` returns (the version string, the column accessors) are
  declared `c_char_p` precisely because they must NOT be freed. Out-parameters
  declared `char **` in C are bound as `POINTER(c_void_p)` for the same reason —
  ABI-identical, and the pointer survives for `chs_free`.
* **`RTLD_NOW | RTLD_LOCAL`.** RTLD_LOCAL is the entire mechanism by which two
  builds that both define `DB::DataTypeFactory` coexist in one process. A loader
  that uses RTLD_GLOBAL appears to work and then answers with the wrong
  version's semantics.
"""

from __future__ import annotations

import ctypes
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Final

from .errors import RegistryError, UnsupportedError

__all__ = ["NativeLibrary"]

_c_int_p: Final = ctypes.POINTER(ctypes.c_int)
_c_owned_p: Final = ctypes.POINTER(ctypes.c_void_p)  # a `char **` out-parameter


class _ChsBytes(ctypes.Structure):
    """The C `chs_bytes` out-param: a counted, library-owned byte buffer.

    `data` is declared `c_void_p`, NOT `c_char_p`, for the same reason owned
    strings are: ctypes would convert a `c_char_p` field straight to `bytes`
    at the NUL byte (the payload legally contains none of ours to respect —
    the LENGTH is the contract) and throw the pointer away, making the
    mandatory `chs_free(data)` impossible.
    """

    _fields_ = (("data", ctypes.c_void_p), ("len", ctypes.c_size_t))


# name -> (restype, argtypes). Ownership is encoded in the restype:
# c_void_p = malloc'd, free with chs_free; c_char_p = borrowed, never free.
_SIGNATURES: Final[dict[str, tuple[object, list[object]]]] = {
    "chs_clickhouse_version": (ctypes.c_char_p, []),
    "chs_abi_revision": (ctypes.c_int, []),
    "chs_init": (ctypes.c_int, [ctypes.c_char_p, ctypes.c_char_p, _c_owned_p]),
    "chs_set_default_settings": (ctypes.c_int, [ctypes.c_char_p, _c_owned_p]),
    "chs_shutdown": (None, []),
    "chs_free": (None, [ctypes.c_void_p]),
    "chs_validate_type": (ctypes.c_int, [ctypes.c_char_p, _c_owned_p, _c_int_p, _c_owned_p]),
    "chs_reference_type": (ctypes.c_void_p, [ctypes.c_char_p]),
    "chs_registered_families": (ctypes.c_void_p, []),
    "chs_function_flags": (ctypes.c_void_p, []),
    # settings_json + mode compile a column list under a DECLARED settings
    # profile; NULL/"{}" settings_json and mode 0 answer identically to the
    # pre-consolidation, settings-less compile — structurally, not just by
    # test (this cycle's whole point: zero movement in the rigs).
    "chs_schema_compile": (
        ctypes.c_void_p,
        [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int, _c_int_p, _c_owned_p],
    ),
    "chs_schema_free": (None, [ctypes.c_void_p]),
    # merge_tree_settings_json is the engine's own SETTINGS clause; NULL/"{}"
    # answers identically to the pre-consolidation, settings-less engine call.
    "chs_schema_engine": (
        ctypes.c_int,
        [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, _c_owned_p],
    ),
    "chs_schema_ttl": (ctypes.c_int, [ctypes.c_void_p, ctypes.c_char_p, _c_owned_p]),
    "chs_schema_column_count": (ctypes.c_int, [ctypes.c_void_p]),
    "chs_schema_column_name": (ctypes.c_char_p, [ctypes.c_void_p, ctypes.c_int]),
    "chs_schema_column_type": (ctypes.c_char_p, [ctypes.c_void_p, ctypes.c_int]),
    "chs_schema_column_default_kind": (ctypes.c_char_p, [ctypes.c_void_p, ctypes.c_int]),
    "chs_schema_column_default_expr": (ctypes.c_char_p, [ctypes.c_void_p, ctypes.c_int]),
    "chs_schema_column_default_is_literal": (ctypes.c_int, [ctypes.c_void_p, ctypes.c_int]),
    "chs_row": (
        ctypes.c_void_p,
        [ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p],
    ),
    # Revision 3: chs_rows carries export_format / doc_flags / out_bytes
    # (docs/reference/c-abi.md §Rows). The ABI-revision gate below is what guarantees
    # this 8-argument declaration describes the loaded artifact before any
    # call is made through it.
    "chs_rows": (
        ctypes.c_void_p,
        [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_size_t,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_uint,
            ctypes.POINTER(_ChsBytes),
        ],
    ),
    # Revision 3: the filter trio (docs/reference/c-abi.md §Filters). Optional symbols —
    # a revision-0 artifact may predate them, and absence degrades to
    # UnsupportedError at call time, never a load failure.
    # Revision 4: chs_filter_compile carries params_json ({name:Type} query
    # parameters — a JSON object of name -> value STRING, "{}" for none). The
    # ABI-revision gate below guarantees this 5-argument declaration describes
    # the loaded artifact before any call is made through it: a rev-3 artifact
    # (4-argument shape) is refused at load, and a rev-0 artifact predates the
    # symbol entirely.
    "chs_filter_compile": (
        ctypes.c_void_p,
        [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p, _c_int_p, _c_owned_p],
    ),
    "chs_filter_free": (None, [ctypes.c_void_p]),
    "chs_filter_rows": (
        ctypes.c_void_p,
        [ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p],
    ),
    # Revision 4: the block twin (docs/reference/c-abi.md §Blocks) — parse a body once,
    # evaluate K filters against the block. Optional, same degradation rule.
    "chs_block_parse": (
        ctypes.c_void_p,
        [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_size_t,
            ctypes.c_char_p,
            _c_int_p,
            _c_owned_p,
        ],
    ),
    "chs_block_free": (None, [ctypes.c_void_p]),
    "chs_filter_eval": (ctypes.c_void_p, [ctypes.c_void_p, ctypes.c_void_p]),
}

# The four an artifact must export to be a chtypes artifact at all. Everything
# else is optional: a missing symbol means "this artifact predates the feature"
# and MUST degrade to unsupported at call time, never to a load failure
# (docs/reference/artifact.md "Loading", steps 4-5).
# The chs_* ABI revision this binding was written against — CHS_ABI_REVISION in
# include/chtypes.h. Unlike the cgo binding, ctypes never sees the header, so
# this is a hand-kept mirror and MUST be bumped in the same cycle the header is.
# 4 = the filter phase-2 cycle, 2026-08-31: chs_filter_compile gained
# params_json ({name:Type} query parameters) and the block twin joined
# (chs_block_parse / chs_block_free / chs_filter_eval).
ABI_REVISION: Final = 4

_MANDATORY: Final = (
    "chs_clickhouse_version",
    "chs_init",
    "chs_schema_compile",
    "chs_rows",
)

# Column introspection shipped as one group. If any member is missing, treat the
# whole group as absent rather than partially populated.
_COLUMN_GROUP: Final = (
    "chs_schema_column_count",
    "chs_schema_column_name",
    "chs_schema_column_type",
    "chs_schema_column_default_kind",
    "chs_schema_column_default_expr",
    "chs_schema_column_default_is_literal",
)


class _RWLock:
    """A writer-preferring readers-writer lock — the shape Go's `Library.mu`
    (`sync.RWMutex`) has on the `dlopen`'d path, in a language whose standard
    library has no such primitive.

    **Readers** are every call that reaches `chs_row`, `chs_rows`,
    `chs_schema_compile`, `chs_schema_engine`, `chs_schema_ttl`,
    `chs_validate_type` and the column accessors. `docs/reference/c-abi.md`
    §Thread-safety declares those safe together **on distinct handles**, so
    per-handle serialisation is `Schema`'s own lock and not this one.

    **The writer** is the pair that mutates per-library PROCESS state —
    `chs_set_default_settings` and `chs_shutdown` (plus `chs_init`, which the
    ABI already restricts to process start). The same spec line says both
    "MUST be serialized against all other calls".
    `chs_set_default_settings` REPLACES the seeded settings list wholesale
    while `chs_row` / `chs_rows` / `chs_schema_compile` read that same list
    **by reference**: replacing it under a reader is not a stale read, it is a
    use-after-free. ctypes releases the GIL for the whole duration of a foreign
    call, so Python threads really can be inside one — the GIL is not the
    exclusion here, this is.

    Writer-preferring, so a settings seed cannot be starved by a steady stream
    of row calls (measured: without it the writer thread in
    `test_registry.py::test_set_default_settings_excludes_the_row_path` never
    completes a swap). Deliberately NOT reentrant: no reader on this path takes
    the lock twice — `_take` / `_take_err` free inside the caller's hold and
    acquire nothing themselves — and a reentrant read under a waiting writer is
    exactly the deadlock writer-preference would otherwise buy.
    """

    __slots__ = ("_cond", "_readers", "_waiting_writers", "_writer")

    def __init__(self) -> None:
        self._cond = threading.Condition(threading.Lock())
        self._readers = 0
        self._waiting_writers = 0
        self._writer = False

    @contextmanager
    def read(self) -> Iterator[None]:
        with self._cond:
            while self._writer or self._waiting_writers:
                self._cond.wait()
            self._readers += 1
        try:
            yield
        finally:
            with self._cond:
                self._readers -= 1
                if self._readers == 0:
                    self._cond.notify_all()

    @contextmanager
    def write(self) -> Iterator[None]:
        with self._cond:
            self._waiting_writers += 1
            while self._writer or self._readers:
                self._cond.wait()
            self._waiting_writers -= 1
            self._writer = True
        try:
            yield
        finally:
            with self._cond:
                self._writer = False
                self._cond.notify_all()


# One lock per dlopen'd IMAGE, not per wrapper object.
#
# `dlopen` refcounts one image per file, so two `Registry` instances over one
# directory share the C globals `chs_set_default_settings` replaces — and two
# `NativeLibrary` objects each holding their own lock would exclude nothing at
# all. Go closes this by interning the wrapper (a process-wide `loadedLibs`
# map, one `*Library` per resolved path) and TypeScript by the same trick (a
# realpath-keyed `loaded` map). Python's `Library` is not interned — only
# `chs_init` is deduplicated, by `_INITIALIZED_IMAGES` — so the LOCK is
# interned instead, on the same key.
_IMAGE_LOCKS: Final[dict[str, _RWLock]] = {}
_IMAGE_LOCKS_MU: Final = threading.Lock()


def _image_lock(path: str) -> _RWLock:
    key = os.path.realpath(path)
    with _IMAGE_LOCKS_MU:
        lock = _IMAGE_LOCKS.get(key)
        if lock is None:
            lock = _RWLock()
            _IMAGE_LOCKS[key] = lock
        return lock


class NativeLibrary:
    """One artifact's C ABI, with the reference implementation's locking.

    The lock is per loaded IMAGE (see `_image_lock`) and is held around every
    call including `chs_schema_compile` and `chs_free`. It is a readers-writer
    lock, mirroring Go's `Library.mu`: row calls on distinct handles run
    together, which is exactly what the ABI permits, and the two calls that
    mutate per-library process state — `chs_set_default_settings`,
    `chs_shutdown` — take it exclusively, which is what the ABI requires.
    Per-handle serialisation is a DIFFERENT lock, on `Schema`, because a single
    `chs_schema *` must never be used from two threads at once.
    """

    __slots__ = ("_fn", "_lock", "abi_revision", "path")

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = _image_lock(path)
        try:
            cdll = ctypes.CDLL(path, mode=os.RTLD_NOW | os.RTLD_LOCAL)
        except OSError as exc:
            raise RegistryError(f"chtypes: dlopen {path}: {exc}") from exc

        self._fn: dict[str, object] = {}
        for name, (restype, argtypes) in _SIGNATURES.items():
            try:
                fn = getattr(cdll, name)
            except AttributeError:
                continue
            fn.restype = restype
            fn.argtypes = argtypes
            self._fn[name] = fn

        missing = [name for name in _MANDATORY if name not in self._fn]
        if missing:
            raise RegistryError(
                f"chtypes: {path} does not export the chtypes C API (missing {', '.join(missing)})"
            )

        # The ABI identity gate (docs/reference/c-abi.md "ABI identity"). 0 = the artifact
        # predates chs_abi_revision, which is ignorance, not incompatibility, so
        # the per-symbol degradation rules stay. A DIFFERENT nonzero revision is
        # a positive statement that these ctypes signatures do not describe this
        # artifact, and calling through them would be undefined.
        self.abi_revision = (
            int(self._fn["chs_abi_revision"]()) if "chs_abi_revision" in self._fn else 0
        )
        if self.abi_revision not in (0, ABI_REVISION):
            raise RegistryError(
                f"chtypes: {path} reports ABI revision {self.abi_revision}, this "
                f"binding speaks {ABI_REVISION}; refusing to call through "
                f"mismatched declarations"
            )

    def has(self, name: str) -> bool:
        return name in self._fn

    @property
    def has_columns(self) -> bool:
        return all(name in self._fn for name in _COLUMN_GROUP)

    def _need(self, name: str, note: str):  # noqa: ANN202 - a ctypes _FuncPtr
        fn = self._fn.get(name)
        if fn is None:
            raise UnsupportedError(note)
        return fn

    # -- owned strings ----------------------------------------------------

    def _take(self, pointer: int | None) -> bytes | None:
        """Copy a malloc'd string out and release it with THIS library's chs_free.

        In a multi-version process the wrong library's `chs_free` on the right
        pointer is a heap corruption waiting to happen, so the free function is
        resolved per artifact and never crossed.
        """
        if not pointer:
            return None
        free = self._fn.get("chs_free")
        if free is None:  # pragma: no cover - no real artifact omits it
            raise UnsupportedError(
                f"{self.path} does not export chs_free, so returned strings cannot be released"
            )
        try:
            return ctypes.cast(pointer, ctypes.c_char_p).value
        finally:
            free(ctypes.c_void_p(pointer))

    def _take_err(self, holder: ctypes.c_void_p) -> str:
        raw = self._take(holder.value)
        return raw.decode("utf-8", "surrogateescape") if raw else ""

    # -- identity and process setup ---------------------------------------

    def clickhouse_version(self) -> str:
        with self._lock.read():
            raw = self._fn["chs_clickhouse_version"]()  # type: ignore[operator]
        return raw.decode() if raw else ""

    def init(self, timezone: str, unsafe_families: str) -> tuple[int, str]:
        """(code, err). err is meaningful only when code != 0 — the one
        reachable failure is an unknown timezone, and a bare code cannot say
        which name was rejected."""
        err = ctypes.c_void_p()
        with self._lock.write():
            rc = int(
                self._fn["chs_init"](  # type: ignore[operator]
                    timezone.encode(), unsafe_families.encode(), ctypes.byref(err)
                )
            )
            return rc, self._take_err(err)

    def set_default_settings(self, settings_json: str) -> tuple[int, str]:
        """(code, err). err carries the server's own message on refusal —
        including its did-you-mean hint naming the setting on an unknown
        name (code 115)."""
        fn = self._need(
            "chs_set_default_settings",
            "this artifact predates chs_set_default_settings (rebuild it)",
        )
        err = ctypes.c_void_p()
        with self._lock.write():
            rc = int(fn(settings_json.encode(), ctypes.byref(err)))
            return rc, self._take_err(err)

    def shutdown(self) -> None:
        fn = self._fn.get("chs_shutdown")
        if fn is None:
            return
        with self._lock.write():
            fn()

    # -- types -------------------------------------------------------------

    def validate_type(self, type_expr: str) -> tuple[str, int, str]:
        """(canonical, code, err). code/err are meaningful only when canonical is empty."""
        fn = self._need(
            "chs_validate_type", "this artifact predates chs_validate_type (rebuild it)"
        )
        canonical = ctypes.c_void_p()
        code = ctypes.c_int(0)
        err = ctypes.c_void_p()
        with self._lock.read():
            rc = int(
                fn(
                    type_expr.encode(),
                    ctypes.byref(canonical),
                    ctypes.byref(code),
                    ctypes.byref(err),
                )
            )
            if rc != 0:
                # The RETURN VALUE carries the code (docs/reference/c-abi.md: every out
                # param is optional); out_code is the convenience copy. Prefer
                # whichever is nonzero, so the guarded-exception corner (the C
                # side answers rc=-1 with out_code=0) still reads as the
                # decline it is rather than "a refusal with code 0".
                return "", int(code.value) or rc, self._take_err(err)
            raw = self._take(canonical.value)
        return (raw or b"").decode("utf-8", "surrogateescape"), 0, ""

    def reference_type(self, type_expr: str) -> str:
        fn = self._need(
            "chs_reference_type", "this artifact predates chs_reference_type (rebuild it)"
        )
        with self._lock.read():
            raw = self._take(fn(type_expr.encode()))
        return (raw or b"").decode("utf-8", "surrogateescape")

    def registered_families(self) -> str:
        """The newline-separated family list, verbatim."""
        fn = self._need(
            "chs_registered_families",
            "this artifact predates chs_registered_families (rebuild it)",
        )
        with self._lock.read():
            raw = self._take(fn())
        return (raw or b"").decode("utf-8", "surrogateescape")

    def function_flags(self) -> str:
        """The function-volatility TSV audit, verbatim. Requires chs_init."""
        fn = self._need(
            "chs_function_flags", "this artifact predates chs_function_flags (rebuild it)"
        )
        with self._lock.read():
            raw = self._take(fn())
        return (raw or b"").decode("utf-8", "surrogateescape")

    # -- schemas -----------------------------------------------------------

    def schema_compile(
        self, columns_sql: str, settings_json: str, mode: int
    ) -> tuple[int | None, int, str]:
        """Compile a column-declaration list, optionally under a DECLARED
        settings profile. `settings_json` NULL/"{}" plus `mode` 0 compiles
        under this build's own compile base — the common case, and the
        path that answers structurally identically to the
        pre-consolidation, settings-less `chs_schema_compile`.
        """
        code = ctypes.c_int(0)
        err = ctypes.c_void_p()
        with self._lock.read():
            handle = self._fn["chs_schema_compile"](  # type: ignore[operator]
                columns_sql.encode(),
                settings_json.encode(),
                mode,
                ctypes.byref(code),
                ctypes.byref(err),
            )
            if not handle:
                return None, int(code.value), self._take_err(err)
        return int(handle), 0, ""

    def schema_free(self, handle: int) -> None:
        fn = self._fn.get("chs_schema_free")
        if fn is None:  # pragma: no cover - no real artifact omits it
            return
        with self._lock.read():
            fn(ctypes.c_void_p(handle))

    def schema_engine(
        self, handle: int, engine: str, order_by: str, merge_tree_settings_json: str
    ) -> tuple[int, str]:
        """Declare the table's engine plus its MergeTree-namespace settings
        (the SETTINGS clause after the engine). `merge_tree_settings_json`
        NULL/"{}" answers structurally identically to the pre-consolidation,
        settings-less `chs_schema_engine`."""
        fn = self._need("chs_schema_engine", "this artifact predates engine support (rebuild it)")
        err = ctypes.c_void_p()
        with self._lock.read():
            rc = int(
                fn(
                    ctypes.c_void_p(handle),
                    engine.encode(),
                    order_by.encode(),
                    merge_tree_settings_json.encode(),
                    ctypes.byref(err),
                )
            )
            return rc, self._take_err(err)

    def schema_ttl(self, handle: int, ttl_sql: str) -> tuple[int, str]:
        fn = self._need("chs_schema_ttl", "this artifact predates TTL support (rebuild it)")
        err = ctypes.c_void_p()
        with self._lock.read():
            rc = int(fn(ctypes.c_void_p(handle), ttl_sql.encode(), ctypes.byref(err)))
            return rc, self._take_err(err)

    def schema_columns(self, handle: int) -> list[tuple[str, str, str, str, bool]]:
        """(name, type, default_kind, default_expr, default_is_literal) per column."""
        if not self.has_columns:
            return []
        with self._lock.read():
            count = int(self._fn["chs_schema_column_count"](ctypes.c_void_p(handle)))  # type: ignore[operator]
            out: list[tuple[str, str, str, str, bool]] = []
            for i in range(max(count, 0)):
                out.append(
                    (
                        _borrowed(self._fn["chs_schema_column_name"], handle, i),  # type: ignore[arg-type]
                        _borrowed(self._fn["chs_schema_column_type"], handle, i),  # type: ignore[arg-type]
                        _borrowed(self._fn["chs_schema_column_default_kind"], handle, i),  # type: ignore[arg-type]
                        _borrowed(self._fn["chs_schema_column_default_expr"], handle, i),  # type: ignore[arg-type]
                        bool(
                            self._fn["chs_schema_column_default_is_literal"](  # type: ignore[operator]
                                ctypes.c_void_p(handle), i
                            )
                        ),
                    )
                )
            return out

    # -- rows --------------------------------------------------------------

    def row(self, handle: int, fmt: int, raw: bytes, settings_json: str) -> bytes:
        fn = self._fn.get("chs_row")
        if fn is None:
            raise UnsupportedError("this artifact predates chs_row (rebuild it)")
        with self._lock.read():
            # NUL bytes are legal inside a RowBinary body, so the length is
            # passed explicitly and `strlen` is never involved.
            out = self._take(
                fn(ctypes.c_void_p(handle), fmt, raw, len(raw), settings_json.encode())
            )
        if out is None:
            raise UnsupportedError("this artifact predates chs_row (rebuild it)")
        return out

    def rows(
        self,
        handle: int,
        fmt: int,
        body: bytes,
        settings_json: str,
        export_format: int,
        doc_flags: int,
    ) -> tuple[bytes, bytes | None]:
        """(document, payload). `export_format` is -1 (CHS_EXPORT_NONE — no
        export, payload None) or an `enum chs_format` value; `doc_flags` the
        CHS_DOC_* bitmask. Both ride through UNVALIDATED — an unknown value is
        the library's loud refusal to make, never this binding's guess.

        Ownership: the export buffer is COPIED into a Python `bytes` and the
        C side's `data` freed with THIS library's `chs_free` before returning
        — no library-owned memory ever outlives the call. The emitted-empty
        case (data non-NULL, len 0) copies to `b""`, distinguishable from a
        decline's None ({NULL, 0}).
        """
        out_bytes = _ChsBytes() if export_format != -1 else None
        with self._lock.read():
            raw = self._fn["chs_rows"](  # type: ignore[operator]
                ctypes.c_void_p(handle),
                fmt,
                body,
                len(body),
                settings_json.encode(),
                export_format,
                doc_flags,
                ctypes.byref(out_bytes) if out_bytes is not None else None,
            )
            payload: bytes | None = None
            if out_bytes is not None and out_bytes.data:
                payload = ctypes.string_at(out_bytes.data, out_bytes.len)
                free = self._fn.get("chs_free")
                if free is not None:  # pragma: no branch - no real artifact omits it
                    free(ctypes.c_void_p(out_bytes.data))
            out = self._take(raw)
        if out is None:  # pragma: no cover - chs_rows is mandatory
            raise UnsupportedError("chs_rows returned null")
        return out, payload

    # -- filters ------------------------------------------------------------

    def filter_compile(
        self, handle: int, expr: str, params_json: str = "{}"
    ) -> tuple[int | None, int, str]:
        """(filter handle, code, err). code/err meaningful only on None.

        `params_json` is the `{name:Type}` query-parameter bindings, a JSON
        object of name -> value STRING ("{}" declares none) — revision 4."""
        fn = self._need(
            "chs_filter_compile", "this artifact predates chs_filter_compile (rebuild it)"
        )
        code = ctypes.c_int(0)
        err = ctypes.c_void_p()
        with self._lock.read():
            fhandle = fn(
                ctypes.c_void_p(handle),
                expr.encode(),
                params_json.encode(),
                ctypes.byref(code),
                ctypes.byref(err),
            )
            if not fhandle:
                return None, int(code.value), self._take_err(err)
        return int(fhandle), 0, ""

    def filter_free(self, fhandle: int) -> None:
        fn = self._fn.get("chs_filter_free")
        if fn is None:  # pragma: no cover - paired with chs_filter_compile
            return
        with self._lock.read():
            fn(ctypes.c_void_p(fhandle))

    def filter_rows(self, fhandle: int, fmt: int, body: bytes, settings_json: str) -> bytes:
        fn = self._need("chs_filter_rows", "this artifact predates chs_filter_rows (rebuild it)")
        with self._lock.read():
            out = self._take(
                fn(ctypes.c_void_p(fhandle), fmt, body, len(body), settings_json.encode())
            )
        if out is None:  # pragma: no cover - the C side never returns NULL here
            raise UnsupportedError("chs_filter_rows returned null")
        return out

    # -- blocks --------------------------------------------------------------

    def block_parse(
        self, handle: int, fmt: int, body: bytes, settings_json: str
    ) -> tuple[int | None, int, str]:
        """(block handle, code, err). code/err meaningful only on None — a
        call-level failure (unknown setting 115, framing, a binary decode
        fault) yields no block and no partial answers (docs/reference/c-abi.md §Blocks).
        """
        fn = self._need("chs_block_parse", "this artifact predates chs_block_parse (rebuild it)")
        code = ctypes.c_int(0)
        err = ctypes.c_void_p()
        with self._lock.read():
            bhandle = fn(
                ctypes.c_void_p(handle),
                fmt,
                body,
                len(body),
                settings_json.encode(),
                ctypes.byref(code),
                ctypes.byref(err),
            )
            if not bhandle:
                return None, int(code.value), self._take_err(err)
        return int(bhandle), 0, ""

    def block_free(self, bhandle: int) -> None:
        fn = self._fn.get("chs_block_free")
        if fn is None:  # pragma: no cover - paired with chs_block_parse
            return
        with self._lock.read():
            fn(ctypes.c_void_p(bhandle))

    def filter_eval(self, fhandle: int, bhandle: int) -> bytes:
        """Evaluate one compiled filter over an already-parsed block — a pure
        function of (filter, block); no settings. Returns the same document
        `chs_filter_rows` returns."""
        fn = self._need("chs_filter_eval", "this artifact predates chs_filter_eval (rebuild it)")
        with self._lock.read():
            out = self._take(fn(ctypes.c_void_p(fhandle), ctypes.c_void_p(bhandle)))
        if out is None:  # pragma: no cover - the C side never returns NULL here
            raise UnsupportedError("chs_filter_eval returned null")
        return out


def _borrowed(fn, handle: int, index: int) -> str:  # noqa: ANN001 - a ctypes _FuncPtr
    """Read a borrowed `const char *` column accessor. Never freed."""
    raw = fn(ctypes.c_void_p(handle), index)
    return raw.decode("utf-8", "surrogateescape") if raw else ""
