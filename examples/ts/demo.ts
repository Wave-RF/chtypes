// The TypeScript/Node tour of chtypes, on the v1 public API.
//
// WHAT THIS IS
//
// chtypes answers one question: "if this row were inserted into this table on
// this ClickHouse version, what would happen?", without a server. The answers
// come from ClickHouse's own C++, vendored per release into a shared library
// behind the C ABI, which is why they are exact rather than approximately right.
//
// This file is a tutorial you RUN. Seventeen numbered sections walk the whole
// public API of the TypeScript SDK, from opening a library to the end of the
// process, each with a comment saying what it demonstrates, why an ingest
// pipeline cares and what to look at in the output. The same seventeen sections,
// with the same numbering, schemas and rows, exist in the Go, Python and Rust
// tours, so two of them can be diffed and only the language idioms differ.
// The API is docs/reference/bindings-v1.md; this tour does not restate it.
//
// EVERYTHING HERE IS OFFLINE once an artifact is installed: no Docker, no
// ClickHouse server. Even the discovery section (11) runs against CANNED bytes
// shaped like a real server's answer.
//
//   chtypes fetch 26.8 && pnpm demo       # an installed line (see `chtypes list`)
//   CHTYPES_VERSION=26.8 pnpm demo        # pick a line
//   ../chplay.sh ts                       # the same, with prerequisite checks
//
// Nothing here is a test: the real suites live in ts/. Every number printed
// below is produced by the run, never written down by hand.

import {
  ArtifactError,
  ArtifactMissingError,
  type BatchResult,
  CallError,
  DefaultKind,
  DocFlags,
  type FilterResult,
  Format,
  formatChName,
  InternalError,
  isChtypesError,
  type Library,
  Registry,
  type RowResult,
  type Schema,
  SchemaError,
  setup,
  UnsupportedError,
  UsageError,
  type Value,
  verdictAnswered,
} from '@wavehouse/chtypes';

// ------------------------------------------------------------- the fixture
//
// These constants are IDENTICAL in all four tours. Change one here and change
// it in the other three: the point of this directory is that the four outputs
// can be diffed.

// The tenant's table for the DEFAULT/result sections: one of everything the
// tour needs, a volatile DEFAULT (now64), a literal DEFAULT, an Enum (how a
// table gets poisoned) and a MATERIALIZED column (never in SELECT *).
const DEMO_DDL = `CREATE TABLE t (
ts DateTime64(3) DEFAULT now64(3),
device_id UInt32,
seq UInt8 DEFAULT 0,
payload String,
grade Enum8('a' = 1, 'b' = 2),
payload_len UInt32 MATERIALIZED length(payload)
) ENGINE = MergeTree ORDER BY tuple()`;

// A small three-column table for the outcome and format sections.
const FORMAT_DDL = "CREATE TABLE t (device_id UInt32, seq UInt8 DEFAULT 7, label String DEFAULT 'unknown') ENGINE = MergeTree ORDER BY tuple()";

// Section 17's table: an EPHEMERAL input column feeding a DEFAULT.
const COLUMNS_DDL = 'CREATE TABLE t (id UInt32, e UInt8 EPHEMERAL, d UInt8 DEFAULT e + 1) ENGINE = MergeTree ORDER BY tuple()';

// Pinning the clock is what makes a demo with now64(3) in it reproducible. The
// value is a STRING at the boundary, always: settings values are strings only.
const PINNED_CLOCK = '1700000000000000000'; // 2023-11-14 22:13:20 UTC

// Hand-built binary payloads (hex), shared by all four tours.
const ROW_BINARY_OK = '0100000007026f6b'; // UInt32 LE 1, UInt8 7, varint-len "ok"
const RBWD_MARKER = '00020000000100026869'; // device_id=2 by value, seq by marker, label="hi"
const RBWD_POISON = '000100000001'; // id=1 by value, e by marker -> raw 0, NO name
const RBWD_VALUE = '00010000000002'; // id=1 by value, e by value 2 ("green")
const RBWNTD_ROW =
  '03' +
  '096465766963655f6964' +
  '03736571' +
  '056c6162656c' +
  '0655496e743332' +
  '0555496e7438' +
  '06537472696e67' +
  '00' +
  '01000000' +
  '01' +
  '00' +
  '026f6b';
// Native blocks captured from a live server (SELECT ... FORMAT Native).
const NATIVE_OK = '0301096465766963655f69640655496e74333201000000037365710555496e743807056c6162656c06537472696e67026f6b';
const NATIVE_CAST =
  '0301096465766963655f69640655496e743332020000000373657106537472696e6703323030056c6162656c06537472696e670463617374';
// Buffers: uint64le n_columns, n_rows, then per column a byte size and the raw column.
const BUFFERS_OK = '010000000000000001000000000000000400000000000000ffffffff';
const BUFFERS_WIDE = '010000000000000001000000000000000800000000000000ffffffff';

// Section 11's CANNED server answer to the discovery query: shaped like a real
// ClickHouse's JSONEachRow rows of system.columns, quoted UInt64s and all.
const CANNED_COLUMNS_RESULT =
  '{"name":"ts","type":"DateTime64(3)","default_kind":"DEFAULT","default_expression":"now64(3)"}\n' +
  '{"name":"device_id","type":"UInt32","default_kind":"","default_expression":""}\n' +
  '{"name":"reading c","type":"Float64","default_kind":"","default_expression":""}\n' +
  `{"name":"note","type":"String","default_kind":"DEFAULT","default_expression":"'unset'"}\n`;

async function main(): Promise<void> {
  // The process setup is chosen once, before the first open (section 1).
  setup({ timezone: 'UTC' });
  const { registry, lib, lines } = await section1();
  section2(lib);
  section3(lib);
  section4(lib);
  section5(lib);
  section6(lib);
  await section7(lib, registry, lines);
  section8(lib);
  section9(lib);
  section10(lib);
  section11(lib);
  await section12(registry, lines);
  section13(registry);
  section14();
  section15(lib);
  section16(lib);
  section17(lib);

  blank();
  console.log('Done. Every value above was measured by this run.');
}

// ---------------------------------------------------------------------------
// SECTION 1: Open a library and read what it says about itself
//
// WHAT: construct a registry, see what is installed, ask for one version.
// WHY: an ingest gateway serves tenants on different ClickHouse versions at
// once; the registry is how one process answers for all of them, exactly.
// LOOK FOR: the library naming ITSELF (build_info), the fingerprint the loader
// checked, and the refusal for a version nobody has installed: never a silent
// nearest-version fallback.
// C API: chs_build_info, chs_initialize (at load); the fetch layer resolves.
// ---------------------------------------------------------------------------
async function section1(): Promise<{ registry: Registry; lib: Library; lines: string[] }> {
  section(1, 'Open a library and read what it says about itself');

  const registry = await Registry.open();
  const installed = await registry.installed();
  const lines = [...new Set(installed.map((r) => r.predicate.clickhouse_minor))].sort(compareLines);
  kv('lines installed', lines.length === 0 ? '(none)' : lines.join('  '));
  note('each line is one dlopen of one signed artifact; all live in THIS process at once');
  if (lines.length === 0) fatal('nothing is installed. Fetch a line first: `chtypes fetch 26.8`.');
  if (lines.length === 1) note("only one line is installed; section 12's sweeps will degrade and say so");

  const want = process.env['CHTYPES_VERSION'] ?? newestLine(lines);
  kv('version selected', `${want}  (${process.env['CHTYPES_VERSION'] ? 'from $CHTYPES_VERSION' : 'default: the newest installed'})`);
  let lib: Library;
  try {
    lib = await registry.for(want);
  } catch (err) {
    return fatal(errText(err));
  }
  const info = lib.buildInfo;
  kv('library reports', `${lib.version}  (minor ${lib.minor}, channel ${info.channel || '(none)'})`);
  kv('loaded from', lib.path);
  kv('abi / fingerprint', `${info.abi} / ${info.abiFingerprint}`);
  kv('built for', `${info.os}-${info.arch}, build ${info.build}`);
  kv('signed by', lib.resolved?.signedBy ? `key ${lib.resolved.signedBy}` : '(not a fetched library)');
  note('the library NAMES ITSELF: nothing is inferred from a directory or file name,');
  note('and the loader refused it unless its fingerprint matched this binding');
  blank();

  try {
    await registry.for('99.9');
    kv('asking for 99.9', '(no error!?)');
  } catch (err) {
    kv('asking for 99.9', `${errName(err)}: ${truncate(errText(err), 80)}`);
  }
  note('with autofetch off, an uninstalled version is an ArtifactMissingError: no');
  note('nearest-neighbor fallback, ever. A wrong-version answer is a wrong answer');
  note('with a green checkmark on it');
  return { registry, lib, lines };
}

