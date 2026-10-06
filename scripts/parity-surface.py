#!/usr/bin/env python3
"""parity-surface.py — every binding's public names against docs/reference/bindings-v1.md (#436).

    scripts/parity-surface.py --selftest              offline: the doc parser, the four surface readers and
                                                      the comparator on tests/fixtures/parity-surface/, with
                                                      drift planted in each binding in each direction
    scripts/parity-surface.py --fixtures [BINDING…]   run each pinned tool on the fixture packages; what it
                                                      prints must read exactly as the recorded surface
    scripts/parity-surface.py run [--tools DIR] [--only BINDING…] [--save DIR]
                                                      CI: the fixture proofs, then each binding's real surface
                                                      against the doc
    scripts/parity-surface.py expected [BINDING]      print the names the doc gives, for a reviewer

Run by ci.yml's non-blocking `parity-surface` job. It is the gate public
issue #436 asks for: "No public-API change merges in any binding until this
gate exists." The doc is the single source; there is no second hand-kept
manifest of names. Read CONTRIBUTING.md's "Changing a public name" first.

WHAT IS EXPECTED. The doc's own tables, parsed here: §2's operation tables
and call options (with the TypeScript and Rust option-type paragraph below
them), §3's per-language types, objects and vocabularies, §4's error classes
and fields, and §5's documents. A cell in a language column gives that
language's spelling (`(r *Registry) For(…)`, `Registry.for_version(…)`,
`registry.for(…)`, `Registry::for_version(…)`); §5 gives canonical snake_case
fields, spelled per language by §1 principle 2's mechanical rule, whose list of
Go initialisms is read from the doc too. Python's keyword-only parameters are
names (`Schema.rows(row_filter=)`), because the doc's Python signatures are
where its call options live. Three things the doc delegates are completed from
where it says they live, never from a list kept here:

  - a vocabulary's members are the description's (spec/abi-v1/abi.json): each
    binding's members must number exactly the description's values and agree
    with the other three bindings, compared without case or underscores;
  - "one class per code" (§4) is expanded over the codes the row names, with
    each language's spelling learned from the row above it that names both a
    code and a class;
  - a type a signature names (`FetchOptions`, `Resolved`, `RowResult`) is a name.

WHERE EACH SURFACE COMES FROM, the same tools scripts/api-surface.py pins,
imported from it so a pin cannot differ:

  go      the compiler's export data for go/chtypes, both builds (the default
          one and -tags chtypes_linked), listed by scripts/parity-surface-go.go
          through go/importer: the data apidiff reads, which only diffs.
  python  griffe's static model of python/src/chtypes, with members inherited
          from a private base (Schema's close, from _Handle) on the subclass.
  ts      api-extractor's report on ts/dist/index.d.ts, built from a scratch
          copy of ts/ with its own lockfile (pnpm, then tsc, as api-surface
          builds a base tree).
  rust    cargo-public-api on rust/Cargo.toml, all features.

A grep of source text is never a surface. Each language's own protocol
members are not names: Python's dunder methods (a constructor's keyword-only
parameters excepted), Rust's trait-impl items, TypeScript's constructors and
private or protected members, and Go's methods that implement a standard
interface (GO_PROTOCOL below).

WHAT FAILS, per binding:

  missing      a name the doc gives that the binding lacks;
  spelling     a missing name and an undocumented one that are the same name
               but for case and separators (`Verdict.answered` against
               `verdictAnswered`): reported once, as a different spelling;
  undocumented a public name the doc does not give (principle 2: a
               convenience in one binding exists in all four or in none);
  forbidden    a name the doc says a binding does not have ("no `close`");
  vocabulary   a vocabulary whose members differ from the description's count
               or from the other bindings'.

The allowlist, scripts/parity-surface-allow.json, carries every deliberate
difference, one entry per binding and kind, each with its written reason. An
entry that matches nothing is stale and fails too, so the allowlist cannot
outlive the difference it explains.

SELFTEST FIRST. --selftest reads tests/fixtures/parity-surface/: a miniature
doc in the real doc's table shapes, a miniature description, a fixture
allowlist, and each tool's recorded output for a fixture package per binding.
The green fixtures must pass; then a missing name, a different spelling and an
undocumented name are planted in each binding's recorded output, and each of
the twelve must fail for its own reason. It also parses the real doc and
requires names from every section it reads. --fixtures (and `run`, first)
runs each real tool on the fixture package and requires its output to read
exactly as the recording, so the selftest's inputs are what the tools print.

Exit status: 0 when every binding read and matched; 1 for a finding, a tool
error, a failed proof or a failed selftest; 2 for a usage error.
"""

from __future__ import annotations

import fnmatch
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "reference" / "bindings-v1.md"
DESCRIPTION = ROOT / "spec" / "abi-v1" / "abi.json"
FETCH_DOC = ROOT / "docs" / "guides" / "fetch-v1.md"
ALLOWLIST = ROOT / "scripts" / "parity-surface-allow.json"
FIXTURES = ROOT / "tests" / "fixtures" / "parity-surface"
GO_LISTER = ROOT / "scripts" / "parity-surface-go.go"
GO_PACKAGE = "github.com/wave-rf/chtypes/go/chtypes"


