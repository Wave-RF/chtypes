# chtypes — TypeScript SDK

`@wavehouse/chtypes`: ClickHouse's own type system, schema validation,
DEFAULT/TTL logic and coercion, per ClickHouse version, behind the
`chs_*` C ABI (pre-1.0 only the names and the format integers are frozen —
signatures can still change by deliberate cycle, which is why the loader
checks `chs_abi_revision` before trusting them). One peer binding among
`{go,python,ts,rust}`; no
language is privileged. Nothing here reimplements a coercion rule — every
verdict comes from real ClickHouse code compiled from the pinned release; the
one derived answer is `transformed`, the silent-change report the product
exists for.

Node ≥ 22, ESM only. Native calls go through `ffi-rs` (prebuilt for
darwin-arm64/x64 and linux arm64/x64, gnu and musl — no build step).

## Install

```sh
pnpm add @wavehouse/chtypes
```

To work against a checkout instead, wire it up by path — with pnpm, this
repo's package manager:

```jsonc
// your app's package.json
{ "dependencies": { "@wavehouse/chtypes": "file:../chtypes/ts" } }
```

or `pnpm link <repo>/ts`. Either way, build the package's `dist/`
first — that is what `package.json#exports` serves:

```sh
cd <repo>/ts
pnpm install && pnpm build
```

(If a linked copy goes stale, note `pnpm install --force` does **not** relink;
remove `node_modules` and reinstall.)

**Artifact prerequisite.** The SDK loads per-version native artifacts; it does
nothing without them. Fetch a published one — verified, into the per-user cache
every SDK here defaults to — with the package's own command, or build one in
the core repository, which lands it in the same place:

```sh
npx @wavehouse/chtypes fetch 25.8      # -> ~/.cache/chtypes/artifacts/<os>-<arch>/25.8/
```

