"""python.py: the Python binding's generated ABI v1 layer (plan PLAN-sdk-v1-ffi
section 2.2, row "Python").

Three files, from the SAME spec/abi-v1/abi.json every other emitter reads:

  python/src/chtypes/_abi1/_decls.py
      A data table (`FUNCTIONS`) describing every parameter and return shape,
      plus a small, fixed interpreter over that table: ctypes restype/argtypes
      derivation, the handle classes (`close()`/`__del__` calling the right
      free function), the two-phase resolve functions (`resolve_abi_version`
      for loader step 3, `resolve_all` for step 6 -- the full `Api`, whose
      constructor only `resolve_all` can call), and `invoke_by_name()`: a
      table-driven, test-only dispatcher the conformance suite
      (python/tests/abi1/) uses to run every case in
      tests/fixtures/abi-v1/cases.json without one hand-written wrapper per
      function. This file is self-contained (stdlib `ctypes` only; no import
      of this repository's scripts/abi-v1/model.py, which is a generator-only
      module, never shipped with the installed package) and it is the ONLY
      file that may read a `chs_*` attribute: that is what lets
      python/src/chtypes/_abi1/_loader.py (hand-written) call
      `resolve_abi_version()`/`resolve_all()` and the `Api`'s
      non-`chs_`-prefixed methods without ever spelling a `chs_` name itself,
      which is what scripts/abi-v1/check-no-hand-decls.py enforces.

  python/src/chtypes/_abi1/_errmap.py
      spec/abi-v1/sdk.json's status -> error-class and loader-refusal ->
      error-class tables, as references to the hand-written classes in
      python/src/chtypes/_abi1/_errors.py (stable across regenerations; only
      the MAPPING is sdk.json-derived, so a status renumbering or a new
      refusal reason is a regeneration, never a hand edit).

  python/src/chtypes/_abi1/_vocab.py
      Every vocabulary the description defines, as the public API's types:
      `Format` and `Status` (IntEnum), `Outcome`, `FilterOutcome`, `Verdict`
      and `DefaultKind` (StrEnum, each with `of()` reading the description's
      fallback), `Reason` and `Source` (plain strings plus the `lossy` and
      `is_stored` facts), `DocFlags` and `EXPORT_NONE`. Nothing else keeps a
      copy of any of it.

  The typed, copy-then-free call wrappers (docs/reference/bindings-v1.md
  section 8, question 1) are methods of `Api` in `_decls.py`, one per
  status-returning function, named without `chs_`: bytes in, bytes or a handle
  out, every `chs_buf` and `chs_error` read and freed inside the call, a
  non-OK status raised as the class `_errmap` gives it. A `process_serial` or
  `process_once` function takes the image's process lock; nothing else locks.

ctypes conventions (plan section 3.3 "Python", and the F-Py dispatch):
  * `ctypes.CDLL(path, mode=os.RTLD_NOW | os.RTLD_LOCAL)` -- the hand-written
    loader's job, not this file's; this file only sets signatures on an
    already-open library.
  * A `bytes_in` parameter is `(ctypes.c_char_p, ctypes.c_size_t)`: `None`/`0`
    for an empty or absent buffer, the bytes object and its own `len()`
    otherwise. ctypes marshals a `bytes` object to its buffer's address
    without scanning for a NUL, so an embedded NUL or an invalid-UTF-8 byte
    passes through intact -- the explicit length is what the library trusts,
    never a terminator.
  * A `handle` parameter is `ctypes.c_void_p`: the `Handle` wrapper's raw
    pointer value, or `None` for a nullable absent handle.
  * An `out_handle`, `out_error` or `out_scalar` parameter is
    `ctypes.POINTER(...)`: the dispatcher passes `ctypes.byref()` and reads
    the pointed-to value back after the call.
  * A `cstr_static` return is `ctypes.c_char_p`, which ctypes converts
    straight to a Python `bytes` object -- never `str`: a column name or a
    message may not be valid UTF-8 (model.py's CONTENTS documents which).
  * An owned `chs_buf` (an `out_handle` of that type, or a `handle`-kind
    return of that type such as the error text accessors) is read with
    `ctypes.string_at(chs_buf_data(buf), chs_buf_len(buf))` and released with
    `chs_buf_free` right away, since this dispatcher's callers never hold a
    `chs_buf` past the call that produced it.
"""

from __future__ import annotations

from model import Function, Model, Param

from . import Output, banner

DECLS_PATH = "python/src/chtypes/_abi1/_decls.py"
ERRMAP_PATH = "python/src/chtypes/_abi1/_errmap.py"
VOCAB_PATH = "python/src/chtypes/_abi1/_vocab.py"

# model.py's SCALARS gives the C spelling; this gives the matching ctypes
# constructor's SOURCE TEXT (this is code generation: these are strings that
# become Python source, not ctypes objects themselves).
_CTYPES_NUMERIC = {
    "int": "ctypes.c_int",
    "int32": "ctypes.c_int32",
    "uint32": "ctypes.c_uint32",
    "int64": "ctypes.c_int64",
    "uint64": "ctypes.c_uint64",
    "size": "ctypes.c_size_t",
}


def _handle_class_name(handle_name: str) -> str:
    """chs_buf -> BufHandle, chs_schema -> SchemaHandle, ..."""
    assert handle_name.startswith("chs_"), handle_name
    return "".join(w.capitalize() for w in handle_name[len("chs_") :].split("_")) + "Handle"


