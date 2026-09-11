// The TypeScript/Node tour of chtypes.
//
// WHAT THIS IS
//
// chtypes answers one question: "if this row were inserted into this table on
// this ClickHouse version, what would happen?" — without a server. The
// answers come from ClickHouse's own C++ (vendored per release into shared
// libraries behind a 22-function C ABI), which is why they are exact rather
// than approximately right.
//
// This file is a tutorial you RUN. Fourteen numbered sections walk the whole
// public API of the TypeScript SDK, from loading an artifact to tearing down,
// each with a comment saying what it demonstrates, why an ingest pipeline
// cares, and what to look at in the output. The same fourteen sections — same
// numbering, same schemas, same rows — exist in go/main.go, python/demo.py
// and rust/src/main.rs, so you can diff two tours and see only the language
// idioms differ.
//
// EVERYTHING HERE IS OFFLINE. You need node + pnpm and the artifacts in
// the registry (scripts/fetch.sh, or a core-repository build) — no Docker, no ClickHouse
// server, no network. Even the discovery-kit section (11) runs offline,
// against CANNED bytes shaped exactly like a real server's responses.
//
//   pnpm install && pnpm demo             # newest vendored version
//   CHTYPES_VERSION=25.8 pnpm demo        # pick a line
//   ../chplay.sh ts                       # same, with prerequisite checks
//
// Nothing here is a test — the real suites live in ts. Every number
// printed below is produced by the run, never written down by hand.


import {
  ABI_REVISION,
  CODE_UNSUPPORTED,
  ChtypesError,
  CompileMode,
  DOC_ALL,
  Format,
  QUERY_CHANGED_SETTINGS,
  QUERY_SERVER_VERSION,
  Registry,
  RegistryError,
  SchemaError,
  UnsupportedError,
  formatName,
  looksLikeRegistry,
  minorOf,
  nativeStats,
  parseChangedSettingsResult,
  parseColumnsResult,
  parseVersionResult,
  reconstructDdl,
  defaultRegistryDir,
} from '@wavehouse/chtypes';

// ------------------------------------------------------------- the fixture
//
// These constants are IDENTICAL in all four playgrounds. Change one here and
// you must change it in go/main.go, python/demo.py and rust/src/main.rs too —
// the point of this directory is that the four outputs can be diffed.

// The tenant's table for the DEFAULT/result sections: one of everything the
// tour needs — a volatile DEFAULT (now64), a literal DEFAULT, an Enum (how a
// table gets poisoned), and a MATERIALIZED column (never in SELECT *).
const DEMO_DDL = `ts DateTime64(3) DEFAULT now64(3),
device_id UInt32,
seq UInt8 DEFAULT 0,
payload String,
grade Enum8('a' = 1, 'b' = 2),
payload_len UInt32 MATERIALIZED length(payload)`;

// A small three-column table for the outcome and format sections. Positional
// formats (CSV, TSV, Values, RowBinary...) are far easier to read against a
// small schema, and both DEFAULTs give the empty-field rules something to do.
const FORMAT_DDL = "device_id UInt32, seq UInt8 DEFAULT 7, label String DEFAULT 'unknown'";

// Pinning the clock is what makes a demo with now64(3) in it reproducible.
// The value is a STRING at the boundary, always: 19 digits do not survive an
// IEEE double — and in this SDK a JS `number` setting is a runtime error on
// purpose (string | bigint only).
const PINNED_CLOCK = '1700000000000000000'; // 2023-11-14 22:13:20 UTC

// Hand-built binary payloads (hex), shared by all four tours. Each is
// explained where it is fed. The RBWD payloads are the committed fixtures
// from tests/fixtures/rbwd/, whose value bytes a real ClickHouse wrote.
const ROW_BINARY_OK = '0100000007026f6b'; // UInt32 LE 1, UInt8 7, varint-len "ok"
const RBWD_MARKER = '00020000000100026869'; // device_id=2 by value, seq by marker, label="hi"
const RBWD_POISON = '000100000001'; // id=1 by value, e by marker -> raw 0, NO name
const RBWD_VALUE = '00010000000002'; // id=1 by value, e by value 2 ("green")
// RowBinaryWithNamesAndTypesAndDefaults: LEB128 column count, names, types,
// then RBWD-style marker+value rows. Built by hand for FORMAT_DDL:
// 3 cols, device_id=1 by value, seq by marker (DEFAULT 7), label="ok".
const RBWNTD_ROW =
  '03' + '096465766963655f6964' + '03736571' + '056c6162656c' +
  '0655496e743332' + '0555496e7438' + '06537472696e67' +
  '00' + '01000000' + '01' + '00' + '026f6b';
// Native blocks captured from a live 25.8 (SELECT ... FORMAT Native).
const NATIVE_OK =
  '0301096465766963655f69640655496e74333201000000037365710555496e743807056c6162656c06537472696e67026f6b';
const NATIVE_CAST =
  '0301096465766963655f69640655496e743332020000000373657106537472696e6703323030056c6162656c06537472696e670463617374';
// Buffers: uint64le n_columns, n_rows, then per column a byte size and the
// raw column. 4 bytes ff ff ff ff — declared 4 wide, then (wrongly) 8 wide.
const BUFFERS_OK = '010000000000000001000000000000000400000000000000ffffffff';
const BUFFERS_WIDE = '010000000000000001000000000000000800000000000000ffffffff';

// Section 11's CANNED server responses. These are not live bytes — they are
// shaped EXACTLY like a real ClickHouse's JSONEachRow answers to the three
// discovery queries (quoted UInt64s and all, matching a stock HTTP server's
// output_format_json_quote_64bit_integers=1). Swap in your own HTTP client's
// bytes and nothing else changes.
const CANNED_VERSION_RESULT = '{"version":"25.8.28.1"}\n';
const CANNED_SETTINGS_RESULT =
  '{"name":"flatten_nested","value":"0"}\n' +
  '{"name":"date_time_input_format","value":"best_effort"}\n';
const CANNED_COLUMNS_RESULT =
  '{"name":"ts","type":"DateTime64(3)","default_kind":"DEFAULT","default_expression":"now64(3)","position":"1"}\n' +
  '{"name":"device_id","type":"UInt32","default_kind":"","default_expression":"","position":"2"}\n' +
  '{"name":"reading c","type":"Float64","default_kind":"","default_expression":"","position":"3"}\n' +
  `{"name":"note","type":"String","default_kind":"DEFAULT","default_expression":"'unset'","position":"4"}\n`;

function main() {
  const { registry, lib } = section1();
  try {
    section2(lib);
    section3(lib);
    section4(lib);
    section5(lib);
    section6(lib);
    section7(lib, registry);
    section8(lib);
    section9(lib);
    section10(lib);
    section11(registry);
    section12(registry);
    section13(registry);
    section14();
    section15(lib);
    section16(lib);
  } finally {
    // The teardown section 13 narrates: the LAST thing this process does
    // with the registry. Reopening an artifact after a full close is not a
    // promised operation (measured: it can crash), so the tour keeps ONE
    // registry for its whole life and closes it exactly once, here.
    registry.close();
  }

  blank();
  console.log('Done. Every value above was measured by this run.');
  console.log('The optional ONLINE demo (a real server, end to end) is go/ingest-demo/.');
}

// ---------------------------------------------------------------------------
// SECTION 1 — Load the library and check the ABI
//
// WHAT: open the artifact registry, see every ClickHouse version
// resident in this one process, pick one, and check the ABI revision.
// WHY: an ingest gateway serves tenants on different ClickHouse versions at
// once; the registry is how one process answers for all of them, exactly.
// LOOK FOR: the artifact NAMING ITSELF, and the refusal for a version that is
// not built — never a silent nearest-version fallback.
// C API: chs_clickhouse_version, chs_abi_revision, chs_init (implicit on
// load), chs_free (implicit on every returned string).
// TS extras: looksLikeRegistry, registry.has(), minorOf.
// ---------------------------------------------------------------------------
function section1() {
  section(1, 'Load the library and check the ABI');

  const dir = registryDir();
  kv('looksLikeRegistry(dir)', String(looksLikeRegistry(dir)));
  let registry;
  try {
    registry = new Registry(dir);
  } catch (err) {
    fatal(
      `open registry ${JSON.stringify(dir)}: ${err.message}\n\n` +
        'Fetch an artifact first: `scripts/fetch.sh 25.8` (or build one in the core repository).',
    );
  }
  const versions = registry.versions();
  kv('registry dir', registry.dir);
  kv('versions resident', versions.join('  '));
  note('one dlopen (RTLD_LOCAL) per version — all live in THIS process at once');
  if (versions.length === 1) {
    note("only one artifact is built; the tour still runs, and section 12's");
    note('cross-version sweeps will degrade gracefully. More: `scripts/fetch.sh 26.7`');
  }

  // Version selection: a minor line ("25.8") and an exact patch
  // ("25.8.28.1-lts") both resolve. Docker tags drift, so an
  // exact-match-only lookup would silently lose a whole version line.
  let want = process.env.CHTYPES_VERSION;
  if (want) {
    kv('version selected', `${want}  (from $CHTYPES_VERSION)`);
  } else {
    want = newestLine(versions);
    kv('version selected', `${want}  (default: newest held; set $CHTYPES_VERSION to change)`);
  }
  kv('registry.has(want)', `${registry.has(want)}   (has("99.9") = ${registry.has('99.9')})`);
  let lib;
  try {
    lib = registry.for(want);
  } catch (err) {
    fatal(err.message);
  }
  kv('artifact reports', `${lib.version}  (minor line ${minorOf(lib.version)} — minorOf)`);
  note('the artifact names ITSELF via chs_clickhouse_version() — nothing is');
  note('ever inferred from a directory or file name');

  // The ABI revision closes the gap symbol presence cannot: a symbol proves
  // a function exists, never that its signature matches. The binding's
  // revision is a module constant; the artifact's is a live call. A nonzero
  // disagreement is refused AT LOAD, not discovered mid-call.
  kv('ABI revision (binding)', String(ABI_REVISION));
  kv('ABI revision (artifact)', `${lib.abiRevision}   (0 would mean 'predates the probe')`);
  kv('compile-settings symbol', `${lib.hasCompileSettings()}  (Library.hasCompileSettings)`);

  // Graceful refusal: answering 26.7 semantics out of a 25.8 artifact would
  // be a lie, so an unknown version throws, NAMING what is loaded.
  blank();
  try {
    registry.for('99.9');
    kv('asking for 99.9', '(no error!?)');
  } catch (err) {
    kv('asking for 99.9', `${err.constructor.name}: ${err.message}`);
  }
  note('no nearest-neighbor fallback, ever — a wrong-version answer is a');
  note('wrong answer with a green checkmark on it');

  return { registry, lib };
}

