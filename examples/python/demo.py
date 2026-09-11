"""The Python tour of chtypes.

WHAT THIS IS

chtypes answers one question: "if this row were inserted into this table on
this ClickHouse version, what would happen?" — without a server. The answers
come from ClickHouse's own C++ (vendored per release into shared libraries
behind a 22-function C ABI), which is why they are exact rather than
approximately right.

This file is a tutorial you RUN. Fourteen numbered sections walk the whole
public API of the Python SDK, from loading an artifact to tearing down, each
with a comment saying what it demonstrates, why an ingest pipeline cares, and
what to look at in the output. The same fourteen sections — same numbering,
same schemas, same rows — exist in go/main.go, ts/demo.mjs and
rust/src/main.rs, so you can diff two tours and see only the language idioms
differ.

EVERYTHING HERE IS OFFLINE. You need uv (or any Python >= 3.11 with the
bindings installed) and artifacts in the registry (scripts/fetch.sh, or a core build;
<version>`) — no Docker, no ClickHouse server, no network. Even the
discovery-kit section (11) runs offline, against CANNED bytes shaped exactly
like a real server's responses.

    uv run demo.py                        # newest vendored version
    CHTYPES_VERSION=25.8 uv run demo.py   # pick a line
    ../chplay.sh python                   # same, with prerequisite checks

Nothing here is a test — the real suites live in python/tests.
Every number printed below is produced by the run, never written down by hand.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import chtypes
from chtypes import Format, Registry

# ------------------------------------------------------------------ the fixture
#
# These constants are IDENTICAL in all four playgrounds. Change one here and
# you must change it in go/main.go, ts/demo.mjs and rust/src/main.rs too — the
# point of this directory is that the four outputs can be diffed.

# The tenant's table for the DEFAULT/result sections: one of everything the
# tour needs — a volatile DEFAULT (now64), a literal DEFAULT, an Enum (how a
# table gets poisoned), and a MATERIALIZED column (never in SELECT *).
DEMO_DDL = """ts DateTime64(3) DEFAULT now64(3),
device_id UInt32,
seq UInt8 DEFAULT 0,
payload String,
grade Enum8('a' = 1, 'b' = 2),
payload_len UInt32 MATERIALIZED length(payload)"""

# A small three-column table for the outcome and format sections. Positional
# formats (CSV, TSV, Values, RowBinary...) are far easier to read against a
# small schema, and both DEFAULTs give the empty-field rules something to do.
FORMAT_DDL = "device_id UInt32, seq UInt8 DEFAULT 7, label String DEFAULT 'unknown'"

# Pinning the clock is what makes a demo with now64(3) in it reproducible.
# The value is a STRING at the boundary, always: 19 digits do not survive an
# IEEE double, and a JSON number here would be silently ignored.
PINNED_CLOCK = "1700000000000000000"  # 2023-11-14 22:13:20 UTC

# Hand-built binary payloads (hex), shared by all four tours. Each is
# explained where it is fed. The RBWD payloads are the committed fixtures
# from tests/fixtures/rbwd/, whose value bytes a real ClickHouse wrote.
ROW_BINARY_OK = "0100000007026f6b"  # UInt32 LE 1, UInt8 7, varint-len "ok"
RBWD_MARKER = "00020000000100026869"  # device_id=2 by value, seq by marker, label="hi"
RBWD_POISON = "000100000001"  # id=1 by value, e by marker -> raw 0, NO name
RBWD_VALUE = "00010000000002"  # id=1 by value, e by value 2 ("green")
# RowBinaryWithNamesAndTypesAndDefaults: LEB128 column count, names, types,
# then RBWD-style marker+value rows. Built by hand for FORMAT_DDL:
# 3 cols, device_id=1 by value, seq by marker (DEFAULT 7), label="ok".
RBWNTD_ROW = (
    "03" + "096465766963655f6964" + "03736571" + "056c6162656c"
    + "0655496e743332" + "0555496e7438" + "06537472696e67"
    + "00" + "01000000" + "01" + "00" + "026f6b"
)
# Native blocks captured from a live 25.8 (SELECT ... FORMAT Native).
NATIVE_OK = "0301096465766963655f69640655496e74333201000000037365710555496e743807056c6162656c06537472696e67026f6b"
NATIVE_CAST = "0301096465766963655f69640655496e743332020000000373657106537472696e6703323030056c6162656c06537472696e670463617374"
# Buffers: uint64le n_columns, n_rows, then per column a byte size and the
# raw column. 4 bytes ff ff ff ff — declared 4 wide, then (wrongly) 8 wide.
BUFFERS_OK = "010000000000000001000000000000000400000000000000ffffffff"
BUFFERS_WIDE = "010000000000000001000000000000000800000000000000ffffffff"

# Section 11's CANNED server responses. These are not live bytes — they are
# shaped EXACTLY like a real ClickHouse's JSONEachRow answers to the three
# discovery queries (quoted UInt64s and all, matching a stock HTTP server's
# output_format_json_quote_64bit_integers=1). Swap in your own HTTP client's
# bytes and nothing else changes.
CANNED_VERSION_RESULT = b'{"version":"25.8.28.1"}\n'
CANNED_SETTINGS_RESULT = (
    b'{"name":"flatten_nested","value":"0"}\n'
    b'{"name":"date_time_input_format","value":"best_effort"}\n'
)
CANNED_COLUMNS_RESULT = (
    b'{"name":"ts","type":"DateTime64(3)","default_kind":"DEFAULT","default_expression":"now64(3)","position":"1"}\n'
    b'{"name":"device_id","type":"UInt32","default_kind":"","default_expression":"","position":"2"}\n'
    b'{"name":"reading c","type":"Float64","default_kind":"","default_expression":"","position":"3"}\n'
    b'{"name":"note","type":"String","default_kind":"DEFAULT","default_expression":"\'unset\'","position":"4"}\n'
)


def main() -> None:
    registry, lib = section1()
    try:
        section2(lib)
        section3(lib)
        section4(lib)
        section5(lib)
        section6(lib)
        section7(lib, registry)
        section8(lib)
        section9(lib)
        section10(lib)
        section11(registry)
        section12(registry)
        section13(registry)
        section14()
        section15(lib)
        section16(lib)
    finally:
        # The teardown section 13 narrates: the LAST thing this process does
        # with the registry. Reopening an artifact after a full close is not a
        # promised operation (measured: it can crash), so the tour keeps ONE
        # registry for its whole life and closes it exactly once, here.
        registry.close()

    blank()
    print("Done. Every value above was measured by this run.")
    print("The optional ONLINE demo (a real server, end to end) is go/ingest-demo/.")


# ---------------------------------------------------------------------------
# SECTION 1 — Load the library and check the ABI
#
# WHAT: open the artifact registry, see every ClickHouse version
# resident in this one process, pick one, and check the ABI revision.
# WHY: an ingest gateway serves tenants on different ClickHouse versions at
# once; the registry is how one process answers for all of them, exactly.
# LOOK FOR: the artifact NAMING ITSELF, and the refusal for a version that is
# not built — never a silent nearest-version fallback.
# C API: chs_clickhouse_version, chs_abi_revision, chs_init (implicit on
# load), chs_free (implicit on every returned string).
# Python extras: read_manifest / verify_library (the artifact's build sheet
# and its sha256 check), minor_of, ENV_REGISTRY.
# ---------------------------------------------------------------------------
def section1() -> tuple[Registry, chtypes.Library]:
    section(1, "Load the library and check the ABI")

    directory = registry_dir()
    try:
        registry = Registry(directory)
    except chtypes.RegistryError as err:
        fatal(f"open registry {directory!r}: {err}\n\nFetch an artifact first: `scripts/fetch.sh 25.8`.")

    versions = list(registry.versions())
    kv("registry dir", directory)
    kv("versions resident", "  ".join(versions))
    note("one dlopen (RTLD_LOCAL) per version — all live in THIS process at once")
    if len(versions) == 1:
        note("only one artifact is built; the tour still runs, and section 12's")
        note("cross-version sweeps will degrade gracefully. More: `scripts/fetch.sh 26.7`")

    # Version selection: a minor line ("25.8") and an exact patch
    # ("25.8.28.1-lts") both resolve. Docker tags drift, so an
    # exact-match-only lookup would silently lose a whole version line.
    want = os.environ.get("CHTYPES_VERSION")
    if want:
        kv("version selected", f"{want}  (from $CHTYPES_VERSION)")
    else:
        want = newest_line(versions)
        kv("version selected", f"{want}  (default: newest held; set $CHTYPES_VERSION to change)")
    try:
        lib = registry.for_version(want)
    except chtypes.RegistryError as err:
        fatal(str(err))
    kv("artifact reports", f"{lib.version}  (minor line {lib.minor} — chtypes.minor_of)")
    note("the artifact names ITSELF via chs_clickhouse_version() — nothing is")
    note("ever inferred from a directory or file name")

    # The ABI revision closes the gap symbol presence cannot: a symbol proves
    # a function exists, never that its signature matches. The binding's
    # revision is a module constant; the artifact's is a live call. A nonzero
    # disagreement is refused AT LOAD, not discovered mid-call.
    kv("ABI revision (binding)", str(chtypes.ABI_REVISION))
    kv("ABI revision (artifact)", f"{lib.abi_revision}   (0 would mean 'predates the probe')")
    kv("compile-settings symbol", f"{lib.has_compile_settings}  (Library.has_compile_settings)")

    # The build sheet next to the artifact: read_manifest returns it,
    # verify_library re-hashes the .dylib/.so against it.
    version_dir = str(Path(lib.path).parent)
    manifest = chtypes.read_manifest(version_dir)
    if manifest is not None:
        kv("manifest (read_manifest)", f"clickhouse_version={manifest.clickhouse_version}")
    try:
        chtypes.verify_library(version_dir)
        kv("verify_library", "sha256 matches the manifest")
    except chtypes.RegistryError as err:
        kv("verify_library", str(err))

    # Graceful refusal: answering 26.7 semantics out of a 25.8 artifact would
    # be a lie, so an unknown version raises, NAMING what is loaded.
    blank()
    try:
        registry.for_version("99.9")
        kv("asking for 99.9", "(no error!?)")
    except chtypes.RegistryError as err:
        kv("asking for 99.9", str(err))
    note("no nearest-neighbor fallback, ever — a wrong-version answer is a")
    note("wrong answer with a green checkmark on it")

    return registry, lib


# ---------------------------------------------------------------------------
# SECTION 2 — Ask a build about itself
#
# WHAT: type validation and canonicalization, straight from this build's own
# DataTypeFactory — plus the widened REFERENCE type this SDK also exposes.
# WHY: canonicalization is how you compare a tenant's declared type against
# what the server will actually store — and it is NOT a spelling normalizer,
# it is the server's own parse.
# LOOK FOR: Variant members being SORTED, BIGINT becoming Int64, the error
# for an unknown family carrying ClickHouse's own code 50, and reference
# types explaining how transformation findings are made.
# C API: chs_validate_type, chs_reference_type, chs_registered_families,
# chs_function_flags — the whole introspection trio, exposed per Library in
# every SDK since the 2026-08-26 parity cycle (docs/reference/bindings.md
# §Introspection).
# ---------------------------------------------------------------------------
def section2(lib: chtypes.Library) -> None:
    section(2, "Ask a build about itself")

    kv("validate_type", "input -> this build's canonical spelling")
    for t in ("Decimal(18,4)", "Variant(UInt8, String)", "BIGINT", "LowCardinality( String )"):
        kv(f"  {t}", "-> " + lib.validate_type(t))
    note("Variant members are SORTED; surplus parameters are dropped; the")
    note("space after each comma is the library's own spelling — compare")
    note("canonical strings verbatim, never re-normalize whitespace")
    blank()

    # An unknown family is a typed error carrying ClickHouse's OWN code and
    # message — not a string you have to pattern-match.
    try:
        lib.validate_type("NotAType")
    except chtypes.SchemaError as err:
        kv("validate_type(NotAType)", f"SchemaError code={err.code}  {err.msg}")
        note("code 50 = UNKNOWN_TYPE — the server's own code, from the server's")
        note("own registry. Section 10 is the full error taxonomy.")
    blank()

    # The widened reference type: the second parse every row call already runs
    # to detect silent transformations, exposed as a diagnostic.
    kv("reference_type", "the widened type transformation findings compare against")
    for t in ("UInt8", "DateTime", "String"):
        ref = lib.reference_type(t)
        kv(f"  {t}", f"-> {ref!r}" + ("   (no wider type exists)" if ref == "" else ""))
    note("UInt8 reparses through Int256, so an overflow wrap is CAUGHT by the")
    note("reference disagreeing; String has no wider type, so ''")
    blank()

    families = lib.registered_families()
    kv("registered_families", f"{len(families)} type families (e.g. {', '.join(families[:3])})")
    flags = [line for line in lib.function_flags().split("\n") if line]
    kv("function_flags", f"{len(flags)} registered functions audited (TSV)")
    note("the volatility audit behind the statelessness gate — every SDK")
    note("exposes the trio (docs/reference/bindings.md §Introspection)")


# ---------------------------------------------------------------------------
# SECTION 3 — Compile a schema and read it back
#
# WHAT: compile a column-declaration list (NOT a CREATE TABLE) and walk the
# compiled columns: canonical types, DEFAULT kinds and expressions.
# WHY: the compiled handle IS the table, as this ClickHouse version would
# create it. Two rewrites below are things no type-string comparison could
# ever catch — the compile is schema-aware.
# LOOK FOR: DEFAULT NULL turning Int64 into Nullable(Int64), an ALIAS column
# whose type is INFERRED, and default_is_literal (which Go does not surface).
# C API: chs_schema_compile, chs_schema_column_count/_name/_type/
# _default_kind/_default_expr/_default_is_literal, chs_schema_free (close).
# ---------------------------------------------------------------------------
def section3(lib: chtypes.Library) -> None:
    section(3, "Compile a schema and read it back")

    kv("the DDL", "")
    for ddl_line in DEMO_DDL.split(",\n"):
        raw("      " + ddl_line)
    with lib.compile_ddl(DEMO_DDL) as schema:
        blank()
        kv("compiled columns", "name  type  (default kind + expression, literal?)")
        for c in schema.columns:
            extra = ""
            if c.default_kind is not chtypes.DefaultKind.NONE:
                extra = f"  {c.default_kind.value} {c.default_expr}  literal={c.default_is_literal}"
            kv(f"  {c.name}", c.type + extra)
    note("payload_len is MATERIALIZED: compiled, introspectable, but never")
    note("read from input — watch it come back separately in section 6.")
    note("default_is_literal marks a DEFAULT applicable without the expression")
    note("interpreter (seq's 0: True; ts's now64(3): False)")
    blank()

    # Rewrite 1: a DEFAULT can change the declared TYPE. This is why
    # validate_type alone is not enough and a schema-aware compile exists.
    with lib.compile_ddl("x Int64 DEFAULT NULL") as schema:
        c = schema.columns[0]
        kv("x Int64 DEFAULT NULL", f"compiles as {c.type} DEFAULT {c.default_expr}")
    note("the DEFAULT rewrote the type to Nullable — the server does this at")
    note("CREATE, so chtypes must too or every later verdict drifts")

    # Rewrite 2: an ALIAS column's type is inferred from its expression.
    with lib.compile_ddl("a UInt8, al ALIAS a + 1") as schema:
        kv("a UInt8, al ALIAS a + 1", "al compiles as " + schema.columns[1].type)
    note("UInt8 + 1 widens to UInt16, ClickHouse's own inference")
    blank()

    # A failed compile is the same typed error as section 2's.
    kv("x NotAType", classify(lambda: lib.compile_ddl("x NotAType")))
    note(truncate(caught(lambda: lib.compile_ddl("x NotAType")), 90))


# ---------------------------------------------------------------------------
# SECTION 4 — Compile under a settings profile
#
# WHAT: the same compile with a DECLARED settings profile fixed into the
# handle — the settings a real server would have had at CREATE TABLE.
# WHY: some settings change the SHAPE of a table (flatten_nested), some gate
# which TYPES may exist (allow_suspicious_low_cardinality_types). A gateway
# discovers a deployment's settings once (section 11) and declares them here;
# the handle then behaves like a table created on THAT server.
# LOOK FOR: one DDL compiling to two different column lists; a type gate
# failing with the server's own 455; a typo'd setting name failing with the
# server's own 115 INCLUDING its did-you-mean hint.
# C API: chs_schema_compile (settings_json + mode arguments).
# ---------------------------------------------------------------------------
def section4(lib: chtypes.Library) -> None:
    section(4, "Compile under a settings profile")

    # (a) A compile-SHAPE setting: the same DDL, two storage shapes.
    kv("(a) flatten_nested", "id UInt32, n Nested(a UInt8, b String)")
    for value in ("1", "0"):
        with lib.compile_ddl(
            "id UInt32, n Nested(a UInt8, b String)", settings={"flatten_nested": value}
        ) as schema:
            kv(f"  ={value}", " | ".join(f"{c.name} {c.type}" for c in schema.columns))
    note("under 1 (the stock default) the Nested column is stored FLATTENED as")
    note("two Arrays; under 0 it is one Array(Tuple) column. Every downstream")
    note("answer — names, arity, the RowBinary wire — follows the compiled shape.")
    blank()

    # (b) A TYPE GATE, declared: checked ONCE at compile with the server's own
    # code, exactly where a real server checks it (at CREATE).
    kv("(b) a type gate", "lc LowCardinality(UInt8), allow_suspicious_low_cardinality_types")
    for value in ("0", "1"):
        try:
            with lib.compile_ddl(
                "lc LowCardinality(UInt8)",
                settings={"allow_suspicious_low_cardinality_types": value},
            ) as schema:
                kv(f"  ={value}", "compiled: " + schema.columns[0].type)
        except chtypes.SchemaError as err:
            kv(f"  ={value}", f"REFUSED, the server's own code {err.code}")
            note(truncate(str(err), 92))
    blank()

    # (c) A typo in the profile is caught at DECLARE time. The did-you-mean
    # hint is the SERVER'S — chtypes passes it through and invents nothing.
    try:
        lib.compile_ddl("a UInt8", settings={"flatten_nestedd": "1"})
    except chtypes.SchemaError as err:
        kv("(c) unknown setting name", f"flatten_nestedd -> code {err.code} (UNKNOWN_SETTING)")
        note(truncate(err.msg, 96))
    blank()

    # (d) The compile MODE. One mode exists (DECLARED = 0). A binding passes
    # an unrecognized mode THROUGH; the refusal (-2, a decline) is the
    # library's to make — unconditionally, even with no profile.
    kv("(d) compile mode", f"chtypes.COMPILE_DECLARED = {chtypes.COMPILE_DECLARED}")
    with lib.compile_ddl("a UInt8", mode=chtypes.COMPILE_DECLARED):
        kv("  mode=0", "compiled")
    kv("  mode=7", classify(lambda: lib.compile_ddl("a UInt8", mode=7)))
    note("a DECLINE (-2), not a rejection: reserved for a future mode")


# ---------------------------------------------------------------------------
# SECTION 5 — Accept, reject, decline (and poison)
#
# WHAT: the three verdicts every row lands on — plus the fourth, poisoned,
# which looks like an accept and bites at read time.
# WHY: this is the contract of the whole product. An ingest gateway routes on
# exactly this: accepted -> insert and publish the STORED values; rejected ->
# 400 the producer with the server's own message; unsupported -> chtypes
# refuses to guess, so fall back to the real server (validate cautiously) and
# NEVER convert the decline into an accept or a reject yourself.
# LOOK FOR: the accept carrying a visible coercion (input 256, stored 0);
# the reject carrying ClickHouse's own error text; the decline carrying no
# ClickHouse code at all.
# C API: chs_rows.
# ---------------------------------------------------------------------------
def section5(lib: chtypes.Library) -> None:
    section(5, "Accept, reject, decline (and poison)")
    kv("schema", FORMAT_DDL)
    blank()

    with lib.compile_ddl(FORMAT_DDL) as schema:
        # ACCEPT — with the coercion made visible. ClickHouse's readIntText
        # wraps integers mod 2^N and reports SUCCESS; chtypes derives the
        # Transform so a gateway can warn the tenant BEFORE the row ships.
        kv("(a) ACCEPT", '{"device_id":1,"seq":256,"label":"ok"}')
        feed(schema, "JSONEachRow", Format.JSON_EACH_ROW, b'{"device_id":1,"seq":256,"label":"ok"}')
        note("accepted — but look at seq: input 256, stored 0. The ~ line is the")
        note("Transform (reason overflow_wrap, LOSSY). Publish the STORED value;")
        note("publishing the payload value is how previews and tables diverge.")
        blank()

        # REJECT — the server's own refusal, code and message verbatim from
        # the vendored ClickHouse code. Nothing to retry; tell the producer.
        kv("(b) REJECT", '{"device_id":"abc"}')
        feed(schema, "JSONEachRow", Format.JSON_EACH_ROW, b'{"device_id":"abc"}')
        note("code 27 and the message are ClickHouse's OWN — chtypes never")
        note("hand-writes an error, so your 400 body matches what a real INSERT")
        note("would have said")
        blank()

        # DECLINE — chtypes refuses to guess. Values falls back to the SQL
        # expression parser for non-literals; evaluating tenant SQL locally is
        # a guess this library will not make: the row is UNSUPPORTED (-2).
        kv("(c) DECLINE", "(2,7,concat('a','b'))  as Values")
        feed(schema, "Values", Format.VALUES, b"(2,7,concat('a','b'))")
        note("unsupported means 'a real server MIGHT WELL accept this; I will not")
        note("guess'. Do not 400 the producer (that manufactures an over-reject),")
        note("do not publish (that manufactures an over-accept): send it to the")
        note("real server unpreviewed and let it decide. Both mistake classes are")
        note("budgeted at zero in this repo.")
        blank()

    # POISON — the fourth verdict. The INSERT genuinely succeeds and every
    # later SELECT throws: RowBinaryWithDefaults' marker byte fills an Enum
    # with the raw zero, and Enum8('red'=1,'green'=2) has NO name for 0.
    kv("(d) POISON", "an accept that bites at read time")
    with lib.compile_ddl("id UInt32, e Enum8('red' = 1, 'green' = 2)") as schema:
        kv("  schema", "id UInt32, e Enum8('red' = 1, 'green' = 2)")
        kv("  payload (RBWD)", RBWD_POISON + "   (id=1 by value, e by marker byte)")
        feed(schema, "RBWD marker", Format.ROW_BINARY_WITH_DEFAULTS, bytes.fromhex(RBWD_POISON))
        note("accepted_poisoned + code 691: the marker fills with the COLUMN-level")
        note("raw zero, and raw 0 has no Enum name. The insert returns success;")
        note("every later SELECT fails. Reported as an ACCEPT variant — never a")
        note("rejection — because the insert really does succeed.")
        kv("  control payload", RBWD_VALUE + "   (e supplied by value = 2)")
        feed(schema, "RBWD value", Format.ROW_BINARY_WITH_DEFAULTS, bytes.fromhex(RBWD_VALUE))
        # Python's convenience predicates on the row result:
        rp = schema.row(Format.ROW_BINARY_WITH_DEFAULTS, bytes.fromhex(RBWD_POISON))
        kv("  .accepted / .poisoned", f"{rp.accepted} / {rp.poisoned}   (Python's convenience predicates)")
    blank()

    # SKIP — the fifth verdict (2026-08-27), and the only per-row-only one: a
    # batch under input_format_allow_errors_* drops a bad row and continues,
    # with the server's own machinery — and since the itemization cycle, every
    # skip keeps its place in rows with the error IRowInputFormat caught
    # before resyncing. The server logs only a count; chtypes reports what it
    # computed.
    kv("(e) SKIP", "a bad middle row under input_format_allow_errors_num=10")
    with lib.compile_ddl(FORMAT_DDL) as schema:
        batch = (b'{"device_id":1,"seq":1,"label":"a"}\n'
                 b'{"device_id":"oops"}\n'
                 b'{"device_id":3,"seq":3,"label":"c"}\n')
        b = schema.rows(Format.JSON_EACH_ROW, batch,
                        {"input_format_allow_errors_num": "10"})
        kv("  batch", f"{b.outcome}  rows_read={b.rows_read} rows_skipped={b.rows_skipped}")
        for i, r in enumerate(b.rows):
            line = str(r.outcome)
            if r.outcome is chtypes.Outcome.SKIPPED:
                line += f"  code={r.err_code} {truncate(r.err_msg, 48)}"
            kv(f"  row {i}", line)
        note("one rows() call answers per input record, IN ORDER: accepted (with")
        note("the coerced values) or skipped (with the error that caused it).")
        note("A SKIPPED row is never stored — route on the row outcome; forward")
        note("only survivors, and never send allow_errors to the real INSERT.")


# ---------------------------------------------------------------------------
# SECTION 6 — DEFAULT evaluation: where every value comes from
#
# WHAT: one row through the demo table with the clock pinned, then reading
# back WHERE each stored value came from (Value.source), which values chtypes
# substituted itself, and which it computed.
# WHY: an INSERT is mostly values the row did NOT supply. A gateway that
# cannot answer "what will the table hold for this column?" cannot preview an
# insert. The volatile-DEFAULT rule is the sharp edge: chtypes resolved
# now64() from ITS clock, so the caller MUST send that column explicitly —
# otherwise the server stamps its own clock and preview != stored, always.
# LOOK FOR: different source values in one row; the substituted warning;
# payload_len under computed (never in values); "bogus" under unknown_fields;
# and a skew-budget DECLINE at the end.
# C API: chs_row (via Schema.row), the chtypes_* clock settings.
# ---------------------------------------------------------------------------
def section6(lib: chtypes.Library) -> None:
    section(6, "DEFAULT evaluation: where every value comes from")

    with lib.compile_ddl(DEMO_DDL) as schema:
        row = b'{"device_id":42,"seq":256,"payload":"hello","grade":"a","bogus":1}'
        kv("row fed", row.decode())
        kv("clock pinned", f"chtypes_now_epoch_nanos={PINNED_CLOCK}  (2023-11-14 22:13:20 UTC)")
        note("ts and label are OMITTED on purpose; bogus matches no column")
        r = schema.row(Format.JSON_EACH_ROW, row, {"chtypes_now_epoch_nanos": PINNED_CLOCK})
        blank()

        kv("outcome / err_code", f"{r.outcome.value} / {r.err_code}")
        kv("values[]  (the stored row)", "column = stored text  (source)")
        for v in r.values:
            kv(f"  {v.column}", f"{text_or(v):<28} ({v.source})")
        note("source values: input (the row supplied it), default (a DEFAULT")
        note("expression evaluated through ClickHouse's own CAST path),")
        note("default_substituted (a VOLATILE default resolved from the pinned")
        note("clock), absent (no DEFAULT: the type's own zero). text is")
        note("ClickHouse's OWN JSON rendering — never re-serialized here, because")
        note("18446744073709551615 through a double comes back ...552000.")

        blank()
        kv("substituted[]", "volatile DEFAULTs chtypes resolved from ITS clock")
        for s in r.substituted:
            kv(f"  {s.column}", f"{s.expr}  ->  {s.text}")
        note("SEND THESE AS EXPLICIT COLUMNS IN THE REAL INSERT. If the server")
        note("evaluates now64() itself, preview and stored differ every time —")
        note("ClickHouse reads the clock once per BLOCK, not once per statement.")

        blank()
        kv("computed[]", "MATERIALIZED values — durable, but never in SELECT *")
        for c in r.computed:
            kv(f"  {c.column}", f"{c.kind}  =  {c.text}")
        note("reported separately from values so the preview matches what a")
        note("subscriber reading the table will actually see")

        blank()
        kv("transformed[]", "every silent change, with a machine-readable reason")
        for t in r.transformed:
            kv(f"  {t.reason}", f"{t.column}: {t.input or '(absent)'} -> {t.stored}   lossy={t.lossy}")
        note("lossy is False for exactly four reasons (reformat, default_filled,")
        note("zero_filled, default_materialized) and True for everything else")

        blank()
        kv("unknown_fields[]", str(list(r.unknown_fields)))
        kv("unsupported_settings[]", str(list(r.unsupported_settings)))
        note("unknown fields are reported, not judged — whether to 400 on them is")
        note("gateway policy. A non-empty unsupported_settings promotes the row")
        note("to unsupported: a declined setting must never score as agreement.")
        blank()

        # A DEFAULT can read OTHER columns of the same row.
        with lib.compile_ddl("a UInt8, d UInt8 DEFAULT a + 1") as dep:
            kv("row-dependent DEFAULT", "a UInt8, d UInt8 DEFAULT a + 1   fed CSV `7,`")
            feed(dep, "CSV", Format.CSV, b"7,")
            note("the bare empty CSV field takes the DEFAULT, and the DEFAULT reads")
            note("a=7 from the same row — d stores 8, exactly as the server computes it")
        blank()

        # The clock-skew budget: the one place a DEFAULT becomes a DECLINE.
        # Past the budget the only safe answer is "unsupported" — a
        # substituted timestamp too far in the past under a TTL is silently
        # deleted at merge time, with no error at any point.
        r2 = schema.row(
            Format.JSON_EACH_ROW,
            b'{"device_id":1,"seq":1,"payload":"x","grade":"b"}',
            {"chtypes_clock_offset_nanos": "5000000000", "chtypes_max_clock_skew_nanos": "1"},
        )
        kv("skew budget decline", "offset=5s, budget=1ns")
        kv("  outcome / err_code", f"{r2.outcome.value} / {r2.err_code}")
        for v in r2.values:
            if v.column == "ts":
                kv("  ts.source", v.source)
        note("default_volatile_unresolved: chtypes refuses to substitute a")
        note("volatile DEFAULT when the measured clock offset exceeds the")
        note("caller's budget (chtypes_max_clock_skew_nanos)")


# ---------------------------------------------------------------------------
# SECTION 7 — One schema, every format
#
# WHAT: the same three-column schema fed in all ten chs_format encodings —
# accept and reject for each text format, then the binary tier with
# hand-built bytes.
# WHY: format is not cosmetic. Each format has signature behaviors (CSV's
# bare-vs-quoted empty field, RBWD's marker byte, Native's silent CAST,
# Buffers' silent reinterpret) that change what the table ends up holding.
# LOOK FOR: the same logical row giving format-specific verdicts, and the
# byte-level payloads in the comments — every binary payload is explained.
# C API: chs_rows with format codes 0..9 (frozen integers: JSON_EACH_ROW=0,
# CSV=1, TSV=2, VALUES=3, JSON_COMPACT_EACH_ROW=4, ROW_BINARY=5,
# ROW_BINARY_WITH_DEFAULTS=6, ROW_BINARY_WITH_NAMES_AND_TYPES_AND_DEFAULTS=7,
# NATIVE=8, BUFFERS=9).
# ---------------------------------------------------------------------------
def section7(lib: chtypes.Library, registry: Registry) -> None:
    section(7, "One schema, every format")
    kv("schema", FORMAT_DDL)
    blank()

    with lib.compile_ddl(FORMAT_DDL) as schema:
        group("text formats (one row per line; positional or named)")
        feed(schema, "JSONEachRow  accept", Format.JSON_EACH_ROW, b'{"device_id":1,"seq":7,"label":"ok"}')
        feed(schema, "JSONEachRow  reject", Format.JSON_EACH_ROW, b'{"device_id":"abc"}')
        feed(schema, "CSV          accept", Format.CSV, b"1,7,ok")
        feed(schema, "CSV          reject", Format.CSV, b"x,7,ok")
        feed(schema, "TSV          accept", Format.TSV, b"1\t7\tok")
        feed(schema, "TSV          reject", Format.TSV, b"y\t7\tok")
        feed(schema, "JSONCompact  accept", Format.JSON_COMPACT_EACH_ROW, b'[1,7,"ok"]')
        feed(schema, "JSONCompact  reject", Format.JSON_COMPACT_EACH_ROW, b'["z",7,"ok"]')
        feed(schema, "Values       accept", Format.VALUES, b"(1,7,'ok')")
        feed(schema, "Values       DEFAULT", Format.VALUES, b"(3,DEFAULT,'d')")
        feed(schema, "Values       decline", Format.VALUES, b"(2,7,concat('a','b'))")
        note("Values has an explicit DEFAULT keyword; an SQL expression is a")
        note("DECLINE (section 5c) — evaluated by a real server, guessed by nobody")
        blank()

        # CSV's signature rule deserves its own two lines.
        kv("CSV empty-field rule", "bare empty takes the DEFAULT; quoted empty is ''")
        feed(schema, "CSV   bare   2,7,", Format.CSV, b"2,7,")
        feed(schema, 'CSV   quoted 3,7,""', Format.CSV, b'3,7,""')
        blank()

        group("binary formats (bytes, COUNTED — never NUL-terminated)")
        kv("  RowBinary payload", ROW_BINARY_OK)
        feed(schema, "RowBinary    accept", Format.ROW_BINARY, bytes.fromhex(ROW_BINARY_OK))
        note('4-byte LE UInt32 (1), 1-byte UInt8 (7), varint-length String ("ok")')
        note("— no framing, no names, no self-description")
        feed(schema, "RowBinary    reject", Format.ROW_BINARY, b"\x01\x00")
        note("truncated mid-row: framing faults are all-or-nothing per batch")
        kv("  RBWD payload", RBWD_MARKER)
        feed(schema, "RBWD  marker byte", Format.ROW_BINARY_WITH_DEFAULTS, bytes.fromhex(RBWD_MARKER))
        note("RowBinaryWithDefaults prefixes each column with a marker byte:")
        note("00 = 'value follows', nonzero = 'compute the DEFAULT, read no value")
        note("bytes'. Above, seq's marker is 01 -> stored 7 (its DEFAULT).")
        kv("  RBWNTD payload", "(hand-built: LEB128 count, names, types, then a marker row)")
        feed(schema, "RBWNTD       accept", Format.ROW_BINARY_WITH_NAMES_AND_TYPES_AND_DEFAULTS, bytes.fromhex(RBWNTD_ROW))
        note("format 7 arrives with ClickHouse 26.x — on an older artifact the line")
        note("above is the server's own 73 UNKNOWN_FORMAT, not a chtypes error.")
        note("Section 12 turns exactly this into the version-pinning lesson.")
        blank()

        group("Native: self-describing, and it CASTs")
        feed(schema, "Native       accept", Format.NATIVE, bytes.fromhex(NATIVE_OK))
        note("a Native block declares its OWN column names and types (captured")
        note("from a live 25.8 SELECT ... FORMAT Native)")
        feed(schema, "Native       CAST", Format.NATIVE, bytes.fromhex(NATIVE_CAST))
        note('this block declares seq as String "200" while the table says UInt8:')
        note("the disagreement is CAST silently (input_format_native_allow_types_")
        note("conversion defaults to true on every vendored era) — visible here as")
        note("a Transform, invisible on a real server")
    blank()

    group("Buffers: NO self-description at all")
    newest = registry.for_version(newest_line(list(registry.versions())))
    with newest.compile_ddl("x Int32") as schema:
        kv("  artifact", f"{newest.minor}  (Buffers arrives at 26.5; this block uses the newest held)")
        kv("  payload", "4 bytes ff ff ff ff, declared x Int32")
        feed(schema, "Buffers reinterpret", Format.BUFFERS, bytes.fromhex(BUFFERS_OK))
        note("a UInt32 producer's 4294967295 reads back as -1: same width, no")
        note("metadata, so no check CAN fire. The over-accept class in a format")
        note("that cannot detect it — the schema is entirely out of band.")
        feed(schema, "Buffers width", Format.BUFFERS, bytes.fromhex(BUFFERS_WIDE))
        note("the same 4 bytes declared 8 wide IS caught: size accounting disagrees")


# ---------------------------------------------------------------------------
# SECTION 8 — Engines, MergeTree settings, and TTL
#
# WHAT: declare the table's engine and TTL, then watch the STORAGE layer
# change what a batch stores — including storing nothing at all.
# WHY: a row can be accepted per row and absent per batch. SummingMergeTree
# folds rows at insert; a TTL already in the past deletes them at merge, with
# no error at any point. A gateway reading only per-row verdicts previews
# rows the table will never hold.
# LOOK FOR: engine_rows (the stored truth) being SHORTER than the input; the
# TTL batch whose row is accepted and whose engine_rows is empty; and the
# refusal-vs-decline pair on MergeTree settings (the sign of the ABI return
# decides which).
# C API: chs_schema_engine, chs_schema_ttl, chs_rows.
# ---------------------------------------------------------------------------
def section8(lib: chtypes.Library) -> None:
    section(8, "Engines, MergeTree settings, and TTL")

    # (a) A specialized engine changes what the table STORES.
    with lib.compile_ddl("day Date, key UInt32, v UInt64") as schema:
        kv("(a) set_engine", "SummingMergeTree ORDER BY (day, key)")
        schema.set_engine("SummingMergeTree", "(day, key)")
        body = b'{"day":"2026-01-01","key":1,"v":5}\n{"day":"2026-01-01","key":1,"v":7}'
        batch = schema.rows(Format.JSON_EACH_ROW, body)
        kv("  rows in / rows_read", f"2 / {batch.rows_read}   (v=5 and v=7, same key)")
        kv("  engine_rows (stored)", "[" + ", ".join(batch.engine_rows or []) + "]")
    note("two rows in, ONE row out, v summed — engine_rows is the post-merge")
    note("preview and, when present, the truth to believe over rows")
    blank()

    # (b) MergeTree-namespace settings: two failures, two KINDS. The sign of
    # the ABI return decides — a positive code is the SERVER refusing, a
    # negative one is this LIBRARY declining. Never flatten them.
    kv("(b) MergeTree settings", "refusal vs decline vs inert")
    with lib.compile_ddl("a UInt8") as s2:
        kv("  unknown NAME", "index_granularityy -> " + classify(
            lambda: s2.set_engine("MergeTree", "tuple()", merge_tree_settings={"index_granularityy": "8192"})))
        note(truncate(caught(
            lambda: s2.set_engine("MergeTree", "tuple()", merge_tree_settings={"index_granularityy": "8192"})), 90))
        note("the server's own 115: this DDL can never exist — tell the tenant")
    with lib.compile_ddl("a UInt8") as s3:
        kv("  known, non-default", "index_granularity=4096 -> " + classify(
            lambda: s3.set_engine("MergeTree", "tuple()", merge_tree_settings={"index_granularity": "4096"})))
        note("a DECLINE: no MergeTree setting's behavior is modeled yet, and")
        note("silently ignoring a declared value would fake the profile being in")
        note("force. A real server might well accept it — validate cautiously.")
    with lib.compile_ddl("a UInt8") as s4:
        kv("  known, AT default", "index_granularity=8192 -> " + classify(
            lambda: s4.set_engine("MergeTree", "tuple()", merge_tree_settings={"index_granularity": "8192"})) + " (inert)")
    blank()

    # (c) TTL: accepted per row, gone per batch.
    with lib.compile_ddl("ts DateTime, v UInt8") as schema:
        schema.set_engine("MergeTree", "ts")
        schema.set_ttl("ts + INTERVAL 1 DAY")
        kv("(c) set_ttl", "MergeTree ORDER BY ts, TTL ts + INTERVAL 1 DAY")
        batch = schema.rows(
            Format.JSON_EACH_ROW,
            b'{"ts":"2020-01-01 00:00:00","v":9}',
            {"chtypes_now_epoch_nanos": PINNED_CLOCK},
        )
        kv("  row fed", '{"ts":"2020-01-01 00:00:00","v":9}  with the clock pinned to 2023')
        kv("  row-level outcome", f"{batch.rows[0].outcome.value}   <- the row PARSED fine")
        engine_rows = list(batch.engine_rows or [])
        kv("  engine_rows (stored)", f"{engine_rows}  (length {len(engine_rows)})")
        for t in batch.transformed:
            kv("  batch transform", f"row={t.row} column={t.column!r} reason={t.reason} lossy={t.lossy}")
        note("accepted per ROW, stored nowhere per BATCH: the 2020 timestamp is")
        note("already past the TTL, so the part holds nothing. On a real server")
        note("this is a silent merge-time delete — the ttl_expired transform is")
        note("the only warning anyone gets.")
        blank()

        # (d) A TTL this library will not guess at.
        kv("(d) set_ttl now()+1 DAY", classify(lambda: schema.set_ttl("now() + INTERVAL 1 DAY")))
        note(truncate(caught(lambda: schema.set_ttl("now() + INTERVAL 1 DAY")), 90))
        note("a clock-reading TTL is DECLINED, not guessed")


# ---------------------------------------------------------------------------
# SECTION 9 — Settings precedence: who wins
#
# WHAT: the same row and the same handle, answered differently as settings
# are supplied at each layer:
#
#     per-call  >  handle profile  >  library defaults  >  ClickHouse's own
#
# WHY: this is how a gateway declares a deployment's settings ONCE (at
# compile) yet still lets one INSERT override per call. If precedence were
# fuzzy, the declared profile would not actually be in force.
# LOOK FOR: layers 3 vs 4 — the SAME handle, the SAME bytes, passing under
# the handle profile and failing the moment a per-call value overrides it.
# Then the library-defaults layer measured via set_default_settings,
# including a WHOLESALE refusal that commits nothing.
# C API: chs_rows (settings_json), chs_set_default_settings.
# Python note: set_default_settings lives on the dlopen'd Library itself
# (Go's Registry path structurally cannot make this call; only its static
# cgo path can).
# ---------------------------------------------------------------------------
def section9(lib: chtypes.Library) -> None:
    section(9, "Settings precedence: who wins")
    kv("the probe", 'ts DateTime  fed  {"ts":"2026-01-15T10:30:00Z"}')
    note("stock ClickHouse parses 'basic' datetimes only; best_effort accepts")
    note("ISO-8601 — so the verdict TELLS you which setting value won")
    blank()

    iso = b'{"ts":"2026-01-15T10:30:00Z"}'
    basic = {"date_time_input_format": "basic"}
    best_effort = {"date_time_input_format": "best_effort"}
    kv("artifact", f"{lib.version}  (the selected version; Go's tour uses its static path here)")

    with lib.compile_ddl("ts DateTime") as plain, \
            lib.compile_ddl("ts DateTime", settings=best_effort) as profiled:
        b1 = plain.rows(Format.JSON_EACH_ROW, iso)
        kv("1. ClickHouse defaults", verdict(b1))
        note("nothing declared anywhere — the stock default decides (it rejected")
        note("ISO-8601 through 25.10 and accepts it from 26.5)")
        b2 = plain.rows(Format.JSON_EACH_ROW, iso, basic)
        kv("2.  + per-call basic", verdict(b2))
        b3 = profiled.rows(Format.JSON_EACH_ROW, iso)
        kv("3. handle profile best_effort", verdict(b3))
        note("the profile declared at COMPILE reaches every later row call")
        b4 = profiled.rows(Format.JSON_EACH_ROW, iso, basic)
        kv("4.  + per-call basic", verdict(b4))
        note("3 vs 4 is the requirement, measured: the same row PASSES under the")
        note("handle profile and FAILS when the per-call value overrides it")
        blank()

        # The library-defaults layer, and its two safety properties: the seed
        # is REPLACED wholesale on every call, and a payload with any unknown
        # name is refused wholesale — nothing committed.
        kv("set_default_settings", "the library-defaults layer, measured")
        lib.set_default_settings(best_effort)
        kv("5. seed best_effort", verdict(plain.rows(Format.JSON_EACH_ROW, iso)))
        lib.set_default_settings(basic)
        kv("6. seed basic", verdict(plain.rows(Format.JSON_EACH_ROW, iso)))
        note("each call REPLACES the whole seed — 5's value did not linger")
        kv("7. seed basic + per-call best_effort", verdict(plain.rows(Format.JSON_EACH_ROW, iso, best_effort)))
        note("per-call still outranks the seed")
        try:
            lib.set_default_settings({"made_up_setting_xyz": "1"})
            kv("8. seed an unknown name", "(no error!?)")
        except chtypes.ChtypesError as err:
            kv("8. seed an unknown name", f"{type(err).__name__}: {truncate(str(err), 88)}")
        kv("   verdict after refusal", verdict(plain.rows(Format.JSON_EACH_ROW, iso))
           + "   <- unchanged: NOTHING was committed")
        note("the 115 and its message are the server's own; a gateway can never")
        note("believe a default profile is in force when part of it never applied")
        lib.set_default_settings({})
        kv("9. seed {} (reset)", verdict(plain.rows(Format.JSON_EACH_ROW, iso)))


# ---------------------------------------------------------------------------
# SECTION 10 — The error taxonomy
#
# WHAT: every kind of answer this SDK gives, told apart BY TYPE — never by
# string matching.
# WHY: the three kinds demand three different reactions (tell the tenant /
# fall back cautiously / fix the deployment). Since 2026-08-26 the decline
# type is a PEER of the refusal type in every SDK — Python's grandfathered
# subclass is retired (docs/reference/bindings.md rule 12) — so a bare `except
# SchemaError` can never swallow a decline again: forgetting the decline arm
# now raises past the handler (loud) instead of silently converting declines
# into rejections (a manufactured over-reject, budgeted at zero).
# LOOK FOR: the two-arm except idiom, the issubclass line printing False, and
# the reminder that ROW verdicts are data (RowResult.outcome), not raised.
# ---------------------------------------------------------------------------
def section10(lib: chtypes.Library) -> None:
    section(10, "The error taxonomy")

    kv("the Python idiom", "two PEER arms — the type IS the answer")
    raw("      try:")
    raw("          schema.set_engine(engine, order_by)")
    raw("      except chtypes.UnsupportedError:   # a DECLINE — validate cautiously")
    raw("          ...")
    raw("      except chtypes.SchemaError as e:   # a REFUSAL — e.code is ClickHouse's")
    raw("          ...")
    raw("      except chtypes.RegistryError:      # deployment problem — fix the registry")
    raw("          ...")
    kv(
        "  issubclass(UnsupportedError, SchemaError)",
        str(issubclass(chtypes.UnsupportedError, chtypes.SchemaError)),
    )
    note("False — every SDK is peer-shaped now (Go/TS: peer types, Rust:")
    note("sibling enum variants; Python completed the split 2026-08-26).")
    note("`except SchemaError` never catches a decline; `except ChtypesError`")
    note("catches both when both is what you mean.")
    blank()

    # A REFUSAL: ClickHouse's own code rides on SchemaError.
    describe_error(lambda: lib.compile_ddl("x NotAType"), "compile x NotAType")
    # A DECLINE: UnsupportedError.
    with lib.compile_ddl("ts DateTime, v UInt8") as schema:
        schema.set_engine("MergeTree", "ts")
        describe_error(lambda: schema.set_ttl("now() + INTERVAL 1 DAY"), "set_ttl now()+1 DAY")
    # A registry miss: RegistryError (a deployment problem, not a verdict).
    describe_error(lambda: Registry("/nonexistent-registry"), "Registry('/nonexistent-registry')")
    blank()

    kv("row verdicts are DATA", "RowResult.outcome, not an exception")
    note("a row the server would reject RETURNS (outcome=rejected, err_code=")
    note("the server's code) — nothing raises. Exceptions are for questions")
    note("that could not be asked; verdicts live in the answer.")
    kv("CODE_UNSUPPORTED", f"{chtypes.CODE_UNSUPPORTED}  (the wire sentinel; never a real ClickHouse code)")


# ---------------------------------------------------------------------------
# SECTION 11 — The discovery kit, offline
#
# WHAT: the three canonical queries chtypes ships for learning who a
# deployment is, their typed parsers, and reconstruct_ddl — run here against
# CANNED bytes shaped exactly like a real server's JSONEachRow responses.
# WHY: chtypes NEVER opens a socket. You run these queries with whatever
# client you already have; the kit gives you the SQL and parses the results.
# The payoff is the last step: the server's own version string resolves an
# artifact, and the discovered settings become the compile profile — so the
# handle behaves like a table created on THAT deployment.
# LOOK FOR: the reconstructed DDL (backticks where needed, DEFAULTs carried),
# and the SAME ROW accepted under the discovered profile but rejected under a
# stock compile — the measurable reason discovery matters.
# C API: none until the compile at the end — the kit is pure client-side.
# (The ONLINE version of this flow, against a real server, is
# go/ingest-demo/ — the optional demo chplay.sh never runs.)
# ---------------------------------------------------------------------------
def section11(registry: Registry) -> None:
    section(11, "The discovery kit, offline")
    kv("NOTE", "responses below are CANNED — shaped exactly like a real")
    kv("", "server's, so the parsers cannot tell. Swap in your HTTP client.")
    blank()

    # Query 1: who are you? (version)
    kv("QUERY_SERVER_VERSION", chtypes.QUERY_SERVER_VERSION)
    kv("  canned response", CANNED_VERSION_RESULT.decode().strip())
    version = chtypes.parse_version_result(CANNED_VERSION_RESULT)
    kv("  parsed", version)
    blank()

    # Query 2: which settings did this deployment change from stock?
    kv("QUERY_CHANGED_SETTINGS", chtypes.QUERY_CHANGED_SETTINGS)
    for canned_line in CANNED_SETTINGS_RESULT.decode().strip().split("\n"):
        kv("  canned response", canned_line)
    settings = chtypes.parse_changed_settings_result(CANNED_SETTINGS_RESULT)
    kv("  parsed", f"{len(settings)} changed settings -> the compile profile")
    blank()

    # Query 3: what does the table look like AS STORED?
    kv("QUERY_TABLE_COLUMNS", "(system.columns for one table; see the constant)")
    cols = chtypes.parse_columns_result(CANNED_COLUMNS_RESULT)
    for col in cols:
        kind = f"  {col.default_kind} {col.default_expression}" if col.default_kind else ""
        kv(f"  [{col.position}] {col.name}", col.type + kind)
    note("default_kind/default_expression are CARRIED — dropping them would")
    note("silently lose the DEFAULT semantics sections 6 and 8 run on")
    ddl = chtypes.reconstruct_ddl(cols)
    kv("  reconstruct_ddl", ddl)
    note("`reading c` came back BACKTICKED — identifiers are quoted exactly")
    note("where ClickHouse requires it")
    blank()

    # The payoff: version -> artifact, settings -> profile, and a measurable
    # difference the discovered profile makes.
    try:
        lib = registry.for_version(version)
    except chtypes.RegistryError as err:
        kv(f"registry.for_version({version})", str(err))
        note("no artifact for this line — `scripts/fetch.sh 25.8` would add it; the")
        note("rest of this section needs it and is skipped")
        return
    kv(f"registry.for_version({version})", f"artifact {lib.version}  (exact patch -> the {lib.minor} line)")
    row = b'{"ts":"2026-01-15T10:30:00Z","device_id":9,"reading c":21.5}'
    kv("the same row, twice", row.decode())
    with lib.compile_ddl(ddl, settings=settings) as profiled:
        kv("  under the discovered profile", verdict(profiled.rows(Format.JSON_EACH_ROW, row)))
    with lib.compile_ddl(ddl) as plain:
        kv("  under a stock compile", verdict(plain.rows(Format.JSON_EACH_ROW, row)))
    note("the deployment declared date_time_input_format=best_effort, so ITS")
    note("server takes the ISO-8601 timestamp — a stock compile answers for a")
    note("server the tenant does not have. Discovery is what closes that gap.")


# ---------------------------------------------------------------------------
# SECTION 12 — Version pinning: same input, different answers
#
# WHAT: the same DDL and the same bytes, swept across every artifact resident
# in this process.
# WHY: version differences are the reason the registry exists. They are not
# monotonic — newer is NOT always more permissive — so no rule can predict
# them; only the real per-version artifact can answer.
# LOOK FOR: 25.10 rejecting a DEFAULT that both 25.8 and 26.5 accept; and the
# Buffers format simply not existing before 26.5 (the server's own 73).
# ---------------------------------------------------------------------------
def section12(registry: Registry) -> None:
    section(12, "Version pinning: same input, different answers")
    versions = list(registry.versions())
    if len(versions) < 2:
        kv("versions resident", "  ".join(versions))
        note("only one artifact is built, so there is nothing to sweep — the")
        note("point of this section needs at least two. Build another line")
        note("(e.g. `scripts/fetch.sh 26.7`) and re-run to see the answers diverge.")
        return

    kv("(a) a mixed-type DEFAULT", "a UInt8, x Int64 DEFAULT if(1,2,'a')")
    for v in versions:
        lib = registry.for_version(v)
        try:
            with lib.compile_ddl("a UInt8, x Int64 DEFAULT if(1,2,'a')") as schema:
                col = schema.columns[1]
                kv(f"  {v}", f"compiled  ({col.type} DEFAULT {col.default_expr})")
        except chtypes.SchemaError as err:
            kv(f"  {v}", f"REJECTED code {err.code}  {truncate(err.msg, 52)}")
    note("NEWER IS NOT ALWAYS MORE PERMISSIVE — no monotonic rule predicts")
    note("this, which is exactly why one real artifact per line exists")
    blank()

    kv("(b) a format's arrival", "Buffers (code 9), added in ClickHouse 26.5")
    kv("  payload", "1 column, 1 row, 4 bytes ff ff ff ff, declared x Int32")
    for v in versions:
        lib = registry.for_version(v)
        with lib.compile_ddl("x Int32") as schema:
            batch = schema.rows(Format.BUFFERS, bytes.fromhex(BUFFERS_OK))
            if batch.outcome is chtypes.Outcome.ACCEPTED and batch.rows:
                kv(f"  {v}", f"accepted  x = {batch.rows[0].values[0].text}")
            else:
                kv(f"  {v}", f"{batch.outcome.value}  code={batch.err_code}  {truncate(batch.err_msg, 40)}")
    note("73 UNKNOWN_FORMAT is the SERVER'S own answer on the older lines —")
    note("probe the artifact (one payload through rows) instead of trusting")
    note("your own version arithmetic")


# ---------------------------------------------------------------------------
# SECTION 13 — Teardown
#
# WHAT: what to release, and when.
# WHY: schema handles are C allocations (chs_schema_free via close/`with`).
# Process teardown is chs_shutdown, which Python exposes as registry.close()
# (also per-library: Library.close()); the Registry is a context manager too.
# C API: chs_schema_free, chs_shutdown.
# ---------------------------------------------------------------------------
def section13(registry: Registry) -> None:
    section(13, "Teardown")
    kv("schema handles", "close() or `with lib.compile_ddl(...) as s:` (chs_schema_free)")
    kv("registry.close()", "chs_shutdown for every loaded library — refcounted per image")
    kv("  when", "this tour runs it at the very END (sections 15-16 still need")
    kv("", "the libraries); teardown is the LAST thing a process does with a")
    kv("", "registry — reopening a closed artifact is not a promised operation")
    note("chs_init also registers chs_shutdown with atexit(), so an ordinary")
    note("process would be fine without this — close() is for callers that")
    note("control their own teardown order. Go's Registry deliberately has NO")
    note("teardown (it never dlcloses; docs/reference/bindings.md §Teardown); TS has")
    note("close()/Symbol.dispose; Rust has Registry::shutdown() and Drop.")


# ---------------------------------------------------------------------------
# SECTION 14 — The static path (Go only)
#
# Go's cgo build can additionally LINK one artifact directly (no dlopen) and
# use it through package-level functions; that is also the only place Go
# exposes SetDefaultSettings and RegisteredFamilies. Python cannot have that
# shape: ctypes always dlopens, so Registry is the only loader here — and it
# is the product path anyway. docs/reference/bindings.md makes the static shape
# explicitly optional. See go/main.go section 14 for the real thing.
# ---------------------------------------------------------------------------
def section14() -> None:
    section(14, "The static path (Go only)")
    kv("not offered in Python", "ctypes always dlopens; Registry is the only loader")
    note("see go/main.go section 14 — docs/reference/bindings.md §The object model makes")
    note("the statically-linked single-version shape explicitly optional")


# ---------------------------------------------------------------------------
# SECTION 15 — Export: bytes + spans
#
# WHAT: the SAME chs_rows call that judges a batch can also serialize its
# accepted rows to wire bytes (JSONCompactEachRow this revision), addressed
# per row by index-aligned spans — rows(..., export=...), one C call.
# WHY: every consumer of an accepted row wants the stored bytes ready to
# publish or INSERT without rebuilding them from the document — reassembly
# is where caller bugs live (the invalid-JSON-on-poisoned-rows class).
# LOOK FOR: the skipped row's (0,0) span; a span slice BEING the row's line;
# the poisoned batch DECLINING the export and saying why; emitted-EMPTY (an
# answer) vs declined (not one); and the lean document (doc_flags=0 — the
# default when export is requested) keeping every verdict.
# C API: chs_rows with export_format/doc_flags/out_bytes (ABI revision 3).
# ---------------------------------------------------------------------------
def section15(lib: chtypes.Library) -> None:
    section(15, "Export: bytes + spans")
    with lib.compile_ddl(FORMAT_DDL) as schema:
        body = (b'{"device_id":1,"label":"ok"}\n'
                b'{"device_id":"zap"}\n'
                b'{"device_id":3,"label":"hi"}\n')
        try:
            batch = schema.rows(Format.JSON_EACH_ROW, body,
                                {"input_format_allow_errors_num": "10"},
                                export=Format.JSON_COMPACT_EACH_ROW,
                                doc_flags=chtypes.DOC_ALL)
        except chtypes.UnsupportedError as err:
            note("this artifact predates the export surface (relink it to ABI")
            note("revision 3, `just refresh <version>`); the section degrades")
            note(f"here rather than failing the tour: {truncate(str(err), 48)}")
            return
        if batch.outcome is chtypes.Outcome.UNSUPPORTED:
            kv("rows(export=...)", f"declined: {truncate(batch.err_msg, 64)}")
            note("this artifact answers the export request unsupported; relink")
            note("the fleet (`just refresh`) to see the live bytes. Degrading.")
            return
        kv("batch", f"{batch.outcome.value}  rows_read={batch.rows_read} rows_skipped={batch.rows_skipped}")
        kv("payload", f"{batch.payload!r}  ({len(batch.payload)} bytes, one line per ACCEPTED row)")
        note("wire order = declared minus MATERIALIZED/ALIAS/EPHEMERAL, so these")
        note("bytes are directly INSERT-able with no column list; DEFAULTs (seq=7,")
        note("label='unknown') are already applied — preview == stored")
        for i, span in enumerate(batch.spans):
            line = "(no bytes — row not accepted)"
            if span.len:
                line = repr(bytes(memoryview(batch.payload)[span.off:span.off + span.len]))
            kv(f"  span[{i}] {{off:{span.off} len:{span.len}}}",
               f"{batch.rows[i].outcome.value} -> {line}")
        note("spans are INDEX-ALIGNED with rows; slicing spans out of the payload")
        note("IS the per-row payload (memoryview-friendly), and concatenating")
        note("non-zero spans reproduces it exactly — batches merge by concatenation")
        blank()

        # The lean document: export without doc_flags defaults to 0 —
        # every verdict, none of the description, same bytes.
        lean = schema.rows(Format.JSON_EACH_ROW, body,
                           {"input_format_allow_errors_num": "10"},
                           export=Format.JSON_COMPACT_EACH_ROW)
        kv("lean (doc_flags=0)",
           f"outcome {lean.outcome.value}, {len(lean.rows)} verdict rows, "
           f"{len(lean.rows[0].values)} values, {len(lean.transformed)} transformed "
           f"— payload identical: {lean.payload == batch.payload}")
        note("flags thin the DESCRIPTION, never the VERDICT; DOC_VALUES /")
        note("DOC_TRANSFORMS / DOC_DEFAULTS pick groups a la carte")
        blank()

    # Fail-closed: a poisoned batch holds a value ClickHouse itself
    # cannot read back — no writer can honestly serialize it, so no bytes.
    with lib.compile_ddl("e Enum8('a' = 1, 'b' = 2)") as poison_schema:
        poi = poison_schema.rows(Format.JSON_EACH_ROW, b'{"e":null}\n',
                                 {"input_format_defaults_for_omitted_fields": "0"},
                                 export=Format.JSON_COMPACT_EACH_ROW,
                                 doc_flags=chtypes.DOC_ALL)
        payload_desc = "None (declined)" if poi.payload is None else f"{len(poi.payload)} bytes"
        kv("poisoned batch",
           f"{poi.outcome.value} -> payload={payload_desc} export_declined={truncate(poi.export_declined, 48)!r}")

    # Emitted-empty is an ANSWER (zero accepted rows), not a decline.
    with lib.compile_ddl(FORMAT_DDL) as schema:
        emp = schema.rows(Format.JSON_EACH_ROW, b"",
                          export=Format.JSON_COMPACT_EACH_ROW)
        kv("empty batch",
           f"payload is not None={emp.payload is not None} len={len(emp.payload)}  (emitted-empty != declined)")


# ---------------------------------------------------------------------------
# SECTION 16 — Filters: WHERE semantics at the edge
#
# WHAT: compile one boolean expression against a schema (compile_filter) and
# evaluate it per row of a body — ClickHouse's own comparison functions, so
# the answers are WHERE-side by construction.
# WHY: read-side row visibility (who may SEE this row) is a WHERE question,
# and WHERE coercion is NOT insert coercion: `x = 256` over UInt8 PROMOTES
# (false for every row) where an insert would wrap 256 to 0. Reusing the
# insert answer would silently match every legitimate zero.
# LOOK FOR: 'f','f' where the insert path stores 0; NULL being not-true; the
# 'e' class (compiles, then THROWS per row — a server fails the WHOLE query
# here); clock reads and {p:Type} parameters REFUSED at compile, never
# guessed; and the enforcement gate at the end.
# C API: chs_filter_compile / chs_filter_rows / chs_filter_free (revision 3).
# ---------------------------------------------------------------------------
def section16(lib: chtypes.Library) -> None:
    section(16, "Filters: WHERE semantics at the edge")
    with lib.compile_ddl("x UInt8") as schema:
        try:
            filt = schema.compile_filter("x = 256")
        except chtypes.UnsupportedError as err:
            note("this artifact predates the filter surface (relink it to ABI")
            note("revision 3, `just refresh <version>`); the section degrades")
            note(f"here rather than failing the tour: {truncate(str(err), 48)}")
            return
        with filt:
            fr = filt.rows(Format.JSON_EACH_ROW, b'{"x":0}\n{"x":255}\n')
            kv("filter `x = 256` over UInt8", f"verdicts {verdict_string(fr)}")
            note("PROMOTES, never wraps: false for x=0 AND x=255. The insert side")
            note("of this same library stores 256 as 0 (section 5's overflow_wrap)")
            note("— which is why predicate constants must never be folded through")
            note("insert coercion (docs/reference/bindings.md §Constants are not payloads)")
        blank()

        # NULL is not true — three-valued logic collapsed at the WHERE
        # boundary.
        with lib.compile_ddl("lvl Nullable(UInt8)") as nullable_schema:
            with nullable_schema.compile_filter("lvl = 1") as nf:
                fr = nf.rows(Format.JSON_EACH_ROW, b'{"lvl":null}\n{"lvl":1}\n')
                kv("`lvl = 1` on [null, 1]",
                   f"verdicts {verdict_string(fr)}   (NULL is not true, as WHERE hides it)")
        blank()

        # The 'e' class: compiles clean, then THROWS on every row's values.
        with lib.compile_ddl("s String") as str_schema:
            with str_schema.compile_filter("s = 257") as sf:
                fr = sf.rows(Format.JSON_EACH_ROW, b'{"s":"hi"}\n')
                kv("`s = 257` over String", f"verdicts {verdict_string(fr)}")
                for fe in fr.errors:
                    kv(f"  row {fe.row}", f"code {fe.code}  {truncate(fe.err, 56)}")
                note("on a real server this WHERE fails the WHOLE query — 'e' is NOT")
                note("an answer, and neither is 'd' (a row this library declines): an")
                note("enforcing caller fails CLOSED on both, or NOT(decline-as-false)")
                note("inverts fail-closed into fail-open — the measured leak class")
        blank()

        # A bad row declines ('d'), itemized, and the tail keeps its indexes.
        with schema.compile_filter("x < 5") as lt:
            fr = lt.rows(Format.JSON_EACH_ROW, b'{"x":1}\n{"x":"zap"}\n{"x":9}\n')
            kv("`x < 5` on [1, bad, 9]",
               f"verdicts {verdict_string(fr)}   (the bad row cannot swallow the tail)")
        blank()

        # Refused at compile, never guessed — and the two REASONS are two TYPES.
        kv("compile `now() > x`", classify(lambda: schema.compile_filter("now() > x")))
        kv("compile `x = {p:UInt8}`", classify(lambda: schema.compile_filter("x = {p:UInt8}")))
        note("clock reads would be answered with THIS process's clock, not the")
        note("server's; an UNBOUND {p:Type} is the SERVER's own 456 since ABI")
        note("revision 4 (\"Substitution `p` is not set\") — bind it instead")
        kv("compile `nosuch = 1`", classify(lambda: schema.compile_filter("nosuch = 1")))
    blank()

    # -- revision 4 sub-demo: query parameters ---------------------------
    # One 3-row "event" body, shared with the twin sub-demo below.
    group("query parameters (ABI revision 4) — values are STRINGS, never escaped")
    event_body = (
        b'{"tenant":"acme","role":"admin","x":1}\n'
        b'{"tenant":"evil","role":"viewer","x":2}\n'
        b'{"tenant":"\' OR 1=1 --","role":"admin","x":3}\n'
    )
    with lib.compile_ddl("tenant String, role String, x UInt8") as ps:
        try:
            tf = ps.compile_filter("tenant = {t:String}", params={"t": "acme"})
        except chtypes.UnsupportedError:
            note("this artifact predates the params surface (relink it to ABI")
            note("revision 4, `just refresh <version>`); the sub-demo degrades")
            note("here rather than failing the tour — the surface is additive.")
        else:
            with tf:
                fr = tf.rows(Format.JSON_EACH_ROW, event_body)
            kv("`tenant = {t:String}`, t=acme", f"verdicts {verdict_string(fr)}")
            note("compiled ONCE per (schema, expr, params) — the value is baked in;")
            note("a per-tenant cache MUST be a bounded LRU + a compile throttle")

            hostile = "' OR 1=1 --"
            with ps.compile_filter("tenant = {t:String}", params={"t": hostile}) as hf:
                fr = hf.rows(Format.JSON_EACH_ROW, event_body)
            from chtypes import FilterOutcome, Verdict
            hostile_ok = fr.outcome is FilterOutcome.OK and fr.verdicts == (
                Verdict.FALSE, Verdict.FALSE, Verdict.TRUE,
            )
            kv("t = `' OR 1=1 --` (hostile)",
               f"verdicts {verdict_string(fr)}   hostile-value-inert: {hostile_ok}")
            note("the value became a typed LITERAL after SQL parsing — it matches")
            note("only the row holding exactly that string; no OR 1=1 semantics,")
            note("and NOTHING was escaped to get there (never hand-escape values)")
            note("traps: size the {brace type} for the value's domain ({p:UInt8}")
            note("given \"256\" BINDS 0 — the reader wraps); and never NAME a param")
            note("`limit`/`offset` — a real server's TCP channel refuses those")
        blank()

        # -- revision 4 sub-demo: the block twin -------------------------
        group("the block twin (ABI revision 4) — parse ONCE, evaluate K filters")
        try:
            block = ps.parse_block(Format.JSON_EACH_ROW, event_body)
        except chtypes.UnsupportedError:
            note("this artifact predates the block twin (relink it to ABI")
            note("revision 4, `just refresh <version>`); the sub-demo degrades")
            note("here rather than failing the tour — the surface is additive.")
        else:
            with block:
                with ps.compile_filter("role = 'admin'") as admin_f:
                    fr = admin_f.eval(block)
                    kv("eval `role = 'admin'`", f"verdicts {verdict_string(fr)}")
                with ps.compile_filter("role = 'viewer'") as viewer_f:
                    fr = viewer_f.eval(block)
                    kv("eval `role = 'viewer'`", f"verdicts {verdict_string(fr)}")
            note("ONE parse of the 3-row event fed BOTH filters: eval neither")
            note("consumes nor mutates the block, and eval(parse_block(body)) ≡")
            note("rows(body) for every verdict class — the live-SSE hot path is")
            note("K per-principal filters × 1 event, and the re-parse is shed")
    blank()

    note("THREADS: a filter call is ALSO a use of its schema handle — two")
    note("filters over one schema never run concurrently (the SDK enforces it)")
    note("LIFETIME: filters AND blocks free before their schema; close()")
    note("ordering is structural in every SDK — Schema.close() frees them first")
    note("ENFORCEMENT GATE: nothing may enforce read-side security on this")
    note("API until the WHERE-truth rig gates green (zero over-admit, zero")
    note("over-hide). Until that run of record exists this is a shadow/replay")
    note("surface: log disagreements, enforce with what enforced yesterday —")
    note("the twin is a call shape, not an enforcement opening.")


def verdict_string(fr: chtypes.FilterResult) -> str:
    """Render a FilterResult's verdicts as the document's compact t/f/e/d
    string."""
    if fr.outcome is not chtypes.FilterOutcome.OK:
        return f"(call-level: outcome={fr.outcome.value} code={fr.err_code} {truncate(fr.err_msg, 40)})"
    return '"' + "".join(v.value for v in fr.verdicts) + '"'


# ------------------------------------------------------------------- plumbing
#
# Everything below is printing helpers — no chtypes calls hide here.


def section(n: int, title: str) -> None:
    print(f"\n=== {n}. {title} ===")


def kv(key: str, value: str) -> None:
    print(f"  {key:<30} {value}")


def note(text: str) -> None:
    print(f"      . {text}")


def raw(text: str) -> None:
    print(text)


def blank() -> None:
    print()


def group(title: str) -> None:
    print(f"  -- {title}")


def fatal(message: str) -> None:
    print(f"\nplayground: {message}", file=sys.stderr)
    raise SystemExit(1)


def feed(schema: chtypes.Schema, label: str, fmt: Format, body: bytes) -> None:
    """Run one payload through rows() and print the verdict on one line, plus
    a "~" line per Transform (a silent change ClickHouse made)."""
    batch = schema.rows(fmt, body)
    parts = [batch.outcome.value]
    if batch.err_code:
        parts.append(f"code={batch.err_code}")
    if batch.rows:
        vals = " ".join(f"{v.column}={text_or(v)}({v.source})" for v in batch.rows[0].values)
        if vals:
            parts.append(vals)
    if batch.err_msg:
        parts.append(truncate(batch.err_msg, 56))
    kv(f"  {label}", "  ".join(parts))
    for t in batch.transformed:
        lossy = ", LOSSY" if t.lossy else ""
        raw(f"      ~ {t.column}: {t.input} -> {t.stored} ({t.reason}{lossy})")


def describe_error(call, what: str) -> None:
    """Print which typed error a call raises. The two verdict types are PEERS
    (docs/reference/bindings.md rule 12): neither except arm can catch the other's."""
    try:
        result = call()
        if hasattr(result, "close"):
            result.close()
        kv(what, "(no error)")
    except chtypes.UnsupportedError as err:
        kv(what, "UnsupportedError  (a DECLINE)")
        kv("  message", truncate(err.msg, 84))
        kv("  also a SchemaError?", "False  <- a PEER type; carries no .code at all")
    except chtypes.SchemaError as err:
        kv(what, f"SchemaError  (a REFUSAL), .code={err.code}")
        kv("  message", truncate(err.msg, 84))
        kv("  an UnsupportedError?", "False")
    except chtypes.RegistryError as err:
        kv(what, "RegistryError  (a deployment problem, not a verdict)")
        kv("  message", truncate(str(err), 84))


