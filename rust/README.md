# chtypes — Rust SDK

ClickHouse's own type system, schema validation, DEFAULT/TTL semantics and
coercion — per ClickHouse version, in one process. A peer SDK over the
`chs_*` C ABI (pre-1.0 only the names and the format integers are frozen —
signatures can still change by deliberate cycle, which is why the loader
checks `chs_abi_revision` before trusting them), alongside Go, Python and
TypeScript. `../../spec/` is the
contract; where this crate and the spec disagree, the spec wins.

`cargo doc --no-deps --open` is the full reference — every public item is
documented (`#![deny(missing_docs)]`), including which `Error` variant each
call can produce and what each one means.

## Install

```toml
[dependencies]
chtypes = "0.1"
```

Unix only — the loader is `dlopen`. To work against a checkout instead,
consume it as a path dependency:

```toml
[dependencies]
chtypes = { path = "../chtypes/rust" }
```

**Artifact prerequisite.** The crate answers nothing by itself: it `dlopen`s
one artifact per ClickHouse release (166–302 MB each — real ClickHouse,
compiled). Get them with the crate's own command (see
[Fetching artifacts](#fetching-artifacts) below),

```sh
cargo install chtypes           # the `chtypes` binary
chtypes fetch 25.8              # verified, into the per-user cache
```

with `scripts/fetch.sh 25.8` (the reference implementation, same result), or
build one in the core repository; all land under
`~/.cache/chtypes/artifacts/<os>-<arch>/<minor>/` — the per-user cache
`Registry::from_env_or_default()` resolves to — so a registry looks like:

```
<registry>/
  24.8/  manifest.json  libchtypes_s1.so  unsafe_families.txt
  25.8/  manifest.json  libchtypes.dylib  unsafe_families.txt
  ...
```

**How loading works, and what the loader checks.** `Registry` scans one
subdirectory per version. The shared library's file name comes from
`manifest.json`'s `library` field — never constructed from the platform (the
Linux artifacts still ship the historical `libchtypes_s1.so` name). Each
library is `dlopen`'d with `RTLD_NOW | RTLD_LOCAL`, names itself via
`chs_clickhouse_version()` (nothing is inferred from the directory), and is
cross-checked against the manifest's size and version fields. The loader then
compares the artifact's `chs_abi_revision()` against this crate's hand-kept
`chtypes::ABI_REVISION`: a **different nonzero** revision is refused at load
(`Error::Load` — calling through mismatched declarations is undefined), while
`0` means the artifact predates the probe and loads with per-symbol
degradation (missing entry points answer `Error::PredatesFeature` at call
time, never at load).

## Quickstart

```rust
use chtypes::{Format, Registry, NO_SETTINGS};

let registry = Registry::from_search_path();             // docs/fetch.md §1: $CHTYPES_REGISTRY, the per-user cache, the system locations
let lib = registry.for_version("25.8")?;                // minor line or exact patch; Error::ArtifactMissing if nowhere
let schema = lib.compile("ts DateTime, seq UInt8")
    .settings([("flatten_nested", "1")])                // the deployment's profile
    .compile()?;

// A good row — accepted, but silently coerced: 256 into UInt8 stores 0.
let ok = schema.rows(Format::JsonEachRow,
    br#"{"ts":"2026-01-15 10:30:00","seq":256}"#, NO_SETTINGS)?;
assert_eq!(ok.outcome.to_string(), "accepted");
assert_eq!(ok.rows[0].values[1].text, "0");
assert_eq!(ok.transformed[0].reason, chtypes::reason::OVERFLOW_WRAP);

// A bad row — rejected with ClickHouse's own code, in the Ok value.
let bad = schema.rows(Format::JsonEachRow, br#"{"ts":"not a date","seq":1}"#, NO_SETTINGS)?;
assert_eq!(bad.outcome, chtypes::Outcome::Rejected);
println!("[{}] {}", bad.err_code, bad.err_msg);          // ClickHouse's message
```

