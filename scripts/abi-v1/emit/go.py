"""The Go ABI v1 declaration layer, generated from spec/abi-v1/abi.json.

This emitter owns everything under `go/internal/abi1` that the plan marks
GENERATED (plan §2.2's Go/Go-linked/Go-invoke/error-map rows):

  go/internal/abi1/abi1_table.h    a small, self-contained C header: one
                                   function-pointer typedef per described
                                   function plus the struct that holds them
                                   all. It does NOT include chtypes.h: every
                                   handle parameter or return is spelled
                                   `void *` here (never the opaque `chs_K *`),
                                   which is the SAME void*/typed-pointer
                                   interchangeability go/chtypes/multiversion.go
                                   already relies on to call through a function
                                   pointer without linking the real header —
                                   measured safe on every ABI this repository
                                   targets (identical pointer representation
                                   and calling convention). This header is
                                   `#include`d by BOTH abi_gen.go's and
                                   linked_gen.go's cgo preambles, so cgo's
                                   per-package type unification (confirmed
                                   empirically: two files that `#include` the
                                   same header and share a named C struct tag
                                   receive the SAME Go type for it) gives both
                                   files one Go-visible `C.struct_chtypes_abi1_table`.
  go/internal/abi1/abi_gen.go      the cgo preamble (dlopen, the glibc-version
                                   probe D3 step 1 needs, handshake/resolve-all,
                                   one trampoline per function) plus, in plain
                                   Go: the Table type, a typed wrapper struct
                                   per handle kind (Close() + a finalizer),
                                   CallError, and one typed, memory-safe call
                                   wrapper method per described function —
                                   copy-then-free on every chs_buf, a minted
                                   handle object for every other out_handle,
                                   and the status/error marshaling every
                                   status-returning call shares.
  go/internal/abi1/linked_gen.go  (`//go:build chtypes_linked`) fills the
                                   SAME table from `&chs_x`, `#include
                                   "chtypes.h"` (the real, linked header);
                                   every symbol is resolved by the linker
                                   itself, so step 6 is trivially satisfied
                                   and this filler can never report a missing
                                   symbol.
  go/internal/abi1/errmap_gen.go  StatusClass(status) -> spec/abi-v1/sdk.json's
                                   errors.status/.classes table, so the
                                   hand-written loader and (later) the public
                                   API never carry their own copy of it.
  go/internal/abi1/invoke_gen_test.go  (test-only) an invoke-by-name
                                   dispatcher over EVERY described function,
                                   for tests/fixtures/abi-v1/cases.json's
                                   echo/status/handshake cases: it marshals a
                                   case's `args` (recursively minting a nested
                                   handle argument by invoking ITS OWN `fn`
                                   first) onto the generated typed wrapper
                                   above and reports a generic, comparable
                                   result. The hand-written conformance runner
                                   (go/internal/abi1/conformance_test.go) is
                                   the only caller.

WHY GOFMT-CLEAN BY CONSTRUCTION, NOT BY RUNNING gofmt. An emitter's output may
depend on nothing but the model (no clock, no environment, no network, no
randomness — emit/__init__.py's contract), and which gofmt binary happens to
be on a given machine's PATH is exactly "environment". So this writes
already-canonical Go text directly:

  * every C declaration lives inside the `/* ... */` cgo preamble, which
    gofmt treats as opaque comment text and never reformats (confirmed
    against this checkout's own gofmt) — so the C side's own indentation
    style is free;
  * on the Go side, every multi-field struct this emitter writes is small and
    FIXED (Table, CallError, and one {ptr, tbl} wrapper per handle kind), so
    its column alignment is computed once, by hand, in `_struct` below,
    using the same "longest name in the block plus one space" rule gofmt's
    tabwriter applies;
  * every var/const group is a sequence of SEPARATE single-statement
    declarations (never a parenthesized `var ( ... )` block), which sidesteps
    tabwriter alignment entirely;
  * every composite literal this emitter needs (the status-name switch, the
    per-function dispatch) is written as a `switch`, never a multi-line map
    literal, for the same reason — gofmt does not align `case` labels.
"""

from __future__ import annotations

from model import BUF_HANDLE, ERROR_HANDLE, SCALARS, STATUS_ENUM

from . import Output, banner

ABI1_TABLE_H = "go/internal/abi1/abi1_table.h"
ABI_GEN = "go/internal/abi1/abi_gen.go"
LINKED_GEN = "go/internal/abi1/linked_gen.go"
ERRMAP_GEN = "go/internal/abi1/errmap_gen.go"
INVOKE_GEN = "go/internal/abi1/invoke_gen_test.go"

TAB = "\t"


# ------------------------------------------------------------------- naming


def _pascal_word(word: str) -> str:
    return word[:1].upper() + word[1:] if word else word


def _pascal(parts) -> str:
    return "".join(_pascal_word(p) for p in parts if p)


def go_func_name(model, fn) -> str:
    override = model.sdk.get("naming", {}).get("go", {}).get(fn.name)
    if override:
        return override
    assert fn.name.startswith(model.prefix), fn.name
    return _pascal(fn.name[len(model.prefix) :].split("_"))


def go_handle_type(handle_name: str) -> str:
    assert handle_name.startswith("chs_"), handle_name
    word = handle_name[len("chs_") :]
    if word == "error":
        return "ErrorHandle"
    return _pascal(word.split("_"))


# Go predeclared identifiers and keywords: a parameter or local this emitter
# derives from an ABI name that collides with one of these (today, only the
# chs_error accessors' own "error" parameter) gets "Arg" appended, so it
# never shadows a builtin the way revive's redefines-builtin-id rule (this
# repository's .golangci.yml) refuses.
_GO_RESERVED = frozenset(
    """bool byte complex64 complex128 error float32 float64 int int8 int16 int32 int64 rune string uint uint8
    uint16 uint32 uint64 uintptr any true false iota nil append cap close complex copy delete imag len make new
    panic print println real recover break case chan const continue default defer else fallthrough for func go
    goto if import interface map package range return select struct switch type var""".split()
)


def _camel(parts) -> str:
    parts = [p for p in parts if p]
    if not parts:
        return ""
    return parts[0] + _pascal(parts[1:])


def go_param_name(name: str) -> str:
    """The lowerCamelCase Go spelling of an ABI parameter name (abi.json's own
    names are snake_case C identifiers)."""
    ident = _camel(name.split("_"))
    return f"{ident}Arg" if ident in _GO_RESERVED else ident


