"""The generated Rust layer for ABI v1: `rust/src/abi1/decls.rs`,
`invoke_gen.rs`, `calls_gen.rs`, `vocab_gen.rs` and `errmap_gen.rs`.

`calls_gen.rs` is the typed, memory-safe call layer the crate's public API
calls (one method per status-returning `api` call, named without the `chs_`
prefix, copying and freeing every `chs_buf` and `chs_error` inside the call
that produced it, plus a Drop-freed wrapper per minted handle). `vocab_gen.rs`
generates every vocabulary (formats, statuses, outcomes, verdicts, reasons with
their `lossy` fact, sources with their `is_stored` fact, default kinds, document
groups), and `errmap_gen.rs` the sdk.json error tables. None of the three is
edited by hand.

The two files below are the FFI layer for ABI v1 — `extern "C"` type aliases, opaque handle structs, the
`Api` symbol table (built by `Api::resolve_all`, loader step 6), the small
`Handshake` table (the two symbols the loader's steps 3-4 need before the rest
of the table is safe to resolve, D1.2's "nothing before the checks"), and a
test-only invoke-by-name dispatcher (`invoke`) that marshals one
already-resolved argument list into a real FFI call and reports back a
generic `Outcome`, for `rust/tests/abi1_conformance.rs`'s case runner.

WHY TWO FILES. `decls.rs` is the part `rust/src/abi1/loader.rs` (hand-written)
needs: handle types, function-pointer type aliases, `Api`/`Handshake`.
`invoke_gen.rs` is conformance-only glue, one match arm per function
`scripts/abi-v1/emit/stub.py`'s `classify()` calls "generic" (the
echo/status-injection shape — see that module's docstring) plus the four
handshake/tombstone calls; free-like functions never appear here because no
case ever calls one by name (`emit/cases.py` never builds one for a
"free"-classified function). Keeping the free functions out of this match,
rather than generating a no-op arm for each, means a function that
`classify()` moves from "free" to "generic" (or back) is the only case that
changes this file's shape — nothing here duplicates that classification.

WHAT STAYS HAND-WRITTEN (never generated, and deliberately so little of it
touches a `chs_*` name that `check-no-hand-decls.py --scope v1` sees nothing
to flag): the loader's step-by-step logic (`rust/src/abi1/loader.rs`), and
the case-tree walker in `rust/tests/abi1_conformance.rs` that turns
`tests/fixtures/abi-v1/cases.json`'s JSON argument expressions into the
`ResolvedArg`s `invoke` consumes, frees minted handles, and writes the
report. Both call into `decls.rs` and `invoke_gen.rs` by name, never
declaring or resolving a `chs_*` symbol themselves. (`rust/src/abi1/mod.rs`
explains why the conformance runner is a `rust/tests/` file that re-declares
these two generated modules with `#[path]`, rather than a third file under
`rust/src/abi1/`: it is not part of this crate's public API and an
integration test cannot reach a private module.)

Every output is piped through the installed `rustfmt` before being returned
(`_rustfmt`, below), so what lands in the tree is already clean under
`cargo fmt --check` (plan §2.3: "the emitters write formatted code") — this
emitter's own string templates make no attempt to match rustfmt's line-width
and wrapping rules by hand.
"""

from __future__ import annotations

import shutil
import subprocess

from model import BUF_HANDLE, ERROR_HANDLE, SCALARS, STATUS_ENUM

from . import Output, banner, stub


def _rustfmt(text: str) -> str:
    """Format generated Rust source with the `rustfmt` on PATH (it ships
    with any standard Rust toolchain, including GitHub's hosted
    `ubuntu-latest` runners — the same assumption `v1-abi-gen`'s header
    syntax-check step already makes of `gcc`/`clang`). Deterministic given a
    fixed rustfmt version, which is what `gen.py --selftest`'s "--write run
    twice is byte-identical" check relies on."""
    rustfmt = shutil.which("rustfmt")
    if rustfmt is None:
        raise RuntimeError(
            "scripts/abi-v1/emit/rust.py needs `rustfmt` on PATH to format its generated output"
        )
    proc = subprocess.run([rustfmt, "--edition", "2024"], input=text, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"rustfmt failed on generated Rust source:\n{proc.stderr}")
    return proc.stdout

DECLS_PATH = "rust/src/abi1/decls.rs"
INVOKE_PATH = "rust/src/abi1/invoke_gen.rs"

# model.py's scalar vocabulary -> the Rust type an `extern "C"` declaration
# spells it as. `void` is handled separately (it is a return-only absence,
# never a parameter), and `int`/`u8ptr_const`/`cstr_static` are return-only
# too (model.py refuses them as a plain parameter kind), so this table is
# total over every scalar a PARAMETER can actually carry as well as every one
# a RETURN can.
SCALAR_RUST: dict[str, str] = {
    "void": "()",
    "int": "std::os::raw::c_int",
    "int32": "i32",
    "uint32": "u32",
    "int64": "i64",
    "uint64": "u64",
    "size": "usize",
    "u8ptr_const": "*const u8",
    "cstr_static": "*const std::os::raw::c_char",
}
assert set(SCALAR_RUST) == set(SCALARS), "model.py's scalar vocabulary moved; update SCALAR_RUST"


def _pascal(name: str) -> str:
    """`chs_back_quote_if_needed` -> `ChsBackQuoteIfNeeded`."""
    return "".join(part[:1].upper() + part[1:] for part in name.split("_") if part)


def handle_rust_name(handle: str) -> str:
    return _pascal(handle)


def fn_type_name(name: str) -> str:
    return "Fn" + _pascal(name)


def _rust_str(s: str) -> str:
    """A Rust `&str` literal. Every string this module quotes (a function,
    parameter or handle name) is `[a-z0-9_]+`, ASCII and free of `"` or `\\`,
    so no escaping beyond what `repr`-style quoting already gives is needed;
    asserted here rather than assumed."""
    assert all(c.isascii() and c not in '"\\' for c in s), s
    return f'"{s}"'


def param_rust_types(p) -> list[tuple[str, str]]:
    """[(rust_type, c_parameter_name), ...] for one Param, in the same order
    as `p.c` (one entry for a scalar/enum/handle, two for bytes_in)."""
    if p.kind == "scalar":
        return [(SCALAR_RUST[p.type], p.c[0].name)]
    if p.kind == "enum":
        return [("i32", p.c[0].name)]
    if p.kind == "bytes_in":
        return [("*const u8", p.c[0].name), ("usize", p.c[1].name)]
    if p.kind == "handle":
        t = handle_rust_name(p.type)
        ptr = "*const" if p.const else "*mut"
        return [(f"{ptr} {t}", p.c[0].name)]
    if p.kind == "out_handle":
        return [(f"*mut *mut {handle_rust_name(p.type)}", p.c[0].name)]
    if p.kind == "out_error":
        return [(f"*mut *mut {handle_rust_name(ERROR_HANDLE)}", p.c[0].name)]
    if p.kind == "out_scalar":
        return [(f"*mut {SCALAR_RUST[p.type]}", p.c[0].name)]
    raise ValueError(f"param_rust_types: unknown kind {p.kind!r}")


def return_rust_type(r) -> str:
    if r.kind == "status":
        return "i32"
    if r.kind == "void":
        return "()"
    if r.kind == "scalar":
        return SCALAR_RUST[r.type]
    if r.kind == "enum":
        return "i32"
    if r.kind == "handle":
        return f"*mut {handle_rust_name(r.type)}"
    raise ValueError(f"return_rust_type: unknown kind {r.kind!r}")


def render_fn_type(fn) -> str:
    types = [t for p in fn.params for t, _ in param_rust_types(p)]
    ret = return_rust_type(fn.returns)
    tail = "" if ret == "()" else f" -> {ret}"
    args = ", ".join(types)
    return f"pub(crate) type {fn_type_name(fn.name)} = unsafe extern \"C\" fn({args}){tail};"