**The verdict is in the `Ok` value.** A row the server would reject is `Ok`
with `Outcome::Rejected`; the `Err` arm is for the machinery (loading,
marshalling, an unreadable document). Three outcomes must never be conflated:
**rejected** (the server itself would refuse — a real ClickHouse code),
**unsupported** (this build declines to guess — `Error::Unsupported` /
`Outcome::Unsupported`, code `-2`; fall back to the server), and **accepted**,
possibly with coercions reported in `transformed` and volatile DEFAULTs in
`substituted` (send those columns explicitly in the INSERT).

## Fetching artifacts

`docs/fetch.md` is the contract, identical in the four SDKs; this crate
implements it behind the Cargo feature **`fetch`** (on by default —
`default-features = false` drops the command, `ensure`, autofetch and every
dependency they bring, leaving the loader alone).

**The command.** `cargo install chtypes` puts a `chtypes` binary on the path:

```
chtypes fetch <line>... [--all] [--platform <os-arch>] [--dest <dir>]
                        [--tag <t> | --url <base>] [--lock <file>] [--frozen]
                        [--force] [--offline]
chtypes verify [--dest <dir>]        re-hash every installed line against its manifest
chtypes list   [--dest <dir>]        what is installed, and what the release offers
chtypes where                        the registry directory fetch would write to
```

Progress prints on stderr; `fetch` prints each installed directory alone on
stdout. Exit codes: 0 ok · 1 verification failed · 2 usage · 3 source
unreachable · 4 not published for this platform/line.

**The function.** `chtypes::ensure(line, &opts)` is the same operation from
Rust — idempotent: once the line is installed and its library hashes what the
signed release lists, nothing is downloaded (the release's three small files
are read, never the tarball; `offline: true` reads no source and trusts the
installed manifest):

```rust
use chtypes::{EnsureOptions, Registry};

let installed = chtypes::ensure("25.8", &EnsureOptions::default())?;   // AlreadyInstalled | Installed | Replaced
let registry = Registry::new(installed.dir.parent().unwrap())?;
```

`EnsureOptions` carries the flags (`dest`, `platform`, `url`/`tag`, `lock`,
`frozen`, `force`, `offline`, `progress`) and the trust policy
(`trusted_keys`, `allow_unsigned`; `None` reads the environment).

**Where.** Lookup walks `docs/fetch.md` §1 in order — an explicit directory,
`$CHTYPES_REGISTRY`, `${XDG_CACHE_HOME:-~/.cache}/chtypes/artifacts/<os>-<arch>`,
then `/usr/local/share/chtypes/artifacts/<os>-<arch>` and
`/opt/chtypes/artifacts/<os>-<arch>` — and takes the first directory holding
the line (`registry_search_path`, `locate`). Fetch writes to the first of the
first three that is set (`install_dir`), never to a system location. With an
explicit `--dest`, "already installed" means installed *in that directory*:
a container build's `--dest /opt/chtypes/artifacts` is never satisfied by
the builder's own cache.

**Verification, in order — nothing else is a verdict.** `SHA256SUMS.sig` must
verify (ed25519) over the exact bytes of `SHA256SUMS` under a trusted key;
`index.json` must agree with the signed sums about the asset; the tarball is
hashed as it streams and never unpacked on a mismatch; it is unpacked flat
(plain top-level files only — no paths, links or traversal) into a temporary
sibling, renamed into `<registry>/<minor>/`, and the installed library is
re-hashed in place. The release key is embedded (`fetch::RELEASE_PUBLIC_KEY`,
id `deb275922dbff76e`); `CHTYPES_TRUSTED_KEYS=<hex>[,<hex>…]` replaces it for
a mirror; `CHTYPES_ALLOW_UNSIGNED=1` skips the signature with one loud
warning naming the source — never the default, never silent.

**Pinning.** `--lock chtypes.lock` records, per `<os>-<arch>/<minor>`, the
asset and sha256 that were installed; `--frozen` (or
`EnsureOptions { frozen: true, .. }`) refuses anything else with
`CHTYPES_ARTIFACT_PINNED`.

**Lazy fetch on first open** is opt-in: `RegistryOptions { autofetch:
Some(true), .. }` or `CHTYPES_AUTOFETCH=1` makes
`Registry::from_search_path_with(opts).for_version(line)` run `ensure` first
for a line found nowhere — once per process per line, under one process-wide
lock, so concurrent opens fetch once. Off by default: a production process
must not begin a 250 MB download inside a request.

