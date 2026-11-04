# Known limitations

Every item here is a deliberate decline or a stated boundary, not a bug queue. The rule underneath all of them is the same one: **chtypes refuses to guess.** An answer it is not certain of is worth less than an honest `unsupported`, because a wrong answer about types is the failure the product exists to prevent.

When you get one, fall back to the server: validate cautiously, forward the row unpreviewed, and never tell a user they are wrong on the strength of a decline.

This page describes the libraries SDK 2.0.0 ships: build `20261104.061020`, on the four supported lines 26.3, 26.8, 26.9 and 26.10 (the exact patches are in [`support-v2.md`](support-v2.md)). 26.7 is served but unsupported. Each entry says what holds on that build and how it was measured.

<a id="macos-is-a-development-floor-not-an-oracle"></a>

## macOS artifacts match ClickHouse on macOS; on 26.3, match the server's platform

**Requirement: run the library on the same operating system and CPU architecture as your ClickHouse server.** That is the caller's responsibility; the library does not detect or decline a mismatch. Its answers, filter verdicts included, are guaranteed relative to a ClickHouse server on the same platform.

The darwin artifacts give exactly the answers ClickHouse itself gives on macOS, on every supported line, with one exception on 26.3 (deep nesting, below). The artifact producer measured this against official macOS ClickHouse binaries.

**On 26.8, 26.9 and 26.10 (and on 26.7, which is served but unsupported),** ClickHouse agrees on every platform (macOS, Linux amd64, Linux arm64), float text included. On these lines, a mismatch of platform changes nothing.

**On 26.3,** ClickHouse's own parse of Float values from text input formats differs across platforms (macOS, Linux amd64, Linux arm64; mostly 1 ULP), and none is correctly rounded. So stored Float values, and comparisons involving them, can differ across architectures, and a verdict computed on another platform can differ from the server's.