# --------------------------------------------------------------- decls.rs


def render_decls(model) -> str:
    out: list[str] = [
        f"// {banner(model)}",
        "//",
        "// The opaque handle types, the `extern \"C\"` function-pointer type for",
        "// every described function, the resolved symbol tables (`Handshake`, the",
        "// two symbols loader steps 3-4 need; `Api`, every symbol, step 6), and the",
        "// small facts (`CHS_ABI_VERSION`, `CHS_ABI_FINGERPRINT`, the build_info",
        "// cross-check field map, the chs_status name table) the hand-written",
        "// loader and conformance runner read by name. See scripts/abi-v1/emit/rust.py.",
        "#![allow(non_camel_case_types, dead_code)]",
        "",
        "use std::os::raw::c_char;",
        "",
        "use libloading::os::unix::{Library as UnixLibrary, Symbol};",
        "",
        "// --------------------------------------------------------------- identity",
        "",
        "/// `CHS_ABI_VERSION`: what a v1 artifact's `chs_abi_version()` must answer.",
        f"pub(crate) const CHS_ABI_VERSION: i32 = {model.abi};",
        "",
        "/// `CHS_ABI_FINGERPRINT`: `sha256:` + the sha256 of the RFC 8785 canonical",
        "/// form of spec/abi-v1/abi.json (D1.1). Compared byte for byte against",
        "/// `chs_build_info()`'s `abi_fingerprint` field at loader step 4.",
        f'pub(crate) const CHS_ABI_FINGERPRINT: &str = "{model.fingerprint}";',
        "",
    ]

    out += [
        "/// `build_info`'s required top-level fields (spec/abi-v1/abi.json's own",
        "/// `build_info.schema.required`), so the loader's step 4 refuses a",
        "/// `chs_build_info()` answer missing any of them, by name.",
        "pub(crate) const BUILD_INFO_REQUIRED: &[&str] = &[",
    ]
    for field in model.raw["build_info"]["schema"]["required"]:
        out.append(f"    {_rust_str(field)},")
    out += ["];", ""]

    out += [
        "/// sdk.json's `cross_check`: loader step 5 compares each `build_info`",
        "/// field against the matching predicate field, either as an exact integer",
        "/// or as exact bytes (never through a float or a lossy string coercion).",
        "pub(crate) enum CrossCheckKind {",
        "    Int,",
        "    Bytes,",
        "}",
        "",
        "pub(crate) const CROSS_CHECK_FIELDS: &[(&str, &str, CrossCheckKind)] = &[",
    ]
    for field in model.sdk["cross_check"]:
        kind = "CrossCheckKind::Int" if field["compare"] == "int" else "CrossCheckKind::Bytes"
        out.append(f"    ({_rust_str(field['build_info'])}, {_rust_str(field['predicate'])}, {kind}),")
    out += ["];", ""]

    out += [
        "/// The `chs_status` enum's C name for each integer value this ABI",
        "/// generation defines (D3, frozen numbering). `None` for a value this",
        "/// generation never defines, which the loader treats as `sdk.json`'s",
        "/// `errors.status.unknown` class rather than a parse failure.",
        "pub(crate) fn status_name(value: i32) -> Option<&'static str> {",
        "    match value {",
    ]
    for v in model.enums[STATUS_ENUM].values:
        out.append(f"        {v.value} => Some({_rust_str(v.name)}),")
    out += ["        _ => None,", "    }", "}", ""]

    out += [
        "// ---------------------------------------------------------------- handles",
        "",
    ]
    for h in model.handles.values():
        rt = handle_rust_name(h.name)
        out += [
            f"/// Opaque `{h.name}`, mirroring the C ABI's opaque handle type. Never",
            "/// constructed in Rust; only ever a pointer this module receives from, or",
            "/// hands to, the loaded library.",
            "#[repr(C)]",
            f"pub(crate) struct {rt} {{",
            "    _opaque: [u8; 0],",
            "}",
            "",
        ]

    out += [
        "/// One utility type alias `decls.rs` needs that the ABI description does",
        "/// not describe: `gnu_get_libc_version`, resolved from the process image",
        "/// itself (`UnixLibrary::this()`), never from a loaded artifact. Declared",
        "/// here, not in `loader.rs`, so every `extern \"C\"` declaration in `abi1/`",
        "/// stays inside a file `check-no-hand-decls.py` exempts.",
        "pub(crate) type FnGlibcVersion = unsafe extern \"C\" fn() -> *const c_char;",
        "",
        "/// Resolve `gnu_get_libc_version` from the PROCESS image itself (`lib`",
        "/// is `UnixLibrary::this()`, never a loaded artifact) — loader step 1.",
        "/// `None` when the symbol cannot be resolved (musl, or no such libc",
        "/// symbol at all). The PM's ruling (round 2): every raw symbol lookup,",
        "/// the glibc probe included, lives in generated, banner-exempt code;",
        "/// hand-written code calls this helper by name, never `.get()` itself.",
        "///",
        "/// # Safety",
        "/// `lib` must be a `Library` that is never `dlclose`d (D2); here that is",
        "/// always true, since it wraps the running process, not a loaded image.",
        "pub(crate) unsafe fn resolve_glibc_version(lib: &UnixLibrary) -> Option<Symbol<FnGlibcVersion>> {",
        "    // SAFETY: the caller's `# Safety` clause.",
        "    unsafe { lib.get(b\"gnu_get_libc_version\\0\").ok() }",
        "}",
        "",
        "// ------------------------------------------------------- function pointers",
        "",
    ]
    for fn in model.functions:
        out.append(render_fn_type(fn))
    out.append("")

    handshake_names = [f.name for f in model.functions if f.name in ("chs_abi_version", "chs_build_info")]
    out += [
        "// -------------------------------------------------------------- handshake",
        "",
        "/// The two symbols loader steps 3 and 4 need before the full table is safe",
        "/// to resolve (D1.2: \"nothing before the checks\"). `Api` is constructible",
        "/// only through `Api::resolve_all`, after those checks and the step-5",
        "/// cross-check pass, so a hand-written caller cannot reach a non-handshake",
        "/// symbol early by construction.",
        "pub(crate) struct Handshake {",
    ]
    for name in handshake_names:
        out.append(f"    pub(crate) {name}: Symbol<{fn_type_name(name)}>,")
    out += [
        "}",
        "",
        "impl Handshake {",
        "    /// # Safety",
        "    /// `lib` must be a library this process just `dlopen`'d and will never",
        "    /// `dlclose` (D2's no-`dlclose`-ever rule): the returned symbols outlive",
        "    /// this call only because the image itself is never unloaded.",
        "    pub(crate) unsafe fn resolve(lib: &UnixLibrary) -> Result<Handshake, &'static str> {",
        "        // SAFETY: the caller's `# Safety` clause is exactly what `Symbol`'s own",
        "        // safety contract needs: the library stays loaded for the process's life.",
        "        unsafe {",
        "            Ok(Handshake {",
    ]
    for name in handshake_names:
        out.append(
            f'                {name}: lib.get(concat!({_rust_str(name)}, "\\0").as_bytes())'
            f".map_err(|_| {_rust_str(name)})?,"
        )
    out += ["            })", "        }", "    }", "}", ""]

    out += [
        "// ------------------------------------------------------------------- api",
        "",
        "/// Every described symbol, resolved once (loader step 6: `api`, `tooling`",
        "/// and the tombstone, all for presence). Built only by `Api::resolve_all`.",
        "pub(crate) struct Api {",
        "    /// Held so the mapping outlives every symbol; never dropped (D2: no",
        "    /// `dlclose`, ever — see `rust/src/ffi.rs`'s identical v0 rule).",
        "    _lib: std::mem::ManuallyDrop<UnixLibrary>,",
    ]
    for fn in model.functions:
        out.append(f"    pub(crate) {fn.name}: Symbol<{fn_type_name(fn.name)}>,")
    out += ["}", ""]

    out += [
        "impl Api {",
        "    /// Resolve every described symbol by name, naming the FIRST one that is",
        "    /// missing (`sdk.json`'s `missing_symbol:<name>` loader refusal, step 6).",
        "    ///",
        "    /// # Safety",
        "    /// `lib` must be a library this process just `dlopen`'d, past steps 1-5,",
        "    /// and will never `dlclose` (D2).",
        "    pub(crate) unsafe fn resolve_all(lib: UnixLibrary) -> Result<Api, &'static str> {",
        "        // SAFETY: the caller's `# Safety` clause is `Symbol`'s own contract.",
        "        unsafe {",
        "            Ok(Api {",
    ]
    for fn in model.functions:
        out.append(
            f'                {fn.name}: lib.get(concat!({_rust_str(fn.name)}, "\\0").as_bytes())'
            f".map_err(|_| {_rust_str(fn.name)})?,"
        )
    out += ["                _lib: std::mem::ManuallyDrop::new(lib),", "            })", "        }", "    }", "}", ""]

    return "\n".join(out)