**The error.** A line found nowhere on the search path is
`Error::ArtifactMissing { line, platform, looked_in }`, whose message is the
one every SDK renders:

```
chtypes: no artifact for ClickHouse 25.8 (linux-arm64). Looked in: /home/u/.cache/chtypes/artifacts/linux-arm64, /usr/local/share/chtypes/artifacts/linux-arm64, /opt/chtypes/artifacts/linux-arm64.
Install it:  cargo install chtypes && chtypes fetch 25.8
or set CHTYPES_AUTOFETCH=1 to fetch on first use.
```

`Error::artifact_code()` answers the shared code string —
`CHTYPES_ARTIFACT_MISSING`, `…_UNTRUSTED` (`Error::ArtifactUntrusted`),
`…_CORRUPT` (`ArtifactCorrupt`, any hash mismatch), `…_PINNED`
(`ArtifactPinned`), `…_UNPUBLISHED` (`ArtifactUnpublished`) and
`CHTYPES_SOURCE_UNREACHABLE` (`SourceUnreachable`) — and `None` for
everything else. It is distinct from `Error::code()`, which stays the
ClickHouse error code of a rejection. A registry over one explicit
directory (`Registry::new`) keeps answering `Error::NoSuchVersion`, naming
what is loaded.

**Tests.** `cargo test --test fetch` runs every fixture in
`spec/fixtures/fetch/` (signed, bad signature, unsigned, tampered tarball,
sums/index mismatch, the lock) through both the library and the binary,
offline, reading the verdicts from the fixtures' own `expected.json`; it
skips loudly when the fixtures are absent.

## API reference

Every public item, with the `chs_*` entry point underneath it. "derived" means
the crate computes it with no C call.

