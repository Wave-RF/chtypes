# Batches — always `rows`, and the two bad-row policies

**Call `rows` for everything.** One row is a batch of one. The vendored reader does all the framing internally, so a bare JSON object, NDJSON and a `[...]`-wrapped array are the same `JSONEachRow` body — never split or sniff a payload in your own language.

There is a `row` sugar in every binding for call sites that _semantically_ expect exactly one row: a config check, a test, a single-record webhook. On a multi-row body it returns the first row and ignores the rest, which makes it the wrong call for anything wire-facing.

Two more reasons `rows` is the unit rather than a loop over `row`:

- **Row separation is format-specific.** A quoted CSV field can contain a newline. Splitting on `\n` yourself produces a different parse from the one the server performs.
- **One batch is one clock instant.** Every volatile DEFAULT in a body resolves to the same value, by construction. A loop over `row` gives each record its own `now()`.

## A plain CSV/TSV body can still have a header

`CSV` and `TSV` bodies are read by ClickHouse's own vendored row readers — not just `CSVWithNames`/`TSVWithNames` — so a first line that spells the column names is detected and consumed as a header under `input_format_csv_detect_header` / `input_format_tsv_detect_header`, honored exactly as the setting is set for that call, following ClickHouse's own default when it is not. That shifts row counts and verdict indices by one, and a data row that happens to repeat the column names can be swallowed as a header the same way it would be on a real server. Set either setting to `0` to force positional reading of every line.

## Framing: BOM, whitespace and line ends

These are decided before a single column is parsed, independently of any bad-row policy. Measured by a downstream consumer on go/v0.5.2 against `26.8.15.10-lts`, build `1790845279`, and confirmed against a stock `clickhouse-server:26.8.15.10` — the server behaves identically, so none of this is a divergence:

- **`JSONEachRow` skips a leading UTF-8 BOM** (`EF BB BF`) and every ASCII whitespace byte — space, tab, `\n`, `\r`, and also `\f` (form feed) and `\v` (vertical tab) — before a value or an array. A BOM followed by `[{…},{…},{…}]` reads as three records, not a malformed first one.
- **`CSV`/`TSV` skip a leading BOM only when the first column's type can hold only valid UTF-8** — `UUID`, `DateTime` and the numeric types. A `String` first column keeps the BOM as data instead: a header line arrives with the raw `EF BB BF` bytes still stuck to the front of the first field, so it no longer reads as plain `id`, and the header-detection setting above will not recognize it as a header.
- **`\n\r` is ONE line end, not two.** A caller counting lines by counting `\r` or `\n` bytes independently of the reader overcounts by one per line that uses this ending.

**A batch exposes no records-seen count, byte offsets or detected-header information today.** `rows` and `rows_read` describe records the reader parsed, not the bytes or lines it consumed getting there, so none of the three facts above is visible in a `BatchResult` — track the input independently, before the call, if you need to reconcile them. Exposing framing is a candidate for a future ABI revision, not something promised here.

## What happens at the first bad row is a policy

It is ClickHouse's own, declared like any other setting, and both modes are measured against real servers.

### Strict — the server's default

The batch aborts at the first unparseable row, exactly as a real INSERT does. `rows` holds every row examined so far, the failure itemized with ClickHouse's real code and message, and rows after the failure are never examined. The batch `outcome` is `rejected`.

### Skip-and-continue

Declare `input_format_allow_errors_ratio` (or `..._num`) at compile and it is the handle's default for every later call.

<details open><summary><b>Go</b></summary>

```go
schema, err := lib.CompileDDL("x UInt8", chtypes.WithCompileSettings(map[string]string{
	"input_format_allow_errors_ratio": "1", // skip any number of bad rows
}))
batch, err := schema.Rows(chtypes.JSONEachRow, body, nil)

fmt.Println(batch.Outcome, batch.RowsRead, batch.RowsSkipped) // accepted 3 1
for i, r := range batch.Rows {
	fmt.Println(i, r.Outcome, r.ErrCode) // 0 accepted 0 / 1 skipped 27 / 2 accepted 0
}
```

</details>

<details><summary><b>Python</b></summary>

