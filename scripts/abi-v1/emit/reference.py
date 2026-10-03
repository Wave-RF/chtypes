"""The generated block of docs/reference/abi-v1.md.

Everything a reader looks up per symbol: identity, the provisional markers,
the parameter and content vocabularies, every handle, enum, vocabulary,
constant, document and function, with each function's C prototype, class,
thread class, statuses, ownership facts and docs.md prose. The hand-written
part of the page (the rules, the loader sequence, how to regenerate) sits
outside the markers and is never touched.

The block must pass the repository's markdownlint and dprint as emitted, so
tables are written the way dprint formats them: every cell padded to its
column's width, the delimiter row as wide as the column. Table cells are
ASCII, so a character is a column.
"""

from __future__ import annotations

import json

from model import CONTENTS, THREADS

from . import Output, banner, marker_key

PATH = "docs/reference/abi-v1.md"
BLOCK = "abi-v1"

PARAM_KINDS = [
    ("scalar", "`int32_t`, `uint32_t`, `int64_t`, `uint64_t` or `size_t`", "passed by value"),
    ("enum", "the enum's `int32_t` typedef", "the binding passes the integer"),
    (
        "bytes_in",
        "`const uint8_t *<name>, size_t <name>_len`",
        "length 0 is none and the pointer may then be NULL; NULL with a nonzero length is CHS_INVALID_ARGUMENT; "
        "NUL and invalid UTF-8 pass through",
    ),
    ("handle", "`[const] chs_K *`", "kind and image tags checked; NULL only where marked nullable"),
    ("out_handle", "`chs_K **`", "the caller owns the result and frees it with the handle's free, in any order"),
    ("out_error", "`chs_error **`", "always optional; set only when the status is not CHS_OK"),
    ("out_scalar", "`int32_t *` and the like", "reserved; no function uses it"),
]


def _cell(s: str) -> str:
    if "|" in s.replace("\\|", ""):
        raise ValueError(f"a table cell contains a bare pipe: {s!r}")
    if not s.isascii():
        raise ValueError(f"a table cell is not ASCII: {s!r}")
    return s


def table(headers: list[str], rows: list[list[str]]) -> list[str]:
    cells = [[_cell(c) for c in headers]] + [[_cell(c) for c in r] for r in rows]
    widths = [max(3, *(len(r[i]) for r in cells)) for i in range(len(headers))]

    def line(r: list[str]) -> str:
        return "| " + " | ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)) + " |"

    return [line(cells[0]), "| " + " | ".join("-" * w for w in widths) + " |", *(line(r) for r in cells[1:])]


def _marks(ms: tuple[str, ...]) -> str:
    return ", ".join(ms) if ms else "FIRM"


def _code(s: str) -> str:
    return f"`{s}`"


