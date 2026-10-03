"""The C header, include/v1/chtypes.h, and the sorted export list.

The header is the artifact producer's input (it pins it by commit and hash)
and the one place a C reader sees the whole ABI, so it carries every fact the
description holds: each symbol's prose from docs.md, each function's class,
thread class, statuses, parameter contents and provisional markers, and
CHS_ABI_FINGERPRINT. It compiles as C11 and as C++20 (CI checks both).

Every int32 enum is spelled as a fixed-width typedef plus #define constants,
never as a C `enum` type, so no parameter or return has an
implementation-defined width, and the header has no tag that clashes with a
typedef name in C++.

spec/abi-v1/generated/exports.txt is every exported symbol, one per line,
sorted, after a `#` banner line: what a loader resolves at step 6, and what an
export list (a linker version script or exported-symbols file) names.
"""

from __future__ import annotations

import textwrap

from model import CONTENTS, THREADS

from . import Output, banner, marker_key

HEADER = "include/v1/chtypes.h"
EXPORTS = "spec/abi-v1/generated/exports.txt"
WIDTH = 100


class ProseError(ValueError):
    pass


def _check_prose(where: str, text: str) -> None:
    for bad in ("/*", "*/", "??"):
        if bad in text:
            raise ProseError(
                f"{where}: the prose contains {bad!r}, which cannot sit inside a C comment "
                "(a nested comment, an early close, or a trigraph); rephrase it in spec/abi-v1/docs.md"
            )


def comment_lines(text: str, prefix: str = " * ") -> list[str]:
    """Markdown prose (one paragraph or list item per line, no hard wraps)
    as wrapped C comment lines. Fenced code and table rows stay verbatim."""
    out: list[str] = []
    in_fence = False
    for line in text.splitlines():
        stripped = line.rstrip()
        if stripped.lstrip().startswith("```"):
            in_fence = not in_fence
            out.append((prefix + stripped).rstrip())
            continue
        if in_fence or stripped.lstrip().startswith("|") or not stripped:
            out.append((prefix + stripped).rstrip())
            continue
        lead = len(stripped) - len(stripped.lstrip())
        body = stripped.lstrip()
        hang = 0
        for marker in ("- ", "* "):
            if body.startswith(marker):
                hang = len(marker)
        if not hang and body[:1].isdigit():
            num, dot, _ = body.partition(". ")
            if dot and num.isdigit():
                hang = len(num) + 2
        first = prefix + " " * lead
        rest = prefix + " " * (lead + hang)
        out += textwrap.wrap(
            body,
            width=WIDTH,
            initial_indent=first,
            subsequent_indent=rest,
            break_long_words=False,
            break_on_hyphens=False,
        ) or [prefix.rstrip()]
    return [line.rstrip() for line in out]


def block_comment(text: str, where: str) -> list[str]:
    _check_prose(where, text)
    return ["/*", *comment_lines(text), " */"]


def _markers(model, markers: tuple[str, ...]) -> str:
    return ", ".join(markers)


def _int_literal(ctype: str, value: int) -> str:
    if ctype == "uint32":
        return f"{value}u"
    if ctype == "int64":
        return f"INT64_C({value})"
    if ctype == "uint64":
        return f"UINT64_C({value})"
    return f"({value})" if value < 0 else str(value)


def _section(title: str) -> list[str]:
    rule = "/* " + "-" * (WIDTH - 6 - len(title) - 1) + " " + title + " */"
    return ["", rule, ""]