// ---------------------------------------------------------------------------
// SECTION 2: Ask a build about itself
//
// WHAT: type validation and canonicalization, straight from this build's own
// DataTypeFactory, plus its three quoting functions and its error-code table.
// WHY: canonicalization is how you compare a tenant's declared type against
// what the server will actually store, and it is NOT a spelling normalizer: it
// is the server's own parse.
// LOOK FOR: Variant members SORTED, BIGINT becoming Int64, an unknown family
// failing with ClickHouse's own code 50, and the code table read from the build.
// C API: chs_type_validate, chs_back_quote, chs_back_quote_if_needed,
// chs_quote_string, chs_error_codes.
// ---------------------------------------------------------------------------
function section2(lib: Library): void {
  section(2, 'Ask a build about itself');

  kv('validateType', "input -> this build's canonical spelling");
  for (const t of ['Decimal(18,4)', 'Variant(UInt8, String)', 'BIGINT', 'LowCardinality( String )']) {
    kv(`  ${t}`, `-> ${lib.validateType(t).toString()}`);
  }
  note('Variant members are SORTED; surplus parameters are dropped; compare');
  note("canonical strings verbatim, never re-normalize whitespace");
  blank();

  try {
    lib.validateType('NotAType');
  } catch (err) {
    if (!(err instanceof SchemaError)) throw err;
    kv('validateType(NotAType)', `SchemaError code=${err.chCode} ${err.chName}  ${truncate(err.messageBytes.toString(), 60)}`);
    note("code 50 is the server's own UNKNOWN_TYPE, from the server's own registry");
  }
  blank();

  kv('quoteIdentifier', `${lib.quoteIdentifier('reading c')}   (always quoted)`);
  kv('quoteIdentifierIfNeeded', `${lib.quoteIdentifierIfNeeded('plain')}  /  ${lib.quoteIdentifierIfNeeded('reading c')}`);
  kv('quoteLiteral', `${lib.quoteLiteral("it's")}`);
  note("the spelling ClickHouse itself prints; no quoting rule lives in this SDK");
  blank();

  const codes = lib.errorCodes();
  kv('errorCodes()', `${codes.all().length} codes in this build`);
  kv('  name(27)', `${codes.name(27)}   (code(${JSON.stringify(codes.name(27))}) = ${codes.code(codes.name(27) ?? '')})`);
  kv('  name(999999)', `${codes.name(999999)}   (absent, never synthesized)`);
}

// ---------------------------------------------------------------------------
// SECTION 3: Compile a table and read it back
//
// WHAT: compile one CREATE TABLE statement and describe the compiled columns.
// WHY: the compiled handle IS the table, as this ClickHouse version would
// create it. Two rewrites below are things no type-string comparison could
// ever catch: the compile is schema-aware.
// LOOK FOR: DEFAULT NULL turning Int64 into Nullable(Int64), and an ALIAS column
// whose type is INFERRED.
// C API: chs_schema_create, chs_schema_describe, chs_schema_free (close).
// ---------------------------------------------------------------------------
function section3(lib: Library): void {
  section(3, 'Compile a table and read it back');

  kv('the DDL', '');
  for (const line of DEMO_DDL.split('\n')) raw(`      ${line}`);
  withSchema(lib, DEMO_DDL, (schema) => {
    blank();
    kv('described columns', 'name  type  (default kind + expression)');
    for (const c of schema.describe().columns) {
      const extra = c.defaultKind === DefaultKind.None ? '' : `  ${c.defaultKind} ${c.defaultExpr.toString()}`;
      kv(`  ${c.name.toString()}`, `${c.type.toString()}${extra}`);
    }
  });
  note('payload_len is MATERIALIZED: compiled and described, but never read from');
  note('input; watch it come back separately in section 6');
  blank();

  withSchema(lib, 'CREATE TABLE t (x Int64 DEFAULT NULL) ENGINE = MergeTree ORDER BY tuple()', (schema) => {
    const c = schema.describe().columns[0];
    kv('x Int64 DEFAULT NULL', `compiles as ${c?.type.toString()} DEFAULT ${c?.defaultExpr.toString()}`);
  });
  note('the DEFAULT rewrote the type to Nullable: the server does this at CREATE,');
  note('so chtypes must too, or every later verdict drifts');

  withSchema(lib, 'CREATE TABLE t (a UInt8, al ALIAS a + 1) ENGINE = MergeTree ORDER BY tuple()', (schema) => {
    kv('a UInt8, al ALIAS a + 1', `al compiles as ${schema.describe().columns[1]?.type.toString()}`);
  });
  note("UInt8 + 1 widens to UInt16: ClickHouse's own inference");
  blank();

  kv('x NotAType', classify(() => lib.compileTable('CREATE TABLE t (x NotAType) ENGINE = MergeTree ORDER BY tuple()')));
  note(truncate(caught(() => lib.compileTable('CREATE TABLE t (x NotAType) ENGINE = MergeTree ORDER BY tuple()')), 90));
}

// ---------------------------------------------------------------------------
// SECTION 4: Compile under a settings profile
//
// WHAT: the same compile with a DECLARED settings profile, the settings a real
// server would have had at CREATE TABLE.
// WHY: some settings change the SHAPE of a table (flatten_nested), some gate
// which TYPES may exist (allow_suspicious_low_cardinality_types). A gateway
// discovers a deployment's settings once and declares them here; the handle
// then behaves like a table created on THAT server.
// LOOK FOR: one DDL compiling to two column lists; a type gate failing with the
// server's own 455; a typo'd setting name failing with the server's own 115
// INCLUDING its did-you-mean hint.
// C API: chs_schema_create (the settings argument).
// ---------------------------------------------------------------------------
function section4(lib: Library): void {
  section(4, 'Compile under a settings profile');

  kv('(a) flatten_nested', 'id UInt32, n Nested(a UInt8, b String)');
  for (const value of ['1', '0']) {
    const schema = lib.compileTable('CREATE TABLE t (id UInt32, n Nested(a UInt8, b String)) ENGINE = MergeTree ORDER BY tuple()', {
      settings: { flatten_nested: value },
    });
    kv(`  =${value}`, schema.describe().columns.map((c) => `${c.name.toString()} ${c.type.toString()}`).join(' | '));
    schema.close();
  }
  note('under 1 (the stock default) the Nested column is stored FLATTENED as two');
  note('Arrays; under 0 it is one Array(Tuple) column. Every downstream answer');
  note('follows the compiled shape.');
  blank();

  kv('(b) a type gate', 'lc LowCardinality(UInt8), allow_suspicious_low_cardinality_types');
  for (const value of ['0', '1']) {
    try {
      const schema = lib.compileTable('CREATE TABLE t (lc LowCardinality(UInt8)) ENGINE = MergeTree ORDER BY tuple()', {
        settings: { allow_suspicious_low_cardinality_types: value },
      });
      kv(`  =${value}`, `compiled: ${schema.describe().columns[0]?.type.toString()}`);
      schema.close();
    } catch (err) {
      if (!(err instanceof SchemaError)) throw err;
      kv(`  =${value}`, `REFUSED, the server's own code ${err.chCode}`);
      note(truncate(err.messageBytes.toString(), 92));
    }
  }
  blank();

  try {
    lib.compileTable('CREATE TABLE t (a UInt8) ENGINE = MergeTree ORDER BY tuple()', { settings: { flatten_nestedd: '1' } });
  } catch (err) {
    if (!(err instanceof SchemaError)) throw err;
    kv('(c) unknown setting name', `flatten_nestedd -> code ${err.chCode} ${err.chName}`);
    note(truncate(err.messageBytes.toString(), 96));
  }
  note("the did-you-mean hint is the SERVER's: chtypes passes it through, invents nothing");
  blank();

  kv('(d) settings are strings', 'a number is a TypeError, never silently rewritten');
  kv('  flatten_nested: 0', caught(() => lib.compileTable('CREATE TABLE t (a UInt8) ENGINE = MergeTree ORDER BY tuple()', { settings: { flatten_nested: 0 as unknown as string } })));
}