def owned_handle_kinds(model) -> list[str]:
    """Every handle kind this emitter mints a Go wrapper struct for: ALL of
    them (buf and error included), so a `handle`/`out_handle` parameter of
    any kind has one uniform Go representation. Order: abi.json's own
    (insertion order), which is what every loop below uses too."""
    return list(model.handles)


# --------------------------------------------------------- C type mapping
#
# Deliberately NOT model.Param.c / model.Return.c_type: those name the real
# opaque `chs_K *` handle types, which only exist once chtypes.h is
# `#include`d (linked_gen.go only). abi1_table.h and abi_gen.go's trampolines
# stay header-independent by spelling every handle pointer `void *` instead
# (this module's docstring explains why that is safe).


def _c_scalar(t: str) -> str:
    return SCALARS[t]


def c_param_parts(p) -> list[tuple[str, str]]:
    """[(c_type, c_name), ...] for one high-level Param — one entry, except
    bytes_in, which is always a (pointer, length) pair."""
    if p.kind == "scalar":
        return [(_c_scalar(p.type), p.name)]
    if p.kind == "enum":
        return [("int32_t", p.name)]
    if p.kind == "bytes_in":
        return [("const uint8_t *", p.name), ("size_t", f"{p.name}_len")]
    if p.kind == "handle":
        const = "const " if p.const else ""
        return [(f"{const}void *", p.name)]
    if p.kind in ("out_handle", "out_error"):
        return [("void **", p.name)]
    if p.kind == "out_scalar":
        return [(f"{_c_scalar(p.type)} *", p.name)]
    raise ValueError(f"go.py: unhandled param kind {p.kind!r}")


def c_flat_params(fn) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for p in fn.params:
        out += c_param_parts(p)
    return out


def c_return_type(r) -> str:
    if r.kind == "void":
        return "void"
    if r.kind == "status":
        return "int32_t"  # chs_status's repr (D3)
    if r.kind == "scalar":
        return _c_scalar(r.type)
    if r.kind == "enum":
        return "int32_t"
    if r.kind == "handle":
        return "void *"
    raise ValueError(f"go.py: unhandled return kind {r.kind!r}")


def _typedef_name(fn) -> str:
    return f"chtypes_abi1_fn_{fn.name}"


def _trampoline_name(fn) -> str:
    return f"chtypes_abi1_call_{fn.name}"


# ------------------------------------------------------- small Go formatting


def _struct(name: str, fields: list, *, doc: str = "") -> str:
    """A gofmt-canonical `type NAME struct { ... }`: tab-indented fields,
    column-aligned the way gofmt's tabwriter aligns a contiguous block (pad
    every column to one space past its longest entry in the block). `fields`
    is (name, type) pairs, or (name, type, tag) triples for a struct tag."""
    rows = [(f[0], f[1], f[2] if len(f) > 2 else None) for f in fields]
    name_pad = max((len(n) for n, _, _ in rows), default=0) + 1
    has_tags = any(tag is not None for _, _, tag in rows)
    type_pad = max((len(t) for _, t, _ in rows), default=0) + 1 if has_tags else 0
    lines = []
    for n, t, tag in rows:
        line = f"{TAB}{n}{' ' * (name_pad - len(n))}{t}"
        if has_tags:
            line += " " * (type_pad - len(t))
            if tag is not None:
                line += f"`{tag}`"
        lines.append(line.rstrip() if not has_tags else line)
    out = []
    if doc:
        out += [f"// {line}" if line else "//" for line in doc.splitlines()]
    out.append(f"type {name} struct {{")
    out += lines
    out.append("}")
    return "\n".join(out)


def _doc(text: str) -> list[str]:
    return [f"// {line}" if line else "//" for line in text.splitlines()]


# --------------------------------------------------------------- C preamble


def _c_glibc_and_dlopen() -> list[str]:
    return [
        "// Resolve glibc's own version string from the CURRENT process (never from the",
        "// candidate artifact, which has not been dlopen'd yet): RTLD_DEFAULT searches only",
        "// images already loaded into this process, so this never touches the artifact path",
        '// at all -- D3\'s loader step 1 runs "before dlopen". Absence (NULL) means a non-glibc',
        '// libc (musl); loader.go reports that as the sdk.json "no_glibc" reason.',
        "static int chtypes_abi1_glibc_version(char *buf, size_t buflen) {",
        "    const char *(*fn)(void) = (const char *(*)(void)) dlsym(RTLD_DEFAULT, \"gnu_get_libc_version\");",
        "    if (!fn) return 0;",
        "    const char *v = fn();",
        "    if (!v) return 0;",
        "    size_t n = strlen(v);",
        "    if (n >= buflen) n = buflen - 1;",
        "    memcpy(buf, v, n);",
        "    buf[n] = 0;",
        "    return 1;",
        "}",
        "",
        "// RTLD_NOW|RTLD_LOCAL (sdk.json loader.dlopen_flags, D-step 2): every relocation",
        "// is bound at open time (a missing symbol in a transitively linked library fails",
        "// HERE, not on first call), and LOCAL keeps this image's symbols out of the global",
        "// namespace the way every vendored ClickHouse build has always required.",
        "static void *chtypes_abi1_dlopen(const char *path, const char **errmsg) {",
        "    dlerror();",
        "    void *h = dlopen(path, RTLD_NOW | RTLD_LOCAL);",
        "    if (!h) *errmsg = dlerror();",
        "    return h;",
        "}",
        "",
    ]


def render_table_header(model) -> str:
    """abi1_table.h: one function-pointer typedef per described function, and
    the struct that holds every one of them, by name. Header-independent on
    purpose (see this module's docstring)."""
    out = [
        f"/* {banner(model)} */",
        "#ifndef CHTYPES_ABI1_TABLE_H",
        "#define CHTYPES_ABI1_TABLE_H",
        "",
        "#include <stddef.h>",
        "#include <stdint.h>",
        "",
        "/* Every handle pointer below is `void *`, never the opaque `chs_K *` chtypes.h",
        "   declares: this header stays include-free of chtypes.h so go/internal/abi1's",
        "   dlopen-mode trampolines never need the real header at all. A function pointer's",
        "   argument/return types only need to match in REPRESENTATION to call through it",
        "   correctly (every target here: identical pointer representation and calling",
        "   convention for `void *` and a struct pointer), which is the same rule",
        "   go/chtypes/multiversion.go already relies on for its own dlopen path. */",
        "",
    ]
    for fn in model.functions:
        params = c_flat_params(fn)
        plist = ", ".join(f"{t} {n}" for t, n in params) or "void"
        out.append(f"typedef {c_return_type(fn.returns)} (*{_typedef_name(fn)})({plist});")
    out.append("")
    out.append("struct chtypes_abi1_table {")
    for fn in model.functions:
        out.append(f"    {_typedef_name(fn)} {fn.name};")
    out.append("};")
    out.append("")
    out.append("#endif /* CHTYPES_ABI1_TABLE_H */")
    out.append("")
    return "\n".join(out)


