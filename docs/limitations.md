# Known limitations

Every item here is a deliberate decline or a stated boundary, not a bug queue. The rule underneath all of them is the same one: **chtypes refuses to guess.** An answer it is not certain of is worth less than an honest `unsupported`, because a wrong answer about types is the failure the product exists to prevent.

When you get one, fall back to the server: validate cautiously, forward the row unpreviewed, and never tell a user they are wrong on the strength of a decline.

<a id="macos-is-a-development-floor-not-an-oracle"></a>

## macOS artifacts are for development; Linux is the reference

The darwin artifacts exist so you can develop and run the suites on a laptop. They are not the reference for what a server does; Linux artifacts and live servers are.

macOS's `long double` is 53-bit, so some float parses diverge from a real server. Measured: on Linux the float corpus matches in full; on macOS none of it does. That is not a near miss to be tolerated — it is a systematic difference in a whole class of values.

**Float expectations must come from a Linux artifact or a live ClickHouse.** Everything else on a Mac is trustworthy for development.

## EPHEMERAL columns cannot be previewed through a format stream

An INSERT whose explicit column list names an EPHEMERAL column cannot be previewed here, because a format stream carries no column list. There is no way to tell, from the body alone, that the caller meant the ephemeral form.

Detect the intersection at compile time and decline it rather than mispreview:

|            |                                                          |
| ---------- | -------------------------------------------------------- |
| Go         | `schema.Columns`, `DefaultKind == chtypes.KindEphemeral` |
| Python     | `schema.columns`, `DefaultKind.EPHEMERAL`                |
| TypeScript | `schema.columns`, `defaultKind === 'EPHEMERAL'`          |
| Rust       | `schema.columns()`, `DefaultKind::Ephemeral`             |

A gateway that previews the intersection anyway shows a row that cannot exist.

## Declined by design

Each of these is `unsupported` — an `UnsupportedError`, `Error::Unsupported`, or the `unsupported` outcome. None of them is a rejection, and none is a defect.

- **Engines and sorting keys** beyond the modeled MergeTree family.
- **TTL forms** that are not a plain rows TTL: `WHERE` and `GROUP BY` TTLs, `TO DISK` and `TO VOLUME` moves, `RECOMPRESS`, and any clock-reading TTL expression.
- **MergeTree settings declared at a non-default value.** An unknown _name_ is the server's own 115, a rejection; a known name at a value this build does not model is a decline, never a silent ignore.
- **Server- and session-property DEFAULTs** — `hostName()`, `currentUser()` and the rest. Their value is a property of the server, and there is no server here.
- **Blocking DEFAULTs**, such as anything calling `sleep`.
- **A multi-row body into a deprecated `Object('json')` column whose stored values depend on how a server splits it** (24.8–25.10; `Values`, `JSONEachRow`, `JSONCompactEachRow`, `CSV`, `TSV`). There are two ways this happens:
  - **Committed in parts.** The call's `max_insert_block_size` splits the body, and `min_insert_block_size_rows` / `_bytes` keep the INSERT's squashing step from joining the pieces back, so a server writes several parts, each typed separately.
  - **A parallel-parsing segment cut mid-chunk** (text formats only). With `input_format_parallel_parsing` on, a segment of `min_chunk_bytes_for_parallel_parsing` ends inside a `max_insert_block_size` chunk. At the default chunk size, a four-row `CSV` body at `max_insert_block_size` 2 is enough.

  In both cases what the table reads back depends on where the body was cut, not only on the body, so this build declines rather than guess. The first needs non-default settings. The second can happen at the defaults for a text body larger than `min_chunk_bytes_for_parallel_parsing`, because parallel parsing is on by default.
- **DEFAULT expressions past the admission budgets** — 256 MiB and one second by default, both adjustable through the process-wide settings in [`guides/settings.md`](guides/settings.md).

## Constants are not payloads

Everything on the row path is **insert-side** coercion. Never reuse it to fold a `WHERE`-clause constant.

The rules genuinely differ: `256` into a `UInt8` column stores `0`, while `x = 256` over that column promotes and is false for every row. Refuse an operand outside the column type's domain instead — or use [`guides/filters.md`](guides/filters.md), which is the surface that answers comparison questions with ClickHouse's own comparison functions.

## Filters are for comparison, not enforcement, for now

No read-side security may be enforced on the filter surface until a release explicitly lifts this limitation — the CHANGELOG will say so, and until it does, assume it has not. Until then the surface is for shadow and replay: run it beside your existing enforcement and compare, do not replace.

The parse-once block twin does not change that — it is a performance shape, not a maturity signal, and sits under the same gate.

## Some formats depend on the artifact, not the binding

