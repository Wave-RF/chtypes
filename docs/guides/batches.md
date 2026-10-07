# Batches — always `rows`, and the two bad-row policies

**Call `rows` for everything.** One row is a batch of one. The vendored reader does all the framing internally, so a bare JSON object, NDJSON and a `[...]`-wrapped array are the same `JSONEachRow` body — never split or sniff a payload in your own language.

There is a `row` call in every binding for call sites that _semantically_ expect exactly one row: a config check, a test, a single-record webhook. It is a separate single-row entry point, which makes it the wrong call for anything wire-facing.

Two more reasons `rows` is the unit rather than a loop over `row`:

- **Row separation is format-specific.** A quoted CSV field can contain a newline. Splitting on `\n` yourself produces a different parse from the one the server performs.
- **One batch is one clock instant.** Every volatile DEFAULT in a body resolves to the same value, by construction. A loop over `row` gives each record its own `now()`.

## One record, or a whole body

`row` (`chs_preview_row`) previews **one record**. Given a body that holds several, it does not pick the first record out and ignore the rest: it reads the whole input as the server's `INSERT` of that body would, and answers with the first row's verdict. So a later record in the same body can decide it. For example, a `LowCardinality(String)` column whose dictionary for the body also holds another row's too-long key makes `toFixedString(lc, 2)` fail with 131 for a first row whose own value fits, because the server's `INSERT` of that body refuses it with 131 too.

To preview a record as its own one-row `INSERT`, pass that record alone. For each record's own verdict in a multi-record body, call `rows` (`chs_preview_batch`): its `rows` carry per-record outcomes, and a record's own verdict there does not depend on a later record's value.

## A plain CSV/TSV body can still have a header

`CSV` and `TSV` bodies are read by ClickHouse's own vendored row readers — not just `CSVWithNames`/`TSVWithNames` — so a first line that spells the column names is detected and consumed as a header under `input_format_csv_detect_header` / `input_format_tsv_detect_header`, honored exactly as the setting is set for that call, following ClickHouse's own default when it is not. That shifts row counts and verdict indices by one, and a data row that happens to repeat the column names can be swallowed as a header the same way it would be on a real server. Set either setting to `0` to force positional reading of every line.

## Framing: BOM, whitespace and line ends

These are decided before a single column is parsed, independently of any bad-row policy. Measured by a downstream consumer on go/v0.5.2, darwin-arm64, against `26.8.15.10-lts`, build `1790845279`, and confirmed against the stock `clickhouse-server:26.8.15.10` server image — the server behaves identically, so none of this is a divergence:

- **`JSONEachRow` skips a leading UTF-8 BOM** (`EF BB BF`) and every ASCII whitespace byte — space, tab, `\n`, `\r`, and also `\f` (form feed) and `\v` (vertical tab) — before a value or an array. A BOM followed by `[{…},{…},{…}]` reads as three records, not a malformed first one.
- **Plain `CSV`/`TSV` skip a leading BOM only when the first column is a type whose text form can contain only valid UTF-8** — `UUID`, `DateTime` and the numeric types. A `String` first column keeps the BOM as data instead: a header line arrives with the raw `EF BB BF` bytes still stuck to the front of the first field, so it no longer reads as plain `id`, and the header-detection setting above will not recognize it as a header. `CSVWithNames`/`TSVWithNames` skip a leading BOM unconditionally, regardless of the first column's type (from the server's reader source, `RowInputFormatWithNamesAndTypes::readPrefix`, not measured).
- **`\n\r` is ONE line end, not two.** A caller counting lines by counting `\r` or `\n` bytes independently of the reader overcounts by one per line that uses this ending.

