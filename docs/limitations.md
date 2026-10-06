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
- **DEFAULT expressions past the admission budgets** — 256 MiB and one second by default, both adjustable through the process-wide settings in [`guides/settings.md`](guides/settings.md). This is what bounds a blocking DEFAULT: `sleep` and `sleepEachRow` are not declined by name, so `DEFAULT sleep(0)` is admitted, while `DEFAULT sleep(1.5)` exceeds the one-second budget and is declined when the schema compiles.

## Constants are not payloads

Everything on the row path is **insert-side** coercion. Never reuse it to fold a `WHERE`-clause constant.

The rules genuinely differ: `256` into a `UInt8` column stores `0`, while `x = 256` over that column promotes and is false for every row. Refuse an operand outside the column type's domain instead — or use [`guides/filters.md`](guides/filters.md), which is the surface that answers comparison questions with ClickHouse's own comparison functions.

## Filters are for comparison, not enforcement, for now

No read-side security may be enforced on the filter surface until a release explicitly lifts this limitation — the CHANGELOG will say so, and until it does, assume it has not. Until then the surface is for shadow and replay: run it beside your existing enforcement and compare, do not replace.

The parse-once block twin does not change that — it is a performance shape, not a maturity signal, and sits under the same gate. This is the canonical statement of the gate; `docs/guides/filters.md` points here rather than restating the criterion. For the rule a filter's own result type must follow, see [`guides/filters.md` → Writing a filter's result type](guides/filters.md#writing-a-filters-result-type).

**The gate lifts per `(ClickHouse line, platform)`, never all at once.** A pair's warning lifts once that pair has **three consecutive records with zero over-admits and zero over-hides against a real server, and no open filter divergences for it** (see [Known divergences](#known-divergences) below). A pair that diverges again after being lifted gets the warning back — a lift is a state, not a one-way promotion, and lifting one pair says nothing about any other.

**Each lift is announced in the CHANGELOG, by `(line, platform)`, as its own entry** — the fixed shape is in [`CONTRIBUTING.md`](../CONTRIBUTING.md#changelog-entries). There is no other record of a lift: a pair stays gated until its own CHANGELOG entry says otherwise.

**Today no `(line, platform)` pair is lifted.** Every supported line, on every published platform, is still under the gate above.

This criterion is scored against the artifact producer's own differential comparison against real servers, and the per-`(line, platform)` state it produces is not served yet — the registry carries no such field today. Once the artifact producer serves one, this page reads it directly, the same principle [`support-v1.md`](support-v1.md) follows for line support: it states what the registry says rather than listing lines by hand. Until then, do not infer a lift from anything but a CHANGELOG entry naming the pair.

## Some formats depend on the artifact, not the binding

Which input formats a library reads is a property of the **loaded artifact**, not of your binding's version: every 1.0 build lists them in its `build_info`. Probe that rather than assuming from the package version. A format the loaded library does not read is refused with 73 or 117, exactly as its own server would refuse it.

## The error model is normative

`unsupported` (`CODE_UNSUPPORTED`, the wire sentinel `-2`) is neither an acceptance nor a rejection, and treating it as either is the most expensive mistake available here.

Over-accepts and over-rejects have **no budget** in the differential proof the artifacts are built from: a row accepted here and rejected by the server ships before the insert fails, and a row rejected here and accepted by the server is silent data loss. Neither is acceptable, so neither gets an allowance. A decline is how the system stays honest about the cases it cannot reach that bar on.

**No budget is a rule about process, not a claim about state.** A non-zero count, in either direction, on any binding against any ClickHouse line, is refused unless a person has named that case and recorded why, with a tracking reference attached. Nothing non-zero passes quietly, and no threshold waves anything through.

**So it does not mean there are none.** The cases known today, in either direction, are listed under [Known divergences](#known-divergences) below, each with the build it was measured on and the workaround until it is fixed. The three that were registered for 0.x (a DEFAULT over `demangle`, a CHECK constraint, and a filter whose result is not a boolean-context type) are fixed in every 1.0 build: this repository measured the `demangle` refusal (446) through the published Go binding on all four supported lines, and the artifact producer measured the filter and CHECK cases on production. Nothing in that list is a promise about tomorrow: the differential proof keeps finding cases, and each one is listed as soon as its server half is measured.

⚠️ **Zero in both directions is a statement about the verdict, not about the value or the error.** There are two other ways to disagree: both sides accept a row and **store different values**, or both refuse it and **report different codes**. Neither is an accept-or-reject disagreement, so the no-budget rule above does not cover them.

They are measured all the same, by the same differential proof, not by this repository. Any that a caller can reach is listed under [Known divergences](#known-divergences), the same as a verdict divergence. Getting there meant investigating the cases one at a time. Some were fixed in the library. Others turned out not to be something a caller can reach, because the disagreement was a property of how the comparison itself was run. Any that a caller **can** reach are listed under [Known divergences](#known-divergences) below.

In Python specifically, `UnsupportedError` is a **peer** of `SchemaError` rather than a subclass, so `except SchemaError` never catches a decline. Handle the two arms explicitly, or catch `ChtypesError` for both. The subtype was retired precisely because catching one and getting the other is a silent misclassification.

The ABI header is the full contract.

## An out-of-domain Enum DEFAULT is refused at compile

This is by design, because the server does too: on every supported line, compiling such a schema refuses it (`691`, or `70`). The rule and the codes are in [`transformations.md`](guides/transformations.md#an-out-of-domain-enum-default-follows-the-server).

## Known divergences

Each entry here is a case where this library and a real ClickHouse server give different answers, and a caller can reach it. Every entry names the direction, the input, both answers, and **which half of the comparison was measured where** — there is no ClickHouse server in this repository, so the server half always comes from the differential proof the artifacts are built from.

An entry disappears when an artifact stops diverging, or when the disagreement turns out to have been a property of how it was measured rather than of the library. Pin nothing to this list.

⚠️ **"A real server" means a real table engine** — a MergeTree table, the kind a tenant writes to. A `CREATE TEMPORARY TABLE` is `ENGINE=Memory`, has no parts, and accepts values that every ordinary table refuses at part-write time. If you reproduce an entry against a temporary table you will not get the answer recorded here, and the temporary table is the one that is wrong about your production write.

Every entry here has a machine-checkable twin in [`docs/divergences.json`](divergences.json). The v0 job that drove each one against loaded artifacts is retired with the v0 registry layout, so nothing checks these entries until a v1 replacement lands; treat each as a claim about the build named, and re-measure before relying on it.

**What gets an entry here, and when.** A divergence that changes the verdict or the stored value at default settings (an over-accept, an over-reject, or a row both sides accept but store differently) is listed as soon as the server's answer has been measured on each line it affects, and not before, because a wrong entry is worse than a late one. A divergence that is only a different error code, or that needs a non-default setting to reach, is listed if a published build still shows it seven days after it was found. Until then it's tracked with the artifact producer, whose comparison against real servers keeps finding such cases, and it's usually fixed in the next relink. Every entry is removed on the relink that fixes it.

### The row preview skips the engine's insert-time merge step on Collapsing and Replacing-with-`is_deleted` tables

**Over-accept, on every supported line, through the row preview only.** `chs_preview_row` (the row preview) does not run the table engine's insert-time merge step. A real server's single-row INSERT runs it, and it refuses a row whose `sign` or `is_deleted` is out of range. For example, a one-row preview is accepted for each of:

- `CollapsingMergeTree(sign)` with `sign = 7`;
- `ReplacingMergeTree(ver, is_deleted)` with `is_deleted = 2`.

|                           |                                                          |
| ------------------------- | -------------------------------------------------------- |
| this library, row preview | accepts the row                                          |
| a real server             | refuses the INSERT with error **117** (`INCORRECT_DATA`) |

The batch preview (`chs_preview_batch`) of the same row answers correctly, with 117, so it agrees with the server.

**Measured**: by the artifact producer, against the production library build `20261004.052404` on all four supported lines (`26.3`, `26.7`, `26.8`, `26.9`), on linux-amd64 and linux-arm64; the server half against the server's single-row INSERT. A library fix is in progress. This entry has no machine check (see its register twin), so it retires on the artifact producer's measured re-run against the production library build that ships the fix, never on a CI result.

Until then, use the batch preview, even for one row, on `CollapsingMergeTree`, `VersionedCollapsingMergeTree` and `ReplacingMergeTree` with an `is_deleted` column, **unless the table's sign, version or `is_deleted` column is `MATERIALIZED`**. On such a table the batch preview can refuse a valid row (see [the batch preview refuses with code 10 when an engine column is MATERIALIZED](#the-batch-preview-refuses-a-row-with-code-10-when-an-engine-column-is-materialized)), so neither preview is a complete answer there.

### Creating a schema accepts MergeTree-family CREATE statements that a real server refuses

**Over-accept, on every supported line, at schema creation.** `chs_schema_create` (the schema-create call) accepts some MergeTree-family `CREATE TABLE` statements that a real server refuses at the CREATE. The cases measured:

- wrong engine arguments: `SummingMergeTree(a, b)`, `AggregatingMergeTree(x)`, `MergeTree(k)`;
- a `String` version column, or a `UInt8` sign column;
- a missing sign, version or columns-to-sum column;
- `ReplacingMergeTree(ver)` with no `ORDER BY`;
- the deprecated engine syntax;
- `ReplacingMergeTree` with a projection (the server refuses with error **344**);
- from 26.7, an `AggregatingMergeTree` dimension outside the keys (the server refuses with error **36**).

For every other case in the list, the server refuses the CREATE.

**Measured**: by the artifact producer, against the production library build `20261004.052404` on all four supported lines (`26.3`, `26.7`, `26.8`, `26.9`), on linux-amd64 and linux-arm64. A library fix is in progress. This entry has no machine check (see its register twin), so it retires on the artifact producer's measured re-run against the production library build that ships the fix, never on a CI result.

Until then, do not treat a successful schema creation on these engines as proof that the server will accept the CREATE.

### A DEFAULT that throws on a row which supplies the column can fail the server's INSERT while this library accepts it

**Over-accept, on every supported line, with `input_format_defaults_for_omitted_fields=1` (the server's default).** A server evaluates a column's `DEFAULT` expression over the whole INSERT block, including the rows that supply the column, whenever some other row of the block omits it. This library accepts every row. For example, with `b Int32 DEFAULT intDiv(10, a)`, a batch in which some rows omit `b` and one row is `{"a":0,"b":5}`:

|               |                                                                             |
| ------------- | --------------------------------------------------------------------------- |
| this library  | accepts every row                                                           |
| a real server | refuses the INSERT, with error **153**, **395** or **70** by the expression |

A batch in which every row supplies the column, and a single row, agree with the server. With `input_format_defaults_for_omitted_fields=0` the server also accepts a mixed batch, so there is no divergence.

The reverse is an over-reject, and needs a non-default setting, so it is a line here and not an entry of its own. With `b Int32 DEFAULT intDiv(10, a)` and a column `m … MATERIALIZED b + 1`, in `JSONEachRow` with `input_format_defaults_for_omitted_fields=0`, a row that omits `b` with `a = 0` is accepted by a server (`b` takes its type's default) and refused by this library with error 153.

**Measured**: by the artifact producer, against the production library build `20261004.052404` and its successor `20261006.170903`, on all four supported lines (`26.3`, `26.7`, `26.8`, `26.9`), on linux-amd64 only, with identical results. A library fix is in progress. This entry has no machine check (see its register twin), so it retires on the artifact producer's measured re-run against the production library build that ships the fix, never on a CI result.

Until then, do not trust an accepted batch that mixes rows omitting and rows supplying a DEFAULT column whose expression can throw.

### A Nullable sorting key is accepted

**Over-accept, on every supported line, at schema creation, linux-amd64 only.** `ORDER BY k` (also `ORDER BY (k, v)`, or `PRIMARY KEY k`) over a `Nullable` column, without `allow_nullable_key`, is accepted by `chs_schema_create`. A real server refuses the CREATE with error **44**.

`allow_nullable_key = 1`, and `ifNull` or `assumeNotNull` keys, are declined (`unsupported`) rather than accepted: an over-decline, not an entry here.

**Measured**: by the artifact producer, against the production library build `20261004.052404` and its successor `20261006.170903`, on all four supported lines (`26.3`, `26.7`, `26.8`, `26.9`), on linux-amd64 only. A library fix is in progress. This entry has no machine check (see its register twin), so it retires on the artifact producer's measured re-run against the production library build that ships the fix, never on a CI result.

Until then, do not treat a successful schema creation with a Nullable sorting key as proof that the server will accept the CREATE.

### A clock-reading MATERIALIZED expression reports 1970, or ignores the clock offset, in both previews

**Value divergence, on every supported line, through both the batch and the row preview.** In a batch preview with no pinned clock, where no row omits a Volatile DEFAULT column, a clock-reading `MATERIALIZED` expression (`now()`, `now64()`, `today()`) reports `1970-01-01` in the batch document's `computed` values. A real server stores the current time. In a mixed batch, the rows that supply the defaulted column get 1970, and the rows that omit it get the current time.

With a pinned clock (`chtypes_now_epoch_nanos`) plus `chtypes_clock_offset_nanos`, an omitted DEFAULT applies the offset, but a `MATERIALIZED` expression ignores it.

The row preview (`chs_preview_row`) is affected the same way. With no pinned clock, its clock-reading `MATERIALIZED` values are `1970-01-01 00:00:00`; with a pinned clock plus an offset, its clock-derived values carry the pin alone, ignoring the offset. Neither returns an error: the row document's outcome is OK, with the wrong value in it.

**Measured**: by the artifact producer, the batch preview against the production library build `20261004.052404` and its successor `20261006.170903`, on all four supported lines (`26.3`, `26.7`, `26.8`, `26.9`), on linux-amd64, with identical results; the row preview against `20261006.170903` on all four lines, on linux-amd64 and linux-arm64. A library fix is in progress. This entry has no machine check (see its register twin), so it retires on the artifact producer's measured re-run against the production library build that ships the fix, never on a CI result.

Until then, do not trust clock values from either preview (a batch document's `computed` values, or the row document's) unless at least one row omits a Volatile DEFAULT column, or the clock is pinned with no offset.

### A projection with no ORDER BY is accepted at schema creation; a real server refuses the CREATE

**Over-accept, on every supported line, at schema creation, linux-amd64 only.** A table whose `PROJECTION` has no `ORDER BY` is accepted by `chs_schema_create`, and a preview then accepts rows for it. A real server refuses the CREATE with error **36** (`ORDER BY cannot be empty`).

**Measured**: by the artifact producer, against the production library build `20261006.170903` and its predecessor `20261004.052404`, on all four supported lines (`26.3`, `26.7`, `26.8`, `26.9`), on linux-amd64 only. A library fix is in progress. This entry has no machine check (see its register twin), so it retires on the artifact producer's measured re-run against the production library build that ships the fix, never on a CI result.

Until then, do not treat a successful schema creation as proof that the server will accept a CREATE whose projections have no `ORDER BY`.

### The row preview skips the TTL step: a row whose TTL expression throws is accepted, where a real server refuses it

**Over-accept, on every supported line, through the row preview only.** `chs_preview_row` (the row preview) does not evaluate a table's `TTL` expressions. A real server evaluates them during the INSERT, and one that throws on a row refuses the INSERT. For example, with `TTL toDate(s) + INTERVAL 100 YEAR` and a row where `s = 'abc'`:

|                           |                                      |
| ------------------------- | ------------------------------------ |
| this library, row preview | accepts the row                      |
| a real server             | refuses the INSERT with error **38** |

The batch preview (`chs_preview_batch`) of the same row answers correctly, with 38.

**Measured**: by the artifact producer, against the production library builds `20261004.052404` and `20261006.170903` on all four supported lines (`26.3`, `26.7`, `26.8`, `26.9`), on linux-amd64 and linux-arm64. A library fix is in progress. This entry has no machine check (see its register twin), so it retires on the artifact producer's measured re-run against the production library build that ships the fix, never on a CI result.

Until then, use the batch preview, even for one row, on a table with a `TTL`.

### The batch preview refuses a row with code 10 when an engine column is MATERIALIZED

**Over-reject, on every supported line, through the batch preview.** On a table without a `PARTITION BY` whose sign, version, `is_deleted` or sorting-key column is `MATERIALIZED`, `chs_preview_batch` (the batch preview) refuses the row with code **10**. A real server accepts it, or refuses it with **117** where the engine's own check applies. On a `SummingMergeTree` table of this kind, the batch document can also report `engine_rows: []` where the server stores the row.

|                             |                                                           |
| --------------------------- | --------------------------------------------------------- |
| this library, batch preview | refuses the row with code **10**                          |
| a real server               | accepts the row (or refuses it with **117**, by the data) |

**Measured**: by the artifact producer, against the production library builds `20261004.052404` and `20261006.170903` on all four supported lines (`26.3`, `26.7`, `26.8`, `26.9`), on linux-amd64 and linux-arm64. A library fix is in progress. This entry has no machine check (see its register twin), so it retires on the artifact producer's measured re-run against the production library build that ships the fix, never on a CI result.

Until then, on such a table, do not read a batch preview's refusal with code 10 as the server's answer. The row preview accepts these rows, but it does not run the engine's merge step (see [the entry above](#the-row-preview-skips-the-engines-insert-time-merge-step-on-collapsing-and-replacing-with-is_deleted-tables)), so it cannot catch an out-of-range sign or `is_deleted` either.

### The batch preview refuses a row with code 10 when a TTL expression reads a MATERIALIZED column

**Over-reject, on every supported line, through the batch preview.** On a table whose `TTL` expression reads a `MATERIALIZED` column, `chs_preview_batch` (the batch preview) refuses the row with code **10** (`Not found column or subcolumn … in block`). A real server accepts it.

|                             |                                  |
| --------------------------- | -------------------------------- |
| this library, batch preview | refuses the row with code **10** |
| a real server               | accepts the row                  |

**Measured**: by the artifact producer, against the production library build `20261006.170903` on all four supported lines (`26.3`, `26.7`, `26.8`, `26.9`), on linux-amd64 and linux-arm64. A library fix is in progress. This entry has no machine check (see its register twin), so it retires on the artifact producer's measured re-run against the production library build that ships the fix, never on a CI result.

Until then, on such a table, do not read a batch preview's refusal with code 10 as the server's answer.

### A column TTL that throws on a row the table's rows TTL expires is accepted, where a real server refuses it

**Over-accept, on every supported line, through both previews.** A real server evaluates every `TTL` expression over every row of the INSERT, including a column `TTL` on a row that the table's rows `TTL` already expires, and refuses the INSERT if one throws. This library does not evaluate that column `TTL` there. For example:

```sql
CREATE TABLE t (k UInt32, d Date, s String, v UInt8 TTL toDate(s) + INTERVAL 100 YEAR)
ENGINE = MergeTree ORDER BY k TTL d + INTERVAL 1 DAY
```

with the row `k = 1, d = '2020-01-01', s = 'abc', v = 1`, in JSONEachRow or CSV:

|                             |                                      |
| --------------------------- | ------------------------------------ |
| this library, both previews | accepts the row                      |
| a real server               | refuses the INSERT with error **38** |

The row preview's acceptance is also the [row preview's skipped TTL step](#the-row-preview-skips-the-ttl-step-a-row-whose-ttl-expression-throws-is-accepted-where-a-real-server-refuses-it); this entry is the batch preview's.

**Measured**: by the artifact producer, against the production library build `20261006.170903` on all four supported lines (`26.3`, `26.7`, `26.8`, `26.9`), on linux-amd64 and linux-arm64. A library fix is in progress. This entry has no machine check (see its register twin), so it retires on the artifact producer's measured re-run against the production library build that ships the fix, never on a CI result.

Until then, do not trust an accepted row on a table with both a rows `TTL` and a column `TTL` whose expression can throw.

### The batch preview evaluates a column's DEFAULT for a column TTL when the row supplies the column

**Over-reject, on `26.3`, `26.7` and `26.8`, through the batch preview.** For a column with both a `DEFAULT` and a `TTL`, the batch preview evaluates the `DEFAULT` at insert time even when the row supplies the column, and refuses the row if it throws. A real server evaluates only the column `TTL` expression at insert; the `DEFAULT` runs only when a later merge resets an expired value. For example:

```sql
CREATE TABLE t (k Int32, d Date, v Int32 DEFAULT intDiv(10, k) TTL d + INTERVAL 100 YEAR)
ENGINE = MergeTree ORDER BY k
```

with the row `k = 0, d = '2026-01-01', v = 5`, which supplies `v`:

|                             |                                    |
| --------------------------- | ---------------------------------- |
| this library, batch preview | refuses the row with error **153** |
| a real server               | accepts the row                    |

The row preview accepts the row, agreeing with the server.

**Measured**: by the artifact producer, against the production library build `20261006.170903` on `26.3`, `26.7` and `26.8`, on linux-amd64 and linux-arm64. `26.9` is not yet confirmed either way. A library fix is in progress. This entry has no machine check (see its register twin), so it retires on the artifact producer's measured re-run against the production library build that ships the fix, never on a CI result.

Until then, on such a table, do not read a batch preview's refusal with 153 as the server's answer when the row supplies the column.

## Known gaps in 1.0

Each item is a place where 1.0 does less than you might expect, or answers differently from a server. None of them returns a wrong answer without saying so, and every one is planned. Each entry says what happens, what to do today, and that a fix is planned.

### Nested String values carry no raw bytes

A `String` value nested inside `Array(String)`, `Map` or `Tuple` carries no raw `value` bytes, only ClickHouse's rendering of it. A scalar `String` or `FixedString` value always carries its raw bytes, including inside `Nullable` and `LowCardinality`.

**Workaround:** read the rendering for a nested value, or select the nested element as its own scalar column when you need the bytes. **Planned.**

### Binary input parameters must be UTF-8

The values of `settings` and `query_params` must be UTF-8. A value that is not valid UTF-8 is refused.

**Workaround:** inline the value as `unhex('<hex>')`, built from hex digits only. **Planned:** a byte-safe object form.

### Zone names outside ClickHouse's embedded table follow the host

Every zone in ClickHouse's own embedded table, the 598 names of `system.time_zones` on 26.3, 26.7, 26.8 and 26.9 (tzdata 2026d), is compiled into the library. It answers the same on every host, with no zoneinfo installed. A name outside that table is looked up the way a ClickHouse server looks it up: in the host's zoneinfo directory, `$TZDIR` if set, else `/usr/share/zoneinfo`. Three kinds of name depend on it:

- `posix/<zone>` loads only where the host has a `posix/` tree. The `clickhouse/clickhouse-server` images (Ubuntu 22.04) and Debian 12 have one. Ubuntu 24.04 has none, even with `tzdata-legacy`.
- `posixrules` loads only where the host has that file. Ubuntu's and Debian's `tzdata` install it.
- `localtime` loads only where `<zoneinfo>/localtime` resolves. On Debian and Ubuntu it is a symlink to `/etc/localtime`, so that file must exist too.

Where the host lacks the file, the library refuses the name with ClickHouse's error 36, as a server whose image lacks it does. `right/*` names are refused on every host, even where the files exist, because ClickHouse's loader rejects leap-second data. When both accept a name, the library and the server render values identically. The library needs glibc. On darwin, the host lookup folds case.

**Workaround:** to answer as the official server image does, give the library host that image's zoneinfo. Copy its `/usr/share/zoneinfo`, or point `TZDIR` at a copy, and copy its `/etc/localtime` for `localtime`. With both in place, the library agreed with the server image on every name tested, on all four lines.

### `SHOW CREATE` of a Memory table is declined

`SHOW CREATE TABLE` text from live 26.3, 26.7, 26.8 and 26.9 servers compiles with the schema call for every MergeTree-family shape measured, which is 28 of the 32 shapes. The 4 `ENGINE = Memory` tables are declined, not mis-compiled.

**Workaround:** use the discovery calls for those tables. **Planned.**

### A batch preview with a filter in a different zone than the batch is declined

When the filter evaluates in a different time zone than the batch it runs over, the preview is declined. It never answers wrongly.

**Workaround:** use one zone for both, or a per-row preview. **Planned.**

### Per-call settings values are not validated the way a server's `SET` validates them

**Fixed in the artifacts, build `20261004.021416` and later** (no SDK change). The artifact producer reports that an invalid value for a per-call setting is now refused with the server's own error code, as a server's `SET` refuses it, and that a filter created with an invalid `session_timezone` is refused at create (code 36). Every tag and line resolves to that build by default.

On the first 1.0 build, `20261003.231921`, which stays fetchable by digest and through a lock that pins it, some invalid values are still accepted. **Workaround on that build:** validate settings values in the caller, or against a server, before relying on a refusal here, or move to the current build: `chtypes fetch <line>` resolves it, and a locked project re-resolves with `chtypes fetch --lock <file> --update`.

### `lossy` on a String holding a raw NUL in TSV

For a TSV `String` that contains a raw NUL byte, the third detector can report `value_changed` with `lossy: true` although the bytes are preserved.

**Workaround:** compare `value`, the raw bytes, with your input; the bindings never second-guess `lossy`. **Planned:** fixed in 1.0.x.

### No call for a server's version or settings

1.0 has no call that queries a server's version or its changed settings, and no settings reader. The caller supplies both.

**Workaround:** read the version and settings from your own connection and pass them in. **Planned.**

### A time-dependent TTL in a CREATE comes back as an internal error

`TTL now() + INTERVAL 1 DAY` in a `CREATE TABLE` returns an `InternalError` (`CHS_INTERNAL`) where a server refuses the statement with its own error. Measured against the 26.9.8.3 library (`measured`).

**Workaround:** treat an internal error from a CREATE whose TTL does not reference a column as that refusal. **Planned:** fixed in 1.0.x.

## Pre-1.0

How a library and the SDK opening it are matched before 1.0 is in [`support-v1.md`](support-v1.md#pre-10).

Anything else may still move before 1.0. Each binding's own CHANGELOG carries its list.