// ---------------------------------------------------------------------------
// SECTION 5: Accept, reject, decline (and poison)
//
// WHAT: the verdicts every row lands on, plus the one that looks like an accept
// and bites at read time.
// WHY: this is the contract of the whole product. An ingest gateway routes on
// exactly this: accepted -> insert and publish the STORED values; rejected ->
// 400 the producer with the server's own message; unsupported -> chtypes
// refuses to guess, so fall back to the real server and NEVER convert the
// decline into an accept or a reject yourself.
// LOOK FOR: the accept carrying a visible coercion (input 256, stored 0); the
// reject carrying ClickHouse's own error text; the decline carrying a decline
// outcome and no refusal.
// C API: chs_preview_row, chs_preview_batch.
// ---------------------------------------------------------------------------
function section5(lib: Library): void {
  section(5, 'Accept, reject, decline (and poison)');
  kv('schema', FORMAT_DDL);
  blank();

  withSchema(lib, FORMAT_DDL, (schema) => {
    kv('(a) ACCEPT', '{"device_id":1,"seq":256,"label":"ok"}');
    feed(schema, 'JSONEachRow', Format.JSONEachRow, utf8('{"device_id":1,"seq":256,"label":"ok"}'));
    note('accepted, but look at seq: input 256, stored 0. The ~ line is the Transform');
    note('(reason overflow_wrap, LOSSY). Publish the STORED value.');
    blank();

    kv('(b) REJECT', '{"device_id":"abc"}');
    feed(schema, 'JSONEachRow', Format.JSONEachRow, utf8('{"device_id":"abc"}'));
    note("the code and message are ClickHouse's OWN: chtypes never hand-writes an error");
    blank();

    kv('(c) DECLINE', "(2,7,concat('a','b'))  as Values");
    feed(schema, 'Values', Format.Values, utf8("(2,7,concat('a','b'))"));
    note("unsupported means 'a real server MIGHT WELL accept this; I will not guess'.");
    note('Do not 400 the producer, do not publish: send it to the real server.');
    blank();
  });

  kv('(d) POISON', 'an accept that bites at read time');
  withSchema(lib, "CREATE TABLE t (id UInt32, e Enum8('red' = 1, 'green' = 2)) ENGINE = MergeTree ORDER BY tuple()", (schema) => {
    kv('  payload (RBWD)', `${RBWD_POISON}   (id=1 by value, e by marker byte)`);
    feed(schema, 'RBWD marker', Format.RowBinaryWithDefaults, unhex(RBWD_POISON));
    note('accepted_poisoned: the marker fills with the COLUMN-level raw zero, and raw 0');
    note('has no Enum name. The insert returns success; every later SELECT fails.');
    kv('  control payload', `${RBWD_VALUE}   (e supplied by value = 2)`);
    feed(schema, 'RBWD value', Format.RowBinaryWithDefaults, unhex(RBWD_VALUE));
  });
  blank();

  kv('(e) SKIP', 'a bad middle row under input_format_allow_errors_num=10');
  withSchema(lib, FORMAT_DDL, (schema) => {
    const batch = schema.rows(
      Format.JSONEachRow,
      utf8('{"device_id":1,"seq":1,"label":"a"}\n{"device_id":"oops"}\n{"device_id":3,"seq":3,"label":"c"}\n'),
      { settings: { input_format_allow_errors_num: '10' } },
    );
    kv('  batch', `${batch.outcome}  rows_read=${batch.rowsRead} rows_skipped=${batch.rowsSkipped}`);
    batch.rows.forEach((r, i) => {
      let line: string = r.outcome;
      if (r.outcome === 'skipped') line += `  code=${r.errCode} ${truncate(r.errMsg.toString(), 48)}`;
      kv(`  row ${i}`, line);
    });
    note('one rows() call answers per input record, IN ORDER; a skipped row is never');
    note('stored. Route on the row outcome, and never send allow_errors to the real INSERT.');
  });
}

// ---------------------------------------------------------------------------
// SECTION 6: DEFAULT evaluation, where every value comes from
//
// WHAT: one row through the demo table with the clock pinned, then reading back
// WHERE each stored value came from (Value.source), which the library
// substituted itself, and which it computed.
// WHY: an INSERT is mostly values the row did NOT supply. The volatile-DEFAULT
// rule is the sharp edge: the library resolved now64() from ITS clock, so the
// caller MUST insert the library's output, otherwise the server stamps its own
// clock and preview != stored, always (docs/reference/bindings-v1.md §5).
// LOOK FOR: different sources in one row; MATERIALIZED payload_len under
// `computed`; "bogus" under unknownFields; a skew-budget DECLINE at the end.
// C API: chs_preview_row.
// ---------------------------------------------------------------------------
function section6(lib: Library): void {
  section(6, 'DEFAULT evaluation: where every value comes from');

  withSchema(lib, DEMO_DDL, (schema) => {
    const rowText = '{"device_id":42,"seq":256,"payload":"hello","grade":"a","bogus":1}';
    kv('row fed', rowText);
    kv('clock pinned', `chtypes_now_epoch_nanos=${PINNED_CLOCK}  (2023-11-14 22:13:20 UTC)`);
    note('ts is OMITTED on purpose; bogus matches no column');
    const r = schema.row(Format.JSONEachRow, utf8(rowText), { settings: { chtypes_now_epoch_nanos: PINNED_CLOCK } });
    blank();

    kv('outcome / errCode', `${r.outcome} / ${r.errCode}`);
    kv('columns[]  (every entry)', 'column = stored text  (source, stored?)');
    for (const v of r.columns) kv(`  ${v.column.toString()}`, `${text(v).padEnd(26)} (${v.source}, stored=${v.isStored})`);
    note('source: input, default, default_substituted (a volatile DEFAULT resolved from');
    note("the pinned clock), absent, ... Value.isStored is the description's own fact for");
    note("the source; values[] is just columns[] filtered by it (no list lives here).");
    note("text is ClickHouse's OWN rendering, never re-serialized here.");
    blank();

    kv('values[]', `${r.values.length} stored of ${r.columns.length} entries`);
    kv('substituted', 'columns whose source is default_substituted');
    for (const v of r.columns.filter((c) => c.source === 'default_substituted')) {
      kv(`  ${v.column.toString()}`, `${text(v)}   (insert the library's output so the server stores THIS value)`);
    }
    blank();

    kv('computed[]', 'MATERIALIZED values: durable, but never in SELECT *');
    for (const c of r.computed) kv(`  ${c.column.toString()}`, `${c.kind}  =  ${c.text.toString()}`);
    blank();

    kv('transformed[]', 'every silent change, with a machine-readable reason');
    for (const t of r.transformed) {
      kv(`  ${t.reason}`, `${t.column.toString()}: ${t.input.toString() || '(absent)'} -> ${t.stored.toString()}   lossy=${t.lossy}`);
    }
    note('Transform.lossy is the description\'s fact for the reason, read from the document');
    blank();

    kv('unknownFields[]', JSON.stringify(r.unknownFields.map((b) => b.toString())));
    kv('unsupportedSettings[]', JSON.stringify(r.unsupportedSettings.map((b) => b.toString())));
    note('unknown fields are reported, not judged: whether to 400 on them is gateway policy');
    blank();

    withSchema(lib, 'CREATE TABLE t (a UInt8, d UInt8 DEFAULT a + 1) ENGINE = MergeTree ORDER BY tuple()', (dep) => {
      kv('row-dependent DEFAULT', 'a UInt8, d UInt8 DEFAULT a + 1   fed CSV `7,`');
      feed(dep, 'CSV', Format.CSV, utf8('7,'));
      note('the bare empty CSV field takes the DEFAULT, which reads a=7 from the same row');
    });
    blank();

    const r2 = schema.row(Format.JSONEachRow, utf8('{"device_id":1,"seq":1,"payload":"x","grade":"b"}'), {
      settings: { chtypes_clock_offset_nanos: '5000000000', chtypes_max_clock_skew_nanos: '1' },
    });
    kv('skew budget decline', 'offset=5s, budget=1ns');
    kv('  outcome / errCode', `${r2.outcome} / ${r2.errCode}`);
    for (const v of r2.columns) if (v.column.toString() === 'ts') kv('  ts.source', v.source);
    note('a volatile DEFAULT past the caller\'s clock-skew budget is not substituted:');
    note('a timestamp too far in the past under a TTL is silently deleted at merge time');
  });
}