// ---------------------------------------------------------------------------
// SECTION 2 — Ask a build about itself
//
// WHAT: type validation and canonicalisation, straight from this build's own
// DataTypeFactory — plus the widened REFERENCE type and the full type-family
// registry this SDK also exposes.
// WHY: canonicalisation is how you compare a tenant's declared type against
// what the server will actually store — and it is NOT a spelling normaliser,
// it is the server's own parse.
// LOOK FOR: Variant members being SORTED, BIGINT becoming Int64, the error
// for an unknown family carrying ClickHouse's own code 50, and 139 type
// families read from the build itself (no list to maintain).
// C API: chs_validate_type, chs_reference_type, chs_registered_families,
// chs_function_flags — the whole introspection trio, exposed per Library in
// every SDK since the 2026-08-26 parity cycle (docs/reference/bindings.md
// §Introspection).
// ---------------------------------------------------------------------------
function section2(lib) {
  section(2, 'Ask a build about itself');

  kv('validateType', "input -> this build's canonical spelling");
  for (const t of ['Decimal(18,4)', 'Variant(UInt8, String)', 'BIGINT', 'LowCardinality( String )']) {
    kv(`  ${t}`, '-> ' + lib.validateType(t));
  }
  note('Variant members are SORTED; surplus parameters are dropped; the');
  note("space after each comma is the library's own spelling — compare");
  note('canonical strings verbatim, never re-normalise whitespace');
  blank();

  // An unknown family is a typed error carrying ClickHouse's OWN code and
  // message — not a string you have to pattern-match.
  try {
    lib.validateType('NotAType');
  } catch (err) {
    if (!(err instanceof SchemaError)) throw err;
    kv('validateType(NotAType)', `SchemaError code=${err.code}  ${err.detail}`);
    note("code 50 = UNKNOWN_TYPE — the server's own code, from the server's");
    note('own registry. Section 10 is the full error taxonomy.');
  }
  blank();

  // The widened reference type: the second parse every row call already runs
  // to detect silent transformations, exposed as a diagnostic.
  kv('referenceType', 'the widened type transformation findings compare against');
  for (const t of ['UInt8', 'DateTime', 'String']) {
    const ref = lib.referenceType(t);
    kv(`  ${t}`, `-> ${JSON.stringify(ref)}` + (ref === '' ? '   (no wider type exists)' : ''));
  }
  note('UInt8 reparses through Int256, so an overflow wrap is CAUGHT by the');
  note("reference disagreeing; String has no wider type, so ''");
  blank();

  // Every type family in this build's own runtime registry.
  const families = lib.registeredFamilies();
  kv('registeredFamilies', `${families.length} type families (e.g. ${families.slice(0, 3).join(', ')})`);
  note("read from the build itself, so there is no per-release list to");
  note('maintain — this is how chtypes tracks upstream type families');
  blank();

  const flags = lib.functionFlags().split('\n').filter((l) => l !== '');
  kv('functionFlags', `${flags.length} registered functions audited (TSV)`);
  note('the volatility audit behind the statelessness gate — every SDK');
  note('exposes the trio (docs/reference/bindings.md §Introspection)');
}

// ---------------------------------------------------------------------------
// SECTION 3 — Compile a schema and read it back
//
// WHAT: compile a column-declaration list (NOT a CREATE TABLE) and walk the
// compiled columns: canonical types, DEFAULT kinds and expressions.
// WHY: the compiled handle IS the table, as this ClickHouse version would
// create it. Two rewrites below are things no type-string comparison could
// ever catch — the compile is schema-aware.
// LOOK FOR: DEFAULT NULL turning Int64 into Nullable(Int64), an ALIAS column
// whose type is INFERRED, and defaultIsLiteral (which Go does not surface).
// C API: chs_schema_compile, chs_schema_column_count/_name/_type/
// _default_kind/_default_expr/_default_is_literal, chs_schema_free (close).
// ---------------------------------------------------------------------------
function section3(lib) {
  section(3, 'Compile a schema and read it back');

  kv('the DDL', '');
  for (const ddlLine of DEMO_DDL.split(',\n')) raw('      ' + ddlLine);
  withSchema(lib, DEMO_DDL, (schema) => {
    blank();
    kv('compiled columns', 'name  type  (default kind + expression, literal?)');
    for (const c of schema.columns) {
      const extra = c.defaultKind === '' ? '' : `  ${c.defaultKind} ${c.defaultExpr}  literal=${c.defaultIsLiteral}`;
      kv(`  ${c.name}`, c.type + extra);
    }
  });
  note('payload_len is MATERIALIZED: compiled, introspectable, but never');
  note('read from input — watch it come back separately in section 6.');
  note('defaultIsLiteral marks a DEFAULT applicable without the expression');
  note("interpreter (seq's 0: true; ts's now64(3): false)");
  blank();

  // Rewrite 1: a DEFAULT can change the declared TYPE. This is why
  // validateType alone is not enough and a schema-aware compile exists.
  withSchema(lib, 'x Int64 DEFAULT NULL', (schema) => {
    const c = schema.columns[0];
    kv('x Int64 DEFAULT NULL', `compiles as ${c.type} DEFAULT ${c.defaultExpr}`);
  });
  note('the DEFAULT rewrote the type to Nullable — the server does this at');
  note('CREATE, so chtypes must too or every later verdict drifts');

  // Rewrite 2: an ALIAS column's type is inferred from its expression.
  withSchema(lib, 'a UInt8, al ALIAS a + 1', (schema) => {
    kv('a UInt8, al ALIAS a + 1', 'al compiles as ' + schema.columns[1].type);
  });
  note("UInt8 + 1 widens to UInt16, ClickHouse's own inference");
  blank();

  // A failed compile is the same typed error as section 2's.
  kv('x NotAType', classify(() => lib.compileDdl('x NotAType')));
  note(truncate(caught(() => lib.compileDdl('x NotAType')), 90));
}

// ---------------------------------------------------------------------------
// SECTION 4 — Compile under a settings profile
//
// WHAT: the same compile with a DECLARED settings profile fixed into the
// handle — the settings a real server would have had at CREATE TABLE.
// WHY: some settings change the SHAPE of a table (flatten_nested), some gate
// which TYPES may exist (allow_suspicious_low_cardinality_types). A gateway
// discovers a deployment's settings once (section 11) and declares them here;
// the handle then behaves like a table created on THAT server.
// LOOK FOR: one DDL compiling to two different column lists; a type gate
// failing with the server's own 455; a typo'd setting name failing with the
// server's own 115 INCLUDING its did-you-mean hint.
// C API: chs_schema_compile (settings_json + mode arguments).
// ---------------------------------------------------------------------------
function section4(lib) {
  section(4, 'Compile under a settings profile');

  // (a) A compile-SHAPE setting: the same DDL, two storage shapes.
  kv('(a) flatten_nested', 'id UInt32, n Nested(a UInt8, b String)');
  for (const value of ['1', '0']) {
    const schema = lib.compileDdl('id UInt32, n Nested(a UInt8, b String)', {
      settings: { flatten_nested: value },
    });
    kv(`  =${value}`, schema.columns.map((c) => `${c.name} ${c.type}`).join(' | '));
    schema.close();
  }
  note('under 1 (the stock default) the Nested column is stored FLATTENED as');
  note('two Arrays; under 0 it is one Array(Tuple) column. Every downstream');
  note('answer — names, arity, the RowBinary wire — follows the compiled shape.');
  blank();

  // (b) A TYPE GATE, declared: checked ONCE at compile with the server's own
  // code, exactly where a real server checks it (at CREATE).
  kv('(b) a type gate', 'lc LowCardinality(UInt8), allow_suspicious_low_cardinality_types');
  for (const value of ['0', '1']) {
    try {
      const schema = lib.compileDdl('lc LowCardinality(UInt8)', {
        settings: { allow_suspicious_low_cardinality_types: value },
      });
      kv(`  =${value}`, 'compiled: ' + schema.columns[0].type);
      schema.close();
    } catch (err) {
      if (!(err instanceof SchemaError)) throw err;
      kv(`  =${value}`, `REFUSED, the server's own code ${err.code}`);
      note(truncate(err.message, 92));
    }
  }
  blank();

  // (c) A typo in the profile is caught at DECLARE time. The did-you-mean
  // hint is the SERVER'S — chtypes passes it through and invents nothing.
  try {
    lib.compileDdl('a UInt8', { settings: { flatten_nestedd: '1' } });
  } catch (err) {
    if (!(err instanceof SchemaError)) throw err;
    kv('(c) unknown setting name', `flatten_nestedd -> code ${err.code} (UNKNOWN_SETTING)`);
    note(truncate(err.detail, 96));
  }
  blank();

  // (d) The compile MODE. One mode exists (Declared = 0). A binding passes
  // an unrecognized mode THROUGH; the refusal (-2, a decline) is the
  // library's to make — unconditionally, even with no profile.
  kv('(d) compile mode', `CompileMode.Declared = ${CompileMode.Declared}`);
  kv('  mode=0', classify(() => lib.compileDdl('a UInt8', { mode: CompileMode.Declared })));
  kv('  mode=7', classify(() => lib.compileDdl('a UInt8', { mode: 7 })));
  note('a DECLINE (-2), not a rejection: reserved for a future mode');
}

