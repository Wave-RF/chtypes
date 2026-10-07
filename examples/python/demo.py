"""The Python tour of chtypes (v1).

WHAT THIS IS

chtypes answers one question: "if this row were inserted into this table on
this ClickHouse version, what would happen?" - without a server. The answers
come from ClickHouse's own C++ (vendored per release into a shared library
behind the frozen chs_* C ABI), which is why they are exact rather than
approximately right.

This file is a tutorial you RUN. Seventeen numbered sections walk the public
API of the Python SDK (docs/reference/bindings-v1.md), from setting up the
process to teardown, each with a comment saying what it demonstrates, why an
ingest pipeline cares, and what to look at in the output. The same seventeen
sections, with the same numbering, schemas and rows, exist in the Go,
TypeScript and Rust tours, so you can diff two tours and see only the language
idioms differ.

Nothing here needs a ClickHouse server. It needs one installed artifact: fetch
it once with `chtypes fetch 26.8` (or `python -m chtypes fetch 26.8`), or set
CHTYPES_AUTOFETCH=1 to let the first open fetch it. Even the discovery
section (11) runs offline, against CANNED bytes shaped like a real server's
answer.

    uv run demo.py                        # the newest installed build
    CHTYPES_VERSION=26.8 uv run demo.py   # pick a release line or an exact version
    ../chplay.sh python                   # same, with prerequisite checks

Nothing here is a test: the real suites live in python/tests. Every value
printed below is produced by the run, never written down by hand.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from typing import Any, NoReturn

import chtypes
from chtypes import DocFlags, Format, Outcome, Registry, Source, Verdict

# ------------------------------------------------------------------ the fixture
#
# These constants are IDENTICAL in all four tours. Change one here and change
# it in the others too: the point of this directory is that the four outputs
# can be diffed.

# One CREATE TABLE statement is the whole input to a compile: the engine, the
# TTL and the partition key are part of it, never separate calls. This table
# holds one of everything the tour needs: a volatile DEFAULT (now64), a
# literal DEFAULT, an Enum (how a table gets poisoned), and a MATERIALIZED
# column (never in SELECT *).
DEMO_DDL = """CREATE TABLE demo (
    ts DateTime64(3) DEFAULT now64(3),
    device_id UInt32,
    seq UInt8 DEFAULT 0,
    payload String,
    grade Enum8('a' = 1, 'b' = 2),
    payload_len UInt32 MATERIALIZED length(payload)
) ENGINE = MergeTree ORDER BY device_id"""

# A small three-column table for the outcome and format sections. Positional
# formats (CSV, TSV, Values, RowBinary...) are far easier to read against a
# small schema, and both DEFAULTs give the empty-field rules something to do.
FORMAT_DDL = """CREATE TABLE events (
    device_id UInt32,
    seq UInt8 DEFAULT 7,
    label String DEFAULT 'unknown'
) ENGINE = MergeTree ORDER BY device_id"""

# Hand-built binary payloads (hex), shared by all four tours. Each is
# explained where it is fed.
ROW_BINARY_OK = "0100000007026f6b"  # UInt32 LE 1, UInt8 7, varint-len "ok"
RBWD_MARKER = "00020000000100026869"  # device_id=2 by value, seq by marker, label="hi"
RBWD_POISON = "000100000001"  # id=1 by value, e by marker -> raw 0, NO name
RBWD_VALUE = "00010000000002"  # id=1 by value, e by value 2 ("green")
# Native block captured from a live 25.8 (SELECT ... FORMAT Native).
NATIVE_OK = (
    "0301096465766963655f69640655496e74333201000000037365710555496e743807"
    "056c6162656c06537472696e67026f6b"
)
# Buffers: uint64le n_columns, n_rows, then per column a byte size and the raw
# column. 4 bytes ff ff ff ff, declared 4 wide, then (wrongly) 8 wide.
BUFFERS_OK = "010000000000000001000000000000000400000000000000ffffffff"
BUFFERS_WIDE = "010000000000000001000000000000000800000000000000ffffffff"

# Section 11's CANNED server answer: shaped like a stock HTTP server's
# JSONEachRow answer to the library's own discovery query (the four columns
# it selects). Swap in your own client's bytes and nothing else changes.
CANNED_COLUMNS_RESULT = (
    b'{"name":"ts","type":"DateTime64(3)","default_kind":"DEFAULT",'
    b'"default_expression":"now64(3)"}\n'
    b'{"name":"device_id","type":"UInt32","default_kind":"",'
    b'"default_expression":""}\n'
    b'{"name":"reading c","type":"Float64","default_kind":"",'
    b'"default_expression":""}\n'
    b'{"name":"note","type":"String","default_kind":"DEFAULT",'
    b'"default_expression":"\'unset\'"}\n'
)


def main() -> None:
    registry, lib = section1()
    section2(lib)
    section3(lib)
    section4(lib)
    section5(lib)
    section6(lib)
    section7(lib)
    section8(lib)
    section9(lib)
    section10(lib, registry)
    section11(lib)
    section12(registry)
    section13(lib)
    section14()
    section15(lib)
    section16(lib)
    section17(lib)
    blank()
    print("Done. Every value above was measured by this run.")


# ---------------------------------------------------------------------------
# SECTION 1 - Set up, open a library, check what it is
#
# WHAT: choose the process setup, open the registry, see what is installed,
# open one build, and read what it says about itself.
# WHY: an ingest gateway serves tenants on different ClickHouse versions at
# once; the registry is how one process answers for all of them, exactly. The
# build names ITSELF: nothing is ever inferred from a path or a file name.
# LOOK FOR: the signed fetch record behind the library (who signed it, from
# which source), the capabilities the build lists, and the two refusals: a
# decorated version spelling, and a version nothing installed answers.
# C API: chs_initialize (once per image, at load), chs_build_info.
# ---------------------------------------------------------------------------
def section1() -> tuple[Registry, chtypes.Library]:
    section(1, "Set up, open a library, check what it is")

    # The image zone governs compiled types (a bare DateTime column's zone,
    # MATERIALIZED, PARTITION BY, TTL). It is chosen once, before the first
    # open; "UTC" is also the default when setup is never called.
    chtypes.setup(timezone="UTC")
    kv("setup", "timezone=UTC  (once per process, before the first open)")

    registry = Registry()  # opens nothing; autofetch follows $CHTYPES_AUTOFETCH
    installed = sorted(registry.installed(), key=lambda r: version_key(r.version))
    kv(
        "cache",
        os.environ.get("CHTYPES_CACHE", "(the per-user default; `chtypes where` prints it)"),
    )
    kv("installed", "  ".join(f"{r.version}/{r.platform}" for r in installed) or "(nothing)")
    want = os.environ.get("CHTYPES_VERSION")
    if want:
        kv("version requested", f"{want}  (from $CHTYPES_VERSION)")
    elif installed:
        want = installed[-1].version
        kv("version requested", f"{want}  (default: the newest installed; set $CHTYPES_VERSION)")
    else:
        fatal("nothing is installed. Run `chtypes fetch 26.8`, or set CHTYPES_AUTOFETCH=1.")
    assert want is not None
    try:
        lib = registry.for_version(want)
    except chtypes.ArtifactError as err:
        fatal(f"{type(err).__name__} ({err.code}): {err}")

    info = lib.build_info
    kv("library reports", f"{lib.version}  (release line {lib.minor}, channel {info.channel})")
    kv("build", f"{info.build}  on {info.os}/{info.arch}")
    kv("ABI generation / fingerprint", f"{info.abi} / {info.abi_fingerprint[:23]}...")
    kv("formats it reads", ", ".join(info.capabilities.input_formats))
    kv("export formats", ", ".join(info.capabilities.export_formats))
    kv("features", ", ".join(info.capabilities.features) or "(none)")
    rec = lib.resolved
    if isinstance(rec, chtypes.Resolved):
        kv("signed by key", f"{rec.signed_by}  (source: {rec.source})")
    note("the loader refused anything but a library whose signed statement, build")
    note("info and symbols all agree - there is no unverified path through a registry")
    blank()

    # The two refusals a gateway sees at open time, told apart by TYPE.
    for spelling in ("v" + lib.minor, "99.9"):
        try:
            registry.for_version(spelling)
            kv(f"asking for {spelling}", "(no error!?)")
        except chtypes.ChtypesError as err:
            kv(f"asking for {spelling}", f"{type(err).__name__}: {truncate(str(err), 60)}")
    note("a version spelling is a bare 26.8 / 26.8.15 / 26.8.15.10: a leading v or a")
    note("-lts suffix is a UsageError before any I/O. A request nothing answers is an")
    note("ArtifactError: never a silent nearest-version fallback")
    return registry, lib


# ---------------------------------------------------------------------------
# SECTION 2 - Ask a build about itself
#
# WHAT: type validation and canonicalization, quoting, and the error-code
# table, straight from this build's own ClickHouse code.
# WHY: canonicalization is how you compare a tenant's declared type against
# what the server will actually store; quoting is how you spell a name or a
# literal exactly as ClickHouse does. A binding keeps no rule of its own.
# LOOK FOR: Variant members being SORTED, BIGINT becoming Int64, an unknown
# family carrying ClickHouse's own code and name, and every name coming back
# as BYTES.
# C API: chs_type_validate, chs_back_quote, chs_back_quote_if_needed,
# chs_quote_string, chs_error_codes.
# ---------------------------------------------------------------------------
def section2(lib: chtypes.Library) -> None:
    section(2, "Ask a build about itself")

    kv("validate_type", "input -> this build's canonical spelling (bytes)")
    for t in ("Decimal(18,4)", "Variant(UInt8, String)", "BIGINT", "LowCardinality( String )"):
        kv(f"  {t}", "-> " + show(lib.validate_type(t)))
    note("compare canonical strings verbatim, never re-normalize whitespace")
    blank()

    try:
        lib.validate_type("NotAType")
    except chtypes.SchemaError as err:
        kv("validate_type(NotAType)", f"SchemaError ch_code={err.ch_code} ch_name={err.ch_name}")
        kv("  message (bytes)", truncate(show(err.message), 70))
        note("the server's own code and message: chtypes never hand-writes an error")
    blank()

    kv("quote_identifier", show(lib.quote_identifier("reading c")))
    kv(
        "quote_identifier_if_needed",
        f"{show(lib.quote_identifier_if_needed('plain'))}  /  "
        f"{show(lib.quote_identifier_if_needed('reading c'))}",
    )
    kv("quote_literal", show(lib.quote_literal("it's")))
    note("a name that is not UTF-8 round-trips: pass bytes in, get bytes out")
    blank()

    codes = lib.error_codes()
    kv("error_codes", f"{len(codes)} codes in this build's table")
    kv("  name(50) / code('UNKNOWN_TYPE')", f"{codes.name(50)} / {codes.code('UNKNOWN_TYPE')}")
    note("the table is the build's own: an unknown code answers None, never a guess")


# ---------------------------------------------------------------------------
# SECTION 3 - Compile a schema and read it back
#
# WHAT: compile exactly one CREATE TABLE statement and describe() the
# compiled columns: canonical types, DEFAULT kinds and expressions.
# WHY: the compiled handle IS the table, as this ClickHouse version would
# create it. Two rewrites below are things no type-string comparison could
# ever catch: the compile is schema-aware.
# LOOK FOR: DEFAULT NULL turning Int64 into Nullable(Int64), an ALIAS column
# whose type is INFERRED, and a failed compile being a typed error.
# C API: chs_schema_create, chs_schema_describe, chs_schema_free (close).
# ---------------------------------------------------------------------------
def section3(lib: chtypes.Library) -> None:
    section(3, "Compile a schema and read it back")

    kv("the statement", "")
    for line in DEMO_DDL.split("\n"):
        raw("      " + line)
    with lib.compile_table(DEMO_DDL) as schema:
        blank()
        kv("described columns", "name  type  (default kind + expression)")
        for c in schema.describe().columns:
            extra = ""
            if c.default_kind is not chtypes.DefaultKind.NONE:
                extra = f"  {c.default_kind.value} {show(c.default_expr)}"
            kv(f"  {show(c.name)}", show(c.type) + extra)
    note("payload_len is MATERIALIZED: compiled and described, but never read from")
    note("input - watch it come back separately in section 6")
    blank()

    with lib.compile_table(table("x Int64 DEFAULT NULL")) as schema:
        c = schema.describe().columns[0]
        kv("x Int64 DEFAULT NULL", f"describes as {show(c.type)} DEFAULT {show(c.default_expr)}")
    note("the DEFAULT rewrote the type: the server does this at CREATE, so the")
    note("compile must too, or every later verdict drifts")
    with lib.compile_table(table("a UInt8, al ALIAS a + 1")) as schema:
        kv("a UInt8, al ALIAS a + 1", "al describes as " + show(schema.describe().columns[1].type))
    note("an ALIAS column's type is inferred from its expression, by ClickHouse")
    blank()

    kv("x NotAType", classify(lambda: lib.compile_table(table("x NotAType"))))
    note(truncate(caught(lambda: lib.compile_table(table("x NotAType"))), 90))


# ---------------------------------------------------------------------------
# SECTION 4 - Compile under a settings profile
#
# WHAT: the same compile with a DECLARED settings profile: the settings a
# real server would have had at CREATE TABLE.
# WHY: some settings change the SHAPE of a table (flatten_nested), some gate
# which TYPES may exist (allow_suspicious_low_cardinality_types). A gateway
# discovers a deployment's settings once and declares them here; the handle
# then behaves like a table created on THAT server.
# LOOK FOR: one statement compiling to two different column lists; a type gate
# refusing with the server's own code; a typo'd setting name refusing with the
# server's own 115, did-you-mean hint included. Settings values are STRINGS.
# C API: chs_schema_create (the settings argument).
# ---------------------------------------------------------------------------
def section4(lib: chtypes.Library) -> None:
    section(4, "Compile under a settings profile")

    nested = table("id UInt32, n Nested(a UInt8, b String)", order="id")
    kv("(a) flatten_nested", "id UInt32, n Nested(a UInt8, b String)")
    for value in ("1", "0"):
        with lib.compile_table(nested, settings={"flatten_nested": value}) as schema:
            cols = schema.describe().columns
            kv(f"  ={value}", " | ".join(f"{show(c.name)} {show(c.type)}" for c in cols))
    note("under 1 (the stock default) a Nested column is stored FLATTENED as two")
    note("Arrays; under 0 it is one Array(Tuple) column. Every later answer follows")
    blank()

    kv("(b) a type gate", "lc LowCardinality(UInt8), allow_suspicious_low_cardinality_types")
    for value in ("0", "1"):
        try:
            with lib.compile_table(
                table("lc LowCardinality(UInt8)"),
                settings={"allow_suspicious_low_cardinality_types": value},
            ) as schema:
                kv(f"  ={value}", "compiled: " + show(schema.describe().columns[0].type))
        except chtypes.SchemaError as err:
            kv(f"  ={value}", f"REFUSED, the server's own code {err.ch_code}")
            note(truncate(show(err.message), 92))
    blank()

    try:
        lib.compile_table(table("a UInt8"), settings={"flatten_nestedd": "1"})
    except chtypes.SchemaError as err:
        kv("(c) unknown setting name", f"flatten_nestedd -> code {err.ch_code} ({err.ch_name})")
        note(truncate(show(err.message), 96))
    blank()

    # Settings are strings and only strings: the binding writes them verbatim.
    try:
        lib.compile_table(table("a UInt8"), settings={"flatten_nested": 1})  # type: ignore[dict-item]
    except TypeError as err:
        kv("(d) a non-string value", f"TypeError: {err}")
    note("no boolean or integer spelling is rewritten for you: say '1', not 1")


# ---------------------------------------------------------------------------
# SECTION 5 - Accept, reject, decline (and poison)
#
# WHAT: the verdicts every row lands on, plus poisoned, which looks like an
# accept and bites at read time, and skipped, the per-row verdict of a batch
# that drops bad rows.
# WHY: this is the contract of the whole product. An ingest gateway routes on
# exactly this: accepted -> insert and publish the STORED values; rejected ->
# 400 the producer with the server's own message; unsupported -> chtypes
# refuses to guess, so fall back to the real server and NEVER convert the
# decline into an accept or a reject yourself.
# LOOK FOR: the accept carrying a visible coercion (input 256, stored 0); the
# reject carrying ClickHouse's own error text; the decline carrying no
# ClickHouse code at all.
# C API: chs_preview_batch, chs_preview_row.
# ---------------------------------------------------------------------------
def section5(lib: chtypes.Library) -> None:
    section(5, "Accept, reject, decline (and poison)")
    kv("schema", "events (device_id UInt32, seq UInt8 DEFAULT 7, label String DEFAULT 'unknown')")
    blank()

    with lib.compile_table(FORMAT_DDL) as schema:
        kv("(a) ACCEPT", '{"device_id":1,"seq":256,"label":"ok"}')
        feed(schema, "JSONEachRow", Format.JSON_EACH_ROW, b'{"device_id":1,"seq":256,"label":"ok"}')
        note("accepted, but look at seq: input 256, stored 0. The ~ line is the")
        note("Transform (reason overflow_wrap, LOSSY). ClickHouse reports success for")
        note("every one of these: publish the STORED value, not the payload value.")
        blank()

        kv("(b) REJECT", '{"device_id":"abc"}')
        feed(schema, "JSONEachRow", Format.JSON_EACH_ROW, b'{"device_id":"abc"}')
        note("the code and message are ClickHouse's OWN: your 400 body matches what a")
        note("real INSERT would have said")
        blank()

        kv("(c) DECLINE", "(2,7,concat('a','b'))  as Values")
        feed(schema, "Values", Format.VALUES, b"(2,7,concat('a','b'))")
        note("unsupported means 'a real server MIGHT WELL accept this; I will not")
        note("guess'. Do not 400 the producer, do not publish: send it to the real")
        note("server unpreviewed and let it decide")
        blank()

    kv("(d) POISON", "an accept that bites at read time")
    poison_ddl = table("id UInt32, e Enum8('red' = 1, 'green' = 2)", order="id")
    with lib.compile_table(poison_ddl) as schema:
        kv("  payload (RBWD)", RBWD_POISON + "   (id=1 by value, e by marker byte)")
        feed(schema, "RBWD marker", Format.ROW_BINARY_WITH_DEFAULTS, bytes.fromhex(RBWD_POISON))
        note("accepted_poisoned: the marker fills the column with the raw zero, and an")
        note("Enum8('red'=1,'green'=2) has no name for 0. The insert succeeds, every")
        note("later SELECT fails. It is an ACCEPT variant, never a rejection")
        kv("  control payload", RBWD_VALUE + "   (e supplied by value = 2)")
        feed(schema, "RBWD value", Format.ROW_BINARY_WITH_DEFAULTS, bytes.fromhex(RBWD_VALUE))
    blank()

    kv("(e) SKIP", "a bad middle row under input_format_allow_errors_num=10")
    with lib.compile_table(FORMAT_DDL) as schema:
        body = (
            b'{"device_id":1,"seq":1,"label":"a"}\n'
            b'{"device_id":"oops"}\n'
            b'{"device_id":3,"seq":3,"label":"c"}\n'
        )
        batch = schema.rows(
            Format.JSON_EACH_ROW, body, settings={"input_format_allow_errors_num": "10"}
        )
        kv(
            "  batch",
            f"{batch.outcome}  rows_read={batch.rows_read} rows_skipped={batch.rows_skipped}",
        )
        for i, r in enumerate(batch.rows):
            line = str(r.outcome)
            if r.outcome is Outcome.SKIPPED:
                line += f"  code={r.err_code} {truncate(show(r.err_msg), 48)}"
            kv(f"  row {i}", line)
        note("one rows() call answers per input record, IN ORDER. A SKIPPED row is")
        note("never stored: forward only the survivors")
        kv("  unconsumed", f"{len(batch.unconsumed)} byte range(s) the reader's recovery skipped")
        note("a skipped row's span can cover several records, so verdicts can be fewer")
        note("than records with unconsumed empty, and a record count can be fooled:")
        note("decline on any skipped row or any unconsumed range")


# ---------------------------------------------------------------------------
# SECTION 6 - DEFAULT evaluation: where every value comes from
#
# WHAT: one row through the demo table, then reading back WHERE each stored
# value came from (Value.source and Value.is_stored), the MATERIALIZED
# values, and every change the library made.
# WHY: an INSERT is mostly values the row did NOT supply. A gateway that
# cannot answer "what will the table hold for this column?" cannot preview an
# insert. A volatile DEFAULT is the sharp edge: the library resolved now64()
# from ITS clock, so the caller must send that column explicitly, by inserting
# the library's export (section 15), or the server stamps its own.
# LOOK FOR: different sources in one row; columns (every entry) versus values
# (only the stored ones); payload_len under computed; "bogus" under
# unknown_fields; a generated DEFAULT, when this build has the feature.
# C API: chs_preview_row.
# ---------------------------------------------------------------------------
def section6(lib: chtypes.Library) -> None:
    section(6, "DEFAULT evaluation: where every value comes from")

    with lib.compile_table(DEMO_DDL) as schema:
        body = b'{"device_id":42,"seq":256,"payload":"hello","grade":"a","bogus":1}'
        kv("row fed", body.decode())
        note("ts is OMITTED on purpose; bogus matches no column")
        r = schema.row(Format.JSON_EACH_ROW, body)
        blank()

        kv("outcome / err_code", f"{r.outcome.value} / {r.err_code}")
        kv("columns[]  (every entry)", "column = text  (source, stored?)")
        for v in r.columns:
            kv(f"  {show(v.column)}", f"{text_or(v):<28} ({v.source}, stored={v.is_stored})")
        note("source says where the value came from; is_stored is the description's")
        note("own fact for that source. values[] is exactly the stored subset:")
        kv("values[]", ", ".join(show(v.column) for v in r.values))
        note("text is ClickHouse's OWN rendering, as bytes - never re-serialized here")
        blank()

        kv("computed[]", "MATERIALIZED values: durable, but never in SELECT *")
        for c in r.computed:
            kv(f"  {show(c.column)}", f"{c.kind}  =  {show(c.text)}")
        blank()

        kv("transformed[]", "every silent change, with a machine-readable reason")
        for t in r.transformed:
            kv(
                f"  {t.reason}",
                f"{show(t.column)}: {show(t.input) or '(absent)'} -> {show(t.stored)}   lossy={t.lossy}",
            )
        note("lossy is the description's fact for each reason, never a list kept here")
        blank()

        kv("unknown_fields[]", str([show(n) for n in r.unknown_fields]))
        kv("unsupported_settings[]", str([show(n) for n in r.unsupported_settings]))
        blank()

    with lib.compile_table(table("a UInt8, d UInt8 DEFAULT a + 1")) as dep:
        kv("row-dependent DEFAULT", "a UInt8, d UInt8 DEFAULT a + 1   fed CSV `7,`")
        feed(dep, "CSV", Format.CSV, b"7,")
        note("the bare empty CSV field takes the DEFAULT, and the DEFAULT reads a=7")
        note("from the same row: d stores 8, exactly as the server computes it")
    blank()

    # A DEFAULT that draws a random value: the library draws it with
    # ClickHouse's own generator and says so.
    generators = "default_generators" in lib.build_info.capabilities.features
    kv("generated DEFAULT", "id UInt32, token UUID DEFAULT generateUUIDv4()")
    if not generators:
        note("this build does not list the default_generators feature; skipped")
        return
    with lib.compile_table(
        table("id UInt32, token UUID DEFAULT generateUUIDv4()", order="id")
    ) as g:
        batch = g.rows(Format.JSON_EACH_ROW, b'{"id":1}', export=Format.JSON_COMPACT_EACH_ROW)
        token = next(v for v in batch.rows[0].columns if v.column == b"token")
        kv(
            "  token.source",
            f"{token.source}  (Source.DEFAULT_GENERATED = {Source.DEFAULT_GENERATED})",
        )
        kv("  value drawn", show(token.text))
        kv("  export payload", repr(batch.payload))
        note("a server draws a DEFAULT only for a column the INSERT leaves out, so INSERT")
        note("the export payload (it carries the drawn value), never the original body")


# ---------------------------------------------------------------------------
# SECTION 7 - One schema, every format
#
# WHAT: the same three-column schema fed in the formats this build lists in
# its capabilities: accept and reject for each text format, the two header
# formats, then the binary tier with hand-built bytes.
# WHY: format is not cosmetic. Each format has signature behaviors (CSV's
# bare-vs-quoted empty field, RBWD's marker byte, Native's silent CAST,
# Buffers' silent reinterpret) that change what the table ends up holding.
# LOOK FOR: the same logical row giving format-specific verdicts. Which formats
# a build reads is capabilities.input_formats, never a probe by calling.
# C API: chs_preview_batch with format codes 0..11.
# ---------------------------------------------------------------------------
def section7(lib: chtypes.Library) -> None:
    section(7, "One schema, every format")
    kv("schema", "events (device_id UInt32, seq UInt8 DEFAULT 7, label String DEFAULT 'unknown')")
    blank()

    with lib.compile_table(FORMAT_DDL) as schema:
        group("text formats (one row per line; positional or named)")
        feed(
            schema,
            "JSONEachRow  accept",
            Format.JSON_EACH_ROW,
            b'{"device_id":1,"seq":7,"label":"ok"}',
        )
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
        note("Values has an explicit DEFAULT keyword; an SQL expression is a DECLINE,")
        note("evaluated by a real server and guessed by nobody")
        blank()

        kv("CSV empty-field rule", "bare empty takes the DEFAULT; quoted empty is ''")
        feed(schema, "CSV   bare   2,7,", Format.CSV, b"2,7,")
        feed(schema, 'CSV   quoted 3,7,""', Format.CSV, b'3,7,""')
        blank()

        group("header formats (the first row NAMES the columns)")
        for fmt, label, body in (
            (Format.CSV_WITH_NAMES, "CSVWithNames accept", b"device_id,seq,label\n1,7,ok"),
            (Format.CSV_WITH_NAMES, "CSVWithNames reorder", b"label,device_id,seq\nok,1,7"),
            (Format.TSV_WITH_NAMES, "TSVWithNames accept", b"device_id\tseq\tlabel\n1\t7\tok"),
            (Format.CSV_WITH_NAMES, "CSVWithNames CASE", b"DEVICE_ID,seq,label\n1,7,ok"),
        ):
            if supports(lib, fmt):
                feed(schema, label, fmt, body)
            else:
                kv(f"  {label}", f"(not in this build's input_formats: {fmt.ch_name})")
        note("the reordered header stores the SAME row: data is addressed by NAME. Header")
        note("name matching can differ between lines: the artifact's answer, not the SDK's")
        blank()

        group("binary formats (bytes, COUNTED - never NUL-terminated)")
        kv("  RowBinary payload", ROW_BINARY_OK)
        feed(schema, "RowBinary    accept", Format.ROW_BINARY, bytes.fromhex(ROW_BINARY_OK))
        note('4-byte LE UInt32 (1), 1-byte UInt8 (7), varint-length String ("ok")')
        feed(schema, "RowBinary    reject", Format.ROW_BINARY, b"\x01\x00")
        note("truncated mid-row: framing faults are all-or-nothing per batch")
        kv("  RBWD payload", RBWD_MARKER)
        feed(
            schema, "RBWD  marker byte", Format.ROW_BINARY_WITH_DEFAULTS, bytes.fromhex(RBWD_MARKER)
        )
        note("RowBinaryWithDefaults prefixes each column with a marker byte: 00 means")
        note("'value follows', nonzero means 'compute the DEFAULT'. seq's marker is 01")
        blank()

        group("Native: self-describing, and it CASTs")
        feed(schema, "Native       accept", Format.NATIVE, bytes.fromhex(NATIVE_OK))
        note("a Native block declares its OWN column names and types (captured from a")
        note("live 25.8 SELECT ... FORMAT Native)")
    blank()

    group("Buffers: NO self-description at all")
    if not supports(lib, Format.BUFFERS):
        note("this build does not list Buffers in its input_formats; skipped")
        return
    with lib.compile_table(table("x Int32")) as schema:
        kv("  payload", "4 bytes ff ff ff ff, declared x Int32")
        feed(schema, "Buffers reinterpret", Format.BUFFERS, bytes.fromhex(BUFFERS_OK))
        note("a UInt32 producer's 4294967295 reads back as -1: same width, no metadata,")
        note("so no check CAN fire. The schema is entirely out of band")
        feed(schema, "Buffers width", Format.BUFFERS, bytes.fromhex(BUFFERS_WIDE))
        note("the same 4 bytes declared 8 wide IS caught: size accounting disagrees")


# ---------------------------------------------------------------------------
# SECTION 8 - Engines, MergeTree settings, TTL and partitions
#
# WHAT: declare the table's engine, TTL and partition key IN the statement,
# then watch the STORAGE layer change what a batch stores.
# WHY: a row can be accepted per row and absent per batch. SummingMergeTree
# folds rows at insert, so a gateway reading only per-row verdicts previews
# rows the table will never hold. A TTL is different: it deletes at the next
# MERGE, which a preview does not perform, so engine_rows matches the INSERT.
# LOOK FOR: engine_rows (the stored truth) being SHORTER than the input for
# SummingMergeTree; the TTL batch whose row is accepted and whose engine_rows
# still HOLDS it (before build 20261007.120436 it was empty and a ttl_expired
# transform was reported); the refusal-versus-decline pair on a MergeTree
# SETTINGS clause; the partition id.
# C API: chs_schema_create, chs_preview_batch.
# ---------------------------------------------------------------------------
def section8(lib: chtypes.Library) -> None:
    section(8, "Engines, MergeTree settings, TTL and partitions")

    summing = (
        "CREATE TABLE sums (day Date, key UInt32, v UInt64) "
        "ENGINE = SummingMergeTree ORDER BY (day, key)"
    )
    with lib.compile_table(summing) as schema:
        kv("(a) the engine", "SummingMergeTree ORDER BY (day, key)")
        body = b'{"day":"2026-01-01","key":1,"v":5}\n{"day":"2026-01-01","key":1,"v":7}'
        batch = schema.rows(Format.JSON_EACH_ROW, body)
        kv("  rows in / rows_read", f"2 / {batch.rows_read}   (v=5 and v=7, same key)")
        kv("  engine_rows (stored)", render_engine_rows(batch))
    note("two rows in, ONE row out, v summed: engine_rows is the post-merge preview")
    blank()

    kv("(b) MergeTree SETTINGS", "what this build answers, told apart by type")
    for label, clause in (
        ("unknown NAME", "index_granularityy = 8192"),
        ("read on insert", "index_granularity = 4096"),
        ("storage-only", "old_parts_lifetime = 100"),
        ("known, AT default", "index_granularity = 8192"),
    ):
        stmt = f"CREATE TABLE s (a UInt8) ENGINE = MergeTree ORDER BY tuple() SETTINGS {clause}"
        kv(f"  {label}", f"{clause} -> " + classify(lambda stmt=stmt: lib.compile_table(stmt)))
    note("a refusal carries the server's own code (this DDL can never exist); a decline")
    note("means this build will not model the setting, and a real server might accept;")
    note("a setting only the storage layer reads is accepted: the rows stored do not change")
    blank()

    ttl_ddl = "CREATE TABLE ttl_t (ts DateTime, v UInt8) ENGINE = MergeTree ORDER BY ts TTL ts + INTERVAL 1 DAY"
    with lib.compile_table(ttl_ddl) as schema:
        kv("(c) TTL", "MergeTree ORDER BY ts, TTL ts + INTERVAL 1 DAY")
        batch = schema.rows(Format.JSON_EACH_ROW, b'{"ts":"2020-01-01 00:00:00","v":9}')
        kv("  row fed", '{"ts":"2020-01-01 00:00:00","v":9}  (long past the TTL)')
        kv("  row-level outcome", f"{batch.rows[0].outcome.value}   <- the row PARSED fine")
        kv("  engine_rows (stored)", render_engine_rows(batch))
        for t in batch.transformed:
            kv(
                "  batch transform",
                f"row={t.row} column={show(t.column)} reason={t.reason} lossy={t.lossy}",
            )
        note("accepted per ROW, and engine_rows keeps it: a server's INSERT still writes the")
        note("row and the next merge deletes it, which a preview does not perform. On builds")
        note("before 20261007.120436 engine_rows was empty and a ttl_expired transform was")
        note("reported; the loop above now prints nothing")
    blank()

    kv(
        "(d) a clock-reading TTL",
        classify(
            lambda: lib.compile_table(
                "CREATE TABLE t2 (ts DateTime) ENGINE = MergeTree ORDER BY ts TTL now() + INTERVAL 1 DAY"
            )
        ),
    )
    blank()

    part_ddl = (
        "CREATE TABLE parts (ts DateTime, v UInt8) ENGINE = MergeTree "
        "PARTITION BY toYYYYMM(ts) ORDER BY ts"
    )
    with lib.compile_table(part_ddl) as schema:
        body = b'{"ts":"2026-01-15 00:00:00","v":1}\n{"ts":"2026-02-15 00:00:00","v":2}'
        batch = schema.rows(Format.JSON_EACH_ROW, body)
        kv("(e) PARTITION BY toYYYYMM(ts)", f"partition_count={batch.partition_count}")
        for i, r in enumerate(batch.rows):
            kv(
                f"  row {i}",
                f"partition_id={show(r.partition_id) if r.partition_id is not None else None}",
            )
        note("both are None unless the statement declared a partition key")


# ---------------------------------------------------------------------------
# SECTION 9 - Settings and zones: who wins
#
# WHAT: the same row and the same handle, answered differently as settings are
# supplied at each layer:
#
#     per-call  >  compile profile  >  setup defaults  >  ClickHouse's own
#
# and the two time zones: the IMAGE zone (setup, once) and the per-call zone
# (session_timezone, any call).
# WHY: this is how a gateway declares a deployment's settings ONCE (at compile)
# yet still lets one INSERT override per call. If precedence were fuzzy, the
# declared profile would not actually be in force.
# LOOK FOR: layers 3 vs 4 - the SAME handle, the SAME bytes, passing under the
# profile and failing the moment a per-call value overrides it; one timestamp
# rendered under two session zones; a zone given twice being a UsageError; and
# a second, different setup being refused.
# C API: chs_preview_batch (the settings argument).
# ---------------------------------------------------------------------------
def section9(lib: chtypes.Library) -> None:
    section(9, "Settings and zones: who wins")
    kv("the probe", 'ts DateTime  fed  {"ts":"2026-01-15T10:30:00Z"}')
    note("stock ClickHouse parses 'basic' datetimes only on some lines; best_effort")
    note("accepts ISO-8601 - so the verdict TELLS you which setting value won")
    blank()

    iso = b'{"ts":"2026-01-15T10:30:00Z"}'
    basic = {"date_time_input_format": "basic"}
    best_effort = {"date_time_input_format": "best_effort"}
    kv("library", f"{lib.version}  (the selected build)")

    with (
        lib.compile_table(table("ts DateTime", order="ts")) as plain,
        lib.compile_table(table("ts DateTime", order="ts"), settings=best_effort) as profiled,
    ):
        kv("1. ClickHouse defaults", verdict(plain.rows(Format.JSON_EACH_ROW, iso)))
        kv("2.  + per-call basic", verdict(plain.rows(Format.JSON_EACH_ROW, iso, settings=basic)))
        kv("3. profile best_effort", verdict(profiled.rows(Format.JSON_EACH_ROW, iso)))
        kv(
            "4.  + per-call basic",
            verdict(profiled.rows(Format.JSON_EACH_ROW, iso, settings=basic)),
        )
        note("3 vs 4 is the requirement, measured: the same row PASSES under the profile")
        note("and FAILS when the per-call value overrides it")
        blank()

        # The per-call zone is a settings key and nothing more.
        local = b'{"ts":"2026-01-15 10:30:00"}'
        for zone in ("UTC", "Asia/Tokyo"):
            batch = plain.rows(Format.JSON_EACH_ROW, local, session_timezone=zone)
            kv(f"5. session_timezone={zone}", verdict(batch))
        note("the per-call zone governs parsing, rendering and DEFAULT evaluation of that")
        note("call; a compiled type keeps the IMAGE zone (section 1's setup)")
        try:
            plain.rows(
                Format.JSON_EACH_ROW,
                local,
                settings={"session_timezone": "UTC"},
                session_timezone="UTC",
            )
        except chtypes.UsageError as err:
            kv("6. zone given twice", f"UsageError: {truncate(show(err.message), 64)}")
        note("two spellings of one zone are refused before the call, even when they agree")
        blank()

    kv("setup, again", "same setup: a no-op; a different zone: refused")
    chtypes.setup(timezone="UTC")
    kv("  setup(timezone='UTC')", "accepted (the setup already in effect)")
    try:
        chtypes.setup(timezone="Asia/Tokyo")
    except chtypes.UsageError as err:
        kv("  setup(timezone='Asia/Tokyo')", f"UsageError: {truncate(show(err.message), 58)}")
    note("the image zone and the default settings are process-wide in ClickHouse, so")
    note("they are fixed once, before traffic; there is no setter to call mid-run")


# ---------------------------------------------------------------------------
# SECTION 10 - The error taxonomy
#
# WHAT: every kind of answer this SDK gives, told apart BY TYPE, never by
# string matching.
# WHY: the kinds demand different reactions: tell the tenant (a refusal), fall
# back cautiously (a decline), fix the caller (a misuse), report a bug (an
# internal error), fix the deployment (an artifact error). The refusal and the
# decline are PEERS, so a bare `except SchemaError` can never swallow a decline.
# LOOK FOR: the issubclass line printing False; the five fields every call
# error carries; a closed object being a UsageError; and the artifact family.
# ---------------------------------------------------------------------------
def section10(lib: chtypes.Library, registry: Registry) -> None:
    section(10, "The error taxonomy")

    kv("the Python idiom", "peer arms - the type IS the answer")
    raw("      try:")
    raw("          schema.compile_filter(expr)")
    raw("      except chtypes.UnsupportedError:   # a DECLINE: validate cautiously elsewhere")
    raw("          ...")
    raw("      except chtypes.SchemaError as e:   # a REFUSAL: e.ch_code is ClickHouse's own")
    raw("          ...")
    raw("      except chtypes.UsageError:         # a misuse: fix the caller")
    raw("          ...")
    raw("      except chtypes.ArtifactError as e: # a deployment problem: e.code says which")
    raw("          ...")
    kv(
        "  issubclass(UnsupportedError, SchemaError)",
        str(issubclass(chtypes.UnsupportedError, chtypes.SchemaError)),
    )
    kv(
        "  all four share CallError",
        str(
            all(
                issubclass(c, chtypes.CallError)
                for c in (
                    chtypes.SchemaError,
                    chtypes.UnsupportedError,
                    chtypes.UsageError,
                    chtypes.InternalError,
                )
            )
        ),
    )
    note("`except SchemaError` never catches a decline; `except CallError` catches all")
    note("four when that is what you mean")
    blank()

    describe_error(lambda: lib.compile_table(table("x NotAType")), "compile x NotAType")
    with lib.compile_table(table("x UInt8")) as schema:
        pass
    describe_error(lambda: schema.describe(), "describe() on a closed schema")
    describe_error(lambda: registry.for_version("99.9"), "for_version('99.9')")
    describe_error(lambda: registry.for_version("v26.8"), "for_version('v26.8')")
    blank()

    kv("row verdicts are DATA", "RowResult.outcome, not an exception")
    note("a row the server would reject RETURNS (outcome=rejected, err_code=the server's")
    note("code): nothing raises. Exceptions are for questions that could not be asked")
    blank()

    codes = [
        chtypes.CODE_ARTIFACT_MISSING,
        chtypes.CODE_ARTIFACT_UNTRUSTED,
        chtypes.CODE_ARTIFACT_CORRUPT,
        chtypes.CODE_ARTIFACT_PINNED,
        chtypes.CODE_ARTIFACT_UNPUBLISHED,
        chtypes.CODE_ARTIFACT_INCOMPATIBLE,
        chtypes.CODE_SOURCE_UNREACHABLE,
    ]
    kv("artifact codes (a few)", "  ".join(c.removeprefix("CHTYPES_") for c in codes))
    note("the same codes, with their exit statuses, are what `chtypes fetch` exits with")


# ---------------------------------------------------------------------------
# SECTION 11 - Discovery, offline
#
# WHAT: the library hands you the SQL to run against YOUR server and reads the
# answer back as column declarations; here the answer is CANNED bytes.
# WHY: chtypes NEVER opens a socket and no binding holds SQL: you run the query
# with whatever client you already have. The payoff is rebuilding a table's
# column list from the server as ClickHouse's own formatter writes it.
# LOOK FOR: the query text with its two parameters ({database:String},
# {table:String}), and the declarations (with their DEFAULTs) coming back as
# bytes. A server's version and changed settings are NOT discovered by this
# API in 1.0: the caller supplies them (docs/limitations.md).
# C API: chs_discover_query, chs_discover_columns.
# ---------------------------------------------------------------------------
def section11(lib: chtypes.Library) -> None:
    section(11, "Discovery, offline")
    kv("NOTE", "the response below is CANNED: shaped like a real server's answer,")
    kv("", "so the reader cannot tell. Swap in your HTTP client.")
    blank()

    query = lib.discover_query()
    kv("discover_query()", f"{len(query)} bytes of SQL, FORMAT JSONEachRow")
    for line in show(query).split("\n")[:6]:
        raw("      " + line)
    note("bind {database:String} and {table:String} with your client (over HTTP:")
    note("param_database and param_table); never splice a name into the text")
    blank()

    try:
        found = lib.discover_columns(CANNED_COLUMNS_RESULT)
    except chtypes.CallError as err:
        kv("discover_columns(canned)", f"{type(err).__name__}: {truncate(show(err.message), 60)}")
        note("the canned bytes above did not match what this build reads; the section")
        note("stops here rather than guessing")
        return
    for col in found.columns:
        kv(f"  {show(col.name)}", show(col.declaration))
    kv("  columns_sql", show(found.columns_sql))
    note("`reading c` came back QUOTED in the spelling THIS build prints, DEFAULTs")
    note("carried: dropping them would lose the semantics sections 6 and 8 run on")
    blank()

    ddl = f"CREATE TABLE discovered ({show(found.columns_sql)}) ENGINE = MergeTree ORDER BY tuple()"
    try:
        with lib.compile_table(ddl) as schema:
            batch = schema.rows(Format.JSON_EACH_ROW, b'{"device_id":9,"reading c":21.5}')
            kv("compiled from the discovery", verdict(batch))
    except chtypes.CallError as err:
        kv("compile from the discovery", f"{type(err).__name__}: {truncate(show(err.message), 60)}")


# ---------------------------------------------------------------------------
# SECTION 12 - Version pinning: same input, different answers
#
# WHAT: the same statement and the same bytes, swept across every build
# installed on this machine and opened in this process.
# WHY: version differences are the reason the registry exists. They are not
# monotonic - newer is NOT always more permissive - so no rule can predict
# them; only the real per-version build can answer.
# LOOK FOR: a statement one line refuses and another accepts, and the Buffers
# format being listed (or not) in a build's capabilities.
# C API: chs_schema_create, chs_preview_batch; chs_build_info capabilities.
# ---------------------------------------------------------------------------
def section12(registry: Registry) -> None:
    section(12, "Version pinning: same input, different answers")
    versions = sorted({r.version for r in registry.installed()}, key=version_key)
    if len(versions) < 2:
        kv("builds installed", "  ".join(versions))
        note("only one build is installed, so there is nothing to sweep - the point of")
        note("this section needs at least two. `chtypes fetch 26.7` and re-run")
        return

    libs: list[chtypes.Library] = []
    for v in versions:
        try:
            libs.append(registry.for_version(v))
        except chtypes.ArtifactError as err:
            kv(f"  {v}", f"SKIPPED  {truncate(str(err), 60)}")

    stmt = table("a UInt8, x Int64 DEFAULT if(1,2,'a')")
    kv("(a) a mixed-type DEFAULT", "a UInt8, x Int64 DEFAULT if(1,2,'a')")
    for lib in libs:
        try:
            with lib.compile_table(stmt) as schema:
                col = schema.describe().columns[1]
                kv(
                    f"  {lib.version}",
                    f"compiled  ({show(col.type)} DEFAULT {show(col.default_expr)})",
                )
        except chtypes.CallError as err:
            kv(
                f"  {lib.version}",
                f"{type(err).__name__} code {err.ch_code}  {truncate(show(err.message), 44)}",
            )
    note("NEWER IS NOT ALWAYS MORE PERMISSIVE: no monotonic rule predicts this")
    blank()

    kv("(b) a format's arrival", "Buffers, 1 column, 1 row, 4 bytes ff ff ff ff, declared x Int32")
    for lib in libs:
        listed = Format.BUFFERS.ch_name in lib.build_info.capabilities.input_formats
        with lib.compile_table(table("x Int32")) as schema:
            batch = schema.rows(Format.BUFFERS, bytes.fromhex(BUFFERS_OK))
            kv(f"  {lib.version}", f"listed={listed}  " + verdict(batch))
    note("what a build supports is its capabilities, not your version arithmetic")


# ---------------------------------------------------------------------------
# SECTION 13 - Lifetimes and teardown
#
# WHAT: what to release, and when.
# WHY: schema, filter and block handles are C allocations (close, or `with`).
# A library is NEVER unloaded and never closed: there is no registry or
# library close in any binding, and a loaded image lives until the process
# exits. A filter or block holds a counted reference to its schema INSIDE the
# library, so any order of closing is safe.
# LOOK FOR: live_handles() counting the handles, a filter outliving its closed
# schema, close() being idempotent, and use after close being a UsageError.
# C API: chs_schema_free, chs_filter_free, chs_block_free, chs_live_handles.
# ---------------------------------------------------------------------------
def section13(lib: chtypes.Library) -> None:
    section(13, "Lifetimes and teardown")
    before = lib.live_handles()
    schema = lib.compile_table(table("x UInt8"))
    filt = schema.compile_filter("x > 1")
    during = lib.live_handles()
    kv("live handles", f"before={dict(before)}")
    kv("", f"during={dict(during)}")
    schema.close()
    verdicts = "".join(
        v.value for v in filt.rows(Format.JSON_EACH_ROW, b'{"x":0}\n{"x":5}\n').verdicts
    )
    kv("filter after its schema closed", f"verdicts {verdicts!r}   (it holds its own reference)")
    filt.close()
    filt.close()
    schema.close()
    kv("close() twice", "idempotent")
    kv("after all closed", f"{dict(lib.live_handles())}")
    note("handles are also freed by a finalizer when abandoned; close() is for callers")
    note("that control their own order. live_handles() is a diagnostic for exactly that")
    note("nothing closes a Registry or a Library: an image is never unloaded in-process")


# ---------------------------------------------------------------------------
# SECTION 14 - The static path (Go only)
#
# Go can additionally LINK one artifact directly (no dlopen) under a build tag
# and open it with OpenLinked(). Python cannot have that shape: ctypes always
# dlopens, so Registry is the only loader here, and it is the product path
# anyway. See the Go tour's section 14 for the real thing.
# ---------------------------------------------------------------------------
def section14() -> None:
    section(14, "The static path (Go only)")
    kv("not offered in Python", "ctypes always dlopens; Registry is the only loader")
    note("see the Go tour's section 14 (docs/reference/bindings-v1.md, OpenLinked)")


# ---------------------------------------------------------------------------
# SECTION 15 - Export: bytes + spans
#
# WHAT: the SAME call that judges a batch can also serialize its accepted rows
# to wire bytes, addressed per row by index-aligned spans.
# WHY: every consumer of an accepted row wants the stored bytes ready to
# publish or INSERT without rebuilding them from the document: reassembly is
# where caller bugs live. The export also carries every value the library
# generated, so it is what you INSERT, never the original body (section 6).
# LOOK FOR: the skipped row's empty span; a span slice BEING the row's line; a
# poisoned batch declining the export and saying why; emitted-EMPTY (an
# answer) versus declined (not one); and the lean document (a smaller doc_flags)
# keeping every verdict.
# C API: chs_preview_batch with export_format and doc_flags.
# ---------------------------------------------------------------------------
def section15(lib: chtypes.Library) -> None:
    section(15, "Export: bytes + spans")
    export = Format.JSON_COMPACT_EACH_ROW
    allow = {"input_format_allow_errors_num": "10"}
    with lib.compile_table(FORMAT_DDL) as schema:
        body = b'{"device_id":1,"label":"ok"}\n{"device_id":"zap"}\n{"device_id":3,"label":"hi"}\n'
        batch = schema.rows(Format.JSON_EACH_ROW, body, settings=allow, export=export)
        kv(
            "batch",
            f"{batch.outcome.value}  rows_read={batch.rows_read} rows_skipped={batch.rows_skipped}",
        )
        payload = batch.payload if batch.payload is not None else b""
        kv("payload", f"{payload!r}  ({len(payload)} bytes, one line per ACCEPTED row)")
        note("wire order = declared minus MATERIALIZED/ALIAS/EPHEMERAL, so these bytes are")
        note("INSERT-able with no column list; DEFAULTs are already applied")
        for i, span in enumerate(batch.spans or ()):
            line = "(no bytes - row not accepted)"
            if span.len:
                line = repr(payload[span.off : span.off + span.len])
            kv(
                f"  span[{i}] {{off:{span.off} len:{span.len}}}",
                f"{batch.rows[i].outcome.value} -> {line}",
            )
        note("spans are INDEX-ALIGNED with rows; concatenating the non-zero spans")
        note("reproduces the payload exactly")
        blank()

        lean = schema.rows(
            Format.JSON_EACH_ROW, body, settings=allow, export=export, doc_flags=DocFlags.VALUES
        )
        kv(
            "lean (DocFlags.VALUES)",
            f"outcome {lean.outcome.value}, {len(lean.rows)} verdict rows, "
            f"{len(lean.transformed)} transformed - payload identical: {lean.payload == batch.payload}",
        )
        note("flags thin the DESCRIPTION, never the VERDICT")
        blank()

    # Fail-closed: a poisoned batch holds a value ClickHouse itself cannot read
    # back, so no writer can honestly serialize it.
    with lib.compile_table(table("e Enum8('a' = 1, 'b' = 2)", order="tuple()")) as poison:
        poi = poison.rows(
            Format.JSON_EACH_ROW,
            b'{"e":null}\n',
            settings={"input_format_defaults_for_omitted_fields": "0"},
            export=export,
        )
        desc = "None (declined)" if poi.payload is None else f"{len(poi.payload)} bytes"
        kv(
            "poisoned batch",
            f"{poi.outcome.value} -> payload={desc} export_declined={show(poi.export_declined)!r}",
        )

    with lib.compile_table(FORMAT_DDL) as schema:
        emp = schema.rows(Format.JSON_EACH_ROW, b"", export=export)
        kv("empty batch", f"outcome={emp.outcome.value} payload={emp.payload!r}")
        note("an empty input block is a decline in this API; emitted-empty (a payload of")
        note("zero bytes for zero accepted rows) is an answer")


# ---------------------------------------------------------------------------
# SECTION 16 - Filters: WHERE semantics at the edge
#
# WHAT: compile one boolean expression against a schema (compile_filter) and
# evaluate it per row of a body - ClickHouse's own comparison functions, so the
# answers are WHERE-side by construction.
# WHY: read-side row visibility (who may SEE this row) is a WHERE question, and
# WHERE coercion is NOT insert coercion: `x = 256` over UInt8 PROMOTES (false
# for every row) where an insert would wrap 256 to 0.
# LOOK FOR: 'f','f' where the insert path stores 0; NULL being not-true; the 'e'
# class (compiles, then THROWS per row); Verdict.answered being False for 'e'
# and 'd'; a filter's zone being its own; query parameters bound, never
# escaped; one parsed block serving several filters; and a filter attached to
# rows().
# C API: chs_filter_create, chs_filter_eval_body, chs_filter_eval_block,
# chs_block_create.
# ---------------------------------------------------------------------------
def section16(lib: chtypes.Library) -> None:
    section(16, "Filters: WHERE semantics at the edge")
    with lib.compile_table(table("x UInt8", order="x")) as schema:
        with schema.compile_filter("x = 256") as filt:
            fr = filt.rows(Format.JSON_EACH_ROW, b'{"x":0}\n{"x":255}\n')
            kv("filter `x = 256` over UInt8", f"verdicts {verdict_string(fr)}")
            note("PROMOTES, never wraps: false for x=0 AND x=255. The insert side of this")
            note("same library stores 256 as 0 (section 5's overflow_wrap), which is why")
            note("predicate constants must never be folded through insert coercion")
        blank()

        with lib.compile_table(table("lvl Nullable(UInt8)", order="tuple()")) as ns:
            with ns.compile_filter("lvl = 1") as nf:
                fr = nf.rows(Format.JSON_EACH_ROW, b'{"lvl":null}\n{"lvl":1}\n')
                kv("`lvl = 1` on [null, 1]", f"verdicts {verdict_string(fr)}   (NULL is not true)")
        blank()

        with lib.compile_table(table("s String", order="tuple()")) as ss:
            with ss.compile_filter("s = 257") as sf:
                fr = sf.rows(Format.JSON_EACH_ROW, b'{"s":"hi"}\n')
                kv("`s = 257` over String", f"verdicts {verdict_string(fr)}")
                for fe in fr.errors:
                    kv(f"  row {fe.row}", f"code {fe.code}  {truncate(show(fe.msg), 56)}")
                kv("  answered?", str([v.answered for v in fr.verdicts]))
                note("on a real server this WHERE fails the WHOLE query. 'e' is NOT an")
                note("answer, and neither is 'd' (a row this library declines): an enforcing")
                note("caller fails CLOSED on both, which is what Verdict.answered says")
        blank()

        with schema.compile_filter("x < 5") as lt:
            fr = lt.rows(Format.JSON_EACH_ROW, b'{"x":1}\n{"x":"zap"}\n{"x":9}\n')
            kv(
                "`x < 5` on [1, bad, 9]",
                f"verdicts {verdict_string(fr)}   (the bad row cannot swallow the tail)",
            )
        blank()

        kv("compile `now() > x`", classify(lambda: schema.compile_filter("now() > x")))
        kv("compile `x = {p:UInt8}`", classify(lambda: schema.compile_filter("x = {p:UInt8}")))
        kv("compile `nosuch = 1`", classify(lambda: schema.compile_filter("nosuch = 1")))
        note("an UNBOUND {p:Type} is the server's own refusal: bind it (below)")
    blank()

    group("query parameters - values are STRINGS, never escaped")
    event_body = (
        b'{"tenant":"acme","role":"admin","x":1}\n'
        b'{"tenant":"evil","role":"viewer","x":2}\n'
        b'{"tenant":"\' OR 1=1 --","role":"admin","x":3}\n'
    )
    with lib.compile_table(table("tenant String, role String, x UInt8", order="x")) as ps:
        with ps.compile_filter("tenant = {t:String}", params={"t": "acme"}) as tf:
            kv(
                "`tenant = {t:String}`, t=acme",
                f"verdicts {verdict_string(tf.rows(Format.JSON_EACH_ROW, event_body))}",
            )
        hostile = "' OR 1=1 --"
        with ps.compile_filter("tenant = {t:String}", params={"t": hostile}) as hf:
            fr = hf.rows(Format.JSON_EACH_ROW, event_body)
        inert = fr.verdicts == (Verdict.FALSE, Verdict.FALSE, Verdict.TRUE)
        kv(
            "t = `' OR 1=1 --` (hostile)",
            f"verdicts {verdict_string(fr)}   hostile-value-inert: {inert}",
        )
        note("the value became a typed LITERAL after SQL parsing: it matches only the row")
        note("holding exactly that string, and NOTHING was escaped to get there")
        blank()

        group("one parsed block, K filters")
        with ps.parse_block(Format.JSON_EACH_ROW, event_body) as block:
            for expr in ("role = 'admin'", "role = 'viewer'"):
                with ps.compile_filter(expr) as f:
                    kv(f"eval `{expr}`", f"verdicts {verdict_string(f.eval(block))}")
        note("ONE parse of the 3-row event fed BOTH filters: eval neither consumes nor")
        note("mutates the block, and eval(parse_block(body)) answers as rows(body) does")
        blank()

        group("a filter's zone is its own")
        with ps.compile_filter("role = 'admin'", session_timezone="UTC") as zf:
            fr = zf.rows(Format.JSON_EACH_ROW, event_body, session_timezone="Asia/Tokyo")
            kv("compile zone UTC, body zone Tokyo", f"verdicts {verdict_string(fr)}")
        note("the filter's zone is its WHERE's session, fixed at compile; an evaluation's")
        note("zone only decides how the body is PARSED. Differing zones are an ordinary input")
        blank()

        group("a filter attached to rows()")
        with ps.compile_filter("role = 'admin'") as af:
            batch = ps.rows(Format.JSON_EACH_ROW, event_body, row_filter=af)
            kv(
                "rows(row_filter=...)",
                f"{batch.outcome.value}  rows_passed={batch.rows_passed} rows_cut={batch.rows_cut}",
            )
    blank()

    note("ENFORCEMENT GATE: nothing may enforce read-side security on this API until")
    note("the WHERE-truth gate lands green; until then it is a shadow/replay surface")
    note("(docs/limitations.md)")


# ---------------------------------------------------------------------------
# SECTION 17 - The INSERT column list
#
# WHAT: the `columns` argument of row, rows and parse_block: the INSERT column
# list. `INSERT INTO t FORMAT X` (no list) supplies every PLAIN column from the
# data; `INSERT INTO t (id, e) FORMAT X` supplies exactly the listed columns
# and the server computes the rest, with the listed values in scope for their
# DEFAULTs. The one situation where the two shapes differ is an EPHEMERAL
# column: it has no stored slot and is reachable ONLY through an explicit list.
# WHY: an explicit-list INSERT is the shape a production pipeline sends.
# LOOK FOR: `d = 6`, not the type-zero `d = 1` a no-list insert would give: the
# listed EPHEMERAL `e` fed `d`'s `DEFAULT e + 1` and was itself discarded; and
# the unknown-column refusal, raised before any byte of the body is parsed.
# C API: chs_preview_row / chs_preview_batch with `columns`.
# ---------------------------------------------------------------------------
def section17(lib: chtypes.Library) -> None:
    section(17, "The INSERT column list")
    ddl = table("id UInt32, e UInt8 EPHEMERAL, d UInt8 DEFAULT e + 1", order="id")
    kv("schema", "id UInt32, e UInt8 EPHEMERAL, d UInt8 DEFAULT e + 1")
    blank()

    with lib.compile_table(ddl) as schema:
        kv("(a) list = (id, e)", '{"id":3,"e":5}')
        feed(
            schema,
            "JSONEachRow (id, e)",
            Format.JSON_EACH_ROW,
            b'{"id":3,"e":5}',
            columns=["id", "e"],
        )
        note("d = 6, not the type-zero d = 1 a no-list insert of this row would give (a")
        note("no-list INSERT cannot reach e at all). e itself never appears: it is read")
        note("and discarded, exactly as the ABI documents")
        blank()

        kv("(b) list names an unknown column", '{"id":1,"nosuch":2}')
        feed(
            schema,
            "JSONEachRow (id, nosuch)",
            Format.JSON_EACH_ROW,
            b'{"id":1,"nosuch":2}',
            columns=["id", "nosuch"],
        )
        note("the server's own NO_SUCH_COLUMN_IN_TABLE; a duplicate name is a different")
        note("refusal. The binding does no local name validation")
    blank()

    note("`columns=None` is the no-list shape; a column name is BYTES-capable: pass bytes")
    note("for a name that is not UTF-8")


# ------------------------------------------------------------------- plumbing
#
# Everything below is printing helpers: no chtypes calls hide here, except the
# ones the sections name.


def table(columns: str, order: str = "tuple()") -> str:
    """One CREATE TABLE statement over `columns`, a plain MergeTree."""
    return f"CREATE TABLE t ({columns}) ENGINE = MergeTree ORDER BY {order}"


def version_key(version: str) -> tuple[int, ...]:
    """Order installed versions numerically for DISPLAY and for picking the newest:
    "25.10" > "25.3", which a string sort gets backwards. The library orders
    nothing for you; this is the tour's own convenience."""
    return tuple(int(p) if p.isdigit() else -1 for p in version.split("."))