// ---------------------------------------------------------------------------
// SECTION 7: One schema, every format
//
// WHAT: the same three-column schema fed in every format: accept and reject for
// each text format, the two header formats, then the binary tier with
// hand-built bytes.
// WHY: format is not cosmetic. Each format has signature behaviors (CSV's
// bare-vs-quoted empty field, RBWD's marker byte, Native's silent CAST,
// Buffers' silent reinterpret) that change what the table ends up holding.
// LOOK FOR: the same logical row giving format-specific verdicts, and the
// byte-level payloads in the comments: every binary payload is explained.
// C API: chs_preview_row with the frozen format codes; BuildInfo.capabilities
// says which a build reads.
// ---------------------------------------------------------------------------
async function section7(lib: Library, registry: Registry, lines: readonly string[]): Promise<void> {
  section(7, 'One schema, every format');
  kv('schema', FORMAT_DDL);
  kv('formatChName(5)', `${formatChName(Format.RowBinary)}   (codes are the frozen ABI integers)`);
  kv('this build reads', lib.buildInfo.capabilities.inputFormats.join(', '));
  blank();

  withSchema(lib, FORMAT_DDL, (schema) => {
    group('text formats (one row per line; positional or named)');
    feed(schema, 'JSONEachRow  accept', Format.JSONEachRow, utf8('{"device_id":1,"seq":7,"label":"ok"}'));
    feed(schema, 'JSONEachRow  reject', Format.JSONEachRow, utf8('{"device_id":"abc"}'));
    feed(schema, 'CSV          accept', Format.CSV, utf8('1,7,ok'));
    feed(schema, 'CSV          reject', Format.CSV, utf8('x,7,ok'));
    feed(schema, 'TSV          accept', Format.TSV, utf8('1\t7\tok'));
    feed(schema, 'TSV          reject', Format.TSV, utf8('y\t7\tok'));
    feed(schema, 'JSONCompact  accept', Format.JSONCompactEachRow, utf8('[1,7,"ok"]'));
    feed(schema, 'JSONCompact  reject', Format.JSONCompactEachRow, utf8('["z",7,"ok"]'));
    feed(schema, 'Values       accept', Format.Values, utf8("(1,7,'ok')"));
    feed(schema, 'Values       DEFAULT', Format.Values, utf8("(3,DEFAULT,'d')"));
    feed(schema, 'Values       decline', Format.Values, utf8("(2,7,concat('a','b'))"));
    note('Values has an explicit DEFAULT keyword; an SQL expression is a DECLINE (5c)');
    blank();

    kv('CSV empty-field rule', "bare empty takes the DEFAULT; quoted empty is ''");
    feed(schema, 'CSV   bare   2,7,', Format.CSV, utf8('2,7,'));
    feed(schema, 'CSV   quoted 3,7,""', Format.CSV, utf8('3,7,""'));
    blank();

    group('header formats (the first row NAMES the columns)');
    feed(schema, 'CSVWithNames accept', Format.CSVWithNames, utf8('device_id,seq,label\n1,7,ok'));
    feed(schema, 'CSVWithNames reorder', Format.CSVWithNames, utf8('label,device_id,seq\nok,1,7'));
    feed(schema, 'TSVWithNames accept', Format.TSVWithNames, utf8('device_id\tseq\tlabel\n1\t7\tok'));
    note('the reordered header stores the SAME row: data is addressed by NAME');
    feed(schema, 'CSVWithNames CASE', Format.CSVWithNames, utf8('DEVICE_ID,seq,label\n1,7,ok'));
    note('header-name matching is EXACT through 26.4 and case-insensitive from 26.5, so');
    note("DEVICE_ID binds on one line and is an unknown field on the other: the library's answer");
    blank();

    group('binary formats (bytes, COUNTED, never NUL-terminated)');
    kv('  RowBinary payload', ROW_BINARY_OK);
    feed(schema, 'RowBinary    accept', Format.RowBinary, unhex(ROW_BINARY_OK));
    note('4-byte LE UInt32 (1), 1-byte UInt8 (7), varint-length String ("ok"): no framing');
    feed(schema, 'RowBinary    reject', Format.RowBinary, new Uint8Array([0x01, 0x00]));
    note('truncated mid-row: framing faults are all-or-nothing per batch');
    kv('  RBWD payload', RBWD_MARKER);
    feed(schema, 'RBWD  marker byte', Format.RowBinaryWithDefaults, unhex(RBWD_MARKER));
    note("RowBinaryWithDefaults prefixes each column with a marker byte: 00 = 'value");
    note("follows', nonzero = 'compute the DEFAULT'. Above, seq's marker is 01 -> stored 7.");
    kv('  RBWNTD payload', '(hand-built: LEB128 count, names, types, then a marker row)');
    feed(schema, 'RBWNTD       accept', Format.RowBinaryWithNamesAndTypesAndDefaults, unhex(RBWNTD_ROW));
    blank();

    group('Native: self-describing, and it CASTs');
    feed(schema, 'Native       accept', Format.Native, unhex(NATIVE_OK));
    feed(schema, 'Native       CAST', Format.Native, unhex(NATIVE_CAST));
    note('this block declares seq as String "200" while the table says UInt8: the');
    note('disagreement is CAST silently, visible here as a Transform, invisible on a server');
  });
  blank();

  group('Buffers: NO self-description at all');
  const newest = await registry.for(newestLine(lines));
  withSchema(newest, 'CREATE TABLE t (x Int32) ENGINE = MergeTree ORDER BY tuple()', (schema) => {
    kv('  library', `${newest.minor}  (Buffers arrives at 26.5; this block uses the newest installed)`);
    kv('  payload', '4 bytes ff ff ff ff, declared x Int32');
    feed(schema, 'Buffers reinterpret', Format.Buffers, unhex(BUFFERS_OK));
    note("a UInt32 producer's 4294967295 reads back as -1: same width, no metadata, so no");
    note('check CAN fire');
    feed(schema, 'Buffers width', Format.Buffers, unhex(BUFFERS_WIDE));
    note('the same 4 bytes declared 8 wide IS caught: size accounting disagrees');
  });
}

// ---------------------------------------------------------------------------
// SECTION 8: Engines, MergeTree settings and TTL, in the CREATE TABLE
//
// WHAT: declare the engine and the TTL in the one CREATE TABLE statement, then
// watch the STORAGE layer change what a batch stores, including storing nothing.
// WHY: a row can be accepted per row and absent per batch. SummingMergeTree
// folds rows at insert; a TTL already in the past deletes them at merge, with no
// error at any point. A gateway reading only per-row verdicts previews rows the
// table will never hold. (The v0 setters setEngine/setTtl are gone: a schema is
// immutable and the statement says it all, bindings-v1.md §7.)
// LOOK FOR: engineRows being SHORTER than the input; the TTL batch whose row is
// accepted and whose engineRows is empty; the refusal-vs-decline pair on a
// MergeTree setting and on a clock-reading TTL.
// C API: chs_schema_create, chs_preview_batch.
// ---------------------------------------------------------------------------
function section8(lib: Library): void {
  section(8, 'Engines, MergeTree settings, and TTL');

  withSchema(lib, 'CREATE TABLE t (day Date, key UInt32, v UInt64) ENGINE = SummingMergeTree ORDER BY (day, key)', (schema) => {
    kv('(a) the engine', 'SummingMergeTree ORDER BY (day, key)');
    const body = utf8('{"day":"2026-01-01","key":1,"v":5}\n{"day":"2026-01-01","key":1,"v":7}');
    const batch = schema.rows(Format.JSONEachRow, body);
    kv('  rows in / rowsRead', `2 / ${batch.rowsRead}   (v=5 and v=7, same key)`);
    kv('  engineRows (stored)', renderEngineRows(batch));
  });
  note('two rows in, ONE row out, v summed: engineRows is the post-merge preview and,');
  note('when present, the truth to believe over rows');
  blank();

  kv('(b) MergeTree settings', 'refusal vs decline vs accepted');
  const mt = (setting: string): string => `CREATE TABLE t (a UInt8) ENGINE = MergeTree ORDER BY tuple() SETTINGS ${setting}`;
  kv('  unknown NAME', `index_granularityy -> ${classify(() => lib.compileTable(mt('index_granularityy = 8192')))}`);
  note("the server's own 115: this DDL can never exist; tell the tenant");
  kv('  read on insert', `index_granularity=4096 -> ${classify(() => lib.compileTable(mt('index_granularity = 4096')))}`);
  note('a DECLINE where no setting behavior is modeled: a real server might well accept it');
  kv('  storage-only', `old_parts_lifetime=100 -> ${classify(() => lib.compileTable(mt('old_parts_lifetime = 100')))}`);
  note('accepted: only the storage layer reads it, so the rows stored do not change');
  kv('  known, AT default', `index_granularity=8192 -> ${classify(() => lib.compileTable(mt('index_granularity = 8192')))} (inert)`);
  blank();

  withSchema(lib, 'CREATE TABLE t (ts DateTime, v UInt8) ENGINE = MergeTree ORDER BY ts TTL ts + INTERVAL 1 DAY', (schema) => {
    kv('(c) the TTL', 'MergeTree ORDER BY ts, TTL ts + INTERVAL 1 DAY');
    const batch = schema.rows(Format.JSONEachRow, utf8('{"ts":"2020-01-01 00:00:00","v":9}'), {
      settings: { chtypes_now_epoch_nanos: PINNED_CLOCK },
    });
    kv('  row fed', '{"ts":"2020-01-01 00:00:00","v":9}  with the clock pinned to 2023');
    kv('  row-level outcome', `${batch.rows[0]?.outcome}   <- the row PARSED fine`);
    kv('  engineRows (stored)', renderEngineRows(batch));
    for (const t of batch.transformed) {
      kv('  batch transform', `row=${t.row} column=${JSON.stringify(t.column.toString())} reason=${t.reason} lossy=${t.lossy}`);
    }
    note('accepted per ROW, stored nowhere per BATCH: the 2020 timestamp is already past the');
    note('TTL. On a real server this is a silent merge-time delete; the ttl_expired');
    note('transform is the only warning anyone gets.');
  });
  blank();

  kv('(d) a clock TTL', 'TTL ts + INTERVAL 1 DAY relative to now()');
  kv('  compile', classify(() => lib.compileTable('CREATE TABLE t (ts DateTime) ENGINE = MergeTree ORDER BY ts TTL now() + INTERVAL 1 DAY')));
  note('a clock-reading TTL is answered by the library per its own rules;');
  note('this tour reports whatever kind of answer comes back');
}

