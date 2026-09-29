#!/usr/bin/env python3
"""check-no-error-code-table.py — no binding may carry its own ClickHouse error-code table.

WHY THIS EXISTS. ClickHouse names its error codes in a table that is a property
of the BUILD, and it moves between lines: codes join, codes leave, and one
number can name two different errors on two lines (903 is LICENSE_EXPIRED on
25.3 and 25.8, absent on 25.10, and DISTRIBUTED_CACHE_REGISTRY_SHUTDOWN on 26.2
through 26.9). Revision 6 of the C ABI serves each build's own table through `chs_error_codes`, and every binding
reaches it per loaded library — `Library.ErrorCodes()` / `error_codes()` /
`errorCodes()` / `error_codes()`. A code -> name table written into a binding
would be right for at most one line and silently wrong for the rest, which is
the same failure a hand-written quoting rule was (see
scripts/check-quoting-passthrough.py, which this is modeled on): it would parse,
round-trip, and answer differently from the server.

    scripts/check-no-error-code-table.py             check the tree
    scripts/check-no-error-code-table.py --selftest  plant a table, a name
                                                     literal and a dropped
                                                     declaration in every
                                                     binding, and prove each
                                                     one fires

================================================================================
THE THREE RULES
================================================================================

WHAT IS SCANNED: binding SOURCE under go/, python/, ts/ and rust/ — tests are
excluded (go *_test.go and testdata/, python/tests/, ts/test/, rust/tests/, and
a Rust file's `#[cfg(test)]` items), because a test is exactly where a known
code and its name are supposed to be written down, as an expectation about the
artifact. Python is read by Python's own tokenizer, so a docstring is one prose
literal and a comment is nothing. Go, TypeScript and Rust are lexed a line at a
time: comment LINES are skipped, so prose may name a code, while a comment that
TRAILS code on the same line is still scanned.

RULE A — no known ClickHouse error name as a string literal.

  A string literal whose ENTIRE content is one of the names in KNOWN_NAMES
  below is a finding: a binding that spells "TOO_MANY_PARTS" is either keying
  on a name the table should answer, or re-growing the table one entry at a
  time. The rule is an exact match on purpose — a name inside an error message
  or a doc string is prose, and "a name appears somewhere in a string" would
  have to be switched off, which is the same as not having it. KNOWN_NAMES is
  a sample, not the table: it lives in this script, which is not a binding, and
  it only has to be broad enough that a real table cannot avoid it.

RULE B — no code -> name table shape.

  Several numeric literals each paired with an UPPER_SNAKE string literal, in
  any map, array, struct-literal or switch shape and in either order —
  `252: "TOO_MANY_PARTS"`, `("TOO_MANY_PARTS", 252)`, `{code: 252, name:
  "TOO_MANY_PARTS"}`, `case 252: return "TOO_MANY_PARTS"`, `252 =>
  "TOO_MANY_PARTS"`. A PAIR is a number and an UPPER_SNAKE literal with at most
  PAIR_GAP words or punctuation marks between them and nothing else; a TABLE
  is TABLE_MIN pairs, each starting within RUN_GAP tokens of the previous one.
  That catches a table of names nobody listed in KNOWN_NAMES, which Rule A
  cannot. `CHTYPES_*` literals are exempt: they are this repository's own
  artifact-error vocabulary (docs/guides/fetch.md §6), never a ClickHouse name.

RULE C — every binding DECLARES chs_error_codes.

  Rules A and B prove no second answer is computed here; they cannot prove the
  first one is reachable. A binding that stops declaring the symbol cannot ask
  the library, and would have to carry the table these rules forbid — so the
  declaration is required, in the same files and by the same wiring patterns
  scripts/check-quoting-passthrough.py requires the quoting trio, and
  scripts/check-abi-decls.py then checks its arity and types.

WHAT NONE OF THEM CAN DO. Nothing here proves the table a library RETURNS is
right for its line: that is the artifact producer's build-time comparison with
the vendored src/Common/ErrorCodes.cpp, and each binding's artifact-backed
tests. These rules prove the only thing a static check can — that no other
answer is being computed here, and that the call is wired.
"""

from __future__ import annotations

