# Filters, query parameters and the block twin

A filter compiles **one boolean expression** against a schema's physical columns and answers it per row, using ClickHouse's own comparison functions. It is the read-side twin of the insert path: same rows, same artifact, different question. The **block twin** is its parse-once variant: parse a body into a block once, then evaluate several filters against that same block.

> **Enforcement gate.** A filter verdict is enforcement-grade against a ClickHouse server of the same line on the same operating system and architecture, for the `(line, platform)` pairs a CHANGELOG entry has lifted; `26.3` on `darwin-arm64` is not covered (a documented platform limitation), and a server that refuses every query is outside the guarantee. See [`limitations.md` → Filters are enforcement-grade against a same-platform server, per lifted (line, platform)](../limitations.md#filters-are-enforcement-grade-against-a-same-platform-server-per-lifted-line-platform).

## WHERE-side semantics, which are not insert-side semantics

This is the single most important thing on the page. `x = 256` over a `UInt8` column is **false for every row**, because a comparison _promotes_ the constant. It does not wrap it to `0` and match every genuine zero, which is exactly what the insert path does to the same literal.

```text
insert side:   256 into UInt8   →  stored as 0            (overflow_wrap)
WHERE side:    x = 256          →  false, for every row   (promotion)
```

So never reuse insert-side coercion to fold a `WHERE` constant. The filter surface exists so you do not have to.

## Writing a filter's result type

**A filter's top-level result must be `UInt8`/`Bool`, an integer up to 64 bits, `Float32`/`Float64`, or `Nullable`/`LowCardinality` of one of those.** That is a real server's own rule (`canBeUsedInBooleanContext()` on every supported line), and 1.0 applies it the same way: a filter whose result is any other type is refused at compile with error 59, before any row is read. Wrap anything else in an explicit comparison instead of filtering on the bare column:

```text
d != toDate(0)            not: d
toInt128(x) != 0          not: x            (x Int128)
e8 = 'a'                  not: e8           (e8 an Enum)
```

1.0 enforces this the way a real server does, on every supported line (`26.3`, `26.7`, `26.8`, `26.9`). 0.x answered such a result with a C-style truthiness instead; 0.x is retired, and 1.0 ships only the supported lines.

**Each row is decided with a real server's own truthiness.** `0.5`, `-0.5` and `NaN` keep a row, as a server's `static_cast<bool>` does, and `NULL` does not. Prefer an explicit comparison anyway — `x != 0` rather than a bare `x` — so the intent is visible.

## Four verdicts, two of which are not answers

| verdict   | meaning                                                                   |
| --------- | ------------------------------------------------------------------------- |
| `true`    | the predicate is non-NULL and non-zero for this row                       |
| `false`   | it is not                                                                 |
| `error`   | the predicate **threw** on this row — a real server fails the whole query |
| `decline` | this library declines to answer for this row                              |

**A verdict counts only when the evaluation's outcome is `ok` (a filter result) or `accepted` (a batch); otherwise treat every verdict as `decline`, whatever the verdict string holds.** Under those outcomes, **`error` and `decline` are not answers, and an enforcing caller must fail closed on both.** Collapsing either into "false" is the bug this vocabulary exists to prevent: "the predicate blew up" and "the predicate is false" lead to opposite decisions when the predicate is a security boundary.

**For a conversion such as `toInt64(x) > N` over a `String` column, when several rows throw, the server reports the first one.** It fails the query with the error of the throwing row that comes first in the body's row order. To reproduce that error, take the `error` row with the lowest row index and read its entry in the result's `errors` list. Pick the row by its index, not by the order of the `errors` list. The artifact producer measured this on `toInt64` over `String` and `LowCardinality(String)`, where rows throw 6 and 32: 17 cases on 12 lines, each against a server pinned to that line's exact version, and all 204 agreed. It also read the same order in the server's conversion path in the source at the oldest and newest served lines. That other kinds of expression throw in the same order is its reading of how the server evaluates, not something it measured. The rule covers a body the server takes as one block, which is the only kind the per-row verdicts describe. **A `decline` row can hide the answer:** if a declined row comes before that `error` row, the server's error may have come from the declined row, and the result cannot tell you which.

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

That behavior is uniform across every supported line and matches a real server, so it is not a bug to route around, and the fix is not a better-sized brace type — binding the column's own narrow type at any width is what reintroduces the wrap above. Binding `{p:String}` instead, with a canonical spelling of the value, moves the comparison onto the coercion path: for a **narrow** integer column — `UInt8`, `UInt16`, `UInt32`, `Int8`, `Int16` or `Int32` — a canonical out-of-domain value **below 2^64** answers a clean `false`, matching nothing, rather than wrapping into a real row.

**That "clean false" promise holds only below 2^64 — it is not a general recipe for an untrusted value**, and the boundaries below were measured by the artifact producer against live servers across every served ClickHouse line. They are part of what the recipe means, not a caveat appended to it:

- **The wrap returns at 2^64, on every integer width — narrow columns included.** A bound value at or above 2^64 wraps **modulo 2^64** before the comparison runs, whatever the column's own width is: `u8 = {p:String}` bound `"18446744073709551616"` (2^64) matches the row holding `0`, exactly as if the column were `UInt64`; `u32 = {p:String}` bound `"18446744073709551621"` (2^64 + 5) matches the row holding `5`. The "clean false" promise above holds only for a canonical out-of-domain value **below** 2^64 — at or above it, the recipe fails silently on a `UInt8` column exactly as it does on a `UInt64` one. **`[U]Int128` and `[U]Int256` columns wrap at their own width instead of at 2^64** — `u128 = {p:String}` wraps modulo 2^128, `u256 = {p:String}` modulo 2^256 — so the boundary to check is the compared column's own width when it is 128 bits or wider, and 2^64 otherwise. This governs ordering comparisons too: `u8 > {p:String}` bound a value at or above 2^64 admits rows depending on where the wrapped value lands, not on the value's true magnitude. Binding `String` **moves** the wrap from the substitution side to the comparison side; it does not remove it, at any width.
- **It depends on the value's spelling, not just its magnitude.** A clean `false` is what a _canonical_ out-of-domain value gets. Against a narrow integer column, `"007"`, `"+7"`, `" 7"`, `"7.0"`, `"-1"` and `"abc"` each throw code **53** instead, and `""` throws **32** — exactly the spellings a gateway forwards from a caller unvalidated. Validate the value's spelling as canonical before binding it; the outcome is `false` or an error depending on spelling, never uniformly `false`.
- **It does not extend to ordering comparisons even below 2^64.** `u8 < 256` (literal) admits every row; `u8 < '256'` (bound `String`) admits none — a failed conversion becomes a constant for the whole comparison, so `<` cannot admit what `=` would not. Do not carry the plain bind-`String` recipe to `<`, `<=`, `>` or `>=`; the round-trip form below is the one that does extend to them.

Two error channels are in play here, and binding `String` moves a value between them rather than exempting it from both. **Substitution time**, before any row is read, is where the declared brace type's own reader deserializes the bound value: `{p:UInt8}` bound `"-1"`, `"+7"` or `"007"` is the server's own **457**, and an empty string is **32**. **Comparison time**, after substitution has already succeeded, is where the code-53 cases above are thrown, against a narrow column. Binding `String` takes a value out of the substitution channel — it does not make a bad spelling valid, it changes when and how that spelling is rejected.

`String` parameter values follow ClickHouse's own escaped-text field rules: a backslash, a tab, a newline and a carriage return must each be escaped in the value you bind. A value ending in a trailing backslash is refused with the server's own **code 25**. Skip the escaping and the failure differs by character, and neither one is a clean rejection you can rely on catching: a raw, unescaped tab or newline inside the bound value is the server's own **code 457**; an unescaped backslash ahead of an ordinary character is read as if it were one of the escapes above — `a\b` parses as `a` followed by a backspace, not the three characters you sent — and the comparison then silently answers `false`, with no error at all. Escape the backslash itself first, or the reader decides for you which escape you meant.

An `Array(String)` parameter literal follows the same quoted-element rule as a scalar `String`: only `'` and `\` are escaped inside each element, with no additional scalar encoding layered on top.

### The round-trip form — the recipe for an untrusted value

Every bound-`String` trick above is bounded by width, by magnitude, or by comparison operator. The recipe that is not — safe against an untrusted value at any magnitude, against an integer column of any width, for `=` and for ordering alike — casts the bound value to the column's own type and round-trips it back through `toString`, discarding anything that does not survive the round trip exactly:

```text
c OP if(toString(accurateCastOrNull({p:String}, 'T')) = {p:String}, accurateCastOrNull({p:String}, 'T'), NULL)
```

`accurateCastOrNull({p:String}, 'T')` already answers `NULL` for a value that overflows `T` — but only up to 64 bits; on `[U]Int128`/`[U]Int256` it silently wraps instead of refusing, an upstream ClickHouse behavior, not a chtypes divergence. The `toString(...) = {p:String}` half is what closes that gap: it does not trust `accurateCastOrNull`'s own precision, it re-serializes whatever the cast produced and checks that text against the exact string you bound, so a value that wrapped anywhere — 64-bit or 256-bit — fails the round trip and the comparison sees `NULL` in place of the wrapped row, whatever `OP` is.

One narrow width:

```text
u8 = if(toString(accurateCastOrNull({p:String}, 'UInt8')) = {p:String}, accurateCastOrNull({p:String}, 'UInt8'), NULL)
```

bound `"18446744073709551616"` (2^64): the cast wraps to `0`, `toString(0)` is `"0"`, which does not equal the bound text, so the whole expression is `NULL` — no row matches. In-range values round-trip unchanged, and using `u8` as a primary key works exactly as it did before.

One 256-bit width:

```text
u256 = if(toString(accurateCastOrNull({p:String}, 'UInt256')) = {p:String}, accurateCastOrNull({p:String}, 'UInt256'), NULL)
```

measured clean at every served ClickHouse line, on both chtypes and a live server: in-range answers are unchanged, an out-of-domain value at any magnitude answers `NULL` rather than wrapping into a real row, and — unlike a bound `String` alone — this form holds for `<`, `<=`, `>` and `>=` as well as `=`.

This is the recipe to reach for whenever the bound value's magnitude is not already validated as in-domain and canonical. The plain bind-`String` recipe above remains true as far as it goes — a canonical, validated, below-2^64 value against a narrow column does get a clean `false` — but it is not a substitute for the round trip once the value's magnitude is untrusted.

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

Per-row parse failures live **inside** the block and answer `decline`. A call-level failure yields no block at all, and no partial answers. That includes a body the server cannot form a block from: a failed or declined `Values` body, or a multi-row body whose deprecated `Object('json')` rows cannot be finalized together (`122`, `645`, or `53` when a small `max_insert_block_size` splits the rows across blocks). `rows` answers such a body `rejected` with the server's code, `rows_read` 0 and no verdicts, and `parse_block` raises with that code. Rows are evaluated independently only when the server would take the body as one block, because a block that cannot exist has no per-row truth.

## Exporting only the rows a filter admits

Attach a compiled filter to `rows`'s export channel ([`batches.md` → Exporting the accepted rows as wire bytes](batches.md#exporting-the-accepted-rows-as-wire-bytes)) and one parse answers both questions at once: the same four verdicts as above, one per row, and — for the rows whose verdict is `t` — the exported bytes. This is the row-level-security shape: compile a tenant's predicate once, reuse the handle across batches, and export only the rows it admits. It is under the enforcement gate at the top of this page: enforcement-grade only for a `(line, platform)` pair a CHANGELOG entry has lifted, against a same-platform server — see [`limitations.md` → Filters are enforcement-grade against a same-platform server, per lifted (line, platform)](../limitations.md#filters-are-enforcement-grade-against-a-same-platform-server-per-lifted-line-platform) for the criterion.

<details open><summary><b>Go</b></summary>

```go
f, err := schema.CompileFilter("tenant = {t:String}", chtypes.WithFilterParams(map[string]string{"t": "acme"}))
if err != nil {
	log.Fatal(err)
}
defer f.Close()

batch, err := schema.Rows(chtypes.JSONEachRow, body, chtypes.WithExport(chtypes.JSONCompactEachRow), chtypes.WithRowFilter(f))
// batch.Payload holds the bytes of only the rows whose verdict was 't'
```

</details>

In 1.0 this is one `rows` call with two options, `WithExport` and `WithRowFilter` in Go, `export=` and `row_filter=` in Python, `exportFormat` and `rowFilter` in TypeScript, `export` and `filter` in Rust ([`reference/bindings-v1.md` §2, The call options](../reference/bindings-v1.md#the-call-options)). 0.x's `RowsExportWith` and its spellings are deleted ([§7](../reference/bindings-v1.md#7-what-v0-api-is-deleted-and-why)). The concept is identical across all four: one call, one parse, a verdict per row, bytes for the `t` rows.

## A filter answers in the zone it was created in

A filter's zone is its own, fixed when it is compiled: its `WHERE` runs in the zone of the settings (`session_timezone`) the filter was compiled with, the way a server's `SELECT ... WHERE` runs in its own session. The zone in an evaluation's settings decides only how the body is parsed. A filter in one zone over a body parsed in another is an ordinary input, answered the way a server answers it, and never refused. One parsed block can serve many filters compiled under different zones. The rule and the per-binding spellings are [`reference/bindings-v1.md` §2, The call options](../reference/bindings-v1.md#the-call-options).

Each row's document gains `verdict` — the same `t`/`f`/`e`/`d` as above — beside its own parse `outcome`; the two are independent facts, and neither one replaces the other. A row whose own `outcome` is not `accepted` — `skipped`, the row that aborted a strict batch, `accepted_poisoned`, an accepted row missing a stored wire column — is answered `d`, carrying that row's own error; a row the predicate itself declines at evaluation time is also `d`, and both cases add `verdict_code` and `verdict_err` beside `verdict`, exactly as an `e` does. Before the artifact producer's relink served at `chtypes_build` 1790845279, the library left `verdict_code`/`verdict_err` at `0`/`""` on a non-accepted row's `d` verdict — this contract and the library's document now agree, by design. One exception, `reported by the artifact producer` (24.8/25.x only — the only lines where an out-of-domain Enum `DEFAULT` reaches `accepted_poisoned` rather than refusing the `CREATE TABLE`, [`transformations.md`](transformations.md#an-out-of-domain-enum-default-follows-the-server)): an `accepted_poisoned` row's `verdict_err` stays `""`, because that row's own message is itself empty — `verdict_code` still carries the server's readback code (`691` on the 25.x lines). Never read an empty `verdict_err` on a `d` verdict as "nothing is wrong"; read `verdict_code` instead.

Three consequences follow, and a consumer meets each of them:

- **A batch whose own `outcome` is not `accepted` exports nothing, filter or no filter — and, as of the artifact producer's relink served at `chtypes_build` 1790845279, reports `rows_passed` and `rows_cut` as both `0`.** Measured by the artifact producer: a `CONSTRAINT … CHECK` violator anywhere in the batch rejects the whole batch with code **469**, and the table stores nothing, so no passing row's bytes flow either, whatever its own verdict would have been. There is no path by which such a batch exports its admitted rows; attaching a filter does not create one. The same rejection used to still count an earlier row's own `accepted` outcome and `t` verdict toward `rows_passed` — a strict (no `input_format_allow_errors_*`) batch aborted by a later bad row measured `rows_passed: 1` despite an overall `outcome` of `rejected` — so a caller keying "exported" off `rows_passed > 0` could publish from a batch that stored and exported nothing. By design, since that relink: both counts are gated on the BATCH's own `outcome`, not only on each row's.

This `verdict_code`/`verdict_err` and `rows_passed`/`rows_cut` fix applies only to builds at `chtypes_build` 1790845279 or later, on the supported lines (`26.3`, `26.7`, `26.8`, `26.9`). A served, unsupported (retired) line never gets a new build or a new ABI revision, so it keeps the pre-relink behavior permanently — `26.6`'s newest build, `1790767905`, predates this relink and was never republished.

> ⚠️ **Verdicts index THIS call's rows — never another call's.** Measured by the artifact producer: a filtered export call and a plain `chs_filter_rows` / block-parse call reading the identical body can come back with a **different number of verdicts**, because the filtered export call stops at whichever row aborts its own parse, while the other reads the whole body regardless. Zipping one call's verdicts against another call's rows is exactly the mistake that costs a caller its own withhold-on-mismatch check — index verdicts only against the rows of the SAME call that produced them.

- **`d` here is this library's convention, not the server's answer.** Measured by the artifact producer: over a CONSTRAINT-bearing schema, a row that never reaches storage never reaches the predicate either — the server has no verdict to offer for it at all. Reporting that as `d` rather than `f` is a choice this library makes, not a fact the server hands back; `f` ("not in the admitted set") is a defensible alternative reading of the same measurement, just not the one made here. Because export emits bytes only for `t` and excludes both `e` and `d`, a reader who treats `d` as "what the server said" may derive behavior from a value that is actually this library's own to define.

The export mechanics you already know from the plain export channel carry over unchanged: `row_spans` stays index-aligned with the batch's rows, and a cut row — `f`, `e` or `d` — gets `{0, 0}` exactly as a non-accepted row does today. Two counts join the document, `rows_passed` (accepted rows whose verdict is `t`) and `rows_cut` (accepted rows with any other verdict); `rows_passed + rows_cut` is the number of accepted rows **on an accepted batch** — see the bullet above for what a non-accepted batch reports instead. A row whose own `outcome` is not `accepted` — `skipped`, the row that aborted a strict batch, `accepted_poisoned` — is answered `d` (above) and sits in **neither** count: `len(rows)` decomposes into `rows_passed + rows_cut` plus the non-accepted rows, not just the first two. A filter compiled over a different schema handle than the one the call runs against rejects the whole call, loudly — the same cross-schema rule as [Lifetimes](#lifetimes), below. A filter attached with no export requested is legal: you get verdicts and no bytes, exactly as `f.Rows` already gives you above.

## A server's settings, and the ones a filter declines (ABI v2)

A filter's `WHERE` sees the settings layered under it: the build's own, the process defaults, the server profile, the schema's own settings and the filter's own, highest last, with `session_timezone` at every layer on the zone path above. Each one is honored (applied to the `WHERE`), passed through (accepted and ignored), or declined, and the library decides which, never a binding. Every layer known when the schema compiles (the process defaults as the compile captured them, the server profile and the schema's own settings) is judged then: a setting there that this build's filters do not honor is listed in the schema description's top-level `filter_declined_settings` (Go `SchemaDescription.FilterDeclinedSettings`, Python `filter_declined_settings`, TypeScript `filterDeclinedSettings`, Rust `filter_declined_settings`), once per name, with its tier from [the WHERE-settings lists](../../spec/abi-v2/where-settings/README.md) and its layer (`defaults`, `server` or `schema`, the highest that sets it), and compiling any filter on that schema raises the binding's `UnsupportedError` (`CHS_DECLINED`), naming each setting and its layer. A declined setting from the defaults or the schema's own settings used to be named only at evaluation, and now fails the compile. A declined setting from the filter's own settings, or from an evaluation's, is still named at evaluation: in the filter result's `unsupported_settings`, with every verdict `d`, and in the batch's own `unsupported_settings` when the filter rides along on `rows`, which makes that batch `unsupported`. A consumer that streams rows instead of asking the server must refuse a tenant if `filter_declined_settings` is non-empty, or a `result-content` setting is changed.

## Lifetimes

A filter and a block must come from the **same schema handle**. A mismatched pair from one library answers `rejected` with code 1002, loudly; a pair from two different libraries is refused before any C call is made.

Neither may outlive its schema, and each binding enforces that in its own idiom:

|            |                                                                                                             |
| ---------- | ----------------------------------------------------------------------------------------------------------- |
| Go         | the schema's `Close` frees open filters and blocks first, and so does its GC cleanup when nothing closed it |
| Python     | `Schema.close()` frees open filters and blocks first                                                        |
| TypeScript | `Schema#close` frees them first; `using` nests naturally — block, filter, schema                            |
| Rust       | a `Filter` and a `Block` **borrow** their `Schema`, so the wrong free order does not compile                |

In Go a filter call is also a use of its schema handle, so two filters over one schema never run concurrently. Parallelism comes from more schemas, not from sharing one.

Parse-once does not open enforcement earlier: the block twin is under the same gate as the rest of this page.

## Next

- [`batches.md`](batches.md) — the insert-side answer over the same body.
- [`multi-version.md`](multi-version.md) — handle lifetimes and thread-safety in general.
- [`../limitations.md`](../limitations.md) — including why constants are not payloads.