**On 26.3, deep nesting** also differs on macOS: the macOS 26.3 server refuses deeply nested expressions and types that the library answers, because its threads have a smaller stack. That is a documented platform limitation of that one pair; the measured band is under [Not covered: `26.3` on `darwin-arm64`](#filters-are-enforcement-grade-against-a-same-platform-server-per-lifted-line-platform).

## Declined by design

Each of these is `unsupported` — an `UnsupportedError`, `Error::Unsupported`, or the `unsupported` outcome. None of them is a rejection, and none is a defect.

- **Engines and sorting keys** beyond the modeled MergeTree family.
- **TTL forms** that are not a plain rows TTL: `WHERE` and `GROUP BY` TTLs, `TO DISK` and `TO VOLUME` moves, `RECOMPRESS`, and any clock-reading TTL expression.
- **MergeTree settings declared at a non-default value.** An unknown _name_ is the server's own 115, a rejection; a known name at a value this build does not model is a decline, never a silent ignore.
- **Anything that would run a query or read storage.** A subquery, or a read of a table, a table function, a file, a remote host or a dictionary, in a `CREATE` (any clause), a filter or a `Values` expression is declined, never executed in your process. The decline reasons are `expression-executes-query` and `expression-reads-storage` (`CREATE` and filter) and `expression-subquery` and `values-reads-storage` (`Values`); the catalog entries in [`spec/abi-v2/declines/declines.json`](../spec/abi-v2/declines/declines.json) give the exact trigger of each.
- **Server- and session-property DEFAULTs** — `hostName()`, `currentUser()` and the rest. Their value is a property of the server, and there is no server here.
- **DEFAULT expressions past the admission budgets** — 256 MiB and one second by default, both adjustable through the process-wide settings in [`guides/settings.md`](guides/settings.md). This is what bounds a blocking DEFAULT: `sleep` and `sleepEachRow` are not declined by name, so `DEFAULT sleep(0)` is admitted, while `DEFAULT sleep(1.5)` exceeds the one-second budget and is declined when the schema compiles.
- **Functions that need a server, in a column expression.** A `DEFAULT`, `MATERIALIZED`, `ALIAS` or `EPHEMERAL` expression that calls a function whose resolver cannot be built outside a running server, or whose value depends on a server's files, objects or identity, declines the whole schema at create (`unsupported`; `CHS_DECLINED` at `chs_schema_create`). The message names the expression kind, the column and the function, and says why. Whether a server accepts such a `CREATE` depends on its own settings, resources and environment, so chtypes does not judge it, and a decline is never a rejection.

  Most of these are functions whose `CREATE` a stock server refuses too (the `region*` functions, `neighbor`, `runningAccumulate` and `runningDifference*`, `showCertificate`, the `detect*`, `lemmatize`, `synonyms` and `stem` language functions, the `dictGet*` family and `joinGet*` without the object, and so on): there the library and the server agree that the schema does not compile. The resolver-declined functions number 42 on 26.3, 26.8 and 26.9 and 41 on 26.10.

  **Over-declines.** The following functions are declined by the library in all four column expressions, although a stock server's `CREATE` accepts them: 18 on 26.3, and 42 on each of 26.8, 26.9 and 26.10 (the same set on the three). A schema that uses one is declined here even though the server creates the table: fall back to the server for it.

  | Line              | Over-declined by the library, accepted by a stock server's `CREATE`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
  | ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
  | 26.3              | `file`, `generateSerialID`, `getMaxTableNameLengthForDatabase`, `hasColumnInTable`, `serverUUID`, and 13 `timeSeries*`: `timeSeriesCopyTag`, `timeSeriesCopyTags`, `timeSeriesExtractTag`, `timeSeriesGroupToTags`, `timeSeriesJoinTags`, `timeSeriesRemoveAllTagsExcept`, `timeSeriesRemoveTag`, `timeSeriesRemoveTags`, `timeSeriesReplaceTag`, `timeSeriesStoreTags`, `timeSeriesTagsGroupToTags`, `timeSeriesTagsToGroup`, `timeSeriesThrowDuplicateSeriesIf` (18 names)                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
  | 26.8, 26.9, 26.10 | `file`, `filesystemAvailable`, `filesystemCapacity`, `filesystemUnreserved`, `generateSerialID`, `getMaxTableNameLengthForDatabase`, `getServerPort`, `hasColumnInTable`, `serverUUID`; the 16 AI functions `AIClassify`, `AIEmbed`, `AIExtract`, `AIFilter`, `AIGenerate`, `AIRedact`, `AISimilarity`, `AITranslate` and their lowercase forms `aiClassify`, `aiEmbed`, `aiExtract`, `aiFilter`, `aiGenerate`, `aiRedact`, `aiSimilarity`, `aiTranslate`; and all 17 `timeSeries*`: `timeSeriesCopyTag`, `timeSeriesCopyTags`, `timeSeriesExtractTag`, `timeSeriesGroupToSamplingKey`, `timeSeriesGroupToTags`, `timeSeriesIdToGroup`, `timeSeriesIdToTags`, `timeSeriesIdToTagsGroup`, `timeSeriesJoinTags`, `timeSeriesRemoveAllTagsExcept`, `timeSeriesRemoveTag`, `timeSeriesRemoveTags`, `timeSeriesReplaceTag`, `timeSeriesStoreTags`, `timeSeriesTagsGroupToTags`, `timeSeriesTagsToGroup`, `timeSeriesThrowDuplicateSeriesIf` (42 names) |

  On 26.3 the three `timeSeriesIdTo*` functions are not over-declines: a stock 26.3 server refuses them with error 36. A `dictGet*` or `joinGet*` call declines even where the server holds the dictionary or Join table it names.

  `getClientHTTPHeader` is declined too, in the four column expressions: the message names the setting it needs and the error 446 a stock server gives it at default settings. A `CHECK` constraint is not analyzed by a stock server's `CREATE`, so it accepts every function above in a `CHECK`; the library declines each of them there.

  **Measured** by the artifact producer on build `20261104.061020`, all four lines on linux-amd64, against the pinned servers: 207, 234, 234 and 237 candidate functions on 26.3, 26.8, 26.9 and 26.10, each in the four column expressions and a `CHECK`; both halves complete for every cell.

- **A body the server may read either serially or in parallel, when the error budget is non-zero.** A server's INSERT reads a CSV, TSV (and their `WithNames` forms), JSONEachRow or JSONCompactEachRow body in parallel segments when `max_parsing_threads` is above 1 and `input_format_parallel_parsing` is on. Each segment's reader starts its error count at zero, so `input_format_allow_errors_num` holds **per segment**, while the ratio (`input_format_allow_errors_ratio`) runs over the whole body. A single reader over the whole body stops at a different row. The library reads such a body in segments as the server does, each with the vendored reader, and declines it (`unsupported`, `CHS_DECLINED`, reason `error-budget-reread`, with a message naming both answers) when both of these hold:

  - the body would be cut into more than one segment, which with default settings needs about 10 MiB or a million rows, or a lowered `max_insert_block_size` or `min_chunk_bytes_for_parallel_parsing`; and
  - `max_parsing_threads` is not named, per call or in the server profile (it defaults to `auto`, the server's CPU count, which the library cannot see; a one-CPU server reads serially), and one reader and the segments would answer the body differently. Where both readings agree, the body is answered either way.

  With `input_format_connection_handling` on, such a body is declined even when `max_parsing_threads` is named. A body is also declined when its segments after the first fail with different codes: the server's code then depends on which segment's parser throws first.

  **To get an exact answer, name the threads:** set `max_parsing_threads` in the server profile or per call (`1` reads serially, as does `input_format_parallel_parsing=0`; a number above 1 reads in segments). The answer is then the way that server reads the body. With default settings and a body under about 10 MiB and a million rows, there is one segment and nothing changes.

  Treat the decline as no answer, never as a rejection.

  **Measured** by the artifact producer on build `20261104.061020`, all four lines on linux-amd64, against the pinned servers: a CSV, TSV and JSONEachRow body with two good rows among bad ones, under `input_format_allow_errors_num=2` and `max_insert_block_size=2`. With `max_parsing_threads` not named the library declines it (`error-budget-reread`); named `1` it is refused with 38, as the server refuses it; named `4` the two good rows are stored, as the server stores them. All 12 cells agree on each line. The server inserts ran with `async_insert=0`: at its default of 1 a server reads the body serially.

## Constants are not payloads

Everything on the row path is **insert-side** coercion. Never reuse it to fold a `WHERE`-clause constant.

The rules genuinely differ: `256` into a `UInt8` column stores `0`, while `x = 256` over that column promotes and is false for every row. Refuse an operand outside the column type's domain instead — or use [`guides/filters.md`](guides/filters.md), which is the surface that answers comparison questions with ClickHouse's own comparison functions.

## Filters are enforcement-grade against a same-platform server, per lifted (line, platform)

**A filter verdict is enforcement-grade relative to a ClickHouse server of the same line, on the same operating system and CPU architecture.** A `t` verdict is a security claim; anything but a definite `t` is a no. That includes v2's `unknown(n)` verdict member, so a caller that maps an unknown verdict to a pass is wrong by contract. A closed filter is never passed to the library as NULL (which would mean "no filter"): the bindings refuse it.

**The criterion.** The artifact producer runs a standing filter differential against real servers of the same platform on each published build, before the push and after it. A build is enforcement-grade for a `(ClickHouse line, platform)` pair when that differential reports zero over-accepts and zero over-rejects for it.

**On build `20261104.061020`, every supported pair is lifted but one:** `26.3`, `26.8`, `26.9` and `26.10` on `linux-amd64` and `linux-arm64`, and `26.8`, `26.9` and `26.10` on `darwin-arm64`. **`26.3` on `darwin-arm64` is not covered**, a documented platform limitation (below). `26.7`, served but unsupported, is outside this statement. The producer's signed gate evidence for that build reads PASS on all 12 cells as published.

**Not covered: `26.3` on `darwin-arm64`, a documented platform limitation.** The macOS 26.3 server runs queries on 512 KiB threads, so it refuses deeply nested expressions and types with 306 (TOO_DEEP_RECURSION) at depths where the library answers or accepts them. Measured on the official 26.3.38.2 macOS server, it refuses:

- nested lambdas from 27 levels (`arrayFilter` and `arrayMap` shapes), 35 (mixed) or 53 (`arrayExists` and `arrayAll` shapes);
- nested `if`, `multiIf` and `CASE`, and `NOT` and nested-`OR` expression chains, from 54;
- `DEFAULT` expression chains from 52;
- partition-key expressions from 76;
- nested `Array(Array(…))` column types from 368.

26.7 and later, and every Linux line, agree with the server. This is a documented platform difference, not a filter over-accept under the same-platform guarantee's terms. The pair is not lifted and has no CHANGELOG lift entry: run it beside your existing enforcement and compare, do not replace, or refuse inputs nested that deep yourself. The committed list is [`spec/abi-v2/documented-platform/documented-platform.tsv`](../spec/abi-v2/documented-platform/documented-platform.tsv). See also [macOS artifacts match ClickHouse on macOS; on 26.3, match the server's platform](#macos-artifacts-match-clickhouse-on-macos-on-263-match-the-servers-platform).

**A date-time column compared with a `String` constant or a `{p:String}` parameter is declined where the server reads the text differently.** On 26.8, 26.9 and 26.10, `chs_filter_create` refuses such a shape with `CHS_DECLINED` (each binding's unsupported error), naming the reason `string-reading`. On 26.3, an `AND` of two equals comparisons of one expression with different constants that the comparison matches with one value is declined `equals-fold`, and the rest of 26.3's date-time behavior is in the two 26.3 entries below. The forms that answer on every line are a typed `{p:DateTime}` parameter and canonical `YYYY-MM-DD hh:mm:ss` text; see [the Known divergences entry](#a-date-time-column-compared-with-a-string-constant-or-a-pstring-parameter-in-text-the-server-converts-differently). Treat a declined filter as no filter, never as a pass.

**Each lift is announced in the CHANGELOG, by `(line, platform)`, as its own entry** — the fixed shape is in [`CONTRIBUTING.md`](../CONTRIBUTING.md#changelog-entries). A pair stays gated until its own CHANGELOG entry says otherwise.

**A pair that diverges again gets the gate back.** A lift is a state, not a one-way promotion: a divergence against a same-platform server (see [Known divergences](#known-divergences) below) puts that pair back under the gate with a fresh entry, and lifting one pair says nothing about any other.

**What the guarantee does not cover.**

- **A server configured so that it refuses every query.** For example `max_expanded_ast_elements=1` refuses everything with code 36, and so does `page=1` on 26.8 and 26.9. The library answers the rows such a server would refuse. The committed list is [`spec/abi-v2/where-settings/documented-pathological.tsv`](../spec/abi-v2/where-settings/documented-pathological.tsv), explained in the README section [Settings the server may refuse outright](../spec/abi-v2/where-settings/README.md#settings-the-server-may-refuse-outright). A consumer refuses such a profile or call; the list grows only by sign-off.
- **The 26.3 float platform requirement.** On 26.3, ClickHouse's own float text parse depends on the platform, so run the library on the server's operating system and architecture. See [macOS artifacts match ClickHouse on macOS; on 26.3, match the server's platform](#macos-artifacts-match-clickhouse-on-macos-on-263-match-the-servers-platform), which this page states as a requirement.

The parse-once block twin sits under the same guarantee. For the rule a filter's own result type must follow, see [`guides/filters.md` → Writing a filter's result type](guides/filters.md#writing-a-filters-result-type).

## Some formats depend on the artifact, not the binding

Which input formats a library reads is a property of the **loaded artifact**, not of your binding's version: every build lists them in its `build_info` (`capabilities.input_formats`). Probe that rather than assuming from the package version. A format the loaded library does not read is refused with 73 or 117, exactly as its own server would refuse it.

## The error model is normative

`unsupported` (`CODE_UNSUPPORTED`, the wire sentinel `-2`) is neither an acceptance nor a rejection, and treating it as either is the most expensive mistake available here.

Over-accepts and over-rejects have **no budget** in the differential proof the artifacts are built from: a row accepted here and rejected by the server ships before the insert fails, and a row rejected here and accepted by the server is silent data loss. Neither is acceptable, so neither gets an allowance. A decline is how the system stays honest about the cases it cannot reach that bar on.

**No budget is a rule about process, not a claim about state.** A non-zero count, in either direction, on any binding against any ClickHouse line, is refused unless a person has named that case and recorded why, with a tracking reference attached. Nothing non-zero passes quietly, and no threshold waves anything through.

**So it does not mean there are none.** The cases known today, in either direction, are listed under [Known divergences](#known-divergences) below, each with the build it was measured on and its workaround. Nothing in that list is a promise about tomorrow: the differential proof keeps finding cases, and each one is listed as soon as its server half is measured.

⚠️ **Zero in both directions is a statement about the verdict, not about the value or the error.** There are two other ways to disagree: both sides accept a row and **store different values**, or both refuse it and **report different codes**. Neither is an accept-or-reject disagreement, so the no-budget rule above does not cover them.

They are measured all the same, by the same differential proof, not by this repository. Any that a caller can reach is listed under [Known divergences](#known-divergences), the same as a verdict divergence. Getting there meant investigating the cases one at a time. Some were fixed in the library. Others turned out not to be something a caller can reach, because the disagreement was a property of how the comparison itself was run. Any that a caller **can** reach are listed under [Known divergences](#known-divergences) below.

In Python specifically, `UnsupportedError` is a **peer** of `SchemaError` rather than a subclass, so `except SchemaError` never catches a decline. Handle the two arms explicitly, or catch `ChtypesError` for both. The subtype was retired precisely because catching one and getting the other is a silent misclassification.

The ABI header is the full contract.

## An out-of-domain Enum DEFAULT is refused at compile

This is by design, because the server does too: on every supported line, compiling such a schema refuses it (`691`, or `70`). The rule and the codes are in [`transformations.md`](guides/transformations.md#an-out-of-domain-enum-default-follows-the-server).

## Another patch of the same line can answer differently

chtypes models one exact ClickHouse patch per line, and the patch is named in [`support-v2.md`](support-v2.md): `26.3.60.2`, `26.8.34.13`, `26.9.25.15` and `26.10.2.103` are the four supported lines, and `26.7` is served but unsupported, frozen on `26.7.19.5`. A server on another patch of the same line can answer differently, so compare the patch your server runs with the one chtypes models.

The previous patches of two lines were measured against the current ones, on a server only, for filter verdicts: the `26.8` bump (`26.8.15.10` to `26.8.34.13`) and the `26.9` bump (`26.9.8.3` to `26.9.25.15`). Only the comparison of an enum column with a literal moved, at the default `validate_enum_literals_in_operators = 0`, and it moved in both directions (`measured`):

- **The newer patch answers what the older refused with error 691** (`UNKNOWN_ELEMENT_OF_ENUM`): `e != 'zz'`, `e <> 'zz'`, `notEquals(e, 'zz')`, `NOT (e = 'zz')`, `e = 'a' OR e = 'zz'` and similar, where `'zz'` is not in the enum. 169 cells on the `26.8` bump, 114 on the `26.9` bump.
- **The newer patch refuses what the older answered with no rows:** `e < 'zz'`, `>`, `<=` and `>=` with an unknown literal are refused with 691. 162 cells on the `26.8` bump, 96 on the `26.9` bump.

Nothing else moved on either bump: the `DateTime` and `DateTime64` edges, `LIKE`, `SIMILAR TO` and regular expressions, `has` and `arrayExists`, 10,304 spelling cells, and lambda depth (none changed), and no verdict changed by error code apart from the two groups above.

The `26.3` bump (`26.3.38.2` to `26.3.60.2`) was **not measured**. Do not read that as no change.

## ClickHouse 26.3: a lone -0.0 can be stored as +0.0

On 26.3, a MergeTree table writes a column whose values are mostly the type's default with sparse serialization, and that version counts `-0.0` as the default. A `Float32`, `Float64` or `BFloat16` value of `-0.0`, or such a value inside a `Tuple`, can therefore be stored, and read back, as `+0.0`, depending on the other rows in the same part. `Nullable` and `Array` values keep the sign, and so do `Memory` and `Log` tables. ClickHouse 26.8 and later keep the sign everywhere.

- **Previews:** a row's stored rendering on 26.3 can show `-0` where the server will store `+0`. The INSERT verdict is unaffected, and inserts are not declined for it.
- **Filters:** a filter that reads such a value answers that row `d` (reason `float-negative-zero-sparse-default`), because the server may see either sign. Treat `d` as "no verdict", as for every decline.

The ABI specification states the same limitation under "Known limitations" in [`spec/abi-v2/docs.md`](../spec/abi-v2/docs.md).

## ClickHouse 26.3: a DateTime string the server cannot read is refused per row, not per query

On 26.3, a filter that compares a `DateTime` column with a string constant that needs best-effort parsing (a `T`, a `Z` or an offset, as in `ts >= '2024-06-01T12:00:00Z'`) compiles without error, and every row it evaluates gets the verdict `e` with code 53, on the body, block and batch paths. A 26.3 server refuses the whole query with 53, even over an empty table. No row is ever reported as matching, but over an empty body the library returns no verdicts where the server refuses, so do not read "the filter compiled" as "the server accepts this query". On 26.8 and later these spellings are declined at create (`string-reading`).

**Workaround:** write date-time constants in the canonical form (`'2024-06-01 12:00:00'`), or pass them as a typed parameter (`{p:DateTime}`).

**Measured** by the artifact producer on build `20261104.061020`, line 26.3, against a stock 26.3.60.2 server.

## ClickHouse 26.3: two spellings of one time in a DateTime filter refuse every row

**On ClickHouse 26.3, comparing a DateTime column with two spellings of a time can refuse every row.** A filter such as `dt = '2024-06-01 12:00:00' AND dt = '2024-06-01T12:00:00Z'`, where one constant is canonical text and the other needs best-effort parsing (`T`, `Z`, an offset), is compiled without error, and every row it evaluates gets the verdict `e` with code 53. A 26.3 server answers the same query with no rows. No row is ever reported as matching. To avoid it, write date-time constants in the canonical form (`'2024-06-01 12:00:00'`) or pass them as a typed parameter (`{p:DateTime}`). ClickHouse 26.8 and later are not affected.

This is a refusal where the server answers, never a wrong match. It is tracked for a 2.0.x patch.

**Measured** by the artifact producer on build `20261104.061020`, all four lines, on linux-amd64, against stock servers.

## The merge-time TTL model does not see the server profile

The model of a merge-time TTL (the batch document's `at_merge`) evaluates a TTL expression under the library's process-wide settings, not under the server profile given to `chs_server_create`, on every line. A server-profile setting that a TTL expression reads, for example `date_time_overflow_behavior`, can therefore move which rows the model reports as expired, in either direction. The INSERT-time evaluation follows the server profile on 26.10.

## Known divergences

Each entry here is a case where this library and a real ClickHouse server give different answers, and a caller can reach it. Every entry names the direction, the input, both answers, and **which half of the comparison was measured where** — there is no ClickHouse server in this repository, so the server half always comes from the differential proof the artifacts are built from.

An entry disappears when an artifact stops diverging, or when the disagreement turns out to have been a property of how it was measured rather than of the library. Pin nothing to this list.

⚠️ **"A real server" means a real table engine** — a MergeTree table, the kind a tenant writes to. A `CREATE TEMPORARY TABLE` is `ENGINE=Memory`, has no parts, and accepts values that every ordinary table refuses at part-write time. If you reproduce an entry against a temporary table you will not get the answer recorded here, and the temporary table is the one that is wrong about your production write.

Every entry here has a machine-checkable twin in [`docs/divergences.json`](divergences.json). No job drives those twins against a loaded library, so each entry is a claim about the build it names.

**What gets an entry here, and when.** A divergence that changes the verdict or the stored value at default settings (an over-accept, an over-reject, or a row both sides accept but store differently) is listed as soon as the server's answer has been measured on each line it affects, and not before, because a wrong entry is worse than a late one. A divergence that is only a different error code, or that needs a non-default setting to reach, is listed if a published build still shows it seven days after it was found. Until then it's tracked with the artifact producer, whose comparison against real servers keeps finding such cases, and it's usually fixed in the next relink. Every entry is removed on the relink that fixes it.

### A date-time column compared with a string constant or a `{p:String}` parameter, in text the server converts differently

**Declined on 26.8, 26.9 and 26.10; on 26.3, see the two 26.3 entries.** A filter that compares a `Date`- or `DateTime`-family column with a `String` constant, or with a `{p:String}` query parameter, reads the text with a conversion that differs from the server's default for some spellings, for example `dt = '20240601120000'`, or `dt >= {p:String}` with that value. The shapes the decline catalog describes are declined at `chs_filter_create` with `CHS_DECLINED` (each binding's unsupported error), naming the reason, rather than answered.

The decline catalog (`spec/abi-v2/declines/declines.json`) gives two reasons, and this entry claims nothing beyond their entries:

- **`string-reading`** (the catalog lists `26.3`, `26.8`, `26.9` and `26.10`): a comparison of a non-String expression with a `String` constant or `{p:String}` parameter whose text the server's own index or analyzer reading reads differently from the comparison. A comparison reads a `String` constant once, with `cast_string_to_date_time_mode`; the server reads the same text again with the default text format in its index analysis, `IN` sets and analyzer, and drops rows the comparison keeps or refuses a query over a key column. The create is declined, naming the column and both readings. Typed parameters and canonical text are answered. For this entry's date-time shapes it declines on 26.8, 26.9 and 26.10. On 26.3 those shapes are answered as the server answers them, or refused row by row: see [the two 26.3 entries](#clickhouse-263-a-datetime-string-the-server-cannot-read-is-refused-per-row-not-per-query).
- **`equals-fold`** (`26.3` only; retires when `26.3` leaves the supported set): an `AND` of two equals comparisons of one expression with different constants that the comparison matches with one value, such as `dt = '2024-06-01 12:00:00' AND dt = '1717243200'`. On the lines whose analyzer folds an `AND` of two equals over different constants to false whatever the comparison reads, the server returns no row where the comparison keeps one; the create is declined. Constants the server's own constant test calls equal are answered.

**Safe forms**, which answer on every line: a typed `{p:DateTime}` parameter, and canonical `YYYY-MM-DD hh:mm:ss` text.

**Measured** by the artifact producer on build `20261104.061020`, all four lines, on linux-amd64, against stock servers.

<a id="known-gaps-in-10"></a>

## Known gaps

Each item is a place where the library does less than you might expect, or answers differently from a server. None of them returns a wrong answer without saying so. Each entry says what happens, what to do today, and how it was measured.

### Nested String values carry no raw bytes

A `String` inside an `Array`, `Tuple` or `Map` carries no `value_b64`. Its rendering is byte-exact: `stored_b64` or `wire_b64` carry the bytes when they are not valid UTF-8. A scalar `String` or `FixedString` value always carries its raw bytes, including inside `Nullable` and `LowCardinality`.

**Workaround:** read the rendering for a nested value, or select the nested element as its own scalar column when you need the bytes.

**Measured** by the artifact producer on build `20261104.061020`, all four lines, against stock servers.

<a id="binary-input-parameters-must-be-utf-8"></a>

### Binary input parameters: Rust settings and query parameters must be valid UTF-8

Go, Python and TypeScript send any bytes as a `settings` or `query_params` value, in the ABI's `{"b64": "<standard base64, padded>"}` form ([`guides/settings.md`](guides/settings.md)). The library accepts that form on every line, and a raw non-UTF-8 byte in the JSON itself is a usage error. `param_encoding=raw` is declined (`option-not-implemented`).

Rust's settings and query-parameter values are Rust `String`s, so they must be valid UTF-8: the `settings` and `params` fields of the schema, insert and filter option types are `Vec<(String, String)>`, and `ServerProfile.settings` is a `BTreeMap<String, String>`. In Rust, only column defaults and column names may be arbitrary bytes.

**Workaround** in Rust: inline the value as `unhex('<hex>')`, built from hex digits only.

**Measured** for the C ABI by the artifact producer on build `20261104.061020`, all four lines, against stock servers. The Rust types are read from the binding's public API.

<a id="zone-names-outside-clickhouses-embedded-table-follow-the-host"></a>

### On 26.3, 26.8 and 26.9, some zone names follow the host

All 598 names of `system.time_zones` compile into the library on all four lines, with no failures, and answer the same on every host with no zoneinfo installed. A name outside that table is a different matter, and it depends on the line:

- **26.10:** the server refuses any zone name outside `system.time_zones`, `posixrules`, `localtime` and `posix/*` included, with error 36 ("Time zone X is not supported"), and the library matches it exactly, with or without `TZDIR`. There is no host dependence.
- **26.3, 26.8 and 26.9:** the server accepts `posixrules`, `localtime` and `posix/<zone>`, and the library looks each up in the host's zoneinfo tree, `$TZDIR` if set, else `/usr/share/zoneinfo`:
  - `posix/<zone>` loads only where the host has a `posix/` tree. Ubuntu 24.04 has none.
  - `posixrules` loads only where the host has that file.
  - `localtime` loads only where `<zoneinfo>/localtime` resolves; on Debian and Ubuntu it is a symlink to `/etc/localtime`, so that file must exist too.

  Where the host lacks the file or tree, the library refuses the name with error 36, as a server whose image lacks it does. `right/*` names are refused with 36 on both sides.

**Workaround, on 26.3, 26.8 and 26.9 only:** to answer as the official server image does, point `TZDIR` at a copy of that image's zoneinfo (and provide its `/etc/localtime` for `localtime`). With that in place every one of these names loads and equals the server. On 26.10 there is nothing to configure: both sides refuse the names.

**Measured** by the artifact producer on build `20261104.061020`, all four lines (the 598 names of `system.time_zones` on each, and `posixrules`, `localtime` and `posix/*` on an Ubuntu 24.04 host with and without the files and `TZDIR`), against stock servers.

### A batch preview with a filter in a different zone than the batch is declined

When the filter was created in a different time zone than the batch it runs over, `chs_preview_batch` is declined (`unsupported`, reason `preview-batch-zone-mismatch`), and so is a named zone paired with none. The same zone on both sides, or none on both, answers. A decline never answers wrongly.

**Workaround:** use one zone for both, or parse the body with `chs_block_create` and evaluate with `chs_filter_eval_block`, or use a per-row preview.

**Measured** by the artifact producer on build `20261104.061020`, all four lines.

### No call for a server's version or settings

The library has no call that queries a server's version or its changed settings, and no settings reader. The caller supplies both. By design: the ABI has 41 exports and none queries a server; `chs_clickhouse_version` reports the library's own version.

**Workaround:** read the version and settings from your own connection and pass them in.

On ABI v2, a table a server already holds was created under that CREATE's own settings, which its `create_table_query` does not carry, so compile such a statement at `posture: load`, as the caller's obligation in [`spec/abi-v2/docs.md`](../spec/abi-v2/docs.md) (`input:schema_options`) requires: compiled at `create` instead, a table created under `flatten_nested = 0` has its `Nested` column read as flattened arrays.

**Measured** by the artifact producer on build `20261104.061020`: `abi.json` and the library's exported symbols agree on the 41 exports.

<a id="pre-10"></a>

## Matching a library to an SDK

A v2 library is matched to the SDK that opens it by the ABI fingerprint its signed statement carries, and the loader refuses a mismatch naming both sides ([`support-v2.md`](support-v2.md#sdk-and-abi)). ABI v2 is locked: it changes only by reserve and decline (rule r9 in [`spec/abi-v2/docs.md`](../spec/abi-v2/docs.md)), so an option a build does not implement is declined, never ignored. Each binding's own CHANGELOG carries what each release changes.

How a 1.x library and SDK were matched before 1.0 is in [`support-v1.md`](support-v1.md#pre-10).
