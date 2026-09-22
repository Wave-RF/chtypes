#!/usr/bin/env python3
"""check-abi-decls.py — every binding's FFI declaration, against the header's prototypes.

WHY THIS EXISTS. `go/chtypes/multiversion.go` declared its own function-pointer
typedef for `chs_rows` with NINE parameters against a symbol that takes TEN. It
passed gofmt, go vet, golangci-lint, the whole Go suite and all twelve required
CI checks, because nothing in this repository compared a declaration against
`include/chtypes.h`. It is the dlopen path — the default — and it could not fire
while published artifacts are revision 4, since the bindings refuse at load. Its
first use in anger would be the first successful load of a revision-5 artifact:
a crash or a garbage pointer arriving AT the cutover, looking like the cutover's
fault.

So this generates a prototype table from the header — one entry per `chs_*`
function, with its parameter count AND each parameter's C type — and asserts
every binding's own declaration against it. Types matter as much as count: a
declaration with the right arity and a wrong type is exactly as silent and
exactly as dangerous.

    scripts/check-abi-decls.py             check the tree, print the table
    scripts/check-abi-decls.py --verbose   print every binding's own classes too,
                                           not only the header's
    scripts/check-abi-decls.py --selftest  plant wrong arities and wrong types
                                           in every binding, prove each fires

WHAT IT READS — the declaration each binding actually uses at runtime, never a
doc comment describing it:

    go         go/chtypes/multiversion.go   the cgo `fn_*` typedefs, paired to
                                            symbols by their own dlsym casts
    go-linked  go/chtypes/linked.go         the direct `C.chs_*(...)` call sites
                                            (arity here; their TYPES are the C
                                            compiler's, run by
                                            scripts/check-linked-build.sh)
    python     python/src/chtypes/_native.py   the `_SIGNATURES` argtypes/restype
    ts         ts/src/ffi.ts                the ffi-rs `d(ret, [params])` table
    rust       rust/src/ffi.rs              the `extern "C"` fn type aliases,
                                            paired to symbols by dlsym-time
                                            `require`/`optional` calls

================================================================================
WHAT THIS DOES NOT CATCH — read this before trusting a green run
================================================================================

Types are compared as EQUIVALENCE CLASSES, not by full C type analysis. The
header's types are mapped to a small set of classes, and each binding's FFI
vocabulary is mapped to the set of classes it is CAPABLE of expressing. A
mismatch is reported when the header's class is not in that set. That is the
honest ceiling: three of the four vocabularies cannot express distinctions the
C header makes, so a checker pretending to full fidelity would either be wrong
or would fail permanently on correct code.

Concretely, this check DOES NOT distinguish:

  * `const`. It is not part of the C ABI, and Python's ctypes and ffi-rs cannot
    express it at all. `chs_schema *` and `const chs_schema *` are one class.
  * WHICH opaque handle. `chs_schema *`, `chs_filter *` and `chs_block *` are
    one class, so passing a filter where a schema belongs is not caught here.
    (It IS caught at runtime by the ABI's own 1002 answer, loudly.)
  * In TypeScript, any two pointer kinds. ffi-rs's `External` is a raw pointer
    with no target type, so `char **`, `int *`, `chs_bytes *`, an owned
    `char *` and a handle are indistinguishable in that vocabulary. `Str` is
    still distinct from `External`, so a string where a pointer belongs — and
    the reverse — IS caught.
  * In TypeScript, `int` from `unsigned`. `doc_flags` is declared `I32` on
    purpose (it carries the defined bits and any refused ones identically), so
    `I32` is accepted for both.
  * In Python, an owned `char *` from a handle. Both are `c_void_p`, and that
    is deliberate: ctypes converts a `c_char_p` result to `bytes` and throws
    the pointer away, which would make the mandatory `chs_free` impossible.
  * In Go's dlopen typedefs, `void *` from a named struct pointer — that file
    never includes the header, which is the whole point of the dlopen path.
  * Struct LAYOUT. `chs_bytes` is re-declared by hand in Go, Python, TS and
    Rust; this checks that a parameter IS the bytes-struct pointer, never that
    the four re-declarations agree field for field.
  * Anything in `go/chtypes/linked.go` beyond ARITY. That file includes the
    real header, so cgo type-checks its calls — which is strictly stronger
    than the equivalence classes below, and a second looser copy of it here
    would be a liability rather than redundancy.

    ⚠️ That exemption was once a hole, and it is worth knowing why it is not
    one now. The types were checked only under `-tags chtypes_linked`, and
    NOTHING BUILT THAT TAG: no CI job passed it, on the received (and
    `unverified`) reason that a linked build needs a native build tree from
    the other half of the project, which public CI has no access to. So the
    linked path's arity was checked on every pull request and its types were
    checked by nobody, while this file said the compiler had them.

    Measured 2026-09-21 (#118), the received reason is WRONG. `go build` and
    `go vet` of a non-main cgo package compile and type-check the translation
    unit and only PROBE the link; the probe's failure for want of the library
    is recorded and deferred to whoever links a binary. The type check
    therefore needs a C compiler and the in-repo header and nothing else.
    scripts/check-linked-build.sh now runs it in the no-artifact CI leg, on
    every pull request, with three mistyped plants proving it goes red.

    So this file's arity-only treatment of `linked.go` is now correct rather
    than merely cheap — but the two are a PAIR. If check-linked-build.sh is
    removed, or stops building the tag, or its `go` job loses these steps,
    the linked path's types go straight back to being checked by nobody and
    this paragraph is the only warning.
  * Calling convention, variadics, and function-pointer parameters. The ABI
    has none of these; if one appears, the header parser refuses it loudly
    rather than guessing.

It DOES catch, in every binding: a wrong parameter COUNT, a wrong return or
parameter class (scalar where a pointer belongs and the reverse, a string
pointer where an opaque pointer belongs and the reverse, `int` vs `unsigned`
vs `size_t`), a declaration naming a `chs_*` symbol the header does not have,
and a parser that stopped seeing a binding at all.

A binding that does not declare a function at all is REPORTED (`—` in the
table), not failed: not declaring a symbol means the binding cannot call it,
which is a design choice, not undefined behavior. Go's dlopen path deliberately
declares neither `chs_shutdown` nor `chs_set_default_settings`.
"""