// ---------------------------------------------------------------------------
// SECTION 9: Settings precedence, who wins
//
// WHAT: the same row and the same handle, answered differently as settings are
// supplied at each layer:
//
//     per-call  >  compile profile  >  setup defaults  >  ClickHouse's own
//
// WHY: this is how a gateway declares a deployment's settings ONCE (at compile)
// yet still lets one INSERT override per call. If precedence were fuzzy, the
// declared profile would not actually be in force.
// LOOK FOR: the SAME handle and bytes passing under the compile profile and
// failing the moment a per-call value overrides it. The setup-defaults layer is
// fixed once, before traffic: there is no setter, and asking for a different one
// is a UsageError (bindings-v1.md §6).
// C API: chs_preview_row (settings), chs_schema_create (settings).
// ---------------------------------------------------------------------------
function section9(lib: Library): void {
  section(9, 'Settings precedence: who wins');
  kv('the probe', 'ts DateTime  fed  {"ts":"2026-01-15T10:30:00Z"}');
  note("stock ClickHouse parses 'basic' datetimes only; best_effort accepts ISO-8601, so");
  note('the verdict TELLS you which setting value won');
  blank();

  const iso = utf8('{"ts":"2026-01-15T10:30:00Z"}');
  const basic = { date_time_input_format: 'basic' };
  const bestEffort = { date_time_input_format: 'best_effort' };
  const ddl = 'CREATE TABLE t (ts DateTime) ENGINE = MergeTree ORDER BY tuple()';
  kv('library', lib.version);

  const plain = lib.compileTable(ddl);
  const profiled = lib.compileTable(ddl, { settings: bestEffort });
  try {
    kv('1. ClickHouse defaults', verdict(plain.row(Format.JSONEachRow, iso)));
    note('nothing declared anywhere: the stock default decides');
    kv('2.  + per-call basic', verdict(plain.row(Format.JSONEachRow, iso, { settings: basic })));
    kv('3. compile profile best_effort', verdict(profiled.row(Format.JSONEachRow, iso)));
    note('the profile declared at COMPILE reaches every later call on that schema');
    kv('4.  + per-call basic', verdict(profiled.row(Format.JSONEachRow, iso, { settings: basic })));
    note('3 vs 4 is the requirement, measured: the same row PASSES under the profile and');
    note('FAILS when the per-call value overrides it');
    kv('5. per-call best_effort', verdict(plain.row(Format.JSONEachRow, iso, { settings: bestEffort })));
    blank();

    kv('setup defaults', 'the process-wide layer, fixed once');
    kv('  setup({ timezone: "UTC" }) again', caught(() => setup({ timezone: 'UTC' })) );
    kv('  setup({ defaults: {...} }) now', truncate(caught(() => setup({ timezone: 'UTC', defaults: bestEffort })), 96));
    note('the first setup stands: defaults are chosen before the first open and never change');
    note('during traffic. A different one is a UsageError naming both, not a silent swap.');
    blank();

    kv('the per-call zone', 'a settings key and nothing more');
    kv('  sessionTimezone + settings key', truncate(caught(() => plain.row(Format.JSONEachRow, iso, { settings: { session_timezone: 'UTC' }, sessionTimezone: 'UTC' })), 90));
    note('given twice, even when they agree, is a UsageError raised before any call');
  } finally {
    plain.close();
    profiled.close();
  }
}

// ---------------------------------------------------------------------------
// SECTION 10: The error taxonomy
//
// WHAT: every kind of answer this SDK gives, told apart BY TYPE, never by string
// matching.
// WHY: the kinds demand different reactions (tell the tenant / fall back
// cautiously / fix the caller / fix the deployment), and the four call classes
// are deliberately PEERS under CallError: a decline can never satisfy
// `instanceof SchemaError`, so a caller handling only refusals cannot silently
// convert declines into rejections.
// LOOK FOR: the same instanceof chain you would write in production, and the
// reminder that ROW verdicts are data (result.outcome), not thrown.
// ---------------------------------------------------------------------------
function section10(lib: Library): void {
  section(10, 'The error taxonomy');

  kv('the TS idiom', 'instanceof against PEER classes');
  raw('      try {');
  raw('        lib.compileTable(ddl);');
  raw('      } catch (err) {');
  raw('        if (err instanceof UnsupportedError) return validateCautiously(err);');
  raw('        if (err instanceof SchemaError) return rejectWith(err.chCode, err.messageBytes);');
  raw('        if (err instanceof UsageError) return fixTheCaller(err);');
  raw('        if (err instanceof ArtifactError) return fixDeployment(err);');
  raw('        throw err;');
  raw('      }');
  blank();

  describeError(() => lib.compileTable('CREATE TABLE t (x NotAType) ENGINE = MergeTree ORDER BY tuple()'), 'compile x NotAType');
  describeError(() => lib.compileTable('CREATE TABLE t (ts DateTime) ENGINE = MergeTree ORDER BY ts TTL now() + INTERVAL 1 DAY'), 'compile a clock TTL');
  const schema = lib.compileTable('CREATE TABLE t (a UInt8) ENGINE = MergeTree ORDER BY tuple()');
  schema.close();
  describeError(() => schema.describe(), 'describe() after close()');
  describeError(() => setup({ timezone: 'America/New_York' }), 'setup with a second zone');
  blank();

  kv('four peers', 'SchemaError, UnsupportedError, UsageError, InternalError');
  kv('common base', `CallError: ${new SchemaError(callFields()) instanceof CallError}   (the explicit way to catch all four)`);
  kv('UnsupportedError is a SchemaError?', `${new UnsupportedError(callFields()) instanceof SchemaError}   <- peers, not a hierarchy`);
  kv('artifact errors', `ArtifactError is not a CallError: ${!(new ArtifactMissingError('x') instanceof CallError)}; isChtypesError is true for both`);
  kv('row verdicts are DATA', 'result.outcome, not a thrown error');
  note('a row the server would reject RETURNS (outcome "rejected", errCode = the server\'s');
  note('code): nothing throws. Exceptions are for questions that could not be asked.');
}

// ---------------------------------------------------------------------------
// SECTION 11: The discovery kit, offline
//
// WHAT: the discovery query chtypes ships for learning what a table looks like
// as STORED, and the call that reads a server's answer into column declarations,
// run here against CANNED bytes shaped exactly like a real server's response.
// WHY: chtypes NEVER opens a socket. You run the query with whatever client you
// already have; the library gives you the SQL and parses the result. The v1
// library holds no SQL of its own beyond that and reconstructs no DDL in the
// binding (bindings-v1.md §7): it hands back the declarations as ClickHouse's
// own formatter writes them, and you build the CREATE TABLE from them.
// LOOK FOR: the declarations (identifier quoting is the library's), then a table
// compiled from them.
// C API: chs_discover_query, chs_discover_columns.
// (The ONLINE version of this flow, against a real server, is in the optional
// Go ingest demo that chplay.sh never runs.)
// ---------------------------------------------------------------------------
function section11(lib: Library): void {
  section(11, 'The discovery kit, offline');
  kv('NOTE', 'the answer below is CANNED: shaped like a real server\'s, so the parser cannot tell');
  blank();

  kv('discoverQuery()', truncate(lib.discoverQuery().toString(), 90));
  note('bind {database:String} and {table:String} and run it with your own client');
  blank();

  for (const line of CANNED_COLUMNS_RESULT.trim().split('\n')) kv('  canned row', truncate(line, 86));
  const found = lib.discoverColumns(utf8(CANNED_COLUMNS_RESULT));
  blank();
  kv('discoverColumns()', `${found.columns.length} columns`);
  for (const c of found.columns) kv(`  ${c.name.toString()}`, c.declaration.toString());
  kv('columnsSql', found.columnsSql.toString());
  note('the DEFAULTs are carried and `reading c` came back QUOTED, in the spelling THIS');
  note('build prints: the quoting is the library\'s, not a rule in the binding');
  blank();

  const ddl = `CREATE TABLE t (${found.columnsSql.toString()}) ENGINE = MergeTree ORDER BY tuple()`;
  kv('compile', ddl);
  withSchema(lib, ddl, (schema) => {
    const row = '{"ts":"2026-01-15T10:30:00Z","device_id":9,"reading c":21.5}';
    kv('the same row, twice', row);
    kv('  stock settings', verdict(schema.row(Format.JSONEachRow, utf8(row))));
    kv('  deployment profile', verdict(schema.row(Format.JSONEachRow, utf8(row), { settings: { date_time_input_format: 'best_effort' } })));
  });
  note('a deployment that declared date_time_input_format=best_effort takes the ISO-8601');
  note('timestamp; a stock compile answers for a server the tenant does not have.');
  note('Discovering the deployment\'s settings is what closes that gap (the caller supplies');
  note('them: 1.0 has no call that reads a server\'s settings).');
}