def _c_resolve_block(name: str, fns, *, doc: list[str]) -> list[str]:
    out = [*doc, f"static const char *{name}(void *h, struct chtypes_abi1_table *t) {{"]
    for fn in fns:
        out.append(f'    t->{fn.name} = ({_typedef_name(fn)}) dlsym(h, "{fn.name}");')
        out.append(f"    if (!t->{fn.name}) return \"{fn.name}\";")
    out.append("    return NULL;")
    out.append("}")
    out.append("")
    return out


def _c_trampolines(model) -> list[str]:
    out = []
    for fn in model.functions:
        params = c_flat_params(fn)
        plist = ", ".join(f"{t} {n}" for t, n in params)
        sig = "struct chtypes_abi1_table *t" + (f", {plist}" if plist else "")
        args = ", ".join(n for _, n in params)
        ret = c_return_type(fn.returns)
        call = f"t->{fn.name}({args})"
        if ret == "void":
            out.append(f"static void {_trampoline_name(fn)}({sig}) {{ {call}; }}")
        else:
            out.append(f"static {ret} {_trampoline_name(fn)}({sig}) {{ return {call}; }}")
    return out


def render_abi_gen_preamble(model) -> list[str]:
    out = [
        "#cgo LDFLAGS: -ldl",
        "// glibc's dlfcn.h only declares RTLD_DEFAULT under _GNU_SOURCE; darwin's does not",
        "// need it, but defining it there too is harmless. Must precede every #include.",
        "#define _GNU_SOURCE",
        '#include "abi1_table.h"',
        "#include <dlfcn.h>",
        "#include <stddef.h>",
        "#include <stdint.h>",
        "#include <stdlib.h>",
        "#include <string.h>",
        "",
    ]
    out += _c_glibc_and_dlopen()
    # One resolver per handshake-class symbol (D1.2: "callable before any
    # check"), not a single bundled resolve: the loader needs chs_abi_version
    # alone at step 3 and chs_build_info alone at step 4, each with its OWN
    # refusal reason, and chs_clickhouse_version is not needed before step 6
    # at all -- bundling all three the way an earlier draft of this emitter
    # did made every one of them read as "not_v1" when only one was absent,
    # which is wrong for chs_build_info (sdk.json wants
    # "missing_symbol:chs_build_info") and for chs_clickhouse_version
    # (step 6's "missing_symbol:chs_clickhouse_version", never reached early).
    for fn in model.functions:
        if fn.cls != "handshake":
            continue
        out += _c_resolve_block(
            f"chtypes_abi1_resolve_{fn.name}",
            [fn],
            doc=[
                f"// Resolves {fn.name} alone (D1.2 handshake class: callable before any",
                "// check). Returns NULL on success, else the symbol's own name.",
            ],
        )
    out += _c_resolve_block(
        "chtypes_abi1_resolve_all",
        model.functions,
        doc=[
            "// Step 6: every described symbol -- api, tooling and the chs_abi_revision",
            "// tombstone included, for presence -- resolved only after steps 1-5 pass.",
            "// Returns the first missing symbol's name, or NULL.",
        ],
    )
    out += _c_trampolines(model)
    return out


def render_abi_gen(model) -> str:
    c = render_abi_gen_preamble(model)
    out = [f"// {banner(model)}", "", "package abi1", "", "/*"]
    out += c
    out += ["*/", 'import "C"', "", "import ("]
    out += [f'{TAB}"fmt"', f'{TAB}"runtime"', f'{TAB}"unsafe"']
    out += [")", ""]

    out.append("// ChsAbiVersion is CHS_ABI_VERSION (D1.2): what chs_abi_version() must return.")
    out.append(f"const ChsAbiVersion = {model.abi}")
    out.append("")
    out.append("// ChsAbiFingerprint is CHS_ABI_FINGERPRINT: sha256 over the RFC 8785 canonical")
    out.append("// form of spec/abi-v1/abi.json. chs_build_info()'s abi_fingerprint field is")
    out.append("// this value, copied from the compiled-in macro, never recomputed.")
    out.append(f'const ChsAbiFingerprint = {_go_str(model.fingerprint)}')
    out.append("")

    out.append(
        "\n".join(
            _doc(
                "Table is one dlopen'd (or, under -tags chtypes_linked, statically linked)\n"
                "ABI v1 image: the resolved function-pointer table plus, in dlopen mode, the\n"
                "dlopen handle itself (nil in linked mode, where there is nothing to dlclose --\n"
                "D3: no dlclose, ever)."
            )
        )
    )
    out.append(_struct("Table", [("h", "unsafe.Pointer"), ("tbl", "C.struct_chtypes_abi1_table")]))
    out.append("")

    out.append("// GlibcVersion reads gnu_get_libc_version() from THIS process (loader step 1).")
    out.append("// The second return is false when the symbol is absent (a non-glibc libc).")
    out.append("func GlibcVersion() (string, bool) {")
    out.append(f"{TAB}var buf [64]C.char")
    out.append(f"{TAB}ok := C.chtypes_abi1_glibc_version(&buf[0], C.size_t(len(buf)))")
    out.append(f"{TAB}if ok == 0 {{")
    out.append(f'{TAB}{TAB}return "", false')
    out.append(f"{TAB}}}")
    out.append(f"{TAB}return C.GoString(&buf[0]), true")
    out.append("}")
    out.append("")

    out.append("// OpenLibrary dlopen's path (RTLD_NOW|RTLD_LOCAL) with an otherwise empty table.")
    out.append("// The second return is the dlerror() text on failure, else \"\".")
    out.append("func OpenLibrary(path string) (*Table, string) {")
    out.append(f"{TAB}cpath := C.CString(path)")
    out.append(f"{TAB}defer C.free(unsafe.Pointer(cpath))")
    out.append(f"{TAB}var errmsg *C.char")
    out.append(f"{TAB}h := C.chtypes_abi1_dlopen(cpath, &errmsg)")
    out.append(f"{TAB}if h == nil {{")
    out.append(f"{TAB}{TAB}if errmsg != nil {{")
    out.append(f"{TAB}{TAB}{TAB}return nil, C.GoString(errmsg)")
    out.append(f"{TAB}{TAB}}}")
    out.append(f'{TAB}{TAB}return nil, "dlopen: unknown error"')
    out.append(f"{TAB}}}")
    out.append(f"{TAB}return &Table{{h: h}}, \"\"")
    out.append("}")
    out.append("")

    for fn in model.functions:
        if fn.cls != "handshake":
            continue
        rname = f"Resolve{go_func_name(model, fn)}"
        out.append(f"// {rname} resolves {fn.name} alone (D1.2). The return is \"\" once it")
        out.append(f'// resolved, else "{fn.name}".')
        out.append(f"func (t *Table) {rname}() string {{")
        out.append(f"{TAB}if m := C.chtypes_abi1_resolve_{fn.name}(t.h, &t.tbl); m != nil {{")
        out.append(f"{TAB}{TAB}return C.GoString(m)")
        out.append(f"{TAB}}}")
        out.append(f'{TAB}return ""')
        out.append("}")
        out.append("")

    out.append("// ResolveAll resolves every described symbol (step 6). The return is the first")
    out.append("// missing symbol's name, or \"\" once every one of them resolved.")
    out.append("func (t *Table) ResolveAll() string {")
    out.append(f"{TAB}if m := C.chtypes_abi1_resolve_all(t.h, &t.tbl); m != nil {{")
    out.append(f"{TAB}{TAB}return C.GoString(m)")
    out.append(f"{TAB}}}")
    out.append(f'{TAB}return ""')
    out.append("}")
    out.append("")

    out.append(render_call_error(model))
    out.append("")
    out.append(render_status_name(model))
    out.append("")
    out.append(render_helpers(model))
    out.append("")

    for kind in owned_handle_kinds(model):
        out.append(render_handle_wrapper(model, kind))
        out.append("")

    for fn in model.functions:
        out.append(render_wrapper(model, fn))
        out.append("")

    text = "\n".join(out).rstrip("\n") + "\n"
    return text