# ----------------------------------------------------------------- invoke.rs


def _minted_variant(handle_type: str) -> str:
    return handle_rust_name(handle_type)[len("Chs") :] if handle_rust_name(handle_type).startswith("Chs") else handle_rust_name(handle_type)


def render_invoke(model) -> str:
    kinds = stub.classify(model)
    generic = [fn for fn in model.functions if kinds.get(fn.name) == "generic"]
    non_error_handles = [h for h in model.handles if h != ERROR_HANDLE]

    out: list[str] = [
        f"// {banner(model)}",
        "//",
        "// The test-only invoke-by-name dispatcher: `invoke(api, name, args)` marshals",
        "// an already-resolved argument list (`ResolvedArg`, rust/src/abi1/conformance.rs)",
        "// into a real call through `Api`'s symbol table and reports back a generic",
        "// `Outcome`. One match arm per handshake/tombstone call and per function",
        "// scripts/abi-v1/emit/stub.py's `classify()` calls \"generic\" (the",
        "// echo/status-injection shape every case in tests/fixtures/abi-v1/cases.json",
        "// drives); a \"free\"-classified function is never dispatched by name here",
        "// because no case ever calls one directly (see this module's own docstring,",
        "// scripts/abi-v1/emit/rust.py).",
        "#![allow(dead_code)]",
        "",
        "use std::collections::BTreeMap;",
        "use std::ffi::{c_void, CStr};",
        "",
        "use super::decls::{",
        "    self, Api,",
    ]
    out.append("    " + ", ".join(sorted({handle_rust_name(h) for h in model.handles})) + ",")
    out += [
        "};",
        "",
        "/// One already-resolved argument `invoke` consumes positionally. Built from",
        "/// a case's JSON expression tree by rust/src/abi1/conformance.rs, which also",
        "/// recursively mints any nested `{\"handle\": {...}}` argument first.",
        "#[derive(Clone)]",
        "pub(crate) enum ResolvedArg {",
        "    Int(i64),",
        "    Bytes(Vec<u8>),",
        "    Handle { kind: &'static str, ptr: *mut c_void },",
        "    Null,",
        "}",
        "",
        "/// A handle this call minted through one of its `out_handle` parameters,",
        "/// kept generic over the handle kind so the conformance runner can free it,",
        "/// read it (a `chs_buf`'s bytes), or feed it into a later call as a",
        "/// `ResolvedArg::Handle` without knowing which kind it minted ahead of time.",
        "#[derive(Clone, Copy)]",
        "pub(crate) enum MintedHandle {",
    ]
    for h in model.handles.values():
        if h.name == ERROR_HANDLE:
            continue
        out.append(f"    {_minted_variant(h.name)}(*mut decls::{handle_rust_name(h.name)}),")
    out += ["}", ""]

    out += [
        "impl MintedHandle {",
        "    /// The `abi.json` handle name this value was minted as (`\"chs_schema\"`",
        "    /// and so on) — what `ResolvedArg::Handle`'s own kind check compares",
        "    /// against when this handle is fed into a later call.",
        "    pub(crate) fn kind_name(&self) -> &'static str {",
        "        match self {",
    ]
    for h in model.handles.values():
        if h.name == ERROR_HANDLE:
            continue
        out.append(f'            MintedHandle::{_minted_variant(h.name)}(_) => {_rust_str(h.name)},')
    out += ["        }", "    }", ""]
    out += [
        "    /// Fed back into a later call as a `handle` argument: the raw pointer,",
        "    /// type-erased, tagged with this value's own kind name.",
        "    pub(crate) fn as_resolved_arg(&self) -> ResolvedArg {",
        "        ResolvedArg::Handle { kind: self.kind_name(), ptr: self.raw_ptr() }",
        "    }",
        "",
        "    /// The minted pointer, type-erased — never dereferenced here.",
        "    pub(crate) fn raw_ptr(&self) -> *mut c_void {",
        "        match *self {",
    ]
    for h in model.handles.values():
        if h.name == ERROR_HANDLE:
            continue
        out.append(f"            MintedHandle::{_minted_variant(h.name)}(p) => p as *mut c_void,")
    out += ["        }", "    }", "}", ""]

    out += [
        "/// One case error's four fields (D3), read from the error accessors once a",
        "/// call's status is not `CHS_OK`.",
        "pub(crate) struct CaseError {",
        "    pub(crate) ch_code: i32,",
        "    pub(crate) ch_name: String,",
        "    pub(crate) message: String,",
        "    pub(crate) column: String,",
        "}",
        "",
        "/// What invoking one function produced, generic across every call shape",
        "/// `invoke` dispatches.",
        "pub(crate) enum Outcome {",
        "    /// A handshake call's int32 (`chs_abi_version`) or the tombstone",
        "    /// (`chs_abi_revision`).",
        "    Int(i64),",
        "    /// A handshake call's borrowed, NUL-terminated string",
        "    /// (`chs_build_info`, `chs_clickhouse_version`).",
        "    Str(String),",
        "    /// A status-returning call: the `chs_status` name, the error (once set,",
        "    /// read and freed) when the status was not `CHS_OK`, and one entry per",
        "    /// `out_handle` parameter this function declares.",
        "    Call {",
        "        status: &'static str,",
        "        error: Option<CaseError>,",
        "        outputs: BTreeMap<String, MintedHandle>,",
        "    },",
        "}",
        "",
    ]

    out += [
        "/// A cursor over one call's already-resolved, non-out arguments, consumed",
        "/// positionally exactly as `abi.json` declares them (model.py's own",
        "/// invariant: every input precedes every output).",
        "struct ArgCursor<'a> {",
        "    args: &'a [ResolvedArg],",
        "    pos: usize,",
        "}",
        "",
        "impl<'a> ArgCursor<'a> {",
        "    fn new(args: &'a [ResolvedArg]) -> Self {",
        "        ArgCursor { args, pos: 0 }",
        "    }",
        "",
        "    fn next(&mut self) -> Result<&'a ResolvedArg, String> {",
        "        let a = self.args.get(self.pos).ok_or_else(|| format!(\"expected an argument at position {}, found none\", self.pos))?;",
        "        self.pos += 1;",
        "        Ok(a)",
        "    }",
        "",
        "    /// A scalar or enum argument, as the integer a case expresses it with.",
        "    fn int(&mut self) -> Result<i64, String> {",
        "        match self.next()? {",
        "            ResolvedArg::Int(v) => Ok(*v),",
        "            other => Err(format!(\"expected an int argument, found {}\", describe(other))),",
        "        }",
        "    }",
        "",
        "    /// A `bytes_in` argument's bytes.",
        "    fn bytes(&mut self) -> Result<&'a [u8], String> {",
        "        match self.next()? {",
        "            ResolvedArg::Bytes(b) => Ok(b.as_slice()),",
        "            other => Err(format!(\"expected a bytes argument, found {}\", describe(other))),",
        "        }",
        "    }",
        "",
        "    /// A required `handle` argument of the named kind.",
        "    fn handle(&mut self, kind: &str) -> Result<*mut c_void, String> {",
        "        match self.next()? {",
        "            ResolvedArg::Handle { kind: got, ptr } if *got == kind => Ok(*ptr),",
        "            ResolvedArg::Handle { kind: got, .. } => {",
        "                Err(format!(\"expected a {kind} handle, found a {got} handle\"))",
        "            }",
        "            other => Err(format!(\"expected a {kind} handle, found {}\", describe(other))),",
        "        }",
        "    }",
        "",
        "    /// A nullable `handle` argument: `None` for `null_handle`, else as",
        "    /// `handle` above.",
        "    fn handle_opt(&mut self, kind: &str) -> Result<Option<*mut c_void>, String> {",
        "        match self.next()? {",
        "            ResolvedArg::Null => Ok(None),",
        "            ResolvedArg::Handle { kind: got, ptr } if *got == kind => Ok(Some(*ptr)),",
        "            ResolvedArg::Handle { kind: got, .. } => {",
        "                Err(format!(\"expected a {kind} handle or null, found a {got} handle\"))",
        "            }",
        "            other => Err(format!(\"expected a {kind} handle or null, found {}\", describe(other))),",
        "        }",
        "    }",
        "}",
        "",
        "fn describe(a: &ResolvedArg) -> &'static str {",
        "    match a {",
        "        ResolvedArg::Int(_) => \"an int\",",
        "        ResolvedArg::Bytes(_) => \"bytes\",",
        "        ResolvedArg::Handle { .. } => \"a handle\",",
        "        ResolvedArg::Null => \"null\",",
        "    }",
        "}",
        "",
        "/// A borrowed, NUL-terminated C string, copied. `p` NULL reads as empty —",
        "/// every handshake call this generation describes is documented never to",
        "/// return NULL.",
        "///",
        "/// # Safety",
        "/// `p` must be null or a valid NUL-terminated `const char *` whose referent",
        "/// outlives this call (every handshake string here is a compiled-in",
        "/// constant, never freed).",
        "unsafe fn cstr_to_string(p: *const std::os::raw::c_char) -> String {",
        "    if p.is_null() {",
        "        return String::new();",
        "    }",
        "    // SAFETY: the caller's `# Safety` clause.",
        "    unsafe { CStr::from_ptr(p).to_string_lossy().into_owned() }",
        "}",
        "",
        "/// Read and free one owned `chs_buf *` text accessor's result (`chs_error_ch_name`,",
        "/// `chs_error_message`, `chs_error_column`: each returns a fresh `chs_buf`, D4,",
        "/// NULL only on allocation failure). Empty string on NULL, never a panic.",
        "///",
        "/// # Safety",
        "/// `buf` must be null or a `chs_buf *` this same `api` minted.",
        "unsafe fn take_buf_text(api: &Api, buf: *mut decls::ChsBuf) -> String {",
        "    if buf.is_null() {",
        "        return String::new();",
        "    }",
        "    // SAFETY: the caller's `# Safety` clause; `chs_buf_data`/`chs_buf_len` read a",
        "    // live buffer this call just received ownership of, and `chs_buf_free` is",
        "    // this same library's own, called exactly once, after the copy.",
        "    unsafe {",
        "        let p = (api.chs_buf_data)(buf as *const decls::ChsBuf);",
        "        let n = (api.chs_buf_len)(buf as *const decls::ChsBuf);",
        "        let text = if p.is_null() || n == 0 {",
        "            String::new()",
        "        } else {",
        "            String::from_utf8_lossy(std::slice::from_raw_parts(p, n)).into_owned()",
        "        };",
        "        (api.chs_buf_free)(buf);",
        "        text",
        "    }",
        "}",
        "",
        "/// Build a status call's `Outcome`: the status name, and — once `err` is",
        "/// read and freed — its four fields when the status was not `CHS_OK`.",
        "///",
        "/// # Safety",
        "/// `err` must be null or a `chs_error *` this same `api` minted.",
        "unsafe fn finish_status(",
        "    api: &Api,",
        "    status: i32,",
        "    err: *mut decls::ChsError,",
        "    outputs: BTreeMap<String, MintedHandle>,",
        ") -> Result<Outcome, String> {",
        "    let name = decls::status_name(status)",
        "        .ok_or_else(|| format!(\"a status of {status}, which this generation does not define\"))?;",
        "    let error = if err.is_null() {",
        "        None",
        "    } else {",
        "        // SAFETY: the caller's `# Safety` clause; every accessor reads a live",
        "        // `chs_error *` this call just received ownership of, and `chs_error_free`",
        "        // is this same library's own, called exactly once, after every read.",
        "        unsafe {",
        "            let ch_code = (api.chs_error_ch_code)(err as *const decls::ChsError);",
        "            let ch_name_buf = (api.chs_error_ch_name)(err as *const decls::ChsError);",
        "            let ch_name = take_buf_text(api, ch_name_buf);",
        "            let message_buf = (api.chs_error_message)(err as *const decls::ChsError);",
        "            let message = take_buf_text(api, message_buf);",
        "            let column_buf = (api.chs_error_column)(err as *const decls::ChsError);",
        "            let column = take_buf_text(api, column_buf);",
        "            (api.chs_error_free)(err);",
        "            Some(CaseError { ch_code, ch_name, message, column })",
        "        }",
        "    };",
        "    Ok(Outcome::Call { status: name, error, outputs })",
        "}",
        "",
    ]

    out += [
        "/// Dispatch one call by name with already-resolved raw arguments: a pure",
        "/// marshal/unmarshal step that never recurses and never frees an `outputs`",
        "/// entry (the caller, rust/src/abi1/conformance.rs, owns that).",
        "///",
        "/// # Safety",
        "/// Every `ResolvedArg::Handle` must carry a live pointer of the kind it",
        "/// claims, minted by this same `api` (or, for a `chs_buf`, read and never",
        "/// otherwise used); `api` must have completed loader step 6.",
        "pub(crate) unsafe fn invoke(api: &Api, name: &str, args: &[ResolvedArg]) -> Result<Outcome, String> {",
        "    match name {",
    ]

    def arm(name: str, body: list[str]) -> list[str]:
        inner = "\n            ".join(body)
        return [f'        {_rust_str(name)} => {{', f"            {inner}", "        }"]

    out += arm(
        "chs_abi_version",
        ["// SAFETY: a handshake call takes no arguments and borrows nothing.",
         "Ok(Outcome::Int(unsafe { (api.chs_abi_version)() } as i64))"],
    )
    out += arm(
        "chs_abi_revision",
        ["// SAFETY: the tombstone takes no arguments and borrows nothing.",
         "Ok(Outcome::Int(unsafe { (api.chs_abi_revision)() } as i64))"],
    )
    out += arm(
        "chs_build_info",
        [
            "// SAFETY: chs_build_info returns a static, NUL-terminated string (D1.2/D4),",
            "// never freed, valid for the process's life.",
            "let p = unsafe { (api.chs_build_info)() };",
            "// SAFETY: see the comment above; cstr_to_string's own contract is the same one.",
            "Ok(Outcome::Str(unsafe { cstr_to_string(p) }))",
        ],
    )
    out += arm(
        "chs_clickhouse_version",
        [
            "// SAFETY: same as chs_build_info: a static, NUL-terminated string.",
            "let p = unsafe { (api.chs_clickhouse_version)() };",
            "// SAFETY: see the comment above; cstr_to_string's own contract is the same one.",
            "Ok(Outcome::Str(unsafe { cstr_to_string(p) }))",
        ],
    )

    for fn in generic:
        has_inputs = any(not p.is_out for p in fn.params)
        body: list[str] = ["let mut cur = ArgCursor::new(args);"] if has_inputs else []
        call_args: list[str] = []
        for p in fn.params:
            if p.is_out:
                continue
            if p.kind == "scalar":
                var = f"a_{p.name}"
                body.append(f"let {var} = cur.int()? as {SCALAR_RUST[p.type]};")
                call_args.append(var)
            elif p.kind == "enum":
                var = f"a_{p.name}"
                body.append(f"let {var} = cur.int()? as i32;")
                call_args.append(var)
            elif p.kind == "bytes_in":
                var = f"a_{p.name}"
                body.append(f"let {var} = cur.bytes()?;")
                call_args.append(f"{var}.as_ptr()")
                call_args.append(f"{var}.len()")
            elif p.kind == "handle":
                var = f"a_{p.name}"
                rt = handle_rust_name(p.type)
                ptr_kw = "*const" if p.const else "*mut"
                if p.nullable:
                    body.append(f"let {var} = cur.handle_opt({_rust_str(p.type)})?;")
                    body.append(
                        f"let {var} = {var}.map_or(std::ptr::null_mut(), |p| p as *mut {rt}) as {ptr_kw} {rt};"
                    )
                else:
                    body.append(f"let {var} = cur.handle({_rust_str(p.type)})? as {ptr_kw} {rt};")
                call_args.append(var)
            else:
                raise ValueError(f"{fn.name}: unsupported input param kind {p.kind!r} for invoke_gen")
        out_locals: list[tuple[str, str, str]] = []
        for p in fn.params:
            if p.kind != "out_handle":
                continue
            rt = handle_rust_name(p.type)
            var = f"out_{p.name}"
            body.append(f"let mut {var}: *mut {rt} = std::ptr::null_mut();")
            call_args.append(f"&mut {var}")
            out_locals.append((p.name, p.type, var))
        has_err = any(p.kind == "out_error" for p in fn.params)
        if has_err:
            body.append(f"let mut err: *mut {handle_rust_name(ERROR_HANDLE)} = std::ptr::null_mut();")
            call_args.append("&mut err")
        body.append(
            f"// SAFETY: every input is marshaled from the case's own recipe above, matching "
            f"{fn.name}'s declared shape; every output pointer is this call's own local."
        )
        body.append(f"let status = unsafe {{ (api.{fn.name})({', '.join(call_args)}) }};")
        outputs_mut = "mut " if out_locals else ""
        body.append(f"let {outputs_mut}outputs: BTreeMap<String, MintedHandle> = BTreeMap::new();")
        for pname, ptype, var in out_locals:
            variant = _minted_variant(ptype)
            body.append(f'outputs.insert({_rust_str(pname)}.to_string(), MintedHandle::{variant}({var}));')
        if has_err:
            body.append(
                "// SAFETY: err is null or a chs_error* this same call just received from api above."
            )
            body.append("unsafe { finish_status(api, status, err, outputs) }")
        else:
            # No FIRM/provisional "generic" function omits the error out-param
            # (model.py requires one on every status-returning call); kept as
            # an explicit refusal, never a silent wrong answer, if that ever
            # changes without this emitter changing too.
            body.append(
                f'Err("{fn.name}: a status-returning call with no out_error parameter, '
                f'which invoke_gen.rs does not know how to report".to_string())'
            )
        out += arm(fn.name, body)

    out += [
        '        other => Err(format!("invoke: {other} is not a function this generation describes, or '
        'it is a \\"free\\"-classified function no case ever calls by name")),',
        "    }",
        "}",
        "",
    ]

    return "\n".join(out)