from __future__ import annotations

import ast
import os
import re
import shutil
import sys
import tempfile

# --------------------------------------------------------------------- classes
#
# The class vocabulary. Every header type maps to exactly one of these; every
# binding token maps to the SET of these it can faithfully represent.

CLASSES = ("void", "int", "uint", "size", "cstr", "charp", "charpp", "intp", "handle", "bytesp")

# The header's own C spellings. Exhaustive on purpose: an unrecognized type is
# a hard error, never a silent pass, because a new type in the ABI is exactly
# when this check must speak up.
C_TYPES: dict[str, str] = {
    "void": "void",
    "int": "int",
    "unsigned": "uint",
    "unsigned int": "uint",
    "size_t": "size",
    "const char *": "cstr",
    "char *": "charp",
    "char **": "charpp",
    "int *": "intp",
    "chs_schema *": "handle",
    "const chs_schema *": "handle",
    "chs_filter *": "handle",
    "const chs_filter *": "handle",
    "chs_block *": "handle",
    "const chs_block *": "handle",
    "chs_bytes *": "bytesp",
    # Go's dlopen typedefs never include the header, so they spell every handle
    # `void *` and the bytes struct with their own name. Same classes.
    "void *": "handle",
    "const void *": "handle",
    "chs_lib_bytes *": "bytesp",
}

PY_TOKENS: dict[str, frozenset[str]] = {
    "None": frozenset({"void"}),
    "ctypes.c_int": frozenset({"int"}),
    "ctypes.c_uint": frozenset({"uint"}),
    "ctypes.c_size_t": frozenset({"size"}),
    "ctypes.c_char_p": frozenset({"cstr"}),
    # Owned `char *` and every handle are both c_void_p — see the limits above.
    "ctypes.c_void_p": frozenset({"charp", "handle"}),
    "ctypes.POINTER(ctypes.c_void_p)": frozenset({"charpp"}),
    "ctypes.POINTER(ctypes.c_int)": frozenset({"intp"}),
    "ctypes.POINTER(_ChsBytes)": frozenset({"bytesp"}),
}

TS_TOKENS: dict[str, frozenset[str]] = {
    "Void": frozenset({"void"}),
    "I32": frozenset({"int", "uint"}),
    "U64": frozenset({"size"}),
    "Str": frozenset({"cstr"}),
    # A caller-provided buffer: the row body (`const char *`) and the chs_bytes
    # out-param scratch are both spelled this way.
    "U8Array": frozenset({"cstr", "bytesp"}),
    # A raw pointer with no target type.
    "External": frozenset({"charp", "charpp", "intp", "handle", "bytesp"}),
}