// ---------------------------------------------------------------------------
// SECTION 12: Version pinning, same input, different answers
//
// WHAT: the same DDL and the same bytes, swept across every installed line.
// WHY: version differences are the reason the registry exists. They are not
// monotonic: newer is NOT always more permissive, so no rule can predict them;
// only the real per-version artifact can answer.
// LOOK FOR: a DEFAULT one line rejects and its neighbors accept; and the Buffers
// format not existing before 26.5 (the server's own 73).
// ---------------------------------------------------------------------------
async function section12(registry: Registry, lines: readonly string[]): Promise<void> {
  section(12, 'Version pinning: same input, different answers');
  if (lines.length < 2) {
    kv('lines installed', lines.join('  '));
    note('only one line is installed, so there is nothing to sweep; this section needs two.');
    note('Fetch another (`chtypes fetch 26.7`) and re-run to see the answers diverge.');
    return;
  }

  kv('(a) a mixed-type DEFAULT', "a UInt8, x Int64 DEFAULT if(1,2,'a')");
  for (const v of lines) {
    const lib = await openOrNote(registry, v);
    if (lib === undefined) continue;
    try {
      const schema = lib.compileTable("CREATE TABLE t (a UInt8, x Int64 DEFAULT if(1,2,'a')) ENGINE = MergeTree ORDER BY tuple()");
      const col = schema.describe().columns[1];
      kv(`  ${v}`, `compiled  (${col?.type.toString()} DEFAULT ${col?.defaultExpr.toString()})`);
      schema.close();
    } catch (err) {
      if (!(err instanceof SchemaError)) throw err;
      kv(`  ${v}`, `REJECTED code ${err.chCode}  ${truncate(err.messageBytes.toString(), 52)}`);
    }
  }
  note('NEWER IS NOT ALWAYS MORE PERMISSIVE: this is exactly why one real artifact per line exists');
  blank();

  kv("(b) a format's arrival", 'Buffers (code 9), added in ClickHouse 26.5');
  for (const v of lines) {
    const lib = await openOrNote(registry, v);
    if (lib === undefined) continue;
    kv(`  ${v} lists Buffers`, String(lib.buildInfo.capabilities.inputFormats.includes(formatChName(Format.Buffers) ?? '')));
    withSchema(lib, 'CREATE TABLE t (x Int32) ENGINE = MergeTree ORDER BY tuple()', (schema) => {
      const batch = schema.rows(Format.Buffers, unhex(BUFFERS_OK));
      const first = batch.rows[0]?.values[0];
      if (batch.outcome === 'accepted' && first !== undefined) kv(`  ${v}`, `accepted  x = ${first.text.toString()}`);
      else kv(`  ${v}`, `${batch.outcome}  code=${batch.errCode}  ${truncate(batch.errMsg.toString(), 40)}`);
    });
  }
  note("73 UNKNOWN_FORMAT is the SERVER's own answer on the older lines: BuildInfo.capabilities");
  note('says what a build reads, so ask the build instead of your own version arithmetic');
}

// ---------------------------------------------------------------------------
// SECTION 13: Teardown
//
// WHAT: what to release, and when.
// WHY: schema, filter and block handles are native allocations: close() each, or
// `using` where the runtime supports it (Node >= 24; on Node 22 `using` is a
// syntax error, so portable code uses close()). A handle you abandon is freed by
// its finalizer, and liveHandles() is how a suite proves it.
// LOOK FOR: the live-handle counts moving with open and close; and that a
// registry and a library own nothing to close.
// C API: chs_schema_free, chs_live_handles.
// ---------------------------------------------------------------------------
function section13(registry: Registry): void {
  section(13, 'Teardown');
  const lib = registry.libraries()[0];
  if (lib === undefined) return;
  const live = (): string => JSON.stringify(lib.liveHandles());
  kv('liveHandles() before', live());
  const a = lib.compileTable('CREATE TABLE t (a UInt8) ENGINE = MergeTree ORDER BY tuple()');
  const f = a.compileFilter('a = 1');
  kv('after a schema + a filter', live());
  a.close();
  kv('a.close()', `${live()}   (the filter holds its own reference to the schema, so it still works)`);
  f.close();
  f.close();
  kv('f.close() twice', `${live()}   (close is idempotent)`);
  note('a library is NEVER unloaded and there is no registry.close(): an image lives until');
  note('the process exits, so no teardown order exists to get wrong. Using a closed object');
  note('is a UsageError raised before any call (section 10).');
}

// ---------------------------------------------------------------------------
// SECTION 14: The linked image (Go only)
//
// Go's cgo build can additionally LINK one artifact directly (OpenLinked, under
// the chtypes_linked build tag) instead of dlopen-ing it. TypeScript cannot have
// that shape: ffi-rs always dlopens, so Registry is the only loader here, and it
// is the product path anyway. See the Go tour's section 14 for the real thing.
// ---------------------------------------------------------------------------
function section14(): void {
  section(14, 'The linked image (Go only)');
  kv('not offered in TypeScript', 'ffi-rs always dlopens; Registry is the only loader');
  note('the statically linked single-version shape is Go-only (bindings-v1.md §2)');
}

// ---------------------------------------------------------------------------
// SECTION 15: Export, bytes plus spans
//
// WHAT: the SAME call that judges a batch can also serialize its accepted rows
// to wire bytes, addressed per row by index-aligned spans.
// WHY: every consumer of an accepted row wants the stored bytes ready to publish
// or INSERT, and this export is also how a generated DEFAULT reaches the table:
// insert the library's output, never the original body (bindings-v1.md §5).
// LOOK FOR: the skipped row's {0,0} span; a span slice BEING the row's line; a
// poisoned batch DECLINING the export and saying why (as bytes); emitted-empty
// versus declined; and the lean document (docFlags 0) keeping every verdict.
// C API: chs_preview_batch with export_format and doc_flags.
// ---------------------------------------------------------------------------
function section15(lib: Library): void {
  section(15, 'Export: bytes + spans');
  const exportFormat = Format.JSONCompactEachRow;
  if (!lib.buildInfo.capabilities.exportFormats.includes(formatChName(exportFormat) ?? '')) {
    note(`this build does not list ${formatChName(exportFormat)} as an export format; section 15 degrades here`);
    return;
  }
  const schema = lib.compileTable(FORMAT_DDL);
  const body = utf8('{"device_id":1,"label":"ok"}\n{"device_id":"zap"}\n{"device_id":3,"label":"hi"}\n');
  const settings = { input_format_allow_errors_num: '10' };
  const batch = schema.rows(Format.JSONEachRow, body, { settings, exportFormat, docFlags: DocFlags.All });
  kv('batch', `${batch.outcome}  rows_read=${batch.rowsRead} rows_skipped=${batch.rowsSkipped}`);
  const payload = batch.payload ?? Buffer.alloc(0);
  kv('payload', `${JSON.stringify(payload.toString())}  (${payload.length} bytes, one line per ACCEPTED row)`);
  note('wire order = declared minus MATERIALIZED/ALIAS/EPHEMERAL, so these bytes are directly');
  note("INSERT-able with no column list; DEFAULTs (seq=7, label='unknown') are already applied");
  (batch.spans ?? []).forEach((span, i) => {
    const line = span.len ? JSON.stringify(payload.subarray(span.off, span.off + span.len).toString()) : '(no bytes: row not accepted)';
    kv(`  span[${i}] {off:${span.off} len:${span.len}}`, `${batch.rows[i]?.outcome} -> ${line}`);
  });
  note('spans are INDEX-ALIGNED with rows; concatenating the non-zero spans reproduces the payload');
  blank();

  const lean = schema.rows(Format.JSONEachRow, body, { settings, exportFormat, docFlags: 0 });
  kv(
    'lean (docFlags 0)',
    `outcome ${lean.outcome}, ${lean.rows.length} verdict rows, ${lean.rows[0]?.values.length ?? 0} values, ${lean.transformed.length} transformed` +
      ` - payload identical: ${lean.payload !== undefined && Buffer.compare(lean.payload, payload) === 0}`,
  );
  note('flags thin the DESCRIPTION, never the VERDICT; Values / Transforms / Defaults pick groups');
  blank();

  withSchema(lib, "CREATE TABLE t (e Enum8('a' = 1, 'b' = 2)) ENGINE = MergeTree ORDER BY tuple()", (poisonSchema) => {
    const poi = poisonSchema.rows(Format.JSONEachRow, utf8('{"e":null}\n'), {
      settings: { input_format_defaults_for_omitted_fields: '0' },
      exportFormat,
      docFlags: DocFlags.All,
    });
    const desc = poi.payload === undefined ? 'undefined (declined)' : `${poi.payload.length} bytes`;
    kv('poisoned batch', `${poi.outcome} -> payload=${desc} exportDeclined=${JSON.stringify(truncate(poi.exportDeclined.toString(), 48))}`);
    note('exportDeclined is bytes: the reason an asked-for export was refused, empty otherwise');
  });

  const emp = schema.rows(Format.JSONEachRow, new Uint8Array(0), { exportFormat });
  kv('empty batch', `payload defined=${emp.payload !== undefined} len=${emp.payload?.length ?? 0}  (emitted-empty != declined)`);
  schema.close();
}