(`scripts/fetch.sh` at the repository root is the reference implementation of
the same contract, `docs/fetch.md`; the command, the function and the lazy
fetch are in [Artifacts: fetch, verify, autofetch](#artifacts-fetch-verify-autofetch).)

A registry holds one subdirectory per version:

    <registry>/25.8/
      manifest.json         # names the library file — the loader reads this, never guesses
      libchtypes.dylib      # (.so on Linux; the name comes from manifest.json)
      CH_VERSION
      unsafe_families.txt   # the build's own refuse-list; empty is a valid list

`new Registry(dir?)` scans such a directory. Where it looks is the search path
of `docs/fetch.md` §1, in order: the explicit `dir`, `$CHTYPES_REGISTRY`, the
per-user cache (`defaultRegistryDir()`), then the reserved system locations
`/usr/local/share/chtypes/artifacts/<os>-<arch>` and `/opt/chtypes/artifacts/<os>-<arch>`.
The first directory holding artifacts is loaded at construction; a line it
lacks is loaded from the first later directory that has it when first asked
for (`registry.searchPath` lists every candidate; a named directory — the
argument or the variable — that does not exist is an error naming it, never a
silent fallback). For each artifact the
loader dlopens the file `manifest.json#library` names, asks the library to name
itself (`chs_clickhouse_version()` — nothing is inferred from paths), and
checks the ABI revision: `chs_abi_revision()` must equal this binding's
`ABI_REVISION` (currently 4). A different nonzero revision is **refused** —
calling through mismatched declarations is undefined; `0` means the artifact
predates the probe and individual missing symbols degrade to
`UnsupportedError` at call time, never to a load failure.

## Artifacts: fetch, verify, autofetch

The fetch/verify contract every SDK implements identically is
[`docs/fetch.md`](../docs/fetch.md); this package carries it as a `bin` and
as a function.

```sh
npx @wavehouse/chtypes fetch 25.8                  # one line, verified, into the per-user cache
npx @wavehouse/chtypes fetch --all                 # every line the release publishes for this host
npx @wavehouse/chtypes fetch 25.8 --platform linux-arm64 --dest /opt/chtypes/artifacts/linux-arm64
npx @wavehouse/chtypes fetch 25.8 --lock chtypes.lock   # record what was installed (file + sha256)
npx @wavehouse/chtypes fetch 25.8 --frozen              # refuse anything chtypes.lock does not pin
npx @wavehouse/chtypes verify                      # re-hash every installed line against its manifest
npx @wavehouse/chtypes list                        # what is installed, and what the release offers
npx @wavehouse/chtypes where                       # the registry directory fetch writes to
```

`fetch` prints the installed directory alone on stdout (progress on stderr),
so `dir="$(npx @wavehouse/chtypes fetch 25.8)"` composes. Exit codes: 0 ok ·
1 verification failed · 2 usage · 3 source unreachable · 4 not published for
this platform/line. A line (`25.8`) resolves to the one patch the release
publishes for it; an exact patch (`25.8.28.1-lts`) is a hard requirement.
Sources: `--tag v1.2.0` picks a frozen release on `CHTYPES_ARTIFACTS_URL`
(default `https://artifacts.wavehouse.dev`; the rolling `artifacts` release
otherwise); `--url <base>` is any other base — a mirror, a `file://` path or
a plain directory. `--offline` never touches the source: installed and
verified is fine, anything else is `CHTYPES_SOURCE_UNREACHABLE`. Fetch writes
to the first of `--dest`, `$CHTYPES_REGISTRY`, the per-user cache; never to a
system location.

**The verification chain**, in order — nothing else is a verdict, not an exit
code, not a `Content-Length`: (0) `SHA256SUMS.sig`, an ed25519 signature over
the bytes of `SHA256SUMS`, verified with the embedded release key
(`RELEASE_PUBLIC_KEYS`, key id `deb275922dbff76e`) or with the keys in
`CHTYPES_TRUSTED_KEYS=<hex>[,<hex>…]`, which *replace* it — an unsigned or
mis-signed release is `CHTYPES_ARTIFACT_UNTRUSTED` and nothing is downloaded
around it (`CHTYPES_ALLOW_UNSIGNED=1` skips this step with one loud warning
naming the source, never silently); (1) `index.json` names the asset and its
sha256; (2) the now-authentic `SHA256SUMS` must agree; (3) the tarball is
hashed before it is unpacked; (4) `manifest.json` inside must agree with the
index, and the installed library is re-hashed in place after the atomic
rename. Any mismatch is `CHTYPES_ARTIFACT_CORRUPT` and nothing is installed.
Installed-and-verified is a no-op (`--force` re-downloads). Once files are in
a registry directory the loader trusts the directory, as a runtime trusts
`node_modules` — pass `verifyChecksums: true` to re-hash at load.

The same thing as a function:

```ts
import { ensure } from '@wavehouse/chtypes';

const r = await ensure('25.8');            // idempotent; r.dir is <registry>/25.8, r.installed says whether bytes moved
await ensure('25.8', { lock: 'chtypes.lock', frozen: true });          // CHTYPES_ARTIFACT_PINNED on drift
await ensure('25.8', { url: 'file:///srv/mirror', trustedKeys: [hex] }); // a mirror signed by someone else
```

Every flag is a property of `EnsureOptions` (`dest`, `platform`, `tag`, `url`,
`lock`, `frozen`, `force`, `offline`, `trustedKeys`, `allowUnsigned`,
`onProgress`, `signal`); `ensureAll`, `verifyInstalled` and `listArtifacts`
are `--all`, `verify` and `list`. Concurrent `ensure`s of one line in one
process share one fetch.

**Lazy fetch on first open is opt-in** — `new Registry(dir, { autofetch: true })`
or `CHTYPES_AUTOFETCH=1` — because a production process must not begin a
250 MB download inside a request. `Registry#for()` is synchronous and never
fetches; `Registry#open()` is its async twin: with autofetch on, a line no
directory on the search path holds is fetched through `ensure` first (into the
directory fetch writes to, once per process per line even under concurrent
opens), then loaded.

```ts
const registry = new Registry(undefined, { autofetch: true, fetch: { tag: 'v1.2.0' } });
const lib = await registry.open('25.8');   // fetched on first use, loaded from then on
```

**The one error.** A line no directory on the search path holds is an
`ArtifactMissingError` — a `RegistryError` with `code === 'CHTYPES_ARTIFACT_MISSING'`
and this message, verbatim apart from the bracketed parts:

```
chtypes: no artifact for ClickHouse <line> (<os>-<arch>). Looked in: <dir1>, <dir2>, ….
Install it:  npx @wavehouse/chtypes fetch <line>
or set CHTYPES_AUTOFETCH=1 to fetch on first use.
```

The fetch verdicts are `FetchError` subclasses carrying the shared codes:
`ArtifactUntrustedError` (`CHTYPES_ARTIFACT_UNTRUSTED`), `ArtifactCorruptError`
(`CHTYPES_ARTIFACT_CORRUPT`), `ArtifactPinnedError` (`CHTYPES_ARTIFACT_PINNED`),
`ArtifactUnpublishedError` (`CHTYPES_ARTIFACT_UNPUBLISHED`) and
`SourceUnreachableError` (`CHTYPES_SOURCE_UNREACHABLE`). Artifacts are Elastic
License 2.0, separate from this package's Apache 2.0; `LICENSE` and `NOTICE`
ship in every release and `fetch` says so once.

## Quickstart

```ts
import { Format, Registry, UnsupportedError } from '@wavehouse/chtypes';

const registry = new Registry();      // $CHTYPES_REGISTRY, else the per-user cache
const lib = registry.for('25.8');     // minor line or exact patch both resolve

// Compile under the deployment's declared settings profile — a type gate
// declared here binds at compile, exactly as that server's CREATE TABLE does.
using schema = lib.compileDdl('id LowCardinality(UInt32), x UInt8, ts DateTime DEFAULT now()', {
  settings: { allow_suspicious_low_cardinality_types: '1' },
});
// `using` needs explicit resource management: TypeScript downlevels it, and
// plain JavaScript needs Node >= 24. On Node 22 call `schema.close()` instead —
// same effect, and every disposable here has both.

const good = schema.row(Format.JSONEachRow, Buffer.from('{"id": 7, "x": 256}'));
console.log(good.outcome);                  // accepted
console.log(good.transformed[0]?.reason);   // overflow_wrap — 256 stored as 0, silently
console.log(good.substituted[0]?.column);   // ts — send it explicitly in the real INSERT

const bad = schema.row(Format.JSONEachRow, Buffer.from('{"id": 7, "x": '));
console.log(bad.outcome, bad.errCode, bad.errMsg);  // rejected <ClickHouse's own code + message>
```

A bad **row** is never an exception — the verdict is `RowResult#outcome`
(`accepted` | `rejected` | `accepted_poisoned` | `unsupported`). Exceptions are
for schema-level answers (`compileDdl`, `setEngine`, `setTtl`,
`validateType`), and there the class *is* the verdict: `SchemaError` = the
server refused (a real ClickHouse code), `UnsupportedError` = chtypes declines
to guess — fall back to the server. The two are peers; a decline never
satisfies `instanceof SchemaError`.

## API reference

Callables, mapped to the C ABI (`spec/c-abi.md`; `spec/bindings.md` is the
cross-SDK shape):

| Symbol | C function | Params | Returns | Errors |
|---|---|---|---|---|
| `new Registry(dir?, options?)` | per artifact: dlopen, `chs_clickhouse_version`, `chs_abi_revision`, `chs_init` | registry dir — the head of the search path (it, `$CHTYPES_REGISTRY`, the per-user cache, the system locations); `{timezone='UTC', verifyChecksums, autofetch, fetch}` | `Registry` | `RegistryError` (a named directory that does not exist, no artifacts anywhere, unreadable directory, checksum, manifest mismatch, load failure); `ChtypesError` (ABI-revision mismatch, `chs_init` failure) |
| `Registry#versions()` | — derived | — | minor lines, oldest first | never throws |
| `Registry#libraries()` | — derived | — | every loaded `Library`, in release order | never throws |
| `Registry#for(version)` | — derived (lookup; loads a line from a later search-path directory on demand) | minor line or exact patch | `Library` | `ArtifactMissingError` (a `RegistryError`, `code` `CHTYPES_ARTIFACT_MISSING`, names every directory looked in); `RegistryError` (a directory holds the line but it does not load); never a nearest-version fallback, never a fetch |
| `Registry#open(version)` | — `for()` behind a promise; with `autofetch`, `ensure` first | minor line or exact patch | `Promise<Library>` | `ArtifactMissingError` (autofetch off); the `FetchError` verdicts; `RegistryError` (fetched, does not load) |
| `Registry#has(version)` | — derived | version string | `boolean` | never throws |
| `Registry#close()` / `Symbol.dispose` | `chs_shutdown` per library | — | — | see `Library#shutdown` |
| `Library#validateType(expr)` | `chs_validate_type` | type expression | canonical spelling (use verbatim) | `SchemaError` (server refusal, e.g. 50); `UnsupportedError` (unsafe family / missing symbol) |
| `Library#compileDdl(ddl, {settings, mode}?)` | `chs_schema_compile` + the six `chs_schema_column_*` getters | column-declaration list (not a CREATE TABLE); optional declared profile; `mode` default `CompileMode.Declared` | `Schema` | `SchemaError` (positive code: bad DDL, unknown setting name 115, declared gate refusing 455/44, Enum-DEFAULT domain 691); `UnsupportedError` (mode ≠ 0, refused DEFAULT, admission budget) |
| `Library#hasCompileSettings()` | probe via `chs_schema_compile` | — | `boolean` (true on every artifact this repo builds) | passes through unexpected errors |
| `Library#setDefaultSettings(settings)` | `chs_set_default_settings` | process-wide seed | — | `ChtypesError` (refused wholesale with the server's 115 + hint; reentrancy; JS `number` value); `UnsupportedError` (missing symbol) |
| `Library#referenceType(expr)` | `chs_reference_type` | type expression | widened reference type, `''` if none | `UnsupportedError` (missing symbol) |
| `Library#registeredFamilies()` | `chs_registered_families` | — | family names | `UnsupportedError` (missing symbol) |
| `Library#functionFlags()` | `chs_function_flags` | — | the volatility TSV audit, verbatim | `UnsupportedError` (missing symbol) |
| `Library#shutdown()` | `chs_shutdown` | — | — | `ChtypesError` (reentrancy); missing symbol ignored |
| `Schema#columns` | `chs_schema_column_count/_name/_type/_default_kind/_default_expr/_default_is_literal` (at compile) | — | `ColumnInfo[]`, canonicalised, declaration order | — |
| `Schema#setEngine(engine, orderBy, {mergeTreeSettings}?)` | `chs_schema_engine` | engine, sorting key, MergeTree-namespace `SETTINGS` | — | sign of rc decides: `SchemaError` (rc > 0 — server refused, today 115 unknown MergeTree setting name); `UnsupportedError` (rc < 0 — unmodelled engine/key, non-default MergeTree value, guarded exception, missing symbol) |
| `Schema#setTtl(ttl)` | `chs_schema_ttl` | rows-TTL expression | — | `UnsupportedError` (any nonzero rc: refused TTL form, guarded exception, missing symbol) |
| `Schema#row(format, raw, settings?)` | `chs_row` | format code, row **bytes**, per-call settings | `RowResult` (verdict in `outcome`, never thrown) | `ChtypesError` (closed schema, `number` setting); `UnsupportedError` (missing symbol) |
| `Schema#rows(format, body, settings?)` | `chs_rows` | format code, body **bytes**, per-call settings | `BatchResult` (`engineRows` is the stored truth when present) | `ChtypesError` (closed schema, `number` setting); `UnsupportedError` (missing symbol — same degradation as `row`) |
| `Schema#compileFilter(expr, {params}?)` | `chs_filter_compile` | one boolean expression; `params` binds `{name:Type}` — values are STRINGS, never hand-escaped | `Filter` (WHERE-side semantics by construction) | `SchemaError` (47 unknown identifier, **456** unbound param, **457** unparseable param value); `UnsupportedError` (clock reads, missing symbol) |
| `Filter#rows(format, body, settings?)` | `chs_filter_rows` | same contract as `Schema#rows` | `FilterResult` — per-row verdicts; `'error'`/`'decline'` are NOT answers, fail closed | `ChtypesError` (closed filter, `number` setting) |
| `Schema#parseBlock(format, body, settings?)` | `chs_block_parse` | parse a body ONCE; per-row parse failures live IN the block | `Block`, evaluable by K filters with no re-parse | `SchemaError` (115 unknown setting, framing, decode fault — no block, no partial answers); `UnsupportedError` (missing symbol) |
| `Filter#eval(block)` | `chs_filter_eval` | an already-parsed `Block` | the same `FilterResult` `rows` answers — `eval(parseBlock(body))` ≡ `rows(body)`; a cross-schema pair answers `'rejected'`/1002 | `ChtypesError` (closed handles, cross-library pair) |
| `Filter#close()` / `Block#close()` / `Symbol.dispose` | `chs_filter_free` / `chs_block_free` | — | — (`Schema#close` frees open filters and blocks FIRST — the C-required order) | never throw; idempotent |
| `Schema#close()` / `Symbol.dispose` | `chs_schema_free` | — | — | never throws; idempotent |
| `minorOf(version)` | — derived | version string | minor line | never throws |
| `compareMinor(a, b)` | — derived | two minor lines | numeric order (25.10 > 25.8) | never throws |
| `ensure(line, opts?)` / `ensureAll(opts?)` | — no C call (the `docs/fetch.md` chain) | line or exact patch; `EnsureOptions` | `Promise<EnsureResult>` / `Promise<EnsureResult[]>` | `ArtifactUntrustedError`, `ArtifactCorruptError`, `ArtifactPinnedError`, `ArtifactUnpublishedError`, `SourceUnreachableError` (each with its `code`); `ChtypesError` (bad spelling, platform key, conflicting options) |
| `verifyInstalled(dest?, platform?)` / `listArtifacts(opts?)` | — no C call | registry dir / `EnsureOptions` | `Promise<InstalledArtifact[]>` / `Promise<ListResult>` | `verifyInstalled` never throws; `listArtifacts` the `FetchError` verdicts unless `offline` |
| `registrySearchPath(explicit?, platform?)` / `fetchDestination(explicit?, platform?)` / `hostPlatform()` | — derived (`docs/fetch.md` §1) | path/env probing | `string[]` / `string` / `string` | never throw |
| `resolveRegistryDir(explicit?)` / `defaultRegistryDir()` / `looksLikeRegistry(dir)` | — derived | path/env probing | resolved dir / `string` / `boolean` | never throw |
| `formatName(f)` | — derived | format code | constant name | never throws |
| `encodeSettings(settings?)` | — derived | settings map | `settings_json` text | `ChtypesError` (a JS `number` value) |
| `parseVersionResult` / `parseChangedSettingsResult` / `parseColumnsResult` / `reconstructDdl` | — no C call (discovery kit) | query result bytes / discovered columns | version / settings map / columns / DDL string | `ChtypesError` on malformed input |
| `isLossyReason(reason)` | — derived | reason string | `boolean` (false for exactly 4 reasons) | never throws |
| `parseDocument` / `parseJsonValue` / `repairBareDenormals` / `rawBytes` / `rawText` / `isValidUtf8` | — derived (byte-exact JSON kit) | bytes | `Json` / `Json\|null` / repaired bytes / `Buffer` / `string` / `boolean` | `parseDocument` throws `ChtypesError` on a malformed document; the rest never throw |
| `nativeStats()` | — diagnostic | — | `{stringsTaken, stringsFreed}` | never throws |

Types and constants: `Format` (the `chs_format` integers 0–9, frozen),
`CompileMode.Declared` (= C `CHS_COMPILE_DECLARED` = 0), `CODE_UNSUPPORTED`
(−2, the wire sentinel — carried by results, never by an error object),
`ABI_REVISION` (4), `Outcome`, `RowResult`, `BatchResult`, `FilterResult`,
`Verdict`, `Value`, `Transform`, `Substitution`, `Computed`, `ColumnDoc`,
`ColumnInfo`, `EngineOptions`, `CompileOptions`, `CompileFilterOptions`,
`Settings`, `SettingValue`, `Reason`, `ServerProfile`, `DiscoveredColumn`,
`Manifest`, `RegistryOptions`, `EnsureOptions`, `EnsureResult`, `FetchEvent`,
`InstalledArtifact`, `ListResult`, `LockFile`, `ArtifactErrorCode`,
`RELEASE_PUBLIC_KEYS`, `Json`, `JsonKind`, and the error classes
`ChtypesError` (base), `RegistryError`, `ArtifactMissingError` (a
`RegistryError` with `code`), `FetchError` and its five verdict subclasses,
`SchemaError`, `UnsupportedError` (a **peer** of `SchemaError`, deliberately
not a subclass).

## Filters, query parameters and the block twin (ABI revisions 3–4)

`Schema#compileFilter(expr)` compiles one boolean expression against the
schema's physical columns and `Filter#rows(...)` answers per-row verdicts
with **WHERE-side** semantics (`x = 256` over `UInt8` promotes — false for
every row — it never wraps), computed by ClickHouse's own comparison
functions. Four verdicts: `'true'`/`'false'` are answers; `'error'` (the
predicate threw — a real server fails the whole query) and `'decline'` (this
library declines) are NOT, and an enforcing caller MUST fail closed on both.
**Enforcement gate**: no read-side security may be enforced on this surface
until the WHERE-truth rig gates green — until then it is shadow/replay only
(`spec/c-abi.md` §Filters).

**Params (revision 4).** The expression may contain `{name:Type}` query
parameters, bound with `compileFilter(expr, { params: { name: 'value' } })`
— values are **strings**, exactly as the server's own parameter channels
carry them. Substitution is the server's own `ReplaceQueryParameterVisitor`:
each value is deserialized by the DECLARED type's own reader and injected as
a typed literal AFTER SQL parsing, so a value is never SQL text and **must
never be hand-escaped into the expression** — injection safety is by
construction, and a hostile value (`' OR 1=1 --`) compares as exactly that
literal. An UNBOUND parameter is the server's own **456** ("Substitution
`name` is not set"), an unparseable value the server's own **457** — both
`SchemaError`, verbatim; a bound name the expression never uses is ignored.
The compiled handle bakes the values in — identity is per
(schema, expr, params) — so **a caller compiling filters from
tenant-influenced values MUST bound its cache and its compile rate**: a
bounded LRU keyed on (schema generation, expr, params-hash) plus a
per-principal compile throttle; an unbounded cache is a memory DoS and an
unmetered compile path is a CPU DoS (`spec/c-abi.md` §Filters, normative).

**Choose the brace type for the value's domain.** A bound value is
deserialized by the brace type's OWN reader (`deserializeTextEscaped`),
which **wraps** an out-of-domain integer — `{p:UInt8}` given `"256"` binds
`0` and matches every genuine zero (measured, uniform 24.8–26.7,
server-matched) — while the same constant written as a literal in the
expression PROMOTES (`x = 256` over `UInt8` is simply never true). A
too-narrow parameter type therefore silently matches the wrong rows: size
the type for the tenant-supplied domain (`{p:UInt64}`, `{p:String}`) or
validate the value before binding it. Malformed integer spellings refuse
loudly with the server's own **457** (`"-1"`, `"+7"`, `"007"` as `UInt8`);
an empty string refuses with **32**. If one name is bound twice at the C
boundary, the LAST binding wins — the server's own `insert_or_assign` rule
(unreachable through this SDK's unique-keyed object, stated for
completeness).

One naming trap, the transport's rather than this library's: on a real
server's **TCP** channel a parameter *named* `limit` or `offset` fails at
the protocol layer with code 26 even when unused (params ride in a Settings
block there); HTTP is fine, and this library matches the HTTP/substitution
semantics. Avoid those two names for anything that will ever cross TCP.

**The block twin (revision 4).** `Schema#parseBlock(format, body, settings)`
parses a body ONCE into a `Block` — the parse half of `Filter#rows` — and
`Filter#eval(block)` answers the same `FilterResult` with no re-parse: the
live-SSE hot path is K filters × 1 event, and the re-parse is shed.
Normative equivalence: `eval(parseBlock(body))` ≡ `rows(body)` for every
verdict class (volatile DEFAULTs resolve against the PARSE call's clock
instant — pin `chtypes_now_epoch_nanos` for cross-call identity).
Evaluation is a pure function of (filter, block), takes no settings, and
never consumes or mutates the block. Filter and block MUST come from the
SAME schema: a mismatched same-library pair answers `'rejected'`/1002
loudly, and a cross-library pair throws `ChtypesError` before any C call. A
`Block` follows the filter's lifetime rules exactly (`Schema#close` frees
open blocks first, and `using` nests naturally: block, filter, schema).
Parse-once does not open enforcement earlier: the twin is under the same
enforcement gate.

## Settings and precedence

Settings ride four channels; on the row path the leftmost channel that names a
setting wins:

    per-call map  >  handle compile profile  >  library defaults (setDefaultSettings)  >  ClickHouse defaults

One documented exception: a **type gate** named in the compile profile
(`allow_suspicious_low_cardinality_types`, `allow_experimental_json_type`, …)
binds at compile and then **outranks the per-call map** for that handle's life
— exactly as a real server checks a gate at CREATE and never re-reads it per
INSERT.

Rules that cost real bugs:

- **Values are strings (or bigints).** A JS `number` is rejected at runtime:
  `chtypes_now_epoch_nanos` is a 19-digit nanosecond epoch, which does not
  survive an IEEE double — sent as a JSON number the setting is *silently
  ignored*. Use `BigInt(…)` or a string; `bigint` crosses via `toString()`.
- **An unknown setting name rejects the whole call with the server's own
  code 115** (with its did-you-mean hint), on every channel.
  `setDefaultSettings` refuses **wholesale** — nothing committed.
- The six `chtypes_*` keys are the whole reserved namespace; anything else
  spelled `chtypes_*` is an unknown name (115). Per-call:
  `chtypes_now_epoch_nanos` (pin the batch instant),
  `chtypes_clock_offset_nanos` (measured server−client offset),
  `chtypes_max_clock_skew_nanos` (refuse volatile-DEFAULT substitution past
  the budget). Process-wide, via `setDefaultSettings` only:
  `chtypes_default_eval_memory_bytes` (default 256 MiB),
  `chtypes_default_eval_wall_nanos` (default 1 s),
  `chtypes_custom_settings_prefixes` (mirror of the server's
  `custom_settings_prefixes`; ships as `SQL_`).
- A row whose `unsupportedSettings` list is non-empty is promoted to
  `unsupported` (a KNOWN name whose value this build declines) — never scored
  as agreement.

## Discovery

**The library never connects to ClickHouse.** It ships three SQL query
constants; your app runs them at connect time with whatever client it already
has, and declares what it learned. Never ask the customer — ask their server.

| constant | feeds |
|---|---|
| `QUERY_SERVER_VERSION` | `parseVersionResult` → `registry.for(version)` |
| `QUERY_CHANGED_SETTINGS` | `parseChangedSettingsResult` → the compile profile AND per-call settings |
| `QUERY_TABLE_COLUMNS` | `parseColumnsResult` → `reconstructDdl` → `compileDdl` (send `param_db`/`param_table`) |

```ts
const profile: ServerProfile = {
  version: parseVersionResult(await run(QUERY_SERVER_VERSION)),
  settings: parseChangedSettingsResult(await run(QUERY_CHANGED_SETTINGS)),
};
// cache per deployment/tenant, then:
const lib = registry.for(profile.version);
const schema = lib.compileDdl(ddl, { settings: profile.settings }); // CREATE-time
const res = schema.rows(Format.JSONEachRow, body, profile.settings); // per-call
```

The parsers read the JSONEachRow bytes through this package's own byte-exact
reader — `position` survives quoted and bare spellings without a float
round-trip, and `default_kind`/`default_expression` are carried (dropping them
silently loses DEFAULT/MATERIALIZED semantics). A typo'd setting name in the
declared profile fails the **compile** with the server's own 115 — caught at
declare time, not swallowed.

## Multi-version use and thread-safety

- One `Registry` loads every version side by side. Each artifact is dlopened
  `RTLD_LOCAL` (that is the whole mechanism letting two builds that both
  define `DB::DataTypeFactory` share a process); cost ≈ 120 MB resident per
  loaded version.
- `for()` resolves a minor line (`"25.8"`) or an exact patch
  (`"25.8.28.1-lts"`); an unknown patch inside a loaded minor resolves to the
  line. Failure is `ArtifactMissingError`, naming every directory looked in
  — never a fallback to the nearest version. Version behaviour is
  non-monotonic; never infer one version's answer from another's.
- Every call is **synchronous on the JS thread**; ordinary single-threaded
  Node code needs no locking. `setDefaultSettings` and `shutdown` carry a
  reentrancy tripwire (the `NativeLibrary#inCall` counter) and refuse loudly
  if another chtypes call is on the stack — they replace process-global state
  the row path reads by reference.
- **`worker_threads` is the boundary.** Two JS threads share one dlopen'd
  image and one set of C globals, which no per-isolate counter can see. Seed
  default settings BEFORE starting workers, or serialise the seed yourself;
  do not share a `Schema` across workers.
- Teardown: `registry.close()` / `library.shutdown()` (or `using` /
  `Symbol.dispose`) join the DEFAULT evaluator's background threads —
  required before any explicit unload; an ordinary process is covered by
  `atexit`.

## Known limitations

- The error model's third arm is deliberate: `unsupported` means "a real
  server might well have accepted this; chtypes will not guess" — fall back
  to the server (`spec/c-abi.md` §Error model). Unmodelled engines, sorting
  keys and TTL forms (WHERE/GROUP BY TTLs, TO DISK/VOLUME, RECOMPRESS,
  clock-reading TTLs) decline rather than guess.
- **EPHEMERAL columns:** a format stream carries no column list, so an INSERT
  that names an EPHEMERAL column in an explicit column list cannot be
  previewed here — detect via `Schema#columns` (`defaultKind ===
  'EPHEMERAL'`) and decline that intersection, never mispreview
  (`spec/bindings.md` §EPHEMERAL).
- **macOS artifacts are a dev floor, not an oracle:** `long double` is
  53-bit, so float parses diverge from real servers (Linux matches 395/395 of
  the float corpus; macOS 0/395). Float expectations must come from a Linux
  artifact or a live server.
- Everything here is **insert-side** coercion. Never fold a `WHERE`-clause
  constant through `row`/`rows` — comparison rules differ; refuse an operand
  outside the column type's domain instead (`spec/bindings.md` §Constants are
  not payloads).
- Binary formats (`RowBinary` family, `Native`, `Buffers`) parse only on
  artifacts new enough to carry the reader — probe the artifact; earlier ones
  answer 73/117 exactly as their servers do.