RUST_TOKENS: dict[str, frozenset[str]] = {
    "": frozenset({"void"}),  # no `-> T` at all
    "()": frozenset({"void"}),
    "c_int": frozenset({"int"}),
    "std::ffi::c_int": frozenset({"int"}),
    "c_uint": frozenset({"uint"}),
    "std::ffi::c_uint": frozenset({"uint"}),
    "usize": frozenset({"size"}),
    "*const c_char": frozenset({"cstr"}),
    "*mut c_char": frozenset({"charp"}),
    "*mut *mut c_char": frozenset({"charpp"}),
    "*mut c_int": frozenset({"intp"}),
    "*mut ChsBytes": frozenset({"bytesp"}),
    "*const ChsSchema": frozenset({"handle"}),
    "*mut ChsSchema": frozenset({"handle"}),
    "*const ChsFilter": frozenset({"handle"}),
    "*mut ChsFilter": frozenset({"handle"}),
    "*const ChsBlock": frozenset({"handle"}),
    "*mut ChsBlock": frozenset({"handle"}),
}

def _check_tables() -> None:
    """Every class named in the tables above must be a real class.

    A typo would otherwise make a rule silently unsatisfiable — the shape of
    failure this whole script exists to refuse.
    """
    known = set(CLASSES)
    for table_name, table in (("C_TYPES", C_TYPES), ("PY", PY_TOKENS), ("TS", TS_TOKENS), ("RUST", RUST_TOKENS)):
        for token, value in table.items():
            got = {value} if isinstance(value, str) else set(value)
            if not got <= known:
                raise SystemExit(f"check-abi-decls: {table_name}[{token!r}] names unknown class(es) {got - known}")
        if table_name != "C_TYPES" and any(not v for v in table.values()):
            raise SystemExit(f"check-abi-decls: {table_name} has a token that accepts nothing")


_check_tables()

# The declaration sources, in report order. --selftest copies exactly these
# (plus the header) into a scratch tree and mutates them there.
SOURCES = (
    ("go", "go/chtypes/multiversion.go"),
    ("go-linked", "go/chtypes/linked.go"),
    ("python", "python/src/chtypes/_native.py"),
    ("ts", "ts/src/ffi.ts"),
    ("rust", "rust/src/ffi.rs"),
)
HEADER = "include/chtypes.h"


class ParseError(Exception):
    """A source could not be read the way this checker must read it.

    Always fatal. A checker that cannot parse a binding must say so, never
    report success for something it did not examine.
    """


# ------------------------------------------------------------------- the header


def strip_c_comments(text: str) -> str:
    return re.sub(r"/\*.*?\*/", " ", text, flags=re.S)


def norm_c(t: str) -> str:
    """Normalize a C type spelling: collapse whitespace, pull `*` together."""
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"\s*\*\s*", "*", t)
    t = re.sub(r"\*+", lambda m: " " + m.group(0), t)
    return t.strip()


def c_class(spelling: str, where: str) -> str:
    key = norm_c(spelling)
    if key not in C_TYPES:
        raise ParseError(f"{where}: unrecognized C type {key!r} — teach C_TYPES or fix the source")
    return C_TYPES[key]


def split_params(text: str) -> list[str]:
    """Split a parameter list on top-level commas."""
    out, depth, cur = [], 0, ""
    for ch in text:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur)
    return [p.strip() for p in out if p.strip()]


def param_type(decl: str) -> str:
    """Drop a C parameter's NAME, keeping its type.

    `const char * timezone` -> `const char *`; `int i` -> `int`; `char ** e` ->
    `char **`. A trailing bare identifier that is not itself a type keyword is
    the name.
    """
    d = norm_c(decl)
    if d in C_TYPES:
        return d
    m = re.match(r"^(.*?)\s*([A-Za-z_]\w*)$", d)
    if m and m.group(1):
        return norm_c(m.group(1))
    return d