// ---------------------------------------------------------------------------
// SECTION 5 — Accept, reject, decline (and poison)
//
// WHAT: the three verdicts every row lands on — plus the fourth, poisoned,
// which looks like an accept and bites at read time.
// WHY: this is the contract of the whole product. An ingest gateway routes on
// exactly this: accepted -> insert and publish the STORED values; rejected ->
// 400 the producer with the server's own message; unsupported -> chtypes
// refuses to guess, so fall back to the real server (validate cautiously) and
// NEVER convert the decline into an accept or a reject yourself.
// LOOK FOR: the accept carrying a visible coercion (input 256, stored 0);
// the reject carrying ClickHouse's own error text; the decline carrying no
// ClickHouse code at all.
// C API: chs_rows.
// ---------------------------------------------------------------------------
function section5(lib) {
  section(5, 'Accept, reject, decline (and poison)');
  kv('schema', FORMAT_DDL);
  blank();

  withSchema(lib, FORMAT_DDL, (schema) => {
    // ACCEPT — with the coercion made visible. ClickHouse's readIntText
    // wraps integers mod 2^N and reports SUCCESS; chtypes derives the
    // Transform so a gateway can warn the tenant BEFORE the row ships.
    kv('(a) ACCEPT', '{"device_id":1,"seq":256,"label":"ok"}');
    feed(schema, 'JSONEachRow', Format.JSONEachRow, utf8('{"device_id":1,"seq":256,"label":"ok"}'));
    note('accepted — but look at seq: input 256, stored 0. The ~ line is the');
    note('Transform (reason overflow_wrap, LOSSY). Publish the STORED value;');
    note('publishing the payload value is how previews and tables diverge.');
    blank();

    // REJECT — the server's own refusal, code and message verbatim from the
    // vendored ClickHouse code. Nothing to retry; tell the producer.
    kv('(b) REJECT', '{"device_id":"abc"}');
    feed(schema, 'JSONEachRow', Format.JSONEachRow, utf8('{"device_id":"abc"}'));
    note("code 27 and the message are ClickHouse's OWN — chtypes never");
    note('hand-writes an error, so your 400 body matches what a real INSERT');
    note('would have said');
    blank();

    // DECLINE — chtypes refuses to guess. Values falls back to the SQL
    // expression parser for non-literals; evaluating tenant SQL locally is a
    // guess this library will not make: the row is UNSUPPORTED (-2).
    kv('(c) DECLINE', "(2,7,concat('a','b'))  as Values");
    feed(schema, 'Values', Format.Values, utf8("(2,7,concat('a','b'))"));
    note("unsupported means 'a real server MIGHT WELL accept this; I will not");
    note("guess'. Do not 400 the producer (that manufactures an over-reject),");
    note('do not publish (that manufactures an over-accept): send it to the');
    note('real server unpreviewed and let it decide. Both mistake classes are');
    note('budgeted at zero in this repo.');
    blank();
  });

  // POISON — the fourth verdict. The INSERT genuinely succeeds and every
  // later SELECT throws: RowBinaryWithDefaults' marker byte fills an Enum
  // with the raw zero, and Enum8('red'=1,'green'=2) has NO name for 0.
  kv('(d) POISON', 'an accept that bites at read time');
  withSchema(lib, "id UInt32, e Enum8('red' = 1, 'green' = 2)", (schema) => {
    kv('  schema', "id UInt32, e Enum8('red' = 1, 'green' = 2)");
    kv('  payload (RBWD)', RBWD_POISON + '   (id=1 by value, e by marker byte)');
    feed(schema, 'RBWD marker', Format.RowBinaryWithDefaults, unhex(RBWD_POISON));
    note('accepted_poisoned + code 691: the marker fills with the COLUMN-level');
    note('raw zero, and raw 0 has no Enum name. The insert returns success;');
    note('every later SELECT fails. Reported as an ACCEPT variant — never a');
    note('rejection — because the insert really does succeed.');
    kv('  control payload', RBWD_VALUE + '   (e supplied by value = 2)');
    feed(schema, 'RBWD value', Format.RowBinaryWithDefaults, unhex(RBWD_VALUE));
  });
  blank();

  // SKIP — the fifth verdict (2026-08-27), and the only per-row-only one: a
  // batch under input_format_allow_errors_* drops a bad row and continues,
  // with the server's own machinery — and since the itemization cycle, every
  // skip keeps its place in rows with the error IRowInputFormat caught
  // before resyncing. The server logs only a count; chtypes reports what it
  // computed.
  kv('(e) SKIP', 'a bad middle row under input_format_allow_errors_num=10');
  withSchema(lib, FORMAT_DDL, (schema) => {
    const batch = schema.rows(
      Format.JSONEachRow,
      utf8('{"device_id":1,"seq":1,"label":"a"}\n{"device_id":"oops"}\n{"device_id":3,"seq":3,"label":"c"}\n'),
      { input_format_allow_errors_num: '10' },
    );
    kv('  batch', `${batch.outcome}  rows_read=${batch.rowsRead} rows_skipped=${batch.rowsSkipped}`);
    batch.rows.forEach((r, i) => {
      let line = r.outcome;
      if (r.outcome === 'skipped') line += `  code=${r.errCode} ${truncate(r.errMsg, 48)}`;
      kv(`  row ${i}`, line);
    });
    note('one rows() call answers per input record, IN ORDER: accepted (with');
    note('the coerced values) or skipped (with the error that caused it).');
    note('A skipped row is never stored — route on the row outcome; forward');
    note('only survivors, and never send allow_errors to the real INSERT.');
  });
}

// ---------------------------------------------------------------------------
// SECTION 6 — DEFAULT evaluation: where every value comes from
//
// WHAT: one row through the demo table with the clock pinned, then reading
// back WHERE each stored value came from (value.source), which values chtypes
// substituted itself, and which it computed.
// WHY: an INSERT is mostly values the row did NOT supply. A gateway that
// cannot answer "what will the table hold for this column?" cannot preview an
// insert. The volatile-DEFAULT rule is the sharp edge: chtypes resolved
// now64() from ITS clock, so the caller MUST send that column explicitly —
// otherwise the server stamps its own clock and preview != stored, always.
// LOOK FOR: different source values in one row; the substituted warning;
// payload_len under computed (never in values); "bogus" under unknownFields;
// and a skew-budget DECLINE at the end.
// C API: chs_row (via Schema.row), the chtypes_* clock settings.
// ---------------------------------------------------------------------------
function section6(lib) {
  section(6, 'DEFAULT evaluation: where every value comes from');

  withSchema(lib, DEMO_DDL, (schema) => {
    const rowText = '{"device_id":42,"seq":256,"payload":"hello","grade":"a","bogus":1}';
    kv('row fed', rowText);
    kv('clock pinned', `chtypes_now_epoch_nanos=${PINNED_CLOCK}  (2023-11-14 22:13:20 UTC)`);
    note('ts and label are OMITTED on purpose; bogus matches no column');
    const r = schema.row(Format.JSONEachRow, utf8(rowText), { chtypes_now_epoch_nanos: PINNED_CLOCK });
    blank();

    kv('outcome / errCode', `${r.outcome} / ${r.errCode}`);
    kv('values[]  (the stored row)', 'column = stored text  (source)');
    for (const v of r.values) kv(`  ${v.column}`, `${textOr(v).padEnd(28)} (${v.source})`);
    note('source values: input (the row supplied it), default (a DEFAULT');
    note("expression evaluated through ClickHouse's own CAST path),");
    note('default_substituted (a VOLATILE default resolved from the pinned');
    note('clock), absent (no DEFAULT: the type\'s own zero). text is');
    note("ClickHouse's OWN JSON rendering — never re-serialized here, because");
    note('18446744073709551615 through a double comes back ...552000.');

    blank();
    kv('substituted[]', 'volatile DEFAULTs chtypes resolved from ITS clock');
    for (const s of r.substituted) kv(`  ${s.column}`, `${s.expr}  ->  ${s.text}`);
    note('SEND THESE AS EXPLICIT COLUMNS IN THE REAL INSERT. If the server');
    note('evaluates now64() itself, preview and stored differ every time —');
    note('ClickHouse reads the clock once per BLOCK, not once per statement.');

    blank();
    kv('computed[]', 'MATERIALIZED values — durable, but never in SELECT *');
    for (const c of r.computed) kv(`  ${c.column}`, `${c.kind}  =  ${c.text}`);
    note('reported separately from values so the preview matches what a');
    note('subscriber reading the table will actually see');

    blank();
    kv('transformed[]', 'every silent change, with a machine-readable reason');
    for (const t of r.transformed) {
      kv(`  ${t.reason}`, `${t.column}: ${t.input || '(absent)'} -> ${t.stored}   lossy=${t.lossy}`);
    }
    note('lossy is false for exactly four reasons (reformat, default_filled,');
    note('zero_filled, default_materialised) and true for everything else');

    blank();
    kv('unknownFields[]', JSON.stringify(r.unknownFields));
    kv('unsupportedSettings[]', JSON.stringify(r.unsupportedSettings));
    note('unknown fields are reported, not judged — whether to 400 on them is');
    note('gateway policy. A non-empty unsupportedSettings promotes the row to');
    note('unsupported: a declined setting must never score as agreement.');
    blank();

    // A DEFAULT can read OTHER columns of the same row.
    withSchema(lib, 'a UInt8, d UInt8 DEFAULT a + 1', (dep) => {
      kv('row-dependent DEFAULT', 'a UInt8, d UInt8 DEFAULT a + 1   fed CSV `7,`');
      feed(dep, 'CSV', Format.CSV, utf8('7,'));
      note('the bare empty CSV field takes the DEFAULT, and the DEFAULT reads');
      note('a=7 from the same row — d stores 8, exactly as the server computes it');
    });
    blank();

    // The clock-skew budget: the one place a DEFAULT becomes a DECLINE. Past
    // the budget the only safe answer is "unsupported" — a substituted
    // timestamp too far in the past under a TTL is silently deleted at merge
    // time, with no error at any point.
    const r2 = schema.row(Format.JSONEachRow, utf8('{"device_id":1,"seq":1,"payload":"x","grade":"b"}'), {
      chtypes_clock_offset_nanos: '5000000000',
      chtypes_max_clock_skew_nanos: '1',
    });
    kv('skew budget decline', 'offset=5s, budget=1ns');
    kv('  outcome / errCode', `${r2.outcome} / ${r2.errCode}`);
    for (const v of r2.values) if (v.column === 'ts') kv('  ts.source', v.source);
    note('default_volatile_unresolved: chtypes refuses to substitute a');
    note('volatile DEFAULT when the measured clock offset exceeds the');
    note("caller's budget (chtypes_max_clock_skew_nanos)");
  });
}