# ------------------------------------------------------------- shared naming


RUST_KEYWORDS = frozenset(
    """as break const continue crate else enum extern false fn for if impl in let loop match mod move mut pub ref
    return self Self static struct super trait true type unsafe use where while async await dyn abstract become box
    do final macro override priv typeof unsized virtual yield try gen""".split()
)


def _ident(name: str) -> str:
    """A parameter name as a Rust identifier: a keyword gains a trailing underscore."""
    return name + "_" if name in RUST_KEYWORDS else name


def _words(text: str) -> str:
    """`accepted_poisoned` / `CHS_JSON_EACH_ROW` words to `AcceptedPoisoned` / `ChsJsonEachRow`."""
    return "".join(part[:1].upper() + part[1:].lower() for part in text.split("_") if part)


def _upper_snake(text: str) -> str:
    return text.upper()


def _doc(text: str, indent: str = "") -> list[str]:
    return [f"{indent}/// {line}" if line else f"{indent}///" for line in text.splitlines()]


def _handle_wrapper_name(handle: str) -> str:
    """`chs_schema` -> `SchemaHandle`."""
    assert handle.startswith("chs_"), handle
    return _words(handle[len("chs_") :]) + "Handle"


# --------------------------------------------------------------- calls_gen.rs

CALLS_PATH = "rust/src/abi1/calls_gen.rs"
VOCAB_PATH = "rust/src/abi1/vocab_gen.rs"
ERRMAP_PATH = "rust/src/abi1/errmap_gen.rs"