def _function_facts(model, fn) -> str:
    facts = [f"Class: {fn.cls}. Thread: {fn.thread}."]
    if fn.returns.kind == "status":
        facts.append("Returns one of: " + ", ".join(fn.may_return) + ".")
    r = fn.returns
    if r.kind == "handle":
        facts.append(
            f"Returns a new {r.type} the caller owns and frees with {model.handles[r.type].free}"
            + (f"; its content is {CONTENTS.get(r.content or '', r.content)}" if r.content else "")
            + ("; NULL only for a NULL or invalid argument." if r.nullable else ".")
        )
    if r.borrows:
        facts.append(f"The pointer is borrowed from `{r.borrows}` and valid until it is freed; copy before freeing.")
    if r.constant:
        facts.append(f"Always returns {r.constant} ({model.constants[r.constant].value}).")
    lines = []
    for p in fn.params:
        if p.kind == "bytes_in":
            lines.append(
                f"- `{p.name}`, `{p.name}_len`: {CONTENTS.get(p.content or '', p.content)}; "
                "length 0 is none, and the pointer may then be NULL."
            )
        elif p.kind == "handle":
            lines.append(f"- `{p.name}`: " + ("may be NULL." if p.nullable else "required."))
        elif p.kind == "out_handle":
            what = (
                CONTENTS.get(p.content or "", p.content)
                if p.content and not p.content.startswith("document:")
                else (f"the JSON document `{p.content[len('document:') :]}`" if p.content else f"a new {p.type}")
            )
            lines.append(
                f"- `{p.name}`: receives {what}, owned by the caller, freed with {model.handles[p.type].free}"
                + ("; may be NULL when that output is not wanted." if p.nullable else "; required.")
            )
        elif p.kind == "out_error":
            lines.append(f"- `{p.name}`: may be NULL; set only when the status is not CHS_OK.")
    text = " ".join(facts)
    if lines:
        text += "\n\n" + "\n".join(lines)
    if fn.provisional:
        legend = "; ".join(f"{m}: {model.markers[m]}" for m in fn.provisional)
        text += f"\n\nPROVISIONAL, waiting on {legend}."
    return text


def prototype_lines(fn) -> list[str]:
    """The prototype on one line when it fits, else one parameter per line."""
    one = fn.prototype()
    if len(one) <= WIDTH or not fn.c_params:
        return [one]
    head = one[: one.index("(") + 1]
    decls = [cp.decl() for cp in fn.c_params]
    return [head, *(f"    {d}," for d in decls[:-1]), f"    {decls[-1]});"]