def supports(lib: chtypes.Library, fmt: Format) -> bool:
    """Whether the build lists `fmt` among its input formats (its capabilities)."""
    return fmt.ch_name in lib.build_info.capabilities.input_formats


def show(value: bytes | str | None) -> str:
    """Bytes for PRINTING only: invalid UTF-8 is replaced here, never in the library."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return value.decode("utf-8", "replace")


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


def fatal(message: str) -> NoReturn:
    print(f"\nplayground: {message}", file=sys.stderr)
    raise SystemExit(1)


def feed(
    schema: chtypes.Schema,
    label: str,
    fmt: Format,
    body: bytes,
    *,
    columns: list[str] | None = None,
) -> None:
    """Run one payload through rows() and print the verdict on one line, plus a "~"
    line per Transform (a silent change ClickHouse made)."""
    batch = schema.rows(fmt, body, columns=columns)
    parts = [batch.outcome.value]
    if batch.err_code:
        parts.append(f"code={batch.err_code}")
    if batch.rows:
        vals = " ".join(f"{show(v.column)}={text_or(v)}({v.source})" for v in batch.rows[0].values)
        if vals:
            parts.append(vals)
    if batch.err_msg:
        parts.append(truncate(show(batch.err_msg), 56))
    kv(f"  {label}", "  ".join(parts))
    for t in batch.transformed:
        lossy = ", LOSSY" if t.lossy else ""
        raw(f"      ~ {show(t.column)}: {show(t.input)} -> {show(t.stored)} ({t.reason}{lossy})")


def render_engine_rows(batch: chtypes.BatchResult) -> str:
    """The post-merge rows: each stored row is a tuple of cells."""
    if batch.engine_rows is None:
        return "(this build gave none)"
    rows = [
        "(" + ", ".join(f"{show(c.column)}={show(c.text)}" for c in row) + ")"
        for row in batch.engine_rows
    ]
    return f"[{', '.join(rows)}]  (length {len(rows)})"


def describe_error(call: Callable[[], Any], what: str) -> None:
    """Print which typed error a call raises. The four call errors are PEERS."""
    try:
        result = call()
        if hasattr(result, "close"):
            result.close()
        kv(what, "(no error)")
    except chtypes.CallError as err:
        kind = type(err).__name__
        kv(what, f"{kind}  status={err.status} ch_code={err.ch_code} ch_name={err.ch_name!r}")
        kv("  message", truncate(show(err.message), 84))
    except chtypes.ArtifactError as err:
        kv(what, f"{type(err).__name__}  code={err.code}")
        kv("  message", truncate(str(err), 84))
    except chtypes.ChtypesError as err:
        kv(what, f"{type(err).__name__}")
        kv("  message", truncate(str(err), 84))


def classify(call: Callable[[], Any]) -> str:
    """Name a call's error KIND in one word."""
    try:
        result = call()
        if hasattr(result, "close"):
            result.close()
        return "accepted"
    except chtypes.UnsupportedError:
        return "DECLINED  (UnsupportedError)"
    except chtypes.SchemaError as err:
        return f"REFUSED   (SchemaError, code {err.ch_code})"
    except chtypes.CallError as err:
        return f"{type(err).__name__}"


