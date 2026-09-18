# Filters, query parameters and the block twin

A filter compiles **one boolean expression** against a schema's physical columns and answers it per row, using ClickHouse's own comparison functions. It is the read-side twin of the insert path: same rows, same artifact, different question.

> **Enforcement gate.** No read-side security may be enforced on this surface until the WHERE-truth rig gates green. Until then a filter is shadow and replay only — compare it against your existing enforcement, do not replace it.

## WHERE-side semantics, which are not insert-side semantics

This is the single most important thing on the page. `x = 256` over a `UInt8` column is **false for every row**, because a comparison _promotes_ the constant. It does not wrap it to `0` and match every genuine zero, which is exactly what the insert path does to the same literal.

```text
insert side:   256 into UInt8   →  stored as 0            (overflow_wrap)
WHERE side:    x = 256          →  false, for every row   (promotion)
```

So never reuse insert-side coercion to fold a `WHERE` constant. The filter surface exists so you do not have to.

## Four verdicts, two of which are not answers

| verdict   | meaning                                                                   |
| --------- | ------------------------------------------------------------------------- |
| `true`    | the predicate is non-NULL and non-zero for this row                       |
| `false`   | it is not                                                                 |
| `error`   | the predicate **threw** on this row — a real server fails the whole query |
| `decline` | this library declines to answer for this row                              |

**`error` and `decline` are not answers, and an enforcing caller must fail closed on both.** Collapsing either into "false" is the bug this vocabulary exists to prevent: "the predicate blew up" and "the predicate is false" lead to opposite decisions when the predicate is a security boundary.

Each binding gives you the distinction as a predicate rather than making you remember it:

<details open><summary><b>Go</b></summary>

```go
f, err := schema.CompileFilter("tenant = {t:String}", chtypes.WithFilterParams(map[string]string{"t": "acme"}))
if err != nil {
	log.Fatal(err)
}
defer f.Close()

res, err := f.Rows(chtypes.JSONEachRow, body, nil)
if err != nil || res.Outcome != chtypes.FilterOK {
	return errFailClosed
}
for i, v := range res.Verdicts {
	if !v.Answered() {
		return errFailClosed // error or decline
	}
	fmt.Println(i, v == chtypes.VerdictTrue)
}
```

</details>

<details><summary><b>Python</b></summary>

```python
from chtypes import FilterOutcome, Verdict

with schema.compile_filter("tenant = {t:String}", params={"t": "acme"}) as f:
    res = f.rows(Format.JSON_EACH_ROW, body)

if res.outcome is not FilterOutcome.OK:
    raise FailClosed
for i, v in enumerate(res.verdicts):
    if v not in (Verdict.TRUE, Verdict.FALSE):
        raise FailClosed        # error or decline
    print(i, v is Verdict.TRUE)
```

</details>

<details><summary><b>TypeScript</b></summary>

```ts
import { isAnswer } from '@wavehouse/chtypes';

const f = schema.compileFilter('tenant = {t:String}', { params: { t: 'acme' } });
const res = f.rows(Format.JSONEachRow, body);

if (res.outcome !== 'ok') throw new FailClosed();
res.verdicts.forEach((v, i) => {
  if (!isAnswer(v)) throw new FailClosed();   // 'e' or 'd'
  console.log(i, v === 't');
});
f.close();
```

</details>

<details><summary><b>Rust</b></summary>

```rust
use chtypes::{FilterOutcome, Verdict};

let f = schema.compile_filter("tenant = {t:String}", &[("t", "acme")])?;
let res = f.rows(Format::JsonEachRow, body, NO_SETTINGS)?;

if res.outcome != FilterOutcome::Ok {
    return Err(fail_closed());
}
for (i, v) in res.verdicts.iter().enumerate() {
    if !v.answered() {
        return Err(fail_closed());   // Error or Decline
    }
    println!("{i} {}", *v == Verdict::True);
}
```

</details>

The call itself has its own outcome, separate from the per-row verdicts. On anything but `ok` the verdict list is not populated, so check it first.