import ast
import io
import os
import re
import shutil
import sys
import tempfile
import tokenize

# --------------------------------------------------------------- what is scanned

SCAN_DIRS = ("go", "python", "ts", "rust")
SCAN_EXTS = (".go", ".py", ".ts", ".mts", ".mjs", ".js", ".rs")
# Build output, dependencies, and the test trees.
SKIP_DIRS = {
    "node_modules",
    "target",
    "dist",
    ".venv",
    "build",
    "__pycache__",
    "testdata",
    "tests",
    "test",
}

# The same declaration sources, and the same wiring patterns, that
# scripts/check-quoting-passthrough.py requires for the quoting trio: a dlsym
# cast, a direct cgo call, a signature-table key — never a bare mention.
DECL_SOURCES = (
    ("go", "go/chtypes/multiversion.go", r'dlsym\(\s*h\s*,\s*"{sym}"\s*\)'),
    ("go-linked", "go/chtypes/linked.go", r"\bC\.{sym}\s*\("),
    ("python", "python/src/chtypes/_native.py", r'"{sym}"\s*:'),
    ("ts", "ts/src/ffi.ts", r"\b{sym}\s*:\s*d\("),
    ("rust", "rust/src/ffi.rs", r'b"{sym}\\0"'),
)
DECL_SYMBOLS = ("chs_error_codes",)

# ------------------------------------------------------------------- rule A
#
# A sample of ClickHouse's own error names — the ones a binding is likeliest to
# key on, and the ones this repository's own documents name. Not the table:
# see Rule A above.
KNOWN_NAMES = frozenset(
    {
        "UNSUPPORTED_METHOD",
        "UNSUPPORTED_PARAMETER",
        "UNEXPECTED_END_OF_FILE",
        "EXPECTED_END_OF_FILE",
        "CANNOT_PARSE_TEXT",
        "INCORRECT_NUMBER_OF_COLUMNS",
        "THERE_IS_NO_COLUMN",
        "CANNOT_READ_ALL_DATA",
        "ATTEMPT_TO_READ_AFTER_EOF",
        "CANNOT_PARSE_ESCAPE_SEQUENCE",
        "CANNOT_PARSE_QUOTED_STRING",
        "CANNOT_PARSE_INPUT_ASSERTION_FAILED",
        "CANNOT_PARSE_DATE",
        "CANNOT_PARSE_DATETIME",
        "CANNOT_PARSE_NUMBER",
        "CANNOT_PARSE_UUID",
        "CANNOT_PARSE_BOOL",
        "CANNOT_CONVERT_TYPE",
        "ILLEGAL_TYPE_OF_ARGUMENT",
        "ILLEGAL_COLUMN",
        "UNKNOWN_IDENTIFIER",
        "UNKNOWN_FUNCTION",
        "UNKNOWN_TYPE",
        "UNKNOWN_SETTING",
        "UNKNOWN_FORMAT",
        "UNKNOWN_ELEMENT_IN_ENUM",
        "UNKNOWN_QUERY_PARAMETER",
        "BAD_QUERY_PARAMETER",
        "BAD_ARGUMENTS",
        "SYNTAX_ERROR",
        "TYPE_MISMATCH",
        "NO_COMMON_TYPE",
        "INCORRECT_DATA",
        "INCORRECT_QUERY",
        "VIOLATED_CONSTRAINT",
        "TOO_MANY_PARTS",
        "TOO_MANY_PARTITIONS",
        "DATA_TYPE_CANNOT_BE_USED_IN_KEY",
        "ARGUMENT_OUT_OF_BOUND",
        "DECIMAL_OVERFLOW",
        "VALUE_IS_OUT_OF_RANGE_OF_DATA_TYPE",
        "MEMORY_LIMIT_EXCEEDED",
        "TIMEOUT_EXCEEDED",
        "TOO_LARGE_STRING_SIZE",
        "TOO_LARGE_ARRAY_SIZE",
        "SIZES_OF_ARRAYS_DONT_MATCH",
        "DUPLICATE_COLUMN",
        "NO_SUCH_COLUMN_IN_TABLE",
        "ILLEGAL_DIVISION",
        "LOGICAL_ERROR",
        "NOT_IMPLEMENTED",
        "UNKNOWN_EXCEPTION",
        "LICENSE_EXPIRED",
        "DISTRIBUTED_CACHE_REGISTRY_SHUTDOWN",
        "SUSPICIOUS_TYPE_FOR_LOW_CARDINALITY",
        "ILLEGAL_TYPE_OF_COLUMN_FOR_FILTER",
        "EMPTY_DATA_PASSED",
        "CANNOT_INSERT_NULL_IN_ORDINARY_COLUMN",
        "INCORRECT_JSON",
        "CANNOT_READ_ARRAY_FROM_TEXT",
        "CANNOT_READ_MAP_FROM_TEXT",
    }
)