def parse_header(root: str) -> dict[str, tuple[str, list[str]]]:
    """`chs_*` -> (return class, [param classes]) straight off include/chtypes.h."""
    path = os.path.join(root, HEADER)
    text = strip_c_comments(open(path, encoding="utf-8").read())
    # The preprocessor lines, including `#define CHS_API ...` itself: the
    # marker is what anchors a prototype, so its own definition must not be one.
    text = re.sub(r"^\s*#.*$", "", text, flags=re.M)
    protos: dict[str, tuple[str, list[str]]] = {}
    # `[^;{}]` keeps a return type inside its own declaration: without it the
    # lazy match happily spans an enum body to reach the next prototype.
    for m in re.finditer(r"CHS_API\s+([^;{}]+?)\b(chs_\w+)\s*\(([^;{}]*?)\)\s*;", text, flags=re.S):
        ret, name, params = m.group(1), m.group(2), m.group(3)
        where = f"{HEADER}:{name}"
        if "(" in params or "..." in params:
            raise ParseError(f"{where}: function-pointer or variadic parameter — refusing to guess")
        rcls = c_class(ret, where + " return")
        plist = split_params(params)
        pcls = [] if plist == ["void"] else [c_class(param_type(p), where) for p in plist]
        if name in protos:
            raise ParseError(f"{where}: declared twice in the header")
        protos[name] = (rcls, pcls)
    if not protos:
        raise ParseError(f"{HEADER}: no CHS_API prototypes found")
    return protos


# ----------------------------------------------------------------------- go


def parse_go(root: str) -> dict[str, tuple[frozenset[str], list[frozenset[str]]]]:
    """The cgo preamble's `fn_*` typedefs, paired to symbols by their own dlsym casts.

    The pairing is read from `(fn_x) dlsym(h, "chs_y")` rather than from the
    struct field names: that cast is the thing the running program does, so a
    typedef reused for two symbols (fn_col_str, fn_owned_str0) lands on both.
    """
    path = os.path.join(root, SOURCES[0][1])
    text = open(path, encoding="utf-8").read()
    pre = text.split("*/", 1)[0]  # the cgo preamble, up to its closing delimiter
    pre = re.sub(r"//[^\n]*", " ", pre)
    pre = re.sub(r"^\s*#.*$", "", pre, flags=re.M)

    typedefs: dict[str, tuple[str, list[str]]] = {}
    # `[^;{}]` again: the struct typedef for chs_lib_bytes sits between these.
    for m in re.finditer(r"typedef\s+([^;{}]+?)\(\s*\*\s*(fn_\w+)\s*\)\s*\(([^)]*)\)\s*;", pre, flags=re.S):
        ret, alias, params = m.group(1), m.group(2), m.group(3)
        where = f"{SOURCES[0][1]}:{alias}"
        plist = split_params(params)
        rcls = c_class(ret, where + " return")
        pcls = [] if plist in ([], ["void"]) else [c_class(param_type(p), where) for p in plist]
        typedefs[alias] = (rcls, pcls)

    out: dict[str, tuple[frozenset[str], list[frozenset[str]]]] = {}
    for m in re.finditer(r"\(\s*(fn_\w+)\s*\)\s*dlsym\(\s*h\s*,\s*\"(chs_\w+)\"\s*\)", pre):
        alias, sym = m.group(1), m.group(2)
        if alias not in typedefs:
            raise ParseError(f"{SOURCES[0][1]}: {sym} is cast to {alias}, which has no typedef")
        rcls, pcls = typedefs[alias]
        out[sym] = (frozenset({rcls}), [frozenset({c}) for c in pcls])
    return out


def parse_go_linked(root: str, protos: dict[str, tuple[str, list[str]]]) -> dict[str, tuple[None, int]]:
    """Arity of every `C.chs_*(...)` call site in the linked build.

    ARITY ONLY — see the limits at the top of this file.
    """
    rel = SOURCES[1][1]
    text = open(os.path.join(root, rel), encoding="utf-8").read()
    text = re.sub(r"//[^\n]*", " ", text)
    out: dict[str, tuple[None, int]] = {}
    for m in re.finditer(r"\bC\.(chs_\w+)\s*\(", text):
        name, i = m.group(1), m.end() - 1
        if name not in protos:
            raise ParseError(f"{rel}: calls C.{name}, which the header does not declare")
        depth, j = 0, i
        while j < len(text):
            if text[j] == "(":
                depth += 1
            elif text[j] == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        else:
            raise ParseError(f"{rel}: unterminated call to C.{name}")
        n = len(split_params(text[i + 1 : j]))
        if name in out and out[name][1] != n:
            raise ParseError(f"{rel}: C.{name} is called with {out[name][1]} and {n} arguments")
        out[name] = (None, n)
    return out


# ------------------------------------------------------------------- python