// ---------------------------------------------------------------------------
// SECTION 7 — One schema, every format
//
// WHAT: the same three-column schema fed in all ten chs_format encodings —
// accept and reject for each text format, then the binary tier with
// hand-built bytes.
// WHY: format is not cosmetic. Each format has signature behaviors (CSV's
// bare-vs-quoted empty field, RBWD's marker byte, Native's silent CAST,
// Buffers' silent reinterpret) that change what the table ends up holding.
// LOOK FOR: the same logical row giving format-specific verdicts, and the
// byte-level payloads in the comments — every binary payload is explained.
// C API: chs_rows with format codes 0..9 (frozen integers; formatName maps
// them back to ClickHouse's spelling).
// ---------------------------------------------------------------------------
function section7(lib, registry) {
  section(7, 'One schema, every format');
  kv('schema', FORMAT_DDL);
  kv('formatName(5)', `${formatName(Format.RowBinary)}   (codes are the frozen ABI integers 0..9)`);
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
    note('Values has an explicit DEFAULT keyword; an SQL expression is a');
    note('DECLINE (section 5c) — evaluated by a real server, guessed by nobody');
    blank();

    // CSV's signature rule deserves its own two lines.
    kv('CSV empty-field rule', "bare empty takes the DEFAULT; quoted empty is ''");
    feed(schema, 'CSV   bare   2,7,', Format.CSV, utf8('2,7,'));
    feed(schema, 'CSV   quoted 3,7,""', Format.CSV, utf8('3,7,""'));
    blank();

    group('binary formats (bytes, COUNTED — never NUL-terminated)');
    kv('  RowBinary payload', ROW_BINARY_OK);
    feed(schema, 'RowBinary    accept', Format.RowBinary, unhex(ROW_BINARY_OK));
    note('4-byte LE UInt32 (1), 1-byte UInt8 (7), varint-length String ("ok")');
    note('— no framing, no names, no self-description');
    feed(schema, 'RowBinary    reject', Format.RowBinary, new Uint8Array([0x01, 0x00]));
    note('truncated mid-row: framing faults are all-or-nothing per batch');
    kv('  RBWD payload', RBWD_MARKER);
    feed(schema, 'RBWD  marker byte', Format.RowBinaryWithDefaults, unhex(RBWD_MARKER));
    note('RowBinaryWithDefaults prefixes each column with a marker byte:');
    note("00 = 'value follows', nonzero = 'compute the DEFAULT, read no value");
    note("bytes'. Above, seq's marker is 01 -> stored 7 (its DEFAULT).");
    kv('  RBWNTD payload', '(hand-built: LEB128 count, names, types, then a marker row)');
    feed(schema, 'RBWNTD       accept', Format.RowBinaryWithNamesAndTypesAndDefaults, unhex(RBWNTD_ROW));
    note('format 7 arrives with ClickHouse 26.x — on an older artifact the line');
    note("above is the server's own 73 UNKNOWN_FORMAT, not a chtypes error.");
    note('Section 12 turns exactly this into the version-pinning lesson.');
    blank();

    group('Native: self-describing, and it CASTs');
    feed(schema, 'Native       accept', Format.Native, unhex(NATIVE_OK));
    note('a Native block declares its OWN column names and types (captured');
    note('from a live 25.8 SELECT ... FORMAT Native)');
    feed(schema, 'Native       CAST', Format.Native, unhex(NATIVE_CAST));
    note('this block declares seq as String "200" while the table says UInt8:');
    note('the disagreement is CAST silently (input_format_native_allow_types_');
    note('conversion defaults to true on every vendored era) — visible here as');
    note('a Transform, invisible on a real server');
  });
  blank();

  group('Buffers: NO self-description at all');
  const newest = registry.for(newestLine(registry.versions()));
  withSchema(newest, 'x Int32', (schema) => {
    kv('  artifact', `${newest.minor}  (Buffers arrives at 26.5; this block uses the newest held)`);
    kv('  payload', '4 bytes ff ff ff ff, declared x Int32');
    feed(schema, 'Buffers reinterpret', Format.Buffers, unhex(BUFFERS_OK));
    note("a UInt32 producer's 4294967295 reads back as -1: same width, no");
    note('metadata, so no check CAN fire. The over-accept class in a format');
    note('that cannot detect it — the schema is entirely out of band.');
    feed(schema, 'Buffers width', Format.Buffers, unhex(BUFFERS_WIDE));
    note('the same 4 bytes declared 8 wide IS caught: size accounting disagrees');
  });
}

// ---------------------------------------------------------------------------
// SECTION 8 — Engines, MergeTree settings, and TTL
//
// WHAT: declare the table's engine and TTL, then watch the STORAGE layer
// change what a batch stores — including storing nothing at all.
// WHY: a row can be accepted per row and absent per batch. SummingMergeTree
// folds rows at insert; a TTL already in the past deletes them at merge, with
// no error at any point. A gateway reading only per-row verdicts previews
// rows the table will never hold.
// LOOK FOR: engineRows (the stored truth) being SHORTER than the input; the
// TTL batch whose row is accepted and whose engineRows is empty; and the
// refusal-vs-decline pair on MergeTree settings (the sign of the ABI return
// decides which).
// C API: chs_schema_engine, chs_schema_ttl, chs_rows.
// ---------------------------------------------------------------------------
function section8(lib) {
  section(8, 'Engines, MergeTree settings, and TTL');

  // (a) A specialised engine changes what the table STORES.
  withSchema(lib, 'day Date, key UInt32, v UInt64', (schema) => {
    kv('(a) setEngine', 'SummingMergeTree ORDER BY (day, key)');
    schema.setEngine('SummingMergeTree', '(day, key)');
    const body = utf8('{"day":"2026-01-01","key":1,"v":5}\n{"day":"2026-01-01","key":1,"v":7}');
    const batch = schema.rows(Format.JSONEachRow, body);
    kv('  rows in / rowsRead', `2 / ${batch.rowsRead}   (v=5 and v=7, same key)`);
    kv('  engineRows (stored)', `[${(batch.engineRows ?? []).join(', ')}]`);
  });
  note('two rows in, ONE row out, v summed — engineRows is the post-merge');
  note('preview and, when present, the truth to believe over rows');
  blank();

  // (b) MergeTree-namespace settings: two failures, two KINDS. The sign of
  // the ABI return decides — a positive code is the SERVER refusing, a
  // negative one is this LIBRARY declining. Never flatten them.
  kv('(b) MergeTree settings', 'refusal vs decline vs inert');
  withSchema(lib, 'a UInt8', (s) => {
    kv('  unknown NAME', 'index_granularityy -> ' + classify(
      () => s.setEngine('MergeTree', 'tuple()', { mergeTreeSettings: { index_granularityy: '8192' } })));
    note("the server's own 115: this DDL can never exist — tell the tenant");
  });
  withSchema(lib, 'a UInt8', (s) => {
    kv('  known, non-default', 'index_granularity=4096 -> ' + classify(
      () => s.setEngine('MergeTree', 'tuple()', { mergeTreeSettings: { index_granularity: '4096' } })));
    note("a DECLINE: no MergeTree setting's behavior is modeled yet, and");
    note('silently ignoring a declared value would fake the profile being in');
    note('force. A real server might well accept it — validate cautiously.');
  });
  withSchema(lib, 'a UInt8', (s) => {
    kv('  known, AT default', 'index_granularity=8192 -> ' + classify(
      () => s.setEngine('MergeTree', 'tuple()', { mergeTreeSettings: { index_granularity: '8192' } })) + ' (inert)');
  });
  blank();

  // (c) TTL: accepted per row, gone per batch.
  withSchema(lib, 'ts DateTime, v UInt8', (schema) => {
    schema.setEngine('MergeTree', 'ts');
    schema.setTtl('ts + INTERVAL 1 DAY');
    kv('(c) setTtl', 'MergeTree ORDER BY ts, TTL ts + INTERVAL 1 DAY');
    const batch = schema.rows(Format.JSONEachRow, utf8('{"ts":"2020-01-01 00:00:00","v":9}'), {
      chtypes_now_epoch_nanos: PINNED_CLOCK,
    });
    kv('  row fed', '{"ts":"2020-01-01 00:00:00","v":9}  with the clock pinned to 2023');
    kv('  row-level outcome', `${batch.rows[0].outcome}   <- the row PARSED fine`);
    const engineRows = batch.engineRows ?? [];
    kv('  engineRows (stored)', `[${engineRows.join(', ')}]  (length ${engineRows.length})`);
    for (const t of batch.transformed) {
      kv('  batch transform', `row=${t.row} column=${JSON.stringify(t.column)} reason=${t.reason} lossy=${t.lossy}`);
    }
    note('accepted per ROW, stored nowhere per BATCH: the 2020 timestamp is');
    note('already past the TTL, so the part holds nothing. On a real server');
    note('this is a silent merge-time delete — the ttl_expired transform is');
    note('the only warning anyone gets.');
    blank();

    // (d) A TTL this library will not guess at.
    kv('(d) setTtl now()+1 DAY', classify(() => schema.setTtl('now() + INTERVAL 1 DAY')));
    note(truncate(caught(() => schema.setTtl('now() + INTERVAL 1 DAY')), 90));
    note('a clock-reading TTL is DECLINED, not guessed');
  });
}