`RowBinary` and its family, `Native` and `Buffers` parse only on artifacts new enough to carry the reader. This is a property of the **loaded artifact's age**, not of your binding's version, so probe the artifact rather than assuming from the package version. Earlier artifacts answer 73 or 117 exactly as their own servers do.

`CSVWithNames` and `TSVWithNames` are text formats with the same property. They joined the format set inside ABI revision 5 without changing the revision number, so an artifact built before them can report revision 5 and still not know them. Probe for them the same way.

## The error model is normative

`unsupported` (`CODE_UNSUPPORTED`, the wire sentinel `-2`) is neither an acceptance nor a rejection, and treating it as either is the most expensive mistake available here.

Over-accepts and over-rejects have **no budget** in the differential proof the artifacts are built from: a row accepted here and rejected by the server ships before the insert fails, and a row rejected here and accepted by the server is silent data loss. Neither is acceptable, so neither gets an allowance. A decline is how the system stays honest about the cases it cannot reach that bar on.

**No budget is a rule about process, not a claim about state.** A non-zero count, in either direction, on any binding against any ClickHouse line, is refused unless a person has named that case and recorded why, with a tracking reference attached. Nothing non-zero passes quietly, and no threshold waves anything through.

**So it does not mean there are none.** What is true today is narrower, and worth stating exactly: **no case is currently known in which this library and a real server disagree about whether to accept a row.** Both directions are at zero across every line the artifacts are proved against — `measured` by the differential proof those artifacts are built from, not by this repository, and a state rather than a promise: it is what the rule has produced so far, not something the rule guarantees will hold tomorrow.

⚠️ **Zero in both directions is a statement about the verdict, not about the value or the error.** There are two other ways to disagree: both sides accept a row and **store different values**, or both refuse it and **report different codes**. Neither is an accept-or-reject disagreement, so the no-budget rule above does not cover them.

They are measured all the same, and as of the current published artifacts **both are at zero on every line, for every binding, except one known value divergence, listed below**. That's `measured` by the same differential proof, not by this repository, and it's the same kind of statement as the one above: a state of what the proof covers, not a promise about every input. A case outside that coverage can still disagree, and any that's known is listed under [Known divergences](#known-divergences). Getting there meant investigating the cases one at a time. Some were fixed in the library. Others turned out not to be something a caller can reach, because the disagreement was a property of how the comparison itself was run. Any that a caller **can** reach are listed under [Known divergences](#known-divergences) below.

In Python specifically, `UnsupportedError` is a **peer** of `SchemaError` rather than a subclass, so `except SchemaError` never catches a decline. Handle the two arms explicitly, or catch `ChtypesError` for both. The subtype was retired precisely because catching one and getting the other is a silent misclassification.

The ABI header is the full contract.

## An out-of-domain Enum DEFAULT answers differently by line

