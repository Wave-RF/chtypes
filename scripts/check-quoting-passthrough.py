#!/usr/bin/env python3
"""check-quoting-passthrough.py — no binding may spell a ClickHouse quoting rule itself.

WHY THIS EXISTS. Every one of the four bindings used to carry its own copy of
ClickHouse's identifier quoting — `QuoteIdentifier` in Go, `_backquote_if_needed`
in Python, `backquoteIfNeeded` in TypeScript, `backquote_if_needed` in Rust.
Four copies of one rule, and they were WRONG: the server back-quotes `all`,
`distinct`, `table` and `null`, and every copy left them bare (issue #52). The
copies round-tripped, so nothing caught it; a hand-written rule that parses is
exactly as dangerous as one that does not, because it is silently a DIFFERENT
answer from the server's.

Those copies are deleted. `chs_quote_identifier`, `chs_quote_identifier_if_needed`
and `chs_quote_literal` are the only source of a quoted spelling in this
repository. This script is what keeps that true: it fails if a binding re-grows
a local quoting rule, and it fails if a binding stops DECLARING the symbols it
must call instead.

    scripts/check-quoting-passthrough.py             check the tree
    scripts/check-quoting-passthrough.py --selftest  plant a re-grown rule in
                                                     every binding, and a
                                                     dropped declaration in
                                                     every binding, and prove
                                                     each one fires

================================================================================
THE TWO RULES, AND WHY EACH IS THE SHAPE IT IS
================================================================================

RULE A — no quoting atom in a string or character literal.

  You cannot emit a back-quoted identifier, or a ClickHouse string literal,
  without naming the quote character in your own source. That is a NECESSARY
  condition, not a heuristic, which is why the rule is stated over literals
  rather than over function names: renaming `backquoteIfNeeded` to `spell` does
  not evade it.

  A finding is a string or character literal whose content is

    A1  exactly a quoting atom     ` , `` , \\` , ' , '' , \\'
    A2  a WRAPPER TEMPLATE         starts and ends with a back-quote and is at
                                   most 8 characters — `{}` , `%s` , `{0}` …
    A3  a TEMPLATE wrapper         a template literal whose content starts and
                                   ends with an ESCAPED back-quote and is at
                                   most 24 characters — `\\`${name}\\`` — which
                                   is A2 in TypeScript's other spelling, where
                                   the back-quotes are the delimiters

  What it deliberately does NOT flag: a back-quote inside prose. Error
  messages, notes and doc text in these trees mention `version`, `unlisted.go`
  and `scripts/fetch.sh` constantly — 179 literals in the tree contain a
  back-quote — and banning those would make the rule unusable, which is the
  same as not having it. A1/A2/A3 hold at zero findings across the whole tree.

  Comment LINES are skipped (a line whose first non-space characters open a
  comment), so this file may describe the atoms it forbids. A comment that
  TRAILS code on the same line is still scanned: a plant hidden behind a
  trailing comment is still a plant.

  A3 is narrow on purpose. An escaped back-quote inside PROSE is everywhere in
  these trees — `ts is missing \\`${spelled}\\`` in a parity message, `no
  \\`name\\` field` in a discovery error — so "an escaped back-quote anywhere"
  would have to be switched off, which is the same as not having a rule. Only a
  template whose WHOLE content is the wrapper is a quoting rule.

  The three delimiters nest, so `literals()` lexes rather than pattern-matches:
  a back-quote inside a single-quoted string is a character (and A1 must see
  it), and a `"` inside a Go raw string is a character (and A1 must NOT see a
  phantom literal). Regexes got both of those backwards.

RULE B — every binding DECLARES all three symbols.

  scripts/check-abi-decls.py checks a declaration against the header, but a
  binding that declares nothing at all is REPORTED there, not failed — "not
  declaring a symbol means the binding cannot call it, which is a design
  choice". For these three that is not a design choice: delete the declaration
  and the binding cannot quote, which is precisely the state Rule A exists to
  make unsurvivable. So Rule B requires the declaration, and check-abi-decls
  then requires it to have the right arity and types. The pair is what makes
  "the call goes through the library" checkable without an artifact.

WHAT NEITHER RULE CAN DO. Nothing here proves the three calls RETURN the
server's answer, or that `if needed` matches a given line: that needs a loaded
artifact and is issue #119. These rules prove the only thing a static check
can — that no other answer is being computed here, and that the call is wired.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile

# --------------------------------------------------------------- what is scanned
#
# Library sources, tests and the examples tours: an example that hand-quotes
# teaches the reader to hand-quote, which is the same defect one layer out.

SCAN_DIRS = ("go", "python", "ts/src", "ts/test", "rust/src", "rust/tests", "examples")
SCAN_EXTS = (".go", ".py", ".ts", ".mjs", ".rs")
SKIP_DIRS = {"node_modules", "target", "dist", ".venv", "build", "__pycache__"}

# The FFI declaration source of each binding — the same files
# scripts/check-abi-decls.py reads, plus Go's linked path, which calls the
# symbols directly rather than through a function pointer.
# Each entry carries the pattern that binding uses to WIRE the symbol at
# runtime — a dlsym cast, a direct cgo call, a signature-table key. Not a bare
# mention: every one of these files names the symbols in prose too, and a doc
# comment describing a call is exactly what this must not accept as the call.
DECL_SOURCES = (
    ("go", "go/chtypes/multiversion.go", r'dlsym\(\s*h\s*,\s*"{sym}"\s*\)'),
    ("go-linked", "go/chtypes/linked.go", r"\bC\.{sym}\s*\("),
    ("python", "python/src/chtypes/_native.py", r'"{sym}"\s*:'),
    ("ts", "ts/src/ffi.ts", r"\b{sym}\s*:\s*d\("),
    ("rust", "rust/src/ffi.rs", r'b"{sym}\\0"'),
)

QUOTE_SYMBOLS = (
    "chs_quote_identifier",
    "chs_quote_identifier_if_needed",
    "chs_quote_literal",
)

# --------------------------------------------------------------------- rule A

ATOMS = frozenset({"`", "``", "\\`", "'", "''", "\\'"})
WRAPPER_MAX = 8
TEMPLATE_MAX = 24
COMMENT_OPENERS = ("//", "#", "*", "/*")


def literals(line: str) -> list[tuple[str, str]]:
    """Every string / character / template literal on one line, as (delimiter, content).

    A hand-rolled scanner rather than three regexes, because the three
    delimiters nest: a back-quote inside a single-quoted string is a CHARACTER,
    not the start of a template literal, and a `"` inside a Go raw string is a
    character, not the start of a string. Getting that backwards either
    destroys the finding this exists to make or invents one.

    Per line, so an unterminated literal at end of line is simply dropped —
    conservative in the safe direction: a quoting rule fits on one line.
    """
    out: list[tuple[str, str]] = []
    i, n = 0, len(line)
    while i < n:
        ch = line[i]
        if ch not in "\"'`":
            i += 1
            continue
        delim, body, i = ch, [], i + 1
        closed = False
        while i < n:
            c = line[i]
            if c == "\\" and i + 1 < n:
                body.append(line[i : i + 2])
                i += 2
                continue
            if c == delim:
                closed = True
                i += 1
                break
            body.append(c)
            i += 1
        if closed:
            out.append((delim, "".join(body)))
    return out


def is_comment_line(line: str) -> bool:
    return line.lstrip().startswith(COMMENT_OPENERS)


def scan_text(rel: str, text: str) -> list[str]:
    """Rule A findings in one file's text."""
    findings: list[str] = []
    for n, raw in enumerate(text.splitlines(), start=1):
        if is_comment_line(raw):
            continue
        for delim, body in literals(raw):
            shown = f"{delim}{body}{delim}"
            if delim != "`":
                if body in ATOMS:
                    findings.append(
                        f"{rel}:{n}: the literal {shown} is a SQL quoting atom — "
                        f"this binding is spelling a quoting rule. Call the library's "
                        f"quote_identifier / quote_literal instead (#52)."
                    )
                elif len(body) <= WRAPPER_MAX and body.startswith("`") and body.endswith("`"):
                    findings.append(
                        f"{rel}:{n}: the literal {shown} wraps a value in back-quotes — "
                        f"this binding is spelling a quoting rule. Call the library's "
                        f"quote_identifier instead (#52)."
                    )
            elif len(body) <= TEMPLATE_MAX and body.startswith("\\`") and body.endswith("\\`"):
                # A3: the template-literal spelling of the same wrapper,
                # `\`${name}\``. Narrow on purpose — an escaped back-quote
                # inside PROSE is everywhere in these trees, and a rule that
                # flagged it would have to be switched off, which is the same
                # as not having one.
                findings.append(
                    f"{rel}:{n}: the template literal {shown} wraps a value in back-quotes — "
                    f"this binding is spelling a quoting rule. Call the library's "
                    f"quoteIdentifier instead (#52)."
                )
    return findings