| Item | C function | Takes | Returns | Errors |
|---|---|---|---|---|
| `Registry::new(dir)` / `from_env()` / `from_env_or(dir)` / `with_timezone(dir, tz)` | `chs_clickhouse_version` + `chs_abi_revision` + `chs_init` per artifact | registry directory; optional timezone (default `UTC`) | `Registry` (eager: every artifact in the directory) | `Registry`, `Load`, `NotAnArtifact`, `CorruptArtifact`, `VersionMismatch`, `Init`, `InitConflict`, `EmptyRegistry`; `NoRegistryEnv` (`from_env`) |
| `Registry::from_search_path()` / `from_search_path_with(RegistryOptions)` | — (loads nothing until asked) | optional explicit dir, timezone, `autofetch` (+ `fetch: EnsureOptions`) | `Registry` (lazy: the `docs/fetch.md` §1 search path, one line per open) | — |
| `Registry::for_version(v)` | derived; lazy: the per-artifact load above, on first open | minor line or exact patch | `Arc<Library>` | eager: `NoSuchVersion` (names what IS loaded; no nearest fallback); lazy: `ArtifactMissing` (§7), or the fetch's own error under autofetch |
| `Registry::versions()` / `libraries()` / `dir()` / `search_path()` / `autofetch()` | derived | — | minor lines, numeric order (lazy: what is installed) / loaded libraries, release order (`Vec<Arc<Library>>`) / path / the directories looked in / the setting | — |
| `ensure(line, &EnsureOptions)` (feature `fetch`) | — | line or exact patch; the flags of `chtypes fetch` | `Installed { dir, line, version, library, library_sha256, platform, action, asset }` | `ArtifactUntrusted`, `ArtifactCorrupt`, `ArtifactPinned`, `ArtifactUnpublished`, `SourceUnreachable`, `Fetch` |
| `fetch::ensure_all` / `verify_installed(dir)` / `release_info(&opts)` / `install_dir(&opts)` / `search_path(&opts)` / `parse_line` (feature `fetch`) | — | as `chtypes fetch --all` / `verify` / `list` / `where` | `Vec<Installed>` / `Vec<Verification>` / `ReleaseInfo` / path / paths / minor | as `ensure` |
| `fetch::TrustPolicy`, `LockFile`, `IndexRow`, `verify_signature`, `sha256_file`, `RELEASE_PUBLIC_KEY[_HEX]`, `RELEASE_KEY_ID` (feature `fetch`) | — | the pieces of the chain, for a host that wants them separately | — | `Fetch` (a malformed key list) |
| `registry_search_path(explicit)` / `search_path_for(platform, explicit)` / `install_dir[_for]` / `locate[_in]` / `installed_lines` / `host_platform` / `cache_dir_for` / `default_registry_dir` | derived | — | the §1 search path, where fetch writes, `<dir>/<minor>` of the first hit, what is installed, `<os>-<arch>` | — |
| `Error::artifact_code()` | derived | — | the shared `CHTYPES_ARTIFACT_*` / `CHTYPES_SOURCE_UNREACHABLE` code, `None` otherwise | — |
| `Registry::shutdown()` | `chs_shutdown` per library | — | — | — |
| `Library::load(path, tz)` | `dlopen` + the four mandatory symbols | artifact path, timezone | `Library` | `Load`, `NotAnArtifact`, `Init`, `InitConflict`, `Nul` |
| `Library::version()` / `minor()` / `path()` | `chs_clickhouse_version` (cached) | — | `"25.8.28.1-lts"` / `"25.8"` / path | — |
| `Library::abi_revision()` | `chs_abi_revision` | — | artifact's revision, `0` = predates probe | — |
| `Library::validate_type(expr)` | `chs_validate_type` | type expression | canonical spelling, ClickHouse's own text | `Schema` (rejected, e.g. 50), `Unsupported`, `PredatesFeature`, `Nul` |
| `Library::compile(ddl)` | — (builder) | column-declaration list (not a `CREATE TABLE`) | `CompileRequest` | — |
| `CompileRequest::settings(...)` / `.mode(...)` | — (builder) | declared profile; `CompileMode::Declared` | `Self` | — |
| `CompileRequest::compile()` | `chs_schema_compile` + column introspection group | — | `Schema` | `Schema` (server rejects: 115 unknown setting, 455/44 declared gate, 691 Enum DEFAULT, …), `Unsupported` (−2 declines), `Nul` |
| `Library::set_default_settings(&[(k,v)])` | `chs_set_default_settings` | process-wide seed; admission budgets live here | `()` | `Schema` (115, wholesale — nothing committed), `PredatesFeature`, `Nul` |
| `Library::reference_type(expr)` | `chs_reference_type` | type expression | `Option<String>` (`None` = no wider type) | `PredatesFeature`, `Nul` |
| `Library::registered_families()` | `chs_registered_families` | — | every type family in this build | `PredatesFeature` |
| `Library::function_flags()` | `chs_function_flags` | — | TSV volatility audit | `PredatesFeature` |
| `Library::has_compile_settings()` | symbol probe | — | `true` (mandatory symbol) | — |
| `Library::shutdown()` | `chs_shutdown` | — | — (idempotent; required before any `dlclose`) | — |
| `Schema::columns()` | `chs_schema_column_*` (read at compile) | — | `&[Column]`, canonicalised | — |
| `Schema::library()` | derived | — | `&Arc<Library>` | — |
| `Schema::set_engine(engine, order_by, mt_settings)` | `chs_schema_engine` | `SHOW CREATE` spelling, sorting key, MergeTree-namespace settings | `()` | sign rule: `Schema` (rc>0, server refusal — 115), `Unsupported` (rc<0, decline), `PredatesFeature`, `Nul` |
| `Schema::set_ttl(sql)` | `chs_schema_ttl` | table-level rows TTL | `()` | `Unsupported` (any nonzero), `PredatesFeature`, `Nul` |
| `Schema::row(format, raw)` / `row_with_settings(...)` | `chs_row` | format code, counted bytes, settings | `RowResult` (verdict inside) | `PredatesFeature`, `BadDocument`, `Nul` |
| `Schema::rows(format, body, settings)` | `chs_rows` | one request body = one clock instant | `BatchResult` (verdict inside) | `PredatesFeature`, `BadDocument`, `Nul` |
| `Schema::compile_filter(expr, params)` | `chs_filter_compile` | one boolean expression; `params` binds `{name:Type}` positionally (`NO_PARAMS` for none — values are STRINGS, never hand-escaped) | `Filter<'_>` (borrows the schema — the free order is a compile-time fact) | `Schema` (47 unknown identifier, **456** unbound param, **457** unparseable param value), `Unsupported` (clock reads), `PredatesFeature`, `Nul` |
| `Filter::rows(format, body, settings)` | `chs_filter_rows` | same contract as `Schema::rows` | `FilterResult` — per-row `Verdict`s; `Error`/`Decline` are NOT answers, fail closed | `PredatesFeature`, `BadDocument`, `Nul` |
| `Schema::parse_block(format, body, settings)` | `chs_block_parse` | parse a body ONCE; per-row parse failures live IN the block | `Block<'_>` (borrows the schema), evaluable by K filters with no re-parse | `Schema` (115 unknown setting, framing, decode fault — no block, no partial answers), `Unsupported`, `PredatesFeature`, `Nul` |
| `Filter::eval(&block)` | `chs_filter_eval` | an already-parsed `Block` | the same `FilterResult` `rows` answers — `eval(parse_block(body))` ≡ `rows(body)`; a cross-schema pair answers `Rejected`/1002 | `CrossLibrary` (a pair from two libraries), `BadDocument`, `PredatesFeature` |
| `Filter` / `Block` drop | `chs_filter_free` / `chs_block_free` | — | — (the borrow makes drop-before-schema a compile-time fact) | — |
| `Schema` drop | `chs_schema_free` | — | — | — |
| `parse_version_result` / `parse_changed_settings_result` / `parse_columns_result` | derived (parsers) | JSONEachRow bytes from the three queries | version / `Vec<(name, value)>` / `Vec<DiscoveredColumn>` | `Discovery` |
| `reconstruct_ddl(&cols)` | derived | discovered columns | column-declaration list for `compile` | `Discovery` |
| `RowResult` / `BatchResult` / `Value` / `Transform` (+ `lossy()`) / `Substitution` / `Computed` / `Outcome` | derived from the result document | — | see rustdoc; `BatchResult::engine_rows_bytes()` is the byte-exact stored truth | — |
| `RawText` | — | — | byte-exact stored rendering; `as_str()` is fallible, `to_lossy()` explicit | — |
| Constants: `Format` (codes 0–9), `CompileMode::Declared`=0, `CODE_UNSUPPORTED`=−2, `ABI_REVISION` (4), `DEFAULT_TIMEZONE`, `NO_SETTINGS`, `NO_PARAMS`, `REGISTRY_ENV`, `AUTOFETCH_ENV`, `SYSTEM_ARTIFACT_ROOTS`, `CODE_ARTIFACT_*` / `CODE_SOURCE_UNREACHABLE`, `FETCH_COMMAND`, `SETTING_*` (the chtypes keys), `reason::*` (24 stable spellings) | — | — | — | — |

