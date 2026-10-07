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
- **A batch whose TTL throws when the merge applies it**, on 26.3 to 26.8. For example, `v Int32 DEFAULT intDiv(10, k) TTL d + INTERVAL 100 YEAR` with a row where `k` is 0: the server's INSERT stores the row, and only the part's later merge meets the error (153). What the part will hold is therefore declined, and the decline's message quotes the error. The row preview still answers, and on 26.9 the batch is accepted.
- **MergeTree settings the server reads while it creates the table or inserts into it, declared at a non-default value**, and `disk` at any value. A setting only the storage layer reads, such as a part lifetime or a merge timeout, is accepted at any value, because the server stores the same rows with it as without it: `old_parts_lifetime = 100` compiles, while `index_granularity = 4096` is declined. An unknown _name_ is the server's own 115 and a value that does not parse is the server's own code (27 for `index_granularity = 'abc'`); both are rejections. A declined setting is never silently ignored. Library builds before `20261007.044112` declined every non-default value.
- **Server- and session-property DEFAULTs** — `hostName()`, `currentUser()` and the rest — **and the same functions in a `MATERIALIZED` expression**, such as `timezone()`, `hostName()` and `version()`, along with any column whose DEFAULT reads such a column. Their value is a property of the server, and there is no server here: the decline's message says that resolving it here would store the gateway's answer.
- **A `UNIQUE KEY`, with the feature enabled.** The server's INSERT into such a table runs the unique-key commit, which this build does not model. With the feature disabled, which is the default, the server's own refusal (344) is reported as a rejection; a `UNIQUE KEY` beside a `PROJECTION` is refused with 344 as well.
- **A query-level setting in a MergeTree table's storage `SETTINGS`**, such as `max_threads = 1`. The server moves it out of the table's `SETTINGS` clause into the CREATE's own context, which this build does not model.
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

⚠️ **What a verdict models.** A row preview (`chs_preview_row`) answers as a one-row `INSERT` of that row would. A batch preview (`chs_preview_batch`) answers as one synchronous `INSERT` of the whole batch would. So a row that a batch refuses because of another row in the same batch is not a divergence, and an entry below names which preview it concerns.

Every entry here has a machine-checkable twin in [`docs/divergences.json`](divergences.json). The v0 job that drove each one against loaded artifacts is retired with the v0 registry layout, so nothing checks these entries until a v1 replacement lands; treat each as a claim about the build named, and re-measure before relying on it.

**What gets an entry here, and when.** A divergence that changes the verdict or the stored value at default settings (an over-accept, an over-reject, or a row both sides accept but store differently) is listed as soon as the server's answer has been measured on each line it affects, and not before, because a wrong entry is worse than a late one. A divergence that is only a different error code, or that needs a non-default setting to reach, is listed if a published build still shows it seven days after it was found. Until then it's tracked with the artifact producer, whose comparison against real servers keeps finding such cases, and it's usually fixed in the next relink. Every entry is removed on the relink that fixes it.

### On 26.3, a non-UInt8 filter is evaluated where that server's projection pass refuses it

**Filter over-accept, on `26.3` only, where the server's query goes through its projection pass.** A filter whose `WHERE` expression has a type other than `UInt8`, for example `Float64` (`WHERE toFloat64(x) / 10 - 0.25`, `WHERE (toFloat64(x) - toFloat64(x)) / 0`), is answered `t` or `f` for every row. A `26.3` server answers such a filter the same way for a plain `SELECT … WHERE`, for `PREWHERE`, under the old analyzer and on a `Memory` table. It refuses the filter with error **59** only through its planner's projection pass: an aggregate over a MergeTree table that uses implicit projections, or a read of a table that declares a projection.

|                                            |                                      |
| ------------------------------------------ | ------------------------------------ |
| this library, on 26.3                      | answers `t` or `f` for each row      |
| a 26.3 server, through the projection pass | refuses the filter with error **59** |
| a 26.3 server, otherwise                   | answers `t` or `f`, as this library  |

On `26.7`, `26.8` and `26.9` the server answers these filters in every case measured, and this library agrees.

**Measured**: by the artifact producer, against a library built from the same core commit as the production library build `20261006.220511`, on all four supported lines, on linux-amd64 and linux-arm64; the narrowing to the projection pass in a later run on `26.3`. The production artifact itself is not measured; it is expected to behave the same (`inferred`: the same source). A library change (declining such a filter on `26.3`) awaits a ruling. This entry has no machine check (see its register twin), so it retires on the artifact producer's measured re-run against the production library build that ships the change, never on a CI result.

This still reproduces on library build `20261007.120436`, on `26.3` only.

Until then, on `26.3`, if the server query a filter stands for reads a table that declares a projection, or aggregates over a MergeTree table, do not treat a verdict for a non-`UInt8` `WHERE` as the server's answer.

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

### A Replicated table assumes the server has Keeper

This library compiles a `Replicated*MergeTree` CREATE on the assumption that the server it stands for has ClickHouse Keeper (or ZooKeeper) configured, as a replicated deployment does. A stock server with no Keeper refuses such a CREATE, so on that server a successful schema creation here is not proof the CREATE will succeed. That is a fact about the deployment, which this library cannot see, not a divergence in a supported setup (`measured` by the artifact producer on production build `20261006.220511`).

It applies to replicated paths that need no `{shard}` or `{replica}` macro. Those two macros are server configuration, so this release declines them (`unsupported`). `{database}`, `{table}` and `{uuid}` are answered as the server would.

## Pre-1.0

How a library and the SDK opening it are matched before 1.0 is in [`support-v1.md`](support-v1.md#pre-10).

Anything else may still move before 1.0. Each binding's own CHANGELOG carries its list.