def parse_python(root: str) -> dict[str, tuple[frozenset[str], list[frozenset[str]]]]:
    """`_SIGNATURES`, read as an AST rather than as text."""
    rel = SOURCES[2][1]
    tree = ast.parse(open(os.path.join(root, rel), encoding="utf-8").read())

    # Module-level aliases (_c_int_p, _c_owned_p) resolved from their own
    # assignments, so renaming one does not quietly disarm this.
    tokens = dict(PY_TOKENS)
    for node in tree.body:
        target, value = None, None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target, value = node.target.id, node.value
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target, value = node.targets[0].id, node.value
        if target and value is not None:
            src = ast.unparse(value)
            if src in tokens:
                tokens[target] = tokens[src]

    sigs = None
    for node in tree.body:
        name = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name = node.target.id
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
        if name == "_SIGNATURES":
            sigs = node.value
    if not isinstance(sigs, ast.Dict):
        raise ParseError(f"{rel}: _SIGNATURES is not a dict literal")

    def cls(node: ast.expr, where: str) -> frozenset[str]:
        src = ast.unparse(node)
        if src not in tokens:
            raise ParseError(f"{rel}:{where}: unrecognized ctypes token {src!r}")
        return tokens[src]

    out: dict[str, tuple[frozenset[str], list[frozenset[str]]]] = {}
    for k, v in zip(sigs.keys, sigs.values):
        if not isinstance(k, ast.Constant) or not isinstance(k.value, str):
            raise ParseError(f"{rel}: a _SIGNATURES key is not a string literal")
        sym = k.value
        if not isinstance(v, ast.Tuple) or len(v.elts) != 2 or not isinstance(v.elts[1], (ast.List, ast.Tuple)):
            raise ParseError(f"{rel}:{sym}: value is not (restype, [argtypes])")
        out[sym] = (cls(v.elts[0], sym), [cls(e, sym) for e in v.elts[1].elts])
    return out


# ----------------------------------------------------------------------- ts


def parse_ts(root: str) -> dict[str, tuple[frozenset[str], list[frozenset[str]]]]:
    """The ffi-rs descriptor table: `chs_x: d(Ret, [P, ...])`."""
    rel = SOURCES[3][1]
    text = open(os.path.join(root, rel), encoding="utf-8").read()
    text = re.sub(r"//[^\n]*", " ", text)
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)

    def cls(tok: str, where: str) -> frozenset[str]:
        tok = tok.strip()
        if tok not in TS_TOKENS:
            raise ParseError(f"{rel}:{where}: unrecognized ffi-rs DataType {tok!r}")
        return TS_TOKENS[tok]

    out: dict[str, tuple[frozenset[str], list[frozenset[str]]]] = {}
    for m in re.finditer(r"\b(chs_\w+)\s*:\s*d\(\s*(\w+)\s*,\s*\[([^\]]*)\]\s*\)", text):
        sym, ret, params = m.group(1), m.group(2), m.group(3)
        plist = [p for p in (x.strip() for x in params.split(",")) if p]
        out[sym] = (cls(ret, sym), [cls(p, sym) for p in plist])
    return out


# --------------------------------------------------------------------- rust


def parse_rust(root: str) -> dict[str, tuple[frozenset[str], list[frozenset[str]]]]:
    """The `extern "C"` fn type aliases, paired to symbols at dlsym time.

    The pairing runs through the struct field that holds each `Symbol<FnX>`,
    because that is how the crate itself binds a name to a signature:
    `f_rows: Symbol<FnRows>` in the struct, `f_rows: optional(&lib, b"chs_rows\\0")`
    at open. An explicit turbofish (`optional::<FnAbiRevision>(...)`) wins when
    present.
    """
    rel = SOURCES[4][1]
    text = open(os.path.join(root, rel), encoding="utf-8").read()
    text = re.sub(r"//[^\n]*", " ", text)

    def cls(tok: str, where: str) -> frozenset[str]:
        tok = re.sub(r"\s+", " ", tok).strip()
        tok = re.sub(r"\*\s+", "*", tok)
        if tok not in RUST_TOKENS:
            raise ParseError(f"{rel}:{where}: unrecognized Rust FFI type {tok!r}")
        return RUST_TOKENS[tok]

    # type FnX = unsafe extern "C" fn(A, B) -> R;
    aliases: dict[str, tuple[frozenset[str], list[frozenset[str]]]] = {}
    for m in re.finditer(r"type\s+(\w+)\s*=\s*unsafe\s+extern\s+\"C\"\s+fn\s*\(", text):
        alias, i = m.group(1), m.end() - 1
        depth, j = 0, i
        while j < len(text):
            if text[j] == "(":
                depth += 1
            elif text[j] == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        else:
            raise ParseError(f"{rel}: unterminated parameter list for {alias}")
        params = split_params(text[i + 1 : j])
        tail = text[j + 1 :].split(";", 1)[0].strip()
        ret = tail[2:].strip() if tail.startswith("->") else ""
        aliases[alias] = (cls(ret, alias), [cls(p, alias) for p in params])

    # Every `field: Symbol<FnX>` / `field: Option<Symbol<FnX>>`, any struct.
    fields = {m.group(1): m.group(2) for m in re.finditer(r"(\w+)\s*:\s*(?:Option<)?Symbol<(\w+)>", text)}

    out: dict[str, tuple[frozenset[str], list[frozenset[str]]]] = {}
    pat = r"(?:let\s+(\w+)\s*=\s*|(\w+)\s*:\s*)(?:require|optional)(?:::<(\w+)>)?\(\s*&lib\s*,\s*b\"(chs_\w+)\\0\""
    for m in re.finditer(pat, text):
        binder = m.group(1) or m.group(2)
        alias = m.group(3) or fields.get(binder)
        sym = m.group(4)
        if alias is None:
            raise ParseError(f"{rel}: cannot tell which signature {sym} is loaded with (binder {binder!r})")
        if alias not in aliases:
            raise ParseError(f"{rel}: {sym} is loaded as {alias}, which has no `extern \"C\"` type alias")
        out[sym] = aliases[alias]
    return out