// ---------------------------------------------------------------------------
// SECTION 16: Filters, WHERE semantics at the edge
//
// WHAT: compile one boolean expression against a schema (compileFilter) and
// evaluate it per row of a body, with ClickHouse's own comparison functions, so
// the answers are WHERE-side by construction.
// WHY: read-side row visibility (who may SEE this row) is a WHERE question, and
// WHERE coercion is NOT insert coercion: `x = 256` over UInt8 PROMOTES (false for
// every row) where an insert would wrap 256 to 0.
// LOOK FOR: 'f','f' where the insert path stores 0; NULL being not-true; the 'e'
// class (compiles, then THROWS per row); clock reads and unbound {p:Type}
// REFUSED at compile; query parameters; the block twin; and the filter's zone.
// C API: chs_filter_create, chs_filter_eval_body, chs_filter_eval_block,
// chs_block_create.
// ---------------------------------------------------------------------------
function section16(lib: Library): void {
  section(16, 'Filters: WHERE semantics at the edge');
  const schema = lib.compileTable('CREATE TABLE t (x UInt8) ENGINE = MergeTree ORDER BY tuple()');
  {
    const filt = schema.compileFilter('x = 256');
    const fr = filt.rows(Format.JSONEachRow, utf8('{"x":0}\n{"x":255}\n'));
    filt.close();
    kv('filter `x = 256` over UInt8', `verdicts ${verdictString(fr)}`);
    note("PROMOTES, never wraps: false for x=0 AND x=255. The insert side of this same library");
    note("stores 256 as 0 (section 5's overflow_wrap), so predicate constants are never");
    note('folded through insert coercion');
  }
  blank();

  {
    const nullableSchema = lib.compileTable('CREATE TABLE t (lvl Nullable(UInt8)) ENGINE = MergeTree ORDER BY tuple()');
    const nf = nullableSchema.compileFilter('lvl = 1');
    const fr = nf.rows(Format.JSONEachRow, utf8('{"lvl":null}\n{"lvl":1}\n'));
    nullableSchema.close();
    kv('`lvl = 1` on [null, 1]', `verdicts ${verdictString(fr)}   (NULL is not true, as WHERE hides it)`);
  }
  blank();

  {
    const strSchema = lib.compileTable('CREATE TABLE t (s String) ENGINE = MergeTree ORDER BY tuple()');
    const sf = strSchema.compileFilter('s = 257');
    const fr = sf.rows(Format.JSONEachRow, utf8('{"s":"hi"}\n'));
    strSchema.close();
    kv('`s = 257` over String', `verdicts ${verdictString(fr)}`);
    for (const fe of fr.errors) kv(`  row ${fe.row}`, `code ${fe.code}  ${truncate(fe.msg.toString(), 56)}`);
    note("on a real server this WHERE fails the WHOLE query: 'e' is NOT an answer, and neither");
    note("is 'd' (a row this library declines). Verdict answered? e=" + verdictAnswered('e') + ' d=' + verdictAnswered('d') + ' t=' + verdictAnswered('t'));
    note('an enforcing caller fails CLOSED on both, or NOT(decline-as-false) inverts it');
  }
  blank();

  {
    const lt = schema.compileFilter('x < 5');
    const fr = lt.rows(Format.JSONEachRow, utf8('{"x":1}\n{"x":"zap"}\n{"x":9}\n'));
    lt.close();
    kv('`x < 5` on [1, bad, 9]', `verdicts ${verdictString(fr)}   (the bad row cannot swallow the tail)`);
  }
  blank();

  kv('compile `now() > x`', classify(() => schema.compileFilter('now() > x')));
  kv('compile `x = {p:UInt8}`', classify(() => schema.compileFilter('x = {p:UInt8}')));
  kv('compile `nosuch = 1`', classify(() => schema.compileFilter('nosuch = 1')));
  note("clock reads would be answered with THIS process's clock, not the server's; an UNBOUND");
  note('{p:Type} is the server\'s own 456: bind it instead');
  schema.close();
  blank();

  group('query parameters: values are STRINGS, never escaped');
  const eventBody = utf8(
    '{"tenant":"acme","role":"admin","x":1}\n' +
      '{"tenant":"evil","role":"viewer","x":2}\n' +
      '{"tenant":"\' OR 1=1 --","role":"admin","x":3}\n',
  );
  const ps = lib.compileTable('CREATE TABLE t (tenant String, role String, x UInt8) ENGINE = MergeTree ORDER BY tuple()');
  {
    const tf = ps.compileFilter('tenant = {t:String}', { params: { t: 'acme' } });
    kv('`tenant = {t:String}`, t=acme', `verdicts ${verdictString(tf.rows(Format.JSONEachRow, eventBody))}`);
    tf.close();
    note('compiled ONCE per (schema, expr, params): the value is baked in; a per-tenant cache');
    note('MUST be a bounded LRU plus a compile throttle');
    const hostile = "' OR 1=1 --";
    const hf = ps.compileFilter('tenant = {t:String}', { params: { t: hostile } });
    const hr = hf.rows(Format.JSONEachRow, eventBody);
    hf.close();
    const inert = hr.verdicts.join('') === 'fft';
    kv("t = `' OR 1=1 --` (hostile)", `verdicts ${verdictString(hr)}   hostile-value-inert: ${inert}`);
    note('the value became a typed LITERAL after SQL parsing: it matches only the row holding');
    note('exactly that string, and NOTHING was escaped to get there');
    note('size the {brace type} for the value\'s domain ({p:UInt8} given "256" BINDS 0)');
  }
  blank();

  group('the block twin: parse ONCE, evaluate K filters');
  {
    const block = ps.parseBlock(Format.JSONEachRow, eventBody);
    const adminF = ps.compileFilter("role = 'admin'");
    const viewerF = ps.compileFilter("role = 'viewer'");
    kv("eval `role = 'admin'`", `verdicts ${verdictString(adminF.eval(block))}`);
    kv("eval `role = 'viewer'`", `verdicts ${verdictString(viewerF.eval(block))}`);
    block.close();
    adminF.close();
    viewerF.close();
    note('ONE parse of the 3-row event fed BOTH filters: eval neither consumes nor mutates the');
    note('block, and eval(parseBlock(body)) is rows(body) for every verdict class');
  }
  blank();

  group("a filter's zone is its own, fixed at compile");
  {
    const zs = lib.compileTable('CREATE TABLE t (ts DateTime) ENGINE = MergeTree ORDER BY tuple()');
    const zf = zs.compileFilter("ts > '2026-01-15 10:00:00'", { sessionTimezone: 'Asia/Tokyo' });
    const fr = zf.rows(Format.JSONEachRow, utf8('{"ts":"2026-01-15 09:30:00"}\n{"ts":"2026-01-15 10:30:00"}\n'), { sessionTimezone: 'UTC' });
    zf.close();
    zs.close();
    kv('compile in Tokyo, body parsed as UTC', `verdicts ${verdictString(fr)}`);
    note('the compile zone governs the WHERE literal, the call zone only how the body is parsed;');
    note('differing zones are an ordinary input, answered the way a server answers them');
  }
  ps.close();
  blank();

  note('ENFORCEMENT GATE: nothing may enforce read-side security on this API until the');
  note('WHERE-truth rig gates green (zero over-admit, zero over-hide). Until then this is a');
  note('shadow/replay surface: log disagreements, enforce with what enforced yesterday.');
}