UPPER_SNAKE = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+$")
OWN_VOCABULARY = "CHTYPES_"
COMMENT_OPENERS = ("//", "#", "*", "/*", "--")

# ------------------------------------------------------------------- rule B


class ScanError(Exception):
    """A file could not be read the way this check must read it. Always fatal:
    a check that cannot lex a binding must say so, never pass what it skipped."""


PAIR_GAP = 4  # words / punctuation allowed between a number and its name
RUN_GAP = 24  # tokens allowed between one pair and the next in a table
TABLE_MIN = 3  # pairs that make a table


def is_comment_line(line: str) -> bool:
    return line.lstrip().startswith(COMMENT_OPENERS)


def _number_or_word(line: str, i: int) -> tuple[tuple[str, str], int]:
    j = i
    while j < len(line) and (line[j].isalnum() or line[j] in "._"):
        j += 1
    text = line[i:j]
    return (("NUM" if re.fullmatch(r"\d+", text.replace("_", "")) else "WORD"), text), j


def tokens(line: str, lang: str) -> list[tuple[str, str]]:
    """One line of Go, TypeScript or Rust as (kind, text) tokens: STR (a
    literal's content), NUM, WORD, P.

    Literals are lexed rather than pattern-matched, for the reason
    check-quoting-passthrough.py gives: the delimiters nest, and a regex gets a
    quote inside a literal backwards. Per line, so an unterminated literal at
    end of line is dropped — a table entry fits on a line. Rust is the one
    language whose `'` is not a string: a char literal is skipped whole and a
    lifetime's `'` alone, and a one-line raw string r#"…"# is read as one
    literal. Python never comes here; `python_tokens` uses Python's own
    tokenizer.
    """
    delims = '"' if lang == "rust" else "\"'`"
    out: list[tuple[str, str]] = []
    i, n = 0, len(line)
    while i < n:
        ch = line[i]
        if lang == "rust" and ch == "'":
            m = re.match(r"'(?:\\.[^']*|[^\\'])'", line[i:])
            i += m.end() if m else 1
            continue
        # A raw string starts a token (`r#"`), or follows a byte prefix (`br#"`).
        starts_token = i == 0 or not (line[i - 1].isalnum() or line[i - 1] == "_")
        if lang == "rust" and ch == "r" and (starts_token or line[i - 1] == "b"):
            m = re.match(r'r(#+)"', line[i:])
            if m:
                close = '"' + m.group(1)
                k = line.find(close, i + m.end())
                if k >= 0:
                    out.append(("STR", line[i + m.end() : k]))
                    i = k + len(close)
                    continue
        if ch in delims:
            delim, body, j, closed = ch, [], i + 1, False
            while j < n:
                c = line[j]
                if c == "\\" and j + 1 < n:
                    body.append(line[j : j + 2])
                    j += 2
                    continue
                if c == delim:
                    closed = True
                    j += 1
                    break
                body.append(c)
                j += 1
            if closed:
                out.append(("STR", "".join(body)))
            i = j
            continue
        if ch.isdigit():
            tok, i = _number_or_word(line, i)
            out.append(tok)
            continue
        if ch.isalpha() or ch == "_":
            j = i
            while j < n and (line[j].isalnum() or line[j] == "_"):
                j += 1
            out.append(("WORD", line[i:j]))
            i = j
            continue
        if not ch.isspace():
            out.append(("P", ch))
        i += 1
    return out


