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

Those copies are deleted. `chs_back_quote`, `chs_back_quote_if_needed` and
`chs_quote_string` (ABI v1) are the only source of a quoted spelling in this
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
  and script names constantly — 179 literals in the tree contain a
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

  Each binding's declaration layer is GENERATED from spec/abi-v1/abi.json, so
  scripts/abi-v1/gen.py --check already proves every declaration matches the
  header. It cannot prove the description still DESCRIBES these three: drop
  one from the description and the binding cannot quote, which is precisely
  the state Rule A exists to make unsurvivable. So Rule B requires the
  generated declaration of each symbol in each binding, and the pair is what
  makes "the call goes through the library" checkable without an artifact.

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

# SCAN_DIRS below walks go/, python/, ts/src, ts/test, rust/src and
# rust/tests wholesale, which now includes each binding's generated */abi1/*
# declaration layer (scripts/abi-v1/gen.py's output). A generated file is
# exempt from Rule A the same way scripts/check-no-error-code-table.py and
# scripts/abi-v1/check-no-hand-decls.py exempt one: the path must be one of
# gen.produced_outputs() AND carry BANNER_RE, both imported rather than
# re-derived so all three checks agree on what "really generated" means.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "abi-v1"))
from emit import BANNER_RE  # noqa: E402
from gen import produced_outputs  # noqa: E402

# --------------------------------------------------------------- what is scanned
#
# Library sources, tests and the examples tours: an example that hand-quotes
# teaches the reader to hand-quote, which is the same defect one layer out.

SCAN_DIRS = ("go", "python", "ts/src", "ts/test", "rust/src", "rust/tests", "examples")
SCAN_EXTS = (".go", ".py", ".ts", ".mjs", ".rs")
# build and dist are walked on purpose: a banner copied into a hand-written file
# under one is not exempt (the exemption needs a produced path).
SKIP_DIRS = {"node_modules", "target", ".venv", "__pycache__"}

# The generated FFI declaration layer of each binding (scripts/abi-v1/gen.py's
# output, banner-exempt from Rule A) and the pattern that layer uses to WIRE a
# symbol at runtime: a dlsym cast, a direct linked address, a declaration-table
# row. Not a bare mention: every one of these files names the symbols in prose
# too, and a doc comment describing a call is exactly what this must not
# accept as the call.
DECL_SOURCES = (
    ("go", "go/internal/abi1/abi_gen.go", r'dlsym\(\s*h\s*,\s*"{sym}"\s*\)'),
    ("go-linked", "go/internal/abi1/linked_gen.go", r"&{sym}\s*;"),
    ("python", "python/src/chtypes/_abi1/_decls.py", r'_add\(\s*"{sym}"'),
    ("ts", "ts/src/abi1/decls.gen.ts", r'"{sym}"\s*:\s*\{{'),
    ("rust", "rust/src/abi1/decls.rs", r'concat!\(\s*"{sym}"'),
)