def render_call_error(model) -> str:
    fields = [("Status", "string"), ("ChCode", "int32"), ("ChName", "string"), ("Message", "string"), ("Column", "string")]
    s = _struct(
        "CallError",
        fields,
        doc=(
            "CallError is D3's per-call error shape, read verbatim off a non-OK status:\n"
            "the chs_status NAME (never the bare int), ch_code, ch_name, message and column.\n"
            "It carries no class of its own -- StatusClass(e.Status) is sdk.json's table,\n"
            "generated into errmap_gen.go, and the public API (wave C) is what turns this\n"
            "into an idiomatic *SchemaError/*UsageError/..."
        ),
    )
    s += "\n\n"
    s += "func (e *CallError) Error() string {\n"
    s += f'{TAB}if e.ChName != "" {{\n'
    s += f'{TAB}{TAB}return fmt.Sprintf("%s: %s (%s)", e.Status, e.Message, e.ChName)\n'
    s += f"{TAB}}}\n"
    s += f'{TAB}return fmt.Sprintf("%s: %s", e.Status, e.Message)\n'
    s += "}"
    return s


def render_status_name(model) -> str:
    out = [
        "// statusName maps a raw chs_status value to its chs_status.h NAME. Every value",
        "// the description does not list reads as \"CHS_STATUS_<n>\" -- it cannot happen",
        "// under a matching fingerprint (D3's closed, frozen enum), and the binding still",
        "// fails closed rather than panicking on it.",
        "func statusName(v int32) string {",
        f"{TAB}switch v {{",
    ]
    for ev in model.enums[STATUS_ENUM].values:
        out.append(f"{TAB}case {ev.value}:")
        out.append(f'{TAB}{TAB}return "{ev.name}"')
    out.append(f"{TAB}default:")
    out.append(f'{TAB}{TAB}return fmt.Sprintf("CHS_STATUS_%d", v)')
    out.append(f"{TAB}}}")
    out.append("}")
    return "\n".join(out)


def render_helpers(model) -> str:
    ok_value = next(v.value for v in model.enums[STATUS_ENUM].values if v.name == "CHS_OK")
    return "\n".join(
        [
            "// bytesPtr returns (NULL, 0) for an empty slice, so a bytes_in parameter's",
            "// \"length 0 means none\" rule (model.py's Param docs) always holds, and a",
            "// pointer into b's backing array plus its length otherwise. The pointer is",
            "// only read synchronously by the trampoline call that follows it in the same",
            "// statement, which is what cgo's pointer-passing rules require.",
            "func bytesPtr(b []byte) (*C.uint8_t, C.size_t) {",
            f"{TAB}if len(b) == 0 {{",
            f"{TAB}{TAB}return nil, 0",
            f"{TAB}}}",
            f"{TAB}return (*C.uint8_t)(unsafe.Pointer(&b[0])), C.size_t(len(b))",
            "}",
            "",
            "// copyBytes copies n bytes out of a BORROWED C pointer (never retained).",
            "func copyBytes(p unsafe.Pointer, n int) []byte {",
            f"{TAB}out := make([]byte, n)",
            f"{TAB}if n > 0 {{",
            f"{TAB}{TAB}copy(out, unsafe.Slice((*byte)(p), n))",
            f"{TAB}}}",
            f"{TAB}return out",
            "}",
            "",
            "// readAndFreeBuf copies b's bytes (nil for a nil b, an absent optional buf)",
            "// then frees it -- D3's copy-then-free rule every status-returning call's",
            "// chs_buf output follows, instead of handing a live chs_buf to a caller.",
            "func readAndFreeBuf(t *Table, b *Buf) []byte {",
            f"{TAB}if b == nil {{",
            f"{TAB}{TAB}return nil",
            f"{TAB}}}",
            f"{TAB}out := copyBytes(t.BufData(b), int(t.BufLen(b)))",
            f"{TAB}b.Close()",
            f"{TAB}return out",
            "}",
            "",
            "// makeCallError is D3's status/error marshaling, shared by every generated",
            "// call wrapper: nil on CHS_OK, else a *CallError read verbatim off err (if",
            "// any) and copy-then-freed, matching the chs_error accessors' owned-buffer",
            "// form (Q-c's current default).",
            "func makeCallError(t *Table, status int32, err *ErrorHandle) *CallError {",
            f"{TAB}if status == {ok_value} {{",
            f"{TAB}{TAB}return nil",
            f"{TAB}}}",
            f"{TAB}ce := &CallError{{Status: statusName(status)}}",
            f"{TAB}if err != nil {{",
            f"{TAB}{TAB}ce.ChCode = t.ErrorChCode(err)",
            f"{TAB}{TAB}ce.ChName = string(readAndFreeBuf(t, t.ErrorChName(err)))",
            f"{TAB}{TAB}ce.Message = string(readAndFreeBuf(t, t.ErrorMessage(err)))",
            f"{TAB}{TAB}ce.Column = string(readAndFreeBuf(t, t.ErrorColumn(err)))",
            f"{TAB}{TAB}err.Close()",
            f"{TAB}}}",
            f"{TAB}return ce",
            "}",
        ]
    )