def _dq(s: str) -> str:
    """A double-quoted Python string literal (ruff format's default quote
    style): plain repr() prefers single quotes, which would make every
    generated line a drift `ruff format --check` wants to rewrite."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _lit(v: object) -> str:
    """A Python literal for a string, None or bool -- the only value types
    this emitter ever places in generated source."""
    if v is None:
        return "None"
    if isinstance(v, bool):
        return "True" if v else "False"
    if isinstance(v, str):
        return _dq(v)
    raise TypeError(f"_lit: unsupported value {v!r}")


def _call_block(head: str, args: list[str]) -> str:
    """`head(arg1, arg2, ...)` on one line when it fits in 100 columns (this
    repository's line length), else one argument per line with a trailing
    comma -- the same shape `ruff format` itself chooses, so there is no
    second, divergent notion of "formatted" for this generator to get wrong."""
    one_line = f"{head}({', '.join(args)})"
    if len(one_line) <= 100 or not args:
        return one_line
    body = "\n".join(f"    {a}," for a in args)
    return f"{head}(\n{body}\n)"


def _dict_block(pairs: list[tuple[str, str]]) -> str:
    """A dict literal, one `key: value,` pair per line -- never the bare
    `{(k: v, ...)}` shape a tuple-block helper would produce if reused for a
    mapping, which is a SyntaxError, not merely unformatted."""
    if not pairs:
        return "{}"
    body = "\n".join(f"    {k}: {v}," for k, v in pairs)
    return "{\n" + body + "\n}"


def _tuple_block(items: list[str]) -> str:
    """One item per line with a trailing comma, so the shape is identical
    whether there are zero, one or many items -- no single-element-tuple
    comma special case to get wrong."""
    if not items:
        return "()"
    body = "\n".join(f"    {it}," for it in items)
    return "(\n" + body + "\n)"


def _param_call(p: Param) -> str:
    args = [_lit(p.name), _lit(p.kind), _lit(p.type)]
    if p.nullable:
        args.append("nullable=True")
    return _call_block("_P", args)


def _add_call(fn: Function) -> str:
    args = [_lit(fn.name), _lit(fn.returns.kind), _lit(fn.returns.type)]
    args += [_param_call(p) for p in fn.params]
    return _call_block("_add", args)


def _status_table(model: Model) -> str:
    """The natural `{0: "CHS_OK", 1: "CHS_REJECTED", ...}` dict literal.
    scripts/check-no-error-code-table.py's Rule B now exempts any file
    carrying the generated banner (this one), so there is no need to reshape
    this into something a lint heuristic can't see -- that pattern is never
    used here (the PM's ruling, round 2): every raw table lives in
    generated, banner-exempt code, in its natural shape."""
    status_enum = model.enums["chs_status"]
    return _dict_block([(str(v.value), _lit(v.name)) for v in status_enum.values])


def _cross_check_literal(model: Model) -> str:
    items = [
        f"({_lit(f['build_info'])}, {_lit(f['predicate'])}, {_lit(f['compare'])})"
        for f in model.sdk["cross_check"]
    ]
    return _tuple_block(items)


# The thread classes (abi.json `thread`) whose calls must not overlap another
# call of their class in one image. Only these take the Api's process lock; a
# `shared` call takes none (the public layer holds no lock around a call
# either), and `handle_serial` is a free, which the public objects' close
# guards order.
_PROCESS_LOCKED = ("process_serial", "process_once")



def _handle_cls(model: Model, handle_type: str) -> str:
    return _handle_class_name(handle_type)


def _param_annotation(model: Model, p: Param) -> str:
    if p.kind in ("scalar", "enum"):
        return "int"
    if p.kind == "bytes_in":
        return "bytes | None"
    if p.kind == "handle":
        ann = _handle_cls(model, p.type)
        return f"{ann} | None" if p.nullable else ann
    raise ValueError(f"python.py: unexpected input param kind {p.kind!r}")


def _out_annotation(model: Model, p: Param) -> str:
    if p.type == "chs_buf":
        return "bytes | None" if p.nullable else "bytes"
    return _handle_cls(model, p.type)


def _wrapper_name(fn: Function) -> str:
    assert fn.name.startswith("chs_"), fn.name
    return fn.name[len("chs_") :]


def _typed_wrappers(model: Model) -> str:
    """One method on `Api` per described status-returning function: bytes in,
    bytes or a handle out, a raised error on a non-OK status (copy-then-free of
    every `chs_buf` and `chs_error` inside the call, as Go's wrappers do). The
    frees, the handle accessors and the three static handshake reads are not
    wrapped here: the handle classes close themselves, and the accessors are
    the read-out's own plumbing."""
    out: list[str] = []
    for fn in model.functions:
        if fn.returns.kind != "status":
            continue
        ins = [p for p in fn.params if not p.is_out]
        outs = [p for p in fn.params if p.kind == "out_handle"]
        name = _wrapper_name(fn)
        ret_types = [_out_annotation(model, p) for p in outs]
        if not ret_types:
            ret = "None"
        elif len(ret_types) == 1:
            ret = ret_types[0]
        else:
            ret = f"tuple[{', '.join(ret_types)}]"
        lines: list[str] = []
        if ins:
            lines.append(f"    def {name}(")
            lines.append("        self,")
            for p in ins:
                lines.append(f"        {p.name}: {_param_annotation(model, p)},")
            lines.append(f"    ) -> {ret}:")
        else:
            lines.append(f"    def {name}(self) -> {ret}:")
        lines.append(f'        """The generated call wrapper for {fn.name} (thread class {fn.thread}).')
        lines.append("")
        lines.append('        Raises the class the status maps to on a non-OK status."""')
        for p in outs:
            lines.append(f"        {p.name} = ctypes.c_void_p()")
        has_err = any(p.kind == "out_error" for p in fn.params)
        if has_err:
            lines.append("        err = ctypes.c_void_p()")
        call_args: list[str] = []
        for p in fn.params:
            if p.kind in ("scalar", "enum"):
                call_args.append(f"int({p.name})")
            elif p.kind == "bytes_in":
                call_args.append(f"*_bytes_in({p.name})")
            elif p.kind == "handle":
                if p.nullable:
                    call_args.append(f"None if {p.name} is None else {p.name}.value")
                else:
                    call_args.append(f"{p.name}.value")
            elif p.kind == "out_handle":
                call_args.append(f"ctypes.byref({p.name})")
            elif p.kind == "out_error":
                call_args.append("ctypes.byref(err)")
            else:
                raise ValueError(f"python.py: {fn.name}: unsupported param kind {p.kind!r}")
        indent = "        "
        if fn.thread in _PROCESS_LOCKED:
            lines.append("        with self._process_lock:")
            indent = "            "
        lines.append(f'{indent}status = self._raw["{fn.name}"](')
        for a in call_args:
            lines.append(f"{indent}    {a},")
        lines.append(f"{indent})")
        lines.append(f"        self._check(status, {'err' if has_err else 'None'})")
        rets: list[str] = []
        for p in outs:
            if p.type == "chs_buf":
                if p.nullable:
                    rets.append(f"None if not {p.name}.value else _read_buf(self, {p.name}.value)")
                else:
                    rets.append(f"_read_buf(self, {p.name}.value)")
            else:
                cls = _handle_cls(model, p.type)
                free = model.handles[p.type].free
                rets.append(f'{cls}({p.name}.value, self._raw["{free}"])')
        if len(rets) == 1:
            lines.append(f"        return {rets[0]}")
        elif rets:
            lines.append("        return (")
            for r in rets:
                lines.append(f"            {r},")
            lines.append("        )")
        out.append("\n".join(lines))
    return "\n\n".join(out)


def render_decls(model: Model) -> str:
    handshake = [fn.name for fn in model.functions if fn.cls == "handshake"]
    handle_names = list(model.handles)  # abi.json's own order: buf, error, schema, filter, block
    handle_classes = {h: _handle_class_name(h) for h in handle_names}

    status_table = _status_table(model)

    add_calls = "\n".join(_add_call(fn) for fn in model.functions)
    all_symbols = _tuple_block([_lit(n) for n in model.symbols()])
    handshake_names = _tuple_block([_lit(n) for n in handshake])
    handle_free = _dict_block([(_lit(h), _lit(model.handles[h].free)) for h in handle_names])
    ok_value = next(v.value for v in model.enums["chs_status"].values if v.name == "CHS_OK")
    typed_wrappers = _typed_wrappers(model)
    handle_class_dict = _dict_block([(_lit(h), cls) for h, cls in handle_classes.items()])
    cross_check = _cross_check_literal(model)

    handle_class_defs = "\n\n\n".join(
        f'class {cls}(Handle):\n    """A `{h}` the library owns the free function for."""'
        for h, cls in handle_classes.items()
    )

    return f'''# {banner(model)}  # noqa: E501
"""The Python binding's generated ABI v1 layer: ctypes signatures, the handle
classes, the two-phase resolve functions and invoke_by_name(), the test-only
dispatcher the conformance suite drives from tests/fixtures/abi-v1/cases.json.
See scripts/abi-v1/emit/python.py's module docstring for the design this file
implements and why. This is the ONLY python/src/chtypes/_abi1 file that reads
a chs_* attribute (scripts/abi-v1/check-no-hand-decls.py exempts it by its
banner, above); every other file in this package calls the non-chs_-prefixed
names this module exposes instead.
"""

from __future__ import annotations

import ctypes
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from . import _errmap

CHS_ABI_VERSION = {model.abi}
CHS_ABI_FINGERPRINT = {_dq(model.fingerprint)}

# sdk.json's loader.cross_check: (build_info field, predicate field, how to
# compare), read by _loader.py's step 5 without it ever touching sdk.json
# itself (not part of the installed package).
CROSS_CHECK_FIELDS: tuple[tuple[str, str, str], ...] = {cross_check}

# chs_status's values, both directions. Frozen by D3, but still generated
# from the description rather than hand-copied, so a future enum change is a
# regeneration, never a drift.
STATUS_BY_VALUE: dict[int, str] = {status_table}
STATUS_BY_NAME: dict[str, int] = {{v: k for k, v in STATUS_BY_VALUE.items()}}
_STATUS_OK = {ok_value}

# handle type name -> its free function's name (never called directly outside
# this file: Handle.close() holds the bound method, resolved once in
# resolve_all()).
HANDLE_FREE: dict[str, str] = {handle_free}

ALL_SYMBOLS: tuple[str, ...] = {all_symbols}
HANDSHAKE_NAMES: tuple[str, ...] = {handshake_names}

_CTYPES_NUMERIC: dict[str, type] = {{
    "int": ctypes.c_int,
    "int32": ctypes.c_int32,
    "uint32": ctypes.c_uint32,
    "int64": ctypes.c_int64,
    "uint64": ctypes.c_uint64,
    "size": ctypes.c_size_t,
}}


class MissingSymbol(Exception):
    """Step 3 or step 6 (plan section 3.2) could not resolve `name` in the
    opened library -- ctypes' own getattr-is-dlsym behavior raised
    AttributeError, which this file turns into a named exception so
    _loader.py never has to spell a chs_ name to report which one."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


class Handle:
    """A reference-counted, kind- and image-tagged C handle (D2): closing it
    releases this binding's own reference, any number of times, from any
    thread, in any order relative to other handles -- never from a __del__
    racing a live reference elsewhere, since CPython only runs a finalizer
    once nothing refers to the object. `value` is the raw pointer (an int,
    for echo/identity comparisons in the conformance suite) or None once
    closed."""

    __slots__ = ("_closed", "_free", "_ptr")

    def __init__(self, ptr: int, free: Callable[..., object]) -> None:
        self._ptr = ptr
        self._free = free
        self._closed = ptr is None or ptr == 0

    @property
    def value(self) -> int | None:
        return None if self._closed else self._ptr

    def close(self) -> None:
        if not self._closed:
            self._free(self._ptr)
            self._closed = True
            self._ptr = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:  # noqa: BLE001 - a finalizer must never raise
            pass


{handle_class_defs}


_HANDLE_CLASS: dict[str, type[Handle]] = {handle_class_dict}


@dataclass(frozen=True)
class _P:
    """One function parameter, as the description's closed param-kind
    vocabulary (model.py's PARAM_KINDS) names it: scalar | enum | bytes_in |
    handle | out_handle | out_error | out_scalar. `type` is a scalar name, a
    handle type name, or None (bytes_in, out_error)."""

    name: str
    kind: str
    type: str | None
    nullable: bool = False


@dataclass(frozen=True)
class _F:
    """One described function: its return shape and its parameters, in
    declaration order -- everything invoke_by_name() and the signature
    setters need, read generically rather than unrolled per function, so a
    regeneration changes only FUNCTIONS, never this interpreter."""

    name: str
    returns_kind: str
    returns_type: str | None
    params: tuple[_P, ...] = ()


FUNCTIONS: dict[str, _F] = {{}}


def _add(name: str, returns_kind: str, returns_type: str | None, *params: _P) -> None:
    FUNCTIONS[name] = _F(name, returns_kind, returns_type, params)


{add_calls}


def _argtypes(spec: _F) -> list[type]:
    out: list[type] = []
    for p in spec.params:
        if p.kind == "scalar":
            out.append(_CTYPES_NUMERIC[p.type])
        elif p.kind == "enum":
            out.append(ctypes.c_int32)
        elif p.kind == "bytes_in":
            out += [ctypes.c_char_p, ctypes.c_size_t]
        elif p.kind == "handle":
            out.append(ctypes.c_void_p)
        elif p.kind in ("out_handle", "out_error"):
            out.append(ctypes.POINTER(ctypes.c_void_p))
        else:  # out_scalar
            out.append(ctypes.POINTER(_CTYPES_NUMERIC[p.type]))
    return out


def _restype(spec: _F) -> type | None:
    if spec.returns_kind == "status":
        return ctypes.c_int32
    if spec.returns_kind == "void":
        return None
    if spec.returns_kind == "enum":
        return ctypes.c_int32
    if spec.returns_kind == "handle":
        return ctypes.c_void_p
    # scalar
    if spec.returns_type == "cstr_static":
        return ctypes.c_char_p
    if spec.returns_type == "u8ptr_const":
        return ctypes.c_void_p
    return _CTYPES_NUMERIC[spec.returns_type]


def _set_signature(fn: Callable[..., object], spec: _F) -> None:
    fn.argtypes = _argtypes(spec)
    restype = _restype(spec)
    if restype is not None:
        fn.restype = restype


def resolve_abi_version(lib: ctypes.CDLL) -> Callable[..., object]:
    """Loader step 3's dlsym: ctypes resolves a CDLL attribute lazily (a
    dlsym, not a call), so a plain getattr IS the resolution; AttributeError
    is "not present"."""
    try:
        fn = lib.chs_abi_version
    except AttributeError:
        raise MissingSymbol("chs_abi_version") from None
    _set_signature(fn, FUNCTIONS["chs_abi_version"])
    return fn


class Api:
    """Every described symbol, resolved and signature-typed, step 6
    (plan section 3.2). Constructed ONLY by resolve_all(): the constructor
    checks a private key, so a loader that skipped resolution -- and so
    might hold a partially-typed or entirely absent symbol table -- cannot
    silently produce one (the "two-phase table" rule, plan section 2.2)."""

    def __init__(self, lib: ctypes.CDLL, raw: dict, key: object) -> None:
        if key is not _API_KEY:
            raise TypeError("Api is constructed only by resolve_all()")
        self._lib = lib
        self._raw = raw
        self._process_lock = threading.RLock()

    def build_info(self) -> bytes | None:
        return self._raw["chs_build_info"]()

    def clickhouse_version(self) -> bytes | None:
        return self._raw["chs_clickhouse_version"]()

    def abi_revision(self) -> int:
        return self._raw["chs_abi_revision"]()

    def _check(self, status: int, err: ctypes.c_void_p | None) -> None:
        """Turn a non-OK `status` into the class the description maps it to,
        carrying the `chs_error` fields read verbatim (and freed) -- or, for a
        status outside the closed set, an InternalError naming its value."""
        if status == _STATUS_OK:
            return
        info = None
        if err is not None and err.value:
            info = _decode_error(self, err.value)
        name = STATUS_BY_VALUE.get(status, f"UNKNOWN:{{status}}")
        cls = _errmap.call_error_class(name)
        message = info.message if info else b""
        if name not in STATUS_BY_NAME:
            message = f"status {{status}} is outside the closed chs_status set".encode() + (
                b": " + message if message else b""
            )
        raise cls(
            status,
            info.ch_code if info else 0,
            info.ch_name.decode("ascii", "replace") if info else "",
            message,
            info.column if info else b"",
        )

{typed_wrappers}


_API_KEY = object()


def resolve_all(lib: ctypes.CDLL) -> Api:
    """Every described symbol, in sorted, deterministic order -- chs_abi_version
    INCLUDED, even though _loader.py's step 3 (resolve_abi_version) already
    resolved it before this runs: the test-only invoke_by_name() dispatcher
    below needs every symbol reachable through ONE Api, handshake class
    included, and re-resolving an already-present symbol is harmless (by the
    time step 6 runs, step 3 has already proven it exists). The first symbol
    ctypes cannot dlsym raises MissingSymbol(name); _loader.py's step 6 turns
    that into "missing_symbol:<name>", matching every missing-<sym> stub
    variant (tests/fixtures/abi-v1/cases.json), chs_build_info and
    chs_clickhouse_version included -- loader step 4 needs chs_build_info
    already resolved, which is exactly why this sweep must run before it."""
    raw: dict = {{}}
    for name in ALL_SYMBOLS:
        try:
            fn = getattr(lib, name)
        except AttributeError:
            raise MissingSymbol(name) from None
        _set_signature(fn, FUNCTIONS[name])
        raw[name] = fn
    return Api(lib, raw, _API_KEY)


@dataclass
class ErrorInfo:
    status: str
    ch_code: int
    ch_name: bytes
    message: bytes
    column: bytes


@dataclass
class InvokeResult:
    status: str | None = None
    int_value: int | None = None
    bytes_value: bytes | None = None
    error: ErrorInfo | None = None
    outputs: dict[str, bytes] = field(default_factory=dict)
    out_handles: dict[str, Handle] = field(default_factory=dict)
    out_scalars: dict[str, int] = field(default_factory=dict)


def _bytes_in(data: bytes | None) -> tuple[bytes | None, int]:
    """A `bytes_in` parameter's two C arguments: (NULL, 0) for empty or
    absent, else the bytes and their own length. ctypes hands a `bytes`
    object's buffer over without scanning for a NUL, so NUL and invalid UTF-8
    cross intact; any other bytes-like object is copied to `bytes` first."""
    if not data:
        return None, 0
    if not isinstance(data, bytes):
        data = bytes(data)
    return data, len(data)


def _read_buf(api: Api, ptr: int | None) -> bytes:
    """Read and free an owned chs_buf (an out_handle of that type, or a
    handle-kind return of that type): string_at(data, len) then
    chs_buf_free, matching every caller's "copy then free" contract (plan
    section 2.2's Python row)."""
    if not ptr:
        return b""
    data_ptr = api._raw["chs_buf_data"](ptr)
    n = api._raw["chs_buf_len"](ptr)
    data = b"" if not data_ptr else ctypes.string_at(data_ptr, n)
    api._raw["chs_buf_free"](ptr)
    return data


def _wrap_or_read(
    api: Api, handle_type: str, ptr: int | None
) -> tuple[bytes | None, Handle | None]:
    """A handle-kind value of type chs_buf is read to bytes and freed
    immediately (nothing in this dispatcher holds a chs_buf past the call
    that produced it); any other handle kind is wrapped, alive, for a later
    call to use as an input."""
    if ptr is None or not ptr:
        return (None, None)
    if handle_type == "chs_buf":
        return (_read_buf(api, ptr), None)
    cls = _HANDLE_CLASS[handle_type]
    free = api._raw[HANDLE_FREE[handle_type]]
    return (None, cls(ptr, free))


def _decode_error(api: Api, err_ptr: int) -> ErrorInfo:
    status_val = api._raw["chs_error_status"](err_ptr)
    ch_code = api._raw["chs_error_ch_code"](err_ptr)
    ch_name = _read_buf(api, api._raw["chs_error_ch_name"](err_ptr))
    message = _read_buf(api, api._raw["chs_error_message"](err_ptr))
    column = _read_buf(api, api._raw["chs_error_column"](err_ptr))
    api._raw["chs_error_free"](err_ptr)
    return ErrorInfo(
        status=STATUS_BY_VALUE.get(status_val, f"UNKNOWN:{{status_val}}"),
        ch_code=ch_code,
        ch_name=ch_name,
        message=message,
        column=column,
    )


def invoke_by_name(api: Api, name: str, args: Iterable[object] = ()) -> InvokeResult:
    """Call the described function `name` generically, from its FUNCTIONS
    entry alone: `args` holds exactly the INPUT parameters (every kind but
    out_handle/out_error/out_scalar), in declaration order, already built as
    Python values -- an int for scalar/enum, bytes for bytes_in (None and 0
    both mean "no bytes"; this dispatcher passes None for either), a Handle
    (or None, where nullable) for handle. Output parameters are never
    supplied by the caller: this dispatcher allocates them, and decodes them
    into the returned InvokeResult. Test-only: every chs_ spelling below is
    why this file, alone in python/src/chtypes/_abi1, carries the generated
    banner check-no-hand-decls.py exempts."""
    spec = FUNCTIONS[name]
    fn = api._raw[name]
    call_args: list[object] = []
    out_handle_params: list[_P] = []
    out_error_present = False
    out_scalar_params: list[_P] = []
    it = iter(args)
    for p in spec.params:
        if p.kind in ("scalar", "enum"):
            call_args.append(int(next(it)))
        elif p.kind == "bytes_in":
            data = next(it)
            if not data:
                call_args += [None, 0]
            else:
                call_args += [data, len(data)]
        elif p.kind == "handle":
            h = next(it)
            call_args.append(None if h is None else h.value)
        elif p.kind == "out_handle":
            var = ctypes.c_void_p()
            out_handle_params.append((p, var))
            call_args.append(ctypes.byref(var))
        elif p.kind == "out_error":
            out_error_present = True
            err_var = ctypes.c_void_p()
            call_args.append(ctypes.byref(err_var))
        else:  # out_scalar
            ctype = _CTYPES_NUMERIC[p.type]
            var = ctype()
            out_scalar_params.append((p, var))
            call_args.append(ctypes.byref(var))

    raw_result = fn(*call_args)
    result = InvokeResult()

    if spec.returns_kind == "status":
        result.status = STATUS_BY_VALUE.get(raw_result, f"UNKNOWN:{{raw_result}}")
        if result.status != "CHS_OK" and out_error_present and err_var.value:
            result.error = _decode_error(api, err_var.value)
    elif spec.returns_kind == "scalar":
        if spec.returns_type == "cstr_static":
            result.bytes_value = raw_result
        else:
            result.int_value = raw_result
    elif spec.returns_kind == "enum":
        result.int_value = raw_result
    elif spec.returns_kind == "handle":
        data, handle = _wrap_or_read(api, spec.returns_type, raw_result)
        if data is not None:
            result.bytes_value = data
        elif handle is not None:
            result.out_handles["__return__"] = handle

    for p, var in out_handle_params:
        if not var.value:
            continue
        data, handle = _wrap_or_read(api, p.type, var.value)
        if data is not None:
            result.outputs[p.name] = data
        elif handle is not None:
            result.out_handles[p.name] = handle
    for p, var in out_scalar_params:
        result.out_scalars[p.name] = var.value
    return result
'''


# The member names the description cannot spell for itself: a verdict is one
# character, and the empty default kind has no spelling. Every other
# vocabulary value is its own name upper-cased. A value added to the
# description without a name here fails generation (never silently skipped).
_MEMBER_NAMES: dict[tuple[str, str], str] = {
    ("filter_verdict", "t"): "TRUE",
    ("filter_verdict", "f"): "FALSE",
    ("filter_verdict", "e"): "ERROR",
    ("filter_verdict", "d"): "DECLINE",
    ("default_kind", ""): "NONE",
}


def _member(vocab: str, value: str) -> str:
    name = _MEMBER_NAMES.get((vocab, value), value.upper())
    if not name.isidentifier():
        raise ValueError(f"python.py: {vocab} value {value!r} has no identifier spelling")
    return name


def _int_members(enum) -> list[tuple[str, int]]:
    """`CHS_JSON_EACH_ROW` -> `JSON_EACH_ROW`: the C constant without its prefix."""
    out = []
    for v in enum.values:
        assert v.name.startswith("CHS_"), v.name
        out.append((v.name[len("CHS_") :], v.value))
    return out


def _str_members(model: Model, vocab: str) -> list[tuple[str, str, dict]]:
    enum = model.enums[vocab]
    members = [(_member(vocab, v.value), v.value, v.fields) for v in enum.values]
    names = [m[0] for m in members]
    if len(set(names)) != len(names):
        raise ValueError(f"python.py: {vocab}: two values share a member name")
    return members


def _fallback_member(model: Model, vocab: str) -> str | None:
    fb = model.enums[vocab].fallback
    return None if fb is None else _member(vocab, fb)


def _str_enum(model: Model, cls: str, vocab: str, doc: str, extra: str = "") -> str:
    members = _str_members(model, vocab)
    fb = _fallback_member(model, vocab)
    lines = [f"class {cls}(StrEnum):", f'    """{doc}"""', ""]
    for name, value, _ in members:
        lines.append(f"    {name} = {_dq(value)}")
    lines.append("")
    lines.append("    @classmethod")
    lines.append(f"    def of(cls, text: str) -> {cls}:")
    if fb is None:
        lines.append(f'        """Map a document\'s spelling. The description gives this vocabulary no')
        lines.append('        fallback, so a spelling it does not list raises ValueError."""')
        lines.append("        return cls(text)")
    else:
        lines.append(f'        """Map a document\'s spelling; one the description does not list reads as')
        lines.append(f'        its fallback, `{cls}.{fb}`."""')
        lines.append("        try:")
        lines.append("            return cls(text)")
        lines.append("        except ValueError:")
        lines.append(f"            return cls.{fb}")
    if extra:
        lines.append("")
        lines.append(extra)
    return "\n".join(lines)


def render_vocab(model: Model) -> str:
    # Outcome is the row vocabulary, which holds every batch value too: one
    # Python type for both, as the spec's vocabulary table has it.
    row = {v.value for v in model.enums["row_outcome"].values}
    batch = {v.value for v in model.enums["batch_outcome"].values}
    if not batch <= row:
        raise ValueError("python.py: batch_outcome has a value row_outcome lacks")
    if model.enums["row_outcome"].fallback != model.enums["batch_outcome"].fallback:
        raise ValueError("python.py: row_outcome and batch_outcome disagree on their fallback")

    fmt_members = _int_members(model.enums["chs_format"])
    status_members = _int_members(model.enums["chs_status"])
    fmt_names = {
        _dq(name): _dq(v.fields["ch_name"])
        for (name, _), v in zip(fmt_members, model.enums["chs_format"].values, strict=True)
    }

    verdict_answered = {
        _member("filter_verdict", v.value): v.fields["answered"]
        for v in model.enums["filter_verdict"].values
    }
    if sum(verdict_answered.values()) < 2:
        raise ValueError("python.py: expected at least two answered verdicts")
    answered_names = ", ".join(f"Verdict.{n}" for n, a in verdict_answered.items() if a)

    reasons = _str_members(model, "transform_reason")
    sources = _str_members(model, "value_src")
    reason_fb = model.enums["transform_reason"].fallback
    reason_lossy = {v.value: v.fields["lossy"] for v in model.enums["transform_reason"].values}
    fb_lossy = reason_lossy[reason_fb]

    c = model.constants
    for need in ("CHS_DOC_VALUES", "CHS_DOC_TRANSFORMS", "CHS_DOC_DEFAULTS", "CHS_DOC_ALL"):
        if need not in c:
            raise ValueError(f"python.py: the description has no constant {need}")

    parts: list[str] = []
    parts.append(f"# {banner(model)}  # noqa: E501")
    parts.append(
        '"""The Python binding\'s generated vocabularies, from spec/abi-v1/abi.json: the C\n'
        "enums (`Format`, `Status`), the document vocabularies (`Outcome`, `FilterOutcome`,\n"
        "`Verdict`, `DefaultKind`, `Reason`, `Source`) with the facts the description\n"
        "attaches to each value (`Format.ch_name`, `Reason` lossy, `Source` is_stored,\n"
        "`Verdict.answered`) and each vocabulary's fallback, and the `DocFlags` groups.\n"
        'No other file in this package keeps a copy of any of it."""\n'
    )
    parts.append("from __future__ import annotations\n")
    parts.append("from enum import IntEnum, IntFlag, StrEnum")
    parts.append("from typing import Final\n")

    parts.append("\nclass Format(IntEnum):")
    parts.append('    """The `chs_format` codes. The numbers are part of the ABI."""\n')
    for n, v in fmt_members:
        parts.append(f"    {n} = {v}")
    parts.append("")
    parts.append("    @property")
    parts.append("    def ch_name(self) -> str:")
    parts.append("        \"\"\"ClickHouse's own name for this format, as `capabilities` lists it.\"\"\"")
    parts.append("        return _FORMAT_CH_NAME[self.name]")
    parts.append("")
    parts.append("")
    parts.append(f"_FORMAT_CH_NAME: Final[dict[str, str]] = {_dict_block(list(fmt_names.items()))}")
    parts.append("")
    parts.append("")
    parts.append("class Status(IntEnum):")
    parts.append('    """The `chs_status` values (D3: five, closed, frozen)."""\n')
    for n, v in status_members:
        parts.append(f"    {n} = {v}")
    parts.append("")
    parts.append("")
    parts.append(
        _str_enum(
            model,
            "Outcome",
            "row_outcome",
            "The verdict on a row or a batch, in the document's own vocabulary.",
        )
    )
    parts.append("")
    parts.append("")
    parts.append(
        _str_enum(
            model,
            "FilterOutcome",
            "filter_outcome",
            "The call-level verdict of a filter evaluation.",
        )
    )
    parts.append("")
    parts.append("")
    parts.append(
        _str_enum(
            model,
            "Verdict",
            "filter_verdict",
            "One row's answer from a filter, in the document's own characters.",
            extra=(
                "    @property\n"
                "    def answered(self) -> bool:\n"
                '        """The description\'s own fact: whether this verdict is an ANSWER (true or\n'
                "        false) rather than an error or a decline. A caller enforcing visibility\n"
                '        fails closed on every verdict for which this is False."""\n'
                f"        return self in ({answered_names})"
            ),
        )
    )
    parts.append("")
    parts.append("")
    parts.append(
        _str_enum(
            model,
            "DefaultKind",
            "default_kind",
            "A column's default kind, `\"\"` meaning none.",
        )
    )
    parts.append("")
    parts.append("")
    parts.append("class Reason:")
    parts.append('    """The `transform_reason` values, kept as plain strings: a reason from a newer')
    parts.append("    library passes through with its spelling, and takes the fallback's lossy fact.")
    parts.append('    """\n')
    for n, v, _ in reasons:
        parts.append(f"    {n}: Final = {_dq(v)}")
    parts.append("")
    parts.append("    @staticmethod")
    parts.append("    def lossy(reason: str) -> bool:")
    parts.append('        """The description\'s `lossy` fact for `reason`; an unlisted reason reads as')
    parts.append(f'        the fallback `{reason_fb}`."""')
    parts.append(f"        return _REASON_LOSSY.get(reason, {fb_lossy})")
    parts.append("")
    parts.append("")
    parts.append(
        "_REASON_LOSSY: Final[dict[str, bool]] = "
        + _dict_block([(_dq(v), _lit(f["lossy"])) for _, v, f in reasons])
    )
    parts.append("")
    parts.append("")
    parts.append("class Source:")
    parts.append('    """The `value_src` values, kept as plain strings. The vocabulary has no fallback,')
    parts.append("    so a source the description does not list is an error to the decoder, never a")
    parts.append('    guess."""\n')
    for n, v, _ in sources:
        parts.append(f"    {n}: Final = {_dq(v)}")
    parts.append("")
    parts.append("    @staticmethod")
    parts.append("    def is_stored(source: str) -> bool:")
    parts.append('        """The description\'s `is_stored` fact for `source`. Raises KeyError for a')
    parts.append('        source the description does not list."""')
    parts.append("        return _SOURCE_IS_STORED[source]")
    parts.append("")
    parts.append("")
    parts.append(
        "_SOURCE_IS_STORED: Final[dict[str, bool]] = "
        + _dict_block([(_dq(v), _lit(f["is_stored"])) for _, v, f in sources])
    )
    parts.append("")
    parts.append("")
    parts.append("class DocFlags(IntFlag):")
    parts.append('    """The document groups a `rows` call asks for (`CHS_DOC_*`)."""\n')
    parts.append(f"    VALUES = {c['CHS_DOC_VALUES'].value}")
    parts.append(f"    TRANSFORMS = {c['CHS_DOC_TRANSFORMS'].value}")
    parts.append(f"    DEFAULTS = {c['CHS_DOC_DEFAULTS'].value}")
    parts.append(f"    ALL = {c['CHS_DOC_ALL'].value}")
    parts.append("")
    parts.append("")
    parts.append(
        f"# `chs_preview_batch`'s `export_format` for no export (`CHS_EXPORT_NONE`).\nEXPORT_NONE: Final = {c['CHS_EXPORT_NONE'].value}"
    )
    return "\n".join(parts) + "\n"


def render_errmap(model: Model) -> str:
    sdk = model.sdk
    status_pairs = [
        (_lit(status), _lit(cls))
        for status, cls in sdk["errors"]["status"].items()
        if status != "unknown"
    ]
    unknown_class = sdk["errors"]["status"]["unknown"]
    # A reason can appear on more than one refusal row (e.g. "missing_symbol"
    # at both step 4 and step 6: sdk.json names the step a refusal is FIRST
    # raised at, not a second axis of the mapping) -- always the same error
    # class for the same reason, so dedupe by reason, first occurrence wins,
    # rather than emit a repeated dict key literal.
    refusal_by_reason: dict[str, str] = {}
    for r in sdk["loader"]["refusals"]:
        refusal_by_reason.setdefault(r["reason"], r["error"])
    refusal_pairs = [(_lit(reason), _lit(error)) for reason, error in refusal_by_reason.items()]
    class_pairs = [(_lit(key), f"_errors.{c['python']}") for key, c in sdk["errors"]["classes"].items()]
    code_pairs = [(_lit(k), _lit(v)) for k, v in sdk["errors"]["codes"].items()]

    return f'''# {banner(model)}  # noqa: E501
"""spec/abi-v1/sdk.json's error-mapping tables, as references to the
public classes (python/src/chtypes/errors.py, re-exported by
python/src/chtypes/_abi1/_errors.py). Only the MAPPING is generated: the
classes themselves are stable across a regeneration (plan section 3.4), so a
status renumbering or a new loader refusal reason changes this file, never
the classes.
"""

from __future__ import annotations

from . import _errors

# chs_status name -> the sdk.json error-class KEY (an index into CLASS_BY_KEY,
# below); "unknown" covers a status value outside the closed set, which
# cannot happen under a matching fingerprint but is handled rather than
# trusted away (plan section 3.4).
STATUS_CLASS_KEY: dict[str, str | None] = {_dict_block(status_pairs)}
UNKNOWN_STATUS_CLASS_KEY: str = {_lit(unknown_class)}

# a loader refusal's BASE reason (before any ":<suffix>") -> the sdk.json
# error-class key.
LOADER_REFUSAL_CLASS_KEY: dict[str, str] = {_dict_block(refusal_pairs)}

# sdk.json error-class key -> the actual exception class.
CLASS_BY_KEY: dict[str, type[_errors.ChtypesError]] = {_dict_block(class_pairs)}

# sdk.json's error codes (reserved exit-status names; plan section 3.4, Q-m).
ERROR_CODES: dict[str, str] = {_dict_block(code_pairs)}


def call_error_class(status: str) -> type[_errors.ChtypesError] | None:
    """The exception class a `status` other than CHS_OK maps to, or None for
    CHS_OK itself."""
    key = STATUS_CLASS_KEY.get(status, UNKNOWN_STATUS_CLASS_KEY)
    return None if key is None else CLASS_BY_KEY[key]


def loader_error_class(reason: str) -> type[_errors.ChtypesError]:
    """The exception class a loader refusal `reason` (its base, before any
    ":<suffix>") maps to."""
    base = reason.split(":", 1)[0]
    return CLASS_BY_KEY[LOADER_REFUSAL_CLASS_KEY[base]]
'''


def outputs(model: Model) -> list[Output]:
    return [
        Output(DECLS_PATH, content=render_decls(model)),
        Output(ERRMAP_PATH, content=render_errmap(model)),
        Output(VOCAB_PATH, content=render_vocab(model)),
    ]