def scanned_files(root: str) -> list[str]:
    """Every file Rule A reads, as repository-relative paths, sorted."""
    out: list[str] = []
    for d in SCAN_DIRS:
        base = os.path.join(root, d)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [x for x in dirnames if x not in SKIP_DIRS]
            for name in filenames:
                if name.endswith(SCAN_EXTS):
                    full = os.path.join(dirpath, name)
                    out.append(os.path.relpath(full, root))
    return sorted(out)


# --------------------------------------------------------------------- rule B


def check_declarations(root: str) -> list[str]:
    findings: list[str] = []
    for label, rel, pattern in DECL_SOURCES:
        path = os.path.join(root, rel)
        try:
            text = open(path, encoding="utf-8").read()
        except OSError as e:
            findings.append(f"{label}: cannot read {rel} ({e}) — refusing to report a pass")
            continue
        for sym in QUOTE_SYMBOLS:
            if not re.search(pattern.format(sym=re.escape(sym)), text):
                findings.append(
                    f"{label}: {rel} does not declare {sym} — the binding cannot call it, "
                    f"so it would have to spell the rule itself (#52)."
                )
    return findings


# ------------------------------------------------------------------- the check


def check(root: str) -> tuple[int, list[str]]:
    files = scanned_files(root)
    lines: list[str] = []
    findings: list[str] = []

    # A scanner that stopped seeing a binding must fail, not report success for
    # what it never read. One anchor per binding, the file each one's quoting
    # would most plausibly re-grow in.
    anchors = (
        "go/chtypes/discover.go",
        "python/src/chtypes/discover.py",
        "ts/src/discover.ts",
        "rust/src/discover.rs",
    )
    for anchor in anchors:
        if anchor not in files:
            findings.append(
                f"the scan did not reach {anchor} — the walker has lost this binding; "
                f"fix it rather than trusting this run"
            )

    for rel in files:
        findings += scan_text(rel, open(os.path.join(root, rel), encoding="utf-8").read())
    findings += check_declarations(root)

    lines.append(f"scanned {len(files)} source file(s) in {len(SCAN_DIRS)} tree(s)")
    for label, rel, _ in DECL_SOURCES:
        lines.append(f"  {label:<10} declares all three chs_quote_* symbols ({rel})")
    return len(findings), lines + ([""] + [f"  {f}" for f in findings] if findings else [])