# ------------------------------------------------------------------ comparing


def fmt(classes: list[str] | list[frozenset[str]] | None) -> str:
    if classes is None:
        return "?"
    parts = []
    for c in classes:
        parts.append(c if isinstance(c, str) else "|".join(sorted(c)))
    return "(" + ", ".join(parts) + ")"


def check(root: str, verbose: bool = False) -> tuple[int, list[str]]:
    """Returns (number of findings, report lines).

    `verbose` prints every binding's own declared classes as well as the
    header's; the default prints them only where they disagree, because the
    header row already carries what a passing binding agreed with.
    """
    protos = parse_header(root)
    declared = {
        "go": parse_go(root),
        "go-linked": parse_go_linked(root, protos),
        "python": parse_python(root),
        "ts": parse_ts(root),
        "rust": parse_rust(root),
    }

    lines: list[str] = []
    findings: list[str] = []

    # A parser that stopped seeing its binding must fail, not report success
    # for something it never examined. `chs_rows` is the anchor: it is one of
    # the mandatory four every artifact exports, every binding declares it, and
    # the linked path calls it.
    for name, decls in declared.items():
        if "chs_rows" not in decls:
            findings.append(
                f"{name}: parsed {len(decls)} declaration(s) and none of them is chs_rows — "
                f"the parser has lost this binding; fix it rather than trusting this run"
            )

    # A binding naming a symbol the header does not have.
    for name, decls in declared.items():
        for sym in sorted(set(decls) - set(protos)):
            findings.append(f"{name}: declares {sym}, which include/chtypes.h does not")

    order = [n for n, _ in SOURCES]
    lines.append(f"{'function / source':<40} {'n':>2}  return + parameter classes")
    lines.append("-" * 118)
    for sym in sorted(protos):
        rcls, pcls = protos[sym]
        lines.append(f"{sym:<40} {len(pcls):>2}  -> {rcls}  {fmt(pcls)}")
        for name in order:
            entry = declared[name].get(sym)
            label = f"  {name}"
            if entry is None:
                lines.append(f"{label:<40} {'—':>2}  not declared")
                continue
            dret, dparams = entry
            if name == "go-linked":
                n = dparams  # an int: this source is checked for ARITY only
                verdict = "ok (arity here; types by scripts/check-linked-build.sh)"
                if n != len(pcls):
                    verdict = f"ARITY: {n} argument(s) at the call site, the header takes {len(pcls)}"
                    findings.append(f"{name}: C.{sym} is called with {n} argument(s); the header takes {len(pcls)}")
                lines.append(f"{label:<40} {n:>2}  {verdict}")
                continue
            bad = []
            if len(dparams) != len(pcls):
                bad.append(f"ARITY: declares {len(dparams)}, the header takes {len(pcls)}")
                findings.append(
                    f"{name}: {sym} declares {len(dparams)} parameter(s); the header takes {len(pcls)}"
                )
            if rcls not in dret:
                bad.append(f"RETURN: declares {'|'.join(sorted(dret))}, the header returns {rcls}")
                findings.append(
                    f"{name}: {sym} return declared {'|'.join(sorted(dret))}; the header returns {rcls}"
                )
            for i, (want, got) in enumerate(zip(pcls, dparams), start=1):
                if want not in got:
                    bad.append(f"PARAM {i}: declares {'|'.join(sorted(got))}, the header says {want}")
                    findings.append(
                        f"{name}: {sym} parameter {i} declared {'|'.join(sorted(got))}; "
                        f"the header says {want}"
                    )
            shown = f"-> {'|'.join(sorted(dret))}  {fmt(dparams)}" if (verbose or bad) else ""
            lines.append(f"{label:<40} {len(dparams):>2}  {shown or 'ok'}")
            if bad:
                lines.append(f"{'':<40} {'':>2}  {'; '.join(bad)}")
        lines.append("")

    counts = ", ".join(f"{n} {len(declared[n])}" for n in order)
    lines.append(f"header: {len(protos)} chs_* prototypes; declarations parsed — {counts}")
    return len(findings), lines + ([""] + [f"  {f}" for f in findings] if findings else [])