// ---------------------------------------------------------------------------
// SECTION 9 — Settings precedence: who wins
//
// WHAT: the same row and the same handle, answered differently as settings
// are supplied at each layer:
//
//     per-call  >  handle profile  >  library defaults  >  ClickHouse's own
//
// WHY: this is how a gateway declares a deployment's settings ONCE (at
// compile) yet still lets one INSERT override per call. If precedence were
// fuzzy, the declared profile would not actually be in force.
// LOOK FOR: layers 3 vs 4 — the SAME handle, the SAME bytes, passing under
// the handle profile and failing the moment a per-call value overrides it.
// Then the library-defaults layer measured via setDefaultSettings, including
// a WHOLESALE refusal that commits nothing.
// C API: chs_rows (settings_json), chs_set_default_settings.
// TS note: setDefaultSettings lives on the dlopen'd Library itself (Go's
// Registry path structurally cannot make this call; only its static cgo path
// can). Settings values are string | bigint — a JS number is a RUNTIME error,
// because a 19-digit epoch does not survive an IEEE double.
// ---------------------------------------------------------------------------
function section9(lib) {
  section(9, 'Settings precedence: who wins');
  kv('the probe', 'ts DateTime  fed  {"ts":"2026-01-15T10:30:00Z"}');
  note("stock ClickHouse parses 'basic' datetimes only; best_effort accepts");
  note('ISO-8601 — so the verdict TELLS you which setting value won');
  blank();

  const iso = utf8('{"ts":"2026-01-15T10:30:00Z"}');
  const basic = { date_time_input_format: 'basic' };
  const bestEffort = { date_time_input_format: 'best_effort' };
  kv('artifact', `${lib.version}  (the selected version; Go's tour uses its static path here)`);

  const plain = lib.compileDdl('ts DateTime');
  const profiled = lib.compileDdl('ts DateTime', { settings: bestEffort });
  try {
    kv('1. ClickHouse defaults', verdict(plain.rows(Format.JSONEachRow, iso)));
    note('nothing declared anywhere — the stock default decides (it rejected');
    note('ISO-8601 through 25.10 and accepts it from 26.5)');
    kv('2.  + per-call basic', verdict(plain.rows(Format.JSONEachRow, iso, basic)));
    kv('3. handle profile best_effort', verdict(profiled.rows(Format.JSONEachRow, iso)));
    note('the profile declared at COMPILE reaches every later row call');
    kv('4.  + per-call basic', verdict(profiled.rows(Format.JSONEachRow, iso, basic)));
    note('3 vs 4 is the requirement, measured: the same row PASSES under the');
    note('handle profile and FAILS when the per-call value overrides it');
    blank();

    // The library-defaults layer, and its two safety properties: the seed is
    // REPLACED wholesale on every call, and a payload with any unknown name
    // is refused wholesale — nothing committed.
    kv('setDefaultSettings', 'the library-defaults layer, measured');
    lib.setDefaultSettings(bestEffort);
    kv('5. seed best_effort', verdict(plain.rows(Format.JSONEachRow, iso)));
    lib.setDefaultSettings(basic);
    kv('6. seed basic', verdict(plain.rows(Format.JSONEachRow, iso)));
    note("each call REPLACES the whole seed — 5's value did not linger");
    kv('7. seed basic + per-call best_effort', verdict(plain.rows(Format.JSONEachRow, iso, bestEffort)));
    note('per-call still outranks the seed');
    try {
      lib.setDefaultSettings({ made_up_setting_xyz: '1' });
      kv('8. seed an unknown name', '(no error!?)');
    } catch (err) {
      kv('8. seed an unknown name', `${err.constructor.name}: ${truncate(err.message, 88)}`);
    }
    kv('   verdict after refusal', verdict(plain.rows(Format.JSONEachRow, iso)) + '   <- unchanged: NOTHING was committed');
    note("the 115 and its message are the server's own; a gateway can never");
    note('believe a default profile is in force when part of it never applied');
    lib.setDefaultSettings({});
    kv('9. seed {} (reset)', verdict(plain.rows(Format.JSONEachRow, iso)));
  } finally {
    plain.close();
    profiled.close();
  }
}

// ---------------------------------------------------------------------------
// SECTION 10 — The error taxonomy
//
// WHAT: every kind of answer this SDK gives, told apart BY TYPE — never by
// string matching.
// WHY: the three kinds demand three different reactions (tell the tenant /
// fall back cautiously / fix the deployment), and the TS classes are
// deliberately PEERS under ChtypesError: a decline can never satisfy
// `instanceof SchemaError`, so a caller handling only refusals cannot
// silently convert declines into rejections.
// LOOK FOR: the same instanceof chain you would write in production, and the
// reminder that ROW verdicts are data (result.outcome), not thrown.
// ---------------------------------------------------------------------------
function section10(lib) {
  section(10, 'The error taxonomy');

  kv('the TS idiom', 'instanceof against PEER classes (all extend ChtypesError)');
  raw('      try {');
  raw('        schema.setEngine(engine, orderBy);');
  raw('      } catch (err) {');
  raw('        if (err instanceof UnsupportedError) return validateCautiously(err);');
  raw('        if (err instanceof SchemaError) return rejectWith(err.code, err.detail);');
  raw('        if (err instanceof RegistryError) return fixDeployment(err);');
  raw('        throw err;');
  raw('      }');
  blank();

  // A REFUSAL: ClickHouse's own code rides on SchemaError.
  describeError(() => lib.compileDdl('x NotAType'), 'compile x NotAType');
  // A DECLINE: UnsupportedError — no .code property, by design.
  withSchema(lib, 'ts DateTime, v UInt8', (schema) => {
    schema.setEngine('MergeTree', 'ts');
    describeError(() => schema.setTtl('now() + INTERVAL 1 DAY'), 'setTtl now()+1 DAY');
  });
  // A registry miss: RegistryError (a deployment problem, not a verdict).
  describeError(() => new Registry('/nonexistent-registry'), "new Registry('/nonexistent-registry')");
  blank();

  kv('UnsupportedError has .code?', `${'code' in UnsupportedError.prototype}   <- no such property, by design`);
  kv('peers, not a hierarchy', `UnsupportedError instanceof-compatible with SchemaError: ${UnsupportedError.prototype instanceof SchemaError}`);
  kv('row verdicts are DATA', 'result.outcome, not a thrown error');
  note('a row the server would reject RETURNS (outcome "rejected", errCode =');
  note("the server's code) — nothing throws. Exceptions are for questions");
  note('that could not be asked; verdicts live in the answer.');
  kv('CODE_UNSUPPORTED', `${CODE_UNSUPPORTED}  (the wire sentinel; never a real ClickHouse code)`);
}