## Filters, query parameters and the block twin (ABI revisions 3–4)

`Schema::compile_filter(expr, NO_PARAMS)` compiles one boolean expression
against the schema's physical columns and `Filter::rows(...)` answers
per-row verdicts with **WHERE-side** semantics (`x = 256` over `UInt8`
promotes — false for every row — it never wraps), computed by ClickHouse's
own comparison functions. Four verdicts: `Verdict::True`/`False` are
answers; `Verdict::Error` (the predicate threw — a real server fails the
whole query) and `Verdict::Decline` (this library declines) are NOT, and an
enforcing caller MUST fail closed on both. **Enforcement gate**: no
read-side security may be enforced on this surface until the WHERE-truth rig
gates green — until then it is shadow/replay only (`spec/c-abi.md`
§Filters).

**Params (revision 4).** The expression may contain `{name:Type}` query
parameters, bound positionally — `compile_filter("t = {t:String}",
&[("t", "acme")])`, the crate's slice convention (`NO_PARAMS` for none) —
values are **strings**, exactly as the server's own parameter channels carry
them. Substitution is the server's own `ReplaceQueryParameterVisitor`: each
value is deserialized by the DECLARED type's own reader and injected as a
typed literal AFTER SQL parsing, so a value is never SQL text and **must
never be hand-escaped into the expression** — injection safety is by
construction, and a hostile value (`' OR 1=1 --`) compares as exactly that
literal. An UNBOUND parameter is the server's own **456** ("Substitution
`name` is not set"), an unparseable value the server's own **457** — both
`Error::Schema`, verbatim; a bound name the expression never uses is
ignored. The compiled handle bakes the values in — identity is per
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
an empty string refuses with **32**. If one name is bound twice, the LAST
binding wins — the server's own `insert_or_assign` rule (reachable here:
`params` is a slice of pairs, and the last pair with a name wins).