```python
schema = library.compile_ddl("x UInt8", settings={"input_format_allow_errors_ratio": "1"})
batch = schema.rows(Format.JSON_EACH_ROW, body)

print(batch.outcome, batch.rows_read, batch.rows_skipped)   # accepted 3 1
for i, r in enumerate(batch.rows):
    print(i, r.outcome, r.err_code)   # 0 accepted 0 / 1 skipped 27 / 2 accepted 0
```

</details>

<details><summary><b>TypeScript</b></summary>

```ts
const schema = lib.compileDdl('x UInt8', {
  settings: { input_format_allow_errors_ratio: '1' },
});
const batch = schema.rows(Format.JSONEachRow, body);

console.log(batch.outcome, batch.rowsRead, batch.rowsSkipped); // accepted 3 1
batch.rows.forEach((r, i) => console.log(i, r.outcome, r.errCode));
```

</details>

<details><summary><b>Rust</b></summary>

```rust
let schema = lib
.compile("x UInt8")
.settings([("input_format_allow_errors_ratio", "1")])
.compile()?;
let batch = schema.rows(Format::JsonEachRow, body, NO_SETTINGS)?;

println!("{:?} {} {}", batch.outcome, batch.rows_read, batch.rows_skipped); // Accepted 3 1
for (i, r) in batch.rows.iter().enumerate() {
    println!("{i} {:?} {}", r.outcome, r.err_code);
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

Bad rows are skipped with the server's own machinery — resync is line-based and byte-matched to real servers across every supported version — and **itemized**, which a real server does not do. ClickHouse's `IRowInputFormat::generate` computes the error for every skip and then logs only a count; reporting it invents nothing, it just keeps what was already computed.

So one `rows` call answers, per input record and in input order: accepted with its stored values, or skipped with ClickHouse's own code and message. **A skipped row is never stored** — filter on the row's own `outcome` when rendering survivors, not on the batch's.

### The wire format decides what one bad record can cost

Under `allow_errors`, NDJSON and a multi-line JSON array resynchronize per record, so a malformed record costs only itself, as in the example above. A **single-line compact JSON array** has no per-record newline to resynchronize on, so one malformed record inside it loses the **entire** batch. This is server-faithful — it is how ClickHouse's own reader behaves, because the line is the unit it can resynchronise on — so the wire format is an operational choice worth making deliberately whenever `allow_errors` is in force.

### A malformed `UUID` swallows the records behind it

Resync under `allow_errors` is byte-matched to the server, and the server's own text `UUID` reader has a quirk this library reproduces exactly: it reads a fixed 36-byte window, and error recovery resumes after that window rather than after the bytes the bad value actually occupied — so recovery can land mid-record and silently swallow whatever follows.

Measured by a downstream consumer on go/v0.5.2 against `26.8.15.10-lts`, build `1790845279`, and confirmed against a stock `clickhouse-server:26.8.15.10` — both match, so this is not a divergence:

- **JSONEachRow**, one record per line, schema `page String, id UUID DEFAULT …`, `input_format_allow_errors_ratio=1`:

  ```text
  {"page":"/a","id":"x"}
  {"page":"/b"}
  {"page":"/c"}
  ```

  The batch answers **one** row — record 0, `skipped` with code 376 `Cannot parse UUID from String` — and nothing else. Records `/b` and `/c` are never parsed and never appear in `rows`: not `accepted`, not `skipped`, just absent.
- **CSV** swallows differently: a bad first row (`zzz,/a,1`) followed by two valid rows loses `/b` but still answers `/c`. The window's reach is format-specific.
- **The server** stores the identical subset under the identical setting, and fails outright with code 376 without it.
- **Control — these do NOT swallow a neighboring record:** `IPv4`, `IPv6`, `Date`, `Date32`, `DateTime`, `DateTime64`, `Decimal`, `Int128`, `UInt256`, `Bool`, `Enum`, `FixedString`.
- **Cause, inferred:** the text `UUID` reader's fixed-width window, not the bytes it actually consumed, is what error recovery resumes from.

A caller can't tell any of this happened from the per-record answers alone — see [Framing: BOM, whitespace and line ends](#framing-bom-whitespace-and-line-ends) for what a batch does and doesn't expose about the input bytes. If a schema takes untrusted `UUID` input under `allow_errors`, verify the record count independently rather than trusting `rows_read`.

### CHECK constraints are batch-level, not per-row

A `CHECK` in the compiled DDL is evaluated, and a violation answers ClickHouse's own **code 469**. Like the server, **one violating row rejects the entire batch** — the batch `outcome` is `rejected`, and every row is discarded together — and the export channel declines rather than emitting partial bytes. A caller that assumes per-row rejection here will build a partial-success path that never fires. `input_format_allow_errors_*` does not rescue this: it skips a row the parser itself cannot read, and a CHECK violation is not a parse failure — it is evaluated against a row the parser read successfully — so `allow_errors` and a CHECK violation are orthogonal, and the batch still rejects whole.

The violation's message matches the server's in its code, the constraint's name and the constraint's expression. A real server's message also names its own table (database, table and UUID) and the violating row's column values. This library has no table, so that part of the message differs by design. Match a CHECK violation on the code and the constraint name, never on the whole message text.

### Too many partitions: a batch-shape 252

A schema that declared its partition key (revision 6: `SetPartitionBy` in Go, `set_partition_by` in Python and Rust, `setPartitionBy` in TypeScript) answers which partition each stored row lands in (`partition_id`) and how many partitions the batch spans (`partition_count`). A body that would split into more partitions than the call's `max_partitions_per_insert_block` allows — `100` unless a setting says otherwise, `0` meaning unlimited — is refused the way the server refuses it: the batch `outcome` is `rejected` with ClickHouse's own **code 252**, and the rows stay itemized, exactly the shape a `CHECK` violation has. Nothing in any row is wrong; the batch is. The remedy is to split the body by partition, and grouping an accepted batch's export spans by `partition_id` gives exactly those per-partition bodies.

**Branch on code 252, never on the message text.** The message is ClickHouse's own wording and moves between lines; the code is the stable part. And the name a code carries is the loaded build's own, so if you want it for a log line, ask the library rather than writing it down: its error-code table (`lib.ErrorCodes()` in Go, `lib.error_codes()` in Python and Rust, `lib.errorCodes()` in TypeScript) answers the name for 252 on that line ([§Error codes](../reference/bindings.md#error-codes--a-binding-never-carries-clickhouses-code-table-revision-6)).

<details open><summary><b>Go</b></summary>

```go
if err := schema.SetPartitionBy("toYYYYMM(ts)"); err != nil {
	return err // *SchemaError: the server refused the key; *UnsupportedError: a decline
}
batch, _ := schema.Rows(chtypes.JSONEachRow, body, nil)
if batch.Outcome == chtypes.Rejected && batch.ErrCode == 252 {
	// too many partitions for one INSERT — split the body by RowResult.PartitionID
}
```

</details>

<details><summary><b>Python</b></summary>

```python
schema.set_partition_by("toYYYYMM(ts)")
batch = schema.rows(Format.JSON_EACH_ROW, body)
if batch.outcome is Outcome.REJECTED and batch.err_code == 252:
    ...  # too many partitions for one INSERT — split the body by row.partition_id