**A v1 batch reports what the reader decided about the framing.** `rows` and `rows_read` still describe the records the reader parsed, not the bytes or lines it consumed getting there, but the batch now carries two more answers ([`reference/bindings-v1.md` §5](../reference/bindings-v1.md#batchresult-from-the-batch-document-and-the-export-buffer)):

- `unconsumed` lists the byte ranges the reader's error recovery skipped, and each row's `input_span` says which bytes the reader consumed for it.
- `framing` has `bom_skipped`, `container` (`array`, `stream`, or none) and `header` (whether one was consumed, how many lines, and its names). `bom_skipped` and `header` can be **unknown**, which is never false and never empty: the vendored reader does not expose them for `TSV`, `TSVWithNames` and `Values`, while `CSV` and the two JSON formats fill both. Unknown is a nil pointer in Go, `None` in Python and Rust, and `null` in TypeScript.

**`unconsumed` does not account for every record.** A skipped row's `input_span` can cover more than one input record, when the reader's recovery resumed past the end of the record that failed, so the verdicts can be fewer than the body's records while `unconsumed` is empty. **Counting the records yourself is not enough either.** A record can be lost while the number of verdicts still equals the number of records: the reader's recovery can open an extra record that masks the swallowed one. This was measured in every format, for example with a multi-line quoted CSV field or a JSON object. In every masked body measured, `unconsumed` was non-empty.

So until a batch-level signal from the reader's own framing exists (#478), the only rule that accounts for every record is: **decline the body when any row is skipped or `unconsumed` is non-empty.** A caller that answers each record on its own can still answer an ordinary refused record per row; it just cannot treat a body with a skipped row or an `unconsumed` range as fully accounted for.

## What happens at the first bad row is a policy

It is ClickHouse's own, declared like any other setting, and both modes are measured against real servers.

### Strict — the server's default

The batch aborts at the first unparseable row, exactly as a real INSERT does. `rows` holds every row examined so far, the failure itemized with ClickHouse's real code and message, and rows after the failure are never examined. The batch `outcome` is `rejected`.

### Skip-and-continue

Declare `input_format_allow_errors_ratio` (or `..._num`) at compile and it is the handle's default for every later call.

<details open><summary><b>Go</b></summary>

```go
schema, err := lib.CompileTable("CREATE TABLE t (x UInt8) ENGINE = Memory", chtypes.WithSettings(map[string]string{
	"input_format_allow_errors_ratio": "1", // skip any number of bad rows
}))
batch, err := schema.Rows(chtypes.JSONEachRow, body)

fmt.Println(batch.Outcome, batch.RowsRead, batch.RowsSkipped) // accepted 3 1
for i, r := range batch.Rows {
	fmt.Println(i, r.Outcome, r.ErrCode) // 0 accepted 0 / 1 skipped 27 / 2 accepted 0
}
```

</details>

<details><summary><b>Python</b></summary>

```python
schema = library.compile_table(
    "CREATE TABLE t (x UInt8) ENGINE = Memory",
    settings={"input_format_allow_errors_ratio": "1"},
)
batch = schema.rows(Format.JSON_EACH_ROW, body)

print(batch.outcome, batch.rows_read, batch.rows_skipped)   # accepted 3 1
for i, r in enumerate(batch.rows):
    print(i, r.outcome, r.err_code)   # 0 accepted 0 / 1 skipped 27 / 2 accepted 0
```

</details>

<details><summary><b>TypeScript</b></summary>

```ts
const schema = lib.compileTable('CREATE TABLE t (x UInt8) ENGINE = Memory', {
  settings: { input_format_allow_errors_ratio: '1' },
});
const batch = schema.rows(Format.JSONEachRow, body);

console.log(batch.outcome, batch.rowsRead, batch.rowsSkipped); // accepted 3 1
batch.rows.forEach((r, i) => console.log(i, r.outcome, r.errCode));
```

</details>

<details><summary><b>Rust</b></summary>

```rust
let options = CompileOptions {
    settings: vec![("input_format_allow_errors_ratio".into(), "1".into())],
    ..Default::default()
};
let schema = lib.compile_table("CREATE TABLE t (x UInt8) ENGINE = Memory", &options)?;
let batch = schema.rows(Format::JsonEachRow, body, &RowsOptions::default())?;

println!("{} {} {}", batch.outcome, batch.rows_read, batch.rows_skipped); // accepted 3 1
for (i, r) in batch.rows.iter().enumerate() {
    println!("{i} {} {}", r.outcome, r.err_code);
}
```

</details>

Given the body

```text
{"x":1}
{"x":oops}
{"x":3}
```

the batch is **accepted**, `rows_read` is 3, `rows_skipped` is 1, and `rows` holds all three input records in input order: the survivors as `accepted` with their coerced values, and the bad one in its own place as `skipped`, carrying the exact error the vendored reader caught before resyncing.

`rows_skipped` counts only rows skipped under an `input_format_allow_errors_*` allowance. A strict batch (no allowance) that is rejected reports `rows_skipped` 0 from artifact build `20261007.162307` on; earlier builds reported 1 for the aborting row.

Bad rows are skipped with the server's own machinery — resync is line-based and byte-matched to real servers across every supported version — and **itemized**, which a real server does not do. ClickHouse's `IRowInputFormat::generate` computes the error for every skip and then logs only a count; reporting it invents nothing, it just keeps what was already computed.

So one `rows` call answers, per input record and in input order: accepted with its stored values, or skipped with ClickHouse's own code and message. **A skipped row is never stored** — filter on the row's own `outcome` when rendering survivors, not on the batch's.

### The wire format decides what one bad record can cost

Under `allow_errors`, NDJSON and a multi-line JSON array resynchronize per record, so a malformed record costs only itself, as in the example above. A **single-line compact JSON array** has no per-record newline to resynchronize on, so one malformed record inside it loses the **entire** batch. This is server-faithful — it is how ClickHouse's own reader behaves, because the line is the unit it can resynchronise on — so the wire format is an operational choice worth making deliberately whenever `allow_errors` is in force.

### A malformed `UUID` swallows the records behind it

Resync under `allow_errors` is byte-matched to the server, and the server's own text `UUID` reader has a quirk this library reproduces exactly: it reads a fixed 36-byte window, and error recovery resumes after that window rather than after the bytes the bad value actually occupied — so recovery can land mid-record and silently swallow whatever follows.

Measured by a downstream consumer on go/v0.5.2, darwin-arm64, against `26.8.15.10-lts`, build `1790845279`, and confirmed against the stock `clickhouse-server:26.8.15.10` server image — both match, so this is not a divergence:

- **JSONEachRow**, one record per line, schema `page String, id UUID DEFAULT …`, `input_format_allow_errors_ratio=1`:

  ```text
  {"page":"/a","id":"x"}
  {"page":"/b"}
  {"page":"/c"}
  ```

  The batch answers **one** row — record 0, `skipped` with code 376 `Cannot parse UUID from String` — and nothing else. Records `/b` and `/c` are never parsed and never appear in `rows`: not `accepted`, not `skipped`, just absent.
- **A longer six-record NDJSON body, first record malformed, answers 2–3 rows — the server itself stores 2.**
- **CSV**, schema `id UUID, page String, n UInt8`: a bad first row (`zzz,/a,1`) followed by two valid rows loses `/b` but still answers `/c`.
- **The server** stores the identical subset under the identical setting, and fails outright with code 376 without it.
- **Control — these do NOT swallow a neighboring record:** `IPv4`, `IPv6`, `Date`, `Date32`, `DateTime`, `DateTime64`, `Decimal`, `Int128`, `UInt256`, `Bool`, `Enum`, `FixedString`.
- **Cause, inferred:** the text `UUID` reader's fixed 36-byte window, not the bytes it actually consumed, is what error recovery resumes from — and how many later records that window swallows depends on how many of their bytes fall inside it, so a run of short records loses more of them than a run of long ones. The three-record, CSV and six-record counts above are consistent with that budget, not with anything specific to the wire format.

A caller can't tell any of this happened from the per-record answers alone — see [Framing: BOM, whitespace and line ends](#framing-bom-whitespace-and-line-ends) for `unconsumed` and `framing`, the v1 fields that report what the reader skipped. Whether `unconsumed` names the range a UUID window swallows is `unverified`; the 0.x measurements above predate it. If a schema takes untrusted `UUID` input under `allow_errors`, verify the record count independently rather than trusting `rows_read`.

### CHECK constraints are batch-level, not per-row

A `CHECK` in the compiled DDL is evaluated, and a violation answers ClickHouse's own **code 469**. Like the server, **one violating row rejects the entire batch** — the batch `outcome` is `rejected`, and every row is discarded together — and the export channel declines rather than emitting partial bytes. A caller that assumes per-row rejection here will build a partial-success path that never fires. `input_format_allow_errors_*` does not rescue this: it skips a row the parser itself cannot read, and a CHECK violation is not a parse failure — it is evaluated against a row the parser read successfully — so `allow_errors` and a CHECK violation are orthogonal, and the batch still rejects whole.

The violation's message matches the server's in its code, the constraint's name and the constraint's expression. A real server's message also names its own table (database, table and UUID) and the violating row's column values. This library has no table, so that part of the message differs by design. Match a CHECK violation on the code and the constraint name, never on the whole message text.

> **The CHECK expression itself must return `UInt8`, and a real server passes a row only when that value equals `1` exactly.** 1.0 enforces the same rule on every supported line: a `UInt8` result other than `1` rejects the batch with code 469, and a non-`UInt8` result with code 1, as a real server does. Write `CHECK x = 1`, or another comparison, rather than a bare column.

### Too many partitions: a batch-shape 252

A schema whose `CREATE TABLE` declares a `PARTITION BY` answers which partition each stored row lands in (`partition_id` on each row) and how many partitions the batch spans (`partition_count`). There is no separate partition setter in 1.0: the key is part of the one statement you compile ([`reference/bindings-v1.md` §7](../reference/bindings-v1.md#7-what-v0-api-is-deleted-and-why)). A body that would split into more partitions than the call's `max_partitions_per_insert_block` allows — `100` unless a setting says otherwise, `0` meaning unlimited — is refused the way the server refuses it: the batch `outcome` is `rejected` with ClickHouse's own **code 252**, and the rows stay itemized, exactly the shape a `CHECK` violation has. Nothing in any row is wrong; the batch is. The remedy is to split the body by partition, and grouping an accepted batch's export spans by `partition_id` gives exactly those per-partition bodies.

**Branch on code 252, never on the message text.** The message is ClickHouse's own wording and moves between lines; the code is the stable part. And the name a code carries is the loaded build's own, so if you want it for a log line, ask the library rather than writing it down: its error-code table (`lib.ErrorCodes()` in Go, `lib.error_codes()` in Python and Rust, `lib.errorCodes()` in TypeScript) answers the name for 252 on that line ([`ErrorCodeTable`](../reference/bindings-v1.md#errorcodetable-from-the-error_code_table-document)).

<details open><summary><b>Go</b></summary>

```go
schema, err := lib.CompileTable("CREATE TABLE t (ts DateTime) ENGINE = MergeTree PARTITION BY toYYYYMM(ts) ORDER BY ts")
if err != nil {
	return err // *SchemaError: the server refused the statement; *UnsupportedError: a decline
}
batch, _ := schema.Rows(chtypes.JSONEachRow, body)
if batch.Outcome == chtypes.Rejected && batch.ErrCode == 252 {
	// too many partitions for one INSERT — split the body by RowResult.PartitionID
}
```

</details>

<details><summary><b>Python</b></summary>

```python
schema = library.compile_table(
    "CREATE TABLE t (ts DateTime) ENGINE = MergeTree PARTITION BY toYYYYMM(ts) ORDER BY ts"
)
batch = schema.rows(Format.JSON_EACH_ROW, body)
if batch.outcome is Outcome.REJECTED and batch.err_code == 252:
    ...  # too many partitions for one INSERT — split the body by row.partition_id
```

</details>

<details><summary><b>TypeScript</b></summary>

```ts
const schema = lib.compileTable(
  'CREATE TABLE t (ts DateTime) ENGINE = MergeTree PARTITION BY toYYYYMM(ts) ORDER BY ts',
);
const batch = schema.rows(Format.JSONEachRow, body);
if (batch.outcome === Outcome.Rejected && batch.errCode === 252) {
  // too many partitions for one INSERT — split the body by row.partitionId
}
```

</details>

<details><summary><b>Rust</b></summary>

```rust
let schema = lib.compile_table(
    "CREATE TABLE t (ts DateTime) ENGINE = MergeTree PARTITION BY toYYYYMM(ts) ORDER BY ts",
    &CompileOptions::default(),
)?;
let batch = schema.rows(Format::JsonEachRow, body, &RowsOptions::default())?;
if batch.outcome == Outcome::Rejected && batch.err_code == 252 {
    // too many partitions for one INSERT — split the body by row.partition_id
}
```

</details>

The verdict is the one a **synchronous** insert of this body gets. Under `async_insert` the server counts partitions over a coalesced flush of which this body is only a part, so a refusal here means the server refuses too, while an acceptance here does not guarantee the server accepts.

## How a batch's outcome is chosen

The batch answers the first of these that applies. Production builds from `20261007.162307` follow this order; earlier builds could read `accepted` while rows were `unsupported`.

1. **A call-level rejection is `rejected`.** The call's own settings are refused (a value the settings constraints or the setter refuse), or the INSERT column list's verdict, a CREATE-time type gate or a contract violation refuses the call. The server refuses these before it reads a byte of the body, so no row changes the answer.
2. **A call-level decline is `unsupported`,** with the setting named in the result's `unsupported_settings`. This holds whatever the rows say, refusing rows and an empty body included: every row was read in a context the call did not ask for, so neither an acceptance nor a refusal there is the server's answer (a `DateTime` past the type's range in the schema's own zone can be in range in the call's zone). Every row reads `unsupported`, filter rows read `d`, the row preview answers the same, and no export bytes are produced.
3. **Otherwise the first row, in body order, that is neither accepted nor skipped** gives its outcome (`rejected` or `unsupported`) and its code. An `unsupported` row ends the read, so a refused row after it is not reached and the batch stays `unsupported`. After the rows, the whole-body steps run in the server's order (the Object and Dynamic block steps, the DEFAULT step over the reader's chunks, the partition split, the writer's index expressions, the engine merge, the TTL, the projections), only on a batch the rows left accepted, and each may still refuse or decline it.
4. **`accepted_poisoned`:** a stored value the server itself cannot read back. The INSERT succeeds, but nothing is exported.
5. **`accepted`,** the only verdict that exports bytes.

A skipped row never changes the verdict. Steps 2 and 3 depart from "rejected if any row is refused" for one reason: never a refusal that is not definite. Both answer `unsupported`, which a caller never scores as agreement.

## The pairing rule for a gateway and a worker

If you validate in one process and INSERT from another, the settings chtypes sees must be the settings the INSERT runs under. `input_format_allow_errors_*` is the one exception, and it matters:

> **Never let `input_format_allow_errors_*` reach the real INSERT.** Server-side skipping is silent data loss.

Validate with it at the gate, forward only the accepted rows, and the worker's INSERT needs no error allowance at all. Any row the server would have skipped has already been reported to you, by name, with the reason.

## `engine_rows` is the stored truth when it is present

`rows` describes the parse. The storage layer can do more: the MergeTree insert-time merge collapses rows under a ReplacingMergeTree or a CollapsingMergeTree.

When the result carries `engine_rows` (`EngineRows` in Go, `engineRows` in TypeScript), **it, not `rows`, is what the table would end up holding.** The batch-level `transformed` folds in the storage layer's own verdicts alongside it, as batch facts rather than parse facts.

`engine_rows` matches what a server's synchronous `INSERT` writes. A TTL does not change that: a row past a rows TTL stays in `engine_rows`, and a column past a column TTL keeps the value you supplied. The server deletes the row or resets the column at the next merge, which a preview does not perform. ABI v2 reports this separately, as `at_merge`.

This holds for library builds from `20261007.120436` on. An earlier build previewed the merge's result instead: it left an expired row out of `engine_rows` and reported `ttl_expired`, or reset an expired column to its `DEFAULT` and reported `ttl_column_expired`. Those two reasons are still in the v1 vocabulary, so a reader accepts them, but a build from `20261007.120436` on never emits them.

## Exporting the accepted rows as wire bytes

Sometimes the point of validating a batch is to forward it. The export channel hands back the accepted rows already serialized, from the **same single call** — never a second pass, never a re-parse.

<details open><summary><b>Go</b></summary>

```go
batch, err := schema.Rows(chtypes.JSONEachRow, body, chtypes.WithExport(chtypes.JSONCompactEachRow))
// batch.Payload holds the wire bytes; batch.Spans[i] addresses row i inside it
```

</details>

<details><summary><b>Python</b></summary>

```python
batch = schema.rows(Format.JSON_EACH_ROW, body, export=Format.JSON_COMPACT_EACH_ROW)
# batch.payload, batch.spans, batch.export_declined
```

</details>

<details><summary><b>TypeScript</b></summary>

```ts
const batch = schema.rows(Format.JSONEachRow, body, {
  exportFormat: Format.JSONCompactEachRow,
});
// batch.payload, batch.spans, batch.exportDeclined
```

</details>

<details><summary><b>Rust</b></summary>

```rust
let options = RowsOptions { export: Some(Format::JsonCompactEachRow), ..Default::default() };
let batch = schema.rows(Format::JsonEachRow, body, &options)?;
// batch.payload, batch.spans, batch.export_declined
```

</details>

Which formats a build can export is in its own `build_info`: `capabilities.export_formats` lists ClickHouse's format names. Read it rather than probing by calling. `rows_export` and its spellings are gone in 1.0; export is the `export` option of the one `rows` call. Each span is a row's place in `payload` as an offset and a length (`off`, `len`). Slicing a span out of the payload **is** that row's line, its trailing newline included, and concatenating the spans reproduces the payload exactly (`measured` on 0.x; unverified on 1.0).

Within a row, `JSONCompactEachRow` separates fields with a comma **and a space** — `", "`, not a bare comma — so anything comparing exported bytes literally, or sizing a buffer from a field count, needs the exact separator.

Three payload states, and they are distinct: absent means no export was requested, or it was declined, with the reason always in `export_declined`; present-but-empty means the export ran and emitted nothing. The bytes are copied out of the C buffer and freed before the call returns, so no ownership crosses the boundary.

`export_declined` is populated for **every** non-accepted batch that requested an export, including a batch with zero rows — a `CSVWithNames` body naming an unknown header column under `input_format_skip_unknown_fields=0`, say, which rejects before a single row is admitted. A non-accepted batch that asked for export always names the reason, rows or no rows. In 0.x, builds from before the artifact producer's relink at `chtypes_build` 1790845279 left it empty there, and v1 artifacts are later builds (`inferred`).

**An export is also how you insert a DEFAULT the library drew.** If a row carries a `default_generated` column, insert the export's `payload`, never the original body: [`reference/bindings-v1.md` §5, Generated defaults](../reference/bindings-v1.md#generated-defaults-insert-the-librarys-output-not-your-input).

**Document flags** thin the _description_ without ever changing the _verdict_. In 1.0 an absent flag set means **all** groups, with or without an export: the 0.x rule that an export defaulted to lean is gone. To get the lean, verdicts-only shape when you only want to forward bytes, ask for it: `WithDocFlags` in Go, `doc_flags=` in Python, `docFlags` in TypeScript, `doc_flags` in Rust, built from `DocValues` / `DocTransforms` / `DocDefaults` in Go, `DocFlags.VALUES` / `.TRANSFORMS` / `.DEFAULTS` in Python and TypeScript, `DocFlags::VALUES` / `TRANSFORMS` / `DEFAULTS` in Rust, or `DocAll` for everything.

Attaching a compiled filter to this same call narrows the export further, to the rows the filter admits, from the same one parse: [`filters.md` → Exporting only the rows a filter admits](filters.md#exporting-only-the-rows-a-filter-admits).

## Next

- [`transformations.md`](transformations.md) — what the accepted rows were silently changed into.
- [`settings.md`](settings.md) — where `input_format_allow_errors_ratio` sits among the channels.
- [`filters.md`](filters.md) — asking a boolean question of the same body.
