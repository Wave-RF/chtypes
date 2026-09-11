# Filters, query parameters and the block twin

A filter compiles **one boolean expression** against a schema's physical columns and answers it per row, using ClickHouse's own comparison functions. It is the read-side twin of the insert path: same rows, same artifact, different question.

> **Enforcement gate.** No read-side security may be enforced on this surface until the WHERE-truth rig gates green. Until then a filter is shadow and replay only — compare it against your existing enforcement, do not replace it.

## WHERE-side semantics, which are not insert-side semantics

This is the single most important thing on the page. `x = 256` over a `UInt8` column is **false for every row**, because a comparison _promotes_ the constant. It does not wrap it to `0` and match every genuine zero, which is exactly what the insert path does to the same literal.

```
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
  if (!isAnswer(v)) throw new FailClosed();   // 'error' or 'decline'
  console.log(i, v === 'true');
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

### Size the brace type for the value's domain

This is the trap, and it is the mirror image of the promotion rule above.

A bound value is deserialized by the **brace type's own reader**, which **wraps** an out-of-domain integer. `{p:UInt8}` given `"256"` binds `0` — and then matches every genuine zero in your data. Meanwhile the same constant written as a literal in the expression promotes and matches nothing.

```
x = 256        (literal)      →  never true
x = {p:UInt8}  bound "256"    →  binds 0, matches every real zero
```

That behavior is uniform across every supported line and matches a real server, so it is not a bug to route around — it is a sizing decision you have to make. **Size the type for the tenant-supplied domain** (`{p:UInt64}`, `{p:String}`) or validate the value before you bind it. A too-narrow parameter type silently matches the wrong rows, which on a tenant boundary is the worst failure available.

Malformed spellings do refuse loudly with the server's own **457** — `"-1"`, `"+7"` and `"007"` as a `UInt8` are all errors — and an empty string refuses with **32**. It is only the in-range-for-the-wire, out-of-range-for-the-type case that wraps.

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