def caught(call: Callable[[], Any]) -> str:
    try:
        result = call()
        if hasattr(result, "close"):
            result.close()
        return "(no error)"
    except chtypes.CallError as err:
        return show(err.message)


def verdict(batch: chtypes.BatchResult) -> str:
    if batch.err_code:
        return (
            f"{batch.outcome.value:<9} code={batch.err_code}  {truncate(show(batch.err_msg), 44)}"
        )
    if batch.rows and batch.rows[0].values:
        v = batch.rows[0].values[0]
        return f"{batch.outcome.value:<9} {show(v.column)} = {show(v.text)}"
    return batch.outcome.value


def verdict_string(fr: chtypes.FilterResult) -> str:
    """Render a FilterResult's verdicts as the document's compact t/f/e/d string."""
    if fr.outcome is not chtypes.FilterOutcome.OK:
        return f"(call-level: outcome={fr.outcome.value} code={fr.err_code} {truncate(show(fr.err_msg), 40)})"
    return '"' + "".join(v.value for v in fr.verdicts) + '"'


def text_or(value: chtypes.Value) -> str:
    if value.text == b"" and not value.null:
        return "<unreadable>"
    return show(value.text)


def truncate(text: str, limit: int) -> str:
    text = text.replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "..."


if __name__ == "__main__":
    main()