// ---------------------------------------------------------------------------
// SECTION 11 — The discovery kit, offline
//
// WHAT: the three canonical queries chtypes ships for learning who a
// deployment is, their typed parsers, and reconstructDdl — run here against
// CANNED bytes shaped exactly like a real server's JSONEachRow responses.
// WHY: chtypes NEVER opens a socket. You run these queries with whatever
// client you already have; the kit gives you the SQL and parses the results.
// The payoff is the last step: the server's own version string resolves an
// artifact, and the discovered settings become the compile profile — so the
// handle behaves like a table created on THAT deployment.
// LOOK FOR: the reconstructed DDL (backticks where needed, DEFAULTs carried),
// and the SAME ROW accepted under the discovered profile but rejected under a
// stock compile — the measurable reason discovery matters.
// C API: none until the compile at the end — the kit is pure client-side.
// (The ONLINE version of this flow, against a real server, is
// go/ingest-demo/ — the optional demo chplay.sh never runs.)
// ---------------------------------------------------------------------------
function section11(registry) {
  section(11, 'The discovery kit, offline');
  kv('NOTE', 'responses below are CANNED — shaped exactly like a real');
  kv('', "server's, so the parsers cannot tell. Swap in your HTTP client.");
  blank();

  // Query 1: who are you? (version)
  kv('QUERY_SERVER_VERSION', QUERY_SERVER_VERSION);
  kv('  canned response', CANNED_VERSION_RESULT.trim());
  const version = parseVersionResult(utf8(CANNED_VERSION_RESULT));
  kv('  parsed', version);
  blank();

  // Query 2: which settings did this deployment change from stock?
  kv('QUERY_CHANGED_SETTINGS', QUERY_CHANGED_SETTINGS);
  for (const cannedLine of CANNED_SETTINGS_RESULT.trim().split('\n')) kv('  canned response', cannedLine);
  const settings = parseChangedSettingsResult(utf8(CANNED_SETTINGS_RESULT));
  kv('  parsed', `${Object.keys(settings).length} changed settings -> the compile profile`);
  blank();

  // Query 3: what does the table look like AS STORED?
  kv('QUERY_TABLE_COLUMNS', '(system.columns for one table; see the constant)');
  const cols = parseColumnsResult(utf8(CANNED_COLUMNS_RESULT));
  for (const col of cols) {
    const kind = col.defaultKind ? `  ${col.defaultKind} ${col.defaultExpression}` : '';
    kv(`  [${col.position}] ${col.name}`, col.type + kind);
  }
  note('default_kind/default_expression are CARRIED — dropping them would');
  note('silently lose the DEFAULT semantics sections 6 and 8 run on');
  const ddl = reconstructDdl(cols);
  kv('  reconstructDdl', ddl);
  note('`reading c` came back BACKTICKED — identifiers are quoted exactly');
  note('where ClickHouse requires it');
  blank();

  // The payoff: version -> artifact, settings -> profile, and a measurable
  // difference the discovered profile makes.
  let lib;
  try {
    lib = registry.for(version);
  } catch (err) {
    kv(`registry.for(${version})`, err.message);
    note('no artifact for this line — `scripts/fetch.sh 25.8` would add it; the');
    note('rest of this section needs it and is skipped');
    return;
  }
  kv(`registry.for(${version})`, `artifact ${lib.version}  (exact patch -> the ${lib.minor} line)`);
  const row = utf8('{"ts":"2026-01-15T10:30:00Z","device_id":9,"reading c":21.5}');
  kv('the same row, twice', '{"ts":"2026-01-15T10:30:00Z","device_id":9,"reading c":21.5}');
  const profiled = lib.compileDdl(ddl, { settings });
  try {
    kv('  under the discovered profile', verdict(profiled.rows(Format.JSONEachRow, row)));
  } finally {
    profiled.close();
  }
  withSchema(lib, ddl, (plain) => {
    kv('  under a stock compile', verdict(plain.rows(Format.JSONEachRow, row)));
  });
  note('the deployment declared date_time_input_format=best_effort, so ITS');
  note('server takes the ISO-8601 timestamp — a stock compile answers for a');
  note('server the tenant does not have. Discovery is what closes that gap.');
}

// ---------------------------------------------------------------------------
// SECTION 12 — Version pinning: same input, different answers
//
// WHAT: the same DDL and the same bytes, swept across every artifact resident
// in this process.
// WHY: version differences are the reason the registry exists. They are not
// monotonic — newer is NOT always more permissive — so no rule can predict
// them; only the real per-version artifact can answer.
// LOOK FOR: 25.10 rejecting a DEFAULT that both 25.8 and 26.5 accept; and the
// Buffers format simply not existing before 26.5 (the server's own 73).
// ---------------------------------------------------------------------------
function section12(registry) {
  section(12, 'Version pinning: same input, different answers');
  const versions = registry.versions();
  if (versions.length < 2) {
    kv('versions resident', versions.join('  '));
    note('only one artifact is built, so there is nothing to sweep — the');
    note('point of this section needs at least two. Build another line');
    note('(e.g. `scripts/fetch.sh 26.7`) and re-run to see the answers diverge.');
    return;
  }

  kv('(a) a mixed-type DEFAULT', "a UInt8, x Int64 DEFAULT if(1,2,'a')");
  for (const v of versions) {
    const lib = registry.for(v);
    try {
      const schema = lib.compileDdl("a UInt8, x Int64 DEFAULT if(1,2,'a')");
      const col = schema.columns[1];
      kv(`  ${v}`, `compiled  (${col.type} DEFAULT ${col.defaultExpr})`);
      schema.close();
    } catch (err) {
      if (!(err instanceof SchemaError)) throw err;
      kv(`  ${v}`, `REJECTED code ${err.code}  ${truncate(err.detail, 52)}`);
    }
  }
  note('NEWER IS NOT ALWAYS MORE PERMISSIVE — no monotonic rule predicts');
  note('this, which is exactly why one real artifact per line exists');
  blank();

  kv("(b) a format's arrival", 'Buffers (code 9), added in ClickHouse 26.5');
  kv('  payload', '1 column, 1 row, 4 bytes ff ff ff ff, declared x Int32');
  for (const v of versions) {
    const lib = registry.for(v);
    withSchema(lib, 'x Int32', (schema) => {
      const batch = schema.rows(Format.Buffers, unhex(BUFFERS_OK));
      if (batch.outcome === 'accepted' && batch.rows.length) {
        kv(`  ${v}`, `accepted  x = ${batch.rows[0].values[0].text}`);
      } else {
        kv(`  ${v}`, `${batch.outcome}  code=${batch.errCode}  ${truncate(batch.errMsg, 40)}`);
      }
    });
  }
  note("73 UNKNOWN_FORMAT is the SERVER'S own answer on the older lines —");
  note('probe the artifact (one payload through rows) instead of trusting');
  note('your own version arithmetic');
}

// ---------------------------------------------------------------------------
// SECTION 13 — Teardown
//
// WHAT: what to release, and when.
// WHY: schema handles are C allocations (chs_schema_free via close(), or
// `using` — Schema has Symbol.dispose). Process teardown is chs_shutdown,
// which TS exposes as registry.close() (Registry has Symbol.dispose too).
// nativeStats shows the string-ownership discipline in action: every string
// the C side malloc'd was freed with the same library's chs_free.
// C API: chs_schema_free, chs_shutdown, chs_free.
// ---------------------------------------------------------------------------
function section13(registry) {
  section(13, 'Teardown');
  kv('schema handles', 'close() each schema, or `using s = lib.compileDdl(...)` (chs_schema_free)');
  const stats = nativeStats();
  kv('nativeStats before close', `stringsTaken=${stats.stringsTaken} stringsFreed=${stats.stringsFreed}`);
  note('every chs_* string is malloc-owned by the artifact that returned it');
  note("and freed with THAT artifact's chs_free — the counters must match");
  kv('registry.close()', 'chs_shutdown for every loaded library (Symbol.dispose works too)');
  kv('  when', 'this tour runs it at the very END (sections 15-16 still need');
  kv('', 'the libraries); teardown is the LAST thing a process does with a');
  kv('', 'registry — reopening a closed artifact is not a promised operation');
  note("chs_init also registers chs_shutdown with atexit(), so an ordinary");
  note('process would be fine without this — close() is for callers that');
  note("control their own teardown order. Go's Registry deliberately has NO");
  note('teardown (it never dlcloses; docs/reference/bindings.md §Teardown); Python has');
  note('close() + context manager; Rust has Registry::shutdown() and Drop.');
}

// ---------------------------------------------------------------------------
// SECTION 14 — The static path (Go only)
//
// Go's cgo build can additionally LINK one artifact directly (no dlopen) and
// use it through package-level functions; that is also the only place Go
// exposes SetDefaultSettings and RegisteredFamilies. TypeScript cannot have
// that shape: ffi-rs always dlopens, so Registry is the only loader here —
// and it is the product path anyway. docs/reference/bindings.md makes the static shape
// explicitly optional. See go/main.go section 14 for the real thing.
// ---------------------------------------------------------------------------
function section14() {
  section(14, 'The static path (Go only)');
  kv('not offered in TypeScript', 'ffi-rs always dlopens; Registry is the only loader');
  note('see go/main.go section 14 — docs/reference/bindings.md §The object model makes');
  note('the statically-linked single-version shape explicitly optional');
}