def python_tokens(text: str) -> list[tuple[str, str, int]]:
    """A Python file through Python's own tokenizer: every string literal's
    VALUE (a docstring is one prose literal, never a pile of fragments), every
    number, every name and operator, and no comment."""
    out: list[tuple[str, str, int]] = []
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, SyntaxError) as e:
        raise ScanError(f"python tokenizer refused the file: {e}") from e
    for t in toks:
        line = t.start[0]
        if t.type == tokenize.STRING:
            try:
                value = ast.literal_eval(t.string)
            except (ValueError, SyntaxError):
                continue  # an f-string on an interpreter that tokenizes it whole
            if isinstance(value, bytes):
                value = value.decode("latin-1")
            out.append(("STR", value, line))
        elif t.type == tokenize.NUMBER:
            kind = "NUM" if re.fullmatch(r"\d+", t.string.replace("_", "")) else "WORD"
            out.append((kind, t.string, line))
        elif t.type == tokenize.NAME:
            out.append(("WORD", t.string, line))
        elif t.type == tokenize.OP:
            out.append(("P", t.string, line))
    return out


def strip_rust_test_items(text: str) -> str:
    """Blank every `#[cfg(test)]` item of a Rust file, keeping line numbers.

    rustfmt shapes the item — `cargo fmt --check` gates the crate — so it ends
    at the first line that is the attribute's own indentation followed by `}`,
    or on its first line when that line ends in `;`. Braces are NOT counted: a
    JSON fixture inside a multi-line raw string would unbalance them.
    """
    out: list[str] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.strip() != "#[cfg(test)]":
            out.append(line)
            i += 1
            continue
        indent = line[: len(line) - len(line.lstrip())]
        out.append("")
        i += 1
        if i < len(lines) and lines[i].rstrip().endswith(";"):
            out.append("")
            i += 1
            continue
        while i < len(lines):
            done = lines[i].rstrip() == indent + "}"
            out.append("")
            i += 1
            if done:
                break
    return "\n".join(out)


def stream_of(rel: str, text: str) -> list[tuple[str, str, int]]:
    """One file as a token stream: (kind, text, line)."""
    if rel.endswith(".py"):
        return python_tokens(text)
    lang = "rust" if rel.endswith(".rs") else "c-like"
    if lang == "rust":
        text = strip_rust_test_items(text)
    stream: list[tuple[str, str, int]] = []
    for n, raw in enumerate(text.splitlines(), start=1):
        if is_comment_line(raw):
            continue
        stream.extend((kind, tok, n) for kind, tok in tokens(raw, lang))
    return stream


def scan_text(rel: str, text: str) -> list[str]:
    """Rule A and Rule B findings in one file's text."""
    findings: list[str] = []
    stream = stream_of(rel, text)
    for kind, tok, n in stream:
        if kind == "STR" and tok in KNOWN_NAMES:
            findings.append(
                f'{rel}:{n}: the literal "{tok}" is a ClickHouse error name — this binding is '
                f"keying on, or re-growing, an error-code table. Ask the loaded library "
                f"(Library error codes, revision 6) instead."
            )

    # Rule B: pairs, then runs of pairs.
    pairs: list[tuple[int, int, int]] = []  # (start index, end index, line)
    for i, (kind, tok, line) in enumerate(stream):
        if kind not in ("NUM", "STR"):
            continue
        for j in range(i + 1, min(len(stream), i + PAIR_GAP + 2)):
            k2, t2, _ = stream[j]
            if k2 in ("NUM", "STR"):
                if {kind, k2} == {"NUM", "STR"}:
                    name = tok if kind == "STR" else t2
                    if UPPER_SNAKE.match(name) and not name.startswith(OWN_VOCABULARY):
                        pairs.append((i, j, line))
                break
    run: list[tuple[int, int, int]] = []
    reported_lines: set[int] = set()
    for p in pairs:
        if run and p[0] - run[-1][1] > RUN_GAP:
            run = []
        if run and p[0] <= run[-1][1]:
            continue  # overlaps the previous pair (a name between two numbers)
        run.append(p)
        if len(run) == TABLE_MIN and run[0][2] not in reported_lines:
            reported_lines.add(run[0][2])
            findings.append(
                f"{rel}:{run[0][2]}: {TABLE_MIN} or more numeric literals paired with UPPER_SNAKE "
                f"names (lines {', '.join(str(x[2]) for x in run)}) — this binding carries a code -> "
                f"name table. The table is the loaded build's own: ask the library."
            )
    return findings