One naming trap, the transport's rather than this library's: on a real
server's **TCP** channel a parameter *named* `limit` or `offset` fails at
the protocol layer with code 26 even when unused (params ride in a Settings
block there); HTTP is fine, and this library matches the HTTP/substitution
semantics. Avoid those two names for anything that will ever cross TCP.

**The block twin (revision 4).** `Schema::parse_block(format, body,
settings)` parses a body ONCE into a `Block<'_>` — the parse half of
`Filter::rows` — and `Filter::eval(&block)` answers the same `FilterResult`
with no re-parse: the live-SSE hot path is K filters × 1 event, and the
re-parse is shed. Normative equivalence: `eval(&parse_block(body)?)` ≡
`rows(body)` for every verdict class (volatile DEFAULTs resolve against the
PARSE call's clock instant — pin `chtypes_now_epoch_nanos` for cross-call
identity). Evaluation is a pure function of (filter, block), takes no
settings, and never consumes or mutates the block. Filter and block MUST
come from the SAME schema: a mismatched same-library pair answers
`FilterOutcome::Rejected`/1002 loudly, and a cross-library pair is
`Error::CrossLibrary` before any C call. A `Block` borrows its `Schema`
exactly as a `Filter` does — the wrong free order does not compile.
Parse-once does not open enforcement earlier: the twin is under the same
enforcement gate.

## Settings and precedence

The row path resolves **three settings channels in one order** — later wins:

```
per-call map  >  handle compile profile  >  library defaults (set_default_settings)  >  ClickHouse defaults
```

One documented exception: a **type gate named in the compile profile** binds
at compile — where a real server binds it, at CREATE — and then outranks the
per-call map for that handle's life (a profile declaring it at a refusing
value fails the compile with the server's own 455/44). Gates the profile does
not name keep the per-row re-check.

Rules that are enforced, not advisory:

- **Values are strings at the boundary.** The crate's `&[(k, v)]` shape makes
  this structural: `chtypes_now_epoch_nanos` is a 19-digit nanosecond epoch,
  and a caller that routes it through a float sends `1.7e+18` — the setting is
  silently ignored. Never stringify through a float.
- **An unknown setting name rejects the whole call with the server's own
  code 115** (did-you-mean hint included), on every channel;
  `set_default_settings` refuses its payload **wholesale**, nothing committed.
- The six `chtypes_*` keys are the entire reserved namespace — three per-call
  (`SETTING_NOW_EPOCH_NANOS`, `SETTING_CLOCK_OFFSET_NANOS`,
  `SETTING_MAX_CLOCK_SKEW_NANOS`) and three per-process
  (`SETTING_DEFAULT_EVAL_MEMORY_BYTES`, `SETTING_DEFAULT_EVAL_WALL_NANOS`,
  plus `chtypes_custom_settings_prefixes`). A per-process key handed to
  `rows()` comes back on `unsupported_settings` — a decline, and the row is
  promoted to `Outcome::Unsupported`, never scored as agreement.
- `NO_SETTINGS` is the spelled-out empty map for the common call.

## Discovery

This crate **never connects to ClickHouse** — the app runs three canonical
queries at connect time with whatever client it already has, and declares the
answers back:

| Constant | Feeds |
|---|---|
| `QUERY_SERVER_VERSION` | `Registry::for_version` |
| `QUERY_CHANGED_SETTINGS` | the compile profile AND per-call settings |
| `QUERY_TABLE_COLUMNS` | `parse_columns_result` → `reconstruct_ddl` → `compile` |

```rust
// once per connection/tenant; cache the profile per deployment
let version  = chtypes::parse_version_result(&query(chtypes::QUERY_SERVER_VERSION))?;
let settings = chtypes::parse_changed_settings_result(&query(chtypes::QUERY_CHANGED_SETTINGS))?;
let lib = registry.for_version(&version)?;

let cols = chtypes::parse_columns_result(&query(chtypes::QUERY_TABLE_COLUMNS))?;
let schema = lib.compile(&chtypes::reconstruct_ddl(&cols)?)
    .settings(settings.clone())
    .compile()?;                       // a typo'd setting = the server's own 115, here
let batch = schema.rows(Format::JsonEachRow, body, &settings)?;   // same profile per call
```