def render(model) -> str:
    out: list[str] = [f"<!-- {banner(model)} -->", ""]
    firm = [f for f in model.functions if f.is_firm]
    prov = [f for f in model.functions if not f.is_firm]
    classes = {}
    for f in model.functions:
        classes[f.cls] = classes.get(f.cls, 0) + 1

    out += ["### Identity", ""]
    out += table(
        ["item", "value"],
        [
            ["`CHS_ABI_VERSION`", str(model.abi)],
            ["`CHS_ABI_FINGERPRINT`", _code(model.fingerprint)],
            ["library", f"`{model.library_stem}`, exporting only `{model.prefix}*`"],
            ["functions", f"{len(model.functions)}: " + ", ".join(f"{n} {c}" for c, n in sorted(classes.items()))],
            ["FIRM functions", str(len(firm))],
            ["provisional functions", str(len(prov))],
            ["`reuse_v0_names`", "true" if model.sdk["reuse_v0_names"] else "false"],
        ],
    )

    used: dict[str, list[str]] = {}

    def note(ms: tuple[str, ...], what: str) -> None:
        for m in ms:
            used.setdefault(m, []).append(what)

    for h in model.handles.values():
        note(h.provisional, _code(h.name))
    for e in model.enums.values():
        note(e.provisional, _code(e.name))
    for c in model.constants.values():
        note(c.provisional, _code(c.name))
    for d in model.documents.values():
        note(d.provisional, f"document `{d.name}`")
    for field, ms in model.build_info_provisional.items():
        note(ms, f"build_info `{field}`")
    for f in model.functions:
        note(f.provisional, _code(f.name))
    out += ["", "### Provisional markers", ""]
    if used:
        out += table(
            ["marker", "waits on", "entries"],
            [[m, model.markers[m], ", ".join(used[m])] for m in sorted(used, key=marker_key)],
        )
    else:
        out.append("None: every entry in the description is FIRM.")

    out += ["", "### Thread classes", ""]
    out += table(["class", "what a caller may run at the same time"], [[_code(k), v] for k, v in THREADS.items()])

    out += ["", "### Parameter kinds", ""]
    out += table(["kind", "C spelling", "rule"], [[_code(k), c, r] for k, c, r in PARAM_KINDS])
    out += [
        "",
        "### Contents",
        "",
        "Every counted input and every `chs_buf` is a byte string; the content says what it carries.",
        "",
    ]
    out += table(
        ["content", "carries"],
        [
            [_code(k), v]
            for k, v in [*CONTENTS.items(), ("document:<name>", "a JSON document, listed under Documents below")]
        ],
    )

    cn = model.column_names
    out += [
        "",
        "### Column names in JSON",
        "",
        f"A column name is a byte string. Wherever a JSON document, input or output, carries one, it is the member "
        f"`{cn['text']}`, a JSON string, when the name's bytes are valid UTF-8 (a NUL written as the JSON escape `\\u0000`), "
        f"and otherwise the member `{cn['bytes']}`, the raw bytes in standard {cn['bytes_encoding']} with padding. "
        f"Exactly one of the two is present. In an array of names, each element is an object carrying one of them. "
        f"A byte-returning accessor, such as `chs_error_column`, returns the raw bytes instead.",
    ]

    out += ["", "### Handles", ""]
    out += table(
        ["handle", "freed by", "holds", "status"],
        [
            [_code(h.name), _code(h.free), ", ".join(_code(x) for x in h.holds) or "-", _marks(h.provisional)]
            for h in model.handles.values()
        ],
    )
    for h in model.handles.values():
        out += ["", f"#### `{h.name}`", "", h.doc]

    out += ["", "### Enums", ""]
    for e in model.int_enums:
        flags = ", ".join(w for w, on in (("closed", e.closed), ("frozen", e.frozen)) if on) or "-"
        out += [f"#### `{e.name}`", "", e.doc, "", f"`int32_t`; {flags}; {_marks(e.provisional)}.", ""]
        extra = list(e.fields)
        out += table(
            ["name", "value", *extra],
            [[_code(v.name), str(v.value), *(str(v.fields[k]) for k in extra)] for v in e.values],
        )
        out.append("")
    out.pop()

    out += ["", "### Document vocabularies", ""]
    for e in model.vocabularies:
        extra = list(e.fields)
        fb = f"; an unrecognized value is read as `{e.fallback}`" if e.fallback else ""
        out += [f"#### `{e.name}`", "", e.doc, "", f"{_marks(e.provisional)}{fb}.", ""]
        out += table(
            ["value", *extra],
            [
                [
                    _code(str(v.value)) if v.value != "" else '`""` (none)',
                    *(str(v.fields[k]).lower() if isinstance(v.fields[k], bool) else str(v.fields[k]) for k in extra),
                ]
                for v in e.values
            ],
        )
        out.append("")
    out.pop()

    out += ["", "### Constants", ""]
    out += table(
        ["constant", "type", "value", "status"],
        [[_code(c.name), c.type, str(c.value), _marks(c.provisional)] for c in model.constants.values()],
    )

    out += ["", "### Documents", ""]
    out += table(
        ["document", "carries names", "fixed fields", "status"],
        [
            [_code(d.name), "yes" if d.carries_names else "no", "yes" if d.schema is not None else "-", _marks(d.provisional)]
            for d in model.documents.values()
        ],
    )
    for d in model.documents.values():
        out += ["", f"#### document `{d.name}`", ""]
        if d.doc:
            out += [d.doc, ""]
        if d.schema is not None:
            out += [
                "The fields the description fixes, as JSON Schema; the document may carry more:",
                "",
                "```json",
                json.dumps(d.schema, indent=2),
                "```",
            ]
        else:
            out.append("The description fixes no field of this document; its prose above is the contract.")

    out += ["", "### build_info", ""]
    props = model.build_info_schema.get("properties", {})
    req = set(model.build_info_schema.get("required", []))
    cross = {c["build_info"]: c["compare"] for c in model.sdk["cross_check"]}
    rows = []
    for name, sch in props.items():
        shape = sch.get("type", "-")
        if "const" in sch:
            shape += f" = {sch['const']}"
        elif "pattern" in sch:
            shape += f" `{sch['pattern']}`"
        rows.append(
            [
                _code(name),
                shape,
                "yes" if name in req else "no",
                cross.get(name, "-"),
                _marks(model.build_info_provisional.get(name, ())),
            ]
        )
    out += table(["field", "shape", "required", "cross-checked", "content"], rows)

    out += ["", "### Functions", ""]
    out += table(
        ["function", "class", "thread", "returns", "status"],
        [
            [
                _code(f.name),
                f.cls,
                f.thread,
                "status" if f.returns.kind == "status" else _code(f.returns.c_type.strip()),
                _marks(f.provisional),
            ]
            for f in model.functions
        ],
    )
    for f in model.functions:
        out += ["", f"#### `{f.name}`", "", "```c", f.prototype(), "```", ""]
        facts = [f"- Class `{f.cls}`, thread `{f.thread}`, {_marks(f.provisional)}."]
        if f.returns.kind == "status":
            facts.append("- May return " + ", ".join(_code(s) for s in f.may_return) + ".")
        if f.returns.kind == "handle":
            facts.append(
                f"- Returns a new `{f.returns.type}` the caller owns"
                + (f" ({f.returns.content})" if f.returns.content else "")
                + ", freed with "
                + _code(model.handles[f.returns.type].free)
                + "."
            )
        if f.returns.borrows:
            facts.append(f"- The pointer is borrowed from `{f.returns.borrows}`.")
        if f.returns.constant:
            facts.append(f"- Always returns `{f.returns.constant}`.")
        for p in f.params:
            bits = [p.kind]
            if p.type and p.kind != "out_error":
                bits.append(_code(p.type))
            if p.content:
                bits.append(_code(p.content))
            if p.nullable and p.kind != "out_error":
                bits.append("nullable")
            facts.append(f"- `{p.name}`: " + ", ".join(bits) + ".")
        out += facts + ["", f.doc]
    out.append("")
    return "\n".join(out)


def outputs(model) -> list[Output]:
    return [Output(PATH, block=BLOCK, body=render(model))]