def _load_api_surface():
    """scripts/api-surface.py, imported by path (its name has a hyphen): its
    pins, its tool runners and its tool cache are this gate's too."""
    spec = importlib.util.spec_from_file_location("api_surface", ROOT / "scripts" / "api-surface.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


AS = _load_api_surface()
BINDINGS: tuple[str, ...] = ("go", "python", "ts", "rust")
LANGUAGE_COLUMNS = {"Go": "go", "Python": "python", "TypeScript": "ts", "Rust": "rust"}
ToolError = AS.ToolError

# ------------------------------------------------------------------- names

IDENT = r"[A-Za-z_$][\w$]*"
SPAN = re.compile(r"`([^`]+)`")


def fold(name: str) -> str:
    """A name without case or separators: `Verdict.answered`, `verdictAnswered`
    and `VERDICT_ANSWERED` fold alike, so one spelled differently pairs up."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


# Words a span may hold that name no binding's own API: the language's
# builtins and standard library, its standard traits and base classes.
BUILTINS = {
    "go": {
        "string",
        "bool",
        "error",
        "byte",
        "rune",
        "int",
        "int8",
        "int16",
        "int32",
        "int64",
        "uint",
        "uint8",
        "uint16",
        "uint32",
        "uint64",
        "float32",
        "float64",
        "any",
        "map",
        "func",
        "chan",
        "nil",
        "true",
        "false",
        "context",
        "errors",
        "runtime",
    },
    "python": {
        "bytes",
        "str",
        "int",
        "bool",
        "float",
        "None",
        "True",
        "False",
        "tuple",
        "dict",
        "list",
        "set",
        "frozenset",
        "Optional",
        "Union",
        "Mapping",
        "Sequence",
        "Iterable",
        "Iterator",
        "Any",
        "object",
        "IntEnum",
        "StrEnum",
        "IntFlag",
        "Enum",
        "Exception",
        "Path",
        "os",
        "self",
        "cls",
    },
    "ts": {
        "string",
        "number",
        "boolean",
        "bigint",
        "void",
        "undefined",
        "null",
        "unknown",
        "any",
        "never",
        "object",
        "Buffer",
        "Uint8Array",
        "Promise",
        "Readonly",
        "Record",
        "Array",
        "ReadonlyArray",
        "Error",
        "Symbol",
        "readonly",
        "Map",
        "Set",
        "Partial",
    },
    "rust": {
        "String",
        "str",
        "Vec",
        "Option",
        "Some",
        "None",
        "Arc",
        "Rc",
        "Box",
        "BTreeMap",
        "HashMap",
        "Path",
        "PathBuf",
        "Self",
        "self",
        "bool",
        "u8",
        "u16",
        "u32",
        "u64",
        "usize",
        "i8",
        "i16",
        "i32",
        "i64",
        "isize",
        "f32",
        "f64",
        "AsRef",
        "Default",
        "Drop",
        "Clone",
        "Copy",
        "Send",
        "Sync",
        "Display",
        "Debug",
        "IntoIterator",
        "Iterator",
        "Eq",
        "PartialEq",
        "Hash",
        "Ord",
        "PartialOrd",
        "impl",
        "dyn",
        "mut",
        "static",
    },
}
# Go methods that implement a standard interface (error, fmt.Stringer, the
# errors package's Is/As/Unwrap): Go's equivalent of a dunder or a trait impl.
GO_PROTOCOL = {
    "Error",
    "String",
    "Unwrap",
    "Is",
    "As",
    "GoString",
    "Format",
    "MarshalJSON",
    "UnmarshalJSON",
    "MarshalText",
    "UnmarshalText",
}
# A span that is never a binding's name: a C symbol, a fetch error code (or
# its `_SUFFIX` shorthand), a Rust attribute or lifetime, a flag, a JSON value.
NOT_A_NAME = re.compile(r"^(?:chs_|CHS_|CHTYPES_|_[A-Z]|#\[|'|-|\{|\"|\d)")


def spell(canonical: str, lang: str, initialisms: dict[str, str]) -> str:
    """§1 principle 2's mechanical rule: Go PascalCase with its initialisms,
    TypeScript camelCase, Python and Rust snake_case as written."""
    parts = canonical.split("_")
    if lang == "go":
        return "".join(initialisms.get(p.lower(), p[:1].upper() + p[1:]) for p in parts)
    if lang == "ts":
        return parts[0] + "".join(p[:1].upper() + p[1:] for p in parts[1:])
    return canonical


def split_top(text: str, seps: str = ",") -> list[str]:
    """Split at `seps` outside every bracket pair (`->` and `=>` are not one)."""
    out, depth, cur, i = [], 0, [], 0
    while i < len(text):
        c = text[i]
        if text.startswith(("->", "=>"), i):
            cur.append(text[i : i + 2])
            i += 2
            continue
        if c in "([{<":
            depth += 1
        elif c in ")]}>":
            depth -= 1
        if c in seps and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    out.append("".join(cur))
    return [p for p in (s.strip() for s in out) if p]


def paren_body(rest: str) -> tuple[str, str]:
    """`(a, b) -> T` → (`a, b`, ` -> T`): the first balanced parenthesis."""
    depth = 0
    for i, c in enumerate(rest):
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return rest[1:i], rest[i + 1 :]
    return rest[1:], ""


def python_kwonly(params: str) -> list[str]:
    """The keyword-only parameter names of a Python parameter list: those after
    a bare `*` or a `*args`."""
    out, star = [], False
    for p in split_top(params):
        if p.startswith("**"):
            continue
        if p.startswith("*"):
            star = True
            continue
        m = re.match(IDENT, p)
        if star and m and m.group(0) not in ("self", "cls"):
            out.append(m.group(0))
    return out


# ---------------------------------------------------------------- markdown


@dataclass
class Block:
    kind: str  # "table", "para" or "item"
    h2: str
    h3: str
    line: int
    text: str = ""
    header: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)


ITEM = re.compile(r"\s*(?:[-*]|\d+\.)\s")


def cells(line: str) -> list[str]:
    parts = re.split(r"(?<!\\)\|", line.strip())
    return [p.strip().replace("\\|", "|") for p in parts[1:-1]]


def blocks(text: str) -> list[Block]:
    """The doc's tables, paragraphs and list items, each with the `##` and
    `###` headings it sits under. Fenced code is skipped."""
    lines, out = text.splitlines(), []
    h2 = h3 = ""
    i, fenced = 0, False
    while i < len(lines):
        line = lines[i]
        if line.startswith("```"):
            fenced = not fenced
            i += 1
            continue
        if fenced or not line.strip():
            i += 1
            continue
        if line.startswith("## "):
            h2, h3 = line[3:].strip(), ""
        elif line.startswith("### "):
            h3 = line[4:].strip()
        elif line.startswith("|"):
            start, rows = i, []
            while i < len(lines) and lines[i].startswith("|"):
                rows.append(cells(lines[i]))
                i += 1
            out.append(Block("table", h2, h3, start + 1, header=rows[0], rows=rows[2:]))
            continue
        elif not line.startswith("#"):
            start, buf = i, [line.strip()]
            is_item = bool(ITEM.match(line))
            i += 1
            while (
                i < len(lines)
                and lines[i].strip()
                and not lines[i].startswith(("#", "|", "```"))
                and not ITEM.match(lines[i])
            ):
                buf.append(lines[i].strip())
                i += 1
            out.append(Block("item" if is_item else "para", h2, h3, start + 1, text=" ".join(buf)))
            continue
        i += 1
    return out


def spans_of(text: str) -> list[tuple[str, str, str]]:
    """Each code span with the text before and after it."""
    return [(m.group(1), text[: m.start()], text[m.end() :]) for m in SPAN.finditer(text)]


def outside_parens(text: str) -> str:
    """`text` with every parenthesized run removed (nested ones included)."""
    out, depth = [], 0
    for c in text:
        if c == "(":
            depth += 1
        elif c == ")":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(c)
    return "".join(out)


# --------------------------------------------------------------- the doc


@dataclass
class Vocab:
    label: str
    entries: list[str]  # description enum or constant names
    containers: dict[str, str | None] = field(default_factory=dict)
    listed: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class Expected:
    names: dict[str, dict[str, str]] = field(default_factory=lambda: {b: {} for b in BINDINGS})
    forbidden: dict[str, dict[str, str]] = field(default_factory=lambda: {b: {} for b in BINDINGS})
    kwargs: dict[str, dict[str, str]] = field(default_factory=lambda: {b: {} for b in BINDINGS})
    fields: dict[str, dict[str, str]] = field(default_factory=lambda: {b: {} for b in BINDINGS})
    vocabs: list[Vocab] = field(default_factory=list)
    unread: list[str] = field(default_factory=list)
    initialisms: dict[str, str] = field(default_factory=dict)

    def add(self, lang: str, name: str, where: str) -> None:
        if name and not name.endswith("."):
            self.names[lang].setdefault(name, where)


# The heading of a §2-§4 section whose bare operations belong to one object.
SECTION_OWNERS = {"The library": "Library", "A schema": "Schema", "The fields": "CallError"}


@dataclass
class Ctx:
    lang: str
    where: str
    known: frozenset
    section_owner: str | None = None
    current: str | None = None
    options: bool = False  # the call-options table: a bare name is an option field
    last_type: str | None = None  # the last bare type name, for "`*ArtifactError` with `Code`"


def type_refs(text: str, ctx: Ctx, exp: Expected) -> None:
    """Every type a signature names, as a name the binding must have: an
    identifier that starts upper-case and is not a builtin, not qualified by
    another package (`context.Context`) and not a member (`DocFlags.ALL`)."""
    text = text.replace("...", " ")
    for m in re.finditer(r"(?<![\w.:$])((?:chtypes(?:\.|::))?)([A-Z][\w$]*)", text):
        name = m.group(2)
        if len(name) > 1 and name not in BUILTINS[ctx.lang] and not NOT_A_NAME.match(name):
            exp.add(ctx.lang, name, ctx.where)


GO_RECEIVER = re.compile(rf"^\((?:{IDENT}\s+)?\*?({IDENT})\)\s+({IDENT})\s*(\(.*)$", re.S)
HEAD = re.compile(rf"^((?:{IDENT}(?:\.|::))*)({IDENT}|\[Symbol\.{IDENT}\])(.*)$", re.S)
BRACE_LIST = re.compile(rf"^((?:{IDENT}::)+)\{{(.*)\}}$", re.S)


def read_span(span: str, ctx: Ctx, exp: Expected, *, forbid: bool = False) -> None:
    """One code span of a language column: the names it gives, added to `exp`
    (or forbidden), with ctx.current updated for the spans after it."""
    lang = ctx.lang
    target = exp.forbidden if forbid else exp.names

    def add(name: str) -> None:
        if forbid:
            target[lang].setdefault(name, ctx.where)
        else:
            exp.add(lang, name, ctx.where)

    s = span.strip()
    if s.startswith("await "):
        s = s[6:].strip()
    if s == "Symbol.dispose":
        s = "[Symbol.dispose]"
    if not s or NOT_A_NAME.match(s):
        return
    m = GO_RECEIVER.match(s)
    if lang == "go" and m:
        owner, name, rest = m.groups()
        add(owner)
        add(f"{owner}.{name}")
        ctx.current = owner
        type_refs(rest, ctx, exp)
        return
    if lang == "go" and s.startswith("*"):
        s = s[1:]
    if s.startswith("."):  # `.version`: a member of the section's object
        m = HEAD.match(s[1:])
        owner = ctx.section_owner or ctx.current
        if m and owner and not m.group(1):
            add(f"{owner}.{m.group(2)}")
            type_refs(m.group(3), ctx, exp)
        else:
            exp.unread.append(f"{ctx.where} [{lang}] {span}")
        return
    m = BRACE_LIST.match(s)  # `chtypes::status::{OK, …}`
    if m:
        parts = [p for p in m.group(1).split("::") if p and p != "chtypes"]
        if parts:
            container = ".".join(parts)
            add(container)
            for part in split_top(m.group(2)):
                mm = re.match(IDENT, part)
                if mm:
                    add(f"{container}.{mm.group(0)}")
        return
    m = HEAD.match(s)
    if not m:
        if not re.match(r"^[\[&*(<]", s):
            exp.unread.append(f"{ctx.where} [{lang}] {span}")
        return
    qual, name, rest = m.groups()
    parts = [p for p in re.split(r"\.|::", qual) if p]
    explicit_top = False
    if parts and parts[0] == "chtypes":
        parts, explicit_top = parts[1:], True
    is_call = rest.startswith("(")
    is_prop = rest.startswith(":") and not rest.startswith("::")
    owner = None
    if parts:
        q = parts[-1]
        if q in BUILTINS[lang]:  # errors.Is(err, ErrX): only what it names
            type_refs(rest, ctx, exp)
            return
        if q[0].islower():
            cap = q[0].upper() + q[1:]
            if cap in ctx.known:
                owner = cap  # registry.for(…): a Registry member
            elif lang == "rust":
                owner = q  # reason::OVERFLOW_WRAP: a module
            else:
                owner = ctx.section_owner  # l.Version: an example variable
            if owner is None:
                exp.unread.append(f"{ctx.where} [{lang}] {span}")
                return
        else:
            owner = ".".join(parts)
            if owner not in BUILTINS[lang]:
                exp.add(lang, owner, ctx.where)
    elif not explicit_top and (is_call or is_prop):
        owner = ctx.current or ctx.section_owner
    if ctx.options and owner is None and (is_prop or not rest.strip()) and name[0].islower():
        exp.fields[lang].setdefault(name, ctx.where)  # an option field: `rowFilter`, `filter: Option<Filter>`
        type_refs(rest, ctx, exp)
        return
    struct = re.match(r"\s*(?:extends\s+([\w.]+)\s*)?\{(.*)\}\s*$", rest, re.S)
    if struct:  # `SetupOptions { timezone?, defaults? }`
        typename = f"{owner}.{name}" if owner else name
        add(typename)
        if struct.group(1):
            add(struct.group(1))
        for part in split_top(struct.group(2), ",;"):
            if part.startswith(".."):
                continue
            mm = re.match(IDENT, part)
            if mm:
                add(f"{typename}.{mm.group(0)}")
                type_refs(part[mm.end() :], ctx, exp)
        ctx.current = None
        return
    if owner is None and name in BUILTINS[lang]:
        return
    full = f"{owner}.{name}" if owner else name
    if is_call:
        add(full)
        params, after = paren_body(rest)
        if lang == "python":
            ctor = owner is None and name[:1].isupper()
            for kw in python_kwonly(params):
                add(f"{full}({kw}=)")
            if ctor:
                ctx.current = None
        type_refs(params + " " + after, ctx, exp)
        if owner:
            ctx.current = owner
        return
    if is_prop:
        add(full)
        type_refs(rest[1:], ctx, exp)
        if owner:
            ctx.current = owner
        return
    if lang == "python" and rest.strip() == "=":
        exp.kwargs[lang].setdefault(name, ctx.where)  # `settings=`
        return
    if re.match(r"\s*=", rest):  # an alias: `BytesIn = Union[bytes, str]`
        add(full)
        return
    if not rest.strip():
        if owner:
            add(full)
        elif name[0].isupper():
            if len(name) > 1:
                add(name)
                ctx.last_type = name
        elif ctx.current or ctx.section_owner:
            add(f"{ctx.current or ctx.section_owner}.{name}")
        else:
            exp.unread.append(f"{ctx.where} [{lang}] {span}")
        return
    if not rest.lstrip().startswith(("<", "[")):
        exp.unread.append(f"{ctx.where} [{lang}] {span}")


def read_cell(cell: str, ctx: Ctx, exp: Expected) -> None:
    """Every span of one language cell, in order. A span after "no " is a name
    the binding must NOT have; one after "as " refers to another row; one
    followed by "'s" is a possessive in prose."""
    for span, before, after in spans_of(cell):
        if re.search(r"(?:^|\W)as\s*$", before) or after.startswith("'s"):
            continue
        if re.search(r"(?:^|\W)no\s*$", before):
            read_span(span, ctx, exp, forbid=True)
            continue
        holder = ctx.last_type or ctx.current
        if re.search(r"(?:^|\W)with(?: that| its| the)?\s*$", before) and holder and re.fullmatch(IDENT, span):
            exp.add(ctx.lang, f"{holder}.{span}", ctx.where)  # `*ArtifactError` with `Code`
            continue
        read_span(span, ctx, exp)


def language_columns(header: list[str]) -> dict[int, str]:
    return {i: LANGUAGE_COLUMNS[h] for i, h in enumerate(header) if h in LANGUAGE_COLUMNS}


def first_span(text: str) -> str | None:
    m = SPAN.search(text)
    return m.group(1) if m else None


def expected_surface() -> Expected:
    """The real doc, with the record §6 delegates (`Resolved` "is the fetch
    layer's record, re-exported unchanged") read from fetch-v1.md §9."""
    return parse_doc(DOC.read_text(encoding="utf-8"), [FETCH_DOC.read_text(encoding="utf-8")])


RECORD = re.compile(r"^A `(\w+)` carries: (.*)$")


def parse_doc(text: str, delegated: list[str] | None = None, label: str = "bindings-v1.md") -> Expected:
    """Every name `text` gives, per binding. `delegated` docs are read only for
    "A `T` carries: `f` …" paragraphs about a type `text` names."""
    exp = Expected()
    bl = blocks(text)
    extra = [b for t in delegated or [] for b in blocks(t) if b.kind == "para" and RECORD.match(b.text)]
    # §1 principle 2: Go's initialisms, read from the doc.
    for b in bl:
        if "initialisms" in b.text:
            tail = b.text.split("initialisms", 1)[1].split(")", 1)[0]
            exp.initialisms = {s.lower(): s for s, _, _ in spans_of(tail)}
            break
    # §3's objects: the types whose lower-case spelling is a variable (`registry.for`).
    known: set[str] = set()
    for b in bl:
        if b.kind == "table" and "C handle" in b.header:
            known.update(s for row in b.rows if (s := first_span(row[0])))
    known_f = frozenset(known)

    learned: dict[str, str] = {}  # per language, a class name template learned from a code row
    per_code_rows: list[tuple[str, list[str], dict[str, str]]] = []
    for b in bl:
        where = f"{label} line {b.line}"
        if b.kind == "table":
            first = b.header[0] if b.header else ""
            cols = language_columns(b.header)
            if first == "operation" or first == "option" or first == "abstract type":
                for row in b.rows:
                    for i, lang in cols.items():
                        ctx = Ctx(lang, where, known_f, SECTION_OWNERS.get(b.h3), options=(first == "option"))
                        if first == "abstract type":
                            for span, _, _ in spans_of(row[i]):
                                m = re.match(rf"^({IDENT})(\s*=.*)?$", span)
                                if (
                                    m
                                    and m.group(1)[0].isupper()
                                    and len(m.group(1)) > 1
                                    and m.group(1) not in BUILTINS[lang]
                                ):
                                    exp.add(lang, m.group(1), where)
                            continue
                        read_cell(row[i], ctx, exp)
            elif "C handle" in b.header:
                for row in b.rows:
                    obj = first_span(row[0])
                    for i, lang in cols.items():
                        ctx = Ctx(lang, where, known_f, obj)
                        for k, (span, before, _after) in enumerate(spans_of(row[i])):
                            if re.search(r"(?:^|\W)as\s*$", before) or "<" in span or NOT_A_NAME.match(span):
                                continue
                            forbid = bool(re.search(r"(?:^|\W)no\s*$", before))
                            s = span.lstrip("*")
                            if k == 0 and not forbid and s == obj:
                                exp.add(lang, obj, where)
                            elif s not in BUILTINS[lang] and s != obj:
                                name = s.split("(")[0]
                                if name == "Symbol.dispose":
                                    name = "[Symbol.dispose]"
                                if forbid:
                                    exp.forbidden[lang].setdefault(f"{obj}.{name}", where)
                                else:
                                    exp.add(lang, f"{obj}.{name}", where)
            elif first == "vocabulary":
                parse_vocab_table(b, where, exp)
            elif first == "error":
                for row in b.rows:
                    per_code = any("per code" in row[i] for i in cols)
                    names: dict[str, list[str]] = {}
                    for i, lang in cols.items():
                        before_names = set(exp.names[lang])
                        read_cell(row[i], Ctx(lang, where, known_f), exp)
                        names[lang] = [n for n in exp.names[lang] if n not in before_names]
                    codes = re.findall(r"`(CHTYPES_[A-Z_]+)`", " ".join(row))
                    if per_code:
                        per_code_rows.append((where, expand_codes(row[1]), {lang: row[i] for i, lang in cols.items()}))
                    elif codes:
                        pascal = code_pascal(codes[0])
                        for lang, got in names.items():
                            for n in got:
                                if pascal in n and lang not in learned:
                                    learned[lang] = n.replace(pascal, "{}")
            elif first == "field" and cols:  # §4's fields: one per call error
                owner = SECTION_OWNERS.get(b.h3)
                for row in b.rows:
                    for i, lang in cols.items():
                        s = first_span(row[i])
                        m = re.match(IDENT, s or "")
                        if owner and m:
                            exp.add(lang, owner, where)
                            exp.add(lang, f"{owner}.{m.group(0)}", where)
                            type_refs(s[m.end() :], Ctx(lang, where, known_f), exp)
            elif b.h2.startswith("5.") and first in ("field", "type"):
                parse_document_table(b, where, exp)
        elif b.h2.startswith("2.") and "TypeScript option types" in b.text:
            ts_part, _, rust_part = b.text.partition("The Rust ones are")
            for lang, part in (("ts", ts_part), ("rust", rust_part)):
                for span, _, _ in spans_of(part):
                    if "{" in span:
                        read_span(span, Ctx(lang, where, known_f), exp)
        elif b.h2.startswith("4.") and b.kind == "item":
            parse_error_item(b, where, exp, known_f)
        elif b.h2.startswith("5."):
            parse_document_prose(b, where, exp)
    for b in [b for b in bl if b.kind == "para" and RECORD.match(b.text)] + extra:
        m = RECORD.match(b.text)
        if all(m.group(1) in exp.names[lang] for lang in BINDINGS):
            fields = [re.match(r"\w+", f).group(0) for f, _, _ in spans_of(outside_parens(m.group(2)))]
            add_canonical([(m.group(1), f) for f in fields], f"a `{m.group(1)}` record, line {b.line}", exp)
    for where, codes, cells_ in per_code_rows:
        for lang, cell in cells_.items():
            template = learned.get(lang)
            if template:
                for code in codes:
                    exp.add(lang, template.format(code_pascal(code)), where)
            else:
                exp.unread.append(f"{where} [{lang}] per code: no class spelling learned for {lang}")
    return exp


def code_pascal(code: str) -> str:
    """`CHTYPES_ARTIFACT_MISSING` → `ArtifactMissing`."""
    return "".join(p.capitalize() for p in code.removeprefix("CHTYPES_").split("_"))


def expand_codes(text: str) -> list[str]:
    """`CHTYPES_ARTIFACT_MISSING`, `_UNTRUSTED`, `CHTYPES_SOURCE_X`, `_Y` → the full
    codes: a `_SUFFIX` takes the prefix of the full code before it."""
    out, prefix = [], ""
    for span, _, _ in spans_of(text):
        if span.startswith("CHTYPES_"):
            out.append(span)
            prefix = "_".join(span.split("_")[:2])
        elif span.startswith("_") and prefix:
            out.append(prefix + span)
    return out


def parse_vocab_table(b: Block, where: str, exp: Expected) -> None:
    """§3's vocabularies: the container each language keeps them in, the
    members the row lists, and the facts it names (in a language column, or as
    "carries `x`" in the notes)."""
    cols = language_columns(b.header)
    notes_i = b.header.index("notes") if "notes" in b.header else None
    for row in b.rows:
        entries = [s for s, _, _ in spans_of(row[1])]
        extends = row[1].strip().startswith("a ")  # "a `value_src` value": members of another row
        facts = re.findall(r"carries (?:its )?`(\w+)`", row[notes_i]) if notes_i is not None else []
        vocab = next((v for v in exp.vocabs if extends and set(entries) & set(v.entries)), None)
        if vocab is None:
            vocab = Vocab(row[0], entries)
            exp.vocabs.append(vocab)
        for i, lang in cols.items():
            spans = [s for s, _, _ in spans_of(row[i])]
            container = vocab.containers.get(lang)
            listed: list[str] = []
            for k, (span, _before, after) in enumerate(spans_of(row[i])):
                s = span.strip()
                if NOT_A_NAME.match(s) or s in BUILTINS[lang]:
                    continue
                mm = BRACE_LIST.match(s)
                if mm:  # chtypes::status::{OK, …}
                    container = [p for p in mm.group(1).split("::") if p and p != "chtypes"][-1]
                    listed += [p for p in split_top(mm.group(2)) if re.fullmatch(IDENT, p)]
                    continue
                q = re.match(rf"^({IDENT})(?:\.|::)({IDENT})$", s)
                if q and q.group(1) != "chtypes":  # Reason.OVERFLOW_WRAP, reason::OVERFLOW_WRAP
                    container = q.group(1)
                    listed.append(q.group(2))
                    continue
                if s.startswith("."):  # Python `.answered`
                    exp.add(lang, f"{container}.{s[1:]}", where)
                    continue
                call = re.fullmatch(rf"({IDENT})\(\)", s)
                if call:  # Go `Answered()`, Rust `answered()`
                    exp.add(lang, f"{container}.{call.group(1)}", where)
                    continue
                if not re.fullmatch(IDENT, s):
                    continue
                if (
                    k == 0
                    and container is None
                    and (
                        lang != "go"
                        or after.lstrip().startswith((":", "(", ","))
                        or len(spans) == 1
                        and not extends
                        and s[0].isupper()
                        and lang != "go"
                    )
                ):
                    container = s
                elif s[0].islower():  # TypeScript `answered`
                    exp.add(lang, f"{container}.{s}", where)
                else:
                    listed.append(s)
            vocab.containers[lang] = container
            vocab.listed.setdefault(lang, []).extend(listed)
            if container:
                exp.add(lang, container, where)
            for member in listed:
                exp.add(lang, member if lang == "go" else f"{container}.{member}", where)
        # A Go row without a container of its own (`ReasonOverflowWrap` …) keeps
        # its members in the type the other languages name.
        if vocab.containers.get("go") is None:
            vocab.containers["go"] = vocab.containers.get("python") or vocab.containers.get("ts")
            if vocab.containers["go"]:
                exp.add("go", vocab.containers["go"], where)
        for fact in facts:
            for lang in BINDINGS:
                if vocab.containers.get(lang):
                    exp.add(lang, f"{vocab.containers[lang]}.{spell(fact, lang, exp.initialisms)}", where)


def doc_owner(h3: str) -> str | None:
    """`### `BuildInfo`, from `chs_build_info`` → BuildInfo."""
    s = first_span(h3)
    return s if s and s[:1].isupper() else None


def parse_document_table(b: Block, where: str, exp: Expected) -> None:
    """§5's tables: canonical fields, spelled per language by principle 2."""
    canonical: list[tuple[str, str]] = []
    owner = doc_owner(b.h3)
    if b.header[0] == "type":  # | type | fields |
        for row in b.rows:
            t = first_span(row[0])
            if t:
                canonical.append((t, ""))
                canonical += [(t, f) for f, _, _ in spans_of(outside_parens(row[1]))]
    elif owner:  # | field | from | type |
        canonical.append((owner, ""))
        for row in b.rows:
            canonical += [(owner, f) for f, _, _ in spans_of(row[0])]
            type_cell = row[-1]
            for t, _, _ in spans_of(type_cell):
                if t[:1].isupper():
                    canonical.append((t, ""))
            for cell in row[1:]:
                canonical += nested_fields(cell, type_cell)
    add_canonical(canonical, where, exp)


def nested_fields(cell: str, type_cell: str) -> list[tuple[str, str]]:
    """`Each `Column`: `name` (…), `type` (…)`, `` `Capabilities`: `a`, `b` and `c`,
    each … `` and `` `errors`: `row` (int), `code` (int32) `` (whose type the
    type column names): the fields of a nested type."""
    out: list[tuple[str, str]] = []
    m = re.search(r"Each `(\w+)`:\s*(.*)$", cell)
    if m:
        return [(m.group(1), "")] + [(m.group(1), f) for f, _, _ in spans_of(outside_parens(m.group(2)))]
    m = re.match(r"^`(\w+)`:\s*(.*)$", cell)
    if not m:
        return out
    key, tail = m.groups()
    if key[0].isupper():
        sentence = tail.split(". ")[0]
        return [(key, "")] + [(key, f) for f, _, _ in spans_of(outside_parens(sentence))]
    if re.match(r"`\w+` \(", tail):  # every item `name` (type)
        t = next((s for s, _, _ in spans_of(type_cell) if s[:1].isupper()), None)
        if t:
            out += [(t, "")] + [(t, f) for f, _, _ in spans_of(outside_parens(tail))]
    return out


def parse_document_prose(b: Block, where: str, exp: Expected) -> None:
    """§5's prose that names fields: `` `Framing` is `a`, `b` and `c` ``, `` is a
    `Header` (`x` (bool), …) ``, and ErrorCodeTable's lookups with each
    language's spelling."""
    canonical: list[tuple[str, str]] = []
    m = re.match(r"^`(\w+)` is ((?:`\w+`(?:, | and |,? and )?)+)", b.text)
    if m and m.group(1)[0].isupper():
        canonical += [(m.group(1), "")] + [(m.group(1), f) for f, _, _ in spans_of(m.group(2))]
    m = re.search(r"is an? `(\w+)` \((.*)\)", b.text)
    if m:
        canonical += [(m.group(1), "")] + [(m.group(1), f) for f, _, _ in spans_of(outside_parens(m.group(2)))]
    owner = doc_owner(b.h3)
    if owner and "keeps v0's surface:" in b.text:
        head, _, tail = b.text.split("keeps v0's surface:", 1)[1].partition(" (Go ")
        methods = [s.split("(")[0] for s, _, _ in spans_of(head) if s.endswith(")")]
        overrides: dict[str, list[str]] = {}
        end = close_paren(tail)
        inside, tail = "Go " + tail[:end], tail[end + 1 :]
        for part in inside.split(";"):
            part = part.strip()
            for word, lang in LANGUAGE_COLUMNS.items():
                if part.startswith(word + " "):
                    overrides[lang] = [
                        s.split("(")[0] for s, _, _ in spans_of(part) if s.split("(")[0] not in BUILTINS[lang]
                    ]
        for lang in BINDINGS:
            spelled = overrides.get(lang) or [spell(x, lang, exp.initialisms) for x in methods]
            exp.add(lang, owner, where)
            for name in spelled:
                exp.add(lang, f"{owner}.{name}", where)
        for span, _, _ in spans_of(tail):
            struct = re.match(rf"^({IDENT})\s*\{{(.*)\}}$", span)
            if struct:
                canonical += [(struct.group(1), "")] + [
                    (struct.group(1), f.strip()) for f in struct.group(2).split(",") if f.strip()
                ]
    add_canonical(canonical, where, exp)


def close_paren(text: str) -> int:
    """The index of the first `)` outside a code span (`iter()` has its own), or len(text)."""
    inside = False
    for i, c in enumerate(text):
        if c == "`":
            inside = not inside
        elif c == ")" and not inside:
            return i
    return len(text)


def add_canonical(canonical: list[tuple[str, str]], where: str, exp: Expected) -> None:
    for owner, f in canonical:
        if not re.fullmatch(r"\w+", f or "x"):
            continue
        for lang in BINDINGS:
            exp.add(lang, owner if not f else f"{owner}.{spell(f, lang, exp.initialisms)}", where)


def parse_error_item(b: Block, where: str, exp: Expected, known: frozenset) -> None:
    """§4's bullets under the field table, each led by the languages it is
    about: `chtypes.`-qualified functions, `Type::method()` calls and type
    names; Go's "embeds `X` … in each of `A`, `B`"; and the loader refusal's
    fields, carried by Rust's `Refusal` and by the artifact error the class
    table names for Go (`*ArtifactError`), which Python and TypeScript name
    alike."""
    m = re.match(r"\s*[-*]\s+\*\*(.+?)\*\*\s*(.*)$", b.text)
    if not m:
        return
    label, body = m.groups()
    if label.startswith("A loader refusal"):
        fields = [s for s, _, _ in spans_of(outside_parens(body)) if re.fullmatch(r"[a-z_]+", s)]
        rust = re.search(r"In Rust they are the `(\w+)`", body)
        for lang in BINDINGS:
            owner = rust.group(1) if lang == "rust" and rust else "ArtifactError"
            exp.add(lang, owner, where)
            for f in fields:
                exp.add(lang, f"{owner}.{spell(f, lang, exp.initialisms)}", where)
        return
    langs = [lang for word, lang in LANGUAGE_COLUMNS.items() if word in label]
    for lang in langs:
        ctx = Ctx(lang, where, known)
        for span, before, after in spans_of(body):
            if re.search(r"(?:Go|Python|TypeScript|Rust)'s\s*$", before) or after.startswith("'s"):
                continue
            if span.startswith(("chtypes.", "chtypes::")) or re.match(rf"^[A-Z]\w*(?:::|\.){IDENT}\(", span):
                read_span(span, ctx, exp)
            elif re.fullmatch(r"[A-Z]\w+", span) and span not in BUILTINS[lang]:
                exp.add(lang, span, where)
        if lang == "go":
            em = re.search(r"embeds (?:one )?`(\w+)`[^;]*? in (?:each of )?(.*?)(?:, so|;|$)", body)
            if em:
                for outer, _, _ in spans_of(em.group(2)):
                    exp.add(lang, f"{outer}.{em.group(1)}", where)


# -------------------------------------------------------- the description


def vocab_values(entries: list[str], description: dict) -> set[str]:
    """The values a vocabulary row's description entries define, as the
    description names them (an enum's value names; `CHS_DOC_*` constants)."""
    out: set[str] = set()
    for e in entries:
        if e in description.get("enums", {}):
            for v in description["enums"][e]["values"]:
                out.add(str(v.get("name", v.get("value"))))
        elif e.endswith("*"):
            out.update(k for k in description.get("constants", {}) if k.startswith(e[:-1]))
    return out


# ---------------------------------------------------------------- surfaces
#
# A surface is {name: kind}. A member is `Owner.member`; a Python keyword-only
# parameter is `Owner.method(kw=)`; a Go constant's kind carries its type
# (`const:Format`); a vocabulary member's kind is `value`, `variant` or
# `const…`.


def go_surface_from(listing: list[dict]) -> dict[str, str]:
    out: dict[str, str] = {}
    for e in listing:
        name, kind = e["name"], e["kind"]
        if kind == "method" and name.rsplit(".", 1)[-1] in GO_PROTOCOL:
            continue
        if kind == "const":
            kind = f"const:{e.get('type', '').rsplit('.', 1)[-1]}"
        out[name] = kind
    return out


def griffe_surface(search_path: str, package: str) -> list[dict]:
    """Runs INSIDE `uv run --with griffe==GRIFFE_VERSION` (see python_surface):
    the package's public names, statically. Public is griffe's rule (`__all__`,
    else no leading underscore). A class's members are its own public ones plus
    those it inherits from a private base; dunders are protocol, except that a
    constructor's keyword-only parameters are names (`Registry(fetch=)`)."""
    import griffe  # noqa: PLC0415 — only importable inside uv's environment

    root = griffe.load(
        package,
        search_paths=[search_path],
        allow_inspection=False,
        resolve_aliases=True,
        resolve_external=False,
        try_relative_path=False,
    )
    out: list[dict] = []

    def final(obj):
        try:
            return obj.final_target if obj.is_alias else obj
        except Exception:  # noqa: BLE001 — an unresolvable re-export is listed as such
            return None

    def kind_of(obj) -> str:
        labels = {str(x) for x in (getattr(obj, "labels", None) or ())}
        if obj.is_function:
            return "function"
        if obj.is_class:
            return "class"
        if obj.is_attribute:
            if "property" in labels:
                return "property"
            if "class-attribute" in labels and obj.value is not None:
                return "value"
            return "attribute"
        return obj.kind.value

    def kwonly(obj, label: str) -> None:
        for p in obj.parameters:
            if p.kind.value == "keyword-only":
                out.append({"name": f"{label}({p.name}=)", "kind": "kw"})

    def members_of(cls) -> dict:
        found = {n: m for n, m in cls.members.items()}
        for base in cls.resolved_bases if hasattr(cls, "resolved_bases") else ():
            if base.name.startswith("_"):
                for n, m in members_of(base).items():
                    found.setdefault(n, m)
        return found

    top = {n: final(m) for n, m in root.members.items() if m.is_public}
    for name, member in sorted(root.members.items()):
        if not member.is_public:
            continue
        target = final(member)
        if target is None:
            out.append({"name": name, "kind": "unresolved"})
            continue
        out.append({"name": name, "kind": kind_of(target)})
        if target.is_module and not member.is_alias:
            # A public submodule: what it makes public and the package's top
            # level does not re-export is reachable only through it.
            for sub, m in sorted(target.members.items()):
                t = final(m)
                if (
                    m.is_public
                    and t is not None
                    and not t.is_module
                    and top.get(sub) is not t
                    and getattr(top.get(sub), "path", None) != t.path
                ):
                    out.append({"name": f"{name}.{sub}", "kind": kind_of(t)})
            continue
        if target.is_function:
            kwonly(target, name)
        if target.is_class:
            for mname, m in sorted(members_of(target).items()):
                t = final(m)
                if t is None:
                    continue
                if mname == "__init__" and t.is_function:
                    kwonly(t, name)
                    continue
                if mname.startswith("_"):
                    continue
                out.append({"name": f"{name}.{mname}", "kind": kind_of(t)})
                if t.is_function:
                    kwonly(t, f"{name}.{mname}")
    return out


def python_surface_from(listing: list[dict]) -> dict[str, str]:
    return {e["name"]: e["kind"] for e in listing}


TS_DECL = re.compile(
    r"^(export\s+)?(?:declare\s+)?(?:default\s+)?(?:abstract\s+)?"
    r"(class|interface|function|const|let|var|type|enum|namespace)\s+([A-Za-z_$][\w$]*)(.*)$"
)
TS_MODIFIERS = re.compile(r"^(?:(readonly|static|get|set|public|protected|private|abstract|declare|async|override)\s+)")
TS_MEMBER = re.compile(r"^(\[[^\]]+\]|[A-Za-z_$][\w$]*|\"[^\"]+\"|'[^']+')\s*(\?)?\s*([(<:;=,]|$)")


def parse_api_report(text: str) -> dict[str, str]:
    """An api-extractor report: every exported declaration, and the public
    members of each (a class's, an interface's, a const object's, an enum's).
    A class extending one the entry point does not export (a "forgotten
    export") also lists that base's members, which is how a caller sees them."""
    body = text
    if "```ts" in text:
        body = text.split("```ts", 1)[1].rsplit("```", 1)[0]
    out: dict[str, str] = {}
    members: dict[str, dict[str, str]] = {}
    exported: dict[str, bool] = {}
    bases: dict[str, str] = {}
    depth, owner, owner_kind = 0, None, ""
    for raw in body.splitlines():
        s = raw.strip()
        if not s or s.startswith(("//", "/*", "*")):
            continue
        if depth == 0:
            m = TS_DECL.match(s)
            if m:
                is_exported, kind, name, rest = bool(m.group(1)), m.group(2), m.group(3), m.group(4)
                exported[name] = exported.get(name, False) or is_exported
                if is_exported and (name not in out or out[name] == "type"):
                    out[name] = kind
                base = re.search(r"\bextends\s+([\w$.]+)", rest)
                if base and kind == "class":
                    bases[name] = base.group(1)
                owner, owner_kind = name, kind
                members.setdefault(name, {})
        elif depth == 1 and owner:
            mods = set()
            while True:
                mm = TS_MODIFIERS.match(s)
                if not mm:
                    break
                mods.add(mm.group(1))
                s = s[mm.end() :]
            if s.startswith("export "):
                s = s[len("export ") :]
                d = TS_DECL.match(s)
                if d:
                    members[owner][d.group(3)] = d.group(2)
            elif not mods & {"private", "protected"} and not s.startswith("constructor"):
                mm = TS_MEMBER.match(s)
                if mm:
                    name = mm.group(1).strip("\"'")
                    if owner_kind in ("const", "enum"):
                        kind = "value"
                    elif mm.group(3) in ("(", "<") and not mods & {"get", "set"}:
                        kind = "method"
                    else:
                        kind = "property"
                    members[owner].setdefault(name, kind)
        depth = max(0, depth + s.count("{") - s.count("}"))
        if depth == 0:
            owner = None

    def all_members(cls: str, seen: frozenset = frozenset()) -> dict[str, str]:
        found = dict(members.get(cls, {}))
        base = bases.get(cls)
        if base and base not in seen and not exported.get(base, False):
            for n, k in all_members(base, seen | {cls}).items():
                found.setdefault(n, k)
        return found

    for name in list(out):
        for mname, kind in all_members(name).items():
            out[f"{name}.{mname}"] = kind
    return out


RUST_ITEM = re.compile(
    r"^(?:#\[[^\]]*\]\s*)*pub\s+(?:(?:unsafe|const|async|extern\s+\"[^\"]*\")\s+)*"
    r"(mod|fn|const|static|struct|enum|union|trait|type|macro)\s+"
    r"([A-Za-z_]\w*(?:::[A-Za-z_]\w*)*)"
)
RUST_FIELD = re.compile(r"^(?:#\[[^\]]*\]\s*)*pub\s+([A-Za-z_]\w*(?:::[A-Za-z_]\w*)+)\s*(.*)$")
RUST_IMPL = re.compile(r"^(?:unsafe\s+)?impl\b(.*)$")


def strip_generics(path: str) -> str:
    out, depth = [], 0
    for c in path:
        if c == "<":
            depth += 1
        elif c == ">":
            depth -= 1
        elif depth == 0:
            out.append(c)
    return "".join(out).strip()


def parse_public_api(text: str) -> dict[str, str]:
    """cargo-public-api's listing: every public item, struct field, enum
    variant and inherent impl item, relative to the crate; a trait impl's
    items are protocol, not names."""
    out: dict[str, str] = {}
    crate = None
    impl_self, impl_trait = None, False
    for raw in text.splitlines():
        s = raw.strip()
        if not s:
            continue
        m = RUST_IMPL.match(s)
        if m:
            body = m.group(1).strip()
            if body.startswith("<"):
                depth, i = 0, 0
                for i, c in enumerate(body):
                    depth += c == "<"
                    depth -= c == ">"
                    if depth == 0:
                        break
                body = body[i + 1 :].strip()
            impl_trait = " for " in f" {body} "
            target = body.split(" for ", 1)[1] if impl_trait else body
            impl_self = strip_generics(target)
            continue
        m = RUST_ITEM.match(s)
        if m:
            kind, path = m.groups()
            if crate is None and kind == "mod":
                crate = path.split("::")[0]
            if impl_self and path.startswith(impl_self + "::") and path.count("::") == impl_self.count("::") + 1:
                if impl_trait:
                    continue
            else:
                impl_self, impl_trait = None, False
            rel = path.split("::", 1)[1] if "::" in path else ""
            if rel:
                out[rel.replace("::", ".")] = kind
            continue
        m = RUST_FIELD.match(s)
        if m and not impl_trait:
            path, rest = m.groups()
            rel = path.split("::", 1)[1] if "::" in path else path
            out[rel.replace("::", ".")] = "field" if rest.startswith(":") else "variant"
    return out


# ---------------------------------------------------------- the allowlist


KINDS = ("extra", "missing", "spelling")


@dataclass
class Entry:
    binding: str
    kind: str
    names: list[str]
    reason: str
    doc: str = ""
    as_: str = ""
    used: set[str] = field(default_factory=set)


def load_allowlist(path: Path) -> tuple[list[Entry], list[str]]:
    problems: list[str] = []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return [], [f"{path.name}: cannot read it: {e}"]
    entries = []
    for i, e in enumerate(raw.get("entries", []) if isinstance(raw, dict) else []):
        where = f"{path.name} entry {i}"
        binding, kind, reason = e.get("binding"), e.get("kind"), (e.get("reason") or "").strip()
        if binding not in BINDINGS:
            problems.append(f"{where}: binding {binding!r} is not one of {list(BINDINGS)}")
            continue
        if kind not in KINDS:
            problems.append(f"{where}: kind {kind!r} is not one of {list(KINDS)}")
            continue
        if len(reason) < 30 or reason.upper().startswith(("TODO", "TBD", "FIXME")):
            problems.append(f"{where} ({binding} {kind}): every entry needs a written reason, not {reason!r}")
            continue
        if kind == "spelling":
            if not e.get("doc") or not e.get("as"):
                problems.append(f"{where}: a spelling entry names the doc's spelling (`doc`) and the binding's (`as`)")
                continue
            entries.append(Entry(binding, kind, [], reason, e["doc"], e["as"]))
        else:
            names = e.get("names") or []
            if not names or not all(isinstance(n, str) and n for n in names):
                problems.append(f"{where}: an {kind} entry lists the names it covers (`names`)")
                continue
            entries.append(Entry(binding, kind, list(names), reason))
    return entries, problems


# ---------------------------------------------------------------- compare


@dataclass
class Finding:
    binding: str
    kind: str  # missing, spelling, undocumented, forbidden, vocabulary, allowlist
    name: str
    detail: str = ""

    def line(self) -> str:
        return f"{self.binding:6} {self.kind:12} {self.name}" + (f"  — {self.detail}" if self.detail else "")


@dataclass
class Result:
    findings: list[Finding] = field(default_factory=list)
    counts: dict[str, dict[str, int]] = field(default_factory=dict)


def vocab_members(vocab: Vocab, lang: str, surface: dict[str, str]) -> list[str]:
    """A binding's members of one vocabulary: Go's constants of its type, the
    container's values elsewhere (an enum's or const object's values, a Rust
    variant or constant)."""
    container = vocab.containers.get(lang)
    if not container:
        return []
    if lang == "go":
        return sorted(n for n, k in surface.items() if k == f"const:{container}")
    return sorted(
        n
        for n, k in surface.items()
        if n.startswith(container + ".")
        and n.count(".") == container.count(".") + 1
        and (k in ("value", "variant") or k.startswith("const"))
    )


def go_prefix(names: list[str]) -> str:
    """The word every Go member of a vocabulary starts with (`Status`, `Kind`),
    cut at a word boundary, or "" (`Accepted`, `JSONEachRow`)."""
    if len(names) < 2:
        return ""
    prefix = os.path.commonprefix(names)
    while prefix and not all(len(n) > len(prefix) and n[len(prefix)].isupper() for n in names):
        prefix = prefix[:-1]
    return prefix


def compare(exp: Expected, surfaces: dict[str, dict[str, str]], allow: list[Entry], description: dict) -> Result:
    res = Result()
    claimed: dict[str, set[str]] = {b: set() for b in surfaces}
    extras_allowed: dict[str, set[str]] = {b: set() for b in surfaces}
    for e in allow:
        if e.binding in surfaces and e.kind == "extra":
            for pat in e.names:
                hits = {n for n in surfaces[e.binding] if fnmatch.fnmatchcase(n, pat)}
                extras_allowed[e.binding] |= hits
    # Vocabularies: each binding's members, the description's count, and the
    # four bindings' members compared without case or separators.
    for vocab in exp.vocabs:
        want = len(vocab_values(vocab.entries, description))
        folded: dict[str, dict[str, str]] = {}
        for b, surface in surfaces.items():
            members = [n for n in vocab_members(vocab, b, surface) if n not in extras_allowed[b]]
            claimed[b].update(members)
            short = [n.rsplit(".", 1)[-1] for n in members]
            if b == "go":
                pre = go_prefix(short)
                short = [n[len(pre) :] for n in short]
            folded[b] = {fold(s): full for s, full in zip(short, members)}
            if want and len(members) != want:
                res.findings.append(
                    Finding(
                        b,
                        "vocabulary",
                        vocab.containers.get(b) or vocab.label,
                        f"{len(members)} member(s), the description defines {want} ({', '.join(vocab.entries)})",
                    )
                )
        if len(folded) > 1:
            sets = [frozenset(v) for v in folded.values()]
            majority = max(set(sets), key=sets.count)
            if sets.count(majority) > len(sets) / 2:
                for b, f in folded.items():
                    for k in sorted(set(f) - majority):
                        res.findings.append(
                            Finding(b, "vocabulary", f[k], f"not a member in the other bindings ({vocab.label})")
                        )
                    for k in sorted(majority - set(f)):
                        res.findings.append(
                            Finding(
                                b,
                                "vocabulary",
                                f"{vocab.containers.get(b)}: {k}",
                                f"a member the other bindings have is missing ({vocab.label})",
                            )
                        )
    for b, surface in surfaces.items():
        expected = exp.names[b]
        missing = sorted(n for n in expected if n not in surface)
        extra = sorted(n for n in surface if n not in expected and n not in claimed[b])
        entries = [e for e in allow if e.binding == b]
        allowed = {"extra": 0, "missing": 0, "spelling": 0}
        # Spelling entries first: a declared pair, however different.
        for e in (e for e in entries if e.kind == "spelling"):
            if e.doc in missing and e.as_ in surface:
                missing.remove(e.doc)
                if e.as_ in extra:
                    extra.remove(e.as_)
                e.used.add(e.doc)
                allowed["spelling"] += 1
        # Then pair what is left: the same name but for case and separators.
        by_fold: dict[str, list[str]] = {}
        for n in extra:
            by_fold.setdefault(fold(n), []).append(n)
        for n in list(missing):
            twin = by_fold.get(fold(n))
            if twin:
                got = twin.pop(0)
                missing.remove(n)
                extra.remove(got)
                res.findings.append(Finding(b, "spelling", n, f"{b} spells it `{got}` (the doc's at {expected[n]})"))
        for kind, names in (("missing", missing), ("extra", extra)):
            for n in list(names):
                for e in entries:
                    if e.kind == kind and any(fnmatch.fnmatchcase(n, p) for p in e.names):
                        e.used.update(p for p in e.names if fnmatch.fnmatchcase(n, p))
                        names.remove(n)
                        allowed[kind] += 1
                        break
        for e in (e for e in entries if e.kind == "extra"):
            for pat in e.names:  # an allowed vocabulary member counts as used
                if any(fnmatch.fnmatchcase(n, pat) for n in claimed[b] | extras_allowed[b]):
                    if any(fnmatch.fnmatchcase(n, pat) for n in surface):
                        e.used.add(pat)
        res.findings += [Finding(b, "missing", n, f"the doc gives it at {expected[n]}") for n in missing]
        res.findings += [Finding(b, "undocumented", n, f"a public {surface[n]} the doc does not give") for n in extra]
        for n, where in exp.forbidden[b].items():
            if n in surface:
                res.findings.append(Finding(b, "forbidden", n, f"the doc says {b} has no such name ({where})"))
        for kw, where in exp.kwargs[b].items():
            if not any(n.endswith(f"({kw}=)") for n in surface):
                res.findings.append(Finding(b, "missing", f"{kw}=", f"no call takes it ({where})"))
        for f, where in exp.fields[b].items():
            if not any(n.endswith(f".{f}") for n in surface):
                res.findings.append(Finding(b, "missing", f"*.{f}", f"no option type has it ({where})"))
        res.counts[b] = {
            "doc": len(expected),
            "surface": len(surface),
            "matched": len(set(expected) & set(surface)),
            "vocabulary": len(claimed[b]),
            **{f"allowed_{k}": v for k, v in allowed.items()},
        }
    for e in allow:
        if e.binding not in surfaces:
            continue
        if e.kind == "spelling":
            if e.doc not in e.used:
                res.findings.append(
                    Finding(
                        e.binding,
                        "allowlist",
                        f"{e.doc} as {e.as_}",
                        "stale: the doc's name is no longer missing, or the binding lacks the other spelling",
                    )
                )
        else:
            for pat in e.names:
                if pat not in e.used:
                    res.findings.append(
                        Finding(e.binding, "allowlist", pat, f"stale: no {e.kind} name matches it any more")
                    )
    return res


# ----------------------------------------------------------- tool runners


def go_listing(ctx, module_dir: Path, package: str, tags: str) -> list[dict]:
    out = AS.run(["go", "run", GO_LISTER, module_dir, package], cwd=ctx.neutral, env=AS.go_env(tags)).stdout
    try:
        listing = json.loads(out)
    except json.JSONDecodeError as e:
        raise ToolError(f"the Go lister printed something that is not its JSON: {e}") from e
    if not listing:
        raise ToolError(f"the Go lister found no exported name in {package}")
    return listing


def go_surface(ctx, module_dir: Path, package: str, builds: tuple[str, ...]) -> tuple[dict[str, str], str]:
    surface: dict[str, str] = {}
    raw = {}
    for tags in builds:
        listing = go_listing(ctx, module_dir, package, tags)
        raw[tags or "default"] = listing
        for name, kind in go_surface_from(listing).items():
            surface.setdefault(name, kind)
    return surface, json.dumps(raw, indent=1)


def python_listing(ctx, search_path: Path, package: str) -> list[dict]:
    cmd = [
        "uv",
        "run",
        "--no-project",
        "--no-config",
        "--quiet",
        "--with",
        f"griffe=={AS.GRIFFE_VERSION}",
        "python3",
        Path(__file__).resolve(),
        "griffe-surface",
        search_path,
        package,
    ]
    out = AS.run(cmd, cwd=ctx.neutral).stdout
    try:
        listing = json.loads(out)
    except json.JSONDecodeError as e:
        raise ToolError(f"griffe-surface printed something that is not its JSON: {e}") from e
    if not listing:
        raise ToolError(f"griffe found no public name in {package} under {search_path}")
    return listing


def ts_report(ctx, ts_dir: Path, *, fixture: bool) -> str:
    """api-extractor's report. The fixture is declarations already; the real
    package is copied to scratch, installed from its own lockfile and built
    with its own TypeScript, as api-surface builds a base tree."""
    copy = ctx.scratch("ts-" + ("fixture" if fixture else "real"))
    shutil.copytree(
        ts_dir, copy, dirs_exist_ok=True, ignore=shutil.ignore_patterns("node_modules", "dist", "api-extractor.*")
    )
    if fixture:
        lines = AS.api_extractor_report(ctx, copy, copy / "index.d.ts", [], "parity-fixture")
    else:
        AS.run(["pnpm", "install", "--frozen-lockfile", "--ignore-scripts"], cwd=copy)
        AS.ts_build(ctx, copy, copy)
        lines = AS.api_extractor_report(ctx, copy, copy / "dist" / "index.d.ts", ["node"], "parity")
    return "\n".join(lines) + "\n"


def rust_listing(ctx, manifest: Path) -> str:
    root = AS.cargo_public_api_root(ctx)
    return "\n".join(AS.rust_public_api(ctx, manifest, root)) + "\n"


# ----------------------------------------------------------------- fixtures


def recorded(binding: str) -> str:
    return (
        FIXTURES
        / "surfaces"
        / {"go": "go.json", "python": "python.json", "ts": "ts.api.md", "rust": "rust.txt"}[binding]
    ).read_text(encoding="utf-8")


def surface_of(binding: str, text: str) -> dict[str, str]:
    """A recorded or live tool output, read the way `run` reads it."""
    if binding == "go":
        raw = json.loads(text)
        merged: dict[str, str] = {}
        for listing in raw.values():
            for name, kind in go_surface_from(listing).items():
                merged.setdefault(name, kind)
        return merged
    if binding == "python":
        return python_surface_from(json.loads(text))
    if binding == "ts":
        return parse_api_report(text)
    return parse_public_api(text)


def live_fixture_output(ctx, binding: str) -> str:
    pkg = FIXTURES / binding
    if binding == "go":
        return go_surface(ctx, pkg, "example.com/parfix", ("",))[1]
    if binding == "python":
        return json.dumps(python_listing(ctx, pkg, "parfix"), indent=1)
    if binding == "ts":
        return ts_report(ctx, pkg, fixture=True)
    work = ctx.scratch("rust-fixture")
    shutil.copytree(pkg, work, dirs_exist_ok=True)
    return rust_listing(ctx, work / "Cargo.toml")


def fixture_inputs() -> tuple[Expected, list[Entry], dict, list[str]]:
    exp = parse_doc((FIXTURES / "doc.md").read_text(encoding="utf-8"), label="doc.md")
    allow, problems = load_allowlist(FIXTURES / "allowlist.json")
    description = json.loads((FIXTURES / "abi.json").read_text(encoding="utf-8"))
    return exp, allow, description, problems


def fresh(allow: list[Entry]) -> list[Entry]:
    return [Entry(e.binding, e.kind, list(e.names), e.reason, e.doc, e.as_) for e in allow]


def prove(ctx, binding: str) -> list[str]:
    """The fixture proof: the tool's live output on the fixture package reads
    exactly as the recorded one the selftest uses, and passes the fixture doc."""
    try:
        live = live_fixture_output(ctx, binding)
    except (ToolError, OSError) as e:
        return [f"{binding}: the tool failed on the fixture package: {e}"]
    want, got = surface_of(binding, recorded(binding)), surface_of(binding, live)
    problems = []
    if want != got:
        diff = AS.text_diff([f"{k} {v}" for k, v in sorted(want.items())], [f"{k} {v}" for k, v in sorted(got.items())])
        problems.append(
            f"{binding}: the tool's output on the fixture package does not read as the recorded "
            f"surface (recorded -> live):\n{diff}\n--- the live output, to record:\n{live}"
        )
    exp, allow, description, _ = fixture_inputs()
    res = compare(exp, {binding: got}, [e for e in fresh(allow) if e.binding == binding], description)
    if res.findings:
        problems.append(
            f"{binding}: the live fixture surface does not pass the fixture doc:\n"
            + "\n".join(f.line() for f in res.findings)
        )
    return problems


# ----------------------------------------------------------------- selftest

# Drift planted in each binding's recorded output: (missing, spelling, undocumented).
PLANTS = {
    "go": ("Registry.For", "Registry.FOR", "Registry.Reload"),
    "python": ("Registry.for_version", "Registry.forVersion", "Registry.reload"),
    "ts": ("Registry.for", "Registry.For", "Registry.reload"),
    "rust": ("Registry.for_version", "Registry.forVersion", "Registry.reload"),
}


def plant(binding: str, text: str, how: str) -> str:
    """One planted drift, written into the tool's own output format."""
    gone, renamed, added = PLANTS[binding]
    if binding in ("go", "python"):
        data = json.loads(text)
        listings = data.values() if binding == "go" else [data]
        for listing in listings:
            for e in list(listing):
                if e["name"] == gone or e["name"].startswith(gone + "("):
                    if how == "missing":
                        listing.remove(e)
                    elif how == "spelling":
                        e["name"] = e["name"].replace(gone, renamed, 1)
            if how == "extra":
                listing.append({"name": added, "kind": "method" if binding == "go" else "function"})
        return json.dumps(data, indent=1)
    owner, member = gone.split(".")
    if binding == "ts":
        pattern = re.compile(rf"^(\s+){re.escape(member)}\(", re.M)
        if how == "missing":
            return pattern.sub(lambda m: m.group(1) + "// removed(", text, count=1)
        if how == "spelling":
            return pattern.sub(lambda m: m.group(1) + renamed.split(".")[1] + "(", text, count=1)
        return re.sub(
            rf"(export (?:declare )?class {owner}\b[^\n]*\{{\n)",
            lambda m: m.group(1) + "    reload(): void;\n",
            text,
            count=1,
        )
    line = re.compile(rf"^pub fn (\w+)::{owner}::{member}\(.*$", re.M)
    if how == "missing":
        return line.sub("", text, count=1)
    if how == "spelling":
        return line.sub(lambda m: m.group(0).replace(f"::{member}(", f"::{renamed.split('.')[1]}(", 1), text, count=1)
    return re.sub(
        rf"^(impl (\w+)::{owner}\n)",
        lambda m: m.group(1) + f"pub fn {m.group(2)}::{owner}::reload(&self)\n",
        text,
        count=1,
        flags=re.M,
    )


def selftest() -> int:
    failures: list[str] = []
    exp, allow, description, problems = fixture_inputs()
    failures += [f"the fixture allowlist: {p}" for p in problems]

    # The green fixtures pass, every binding.
    surfaces = {b: surface_of(b, recorded(b)) for b in BINDINGS}
    for b in BINDINGS:
        if not surfaces[b]:
            failures.append(f"the recorded {b} surface read as empty")
    res = compare(exp, surfaces, fresh(allow), description)
    if res.findings:
        failures.append("the green fixtures do not pass:\n" + "\n".join(f.line() for f in res.findings))
    for b in BINDINGS:
        if res.counts.get(b, {}).get("matched", 0) < 20:
            failures.append(
                f"the green {b} fixture matched only {res.counts.get(b)} names; the reader or the doc "
                "parser read too little"
            )

    # Each binding, each direction: planted drift must fail, for its own reason.
    for b in BINDINGS:
        for how, kind in (("missing", "missing"), ("spelling", "spelling"), ("extra", "undocumented")):
            planted = plant(b, recorded(b), how)
            if planted == recorded(b):
                failures.append(f"{b}: the {how} plant changed nothing in the recorded output")
                continue
            got = compare(exp, {**surfaces, b: surface_of(b, planted)}, fresh(allow), description)
            kinds = {(f.binding, f.kind) for f in got.findings}
            if (b, kind) not in kinds:
                failures.append(
                    f"{b}: a planted {how} did not fail as `{kind}` "
                    f"(got: {[f.line() for f in got.findings] or 'a pass'})"
                )
            elif any(f.binding != b for f in got.findings):
                failures.append(f"{b}: a planted {how} also failed another binding: {[f.line() for f in got.findings]}")

    # A forbidden name, a vocabulary member gone, and the allowlist's own rules.
    forbidden = dict(surfaces["python"], **{"Registry.close": "function"})
    if not any(
        f.kind == "forbidden"
        for f in compare(exp, {**surfaces, "python": forbidden}, fresh(allow), description).findings
    ):
        failures.append("python: `Registry.close`, which the doc says no binding has, did not fail as forbidden")
    short = {k: v for k, v in surfaces["rust"].items() if k != "Format.Csv"}
    if not any(
        f.kind == "vocabulary" for f in compare(exp, {**surfaces, "rust": short}, fresh(allow), description).findings
    ):
        failures.append("rust: a vocabulary member removed did not fail as `vocabulary`")
    stale = fresh(allow) + [Entry("go", "extra", ["NoSuchName"], "a reason long enough to be accepted here")]
    if not any(f.kind == "allowlist" for f in compare(exp, surfaces, stale, description).findings):
        failures.append("an allowlist entry that matches nothing was not reported stale")
    _, bad = load_allowlist_text('{"entries": [{"binding": "go", "kind": "extra", "names": ["X"], "reason": "x"}]}')
    if not bad:
        failures.append("an allowlist entry without a written reason was accepted")
    without = [e for e in fresh(allow) if not (e.binding == "ts" and e.kind == "spelling")]
    if not any(f.binding == "ts" for f in compare(exp, surfaces, without, description).findings):
        failures.append("ts: removing its allowlisted spelling difference did not fail")

    # The parser's pieces, on the shapes the real doc uses.
    if python_kwonly("format: Format, body: bytes, *, settings=None, doc_flags: DocFlags = DocFlags.ALL") != [
        "settings",
        "doc_flags",
    ]:
        failures.append("python_kwonly misread a keyword-only parameter list")
    if expand_codes("`CHTYPES_ARTIFACT_MISSING`, `_UNTRUSTED`, `CHTYPES_SOURCE_UNREACHABLE`, `_FORBIDDEN`") != [
        "CHTYPES_ARTIFACT_MISSING",
        "CHTYPES_ARTIFACT_UNTRUSTED",
        "CHTYPES_SOURCE_UNREACHABLE",
        "CHTYPES_SOURCE_FORBIDDEN",
    ]:
        failures.append("expand_codes did not complete a `_SUFFIX` code from the one before it")
    if spell("columns_sql", "go", {"sql": "SQL"}) != "ColumnsSQL" or spell("is_stored", "ts", {}) != "isStored":
        failures.append("spell() broke principle 2's mechanical rule")

    # The real doc: every section the parser reads yields names, in every binding.
    real = expected_surface()
    musts = {
        "go": [
            "Setup",
            "SetupOptions.Timezone",
            "Registry.For",
            "Registry.ForContext",
            "OpenLinked",
            "Library.Version",
            "Library.CompileTable",
            "Schema.Close",
            "WithRowFilter",
            "RowsOption",
            "Format",
            "StatusOK",
            "Verdict.Answered",
            "SchemaError",
            "ErrArtifactMissing",
            "CallError.ChCode",
            "AsCallError",
            "BuildInfo.InputsSHA256",
            "RowResult.PartitionID",
            "Header.Consumed",
            "ErrorCodeTable.All",
            "SchemaError.CallError",
            "Capabilities.InputFormats",
            "FilterRowError.Msg",
        ],
        "python": [
            "setup(timezone=)",
            "Registry(fetch=)",
            "Registry.for_version",
            "Library.version",
            "Schema.rows(row_filter=)",
            "BytesIn",
            "Reason.OVERFLOW_WRAP",
            "Source.is_stored",
            "ArtifactMissingError",
            "CallError.ch_code",
            "Framing.bom_skipped",
            "ErrorCodeTable.all",
        ],
        "ts": [
            "setup",
            "SetupOptions.timezone",
            "Registry.open",
            "Registry.for",
            "Library.buildInfo",
            "Schema.[Symbol.dispose]",
            "RowsOptions.rowFilter",
            "Verdict.answered",
            "CallError.messageBytes",
            "SourceUnreachableError",
            "BuildInfo.inputsSha256",
        ],
        "rust": [
            "setup",
            "Registry.new",
            "Library.open_unverified",
            "Library.compile_table",
            "RowsOptions.filter",
            "RawText",
            "status.OK",
            "reason.OVERFLOW_WRAP",
            "Error.Schema",
            "Error.ArtifactMissing",
            "Refusal.want",
            "Error.is_unsupported",
            "ErrorCodeTable.iter",
            "Result",
        ],
    }
    for b, names in musts.items():
        for n in names:
            if n not in real.names[b]:
                failures.append(f"the real doc's {b} names lack {n!r}: a section stopped parsing")
    for n in ("Registry.Close", "Library.Close"):
        if n not in real.forbidden["go"]:
            failures.append(f"the real doc's §3 'no `Close`' did not forbid go {n}")
    if len(real.vocabs) < 9:
        failures.append(f"the real doc's §3 vocabularies read as {len(real.vocabs)}, not at least 9")

    if failures:
        for f in failures:
            print(f"SELFTEST FAILED: {f}", file=sys.stderr)
        return 1
    print(
        "parity-surface: selftest ok — the green fixtures pass in all four bindings; a planted missing name, "
        "different spelling and undocumented name each fail in each binding, for their own reason and in that "
        "binding only; a forbidden name, a vocabulary member removed, a stale allowlist entry, an entry without a "
        "reason and a removed spelling entry each fail; the real doc yields names from every section the parser "
        "reads"
    )
    return 0


def load_allowlist_text(text: str) -> tuple[list[Entry], list[str]]:
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "allow.json"
        p.write_text(text, encoding="utf-8")
        return load_allowlist(p)


# ---------------------------------------------------------------------- run


def real_surface(ctx, binding: str) -> tuple[dict[str, str], str]:
    if binding == "go":
        return go_surface(ctx, ROOT / "go", GO_PACKAGE, ("", "chtypes_linked"))
    if binding == "python":
        listing = python_listing(ctx, ROOT / "python" / "src", "chtypes")
        return python_surface_from(listing), json.dumps(listing, indent=1)
    if binding == "ts":
        text = ts_report(ctx, ROOT / "ts", fixture=False)
        return parse_api_report(text), text
    text = rust_listing(ctx, ROOT / "rust" / "Cargo.toml")
    return parse_public_api(text), text


def summary(lines: list[str]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


def cmd_run(tools: Path, only: tuple[str, ...], save: Path | None, proofs_only: bool = False) -> int:
    with tempfile.TemporaryDirectory(prefix="parity-surface-") as tmp:
        ctx = AS.Ctx(tools=tools, work=Path(tmp), neutral=Path(tmp) / "neutral")
        ctx.neutral.mkdir()
        proof: dict[str, list[str]] = {}
        for b in only:
            print(f"::group::fixture proof — {b}", flush=True)
            proof[b] = prove(ctx, b)
            for p in proof[b]:
                print(p)
            print("::endgroup::")
            print(f"parity-surface: fixture proof for {b}: {'FAILED' if proof[b] else 'ok'}", flush=True)
        if proofs_only:
            return 1 if any(proof.values()) else 0
        exp = expected_surface()
        allow, problems = load_allowlist(ALLOWLIST)
        description = json.loads(DESCRIPTION.read_text(encoding="utf-8"))
        for p in problems:
            print(f"parity-surface: allowlist: {p}", file=sys.stderr)
        surfaces: dict[str, dict[str, str]] = {}
        errors: dict[str, str] = {}
        for b in only:
            if proof[b]:
                errors[b] = "its fixture proof failed above, so its reader is not trusted"
                continue
            print(f"::group::{b} — reading the real surface", flush=True)
            try:
                surfaces[b], raw = real_surface(ctx, b)
                if save:
                    save.mkdir(parents=True, exist_ok=True)
                    (save / f"{b}.surface.txt").write_text(raw, encoding="utf-8")
            except (ToolError, OSError) as e:
                errors[b] = str(e)
            print("::endgroup::")
        res = compare(exp, surfaces, [e for e in allow if e.binding in surfaces], description)
        print(
            "parity-surface: per binding — the doc's names, the surface's names, how many matched, how many "
            "vocabulary members were checked against the description, and what the allowlist covered:"
        )
        rows = []
        for b in only:
            if b in errors:
                print(f"  {b:6} ERROR: {errors[b]}")
                rows.append(f"| {b} | error | | | | | |")
                continue
            c = res.counts[b]
            n = sum(1 for f in res.findings if f.binding == b)
            print(
                f"  {b:6} doc={c['doc']} surface={c['surface']} matched={c['matched']} "
                f"vocabulary={c['vocabulary']} allowlisted: extra={c['allowed_extra']} "
                f"missing={c['allowed_missing']} spelling={c['allowed_spelling']} findings={n}"
            )
            rows.append(
                f"| {b} | {c['doc']} | {c['surface']} | {c['matched']} | {c['vocabulary']} | "
                f"{c['allowed_extra']}/{c['allowed_missing']}/{c['allowed_spelling']} | {n} |"
            )
        for f in res.findings:
            print(f"  FINDING {f.line()}")
        summary(
            [
                "### parity-surface — each binding's public names against docs/reference/bindings-v1.md",
                "",
                "| binding | doc names | surface names | matched | vocabulary members | allowlisted "
                "(extra/missing/spelling) | findings |",
                "| --- | --- | --- | --- | --- | --- | --- |",
            ]
            + rows
            + [""]
            + [f"- `{f.line()}`" for f in res.findings]
        )
        bad = problems or errors or res.findings
        print(
            f"parity-surface: {'red' if bad else 'green'} — {len(res.findings)} finding(s), "
            f"{len(problems)} allowlist problem(s), tool error(s) for {sorted(errors) or 'none'}"
        )
        return 1 if bad else 0


def cmd_expected(only: tuple[str, ...]) -> int:
    exp = expected_surface()
    for b in only:
        print(f"## {b}: {len(exp.names[b])} name(s)")
        for n, where in sorted(exp.names[b].items()):
            print(f"  {n}    ({where})")
        for n in sorted(exp.forbidden[b]):
            print(f"  NOT {n}")
        for n in sorted(exp.kwargs[b]):
            print(f"  some call takes {n}=")
        for n in sorted(exp.fields[b]):
            print(f"  some option type has .{n}")
    print("## vocabularies")
    for v in exp.vocabs:
        print(f"  {v.label}: {v.entries} containers={v.containers}")
    print(f"## spans the parser could not read ({len(exp.unread)})")
    for u in exp.unread:
        print(f"  {u}")
    return 0


def main(argv: list[str]) -> int:
    if argv == ["--selftest"]:
        return selftest()
    if argv[:1] == ["griffe-surface"] and len(argv) == 3:
        print(json.dumps(griffe_surface(argv[1], argv[2]), indent=1))
        return 0
    tools = Path(os.environ.get("CHTYPES_API_SURFACE_TOOLS") or AS.default_tools())
    if argv[:1] == ["--fixtures"]:
        only = tuple(argv[1:]) or BINDINGS
        if any(b not in BINDINGS for b in only):
            print(f"parity-surface: the bindings are {list(BINDINGS)}", file=sys.stderr)
            return 2
        return cmd_run(tools, only, None, proofs_only=True)
    if argv[:1] == ["expected"]:
        only = tuple(argv[1:]) or BINDINGS
        return cmd_expected(only)
    if argv[:1] == ["run"]:
        rest, only, save = argv[1:], BINDINGS, None
        while rest:
            flag = rest.pop(0)
            if flag == "--tools" and rest:
                tools = Path(rest.pop(0))
            elif flag == "--save" and rest:
                save = Path(rest.pop(0))
            elif flag == "--only" and rest:
                picked = []
                while rest and not rest[0].startswith("--"):
                    picked.append(rest.pop(0))
                if not picked or any(b not in BINDINGS for b in picked):
                    print(f"parity-surface: --only takes bindings from {list(BINDINGS)}", file=sys.stderr)
                    return 2
                only = tuple(picked)
            else:
                print(f"parity-surface: unexpected argument {flag!r}", file=sys.stderr)
                return 2
        return cmd_run(tools, only, save)
    print(__doc__.split("\n\n")[1], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