QUOTE_SYMBOLS = (
    "chs_back_quote",
    "chs_back_quote_if_needed",
    "chs_quote_string",
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


def check(root: str, produced: frozenset[str] | None = None) -> tuple[int, list[str]]:
    files = scanned_files(root)
    if produced is None:
        produced = produced_outputs()
    lines: list[str] = []
    findings: list[str] = []

    # A scanner that stopped seeing a binding must fail, not report success for
    # what it never read. One anchor per binding, the file each one's quoting
    # would most plausibly re-grow in.
    anchors = (
        "go/internal/abi1/loader.go",
        "python/src/chtypes/_abi1/_loader.py",
        "ts/src/abi1/loader.ts",
        "rust/src/abi1/loader.rs",
    )
    for anchor in anchors:
        if anchor not in files:
            findings.append(
                f"the scan did not reach {anchor} — the walker has lost this binding; "
                f"fix it rather than trusting this run"
            )

    for rel in files:
        text = open(os.path.join(root, rel), encoding="utf-8").read()
        if rel in produced and BANNER_RE.search(text[:512]):
            continue  # scripts/abi-v1/gen.py's own output
        findings += scan_text(rel, text)
    findings += check_declarations(root)

    lines.append(f"scanned {len(files)} source file(s) in {len(SCAN_DIRS)} tree(s)")
    for label, rel, _ in DECL_SOURCES:
        lines.append(f"  {label:<10} declares all three quoting symbols ({rel})")
    return len(findings), lines + ([""] + [f"  {f}" for f in findings] if findings else [])


# -------------------------------------------------------------------- selftest
#
# Each plant is (label, file, find, replace, the substring the finding must
# contain). Every binding gets a re-grown quoting rule AND a dropped
# declaration, because a rule that has only ever passed is not known to work.

# A fabricated stand-in for a generated */abi1/* declaration file, proving
# the generated-file exemption (BANNER_RE, imported above) in both
# directions with the SAME literal, varying only whether a real banner is
# present. Written directly into the selftest's temp tree (not a real file
# in this repository today): the baseline check(tmp) call right after it is
# created is the "banner present -> silent" control; the PLANT below strips
# the banner and expects the atom to fire.
GENERATED_GO_DECLS = "go/internal/abi1/decls.go"
GENERATED_BANNER_LINE = (
    "// GENERATED by scripts/abi-v1/gen.py from spec/abi-v1/abi.json "
    "(CHS_ABI_FINGERPRINT sha256:" + "a" * 64 + ") — DO NOT EDIT"
)
GENERATED_QUOTE_ATOM = 'package abi1\n\nvar _ = "`"\n'

# A plant with find=None creates a NEW file in the copied tree holding `repl`,
# so a rule-A plant never depends on what any hand-written file happens to
# say; a plant with a find edits a GENERATED declaration file, whose text is
# fixed by spec/abi-v1/abi.json.
PLANTS = (
    (
        "go re-grown rule",
        "go/internal/abi1/zz_planted.go",
        None,
        'package abi1\n\nvar _ = "`" + "x" + "`"\n',
        "go/internal/abi1/zz_planted.go:",
    ),
    (
        "python re-grown rule",
        "python/src/chtypes/_abi1/_planted.py",
        None,
        '_unused = "`" + "x" + "`"\n',
        "python/src/chtypes/_abi1/_planted.py:",
    ),
    (
        "ts re-grown rule",
        "ts/src/abi1/planted.ts",
        None,
        "export const unused = '`' + 'x' + '`';\n",
        "ts/src/abi1/planted.ts:",
    ),
    (
        "ts template quoter",
        "ts/src/abi1/planted.ts",
        None,
        "export const unused = `\\`x\\``;\n",
        "the template literal `\\`x\\`` wraps a value in back-quotes",
    ),
    (
        "rust re-grown rule",
        "rust/src/abi1/planted.rs",
        None,
        'pub(crate) fn unused() -> String {\n    format!("`{}`", "x")\n}\n',
        "rust/src/abi1/planted.rs:",
    ),
    (
        "go declaration dropped",
        "go/internal/abi1/abi_gen.go",
        'dlsym(h, "chs_quote_string")',
        'dlsym(h, "chs_absent_symbol")',
        "go: go/internal/abi1/abi_gen.go does not declare chs_quote_string",
    ),
    (
        "go-linked declaration dropped",
        "go/internal/abi1/linked_gen.go",
        "&chs_quote_string;",
        "&chs_absent_symbol;",
        "go-linked: go/internal/abi1/linked_gen.go does not declare chs_quote_string",
    ),
    (
        "python declaration dropped",
        "python/src/chtypes/_abi1/_decls.py",
        '    "chs_quote_string",\n    "status",',
        '    "chs_absent_symbol",\n    "status",',
        "python: python/src/chtypes/_abi1/_decls.py does not declare chs_quote_string",
    ),
    (
        "ts declaration dropped",
        "ts/src/abi1/decls.gen.ts",
        '"chs_quote_string": {',
        '"chs_absent_symbol": {',
        "ts: ts/src/abi1/decls.gen.ts does not declare chs_quote_string",
    ),
    (
        "rust declaration dropped",
        "rust/src/abi1/decls.rs",
        'concat!("chs_quote_string", "\\0")',
        'concat!("chs_absent_symbol", "\\0")',
        "rust: rust/src/abi1/decls.rs does not declare chs_quote_string",
    ),
    (
        "generated banner stripped from a would-be-generated file",
        GENERATED_GO_DECLS,
        GENERATED_BANNER_LINE,
        "// NOT a generated banner",
        "is a SQL quoting atom",
    ),
)


def selftest(root: str) -> int:
    n, lines = check(root)
    if n:
        # The findings go out too. This branch is what a REAL re-grown rule
        # trips first (the selftest step runs before the bare one), and a red
        # log that says only "the tree does not pass" sends the reader back to
        # run the check by hand to learn which line it was.
        print(
            "SELFTEST FAILED: the tree itself does not pass, so a planted failure would prove\n"
            "nothing. The findings below are REAL — fix them first:\n" + "\n".join(lines),
            file=sys.stderr,
        )
        return 1

    tmp = tempfile.mkdtemp(prefix="quoting-passthrough-selftest-")
    try:
        for rel in set(scanned_files(root)) | {rel for _, rel, _ in DECL_SOURCES}:
            dst = os.path.join(tmp, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(os.path.join(root, rel), dst)
        os.makedirs(os.path.join(tmp, os.path.dirname(GENERATED_GO_DECLS)), exist_ok=True)
        open(os.path.join(tmp, GENERATED_GO_DECLS), "w", encoding="utf-8").write(
            GENERATED_BANNER_LINE + "\n" + GENERATED_QUOTE_ATOM
        )

        # The copy itself must still pass: a plant that fires against a tree
        # that was already failing proves nothing. This is ALSO the
        # generated-file exemption's "banner present -> silent" control.
        # What the emitters produce, standing in for the real set (the real
        # outputs, so a real generated file copied into the tree stays exempt) plus
        # the fabricated file above.
        produced = produced_outputs() | {GENERATED_GO_DECLS}
        n, lines = check(tmp, produced)
        if n:
            print("SELFTEST FAILED: the copied tree does not pass\n" + "\n".join(lines), file=sys.stderr)
            return 1

        # The same real banner and atom in a hand-written file under a build
        # directory inside a binding, which is not a produced path: the walk
        # must reach it and the exemption must refuse it.
        built = os.path.join(tmp, "go", "internal", "abi1", "build", "decls.go")
        os.makedirs(os.path.dirname(built), exist_ok=True)
        open(built, "w", encoding="utf-8").write(GENERATED_BANNER_LINE + "\n" + GENERATED_QUOTE_ATOM)
        n, lines = check(tmp, produced)
        if n == 0 or "go/internal/abi1/build/decls.go" not in "\n".join(lines):
            print("SELFTEST FAILED: a bannered hand-written file under build/ was exempted or never walked", file=sys.stderr)
            return 1
        print(f"  plant {'bannered hand-written file under build/':<28} caught")
        os.remove(built)

        for label, rel, find, repl, want in PLANTS:
            path = os.path.join(tmp, rel)
            if find is None:
                original = None
                os.makedirs(os.path.dirname(path), exist_ok=True)
                open(path, "w", encoding="utf-8").write(repl)
            else:
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
                n, lines = check(tmp, produced)
            finally:
                if original is None:
                    os.remove(path)
                else:
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
            "  chs_back_quote, chs_back_quote_if_needed and chs_quote_string.\n"
            "  A second implementation here is how the `null` divergence happened (#52).",
            file=sys.stderr,
        )
        return 1
    print("\ncheck-quoting-passthrough: ok — no binding spells a quoting rule, and all four declare the symbols")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