This is by design, because the server does too. On 24.8–25.10 such a schema compiles, and a row relying on the default is `accepted_poisoned`. On 26.x, compiling refuses it (`691`, or `70`). The poisoned row's code is the server's readback code, which also differs by line. The conformance suite compares that code on every line and binding, and finds no mismatch. The rule and the codes are in [`transformations.md`](guides/transformations.md#an-out-of-domain-enum-default-follows-the-server).

## Known divergences

Each entry here is a case where this library and a real ClickHouse server give different answers, and a caller can reach it. Every entry names the direction, the input, both answers, and **which half of the comparison was measured where** — there is no ClickHouse server in this repository, so the server half always comes from the differential proof the artifacts are built from.

An entry disappears when an artifact stops diverging, or when the disagreement turns out to have been a property of how it was measured rather than of the library. Pin nothing to this list.

⚠️ **"A real server" means a real table engine** — a MergeTree table, the kind a tenant writes to. A `CREATE TEMPORARY TABLE` is `ENGINE=Memory`, has no parts, and accepts values that every ordinary table refuses at part-write time. If you reproduce an entry against a temporary table you will not get the answer recorded here, and the temporary table is the one that is wrong about your production write.

Every entry below has a machine-checkable twin in [`docs/divergences.json`](divergences.json): `scripts/check-divergences.py` drives each one against loaded artifacts and, non-blocking in CI (the same volume as `scripts/support-matrix.sh`, for the same reason — see `.github/workflows/ci.yml`'s `docs` job), flags an entry the artifacts no longer support. A red there means "update this page", not "the library regressed" — read the check's own message before assuming either.

**What gets an entry here, and when.** A divergence that changes the verdict or the stored value at default settings (an over-accept, an over-reject, or a row both sides accept but store differently) is listed as soon as the server's answer has been measured on each line it affects, and not before, because a wrong entry is worse than a late one. A divergence that is only a different error code, or that needs a non-default setting to reach, is listed if a published build still shows it seven days after it was found. Until then it's tracked with the artifact producer, whose comparison against real servers keeps finding such cases, and it's usually fixed in the next relink. Every entry is removed on the relink that fixes it.

### A later row's Dynamic value in a multi-row text body, typed without the earlier rows

**Same accept, different stored value.** Both sides accept the row — nothing is over-accepted and nothing is rejected — but what a later row stores does not match.

The input is a multi-row `TSV`, `JSONEachRow` or `JSONCompactEachRow` body into a table with a `Dynamic` column (or a `JSON` column's dynamic path), where an earlier row's value settles a variant — Float64, Int64, Bool, or an Array type — and a later row's own value is incomplete on its own: an empty array `[]`, an array holding only `NULL` (`[NULL]`), or, in the JSON formats, an integer literal too big for any integer type. For example, with `d Dynamic`, this `TSV` body:

```text
7
[]
```

|               |                                                                            |
| ------------- | -------------------------------------------------------------------------- |
| this library  | row 2 stored as **String** `"[]"`                                          |
| a real server | row 2 stored as **Int64** `0` — the earlier row's variant, applied to `[]` |

A server resolves a `Dynamic`/`JSON` variant across the whole chunk it is writing: a later row whose own value cannot fully determine a type reuses the first earlier row in that chunk that CAN parse it, and falls back to String only when no earlier variant can. This library types every row on its own and never looks at an earlier row, so it answers String whenever a row is shaped this way. Only rows after the first are affected; each `max_insert_block_size` chunk restarts the reuse from empty; a single-row insert always agrees, because there is no earlier row to disagree about.

The same shape reproduces across several bodies:

- `TSV` `7` then `[]`, or `7` then `[NULL]`: server **Int64** `0`, library **String** — every published line.
- `TSV` `3.14` then `[]`, or `3.14` then `[NULL]`: server **Float64** `0`, library **String** — 24.8–26.6.
- `TSV` `[[1]]` then `[]`: server **Array(Array(Int64))** `[]`, library **String** `"[]"` — every published line.
- `TSV` `[[1]]` then `[NULL]`: server `[[]]`, library **String** — every published line.
- `TSV` `Array(Dynamic)` `[3.14]` then `[[]]`: server `[0]`, library `["[]"]` — 24.8–26.6.
- `JSONEachRow`/`JSONCompactEachRow` `3.14` then `1180591620717411303424` (too big for any integer): server **Float64** `1.18e21`, library **String** — 24.8–26.7.
- the same with `true` first: server **Bool** `true`, library **String** — 24.8–26.7.
- a `JSON` column, `JSONEachRow` and `TSV`, `{"a":[[1]]}` then `{"a":[null]}`: server `{"a":[[]]}`, library `{"a":[null]}` — every published line.

No divergence: `CSV` and `Values` bodies; a sequence starting from `Bool`, `String` or `Date`; a `Tuple` field, a `Map` value, or a `Variant` column; a `JSONEachRow` row of `[]`, `{}` or `null` following any variant.

**Measured**: this library's answer, in this repository, against the published artifacts on 25.10 and 26.9 (build 1790372645) — row 2's stored text, above, for the `TSV` `7`/`[]`, `TSV` `[[1]]`/`[]`, and `JSON`-column shapes. This library types every row independently of the loaded ClickHouse version, so the same answer holds on every other published line. The server's answer, against MergeTree servers pinned to each artifact's exact patch, by the artifact producer, not measured here; default settings throughout, except the setting that enables `Dynamic`/`JSON` at all on 24.8, where both types are still experimental. Its differential proof found 24 such diverging cases on 24.8–26.6, 19 on 26.7, and 13 on 26.8–26.9: upstream ClickHouse's own fix for the too-large-integer shapes lands at 26.8 (the range above, 24.8–26.7, is where those two still diverge), and the float shapes converge one release earlier, at 26.7 (diverging through 26.6 only).

Treat a stored `Dynamic`/`JSON` value from a multi-row `TSV`, `JSONEachRow` or `JSONCompactEachRow` body as unconfirmed for any row after one that created a numeric, Bool or Array variant.

⚠️ **An empty register is not a claim that nothing diverges** — the state above, when this list last held zero, wasn't a promise that it would stay that way. It means nothing was known, named and checked here at the time, and the limits stated above — including that the server half of any comparison is never measured in this repository — apply to a future emptiness exactly as they applied then.

## Pre-1.0

What is already frozen before 1.0, and how an artifact and the SDK opening it are matched, is in [`support.md`](support.md#pre-10).

Anything else may still move before 1.0. Each binding's own CHANGELOG carries its list.