def scanned_files(root: str) -> list[str]:
    """Every file Rules A and B read, as repository-relative paths, sorted."""
    out: list[str] = []
    for d in SCAN_DIRS:
        base = os.path.join(root, d)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [x for x in dirnames if x not in SKIP_DIRS]
            for name in filenames:
                if name.endswith(SCAN_EXTS) and not name.endswith("_test.go"):
                    out.append(os.path.relpath(os.path.join(dirpath, name), root))
    return sorted(out)


# ------------------------------------------------------------------- rule C


def check_declarations(root: str) -> list[str]:
    findings: list[str] = []
    for label, rel, pattern in DECL_SOURCES:
        path = os.path.join(root, rel)
        try:
            text = open(path, encoding="utf-8").read()
        except OSError as e:
            findings.append(f"{label}: cannot read {rel} ({e}) — refusing to report a pass")
            continue
        for sym in DECL_SYMBOLS:
            if not re.search(pattern.format(sym=re.escape(sym)), text):
                findings.append(
                    f"{label}: {rel} does not declare {sym} — the binding cannot ask the library "
                    f"for its table, so it would have to carry one."
                )
    return findings


# ------------------------------------------------------------------- the check


def check(root: str) -> tuple[int, list[str]]:
    files = scanned_files(root)
    findings: list[str] = []

    # A scanner that stopped seeing a binding must fail, not report success for
    # what it never read: one anchor per binding, the file its table would
    # most plausibly grow in.
    anchors = (
        "go/chtypes/error_codes.go",
        "python/src/chtypes/_error_codes.py",
        "ts/src/error-codes.ts",
        "rust/src/error_codes.rs",
    )
    for anchor in anchors:
        if anchor not in files:
            findings.append(
                f"the scan did not reach {anchor} — the walker has lost this binding; fix it "
                f"rather than trusting this run"
            )
    # And it must not be reading the test trees it promises to skip.
    leaked = [f for f in files if f.endswith("_test.go") or "/tests/" in f or "/test/" in f]
    for f in leaked:
        findings.append(f"the scan read {f}, a test file it promises to skip")

    for rel in files:
        try:
            findings += scan_text(rel, open(os.path.join(root, rel), encoding="utf-8").read())
        except ScanError as e:
            raise ScanError(f"{rel}: {e}") from e
    findings += check_declarations(root)

    lines = [f"scanned {len(files)} binding source file(s) under {', '.join(SCAN_DIRS)} (tests excluded)"]
    for label, rel, _ in DECL_SOURCES:
        lines.append(f"  {label:<10} declares chs_error_codes ({rel})")
    return len(findings), lines + ([""] + [f"  {f}" for f in findings] if findings else [])


# -------------------------------------------------------------------- selftest
#
# Each plant is (label, file, find, replace, the substring the finding must
# contain). Every binding gets a planted table AND a name literal AND a dropped
# declaration, because a rule that has only ever passed is not known to work.
# Two controls follow: the same table planted in a test file, and a name inside
# prose, must both stay silent.

TABLE_FINDING = "numeric literals paired with UPPER_SNAKE names"

