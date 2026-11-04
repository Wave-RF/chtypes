# Transformations — the silent-change report

ClickHouse never says _"I changed your value."_ It accepts the row and stores something else. `256` into a `UInt8` is stored as `0`, `'2300-01-01'` into a `Date` is clamped, an out-of-domain `Enum` element is coerced — and the INSERT returns success every time.

`transformed` is chtypes' report of exactly that, per row, per column, with a name for what happened. It is the **one derived answer** in the whole system: every other verdict is relayed from ClickHouse's own code, but ClickHouse has no "what did you change" channel to relay, so chtypes computes it by parsing the value a second time through a widened reference type and comparing. That derivation is why the product exists.

## The shape

An accepted row carries three lists, and they answer three different questions.

| list          | question                                    | must the caller act?                     |
| ------------- | ------------------------------------------- | ---------------------------------------- |
| `transformed` | what did ClickHouse silently change?        | show it, or you are lying to a user      |
| `substituted` | which volatile DEFAULTs were resolved here? | **yes** — echo them into the real INSERT |
| `computed`    | what did MATERIALIZED columns evaluate to?  | no, informational                        |

A `Transform` names the column, the input text, the stored text, and the reason. Reading one:

<details open><summary><b>Go</b></summary>

```go
for _, t := range row.Transformed {
	fmt.Printf("%s: %s -> %s (%s) lossy=%v\n", t.Column, t.Input, t.Stored, t.Reason, t.Lossy())
}
```

</details>

<details><summary><b>Python</b></summary>

```python
from chtypes import LOSSLESS_REASONS

for t in row.transformed:
    lossy = t.reason not in LOSSLESS_REASONS
    print(f"{t.column}: {t.input} -> {t.stored} ({t.reason}) lossy={lossy}")
```

</details>

<details><summary><b>TypeScript</b></summary>

```ts
import { isLossyReason } from '@wavehouse/chtypes';

for (const t of row.transformed) {
  console.log(`${t.column}: ${t.input} -> ${t.stored} (${t.reason}) lossy=${isLossyReason(t.reason)}`);
}
```

</details>

<details><summary><b>Rust</b></summary>

```rust
for t in &row.transformed {
    println!("{}: {} -> {} ({}) lossy={}", t.column, t.input, t.stored, t.reason, t.lossy());
}
```

</details>

The `stored` text is byte-exact: numbers keep their exact spelling rather than passing through a float, because a preview that rounds is not a preview. Each binding preserves that its own way — Rust hands back a `RawText` whose `as_str()` is fallible, Python a `RawNumber` for exact numerics, TypeScript the raw `bytes` beside the `text`.

## The reasons

There are 24 stable reason spellings, and they are wire constants: the artifact emits them and every binding passes them through unchanged. Spell them from your binding's constants (`chtypes.Reason*` in Go, `Reason` in Python and TypeScript, `chtypes::reason::*` in Rust) rather than as string literals.

Four of them are **lossless** — the stored value means the same thing, it is just spelled differently or was filled in:

`reformat` · `default_filled` · `zero_filled` · `default_materialized`

Every other reason is **lossy**: information the caller sent is not in the table. Each binding exposes the distinction as one predicate rather than making you keep the list — `Transform.Lossy()` in Go, `Transform::lossy()` in Rust, `isLossyReason(reason)` in TypeScript, `LOSSLESS_REASONS` in Python.

The lossy ones, by what they are about:

|                              |                                                                                                          |
| ---------------------------- | -------------------------------------------------------------------------------------------------------- |
| numeric domain               | `overflow_wrap`, `decimal_truncate`, `lossy_numeric`, `float_precision`                                  |
| time                         | `date_clamp`, `datetime_wrap`, `date_shift`, `ttl_expired`, `ttl_column_expired`                         |
| null handling                | `null_to_default`, `null_loss`                                                                           |
| strings and structured types | `fixedstring_pad`, `emptied`, `element_changed`, `enum_coerce`, `duplicate_key_dropped`, `value_changed` |
| identifiers                  | `uuid_mangle`, `ip_mangle`                                                                               |
| the loud one                 | `poisoned`                                                                                               |

> `default_materialized` is spelled with an `s`. It is a frozen wire constant rather than prose, so it stays as the artifact emits it — do not "correct" it in code or in a comparison.