```

</details>

<details><summary><b>TypeScript</b></summary>

```ts
schema.setPartitionBy('toYYYYMM(ts)');
const batch = schema.rows(Format.JSONEachRow, body);
if (batch.outcome === Outcome.Rejected && batch.errCode === 252) {
  // too many partitions for one INSERT — split the body by row.partitionId
}
```

</details>

<details><summary><b>Rust</b></summary>

```rust
schema.set_partition_by("toYYYYMM(ts)")?;
let batch = schema.rows(Format::JsonEachRow, body, NO_SETTINGS)?;
if batch.outcome == Outcome::Rejected && batch.err_code == 252 {
    // too many partitions for one INSERT — split the body by row.partition_id
}
```

</details>

The verdict is the one a **synchronous** insert of this body gets. Under `async_insert` the server counts partitions over a coalesced flush of which this body is only a part, so a refusal here means the server refuses too, while an acceptance here does not guarantee the server accepts.

## The pairing rule for a gateway and a worker

If you validate in one process and INSERT from another, the settings chtypes sees must be the settings the INSERT runs under. `input_format_allow_errors_*` is the one exception, and it matters:

> **Never let `input_format_allow_errors_*` reach the real INSERT.** Server-side skipping is silent data loss.

Validate with it at the gate, forward only the accepted rows, and the worker's INSERT needs no error allowance at all. Any row the server would have skipped has already been reported to you, by name, with the reason.

## `engineRows` is the stored truth when it is present

`rows` describes the parse. The storage layer can do more: the MergeTree insert-time merge collapses rows under a ReplacingMergeTree or a CollapsingMergeTree, and a table-level TTL can drop them outright.

When the result carries `engineRows` (`engine_rows` in Python and Rust), **it, not `rows`, is what the table would end up holding.** The batch-level `transformed` folds in the storage layer's own verdicts alongside it — `ttl_expired` and `ttl_column_expired` are batch facts, not parse facts.

## Exporting the accepted rows as wire bytes

Sometimes the point of validating a batch is to forward it. The export channel hands back the accepted rows already serialized, from the **same single call** — never a second pass, never a re-parse.

<details open><summary><b>Go</b></summary>

```go
batch, err := schema.RowsExport(chtypes.JSONEachRow, body, nil, chtypes.JSONCompactEachRow)
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
const batch = schema.rows(Format.JSONEachRow, body, undefined, {
  exportFormat: Format.JSONCompactEachRow,
});
// batch.payload, batch.spans, batch.exportDeclined
```

</details>

<details><summary><b>Rust</b></summary>

```rust
let batch = schema.rows_export(
    Format::JsonEachRow, body, NO_SETTINGS,
    Some(Format::JsonCompactEachRow), DocFlags::NONE,
)?;
// batch.payload, batch.spans, batch.export_declined
```

</details>

`JSONCompactEachRow` is the one format this ABI revision serializes; asking for another answers `unsupported` for the whole call rather than silently emitting nothing. `spans` is index-aligned with `rows` and is `{0, 0}` for any row that was not accepted. Slicing a span out of the payload **is** that row's line, its trailing newline included, and concatenating the non-zero spans reproduces the payload exactly.

Within a row, `JSONCompactEachRow` separates fields with a comma **and a space** — `", "`, not a bare comma — so anything comparing exported bytes literally, or sizing a buffer from a field count, needs the exact separator.

Three payload states, and they are distinct: absent means no export was requested, or it was declined, with the reason always in `export_declined`; present-but-empty means the export ran and emitted nothing. The bytes are copied out of the C buffer and freed before the call returns, so no ownership crosses the boundary.

`export_declined` is populated for **every** non-accepted batch that requested an export, including a batch with zero rows — a `CSVWithNames` body naming an unknown header column under `input_format_skip_unknown_fields=0`, say, which rejects before a single row is admitted. Before the artifact producer's relink served at `chtypes_build` 1790845279, such a batch left `export_declined` empty even though bytes were withheld; a caller could not tell "no export requested" from "an export was declined but the library did not say why." The relink closed that gap, by design: a non-accepted batch that asked for export always names the reason, rows or no rows.

> **This applies only to builds at `chtypes_build` 1790845279 or later, on the supported lines (`26.3`, `26.7`, `26.8`, `26.9` — [`support.md`](../support.md)).** A served, unsupported (retired) line never gets a new build or a new ABI revision ([`support.md` → Served, unsupported ClickHouse lines](../support.md#served-unsupported-clickhouse-lines)), so it keeps the pre-relink behavior permanently — `26.6`'s newest build, `1790767905`, predates this relink and was never republished.

**Document flags** thin the _description_ without ever changing the _verdict_. Passing an export format defaults them to lean — verdicts only — because the usual reason to export is to forward bytes rather than to read a report. Ask for the values, transforms or defaults back explicitly if you want them — `DocValues` / `DocTransforms` / `DocDefaults` in Go, `DOC_VALUES` / `DOC_TRANSFORMS` / `DOC_DEFAULTS` in Python and TypeScript, `DocFlags::VALUES` / `DocFlags::TRANSFORMS` / `DocFlags::DEFAULTS` in Rust. A plain `rows` call is the all-flags spelling and stays byte-identical to what it always returned.

Attaching a compiled filter to this same call narrows the export further, to the rows the filter admits, from the same one parse: [`filters.md` → Exporting only the rows a filter admits](filters.md#exporting-only-the-rows-a-filter-admits).

## Next

- [`transformations.md`](transformations.md) — what the accepted rows were silently changed into.
- [`settings.md`](settings.md) — where `input_format_allow_errors_ratio` sits among the channels.
- [`filters.md`](filters.md) — asking a boolean question of the same body.
