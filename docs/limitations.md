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

The parse-once block twin does not change that — it is a performance shape, not a maturity signal, and sits under the same gate. This is the canonical statement of the gate; `docs/guides/filters.md` points here rather than restating the criterion. For the rule a filter's own result type must follow, see [`guides/filters.md` → Writing a filter's result type](guides/filters.md#writing-a-filters-result-type).

**The gate lifts per `(ClickHouse line, platform)`, never all at once.** A pair's warning lifts once that pair has **three consecutive records with zero over-admits and zero over-hides against a real server, and no open filter divergences for it** (see [Known divergences](#known-divergences) below). A pair that diverges again after being lifted gets the warning back — a lift is a state, not a one-way promotion, and lifting one pair says nothing about any other.

**Each lift is announced in the CHANGELOG, by `(line, platform)`, as its own entry** — the fixed shape is in [`CONTRIBUTING.md`](../CONTRIBUTING.md#changelog-entries). There is no other record of a lift: a pair stays gated until its own CHANGELOG entry says otherwise.

**Today no `(line, platform)` pair is lifted.** Every supported line, on every published platform, is still under the gate above.

This criterion is scored against the artifact producer's own differential comparison against real servers, and the per-`(line, platform)` state it produces is not served yet — the served `index.json` carries no such field today. Once the artifact producer serves one, this page reads it directly, the same principle [`support-v1.md`](support-v1.md) follows for line support: it states what the registry says rather than listing lines by hand. Until then, do not infer a lift from anything but a CHANGELOG entry naming the pair.

## Some formats depend on the artifact, not the binding

`RowBinary` and its family, `Native` and `Buffers` parse only on artifacts new enough to carry the reader. This is a property of the **loaded artifact's age**, not of your binding's version, so probe the artifact rather than assuming from the package version. Earlier artifacts answer 73 or 117 exactly as their own servers do.

`CSVWithNames` and `TSVWithNames` are text formats with the same property. They joined the format set inside ABI revision 5 without changing the revision number, so an artifact built before them can report revision 5 and still not know them. Probe for them the same way.

## The error model is normative

`unsupported` (`CODE_UNSUPPORTED`, the wire sentinel `-2`) is neither an acceptance nor a rejection, and treating it as either is the most expensive mistake available here.

Over-accepts and over-rejects have **no budget** in the differential proof the artifacts are built from: a row accepted here and rejected by the server ships before the insert fails, and a row rejected here and accepted by the server is silent data loss. Neither is acceptable, so neither gets an allowance. A decline is how the system stays honest about the cases it cannot reach that bar on.

**No budget is a rule about process, not a claim about state.** A non-zero count, in either direction, on any binding against any ClickHouse line, is refused unless a person has named that case and recorded why, with a tracking reference attached. Nothing non-zero passes quietly, and no threshold waves anything through.

**So it does not mean there are none.** What is true today is narrower, and worth stating exactly: **three cases are currently known in which this library and a real server disagree about whether a row gets through, all listed under [Known divergences](#known-divergences) below: two over-accepts (a DEFAULT over `demangle`, and a CHECK constraint) and one over-admit by a filter, which on 25.10 and later also over-hides some float results.** Apart from them, both directions are at zero across every line the artifacts are proved against — `measured` by the differential proof those artifacts are built from, not by this repository, and a state rather than a promise: it is what the rule has produced so far, not something the rule guarantees will hold tomorrow.

⚠️ **Zero in both directions is a statement about the verdict, not about the value or the error.** There are two other ways to disagree: both sides accept a row and **store different values**, or both refuse it and **report different codes**. Neither is an accept-or-reject disagreement, so the no-budget rule above does not cover them.

They are measured all the same, and as of the current published artifacts **both are also at zero on every line, for every binding**. That's `measured` by the same differential proof, not by this repository, and it's the same kind of statement as the one above: a state of what the proof covers, not a promise about every input. A case outside that coverage can still disagree, and any that's known is listed under [Known divergences](#known-divergences). Getting there meant investigating the cases one at a time. Some were fixed in the library. Others turned out not to be something a caller can reach, because the disagreement was a property of how the comparison itself was run. Any that a caller **can** reach are listed under [Known divergences](#known-divergences) below.

In Python specifically, `UnsupportedError` is a **peer** of `SchemaError` rather than a subclass, so `except SchemaError` never catches a decline. Handle the two arms explicitly, or catch `ChtypesError` for both. The subtype was retired precisely because catching one and getting the other is a silent misclassification.

The ABI header is the full contract.

## An out-of-domain Enum DEFAULT answers differently by line

This is by design, because the server does too. On 24.8–25.10 such a schema compiles, and a row relying on the default is `accepted_poisoned`. On 26.x, compiling refuses it (`691`, or `70`). The poisoned row's code is the server's readback code, which also differs by line. The conformance suite compares that code on every line and binding, and finds no mismatch. The rule and the codes are in [`transformations.md`](guides/transformations.md#an-out-of-domain-enum-default-follows-the-server).

## Known divergences

Each entry here is a case where this library and a real ClickHouse server give different answers, and a caller can reach it. Every entry names the direction, the input, both answers, and **which half of the comparison was measured where** — there is no ClickHouse server in this repository, so the server half always comes from the differential proof the artifacts are built from.

An entry disappears when an artifact stops diverging, or when the disagreement turns out to have been a property of how it was measured rather than of the library. Pin nothing to this list.

⚠️ **"A real server" means a real table engine** — a MergeTree table, the kind a tenant writes to. A `CREATE TEMPORARY TABLE` is `ENGINE=Memory`, has no parts, and accepts values that every ordinary table refuses at part-write time. If you reproduce an entry against a temporary table you will not get the answer recorded here, and the temporary table is the one that is wrong about your production write.

Every entry below has a machine-checkable twin in [`docs/divergences.json`](divergences.json). The v0 job that drove each one against loaded artifacts is retired with the v0 registry layout, so nothing checks these entries until a v1 replacement lands; treat each as a claim about the build named, and re-measure before relying on it.

**What gets an entry here, and when.** A divergence that changes the verdict or the stored value at default settings (an over-accept, an over-reject, or a row both sides accept but store differently) is listed as soon as the server's answer has been measured on each line it affects, and not before, because a wrong entry is worse than a late one. A divergence that is only a different error code, or that needs a non-default setting to reach, is listed if a published build still shows it seven days after it was found. Until then it's tracked with the artifact producer, whose comparison against real servers keeps finding such cases, and it's usually fixed in the next relink. Every entry is removed on the relink that fixes it.

### A DEFAULT over demangle admits what a real server refuses to create

**Over-accept, on `24.8` and `25.10`. Served, unsupported: not fixed on retired lines.** This library compiles a schema, and admits a row against it, that a real server refuses to create in the first place.

The ClickHouse setting `allow_introspection_functions` defaults to disabled. At that default, a real server refuses a `CREATE TABLE` containing a `DEFAULT` over `demangle` with error 446 (`FUNCTION_NOT_ALLOWED`). This library never consults the setting, so it compiles the same schema and evaluates the same DEFAULT identically at either value. For example, `s String, v String DEFAULT demangle(s)`, with a body that never supplies `v`:

|               |                                                                               |
| ------------- | ----------------------------------------------------------------------------- |
| this library  | compiles the schema and stores the function's answer, at either setting value |
| a real server | refuses the `CREATE TABLE` itself, error 446, at the default setting          |

The artifact producer's relink at build `1790845279` made this library refuse the same `CREATE TABLE` with 446 on every **supported** line — 26.3, 26.7, 26.8 and 26.9 — so the over-accept no longer reproduces there. It is unchanged on `24.8` and `25.10`: both are **served, unsupported** lines (see [ClickHouse lines](support-v1.md#clickhouse-lines)), upstream's own support for them has ended, and the artifact producer builds no new artifact for a retired line, ever — so their library half is still the pre-relink build and `demangle`'s DEFAULT still compiles and stores the demangled name there. On those two lines the divergence is permanent: a retired line gets no new builds and no new ABI revisions, so no fix will ever be served for it, and this entry stays registered for them for as long as they are served. On ABI revision 6, `addressToLine`, `addressToLineWithInlines` and `addressToSymbol` are declined (`unsupported`) at insert time on every served line, so they do not diverge and carry no entry here.

**Measured**: this library's own answer, in this repository, against the published ABI revision 6 artifacts. `demangle`'s DEFAULT still compiles and stores the demangled name on `25.10` (darwin-arm64, build `1790767905`) and, by CI's linux-amd64 job, on `24.8` (no darwin artifact is published for that line). On `26.3`, `26.8` and `26.9` (darwin-arm64, build `1790845279`) this library now refuses the `CREATE TABLE` itself with code 446, matching a real server; `26.7` was not part of this check's line list before and is not added by this measurement. The server half — a 446 refusal of the `CREATE TABLE` at the default setting — was measured by the artifact producer against stock ClickHouse servers pinned to each line's exact patch, not measured here.

Do not put `demangle` in a DEFAULT for a server that runs at the default setting on `24.8` or `25.10`: this library still compiles the schema there, and the server refuses the `CREATE TABLE` with 446. On every supported line this library now refuses the same `CREATE TABLE` the same way.

### A filter whose result is not a boolean-context type admits rows a real server refuses

**Over-admit, on every served line.** A filter's top-level result is evaluated whatever its type is. A real server restricts which types a filter may answer over; this library does not — it answers a per-row verdict for any result type by truncating it to a C-style truthiness: `t` when the value's low 64 bits are non-zero, `f` otherwise.

A real server's own rule is `canBeUsedInBooleanContext()`, and it differs by line:

- **On `25.10` and `26.2`–`26.9`**, it is true only for the native-number types — `Int8`..`Int64`, `UInt8`..`UInt64`, `Float32`, `Float64`, `Bool` (`UInt8`) — plus `Nullable` and `LowCardinality` of those, and `Nullable(Nothing)`. Outside that set, a real server refuses the whole `SELECT … WHERE` at analysis, before any row is read, with error **59** (`ILLEGAL_TYPE_OF_COLUMN_FOR_FILTER`). This library over-admits there for `Date`, `Date32`, `DateTime`, `Enum8`, `Int128`, `UInt256`, `BFloat16`, and any other type outside that set.
- **On `24.8`, `25.3` and `25.8`**, the rule is narrower: those servers' own `canBeUsedInBooleanContext()` accepts only `UInt8` and `Nullable(UInt8)` as a filter column — upstream widened it in `FilterDescription.cpp` at `25.10` (`tryConvertAnyColumnToBool`). This library over-admits more broadly there: every other numeric filter is affected too, including `Int64`, `Float64`, and a bare integer literal such as `256` or `-1`. `24.8` has no `BFloat16` type to test against at all.

|               |                                                                                                                   |
| ------------- | ----------------------------------------------------------------------------------------------------------------- |
| this library  | answers a per-row verdict, truthy on a non-zero low 64 bits, for any result type                                  |
| a real server | refuses the query itself, error 59, before evaluating any row, outside that line's own boolean-context rule above |

Write the comparison explicitly instead — [`guides/filters.md` → Writing a filter's result type](guides/filters.md#writing-a-filters-result-type) has the rule and worked examples.

**Measured**: server side, by the artifact producer, against all 12 served lines (`24.8`, `25.3`, `25.8`, `25.10`, `26.2`–`26.9`) — the boolean-context boundary above, and the over-admit it produces, is identical on every one of them. Library side, in this repository, against the published artifacts for the four **supported** lines (`26.3`, `26.7`, `26.8`, `26.9`), through both `Filter.rows` and `Filter.eval` over a parsed block, with identical verdicts. A fix is in progress for those four; whether the served, unsupported lines get one is not yet decided.

A known **over-hide** (the safe direction) travels with the same surface, present from `25.10` on only (the earlier, narrower boolean-context rule gives this library no type-level divergence to combine it with): this library's truthiness truncates to an integer, so `0.5` and `-0.5` hide a row a real server's `static_cast<bool>` keeps, and `NaN` hides a row on `arm64` only. Compare explicitly there too.

The fix belongs to the library. This entry is removed, per line, on the relink that makes it refuse the query the way that line's own server does.

### A CHECK constraint admits any non-zero or non-UInt8 result a real server refuses

**Over-admit, on every served line.** The same truthiness as the filter entry above governs a `CONSTRAINT … CHECK` expression. A real server passes a row only when the CHECK expression's own type is `UInt8` and its value **equals `1`** exactly; anything else rejects the whole batch. This library instead admits any non-zero `UInt8` value, and admits a result of a wider or different type on the same truthy-low-64-bits rule, rather than rejecting it for its type.

|               |                                                                                                                                               |
| ------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| this library  | admits a row whose CHECK result is non-zero and/or not `UInt8`                                                                                |
| a real server | rejects a non-`UInt8` result with code **1** ("does not return a value of type UInt8"), and a `UInt8` result other than `1` with code **469** |

For example, `x UInt8, CONSTRAINT c CHECK x` admits a row with `x = 2` here; a real server rejects it with 469. `x Int64, CONSTRAINT c CHECK x` admits a row with `x = 1` here; a real server rejects every row with code 1, because the CHECK result is `Int64`, never `UInt8`. Write `CHECK x = 1`, or another comparison whose own result is genuinely `UInt8` — see [`guides/batches.md` → CHECK constraints are batch-level, not per-row](guides/batches.md#check-constraints-are-batch-level-not-per-row).

**Measured**: server side, by the artifact producer, identically against all 12 served lines (`24.8`, `25.3`, `25.8`, `25.10`, `26.2`–`26.9`) — unlike the filter entry above, this one does not vary by line: the CHECK pipeline's own type and value rule is the same on every served server. Library side, in this repository, against the published artifacts for the four **supported** lines (`26.3`, `26.7`, `26.8`, `26.9`). A fix is in progress for those four; whether the served, unsupported lines get one is not yet decided. The cause is the same `!= 0` truthiness as the filter entry above.

The fix belongs to the library. This entry is removed, per line, on the relink that makes it reject the way that line's own server does.

## Known gaps in 1.0

Each item is a place where 1.0 does less than you might expect, or answers differently from a server. None of them returns a wrong answer without saying so, and every one is planned. Each entry says what happens, what to do today, and that a fix is planned.

### Nested String values carry no raw bytes

A `String` value nested inside `Array(String)`, `Map` or `Tuple` carries no raw `value` bytes, only ClickHouse's rendering of it. A scalar `String` or `FixedString` value always carries its raw bytes, including inside `Nullable` and `LowCardinality`.

**Workaround:** read the rendering for a nested value, or select the nested element as its own scalar column when you need the bytes. **Planned.**

### Binary input parameters must be UTF-8

The values of `settings` and `query_params` must be UTF-8. A value that is not valid UTF-8 is refused.

**Workaround:** inline the value as `unhex('<hex>')`, built from hex digits only. **Planned:** a byte-safe object form.

### Zone names follow the host

Whether a time zone name is valid is ClickHouse's own `DateLUT` rule, and `DateLUT` loads zone files from the host the library runs on. A host that is missing some zone files refuses names a server accepts. Measured on a CI runner with no `posix/` zoneinfo: every `posix/*` name was refused (`measured`). On darwin, case-folding of zone names differs.

**Workaround:** give the host the server's zoneinfo, so its zone files match the server's. **Planned.**

### `SHOW CREATE` of a Memory table is declined

`SHOW CREATE TABLE` text from live 26.3, 26.7, 26.8 and 26.9 servers compiles with the schema call for every MergeTree-family shape measured, which is 28 of the 32 shapes. The 4 `ENGINE = Memory` tables are declined, not mis-compiled.

**Workaround:** use the discovery calls for those tables. **Planned.**

### A batch preview with a filter in a different zone than the batch is declined

When the filter evaluates in a different time zone than the batch it runs over, the preview is declined. It never answers wrongly.

**Workaround:** use one zone for both, or a per-row preview. **Planned.**

### Per-call settings values are not validated the way a server's `SET` validates them

Some invalid values for a per-call setting are accepted, where a server's `SET` would refuse them.

**Workaround:** validate settings values in the caller, or against a server, before relying on a refusal here. **Planned.**

### `lossy` on a String holding a raw NUL in TSV

For a TSV `String` that contains a raw NUL byte, the third detector can report `value_changed` with `lossy: true` although the bytes are preserved.

**Workaround:** compare `value`, the raw bytes, with your input; the bindings never second-guess `lossy`. **Planned:** fixed in 1.0.x.

### No call for a server's version or settings

1.0 has no call that queries a server's version or its changed settings, and no settings reader. The caller supplies both.

**Workaround:** read the version and settings from your own connection and pass them in. **Planned.**

## Pre-1.0

How a library and the SDK opening it are matched before 1.0 is in [`support-v1.md`](support-v1.md#pre-10).

Anything else may still move before 1.0. Each binding's own CHANGELOG carries its list.