# The handles that get a Rust wrapper object with a Drop: every handle but the
# two the generated read-out consumes itself (a buffer is copied then freed
# inside the call that produced it, an error likewise).
_PLAIN_HANDLES_EXCLUDED = (BUF_HANDLE, ERROR_HANDLE)


def _wrapped_functions(model):
    """The status-returning `api` functions that get a typed method. A call
    taking a buffer or an error handle is the read-out's own plumbing and
    never a public call."""
    out = []
    for fn in model.functions:
        if fn.cls != "api" or fn.returns.kind != "status":
            continue
        if any(p.kind == "handle" and p.type in _PLAIN_HANDLES_EXCLUDED for p in fn.params):
            continue
        out.append(fn)
    return out


def _method_name(fn, model) -> str:
    assert fn.name.startswith(model.prefix), fn.name
    return fn.name[len(model.prefix) :]


def render_calls(model) -> str:
    out: list[str] = [
        f"// {banner(model)}",
        "//",
        "// The typed, memory-safe call layer over `decls.rs`: one method per",
        "// status-returning `api` call, named without the `chs_` prefix, that copies",
        "// every `chs_buf` into a `Vec<u8>` and frees it in the same call, reads and",
        "// frees every `chs_error` into a `RawCallError`, and wraps every minted",
        "// handle in an object with a `Drop` that frees it. No `chs_buf` or",
        "// `chs_error` ever leaves this file. See scripts/abi-v1/emit/rust.py.",
        "#![allow(dead_code)]",
        "",
        "use std::sync::Arc;",
        "",
        "use super::decls::{",
        "    Api,",
    ]
    out.append("    " + ", ".join(sorted({handle_rust_name(h) for h in model.handles})) + ",")
    out += [
        "};",
        "",
        "/// One call's error, read out of a `chs_error` verbatim and freed: the five",
        "/// fields every call error carries, and nothing synthesized. `ch_name` is",
        "/// ASCII by the description; `message` and `column` are bytes.",
        "#[derive(Debug, Clone, PartialEq, Eq)]",
        "pub(crate) struct RawCallError {",
        "    pub(crate) status: i32,",
        "    pub(crate) ch_code: i32,",
        "    pub(crate) ch_name: String,",
        "    pub(crate) message: Vec<u8>,",
        "    pub(crate) column: Vec<u8>,",
        "}",
        "",
        "/// Copy one owned `chs_buf *` into a `Vec<u8>` and free it. NULL reads as",
        "/// empty (`chs_buf` accessors answer NULL only on allocation failure).",
        "///",
        "/// # Safety",
        "/// `buf` must be null or a `chs_buf *` this same `api` handed over, not",
        "/// yet freed.",
        "unsafe fn copy_free_buf(api: &Api, buf: *mut ChsBuf) -> Vec<u8> {",
        "    if buf.is_null() {",
        "        return Vec::new();",
        "    }",
        "    // SAFETY: the caller's `# Safety` clause; the data pointer and length are",
        "    // read before the single free, and the copy is made before it.",
        "    unsafe {",
        "        let data = (api.chs_buf_data)(buf as *const ChsBuf);",
        "        let len = (api.chs_buf_len)(buf as *const ChsBuf);",
        "        let bytes = if data.is_null() || len == 0 {",
        "            Vec::new()",
        "        } else {",
        "            std::slice::from_raw_parts(data, len).to_vec()",
        "        };",
        "        (api.chs_buf_free)(buf);",
        "        bytes",
        "    }",
        "}",
        "",
        "/// Read every field of one `chs_error *` and free it. A null `err` with a",
        "/// non-OK status (never expected) still yields an error carrying the status.",
        "///",
        "/// # Safety",
        "/// `err` must be null or a `chs_error *` this same `api` handed over, not",
        "/// yet freed.",
        "unsafe fn read_free_error(api: &Api, status: i32, err: *mut ChsError) -> RawCallError {",
        "    if err.is_null() {",
        "        return RawCallError {",
        "            status,",
        "            ch_code: 0,",
        "            ch_name: String::new(),",
        "            message: b\"the library returned a non-OK status with no error object\".to_vec(),",
        "            column: Vec::new(),",
        "        };",
        "    }",
        "    // SAFETY: the caller's `# Safety` clause; every accessor reads the live",
        "    // error and the one free comes after every read.",
        "    unsafe {",
        "        let ch_code = (api.chs_error_ch_code)(err as *const ChsError);",
        "        let ch_name = String::from_utf8_lossy(&copy_free_buf(api, (api.chs_error_ch_name)(err as *const ChsError)))",
        "            .into_owned();",
        "        let message = copy_free_buf(api, (api.chs_error_message)(err as *const ChsError));",
        "        let column = copy_free_buf(api, (api.chs_error_column)(err as *const ChsError));",
        "        (api.chs_error_free)(err);",
        "        RawCallError { status, ch_code, ch_name, message, column }",
        "    }",
        "}",
        "",
        "/// What a call that must have minted an output reports when the library",
        "/// answered OK and left the output NULL (never expected under a matching",
        "/// fingerprint; still reported, never dereferenced).",
        "fn missing_output(status: i32, what: &str) -> RawCallError {",
        "    RawCallError {",
        "        status,",
        "        ch_code: 0,",
        "        ch_name: String::new(),",
        "        message: format!(\"the library answered OK and returned no {what}\").into_bytes(),",
        "        column: Vec::new(),",
        "    }",
        "}",
        "",
    ]

    for h in model.handles.values():
        if h.name in _PLAIN_HANDLES_EXCLUDED:
            continue
        wrapper = _handle_wrapper_name(h.name)
        rt = handle_rust_name(h.name)
        out += [
            f"/// One live `{h.name}`, freed by `{h.free}` when dropped. It keeps the",
            "/// library's symbol table alive through its own `Arc`, so a handle can",
            "/// never outlive the table that frees it.",
            f"pub(crate) struct {wrapper} {{",
            f"    ptr: *mut {rt},",
            "    api: Arc<Api>,",
            "}",
            "",
            "// SAFETY: the library makes every call on a compiled handle safe to run",
            "// concurrently with any other call on it, except its own free; `Drop` runs",
            "// only when no borrow of this wrapper remains, so it cannot overlap a call.",
            f"unsafe impl Send for {wrapper} {{}}",
            "// SAFETY: as above.",
            f"unsafe impl Sync for {wrapper} {{}}",
            "",
            f"impl {wrapper} {{",
            "    /// The raw pointer, for passing to a call that reads this handle.",
            f"    pub(crate) fn as_ptr(&self) -> *const {rt} {{",
            "        self.ptr",
            "    }",
            "}",
            "",
            f"impl Drop for {wrapper} {{",
            "    fn drop(&mut self) {",
            "        // SAFETY: `ptr` came from this table's own constructor call and is freed",
            f"        // exactly once, here; `{h.free}` accepts it (it is non-null).",
            f"        unsafe {{ (self.api.{h.free})(self.ptr) }}",
            "    }",
            "}",
            "",
        ]

    out += ["impl Api {"]
    for fn in _wrapped_functions(model):
        out += _render_method(model, fn)
    out += ["}", ""]
    return "\n".join(out)