PLANTS = (
    (
        "go table",
        "go/chtypes/error_codes.go",
        "type ErrorCodeEntry struct {",
        'var codeNames = map[int]string{\n\t0: "OK",\n\t1: "FIRST_ERROR",\n\t47: "SOME_ERROR",\n'
        '\t62: "OTHER_ERROR",\n}\n\ntype ErrorCodeEntry struct {',
        TABLE_FINDING,
    ),
    (
        "go switch table",
        "go/chtypes/error_codes.go",
        "type ErrorCodeEntry struct {",
        'func nameOf(c int) string {\n\tswitch c {\n\tcase 36:\n\t\treturn "BAD_ARG_X"\n\tcase 44:\n'
        '\t\treturn "ILLEGAL_X"\n\tcase 62:\n\t\treturn "SYNTAX_X"\n\t}\n\treturn ""\n}\n\n'
        "type ErrorCodeEntry struct {",
        TABLE_FINDING,
    ),
    (
        "go name literal",
        "go/chtypes/error_codes.go",
        "type ErrorCodeEntry struct {",
        'const tooMany = "TOO_MANY_PARTS"\n\ntype ErrorCodeEntry struct {',
        'the literal "TOO_MANY_PARTS" is a ClickHouse error name',
    ),
    (
        "python table",
        "python/src/chtypes/_error_codes.py",
        '__all__ = ["ErrorCodeEntry", "ErrorCodeTable"]',
        '__all__ = ["ErrorCodeEntry", "ErrorCodeTable"]\n\n_NAMES = {\n    ("A_ERROR", 1),\n    ("B_ERROR", 2),\n'
        '    ("C_ERROR", 3),\n}',
        TABLE_FINDING,
    ),
    (
        "python name literal",
        "python/src/chtypes/_error_codes.py",
        '__all__ = ["ErrorCodeEntry", "ErrorCodeTable"]',
        '__all__ = ["ErrorCodeEntry", "ErrorCodeTable"]\n\nif True:\n    _X = "SYNTAX_ERROR"',
        'the literal "SYNTAX_ERROR" is a ClickHouse error name',
    ),
    (
        "ts table",
        "ts/src/error-codes.ts",
        "export interface ErrorCodeEntry {",
        "const NAMES = [\n  { code: 1, name: 'A_ERROR' },\n  { code: 2, name: 'B_ERROR' },\n"
        "  { code: 3, name: 'C_ERROR' },\n];\n\nexport interface ErrorCodeEntry {",
        TABLE_FINDING,
    ),
    (
        "ts name literal",
        "ts/src/error-codes.ts",
        "export interface ErrorCodeEntry {",
        "const X = 'UNKNOWN_IDENTIFIER';\n\nexport interface ErrorCodeEntry {",
        'the literal "UNKNOWN_IDENTIFIER" is a ClickHouse error name',
    ),
    (
        "rust table",
        "rust/src/error_codes.rs",
        "/// One row of a build's error-code table",
        'pub(crate) fn name_of(c: i32) -> &\'static str {\n    match c {\n        1 => "A_ERROR",\n'
        '        2 => "B_ERROR",\n        3 => "C_ERROR",\n        _ => "",\n    }\n}\n\n'
        "/// One row of a build's error-code table",
        TABLE_FINDING,
    ),
    (
        "rust name literal",
        "rust/src/error_codes.rs",
        "/// One row of a build's error-code table",
        'pub(crate) const X: &str = "TOO_MANY_PARTS";\n\n/// One row of a build\'s error-code table',
        'the literal "TOO_MANY_PARTS" is a ClickHouse error name',
    ),
    (
        "go declaration dropped",
        "go/chtypes/multiversion.go",
        'dlsym(h, "chs_error_codes")',
        'dlsym(h, "chs_absent_symbol")',
        "go: go/chtypes/multiversion.go does not declare chs_error_codes",
    ),
    (
        "go-linked declaration dropped",
        "go/chtypes/linked.go",
        "C.chs_error_codes()",
        "C.chs_absent_symbol()",
        "go-linked: go/chtypes/linked.go does not declare chs_error_codes",
    ),
    (
        "python declaration dropped",
        "python/src/chtypes/_native.py",
        '"chs_error_codes": (',
        '"chs_absent_symbol": (',
        "python: python/src/chtypes/_native.py does not declare chs_error_codes",
    ),
    (
        "ts declaration dropped",
        "ts/src/ffi.ts",
        "chs_error_codes: d(",
        "chs_absent_symbol: d(",
        "ts: ts/src/ffi.ts does not declare chs_error_codes",
    ),
    (
        "rust declaration dropped",
        "rust/src/ffi.rs",
        'b"chs_error_codes\\0"',
        'b"chs_absent_symbol\\0"',
        "rust: rust/src/ffi.rs does not declare chs_error_codes",
    ),
)