# -------------------------------------------------------------------- selftest
#
# Each plant is (label, file, find, replace, the substring the finding must
# contain). Every binding gets a re-grown quoting rule AND a dropped
# declaration, because a rule that has only ever passed is not known to work.

PLANTS = (
    (
        "go re-grown rule",
        "go/chtypes/discover.go",
        "\tvar b strings.Builder",
        '\tvar b strings.Builder\n\t_ = "`" + "x" + "`"',
        "go/chtypes/discover.go:",
    ),
    (
        "python re-grown rule",
        "python/src/chtypes/discover.py",
        "    parts: list[str] = []",
        '    parts: list[str] = []\n    _unused = "`" + "x" + "`"',
        "python/src/chtypes/discover.py:",
    ),
    (
        "ts re-grown rule",
        "ts/src/discover.ts",
        "  const parts: string[] = [];",
        "  const parts: string[] = [];\n  const unused = '`' + 'x' + '`';",
        "ts/src/discover.ts:",
    ),
    (
        "ts template quoter",
        "ts/src/discover.ts",
        "  const parts: string[] = [];",
        "  const parts: string[] = [];\n  const unused = `\\`x\\``;",
        "the template literal `\\`x\\`` wraps a value in back-quotes",
    ),
    (
        "rust re-grown rule",
        "rust/src/discover.rs",
        "    let mut out = String::new();",
        '    let mut out = String::new();\n    let _unused = format!("`{}`", "x");',
        "rust/src/discover.rs:",
    ),
    (
        "go declaration dropped",
        "go/chtypes/multiversion.go",
        'dlsym(h, "chs_quote_literal")',
        'dlsym(h, "chs_absent_symbol")',
        "go: go/chtypes/multiversion.go does not declare chs_quote_literal",
    ),
    (
        "go-linked declaration dropped",
        "go/chtypes/linked.go",
        "C.chs_quote_literal(p, n, out, err)",
        "C.chs_absent_symbol(p, n, out, err)",
        "go-linked: go/chtypes/linked.go does not declare chs_quote_literal",
    ),
    (
        "python declaration dropped",
        "python/src/chtypes/_native.py",
        '"chs_quote_literal": (',
        '"chs_absent_symbol": (',
        "python: python/src/chtypes/_native.py does not declare chs_quote_literal",
    ),
    (
        "ts declaration dropped",
        "ts/src/ffi.ts",
        "chs_quote_literal: d(I32,",
        "chs_absent_symbol: d(I32,",
        "ts: ts/src/ffi.ts does not declare chs_quote_literal",
    ),
    (
        "rust declaration dropped",
        "rust/src/ffi.rs",
        'b"chs_quote_literal\\0"',
        'b"chs_absent_symbol\\0"',
        "rust: rust/src/ffi.rs does not declare chs_quote_literal",
    ),
)


def selftest(root: str) -> int:
    n, _ = check(root)
    if n:
        print("SELFTEST FAILED: the tree does not pass, so a planted failure proves nothing", file=sys.stderr)
        return 1

    tmp = tempfile.mkdtemp(prefix="quoting-passthrough-selftest-")
    try:
        for rel in set(scanned_files(root)) | {rel for _, rel, _ in DECL_SOURCES}:
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
            print(f"  plant {label:<28} caught")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("check-quoting-passthrough: selftest ok — a re-grown rule and a dropped declaration fire in every binding")
    return 0


def main(argv: list[str]) -> int:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if "--selftest" in argv:
        return selftest(root)
    n, lines = check(root)
    print("\n".join(lines))
    if n:
        print(
            f"\ncheck-quoting-passthrough: {n} finding(s).\n"
            "  ClickHouse's own backQuote / backQuoteIfNeed / quoteString are reached through\n"
            "  chs_quote_identifier, chs_quote_identifier_if_needed and chs_quote_literal.\n"
            "  A second implementation here is how the `null` divergence happened (#52).",
            file=sys.stderr,
        )
        return 1
    print("\ncheck-quoting-passthrough: ok — no binding spells a quoting rule, and all four declare the symbols")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