# -------------------------------------------------------------------- selftest

# Each plant is (source label, file, find, replace, the substring the finding
# must contain). Every binding gets a wrong ARITY and a wrong TYPE, because a
# rule that has only ever passed is not known to work — and because arity alone
# would have caught the Go defect and would miss its sibling.
PLANTS = (
    (
        "go arity",
        "go/chtypes/multiversion.go",
        "typedef char *       (*fn_row)(const void *, int, const char *, size_t, const char *, const char *);",
        "typedef char *       (*fn_row)(const void *, int, const char *, size_t, const char *);",
        "go: chs_row declares 5 parameter(s); the header takes 6",
    ),
    (
        "go type",
        "go/chtypes/multiversion.go",
        "typedef char *       (*fn_row)(const void *, int, const char *, size_t, const char *, const char *);",
        "typedef char *       (*fn_row)(const void *, unsigned, const char *, size_t, const char *, const char *);",
        "go: chs_row parameter 2 declared uint; the header says int",
    ),
    (
        "go-linked arity",
        "go/chtypes/linked.go",
        "out := C.chs_row(cs.handle, C.int(format), praw, C.size_t(len(raw)), csj, pcols)",
        "out := C.chs_row(cs.handle, C.int(format), praw, C.size_t(len(raw)), csj)",
        "go-linked: C.chs_row is called with 5 argument(s); the header takes 6",
    ),
    (
        "python arity",
        "python/src/chtypes/_native.py",
        '"chs_schema_ttl": (ctypes.c_int, [ctypes.c_void_p, ctypes.c_char_p, _c_owned_p]),',
        '"chs_schema_ttl": (ctypes.c_int, [ctypes.c_void_p, ctypes.c_char_p]),',
        "python: chs_schema_ttl declares 2 parameter(s); the header takes 3",
    ),
    (
        "python type",
        "python/src/chtypes/_native.py",
        '"chs_schema_ttl": (ctypes.c_int, [ctypes.c_void_p, ctypes.c_char_p, _c_owned_p]),',
        '"chs_schema_ttl": (ctypes.c_int, [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]),',
        "python: chs_schema_ttl parameter 3 declared cstr; the header says charpp",
    ),
    (
        "ts arity",
        "ts/src/ffi.ts",
        "chs_schema_ttl: d(I32, [External, Str, External]),",
        "chs_schema_ttl: d(I32, [External, Str]),",
        "ts: chs_schema_ttl declares 2 parameter(s); the header takes 3",
    ),
    (
        "ts type",
        "ts/src/ffi.ts",
        "chs_schema_ttl: d(I32, [External, Str, External]),",
        "chs_schema_ttl: d(I32, [External, Str, Str]),",
        "ts: chs_schema_ttl parameter 3 declared cstr; the header says charpp",
    ),
    (
        "rust arity",
        "rust/src/ffi.rs",
        "type FnTtl = unsafe extern \"C\" fn(*mut ChsSchema, *const c_char, *mut *mut c_char) -> c_int;",
        "type FnTtl = unsafe extern \"C\" fn(*mut ChsSchema, *const c_char) -> c_int;",
        "rust: chs_schema_ttl declares 2 parameter(s); the header takes 3",
    ),
    # The revision-5 quoting trio, symbol by symbol: the table above shows the
    # three are DECLARED everywhere, and these show the checker actually fires
    # on them — separately, because a symbol reported as `—` would have looked
    # exactly as calm as one that agrees.
    (
        "go quote arity",
        "go/chtypes/multiversion.go",
        "typedef int          (*fn_quote)(const char *, size_t, char **, char **);",
        "typedef int          (*fn_quote)(const char *, size_t, char **);",
        "go: chs_quote_literal declares 3 parameter(s); the header takes 4",
    ),
    (
        "python quote type",
        "python/src/chtypes/_native.py",
        "        [ctypes.c_char_p, ctypes.c_size_t, _c_owned_p, _c_owned_p],\n    ),\n    \"chs_registered_families\"",
        "        [ctypes.c_char_p, ctypes.c_int, _c_owned_p, _c_owned_p],\n    ),\n    \"chs_registered_families\"",
        "python: chs_quote_literal parameter 2 declared int; the header says size",
    ),
    (
        "ts quote arity",
        "ts/src/ffi.ts",
        "chs_quote_literal: d(I32, [U8Array, U64, External, External]),",
        "chs_quote_literal: d(I32, [U8Array, U64, External]),",
        "ts: chs_quote_literal declares 3 parameter(s); the header takes 4",
    ),
    (
        "rust quote type",
        "rust/src/ffi.rs",
        "    unsafe extern \"C\" fn(*const c_char, usize, *mut *mut c_char, *mut *mut c_char) -> c_int;",
        "    unsafe extern \"C\" fn(*const c_char, c_int, *mut *mut c_char, *mut *mut c_char) -> c_int;",
        "rust: chs_quote_identifier parameter 2 declared int; the header says size",
    ),
    (
        "rust type",
        "rust/src/ffi.rs",
        "type FnTtl = unsafe extern \"C\" fn(*mut ChsSchema, *const c_char, *mut *mut c_char) -> c_int;",
        "type FnTtl = unsafe extern \"C\" fn(*mut ChsSchema, *const c_char, *mut c_char) -> c_int;",
        "rust: chs_schema_ttl parameter 3 declared charp; the header says charpp",
    ),
)