def render_handle_wrapper(model, kind: str) -> str:
    go_type = go_handle_type(kind)
    free_fn = go_func_name(model, model.function(model.handles[kind].free))
    s = "\n".join(
        _doc(
            f"{go_type} is a live {kind}: a copy-then-free chs_buf and a copy-then-free\n"
            "chs_error never reach a caller as one of these (the generated wrapper layer\n"
            "reads their content and frees them immediately), so in practice only a type\n"
            "this ABI generation marks provisional lives this long -- Close() (and the\n"
            "finalizer backing it) call the SAME free function every free order is safe\n"
            "under (D2: a child holds its parents alive, so freeing a live parent first\n"
            "is not a use-after-free)."
            if kind not in (BUF_HANDLE, ERROR_HANDLE)
            else f"{go_type} wraps a live {kind}. Close() (and the finalizer backing it)\n"
            f"call {free_fn}; D2 makes any free order safe."
        )
    )
    s += "\n" + _struct(go_type, [("ptr", "unsafe.Pointer"), ("tbl", "*Table")])
    s += "\n\n"
    s += f"func new{go_type}(t *Table, ptr unsafe.Pointer) *{go_type} {{\n"
    s += f"{TAB}if ptr == nil {{\n"
    s += f"{TAB}{TAB}return nil\n"
    s += f"{TAB}}}\n"
    s += f"{TAB}h := &{go_type}{{ptr: ptr, tbl: t}}\n"
    s += f"{TAB}runtime.SetFinalizer(h, (*{go_type}).Close)\n"
    s += f"{TAB}return h\n"
    s += "}\n\n"
    s += f"// rawPtr is nil-receiver-safe: a nil *{go_type} (an absent optional handle)\n"
    s += "// marshals to a C NULL, never a panic.\n"
    s += f"func (h *{go_type}) rawPtr() unsafe.Pointer {{\n"
    s += f"{TAB}if h == nil {{\n"
    s += f"{TAB}{TAB}return nil\n"
    s += f"{TAB}}}\n"
    s += f"{TAB}return h.ptr\n"
    s += "}\n\n"
    s += f"// Close frees this {kind} (a no-op if already closed or nil); any free order"
    s += "\n// is safe (D2). It never returns a non-nil error; the signature matches io.Closer\n"
    s += "// for callers that want to defer it.\n"
    s += f"func (h *{go_type}) Close() error {{\n"
    s += f"{TAB}if h == nil || h.ptr == nil {{\n"
    s += f"{TAB}{TAB}return nil\n"
    s += f"{TAB}}}\n"
    s += f"{TAB}runtime.SetFinalizer(h, nil)\n"
    fn = model.function(model.handles[kind].free)
    s += f"{TAB}C.{_trampoline_name(fn)}(&h.tbl.tbl, h.ptr)\n"
    s += f"{TAB}h.ptr = nil\n"
    s += f"{TAB}return nil\n"
    s += "}"
    return s


# ------------------------------------------------------- per-function wrapper


def _go_scalar_type(scalar: str) -> str:
    return {
        "void": "",
        "int": "int32",
        "int32": "int32",
        "uint32": "uint32",
        "int64": "int64",
        "uint64": "uint64",
        "size": "uint64",
    }[scalar]


def _out_go_type(model, p) -> str:
    if p.type == BUF_HANDLE:
        return "*Buf"
    return f"*{go_handle_type(p.type)}"


def render_wrapper(model, fn) -> str:
    name = go_func_name(model, fn)
    ins = [p for p in fn.params if not p.is_out]
    outs = [p for p in fn.params if p.kind == "out_handle"]
    err_param = next((p for p in fn.params if p.kind == "out_error"), None)

    go_params = []
    for p in ins:
        n = go_param_name(p.name)
        if p.kind == "scalar":
            go_params.append((n, _go_scalar_type(p.type)))
        elif p.kind == "enum":
            go_params.append((n, "int32"))
        elif p.kind == "bytes_in":
            go_params.append((n, "[]byte"))
        elif p.kind == "handle":
            go_params.append((n, f"*{go_handle_type(p.type)}"))
        else:
            raise ValueError(f"go.py: unexpected input param kind {p.kind!r} on {fn.name}")
    param_list = ", ".join(f"{n} {t}" for n, t in go_params)

    if fn.returns.kind == "status":
        ret_types = [_out_go_type(model, p) for p in outs] + ["*CallError"]
    elif fn.returns.kind == "void":
        ret_types = []
    else:
        ret_types = [_return_go_type(fn.returns)]
    ret_sig = "" if not ret_types else (f" {ret_types[0]}" if len(ret_types) == 1 else f" ({', '.join(ret_types)})")

    lines = [f"// {name} is the generated call wrapper for {fn.name}."]
    if fn.provisional:
        legend = ", ".join(fn.provisional)
        lines.append(f"// PROVISIONAL, waiting on {legend}.")
    lines.append(f"func (t *Table) {name}({param_list}){ret_sig} {{")
    body = TAB

    c_args = ["&t.tbl"]
    pre: list[str] = []
    for p in ins:
        n = go_param_name(p.name)
        if p.kind in ("scalar", "enum"):
            c_type = "int32_t" if p.kind == "enum" else SCALARS[p.type]
            c_args.append(f"C.{c_type}({n})")
        elif p.kind == "bytes_in":
            ptr_var, len_var = f"{n}Ptr", f"{n}Len"
            pre.append(f"{ptr_var}, {len_var} := bytesPtr({n})")
            c_args.append(ptr_var)
            c_args.append(len_var)
        elif p.kind == "handle":
            c_args.append(f"{n}.rawPtr()")

    out_vars = []
    for p in outs:
        v = f"{go_param_name(p.name)}Out"
        pre.append(f"var {v} unsafe.Pointer")
        c_args.append(f"&{v}")
        out_vars.append((p, v))
    err_var = None
    if err_param is not None:
        err_var = "errOut"
        pre.append(f"var {err_var} unsafe.Pointer")
        c_args.append(f"&{err_var}")

    for p in pre:
        lines.append(f"{body}{p}")

    call = f"C.{_trampoline_name(fn)}({', '.join(c_args)})"
    if fn.returns.kind == "status":
        lines.append(f"{body}status := {call}")
        ret_vals = []
        for p, v in out_vars:
            if p.type == BUF_HANDLE:
                ret_vals.append(f"newBuf(t, {v})")
            else:
                ret_vals.append(f"new{go_handle_type(p.type)}(t, {v})")
        err_expr = f"makeCallError(t, int32(status), newErrorHandle(t, {err_var}))" if err_var else "makeCallError(t, int32(status), nil)"
        ret_vals.append(err_expr)
        lines.append(f"{body}return {', '.join(ret_vals)}")
    elif fn.returns.kind == "void":
        lines.append(f"{body}{call}")
    else:
        r = fn.returns
        if r.type == "cstr_static":
            lines.append(f"{body}return C.GoString({call})")
        elif r.type == "u8ptr_const":
            lines.append(f"{body}return unsafe.Pointer({call})")
        elif r.kind == "handle":
            ctor = "newBuf" if r.type == BUF_HANDLE else f"new{go_handle_type(r.type)}"
            lines.append(f"{body}return {ctor}(t, {call})")
        else:
            go_t = _go_scalar_type(r.type) if r.kind == "scalar" else "int32"
            lines.append(f"{body}return {go_t}({call})")
    lines.append("}")
    return "\n".join(lines)