Never ask the customer for their settings — ask their server. The parsers keep
integer fields exact through both quoted and bare spellings (never through a
float), and `reconstruct_ddl` carries `default_kind`/`default_expression`,
without which DEFAULT/MATERIALIZED/EPHEMERAL semantics are silently lost.
`system.columns` reports the table **as stored**, so reconstruction is
shape-faithful exactly when the discovered profile is declared at compile.

## Multi-version use and thread-safety

- Every artifact loads with `RTLD_NOW | RTLD_LOCAL` — that is the whole
  mechanism letting several ClickHouse builds coexist in one process. Cost,
  measured: ~120 MB resident per loaded version.
- `for_version` resolves a minor line (`"25.8"`) or an exact patch
  (`"25.8.28.1-lts"`); a drifted patch resolves to its minor line. Failure
  names what is loaded — there is deliberately no nearest-version fallback,
  because version behaviour is not monotonic (25.10 rejects a DEFAULT that
  25.8 and 26.6 both accept).
- **One mutex per loaded image** serialises every call into a `Library` —
  including `set_default_settings`, which the ABI requires be excluded against
  everything else on the same image. `Library` and `Registry` are
  `Send + Sync`; share them freely.
- `Schema` is `Send` and deliberately **not** `Sync`: one native handle must
  not be used from two threads at once, and the type system enforces it.
  Parallelism comes from more schemas, not shared ones.
- `Registry::shutdown()` / `Library::shutdown()` join the DEFAULT evaluator's
  threads. `chs_init` registers the same via `atexit`, so an ordinary process
  needs no call; it is **required before `dlclose`** — which this crate never
  performs itself (the handle is intentionally leaked to the process).

## Known limitations

- **The error model is normative** — `spec/c-abi.md` §Error model. A decline
  (`Error::Unsupported` / `Outcome::Unsupported`, code −2) is neither an
  acceptance nor a rejection; both over-accepts and over-rejects are budgeted
  at zero.
- **EPHEMERAL columns:** an INSERT whose explicit column list names an
  EPHEMERAL column cannot be previewed by `row`/`rows` (a format stream
  carries no column list). Detect via `Schema::columns()`'s
  `DefaultKind::Ephemeral` and decline that intersection —
  `spec/bindings.md` §EPHEMERAL.
- **macOS is a dev floor, not an oracle:** its `long double` is 53-bit, so
  float parses diverge from a real server (Linux matches 395/395 of the float
  corpus; macOS 0/395). Float expectations must come from a Linux artifact or
  a live server.
- **Declined by design:** unmodelled engines and sorting keys,
  `WHERE`/`GROUP BY` TTLs, `TO DISK`/`VOLUME` moves, `RECOMPRESS`,
  clock-reading TTL expressions, server/session-property DEFAULTs
  (`hostName()`, `currentUser()`), blocking DEFAULTs (`sleep`), DEFAULTs past
  the admission budgets, MergeTree settings declared at non-default values.
  Each is `Error::Unsupported` — fall back to the server.
- `Format::Native`/`Buffers`/`RowBinary*` support depends on the loaded
  artifact's age, not this crate's version — probe the artifact
  (`spec/bindings.md` §Values a binding must accept and reject).

## More

- `cargo run --example demo` — the product in one screen (overflow, pinned
  `now()`, TTL, reject, decline).
- [`playground/rust/`](../../playground/rust/) — a nine-section runnable tour;
  the go/, python/ and ts/ tours beside it print the same nine sections.
- `cargo test` — integration tests and the golden set skip without a registry;
  `CHTYPES_REGISTRY=/path/to/registry cargo test` runs them. The fetch suite
  (`tests/fetch.rs`) needs only `spec/fixtures/fetch/` and runs offline.
- `cargo build --no-default-features` — the loader without the `fetch`
  feature: no binary, no `ensure`, and none of the fetch dependencies
  (`ed25519-dalek`, `sha2`, `ureq`, `base64`, `tar`, `flate2`).