// ---------------------------------------------------------------------------
// SECTION 15 — Export: bytes + spans
//
// WHAT: the SAME chs_rows call that judges a batch can also serialize its
// accepted rows to wire bytes (JSONCompactEachRow this revision), addressed
// per row by index-aligned spans — rows(..., { exportFormat }), one C call.
// WHY: every consumer of an accepted row wants the stored bytes ready to
// publish or INSERT without rebuilding them from the document — reassembly
// is where caller bugs live (the invalid-JSON-on-poisoned-rows class).
// LOOK FOR: the skipped row's {0,0} span; a span slice BEING the row's line;
// the poisoned batch DECLINING the export and saying why; emitted-EMPTY (an
// answer) vs declined (not one); and the lean document (docFlags 0 — the
// default when an export is requested) keeping every verdict.
// C API: chs_rows with export_format/doc_flags/out_bytes (ABI revision 3).
// ---------------------------------------------------------------------------
function section15(lib) {
  section(15, 'Export: bytes + spans');
  const schema = lib.compileDdl(FORMAT_DDL);
  const body = utf8('{"device_id":1,"label":"ok"}\n{"device_id":"zap"}\n{"device_id":3,"label":"hi"}\n');
  let batch;
  try {
    batch = schema.rows(Format.JSONEachRow, body, { input_format_allow_errors_num: '10' },
      { exportFormat: Format.JSONCompactEachRow, docFlags: DOC_ALL });
  } catch (err) {
    schema.close();
    if (err instanceof UnsupportedError) {
      note('this artifact predates the export surface (relink it to ABI');
      note('revision 3, `just refresh <version>`); the section degrades here');
      note(`rather than failing the tour: ${truncate(String(err), 48)}`);
      return;
    }
    throw err;
  }
  if (batch.outcome === 'unsupported') {
    kv('rows({exportFormat})', `declined: ${truncate(batch.errMsg, 64)}`);
    note('this artifact answers the export request unsupported; relink the');
    note('fleet (`just refresh`) to see the live bytes. Degrading.');
    schema.close();
    return;
  }
  kv('batch', `${batch.outcome}  rows_read=${batch.rowsRead} rows_skipped=${batch.rowsSkipped}`);
  kv('payload', `${JSON.stringify(payloadText(batch.payload))}  (${batch.payload.length} bytes, one line per ACCEPTED row)`);
  note('wire order = declared minus MATERIALIZED/ALIAS/EPHEMERAL, so these');
  note('bytes are directly INSERT-able with no column list; DEFAULTs (seq=7,');
  note("label='unknown') are already applied — preview == stored");
  batch.spans.forEach((span, i) => {
    const line = span.len
      ? JSON.stringify(payloadText(batch.payload.subarray(span.off, span.off + span.len)))
      : '(no bytes — row not accepted)';
    kv(`  span[${i}] {off:${span.off} len:${span.len}}`, `${batch.rows[i].outcome} -> ${line}`);
  });
  note('spans are INDEX-ALIGNED with rows; slicing spans out of the payload');
  note('IS the per-row payload, and concatenating non-zero spans reproduces');
  note('it exactly — batches merge by byte concatenation');
  blank();

  // The lean document: an export without docFlags defaults to 0 — every
  // verdict, none of the description, same bytes.
  const lean = schema.rows(Format.JSONEachRow, body, { input_format_allow_errors_num: '10' },
    { exportFormat: Format.JSONCompactEachRow });
  kv('lean (docFlags 0)',
    `outcome ${lean.outcome}, ${lean.rows.length} verdict rows, ${lean.rows[0].values.length} values, ` +
    `${lean.transformed.length} transformed — payload identical: ${Buffer.compare(lean.payload, batch.payload) === 0}`);
  note('flags thin the DESCRIPTION, never the VERDICT; DOC_VALUES /');
  note('DOC_TRANSFORMS / DOC_DEFAULTS pick groups a la carte');
  blank();

  // Fail-closed: a poisoned batch holds a value ClickHouse itself cannot
  // read back — no writer can honestly serialize it, so no bytes.
  const poisonSchema = lib.compileDdl("e Enum8('a' = 1, 'b' = 2)");
  const poi = poisonSchema.rows(Format.JSONEachRow, utf8('{"e":null}\n'),
    { input_format_defaults_for_omitted_fields: '0' },
    { exportFormat: Format.JSONCompactEachRow, docFlags: DOC_ALL });
  poisonSchema.close();
  const payloadDesc = poi.payload === undefined ? 'undefined (declined)' : `${poi.payload.length} bytes`;
  kv('poisoned batch',
    `${poi.outcome} -> payload=${payloadDesc} exportDeclined=${JSON.stringify(truncate(poi.exportDeclined ?? '', 48))}`);

  // Emitted-empty is an ANSWER (zero accepted rows), not a decline.
  const emp = schema.rows(Format.JSONEachRow, new Uint8Array(0), undefined,
    { exportFormat: Format.JSONCompactEachRow });
  kv('empty batch',
    `payload defined=${emp.payload !== undefined} len=${emp.payload.length}  (emitted-empty != declined)`);
  schema.close();
}

// ---------------------------------------------------------------------------
// SECTION 16 — Filters: WHERE semantics at the edge
//
// WHAT: compile one boolean expression against a schema (compileFilter) and
// evaluate it per row of a body — ClickHouse's own comparison functions, so
// the answers are WHERE-side by construction.
// WHY: read-side row visibility (who may SEE this row) is a WHERE question,
// and WHERE coercion is NOT insert coercion: `x = 256` over UInt8 PROMOTES
// (false for every row) where an insert would wrap 256 to 0. Reusing the
// insert answer would silently match every legitimate zero.
// LOOK FOR: 'f','f' where the insert path stores 0; NULL being not-true; the
// 'e' class (compiles, then THROWS per row — a server fails the WHOLE query
// here); clock reads and {p:Type} parameters REFUSED at compile, never
// guessed; and the enforcement gate at the end.
// C API: chs_filter_compile / chs_filter_rows / chs_filter_free (revision 3).
// ---------------------------------------------------------------------------
function section16(lib) {
  section(16, 'Filters: WHERE semantics at the edge');
  const schema = lib.compileDdl('x UInt8');
  let filt;
  try {
    filt = schema.compileFilter('x = 256');
  } catch (err) {
    schema.close();
    if (err instanceof UnsupportedError) {
      note('this artifact predates the filter surface (relink it to ABI');
      note('revision 3, `just refresh <version>`); the section degrades here');
      note(`rather than failing the tour: ${truncate(String(err), 48)}`);
      return;
    }
    throw err;
  }
  {
    const fr = filt.rows(Format.JSONEachRow, utf8('{"x":0}\n{"x":255}\n'));
    filt.close();
    kv('filter `x = 256` over UInt8', `verdicts ${verdictString(fr)}`);
    note('PROMOTES, never wraps: false for x=0 AND x=255. The insert side of');
    note("this same library stores 256 as 0 (section 5's overflow_wrap) —");
    note('which is why predicate constants must never be folded through');
    note('insert coercion (docs/reference/bindings.md §Constants are not payloads)');
  }
  blank();

  // NULL is not true — three-valued logic collapsed at the WHERE boundary.
  {
    const nullableSchema = lib.compileDdl('lvl Nullable(UInt8)');
    const nf = nullableSchema.compileFilter('lvl = 1');
    const fr = nf.rows(Format.JSONEachRow, utf8('{"lvl":null}\n{"lvl":1}\n'));
    nullableSchema.close(); // closes the open filter FIRST, then the schema
    kv('`lvl = 1` on [null, 1]', `verdicts ${verdictString(fr)}   (NULL is not true, as WHERE hides it)`);
  }
  blank();

  // The 'e' class: compiles clean, then THROWS on every row's values.
  {
    const strSchema = lib.compileDdl('s String');
    const sf = strSchema.compileFilter('s = 257');
    const fr = sf.rows(Format.JSONEachRow, utf8('{"s":"hi"}\n'));
    strSchema.close();
    kv('`s = 257` over String', `verdicts ${verdictString(fr)}`);
    for (const fe of fr.errors) {
      kv(`  row ${fe.row}`, `code ${fe.code}  ${truncate(fe.err, 56)}`);
    }
    note("on a real server this WHERE fails the WHOLE query — 'e' is NOT an");
    note("answer, and neither is 'd' (a row this library declines): an");
    note("enforcing caller fails CLOSED on both, or NOT(decline-as-false)");
    note('inverts fail-closed into fail-open — the measured leak class');
  }
  blank();

  // A bad row declines ('d'), itemized, and the tail keeps its indexes.
  {
    const lt = schema.compileFilter('x < 5');
    const fr = lt.rows(Format.JSONEachRow, utf8('{"x":1}\n{"x":"zap"}\n{"x":9}\n'));
    lt.close();
    kv('`x < 5` on [1, bad, 9]', `verdicts ${verdictString(fr)}   (the bad row cannot swallow the tail)`);
  }
  blank();

  // Refused at compile, never guessed — and the two REASONS are two TYPES.
  kv('compile `now() > x`', classify(() => schema.compileFilter('now() > x')));
  kv('compile `x = {p:UInt8}`', classify(() => schema.compileFilter('x = {p:UInt8}')));
  note('clock reads would be answered with THIS process\'s clock, not the');
  note("server's; an UNBOUND {p:Type} is the SERVER's own 456 since ABI");
  note('revision 4 ("Substitution `p` is not set") — bind it instead');
  kv('compile `nosuch = 1`', classify(() => schema.compileFilter('nosuch = 1')));
  schema.close();
  blank();

  // -- revision 4 sub-demo: query parameters -----------------------------
  // One 3-row "event" body, shared with the twin sub-demo below.
  group('query parameters (ABI revision 4) — values are STRINGS, never escaped');
  const eventBody = utf8(
    '{"tenant":"acme","role":"admin","x":1}\n' +
    '{"tenant":"evil","role":"viewer","x":2}\n' +
    '{"tenant":"\' OR 1=1 --","role":"admin","x":3}\n',
  );
  const ps = lib.compileDdl('tenant String, role String, x UInt8');
  let paramsPresent = true;
  try {
    const tf = ps.compileFilter('tenant = {t:String}', { params: { t: 'acme' } });
    const fr = tf.rows(Format.JSONEachRow, eventBody);
    tf.close();
    kv('`tenant = {t:String}`, t=acme', `verdicts ${verdictString(fr)}`);
    note('compiled ONCE per (schema, expr, params) — the value is baked in;');
    note('a per-tenant cache MUST be a bounded LRU + a compile throttle');

    const hostile = "' OR 1=1 --";
    const hf = ps.compileFilter('tenant = {t:String}', { params: { t: hostile } });
    const hr = hf.rows(Format.JSONEachRow, eventBody);
    hf.close();
    const hostileOk =
      hr.outcome === 'ok' &&
      hr.verdicts.length === 3 &&
      hr.verdicts[0] === 'false' && hr.verdicts[1] === 'false' && hr.verdicts[2] === 'true';
    kv("t = `' OR 1=1 --` (hostile)", `verdicts ${verdictString(hr)}   hostile-value-inert: ${hostileOk}`);
    note('the value became a typed LITERAL after SQL parsing — it matches');
    note('only the row holding exactly that string; no OR 1=1 semantics,');
    note('and NOTHING was escaped to get there (never hand-escape values)');
    note('traps: size the {brace type} for the value\'s domain ({p:UInt8}');
    note('given "256" BINDS 0 — the reader wraps); and never NAME a param');
    note('`limit`/`offset` — a real server\'s TCP channel refuses those');
  } catch (err) {
    if (!(err instanceof UnsupportedError)) throw err;
    paramsPresent = false;
    note('this artifact predates the params surface (relink it to ABI');
    note('revision 4, `just refresh <version>`); the sub-demo degrades');
    note('here rather than failing the tour — the surface is additive.');
  }
  blank();

  // -- revision 4 sub-demo: the block twin -------------------------------
  group('the block twin (ABI revision 4) — parse ONCE, evaluate K filters');
  try {
    const block = ps.parseBlock(Format.JSONEachRow, eventBody);
    const adminF = ps.compileFilter("role = 'admin'");
    const viewerF = ps.compileFilter("role = 'viewer'");
    kv("eval `role = 'admin'`", `verdicts ${verdictString(adminF.eval(block))}`);
    kv("eval `role = 'viewer'`", `verdicts ${verdictString(viewerF.eval(block))}`);
    block.close();
    adminF.close();
    viewerF.close();
    note('ONE parse of the 3-row event fed BOTH filters: eval neither');
    note('consumes nor mutates the block, and eval(parseBlock(body)) ≡');
    note('rows(body) for every verdict class — the live-SSE hot path is');
    note('K per-principal filters × 1 event, and the re-parse is shed');
  } catch (err) {
    if (!(err instanceof UnsupportedError)) throw err;
    note('this artifact predates the block twin (relink it to ABI');
    note('revision 4, `just refresh <version>`); the sub-demo degrades');
    note('here rather than failing the tour — the surface is additive.');
  }
  void paramsPresent;
  ps.close(); // closes any open filters and blocks FIRST, then the schema
  blank();

  note('THREADS: a filter call is ALSO a use of its schema handle — two');
  note('filters over one schema never run concurrently (the SDK enforces it)');
  note('LIFETIME: filters AND blocks free before their schema; close()');
  note('ordering is structural in every SDK — schema.close() frees them first');
  note('ENFORCEMENT GATE: nothing may enforce read-side security on this');
  note('API until the WHERE-truth rig gates green (zero over-admit, zero');
  note('over-hide). Until that run of record exists this is a shadow/replay');
  note('surface: log disagreements, enforce with what enforced yesterday —');
  note('the twin is a call shape, not an enforcement opening.');
}