**`poisoned` is the one to wire an alert to.** It means the stored value is not merely different but meaningless — the row was accepted and what landed cannot be read back as what was sent. A batch carrying one gets the outcome `accepted_poisoned` rather than `accepted`, which exists precisely so a caller can branch on it without scanning the reasons.

## A NULL into a non-Nullable Enum is poisoned

A JSON `null` (JSONEachRow) or a TSV `\N` read into a column that is not `Nullable` stores the column's default, and `null_input` is true on that entry. For an `Enum8('a' = 1, 'b' = 2)` the default the server stores is 0, which is outside the Enum. The server accepts the insert and cannot read the row back, so the answer is the outcome `accepted_poisoned` with code `691`, the column's `poison` is true (and `null_input` too), and `transformed` names it with the reason `poisoned`. `measured` on a real library build, all four lines and all four bindings.

- **Every other type in the `null_input` set stores a readable default** (`0`, the empty string, the zero UUID, `1970-01-01 00:00:00`), so its entry carries `null_input` true and `poison` false.
- **A row that supplies an in-domain value is unaffected**: `poison` and `null_input` are both false.

## An out-of-domain Enum DEFAULT follows the server

An `Enum` column whose literal `DEFAULT` is outside the Enum's domain is a different case from the NULL above, and it follows the server's `CREATE`. On the supported 26.x lines the server refuses such a table, so compiling the schema fails with a `SchemaError`, code `691` (or `70` for a literal out of the Enum's integer range, such as `300`). With the `load` posture the schema compiles, but a row that leaves the column out is `rejected` with code `691` and `poison` false: no row there is poisoned. Use the NULL case above to see `poison`.

Branch on the outcome, never on the code: which code a poisoned row carries is the server's. `dup_dropped` is the one other flag of this kind: it says that the reader dropped a duplicate key for the column. At default settings it is set by a repeated top-level JSONEachRow key that arrives after every column has been read (`{"a":1,"a":2}` into `a Int32`): the reader skips the repeat, so the server stores the first value, `dup_dropped` is true, and a `duplicate_key_dropped` transform is reported. A repeat that arrives while some column is still unread (`{"a":1,"a":2,"b":3}` into `a Int32, b Int32`) is refused with code 117 instead. A repeated key inside a Map value is neither: both keys are kept and `dup_dropped` is false.

## Substituted DEFAULTs, and the mistake they exist to prevent

A column with a volatile DEFAULT — `now()`, `now64(n)`, `today()`, `yesterday()` — has no value until something evaluates it. chtypes evaluates it **here**, once per batch, so the preview it gives you is a complete row.

That creates an obligation. **Send every substituted column as an explicit value in the real INSERT.** If you do not, the server evaluates `now()` at its own instant, and the row it stores is not the row you previewed. The gap is small and it is real, and it is worst in exactly the case you care about: a user staring at a preview of what is about to be written.

```text
preview at 10:30:00.120  ts = 2026-01-15 10:30:00
INSERT   at 10:30:01.4   ts = 2026-01-15 10:30:01   ← what actually landed
```

For tests, pin the instant so the answer is reproducible — the per-call setting `chtypes_now_epoch_nanos` takes a 19-digit nanosecond epoch, as a **string** ([`settings.md`](settings.md) on why it must never be a number). One batch is one clock instant, by construction, so every volatile DEFAULT in a body resolves to the same value.

Two related settings bound the same clock: `chtypes_clock_offset_nanos` carries a measured server-minus-client offset, and `chtypes_max_clock_skew_nanos` refuses substitution altogether past a budget you set, answering `unsupported` rather than substituting a value you would not trust.

## The value-injection pattern, and the trap inside it

To stamp a server-side value — a tenant, a role claim — onto every row, compile one schema per `(table, role, claim)` with `DEFAULT '<claim>'`, and cache the compiled schema per key.

Quoting the claim into a valid ClickHouse string literal before it is spliced into that DDL text is **`QuoteLiteral`'s job, not yours** — `lib.QuoteLiteral(claim)` in Go, `quote_literal` / `quoteLiteral` / `quote_literal` in the other three. It is a passthrough over ClickHouse's own `quoteString`, so the escaping is the server's rather than a rule any binding spells, and its input is counted: a claim carrying a NUL byte is quoted correctly. Do not hand-roll it, and do not reach for `QuoteIdentifier` here — that one escapes an **identifier**, a table or column name, not a string value ([`../reference/bindings-v1.md` §2, The library](../reference/bindings-v1.md#the-library)).

⚠️ **Safe only when paired with a filter that checks the STORED value against the same claim, using a check that is itself safe against the claim's magnitude.** An out-of-domain claim into a narrow column wraps silently **at compile**: `UInt8 DEFAULT '256'` stores **0** — and an _unquoted_ literal wraps the same way, with no refusal to catch it: `UInt8 DEFAULT 256` also stores **0**, because 256 is a value ClickHouse can coerce, just not one in this column's domain. A literal it genuinely cannot coerce is a compile-time refusal instead: `UInt64 DEFAULT 'abc'` fails with the server's own code **6**, fail-closed. So the DEFAULT alone is not an isolation mechanism — the check on the stored value is what makes it one, and that check inherits the query-parameter hole documented on [`filters.md`](filters.md#the-round-trip-form--the-recipe-for-an-untrusted-value) if it binds the claim as a plain `{p:String}`: a claim at or above 2^64 defeats the DEFAULT's wrap and a naive check together — `tenant UInt8 DEFAULT '18446744073709551616'` stores `0`, and `tenant = {p:String}` bound that same claim answers `true`. The check is safe only when it uses the round-trip cast form from `filters.md`, or the claim is validated below 2^64 and in the column's domain before it is ever spliced into the DDL.

That wrap happens at **compile**, the moment the schema is built from the DDL text — a different site from the bind-side wrap documented on [`filters.md`](filters.md), where the same shape of out-of-domain value arrives as a query parameter instead of a DDL literal.

An alternative removes the literal, and the quoting question, entirely: `tenant String DEFAULT _tenant, _tenant String EPHEMERAL`, where the claim travels in the row itself and reaches the `DEFAULT` only through an explicit column list — the listed `EPHEMERAL` column's value is read and is in scope for the DEFAULT that references it, and is still never stored and never exported. This needs the explicit INSERT column list, the `columns` option of the row calls (`WithColumns` in Go, `columns=` in Python, `columns` in TypeScript, `RowOptions.columns` in Rust — see [`reference/bindings-v1.md` §2, The call options](../reference/bindings-v1.md#the-call-options)). The column list composes with the filtered-export channel in every binding — [`filters.md` → Exporting only the rows a filter admits](filters.md#exporting-only-the-rows-a-filter-admits) — so `tenant`'s DEFAULT reaches the filter on the same call that carries the EPHEMERAL column in.

`getSetting(...)` inside a compiled expression is not a second route around either check: this library declines it as `unsupported`, the same non-deterministic-expression declination a clock read gets, the custom setting it names is gated behind an `SQL_` prefix, and the server refuses the `CREATE` outright unless that named setting already exists. There is no value channel here beyond the parameter and `DEFAULT` paths already covered above.

## MATERIALIZED columns, and why they are not in `values`

`computed` carries what each MATERIALIZED column evaluated to. It is deliberately kept out of `values`, because `SELECT *` does not return a MATERIALIZED column and a preview that mixed it in would disagree with what a subscriber reading the table actually sees.

ALIAS columns are never reported at all. An `ALTER … MODIFY COLUMN a ALIAS <expr>` retroactively changes what already-inserted rows read back as, so an ALIAS is a fact about the schema at read time, not a fact about the row.

## Where the batch differs from the row

On a `BatchResult`, `transformed` folds in the storage layer's own verdicts — the ones that are properties of the write rather than of the parse, `ttl_expired` and `ttl_column_expired` among them. When `engineRows` is present it, not `rows`, is the stored truth: the MergeTree insert-time merge can collapse rows, and the batch reports what the table would end up holding.

Also worth knowing: `substituted` lives on the **row**, not on the batch, in every binding. A batch-level `substituted` does not exist — reach it through `batch.rows[i]`.

## Next

- [`batches.md`](batches.md) — the body-level picture, including what happens at the first bad row.
- [`settings.md`](settings.md) — the clock settings above, and the precedence rules around them.
- [`../limitations.md`](../limitations.md) — what chtypes declines to answer rather than guess.