def _render_method(model, fn) -> list[str]:
    args: list[str] = []
    call: list[str] = []
    outs: list[tuple[str, str, bool]] = []  # (var, kind, nullable)
    prelude: list[str] = []
    for p in fn.params:
        n = _ident(p.name)
        if p.kind == "bytes_in":
            args.append(f"{n}: &[u8]")
            call += [f"{n}.as_ptr()", f"{n}.len()"]
        elif p.kind == "enum":
            args.append(f"{n}: i32")
            call.append(n)
        elif p.kind == "scalar":
            args.append(f"{n}: {SCALAR_RUST[p.type]}")
            call.append(n)
        elif p.kind == "handle":
            w = _handle_wrapper_name(p.type)
            if p.nullable:
                args.append(f"{n}: Option<&{w}>")
                call.append(f"{n}.map_or(std::ptr::null(), |h| h.as_ptr())")
            else:
                args.append(f"{n}: &{w}")
                call.append(f"{n}.as_ptr()")
        elif p.kind == "out_handle":
            rt = handle_rust_name(p.type)
            var = f"out_{p.name}"
            prelude.append(f"let mut {var}: *mut {rt} = std::ptr::null_mut();")
            call.append(f"&mut {var}")
            outs.append((p.name, p.type, p.nullable))
        elif p.kind == "out_error":
            prelude.append(f"let mut err: *mut {handle_rust_name(ERROR_HANDLE)} = std::ptr::null_mut();")
            call.append("&mut err")
        else:
            raise ValueError(f"{fn.name}: no typed method for a {p.kind!r} parameter")
    ret_types: list[str] = []
    ret_exprs: list[str] = []
    ok: list[str] = []
    for name, htype, nullable in outs:
        var = f"out_{name}"
        if htype == BUF_HANDLE:
            if nullable:
                ret_types.append("Option<Vec<u8>>")
                ret_exprs.append(f"copied_{name}")
                ok.append(
                    f"let copied_{name} = if {var}.is_null() {{\n"
                    f"            None\n"
                    f"        }} else {{\n"
                    f"            // SAFETY: `{var}` is a buffer this call just received, freed once here.\n"
                    f"            Some(unsafe {{ copy_free_buf(self, {var}) }})\n"
                    f"        }};"
                )
            else:
                ret_types.append("Vec<u8>")
                ret_exprs.append(f"copied_{name}")
                ok.append(
                    f"// SAFETY: `{var}` is a buffer this call just received (or null), freed once here.\n"
                    f"        let copied_{name} = unsafe {{ copy_free_buf(self, {var}) }};"
                )
        else:
            w = _handle_wrapper_name(htype)
            ret_types.append(w)
            ok.append(
                f"if {var}.is_null() {{\n            return Err(missing_output(status, {_rust_str(name)}));\n        }}"
            )
            ret_exprs.append(f"{w} {{ ptr: {var}, api: Arc::clone(self) }}")
    if len(ret_types) == 0:
        rty, rexpr = "()", "()"
    elif len(ret_types) == 1:
        rty, rexpr = ret_types[0], ret_exprs[0]
    else:
        rty, rexpr = "(" + ", ".join(ret_types) + ")", "(" + ", ".join(ret_exprs) + ")"
    recv = "self: &Arc<Api>"
    sig_args = ", ".join([recv] + args)
    lines = [f"    /// `{fn.name}`, thread class `{fn.thread}`: typed, copy-then-free."]
    lines += [
        "    #[allow(clippy::too_many_arguments)]",
        f"    pub(crate) fn {_method_name(fn, model)}({sig_args}) -> Result<{rty}, RawCallError> {{",
    ]
    for line in prelude:
        lines.append(f"        {line}")
    lines += [
        "        // SAFETY: every input is a live borrow for the whole call, every output",
        "        // pointer is this call's own local, and the table is fully resolved.",
        f"        let status = unsafe {{ (self.{fn.name})({', '.join(call)}) }};",
        "        if status != 0 {",
        "            // SAFETY: `err` is null or a `chs_error *` this call just received.",
        "            return Err(unsafe { read_free_error(self, status, err) });",
        "        }",
    ]
    for block in ok:
        lines.append(f"        {block}")
    lines.append(f"        Ok({rexpr})")
    lines += ["    }", ""]
    return lines


