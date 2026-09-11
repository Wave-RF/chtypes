# Batches — always `rows`, and the two bad-row policies

**Call `rows` for everything.** One row is a batch of one. The vendored reader does all the framing internally, so a bare JSON object, NDJSON and a `[...]`-wrapped array are the same `JSONEachRow` body — never split or sniff a payload in your own language.

There is a `row` sugar in every binding for call sites that _semantically_ expect exactly one row: a config check, a test, a single-record webhook. On a multi-row body it returns the first row and ignores the rest, which makes it the wrong call for anything wire-facing.

Two more reasons `rows` is the unit rather than a loop over `row`:

- **Row separation is format-specific.** A quoted CSV field can contain a newline. Splitting on `\n` yourself produces a different parse from the one the server performs.
- **One batch is one clock instant.** Every volatile DEFAULT in a body resolves to the same value, by construction. A loop over `row` gives each record its own `now()`.

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

Three payload states, and they are distinct: absent means no export was requested, it was declined (the reason is in `export_declined`), or a call-level verdict preempted it; present-but-empty means the export ran and emitted nothing. The bytes are copied out of the C buffer and freed before the call returns, so no ownership crosses the boundary.

**Document flags** thin the _description_ without ever changing the _verdict_. Passing an export format defaults them to lean — verdicts only — because the usual reason to export is to forward bytes rather than to read a report. Ask for the values, transforms or defaults back explicitly if you want them — `DocValues` / `DocTransforms` / `DocDefaults` in Go, `DOC_VALUES` / `DOC_TRANSFORMS` / `DOC_DEFAULTS` in Python and TypeScript, `DocFlags::VALUES` / `DocFlags::TRANSFORMS` / `DocFlags::DEFAULTS` in Rust. A plain `rows` call is the all-flags spelling and stays byte-identical to what it always returned.

## Next

- [`transformations.md`](transformations.md) — what the accepted rows were silently changed into.
- [`settings.md`](settings.md) — where `input_format_allow_errors_ratio` sits among the channels.
- [`filters.md`](filters.md) — asking a boolean question of the same body.