// verdictString renders a FilterResult's verdicts as the document's compact
// t/f/e/d string ('true' -> t, 'false' -> f, 'error' -> e, 'decline' -> d).
function verdictString(fr) {
  if (fr.outcome !== 'ok') {
    return `(call-level: outcome=${fr.outcome} code=${fr.errCode} ${truncate(fr.errMsg, 40)})`;
  }
  return `"${fr.verdicts.map((v) => v[0]).join('')}"`;
}

// payloadText decodes export payload bytes for printing (they are UTF-8 JSON
// lines by construction in this section).
function payloadText(bytes) {
  return Buffer.from(bytes).toString('utf8');
}

// ------------------------------------------------------------- plumbing
//
// Everything below is printing helpers — no chtypes calls hide here.

function section(n, title) {
  console.log(`\n=== ${n}. ${title} ===`);
}
function kv(key, value) {
  console.log(`  ${key.padEnd(30)} ${value}`);
}
function note(text) {
  console.log(`      . ${text}`);
}
function raw(text) {
  console.log(text);
}
function blank() {
  console.log();
}
function group(title) {
  console.log(`  -- ${title}`);
}
function fatal(message) {
  console.error(`\nplayground: ${message}`);
  process.exit(1);
}

/** Compile, hand the schema to fn, always close it. */
function withSchema(lib, ddl, fn, options) {
  const schema = lib.compileDdl(ddl, options);
  try {
    fn(schema);
  } finally {
    schema.close();
  }
}

/** Run one payload through rows() and print the verdict on one line, plus a
 *  "~" line per Transform (a silent change ClickHouse made). */
function feed(schema, label, format, body) {
  const batch = schema.rows(format, body);
  const parts = [batch.outcome];
  if (batch.errCode) parts.push(`code=${batch.errCode}`);
  if (batch.rows.length) {
    const vals = batch.rows[0].values.map((v) => `${v.column}=${textOr(v)}(${v.source})`).join(' ');
    if (vals) parts.push(vals);
  }
  if (batch.errMsg) parts.push(truncate(batch.errMsg, 56));
  kv(`  ${label}`, parts.join('  '));
  for (const t of batch.transformed) {
    raw(`      ~ ${t.column}: ${t.input} -> ${t.stored} (${t.reason}${t.lossy ? ', LOSSY' : ''})`);
  }
}

/** Print which typed error a call throws — peers, so plain instanceof. */
function describeError(call, what) {
  try {
    const result = call();
    if (result && typeof result.close === 'function') result.close();
    kv(what, '(no error)');
  } catch (err) {
    if (err instanceof UnsupportedError) {
      kv(what, 'UnsupportedError  (a DECLINE)');
      kv('  detail', truncate(err.detail, 84));
      kv('  also a SchemaError?', `${err instanceof SchemaError}   <- peers, not a hierarchy`);
    } else if (err instanceof SchemaError) {
      kv(what, `SchemaError  (a REFUSAL), .code=${err.code}`);
      kv('  detail', truncate(err.detail, 84));
      kv('  an UnsupportedError?', `${err instanceof UnsupportedError}   <- peers, not a hierarchy`);
    } else if (err instanceof RegistryError) {
      kv(what, 'RegistryError  (a deployment problem, not a verdict)');
      kv('  message', truncate(err.message, 84));
    } else {
      throw err;
    }
  }
}

/** Name a call's error KIND in one word. */
function classify(call) {
  try {
    const result = call();
    if (result && typeof result.close === 'function') result.close();
    return 'accepted';
  } catch (err) {
    if (err instanceof UnsupportedError) return 'DECLINED  (UnsupportedError)';
    if (err instanceof SchemaError) return `REFUSED   (SchemaError, code ${err.code})`;
    throw err;
  }
}

function caught(call) {
  try {
    const result = call();
    if (result && typeof result.close === 'function') result.close();
    return '(no error)';
  } catch (err) {
    return err.message;
  }
}

function verdict(batch) {
  if (batch.errCode) {
    return `${batch.outcome.padEnd(9)} code=${batch.errCode}  ${truncate(batch.errMsg, 44)}`;
  }
  if (batch.rows.length && batch.rows[0].values.length) {
    const v = batch.rows[0].values[0];
    return `${batch.outcome.padEnd(9)} ${v.column} = ${v.text}`;
  }
  return batch.outcome;
}

function textOr(value) {
  return value.text === '' && !value.isNull ? '<unreadable>' : value.text;
}

function truncate(text, limit) {
  const flat = String(text).replace(/\n/g, ' ');
  return flat.length <= limit ? flat : flat.slice(0, limit) + '...';
}

function utf8(text) {
  return new TextEncoder().encode(text);
}

function unhex(text) {
  const out = new Uint8Array(text.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(text.slice(i * 2, i * 2 + 2), 16);
  return out;
}

/** $CHTYPES_REGISTRY, else the per-user artifact cache every SDK defaults to. */
function registryDir() {
  if (process.env.CHTYPES_REGISTRY) return process.env.CHTYPES_REGISTRY;
  return defaultRegistryDir();
}

/** Pick the numerically highest minor line: "25.10" > "25.3", which a string
 *  sort gets exactly backwards. */
function newestLine(lines) {
  return lines.reduce((best, current) => {
    if (!best) return current;
    const a = best.split('.').map(Number);
    const b = current.split('.').map(Number);
    for (let i = 0; i < Math.max(a.length, b.length); i++) {
      if ((a[i] ?? -1) !== (b[i] ?? -1)) return (a[i] ?? -1) < (b[i] ?? -1) ? current : best;
    }
    return best;
  }, '');
}

main();