# --------------------------------------------------------------- vocab_gen.rs

# The spellings of the one vocabulary whose wire values are single letters.
# This is generator input, not binding state: the emitter refuses a
# description that adds or drops a verdict without this table following.
VERDICT_NAMES = {"t": "True", "f": "False", "e": "Error", "d": "Decline"}
VERDICT_DOCS = {
    "t": "The predicate is non-NULL and non-zero for this row.",
    "f": "False or NULL: SQL's three-valued logic collapsed at the WHERE boundary.",
    "e": "The predicate threw on this row's values. Not an answer; fail closed.",
    "d": "This library declines to answer for this row. Not an answer; fail closed.",
}


def _string_enum(model, name: str, type_name: str, *, variant, doc: str, with_default: bool, extra_fields=()) -> list[str]:
    enum = model.enums[name]
    out: list[str] = []
    out += _doc(doc)
    derive = "#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash"
    derive += ", Default)]" if with_default else ")]"
    out.append(derive)
    out.append("#[non_exhaustive]")
    out.append(f"pub enum {type_name} {{")
    for v in enum.values:
        vn = variant(v.value)
        if with_default and v.value == enum.fallback:
            out.append("    #[default]")
        out.append(f"    /// The wire value `{v.value}`.")
        out.append(f"    {vn},")
    out += ["}", "", f"impl {type_name} {{"]
    out += [
        "    /// The wire spelling.",
        "    pub fn as_str(self) -> &'static str {",
        "        match self {",
    ]
    for v in enum.values:
        out.append(f"            {type_name}::{variant(v.value)} => {_rust_str_any(v.value)},")
    out += ["        }", "    }", ""]
    fb = variant(enum.fallback) if enum.fallback is not None else None
    out += [
        "    /// Read one wire value; an unknown value reads as the description's fallback"
        if fb
        else "    /// Read one wire value; `None` for a value the description does not list.",
        f"    pub(crate) fn from_wire(s: &str) -> {'Self' if fb else 'Option<Self>'} {{",
        "        match s {",
    ]
    for v in enum.values:
        arm = f"{type_name}::{variant(v.value)}"
        out.append(f"            {_rust_str_any(v.value)} => {arm if fb else 'Some(' + arm + ')'},")
    out.append(f"            _ => {('Self::' + fb) if fb else 'None'},")
    out += ["        }", "    }"]
    for field, ftype in extra_fields:
        assert ftype == "boolean", ftype
        out += [
            "",
            f"    /// The description's `{field}` fact for this value.",
            f"    pub fn {field}(self) -> bool {{",
            "        match self {",
        ]
        for v in enum.values:
            out.append(f"            {type_name}::{variant(v.value)} => {'true' if v.fields[field] else 'false'},")
        out += ["        }", "    }"]
    out += ["}", ""]
    out += [
        f"impl std::fmt::Display for {type_name} {{",
        "    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {",
        "        f.write_str(self.as_str())",
        "    }",
        "}",
        "",
    ]
    return out


def _rust_str_any(s: str) -> str:
    assert all(c.isascii() and c not in '"\\' for c in s), s
    return f'"{s}"'


