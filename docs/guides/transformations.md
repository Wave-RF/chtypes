# Transformations — the silent-change report

ClickHouse never says *"I changed your value."* It accepts the row and stores something else. `256` into a `UInt8` is stored as `0`, `'2300-01-01'` into a `Date` is clamped, an out-of-domain `Enum` element is coerced — and the INSERT returns success every time.

`transformed` is chtypes' report of exactly that, per row, per column, with a name for what happened. It is the **one derived answer** in the whole system: every other verdict is relayed from ClickHouse's own code, but ClickHouse has no "what did you change" channel to relay, so chtypes computes it by parsing the value a second time through a widened reference type and comparing. That derivation is why the product exists.

## The shape

An accepted row carries three lists, and they answer three different questions.

| list | question | must the caller act? |
|---|---|---|
| `transformed` | what did ClickHouse silently change? | show it, or you are lying to a user |
| `substituted` | which volatile DEFAULTs were resolved here? | **yes** — echo them into the real INSERT |
| `computed` | what did MATERIALIZED columns evaluate to? | no, informational |

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

| | |
|---|---|
| numeric domain | `overflow_wrap`, `decimal_truncate`, `lossy_numeric`, `float_precision` |
| time | `date_clamp`, `datetime_wrap`, `date_shift`, `ttl_expired`, `ttl_column_expired` |
| null handling | `null_to_default`, `null_loss` |
| strings and structured types | `fixedstring_pad`, `emptied`, `element_changed`, `enum_coerce`, `duplicate_key_dropped`, `value_changed` |
| identifiers | `uuid_mangle`, `ip_mangle` |
| the loud one | `poisoned` |

> `default_materialized` is spelled with an `s`. It is a frozen wire constant rather than prose, so it stays as the artifact emits it — do not "correct" it in code or in a comparison.

**`poisoned` is the one to wire an alert to.** It means the stored value is not merely different but meaningless — the row was accepted and what landed cannot be read back as what was sent. A batch carrying one gets the outcome `accepted_poisoned` rather than `accepted`, which exists precisely so a caller can branch on it without scanning the reasons.

## Substituted DEFAULTs, and the mistake they exist to prevent

A column with a volatile DEFAULT — `now()`, `now64(n)`, `today()`, `yesterday()` — has no value until something evaluates it. chtypes evaluates it **here**, once per batch, so the preview it gives you is a complete row.

That creates an obligation. **Send every substituted column as an explicit value in the real INSERT.** If you do not, the server evaluates `now()` at its own instant, and the row it stores is not the row you previewed. The gap is small and it is real, and it is worst in exactly the case you care about: a user staring at a preview of what is about to be written.

```
preview at 10:30:00.120  ts = 2026-01-15 10:30:00
INSERT   at 10:30:01.4   ts = 2026-01-15 10:30:01   ← what actually landed
```

For tests, pin the instant so the answer is reproducible — the per-call setting `chtypes_now_epoch_nanos` takes a 19-digit nanosecond epoch, as a **string** ([`settings.md`](settings.md) on why it must never be a number). One batch is one clock instant, by construction, so every volatile DEFAULT in a body resolves to the same value.

Two related settings bound the same clock: `chtypes_clock_offset_nanos` carries a measured server-minus-client offset, and `chtypes_max_clock_skew_nanos` refuses substitution altogether past a budget you set, answering `unsupported` rather than substituting a value you would not trust.

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