def _return_go_type(r) -> str:
    if r.type == "cstr_static":
        return "string"
    if r.type == "u8ptr_const":
        return "unsafe.Pointer"
    if r.kind == "handle":
        return "*Buf" if r.type == BUF_HANDLE else f"*{go_handle_type(r.type)}"
    if r.kind == "scalar":
        return _go_scalar_type(r.type)
    return "int32"  # enum


# ---------------------------------------------------------------- linked_gen


def render_linked_gen(model) -> str:
    out = [f"// {banner(model)}", "", "//go:build chtypes_linked", "", "package abi1", "", "/*"]
    out.append("// The header is this repository's own include/chtypes.h -- the SDK owns the")
    out.append("// contract. A full LINK (never needed by scripts/abi-v1/check-linked.sh's")
    out.append("// type-check alone) additionally needs CGO_LDFLAGS naming where libchtypes")
    out.append("// lives.")
    out.append("#cgo CFLAGS: -I${SRCDIR}/../../../include")
    out.append("#cgo LDFLAGS: -lchtypes")
    out.append('#include "abi1_table.h"')
    out.append('#include "chtypes.h"')
    out.append("")
    out.append("// Every address below is resolved by the LINKER, at build time: a described")
    out.append("// symbol chtypes.h does not declare is a link error here, not a runtime one, so")
    out.append("// this fill can never report a missing symbol the way the dlopen path's step 6")
    out.append("// can -- linked mode runs steps 3, 4 and 6, and 6 is trivially satisfied.")
    out.append("static void chtypes_abi1_linked_fill(struct chtypes_abi1_table *t) {")
    for fn in model.functions:
        out.append(f"    t->{fn.name} = ({_typedef_name(fn)}) &{fn.name};")
    out.append("}")
    out += ["*/", 'import "C"', ""]
    out.append("// OpenLinked fills a Table from the statically linked library: no dlopen, no")
    out.append("// dlsym, nothing to ever dlclose (there is no separate image to close).")
    out.append("func OpenLinked() *Table {")
    out.append(f"{TAB}t := &Table{{}}")
    out.append(f"{TAB}C.chtypes_abi1_linked_fill(&t.tbl)")
    out.append(f"{TAB}return t")
    out.append("}")
    out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------- errmap_gen


def render_errmap_gen(model) -> str:
    status_map: dict[str, str | None] = model.sdk["errors"]["status"]
    out = [f"// {banner(model)}", "", "package abi1", ""]
    out.append("// StatusClass maps a chs_status NAME to spec/abi-v1/sdk.json's errors.classes")
    out.append('// key ("" for CHS_OK, which never becomes an error). An unrecognized name --')
    out.append("// never possible under a matching fingerprint -- reads as sdk.json's own")
    out.append('// "unknown" fallback, so a caller still fails closed instead of panicking.')
    out.append("func StatusClass(status string) string {")
    out.append(f"{TAB}switch status {{")
    for name, cls in status_map.items():
        if name == "unknown":
            continue
        out.append(f"{TAB}case {_go_str(name)}:")
        out.append(f'{TAB}{TAB}return {_go_str(cls or "")}')
    out.append(f"{TAB}default:")
    out.append(f'{TAB}{TAB}return {_go_str(status_map.get("unknown") or "")}')
    out.append(f"{TAB}}}")
    out.append("}")
    out.append("")
    return "\n".join(out)


def _go_str(s: str) -> str:
    escaped = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


# ----------------------------------------------------------- invoke_gen_test