def selftest(root: str) -> int:
    n, _ = check(root)
    if n:
        print("SELFTEST FAILED: the tree does not pass, so a planted failure proves nothing", file=sys.stderr)
        return 1

    tmp = tempfile.mkdtemp(prefix="abi-decls-selftest-")
    try:
        for rel in [HEADER] + [f for _, f in SOURCES]:
            dst = os.path.join(tmp, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(os.path.join(root, rel), dst)

        # The copy itself must still pass: a plant that fires against a tree
        # that was already failing proves nothing.
        n, lines = check(tmp)
        if n:
            print("SELFTEST FAILED: the copied tree does not pass\n" + "\n".join(lines), file=sys.stderr)
            return 1

        for label, rel, find, repl, want in PLANTS:
            path = os.path.join(tmp, rel)
            original = open(path, encoding="utf-8").read()
            if original.count(find) != 1:
                print(
                    f"SELFTEST FAILED: the {label} plant's anchor is not in {rel} exactly once "
                    f"(found {original.count(find)}) — the source moved; update PLANTS",
                    file=sys.stderr,
                )
                return 1
            open(path, "w", encoding="utf-8").write(original.replace(find, repl))
            try:
                n, lines = check(tmp)
            finally:
                open(path, "w", encoding="utf-8").write(original)
            report = "\n".join(lines)
            if n == 0:
                print(f"SELFTEST FAILED: the {label} plant was not caught", file=sys.stderr)
                return 1
            if want not in report:
                print(
                    f"SELFTEST FAILED: the {label} plant fired, but not with the expected finding.\n"
                    f"  wanted: {want}\n  got:\n{report}",
                    file=sys.stderr,
                )
                return 1
            print(f"  plant {label:<18} caught: {want}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("check-abi-decls: selftest ok — a wrong arity and a wrong type fire in every binding")
    return 0


def main(argv: list[str]) -> int:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        if "--selftest" in argv:
            return selftest(root)
        n, lines = check(root, verbose="--verbose" in argv)
    except ParseError as e:
        print(f"check-abi-decls: {e}", file=sys.stderr)
        return 2
    print("\n".join(lines))
    if n:
        print(
            f"\ncheck-abi-decls: {n} declaration(s) disagree with include/chtypes.h.\n"
            "  The header is the single source of truth. Fix the declaration, passing NULL\n"
            "  where the binding has no value to supply — never by adding public surface.",
            file=sys.stderr,
        )
        return 1
    print("\ncheck-abi-decls: ok — every binding's chs_* declaration matches the header")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