def render_header(model) -> str:
    out: list[str] = [f"/* {banner(model)} */"]
    out += block_comment(
        f"chtypes.h: the chtypes C ABI, generation {model.abi}.\n\n{model.docs.preamble}", "docs.md preamble"
    )
    out += [
        "#ifndef CHTYPES_H",
        "#define CHTYPES_H",
        "",
        "#include <stddef.h>",
        "#include <stdint.h>",
        "",
        "#ifdef __cplusplus",
        'extern "C" {',
        "#endif",
        "",
        "/* Every chs_* entry point is exported with default visibility; the library exports nothing else. */",
        "#ifndef CHS_API",
        '#define CHS_API __attribute__((visibility("default")))',
        "#endif",
    ]

    out += _section("identity")
    out += block_comment(
        "The ABI generation: what chs_abi_version() returns. A loader refuses a library whose answer differs.",
        "identity",
    )
    out.append(f"#define CHS_ABI_VERSION {model.abi}")
    out += [""] + block_comment(
        "`sha256:` and 64 lowercase hex digits: sha256 over the RFC 8785 canonical form of "
        "spec/abi-v1/abi.json. chs_build_info()'s `abi_fingerprint` is this macro, copied; a loader compares "
        "the two byte for byte and refuses any difference.",
        "identity",
    )
    out.append(f'#define CHS_ABI_FINGERPRINT "{model.fingerprint}"')

    used = sorted(
        {m for f in model.functions for m in f.provisional}
        | {m for h in model.handles.values() for m in h.provisional}
        | {m for e in model.enums.values() for m in e.provisional}
        | {m for c in model.constants.values() for m in c.provisional}
        | {m for d in model.documents.values() for m in d.provisional}
        | {m for ms in model.build_info_provisional.values() for m in ms},
        key=marker_key,
    )
    if used:
        out += _section("provisional markers")
        legend = "\n".join(f"- {m}: {model.markers[m]}" for m in used)
        out += block_comment(
            "A declaration marked PROVISIONAL is in its proposed form and may change before the ABI is "
            "confirmed; one with no marker is FIRM. The markers:\n\n" + legend,
            "markers",
        )

    out += _section("thread classes")
    out += block_comment(
        "Every function below names its thread class: what a caller may run at the same time as it.\n\n"
        + "\n".join(f"- {k}: {v}." for k, v in THREADS.items()),
        "thread classes",
    )

    bs = model.byte_strings
    out += _section("byte strings in JSON")
    lines = []
    for d in model.documents.values():
        if d.byte_fields:
            lines.append(f"- {d.name}: " + ", ".join(d.byte_fields) + ".")
    out += block_comment(
        "Every data-derived string a JSON document carries (a column name, a value's rendering, a type, SQL "
        f"text, a message) is a byte string, so one rule carries them all. The member F is a JSON string when "
        f"the bytes are valid UTF-8 (a NUL written as the JSON escape for U+0000), and otherwise the member "
        f"F{bs['suffix']} holds the raw bytes in standard {bs['encoding']} with padding. Never both; a member "
        "that names an entry (a column name) is always present in one of its two forms. Every document is "
        "therefore valid UTF-8 JSON. Where a list or a map would hold a bare data-derived string, it holds an "
        f"object instead, so the rule applies to its members. An entry that reports a stored value carries "
        f"`{bs['value']}`, the raw bytes of a scalar String or FixedString value, beside its rendering. A "
        "byte-returning accessor such as chs_error_column returns the raw bytes instead.\n\n"
        "The data-derived fields, by document:\n\n" + "\n".join(lines),
        "byte strings",
    )

    out += _section("handles")
    for h in model.handles.values():
        text = h.doc + f"\n\nFreed with {h.free}."
        if h.holds:
            text += " Holds a reference to its " + ", ".join(h.holds) + ", so any free order is safe."
        if h.provisional:
            text += f"\n\nPROVISIONAL ({_markers(model, h.provisional)})."
        out += block_comment(f"{h.name}: {text}", f"handle {h.name}")
        out.append(f"typedef struct {h.name} {h.name};")
        out.append("")
    out.pop()

    out += _section("enums")
    for e in model.int_enums:
        flags = [w for w, on in (("closed", e.closed), ("frozen", e.frozen)) if on]
        text = e.doc
        if flags:
            text += (
                "\n\n"
                + " and ".join(flags).capitalize()
                + ": "
                + (
                    "no value is added within this generation, and none is renumbered or removed."
                    if e.closed and e.frozen
                    else "no value is renumbered or removed."
                )
            )
        if e.provisional:
            text += f"\n\nPROVISIONAL ({_markers(model, e.provisional)})."
        out += block_comment(f"{e.name}: {text}", f"enum {e.name}")
        out.append(f"typedef int32_t {e.name};")
        width = max(len(v.name) for v in e.values)
        vwidth = max(len(_int_literal("int32", v.value)) for v in e.values)
        for v in e.values:
            extra = "".join(f" /* {k}: {val} */" for k, val in v.fields.items())
            lit = _int_literal("int32", v.value)
            out.append(f"#define {v.name.ljust(width)} {lit.ljust(vwidth) if extra else lit}{extra}")
        out.append("")
    out.pop()

    out += _section("constants")
    for c in model.constants.values():
        text = c.doc or c.name
        if c.provisional:
            text += f"\n\nPROVISIONAL ({_markers(model, c.provisional)})."
        out += block_comment(f"{c.name}: {text}", f"constant {c.name}")
        out.append(f"#define {c.name} {_int_literal(c.type, c.value)}")
        out.append("")
    out.pop()

    out += _section("functions")
    for fn in model.functions:
        text = f"{fn.name}: {fn.doc}\n\n{_function_facts(model, fn)}"
        out += block_comment(text, f"function {fn.name}")
        out += prototype_lines(fn)
        out.append("")
    out.pop()

    out += [
        "",
        "#ifdef __cplusplus",
        "}",
        "#endif",
        "",
        "#endif /* CHTYPES_H */",
        "",
    ]
    return "\n".join(out)


def render_exports(model) -> str:
    lines = [
        f"# {banner(model)}",
        "# Every symbol a v1 library exports, one per line, sorted. Nothing else is exported.",
        *model.symbols(),
        "",
    ]
    return "\n".join(lines)


def outputs(model) -> list[Output]:
    return [
        Output(HEADER, content=render_header(model)),
        Output(EXPORTS, content=render_exports(model)),
    ]