def render_invoke_gen(model) -> str:
    out = [f"// {banner(model)}", "", "package abi1", "", "import ("]
    out += [f'{TAB}"bytes"', f'{TAB}"encoding/hex"', f'{TAB}"encoding/json"', f'{TAB}"fmt"', f'{TAB}"unicode/utf8"']
    out += [")", ""]

    out.append(
        "\n".join(
            _doc(
                "Arg is one cases.schema.json argument expression (scripts/abi-v1/emit/cases.py's\n"
                "module docstring): an int literal, a bytes_hex literal, a nested handle\n"
                "minted by invoking its own fn first, or an explicit NULL handle."
            )
        )
    )
    out.append(
        _struct(
            "Arg",
            [
                ("Int", "*int64", 'json:"int,omitempty"'),
                ("BytesHex", "*string", 'json:"bytes_hex,omitempty"'),
                ("Handle", "*HandleArg", 'json:"handle,omitempty"'),
                ("NullHandle", "bool", 'json:"null_handle,omitempty"'),
            ],
        )
    )
    out.append("")
    out.append(
        _struct(
            "HandleArg",
            [("Fn", "string", 'json:"fn"'), ("Args", "[]Arg", 'json:"args"')],
        )
    )
    out.append("")
    out.append(
        "\n".join(
            _doc(
                "InvokeResult is invoke()'s generic report of one call, shaped to match every\n"
                "kind of case tests/fixtures/abi-v1/cases.json describes:\n"
                "  - a handshake case reads Int or Str;\n"
                "  - an echo/status case reads Status, Outputs (the parsed JSON document of\n"
                "    each chs_buf out_handle, keyed by its C parameter name), Handle (a minted\n"
                "    non-buf out_handle, for a nested handle argument to mint from) and Error."
            )
        )
    )
    out.append(
        _struct(
            "InvokeResult",
            [
                ("Int", "*int64"),
                ("Str", "*string"),
                ("Status", "string"),
                ("Outputs", "map[string]interface{}"),
                ("Handle", "interface{}"),
                ("Error", "*CallError"),
            ],
        )
    )
    out.append("")

    for kind in owned_handle_kinds(model):
        go_type = go_handle_type(kind)
        out.append(f"func mint{go_type}(t *Table, a Arg) (*{go_type}, error) {{")
        out.append(f"{TAB}if a.NullHandle {{")
        out.append(f"{TAB}{TAB}return nil, nil")
        out.append(f"{TAB}}}")
        out.append(f"{TAB}if a.Handle == nil {{")
        out.append(f'{TAB}{TAB}return nil, fmt.Errorf("expected a %s handle argument")'.replace("%s", kind))
        out.append(f"{TAB}}}")
        out.append(f"{TAB}r, err := invoke(t, a.Handle.Fn, a.Handle.Args)")
        out.append(f"{TAB}if err != nil {{")
        out.append(f"{TAB}{TAB}return nil, err")
        out.append(f"{TAB}}}")
        out.append(f"{TAB}if r.Error != nil {{")
        out.append(
            f'{TAB}{TAB}return nil, fmt.Errorf("minting a {kind} via %s: %w", a.Handle.Fn, r.Error)'
        )
        out.append(f"{TAB}}}")
        out.append(f"{TAB}h, ok := r.Handle.(*{go_type})")
        out.append(f"{TAB}if !ok {{")
        out.append(f'{TAB}{TAB}return nil, fmt.Errorf("%s did not mint a *{go_type}", a.Handle.Fn)')
        out.append(f"{TAB}}}")
        out.append(f"{TAB}return h, nil")
        out.append("}")
        out.append("")

    out.append(
        "\n".join(
            _doc(
                "bufJSON reads b's content, frees it (copy-then-free, same as the generated\n"
                "call wrappers' own out_handle chs_buf handling), and parses it as JSON --\n"
                "every chs_buf a generic function's out_handle fills holds the stub's echo\n"
                "document (emit/stub.py), a JSON object. Numbers decode as json.Number, so a\n"
                "canonical re-encoding (canonicalJSON, in conformance_test.go) matches\n"
                "cases.json's own literal digits exactly. A buffer that is not valid UTF-8\n"
                "is an error, never decoded: every v1 document is valid UTF-8 JSON (the\n"
                "byte_strings rule), and encoding/json would otherwise replace the bytes."
            )
        )
    )
    out.append("func bufJSON(t *Table, b *Buf) (interface{}, error) {")
    out.append(f"{TAB}data := t.BufData(b)")
    out.append(f"{TAB}n := t.BufLen(b)")
    out.append(f"{TAB}raw := copyBytes(data, int(n))")
    out.append(f"{TAB}b.Close()")
    out.append(f"{TAB}if !utf8.Valid(raw) {{")
    out.append(f'{TAB}{TAB}return nil, fmt.Errorf("bufJSON: the document is not valid UTF-8 (raw: %q)", raw)')
    out.append(f"{TAB}}}")
    out.append(f"{TAB}dec := json.NewDecoder(bytes.NewReader(raw))")
    out.append(f"{TAB}dec.UseNumber()")
    out.append(f"{TAB}var v interface{{}}")
    out.append(f"{TAB}if err := dec.Decode(&v); err != nil {{")
    out.append(f'{TAB}{TAB}return nil, fmt.Errorf("bufJSON: %w (raw: %q)", err, raw)')
    out.append(f"{TAB}}}")
    out.append(f"{TAB}return v, nil")
    out.append("}")
    out.append("")

    out.append("func argBytes(a Arg) ([]byte, error) {")
    out.append(f"{TAB}if a.BytesHex == nil {{")
    out.append(f'{TAB}{TAB}return nil, fmt.Errorf("expected a bytes_hex argument")')
    out.append(f"{TAB}}}")
    out.append(f"{TAB}return hex.DecodeString(*a.BytesHex)")
    out.append("}")
    out.append("")
    out.append("func argInt(a Arg) (int64, error) {")
    out.append(f"{TAB}if a.Int == nil {{")
    out.append(f'{TAB}{TAB}return 0, fmt.Errorf("expected an int argument")')
    out.append(f"{TAB}}}")
    out.append(f"{TAB}return *a.Int, nil")
    out.append("}")
    out.append("")

    out.append(
        "\n".join(
            _doc(
                "invoke calls the function named fn (one of spec/abi-v1/abi.json's described\n"
                "names) with the given case arguments, recursively minting any nested handle\n"
                "argument by invoking ITS OWN fn first (tests/fixtures/abi-v1/cases.json's\n"
                "handle-argument shape). It is test-only: the hand-written conformance runner\n"
                "(conformance_test.go) is its only caller."
            )
        )
    )
    out.append("func invoke(t *Table, fn string, args []Arg) (*InvokeResult, error) {")
    out.append(f"{TAB}switch fn {{")
    for f in model.functions:
        out.append(f"{TAB}case {_go_str(f.name)}:")
        out += _render_invoke_case(model, f)
    out.append(f"{TAB}default:")
    out.append(f'{TAB}{TAB}return nil, fmt.Errorf("invoke: no described function %q", fn)')
    out.append(f"{TAB}}}")
    out.append("}")
    out.append("")
    return "\n".join(out)