def classify(call) -> str:
    """Name a call's error KIND in one word."""
    try:
        result = call()
        if hasattr(result, "close"):
            result.close()
        return "accepted"
    except chtypes.UnsupportedError:
        return "DECLINED  (UnsupportedError)"
    except chtypes.SchemaError as err:
        return f"REFUSED   (SchemaError, code {err.code})"


def caught(call) -> str:
    try:
        result = call()
        if hasattr(result, "close"):
            result.close()
        return "(no error)"
    except chtypes.ChtypesError as err:
        # Both verdict arms on purpose: since the peer-type split
        # (docs/reference/bindings.md rule 12) `except SchemaError` no longer catches
        # declines, and this helper wants either arm's rendered text.
        return str(err)


def verdict(batch: chtypes.BatchResult) -> str:
    if batch.err_code:
        return f"{batch.outcome.value:<9} code={batch.err_code}  {truncate(batch.err_msg, 44)}"
    if batch.rows and batch.rows[0].values:
        v = batch.rows[0].values[0]
        return f"{batch.outcome.value:<9} {v.column} = {v.text}"
    return batch.outcome.value


def text_or(value: chtypes.Value) -> str:
    if value.text == "" and not value.null:
        return "<unreadable>"
    return value.text


def truncate(text: str, limit: int) -> str:
    text = text.replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "..."


def registry_dir() -> str:
    """$CHTYPES_REGISTRY, else the per-user artifact cache every SDK defaults to."""
    env = os.environ.get(chtypes.ENV_REGISTRY)
    if env:
        return env
    return chtypes.default_registry_dir()


def newest_line(lines: list[str]) -> str:
    """Pick the numerically highest minor line: "25.10" > "25.3", which a
    string sort gets exactly backwards."""
    return max(lines, key=lambda line: tuple(int(p) if p.isdigit() else -1 for p in line.split(".")))


if __name__ == "__main__":
    main()