# Controls: each must leave the check GREEN. (label, file, find, replace)
CONTROLS = (
    (
        "a table inside a Rust #[cfg(test)] module",
        "rust/src/error_codes.rs",
        "#[cfg(test)]\nmod tests {",
        '#[cfg(test)]\nmod tests {\n    const T: [(i32, &str); 3] = [(1, "A_ERROR"), (2, "B_ERROR"), (3, "TOO_MANY_PARTS")];',
    ),
    (
        "a name inside prose",
        "go/chtypes/error_codes.go",
        "type ErrorCodeEntry struct {",
        'var proseOnly = "a 252 TOO_MANY_PARTS refusal is an ordinary rejection"\n\ntype ErrorCodeEntry struct {',
    ),
    (
        "this repository's own CHTYPES_ vocabulary beside numbers",
        "go/chtypes/error_codes.go",
        "type ErrorCodeEntry struct {",
        'var exits = map[string]int{\n\t"CHTYPES_A_B": 1,\n\t"CHTYPES_C_D": 2,\n\t"CHTYPES_E_F": 3,\n}\n\n'
        "type ErrorCodeEntry struct {",
    ),
)


def _apply(path: str, find: str, repl: str, label: str) -> str | None:
    original = open(path, encoding="utf-8").read()
    if original.count(find) != 1:
        print(
            f"SELFTEST FAILED: the {label} plant's anchor is not in {path} exactly once "
            f"(found {original.count(find)}) — the source moved; update PLANTS/CONTROLS",
            file=sys.stderr,
        )
        return None
    open(path, "w", encoding="utf-8").write(original.replace(find, repl))
    return original


def selftest(root: str) -> int:
    n, lines = check(root)
    if n:
        print(
            "SELFTEST FAILED: the tree itself does not pass, so a planted failure would prove\n"
            "nothing. The findings below are REAL — fix them first:\n" + "\n".join(lines),
            file=sys.stderr,
        )
        return 1

    tmp = tempfile.mkdtemp(prefix="no-error-code-table-selftest-")
    try:
        for rel in set(scanned_files(root)) | {rel for _, rel, _ in DECL_SOURCES}:
            dst = os.path.join(tmp, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(os.path.join(root, rel), dst)
        # A test tree, so the skip is exercised: a planted table there must NOT fire.
        os.makedirs(os.path.join(tmp, "python", "tests"), exist_ok=True)
        open(os.path.join(tmp, "python", "tests", "test_planted.py"), "w", encoding="utf-8").write(
            'T = {1: "A_ERROR", 2: "B_ERROR", 3: "TOO_MANY_PARTS"}\n'
        )

        n, lines = check(tmp)
        if n:
            print("SELFTEST FAILED: the copied tree does not pass\n" + "\n".join(lines), file=sys.stderr)
            return 1

        for label, rel, find, repl, want in PLANTS:
            path = os.path.join(tmp, rel)
            original = _apply(path, find, repl, label)
            if original is None:
                return 1
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
            print(f"  plant   {label:<52} caught")

        for label, rel, find, repl in CONTROLS:
            path = os.path.join(tmp, rel)
            original = _apply(path, find, repl, label)
            if original is None:
                return 1
            try:
                n, lines = check(tmp)
            finally:
                open(path, "w", encoding="utf-8").write(original)
            if n:
                print(
                    f"SELFTEST FAILED: the control '{label}' fired, and must not:\n" + "\n".join(lines),
                    file=sys.stderr,
                )
                return 1
            print(f"  control {label:<52} silent")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(
        "check-no-error-code-table: selftest ok — a planted table, a name literal and a dropped "
        "declaration fire in every binding; a test file, prose and the CHTYPES_ vocabulary stay silent"
    )
    return 0


def main(argv: list[str]) -> int:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        if "--selftest" in argv:
            return selftest(root)
        n, lines = check(root)
    except ScanError as e:
        print(f"check-no-error-code-table: {e}", file=sys.stderr)
        return 2
    print("\n".join(lines))
    if n:
        print(
            f"\ncheck-no-error-code-table: {n} finding(s).\n"
            "  ClickHouse's error-code table is a property of each build, and moves between lines\n"
            "  (903 names two different errors on 25.8 and 26.2). Every binding asks the loaded\n"
            "  library through chs_error_codes; a table written here is right for one line at most.",
            file=sys.stderr,
        )
        return 1
    print("\ncheck-no-error-code-table: ok — no binding carries a code -> name table, and all four declare chs_error_codes")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