def _render_invoke_case(model, fn) -> list[str]:
    ins = [p for p in fn.params if not p.is_out]
    outs = [p for p in fn.params if p.kind == "out_handle"]
    name = go_func_name(model, fn)
    i3 = f"{TAB}{TAB}"
    i4 = f"{TAB}{TAB}{TAB}"
    out = []
    out.append(f"{i3}if len(args) != {len(ins)} {{")
    out.append(f'{i4}return nil, fmt.Errorf("{fn.name}: want %d args, got %d", {len(ins)}, len(args))')
    out.append(f"{i3}}}")
    call_args = []
    for i, p in enumerate(ins):
        a = f"args[{i}]"
        if p.kind in ("scalar", "enum"):
            v = f"v{i}"
            out.append(f"{i3}{v}, err := argInt({a})")
            out.append(f"{i3}if err != nil {{")
            out.append(f"{i4}return nil, fmt.Errorf(\"{fn.name}: arg %d: %w\", {i}, err)")
            out.append(f"{i3}}}")
            cast = "int32" if p.kind == "enum" else _go_scalar_type(p.type)
            call_args.append(f"{cast}({v})")
        elif p.kind == "bytes_in":
            v = f"b{i}"
            out.append(f"{i3}{v}, err := argBytes({a})")
            out.append(f"{i3}if err != nil {{")
            out.append(f"{i4}return nil, fmt.Errorf(\"{fn.name}: arg %d: %w\", {i}, err)")
            out.append(f"{i3}}}")
            call_args.append(v)
        elif p.kind == "handle":
            v = f"h{i}"
            out.append(f"{i3}{v}, err := mint{go_handle_type(p.type)}(t, {a})")
            out.append(f"{i3}if err != nil {{")
            out.append(f"{i4}return nil, fmt.Errorf(\"{fn.name}: arg %d: %w\", {i}, err)")
            out.append(f"{i3}}}")
            call_args.append(v)
        else:
            raise ValueError(f"invoke_gen: unexpected input kind {p.kind!r} on {fn.name}")

    if fn.returns.kind == "status":
        lhs = [f"o{i}" for i in range(len(outs))] + ["callErr"]
        out.append(f"{i3}{', '.join(lhs)} := t.{name}({', '.join(call_args)})")
        out.append(f"{i3}res := &InvokeResult{{Error: callErr}}")
        out.append(f"{i3}if callErr != nil {{")
        out.append(f"{i4}res.Status = callErr.Status")
        out.append(f"{i3}}} else {{")
        ok_value = next(v.value for v in model.enums[STATUS_ENUM].values if v.name == "CHS_OK")
        out.append(f"{i4}res.Status = statusName({ok_value})")
        out.append(f"{i3}}}")
        buf_outs = [(p, v) for p, v in zip(outs, lhs[:-1]) if p.type == BUF_HANDLE]
        other_outs = [(p, v) for p, v in zip(outs, lhs[:-1]) if p.type != BUF_HANDLE]
        if buf_outs:
            out.append(f"{i3}res.Outputs = map[string]interface{{}}{{}}")
            for p, v in buf_outs:
                out.append(f"{i3}if {v} != nil {{")
                out.append(f"{i4}doc, err := bufJSON(t, {v})")
                out.append(f"{i4}if err != nil {{")
                out.append(f'{i4}{TAB}return nil, fmt.Errorf("{fn.name}: %s: %w", {_go_str(p.name)}, err)')
                out.append(f"{i4}}}")
                out.append(f'{i4}res.Outputs[{_go_str(p.name)}] = doc')
                out.append(f"{i3}}}")
        if other_outs:
            # Exactly one, in the current description (model.handles has one
            # non-buf/error kind per out_handle today); the generic shape
            # still loops, so a second one needs no change here.
            p, v = other_outs[0]
            out.append(f"{i3}res.Handle = {v}")
        out.append(f"{i3}return res, nil")
    elif fn.returns.kind == "void":
        out.append(f"{i3}t.{name}({', '.join(call_args)})")
        out.append(f"{i3}return &InvokeResult{{}}, nil")
    else:
        r = fn.returns
        out.append(f"{i3}v := t.{name}({', '.join(call_args)})")
        if r.kind == "handle":
            # Not exercised by any current case (every handle-returning
            # function -- chs_error_ch_name/message/column -- is "special",
            # per emit/stub.classify): read its owned buf, or carry a minted
            # non-buf handle on, for a nested handle argument.
            if r.type == BUF_HANDLE:
                out.append(f"{i3}if v == nil {{")
                out.append(f"{i4}return &InvokeResult{{}}, nil")
                out.append(f"{i3}}}")
                out.append(f"{i3}doc, err := bufJSON(t, v)")
                out.append(f"{i3}if err != nil {{")
                out.append(f'{i4}return nil, fmt.Errorf("{fn.name}: %w", err)')
                out.append(f"{i3}}}")
                out.append(f'{i3}return &InvokeResult{{Outputs: map[string]interface{{}}{{"return": doc}}}}, nil')
            else:
                out.append(f"{i3}return &InvokeResult{{Handle: v}}, nil")
        elif r.type == "cstr_static":
            out.append(f"{i3}s := v")
            out.append(f"{i3}return &InvokeResult{{Str: &s}}, nil")
        elif r.type == "u8ptr_const":
            # Not exercised by any current case (chs_buf_data is "special",
            # per emit/stub.classify, so no cases.json case names it as a
            # top-level fn): kept total over every return kind regardless, the
            # pointer's numeric value standing in for "some address or NULL".
            out.append(f"{i3}n := int64(uintptr(v))")
            out.append(f"{i3}return &InvokeResult{{Int: &n}}, nil")
        else:
            out.append(f"{i3}n := int64(v)")
            out.append(f"{i3}return &InvokeResult{{Int: &n}}, nil")
    return out


# --------------------------------------------------------------------- entry


def _check_names(model) -> None:
    seen: dict[str, str] = {}
    for fn in model.functions:
        name = go_func_name(model, fn)
        if name in seen:
            raise ValueError(f"emit/go.py: {fn.name} and {seen[name]} both mangle to Go name {name!r}")
        seen[name] = fn.name
    seen_h: dict[str, str] = {}
    for kind in owned_handle_kinds(model):
        t = go_handle_type(kind)
        if t in seen_h:
            raise ValueError(f"emit/go.py: {kind} and {seen_h[t]} both mangle to Go handle type {t!r}")
        seen_h[t] = kind


def outputs(model) -> list[Output]:
    _check_names(model)
    return [
        Output(ABI1_TABLE_H, content=render_table_header(model)),
        Output(ABI_GEN, content=render_abi_gen(model)),
        Output(LINKED_GEN, content=render_linked_gen(model)),
        Output(ERRMAP_GEN, content=render_errmap_gen(model)),
        Output(INVOKE_GEN, content=render_invoke_gen(model)),
    ]