def render_vocab(model) -> str:
    out: list[str] = [
        f"// {banner(model)}",
        "//",
        "// Every vocabulary of the description, as Rust: the `chs_format` and",
        "// `chs_status` numbers, the document vocabularies (outcomes, verdicts,",
        "// reasons with their `lossy` fact, sources with their `is_stored` fact,",
        "// default kinds), and the document-group flags. No binding keeps a copy",
        "// of its own. See scripts/abi-v1/emit/rust.py.",
        "",
        "// ----------------------------------------------------------------- format",
        "",
    ]
    fmt = model.enums["chs_format"]
    out += [
        "/// The input (and export) encoding of a body, as the `chs_format` integer.",
        "/// **The numbers are part of the ABI** (frozen) and are never renumbered.",
        "/// Each carries `ch_name`, ClickHouse's own name for the format, which is how",
        "/// a build's `capabilities` lists it.",
        "#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]",
        "#[repr(i32)]",
        "pub enum Format {",
    ]
    for v in fmt.values:
        out += [f"    /// `{v.fields['ch_name']}` (`{v.name}`).", f"    {_words(v.name[len('CHS_'):])} = {v.value},"]
    out += ["}", "", "impl Format {", "    /// Every format, in numeric order.", f"    pub const ALL: [Format; {len(fmt.values)}] = ["]
    for v in fmt.values:
        out.append(f"        Format::{_words(v.name[len('CHS_'):])},")
    out += [
        "    ];",
        "",
        "    /// ClickHouse's own name for this format, as `capabilities` lists it.",
        "    pub fn ch_name(self) -> &'static str {",
        "        match self {",
    ]
    for v in fmt.values:
        out.append(f"            Format::{_words(v.name[len('CHS_'):])} => {_rust_str_any(v.fields['ch_name'])},")
    out += [
        "        }",
        "    }",
        "",
        "    /// The `chs_format` value that crosses the C boundary.",
        "    pub(crate) fn code(self) -> i32 {",
        "        self as i32",
        "    }",
        "}",
        "",
    ]

    out += [
        "/// The call statuses, as the `chs_status` integers (D3, frozen). A call error",
        "/// carries the raw status; compare it with these.",
        "pub mod status {",
    ]
    for v in model.enums[STATUS_ENUM].values:
        out += [f"    /// `{v.name}`.", f"    pub const {v.name[len('CHS_'):]}: i32 = {v.value};"]
    out += ["}", ""]

    out += _string_enum(
        model,
        "row_outcome",
        "Outcome",
        variant=_words,
        doc="A row's or a batch's outcome, as the library's document states it (already final: the library applies every promotion).\nAn unknown value reads as the description's fallback, `unsupported`.",
        with_default=True,
    )
    out += _string_enum(
        model,
        "filter_outcome",
        "FilterOutcome",
        variant=_words,
        doc="A filter evaluation's outcome. An unknown value reads as the description's fallback, `unsupported`.",
        with_default=True,
    )
    verdict = model.enums["filter_verdict"]
    assert {v.value for v in verdict.values} == set(VERDICT_NAMES), "filter_verdict changed; update VERDICT_NAMES"
    out += _string_enum(
        model,
        "filter_verdict",
        "Verdict",
        variant=lambda w: VERDICT_NAMES[w],
        doc="One row's filter verdict. `answered` says which are answers; `Error` and `Decline` are never answers, and a caller enforcing visibility fails closed on both.\nAn unknown value reads as the description's fallback, `d`.",
        with_default=True,
        extra_fields=[("answered", "boolean")],
    )
    out += [
        "impl Verdict {",
        "    /// The document character: `t`, `f`, `e` or `d`.",
        "    pub fn as_char(self) -> char {",
        "        match self {",
    ]
    for v in verdict.values:
        out.append(f"            Verdict::{VERDICT_NAMES[v.value]} => '{v.value}',")
    out += ["        }", "    }", "}", ""]

    out += _string_enum(
        model,
        "default_kind",
        "DefaultKind",
        variant=lambda w: _words(w) if w else "None",
        doc="What a column's DEFAULT clause is. The empty wire value is `None`.",
        with_default=False,
    )

    reason = model.enums["transform_reason"]
    out += [
        "/// The `transform_reason` vocabulary: the reason spellings a transformation",
        "/// carries, and the `lossy` fact the description gives each.",
        "pub mod reason {",
    ]
    for v in reason.values:
        out += [f"    /// `{v.value}`.", f"    pub const {_upper_snake(v.value)}: &str = {_rust_str_any(v.value)};"]
    fb = next(v for v in reason.values if v.value == reason.fallback)
    out += [
        "",
        "    /// The description's `lossy` fact for a reason; a reason the description does",
        "    /// not list takes the fallback's fact.",
        "    pub fn is_lossy(reason: &str) -> bool {",
        "        match reason {",
    ]
    for v in reason.values:
        out.append(f"            {_rust_str_any(v.value)} => {'true' if v.fields['lossy'] else 'false'},")
    out += [f"            _ => {'true' if fb.fields['lossy'] else 'false'},", "        }", "    }", "}", ""]

    src = model.enums["value_src"]
    out += [
        "/// The `value_src` vocabulary: where a column's value came from, and whether",
        "/// the description says it is stored.",
        "pub mod source {",
    ]
    for v in src.values:
        out += [f"    /// `{v.value}`.", f"    pub const {_upper_snake(v.value)}: &str = {_rust_str_any(v.value)};"]
    out += [
        "",
        "    /// The description's `is_stored` fact for a source, or `None` for a value the",
        "    /// description does not list (the vocabulary has no fallback).",
        "    pub fn is_stored(src: &str) -> Option<bool> {",
        "        match src {",
    ]
    for v in src.values:
        out.append(f"            {_rust_str_any(v.value)} => Some({'true' if v.fields['is_stored'] else 'false'}),")
    out += ["            _ => None,", "        }", "    }", "}", ""]

    flags = [(c.name, c.value) for c in model.constants.values() if c.name.startswith("CHS_DOC_")]
    out += [
        "/// Which groups a `rows` document carries: values (`cols`), transformations",
        "/// and defaults. Combine with `|`.",
        "#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]",
        "pub struct DocFlags(u32);",
        "",
        "impl DocFlags {",
    ]
    for name, value in flags:
        out += [f"    /// `{name}`.", f"    pub const {name[len('CHS_DOC_'):]}: DocFlags = DocFlags({value});"]
    out += [
        "",
        "    /// The raw bitmask, as it crosses the C boundary.",
        "    pub fn bits(self) -> u32 {",
        "        self.0",
        "    }",
        "",
        "    /// A mask from raw bits, unvalidated on purpose: an unknown bit reaches the",
        "    /// library, which refuses it loudly.",
        "    pub fn from_bits(bits: u32) -> DocFlags {",
        "        DocFlags(bits)",
        "    }",
        "}",
        "",
        "impl std::ops::BitOr for DocFlags {",
        "    type Output = DocFlags;",
        "    fn bitor(self, rhs: DocFlags) -> DocFlags {",
        "        DocFlags(self.0 | rhs.0)",
        "    }",
        "}",
        "",
        "impl std::ops::BitOrAssign for DocFlags {",
        "    fn bitor_assign(&mut self, rhs: DocFlags) {",
        "        self.0 |= rhs.0;",
        "    }",
        "}",
        "",
    ]
    export_none = model.constants["CHS_EXPORT_NONE"].value
    out += [
        "/// `export_format` when no export is asked for.",
        f"pub(crate) const EXPORT_NONE: i32 = {export_none};",
        "",
        "/// The `byte_strings` rule: a data-derived string `F` is the member `F` when its",
        "/// bytes are valid UTF-8, and otherwise `F` is absent and `F` plus this suffix",
        "/// carries the standard base64 of the raw bytes.",
        f"pub(crate) const BYTES_SUFFIX: &str = {_rust_str_any(model.byte_strings['suffix'])};",
        "",
        "/// The member that carries the raw bytes of a scalar `String` or `FixedString`",
        "/// value, beside its rendering.",
        f"pub(crate) const VALUE_BYTES: &str = {_rust_str_any(model.byte_strings['value'])};",
        "",
    ]
    return "\n".join(out)


# -------------------------------------------------------------- errmap_gen.rs


def render_errmap(model) -> str:
    errors = model.sdk["errors"]
    status_map = errors["status"]
    classes = sorted({v for v in status_map.values() if v})
    refusals = model.sdk["loader"]["refusals"]
    by_reason: dict[str, str] = {}
    for r in refusals:
        assert by_reason.setdefault(r["reason"], r["error"]) == r["error"], r
    ref_classes = sorted(set(by_reason.values()))
    out: list[str] = [
        f"// {banner(model)}",
        "//",
        "// spec/abi-v1/sdk.json's error table: which public error class each call",
        "// status and each loader refusal maps to. See scripts/abi-v1/emit/rust.py.",
        "#![allow(dead_code)]",
        "",
        "/// The call-error class a non-OK status maps to (D3).",
        "#[derive(Debug, Clone, Copy, PartialEq, Eq)]",
        "pub(crate) enum ErrorClass {",
    ]
    for c in classes:
        out.append(f"    {_words(c)},")
    out += [
        "}",
        "",
        "/// The class for one `chs_status` value; `None` for `CHS_OK`. A value outside",
        "/// the closed set reads as `sdk.json`'s `unknown` class.",
        "pub(crate) fn status_class(status: i32) -> Option<ErrorClass> {",
        "    match status {",
    ]
    for v in model.enums[STATUS_ENUM].values:
        cls = status_map[v.name]
        out.append(f"        {v.value} => {'Some(ErrorClass::' + _words(cls) + ')' if cls else 'None'},")
    out += [f"        _ => Some(ErrorClass::{_words(status_map['unknown'])}),", "    }", "}", ""]
    out += [
        "/// The artifact-error class a loader refusal maps to.",
        "#[derive(Debug, Clone, Copy, PartialEq, Eq)]",
        "pub(crate) enum RefusalClass {",
    ]
    for c in ref_classes:
        out.append(f"    {_words(c[len('artifact_'):])},")
    out += [
        "}",
        "",
        "/// The class for one loader refusal reason (`reason` or `reason:<suffix>`).",
        "/// A reason the table does not list is `Incompatible`, the fail-closed side.",
        "pub(crate) fn refusal_class(reason: &str) -> RefusalClass {",
        "    let word = reason.split(':').next().unwrap_or(reason);",
        "    match word {",
    ]
    for reason, cls in sorted(by_reason.items()):
        out.append(f"        {_rust_str_any(reason)} => RefusalClass::{_words(cls[len('artifact_'):])},")
    out += ["        _ => RefusalClass::Incompatible,", "    }", "}", ""]
    out += [
        "/// The environment variable that is the second opt-in of an unverified open.",
        f"pub(crate) const UNVERIFIED_ENV: &str = {_rust_str_any(model.sdk['loader']['unverified_env'])};",
        "",
    ]
    return "\n".join(out)


def outputs(model) -> list[Output]:
    return [
        Output(DECLS_PATH, content=_rustfmt(render_decls(model))),
        Output(INVOKE_PATH, content=_rustfmt(render_invoke(model))),
        Output(CALLS_PATH, content=_rustfmt(render_calls(model))),
        Output(VOCAB_PATH, content=_rustfmt(render_vocab(model))),
        Output(ERRMAP_PATH, content=_rustfmt(render_errmap(model))),
    ]