Clock reads are refused at **compile** time, as a decline: a predicate whose truth depends on when you ask it is not a predicate this surface will answer.

## Query parameters — the only safe way to put a value in an expression

An expression may carry `{name:Type}` query parameters, bound as a map (a slice of pairs in Rust) of **string** values, exactly as the server's own parameter channels carry them.

Substitution is the server's own `ReplaceQueryParameterVisitor`: each value is deserialized by the **declared type's own reader** and injected as a typed literal _after_ SQL parsing. A value is therefore never SQL text.

> **Never hand-escape a value into the expression.** Injection safety here is by construction, and building the string yourself throws it away. A hostile value like `' OR 1=1 --` bound as a parameter compares as exactly that literal string.

An **unbound** parameter is the server's own **456** ("Substitution `name` is not set"); an **unparseable** value is the server's own **457**. Both arrive as schema errors, verbatim. A bound name the expression never uses is simply ignored.

### Bind `String` for narrow integer columns

This is the trap, and it is the mirror image of the promotion rule above.

A bound value is deserialized by the **brace type's own reader**, which **wraps** an out-of-domain integer. `{p:UInt8}` given `"256"` binds `0` — and then matches every genuine zero in your data. Meanwhile the same constant written as a literal in the expression promotes and matches nothing.

```text
x = 256        (literal)      →  never true
x = {p:UInt8}  bound "256"    →  binds 0, matches every real zero
```

That behavior is uniform across every supported line and matches a real server, so it is not a bug to route around, and the fix is not a better-sized brace type — binding the column's own narrow type at any width is what reintroduces the wrap above. For a **narrow** integer column — `UInt8`, `UInt16`, `UInt32`, `Int8`, `Int16` or `Int32` — bind `{p:String}` instead, with a canonical spelling of the value: the comparison then coerces the bound `String` against the column's type, and an out-of-domain canonical value answers a clean `false`, matching nothing, rather than wrapping into a real row. That recommendation is bounded, not unconditional, and the boundaries below were measured by the artifact producer against live servers across eight served ClickHouse lines — they are part of what the recipe means, not a caveat appended to it:

- **It stops at 64 bits.** Comparison-time coercion promotes the column's own type before parsing the bound value, and `UInt64`/`Int64` have nothing wider left to promote to — so the same wrap this section opened with returns, on the comparison side instead of the substitution side. `u64 = {p:String}` bound `"18446744073709551616"` matches exactly the row holding `0`; `i64 = {p:String}` bound `"9223372036854775808"` matches the row holding `-9223372036854775808`. Binding `String` **moves** that hazard from narrow columns to wide ones; it does not remove it — do not bind `String` in place of `UInt64` or `Int64` on this reasoning.
- **It depends on the value's spelling, not just its magnitude.** A clean `false` is what a _canonical_ out-of-domain value gets. Against a narrow integer column, `"007"`, `"+7"`, `" 7"`, `"7.0"`, `"-1"` and `"abc"` each throw code **53** instead, and `""` throws **32** — exactly the spellings a gateway forwards from a caller unvalidated. Validate the value's spelling as canonical before binding it; the outcome is `false` or an error depending on spelling, never uniformly `false`.
- **It does not extend to ordering comparisons.** `u8 < 256` (literal) admits every row; `u8 < '256'` (bound `String`) admits none — a failed conversion becomes a constant for the whole comparison, so `<` cannot admit what `=` would not. Do not carry this recipe to `<`, `<=`, `>` or `>=`.

Two error channels are in play here, and binding `String` moves a value between them rather than exempting it from both. **Substitution time**, before any row is read, is where the declared brace type's own reader deserializes the bound value: `{p:UInt8}` bound `"-1"`, `"+7"` or `"007"` is the server's own **457**, and an empty string is **32**. **Comparison time**, after substitution has already succeeded, is where the code-53 cases above are thrown, against a narrow column. Binding `String` takes a value out of the substitution channel — it does not make a bad spelling valid, it changes when and how that spelling is rejected.