// ---------------------------------------------------------------------------
// SECTION 17: The INSERT column list
//
// WHAT: name the columns THIS ROW supplies (the `columns` option) instead of
// relying on the no-list default.
// WHY: an INSERT is often an explicit column list, including EPHEMERAL columns,
// which the no-list shape cannot express: fed without a list, an EPHEMERAL value
// is silently dropped as an unknown field and a DEFAULT that references it
// computes from the type's own zero: a wrong stored value with no error anywhere.
// LOOK FOR: the listed EPHEMERAL value feeding the DEFAULT (d = 6, not the
// type-zero-derived 1); the refusal when the list names a column the schema does
// not have (code 16).
// C API: chs_preview_row (columns).
// ---------------------------------------------------------------------------
function section17(lib: Library): void {
  section(17, 'The INSERT column list');
  withSchema(lib, COLUMNS_DDL, (schema) => {
    kv('schema', COLUMNS_DDL);
    blank();

    const listed = schema.row(Format.JSONEachRow, utf8('{"id":3,"e":5}'), { columns: ['id', 'e'] });
    const d = listed.values.find((v) => v.column.toString() === 'd');
    kv('row {id:3,e:5}  columns=[id,e]', `${listed.outcome}  d=${d ? d.text.toString() : '<missing>'}`);
    kv('  e as reported', listed.columns.filter((v) => v.column.toString() === 'e').map((v) => `${text(v)} (${v.source}, stored=${v.isStored})`).join(''));
    note("e is EPHEMERAL: LISTED, so its value IS read and IS in scope for d's DEFAULT (e + 1),");
    note('so d stores 6; e itself is never stored. With NO list e would be an unknown field.');
    blank();

    const refused = schema.row(Format.JSONEachRow, utf8('{"id":1,"nosuch":5}'), { columns: ['id', 'nosuch'] });
    kv('row  columns=[id,nosuch]  (unknown column)', `${refused.outcome}  code=${refused.errCode}  ${truncate(refused.errMsg.toString(), 56)}`);
    note('code 16 NO_SUCH_COLUMN_IN_TABLE: the SAME code and message an ALIAS column in the');
    note('list would get, indistinguishable from the wire');
  });
}

// ------------------------------------------------------------- plumbing
//
// Everything below is printing helpers: no chtypes call hides here beyond the
// small wrappers named in their comments.

function section(n: number, title: string): void {
  console.log(`\n=== ${n}. ${title} ===`);
}
function kv(key: string, value: string): void {
  console.log(`  ${key.padEnd(30)} ${value}`);
}
function note(line: string): void {
  console.log(`      . ${line}`);
}
function raw(line: string): void {
  console.log(line);
}
function blank(): void {
  console.log();
}
function group(title: string): void {
  console.log(`  -- ${title}`);
}
function fatal(message: string): never {
  console.error(`\nplayground: ${message}`);
  process.exit(1);
}

/** Compile, hand the schema to fn, always close it. */
function withSchema(lib: Library, ddl: string, fn: (schema: Schema) => void): void {
  const schema = lib.compileTable(ddl);
  try {
    fn(schema);
  } finally {
    schema.close();
  }
}

/** Run one payload through row() and print the verdict on one line, plus a "~" line per Transform (a silent change ClickHouse made). */
function feed(schema: Schema, label: string, format: Format, body: Uint8Array): void {
  const r: RowResult = schema.row(format, body);
  const parts: string[] = [r.outcome];
  if (r.errCode) parts.push(`code=${r.errCode}`);
  const vals = r.values.map((v) => `${v.column.toString()}=${text(v)}(${v.source})`).join(' ');
  if (vals) parts.push(vals);
  if (r.errMsg.length) parts.push(truncate(r.errMsg.toString(), 56));
  kv(`  ${label}`, parts.join('  '));
  for (const t of r.transformed) {
    raw(`      ~ ${t.column.toString()}: ${t.input.toString()} -> ${t.stored.toString()} (${t.reason}${t.lossy ? ', LOSSY' : ''})`);
  }
}

/** Print which typed error a call throws: peers, so plain instanceof. */
function describeError(call: () => unknown, what: string): void {
  try {
    call();
    kv(what, '(no error)');
  } catch (err) {
    if (err instanceof UnsupportedError) {
      kv(what, 'UnsupportedError  (a DECLINE)');
      kv('  detail', truncate(err.messageBytes.toString(), 84));
      kv('  also a SchemaError?', `${(err as unknown) instanceof SchemaError}   <- peers, not a hierarchy`);
    } else if (err instanceof SchemaError) {
      kv(what, `SchemaError  (a REFUSAL), chCode=${err.chCode} ${err.chName}`);
      kv('  detail', truncate(err.messageBytes.toString(), 84));
    } else if (err instanceof InternalError) {
      kv(what, 'InternalError  (a LIBRARY FAULT)');
      kv('  message', truncate(err.message, 84));
    } else if (err instanceof UsageError) {
      kv(what, 'UsageError  (caller misuse, caught before the library)');
      kv('  message', truncate(err.message, 84));
    } else if (err instanceof ArtifactError) {
      kv(what, `${errName(err)}  (a deployment problem, not a verdict)`);
      kv('  message', truncate(err.message, 84));
    } else {
      throw err;
    }
  }
}

/** Name a call's error KIND in one word. */
function classify(call: () => unknown): string {
  try {
    const result = call() as { close?: () => void } | undefined;
    result?.close?.();
    return 'accepted';
  } catch (err) {
    if (err instanceof UnsupportedError) return 'DECLINED  (UnsupportedError)';
    if (err instanceof SchemaError) return `REFUSED   (SchemaError, code ${err.chCode})`;
    if (err instanceof InternalError) return 'LIBRARY FAULT  (InternalError)';
    throw err;
  }
}

function caught(call: () => unknown): string {
  try {
    call();
    return '(no error)';
  } catch (err) {
    return errText(err);
  }
}

function verdict(r: RowResult): string {
  if (r.errCode) return `${r.outcome.padEnd(9)} code=${r.errCode}  ${truncate(r.errMsg.toString(), 44)}`;
  const v = r.values[0];
  if (v !== undefined) return `${r.outcome.padEnd(9)} ${v.column.toString()} = ${v.text.toString()}`;
  return r.outcome;
}

function renderEngineRows(batch: BatchResult): string {
  const rows = batch.engineRows ?? [];
  return `[${rows.map((cells) => `(${cells.map((c) => c.text.toString()).join(', ')})`).join(', ')}]  (length ${rows.length})`;
}

/** A FilterResult's verdicts as the document's compact t/f/e/d string. */
function verdictString(fr: FilterResult): string {
  if (fr.outcome !== 'ok') return `(call-level: outcome=${fr.outcome} code=${fr.errCode} ${truncate(fr.errMsg.toString(), 40)})`;
  return `"${fr.verdicts.join('')}"`;
}

/** A value's rendering, with a marker for an unreadable one. */
function text(value: Value): string {
  const t = value.text.toString();
  return t === '' && !value.null ? '<unreadable>' : t;
}

async function openOrNote(registry: Registry, line: string): Promise<Library | undefined> {
  try {
    return await registry.for(line);
  } catch (err) {
    if (!isChtypesError(err)) throw err;
    kv(`  ${line}`, `SKIPPED  ${truncate(err.message, 70)}`);
    return undefined;
  }
}

/** A well-formed set of call-error fields, only to show the class relationships in section 10. */
function callFields(): ConstructorParameters<typeof SchemaError>[0] {
  return { status: 0, chCode: 0, chName: '', messageBytes: Buffer.alloc(0), column: Buffer.alloc(0) };
}

function errName(err: unknown): string {
  return err instanceof Error ? err.constructor.name : typeof err;
}

function errText(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

function truncate(value: string, limit: number): string {
  const flat = value.replace(/\n/g, ' ');
  return flat.length <= limit ? flat : `${flat.slice(0, limit)}...`;
}

function utf8(value: string): Uint8Array {
  return new TextEncoder().encode(value);
}

function unhex(value: string): Uint8Array {
  const out = new Uint8Array(value.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = Number.parseInt(value.slice(i * 2, i * 2 + 2), 16);
  return out;
}

/** Numeric order of two lines ("25.10" > "25.3"), which a string sort gets backwards. */
function compareLines(a: string, b: string): number {
  const pa = a.split('.').map(Number);
  const pb = b.split('.').map(Number);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const d = (pa[i] ?? -1) - (pb[i] ?? -1);
    if (d !== 0) return d;
  }
  return 0;
}

function newestLine(lines: readonly string[]): string {
  const sorted = [...lines].sort(compareLines);
  return sorted[sorted.length - 1] ?? '';
}

main().catch((err: unknown) => {
  console.error(`\nplayground: ${err instanceof Error ? (err.stack ?? err.message) : String(err)}`);
  process.exit(1);
});