`String` parameter values follow ClickHouse's own escaped-text field rules: a backslash, a tab, a newline and a carriage return must each be escaped in the value you bind. A value ending in a trailing backslash is refused with the server's own **code 25**.

### Bounding the compile path

The compiled handle **bakes the parameter values in**: identity is per `(schema, expression, params)`. A caller compiling filters from tenant-influenced values must therefore bound two things, and this is normative rather than advisory:

- **a bounded cache**, keyed on (schema generation, expression, params hash) — an unbounded one is a memory denial of service;
- **a per-principal compile throttle** — an unmetered compile path is a CPU denial of service.

One naming trap, and it belongs to the transport rather than to this library: on a real server's **TCP** channel, a parameter _named_ `limit` or `offset` fails at the protocol layer with code 26 even when unused, because parameters ride in a Settings block there. HTTP is fine, and this library matches the HTTP substitution semantics. Avoid those two names for anything that will ever cross TCP.

## The block twin — parse once, evaluate K times

`Filter.rows(body)` parses the body and evaluates. When you have K filters and one event — the live-SSE shape — that parse happens K times for no reason.

Splitting it: `parse_block` does the parse once and hands back a block, and `Filter.eval(block)` answers the same result with no re-parse.

<details open><summary><b>Go</b></summary>

```go
blk, err := schema.ParseBlock(chtypes.JSONEachRow, body, nil)
if err != nil {
	log.Fatal(err)
}
defer blk.Close()

for _, f := range filters {
	res, err := f.Eval(blk) // same FilterResult f.Rows(body) would give
	_ = res
	_ = err
}
```

</details>

<details><summary><b>Python</b></summary>

```python
with schema.parse_block(Format.JSON_EACH_ROW, body) as blk:
    for f in filters:
        res = f.eval(blk)   # same FilterResult f.rows(body) would give
```

</details>

<details><summary><b>TypeScript</b></summary>

```ts
const blk = schema.parseBlock(Format.JSONEachRow, body);
for (const f of filters) {
  const res = f.eval(blk); // same FilterResult f.rows(body) would give
}
blk.close();
```

</details>

<details><summary><b>Rust</b></summary>

```rust
let blk = schema.parse_block(Format::JsonEachRow, body, NO_SETTINGS)?;
for f in &filters {
    let res = f.eval(&blk)?;   // same FilterResult f.rows(body) would give
}
```

</details>

The equivalence is normative: `eval(parse_block(body))` ≡ `rows(body)` for every verdict class. The one thing to know is that volatile DEFAULTs resolve against the **parse** call's clock instant, so pin `chtypes_now_epoch_nanos` if you need cross-call identity.

Evaluation is a pure function of (filter, block). It takes no settings, and it never consumes or mutates the block — that is what makes K evaluations over one block safe.

Per-row parse failures live **inside** the block and answer `decline`. A call-level failure yields no block at all, and no partial answers.

## Lifetimes

A filter and a block must come from the **same schema handle**. A mismatched pair from one library answers `rejected` with code 1002, loudly; a pair from two different libraries is refused before any C call is made.

Neither may outlive its schema, and each binding enforces that in its own idiom:

|            |                                                                                                  |
| ---------- | ------------------------------------------------------------------------------------------------ |
| Go         | the schema's `Close` frees open filters and blocks first, and finalizers run in dependency order |
| Python     | `Schema.close()` frees open filters and blocks first                                             |
| TypeScript | `Schema#close` frees them first; `using` nests naturally — block, filter, schema                 |
| Rust       | a `Filter` and a `Block` **borrow** their `Schema`, so the wrong free order does not compile     |

In Go a filter call is also a use of its schema handle, so two filters over one schema never run concurrently. Parallelism comes from more schemas, not from sharing one.

Parse-once does not open enforcement earlier: the block twin is under the same gate as the rest of this page.

## Next

- [`batches.md`](batches.md) — the insert-side answer over the same body.
- [`multi-version.md`](multi-version.md) — handle lifetimes and thread-safety in general.
- [`../limitations.md`](../limitations.md) — including why constants are not payloads.
