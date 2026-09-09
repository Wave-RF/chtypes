# chtypes — Go SDK

ClickHouse's own type machinery — schema validation, DEFAULT/TTL logic,
coercion — vendored per release and reached through the `chs_*` C ABI via cgo
(pre-1.0 only the names and the format integers are frozen — signatures can
still change by deliberate cycle, which is why the loader checks
`chs_abi_revision` before trusting them). One peer binding among
`{go,python,ts,rust}`; no language is
privileged. `spec/bindings.md` is the shape every SDK implements,
`spec/c-abi.md` the contract underneath.

Every answer is produced by real ClickHouse code compiled from the pinned
release. The one derived result is `Transformed` — ClickHouse never says
"I silently changed your value", so the SDK computes it from a second parse
through a widened reference type (`chtypes/transform.go`).

## Install (pre-publish)

Nothing is published yet. Consume the module with a `replace` directive:

```
require github.com/wave-rf/chtypes/go v0.0.0
replace github.com/wave-rf/chtypes/go => ../path/to/chtypes/go
```

**Two builds, one package.** The default build is **dlopen-only**: it
compiles with cgo (for `dlfcn`) but links nothing, includes no header, and
needs no build tree — a consumer `go get`s it and points `NewRegistry` at
a directory of artifacts. The `chtypes_linked` build tag adds the
statically linked path (package-level `CompileDDL` / `ValidateType` /
`ParseSchema`, `BuiltVersion`), which is a development and rig instrument:
it links `-lchtypes` out of the core repository's `lib/build` at compile time
(`CGO_LDFLAGS` says where) and answers for exactly that one artifact. The frozen numbers the default build hardcodes
(`Format`, `DocFlags`, `CodeUnsupported`, `ExportNone`, `ABIRevision`) are
pinned to `chtypes.h` by compile-time assertions that only the tagged build
sees (`linked_abi_check.go`), so the core repository's `just test` — which
builds with the tag — catches any drift before a consumer could.

    go build ./...                        # dlopen-only: what a consumer gets
    go build -tags chtypes_linked ./...   # + the linked path (needs a core build tree via CGO_LDFLAGS)
    scripts/check-standalone.sh           # proves the first from a bare copy of go/

**Getting artifacts.** The native code is a per-version prebuilt artifact this
package `dlopen`s at runtime; nothing here builds one. Either fetch a published
one — signed, verified, into the per-user cache every SDK in this repository
defaults to — or build one in the core repository, which lands it in the same
place:

```sh
go run github.com/wave-rf/chtypes/go/cmd/chtypes@latest fetch 25.8   # -> ~/.cache/chtypes/artifacts/<os>-<arch>/25.8/
scripts/fetch.sh 25.8                                                # the same, from a checkout (docs/artifacts.md)
```

A registry directory holds one subdirectory per minor line — `25.8/manifest.json`,
the shared library it names, `CH_VERSION`, `unsafe_families.txt`. Lookup walks
the search path in [`docs/fetch.md`](../docs/fetch.md) §1 — the directory given
to `NewRegistry`, `$CHTYPES_REGISTRY`, the per-user cache, then the system
locations — and takes the first one holding the line; see [Fetching
artifacts](#fetching-artifacts) below for the command, `Ensure`, lazy fetch and
the one error.

- **Registry path** (`NewRegistry`, the default build): dlopens per-version
  artifacts, one directory per version:

      <registry>/25.8/{manifest.json, libchtypes.so|.dylib, unsafe_families.txt}

  The loader reads `manifest.json`'s `library` field for the file name, then
  the library **names itself** via `chs_clickhouse_version()` — nothing is
  inferred from the path. At load it compares `chs_abi_revision()` with the
  package's `ABIRevision`: a *different nonzero* revision is refused with both
  numbers named; revision `0` means the artifact predates the probe and only
  per-symbol degradation applies (a missing optional symbol degrades to
  `UnsupportedError`, never a failed load).
- **Static path** (`-tags chtypes_linked`): cgo links against a core
  repository build tree; `CGO_LDFLAGS=-L<core>/lib/build` names it (the core's
  `ci/steps/_lib.sh linked_env` does this). A rig instrument, not a consumer path.

macOS artifacts are a dev floor, not an oracle — float parses diverge from
real servers (see Known limitations).

## Quickstart

```go
reg, err := chtypes.NewRegistry(chtypes.DefaultRegistryDir()) // or any registry dir / $CHTYPES_REGISTRY
if err != nil { log.Fatal(err) }
lib, err := reg.For("25.8") // minor line or exact patch; never a nearest-match
if err != nil { log.Fatal(err) }

// Compile under the deployment's own settings profile (see Discovery).
cs, err := lib.CompileDDL("x UInt8, ts DateTime DEFAULT now()",
    chtypes.WithCompileSettings(map[string]string{"flatten_nested": "0"}))
if err != nil { log.Fatal(err) } // *SchemaError (server refused) or *UnsupportedError (declined)
defer cs.Close()

good, _ := cs.Row(chtypes.JSONEachRow, []byte(`{"x":256}`))
fmt.Println(good.Outcome)               // accepted
fmt.Println(good.Transformed[0].Reason) // overflow_wrap — 256 stored as 0, silently
fmt.Println(good.Substituted)           // ts: send it explicitly, or preview != stored

bad, _ := cs.Row(chtypes.JSONEachRow, []byte(`{"x":"abc"}`))
fmt.Println(bad.Outcome, bad.ErrCode)   // rejected 27 — the server's own code
```

## Fetching artifacts

The fetch/verify/install contract every chtypes SDK implements is
[`docs/fetch.md`](../docs/fetch.md); this package implements it in pure
Go (stdlib only: `crypto/ed25519`, `crypto/sha256`, `archive/tar`,
`net/http`), as one command and one function.

**The command** — `go/cmd/chtypes`, runnable without installing anything:

```
go run github.com/wave-rf/chtypes/go/cmd/chtypes@latest fetch 25.8
chtypes fetch <line>... [--all] [--platform <os-arch>] [--dest <dir>]
                        [--tag <t> | --url <base>] [--lock <file>] [--frozen]
                        [--force] [--offline]
chtypes verify [--dest <dir>]        re-hash every installed line against its manifest
chtypes list   [--dest <dir>]        what is installed, and what the release offers
chtypes where                        the registry directory fetch would write to
```

Progress goes to stderr; `fetch` prints the installed directory alone on
stdout, so `dir="$(chtypes fetch 25.8)"` composes. Exit codes: 0 ok · 1
verification failed · 2 usage · 3 source unreachable · 4 not published for
this platform/line.

**The function** — idempotent: installed-and-verified is a no-op, otherwise
it walks the chain:

```go
inst, err := chtypes.Ensure(ctx, "25.8", chtypes.FetchOptions{Progress: os.Stderr})
// inst.Dir is <registry>/25.8; inst.AlreadyInstalled says whether anything was downloaded
```

`FetchOptions` is the flag set: `Dest`, `Platform`, `URL` (an http(s) mirror,
a `file://` path or a plain directory), `Tag`, `LockFile`, `Frozen`, `Force`,
`Offline`, `AllowUnsigned`, `TrustedKeys`, `Progress`, `HTTPClient`. The
zero value is the default fetch. `FetchAll` installs every line the release
publishes for the platform; `ListRelease`, `ListInstalled` and
`VerifyInstalled` are the `list` and `verify` commands.

**What is verified, in order** (§3): `SHA256SUMS.sig` is an ed25519
signature over the exact bytes of `SHA256SUMS`, checked against the embedded
release key (`ReleasePublicKeyHex`, key id `deb275922dbff76e`) — an unsigned
or mis-signed release is `CHTYPES_ARTIFACT_UNTRUSTED` and nothing is
downloaded around it; `index.json` names the asset and its sha256;
`SHA256SUMS` must agree; the tarball is hashed before it is unpacked; the
installed library is re-hashed in place against the `manifest.json` that
came inside. The install is atomic (temp sibling, rename).
`CHTYPES_TRUSTED_KEYS=<hex>[,<hex>…]` replaces the embedded key (a mirror,
a custom registry); `CHTYPES_ALLOW_UNSIGNED=1` skips the signature with one
loud warning naming the source — never the default, never silent. Once files
are in a registry directory the loader trusts the directory: verification is
a fetch-time policy, not a load-time gate.

**Pinning** (§5): `fetch --lock chtypes.lock` records, per
`<os>-<arch>/<minor>`, the asset file and sha256 installed; `fetch --frozen`
refuses anything else with `CHTYPES_ARTIFACT_PINNED`. `ReadLockFile` /
`LockFile.Write` read and write the same schema-1 JSON.

**Lazy fetch on first open** is opt-in — `NewRegistry(dir,
chtypes.WithAutoFetch(true), chtypes.WithFetchOptions(opts))`, or
`CHTYPES_AUTOFETCH=1` for every registry — because a production process must
not begin a 250 MB download inside a request. On, opening a missing line runs
`Ensure` first, once per process per line (concurrent opens wait for the one
in flight and share its result); `ForContext` bounds that fetch with a
context. `NewRegistry("")` opens the §1 search path alone and dlopens nothing
until asked.

**The one error** (§7): a missing line is `ErrArtifactMissing`, with the
message every SDK prints:

```go
lib, err := reg.For("25.8")
if errors.Is(err, chtypes.ErrArtifactMissing) {
    // chtypes: no artifact for ClickHouse 25.8 (darwin-arm64). Looked in: <dir1>, <dir2>, ….
    // Install it:  go run github.com/wave-rf/chtypes/go/cmd/chtypes@latest fetch 25.8
    // or set CHTYPES_AUTOFETCH=1 to fetch on first use.
}
var ae *chtypes.ArtifactError
if errors.As(err, &ae) { ae.Code /* CHTYPES_ARTIFACT_MISSING, …_UNTRUSTED, …_CORRUPT, …_PINNED, …_UNPUBLISHED, CHTYPES_SOURCE_UNREACHABLE */ }
```

Every fetch-time failure is an `*ArtifactError` too, with `Code` from that
shared vocabulary, a sentinel per code for `errors.Is`
(`ErrArtifactUntrusted`, `ErrArtifactCorrupt`, `ErrArtifactPinned`,
`ErrArtifactUnpublished`, `ErrSourceUnreachable`), and `ExitCode(err)` for
the §6 status.

Tested offline against the shared fixtures in `spec/fixtures/fetch/`
(signed, bad-signature, tampered-tarball, sums-index-mismatch, unsigned, the
lock, the TEST key) — every fixture produces the spec's verdict and code —
plus self-built releases for every link of the chain and one real artifact
fetched, installed and dlopen'd through `AutoFetch`.

## API reference

Errors column: **S** = `*SchemaError` (the server refused; `Code` is a real
ClickHouse code), **U** = `*UnsupportedError` (this build declines; fall back
to the server), **E** = plain `error` (usage/environment: closed handle,
version mismatch, bad artifact dir). Row/batch verdicts are never Go errors —
they are `Outcome` in the result.

| Symbol | C function | Params → returns | Errors |
|---|---|---|---|
| `ValidateType(v, expr)` | `chs_validate_type` | type expr → canonical spelling | S, U, E |
| `CompileDDL(v, ddl, opts...)` | `chs_schema_compile` + `chs_schema_column_*` | column list (+ `WithCompileSettings`, `WithCompileMode`) → `*CompiledSchema` | S (incl. 115 unknown setting, 455/44 declared gates), U (admission budget, mode≠0), E |
| `ParseSchema(v, Schema)` | via `CompileDDL` | `[]Column` → `*CompiledSchema` | same |
| `SetDefaultSettings(m)` | `chs_set_default_settings` | string-valued map → error | E wrapping the server's 115; refuses wholesale |
| `RegisteredFamilies()` | `chs_registered_families` | → `[]string` (139 on 25.8) | E |
| `FunctionFlags()` | `chs_function_flags` | → the volatility TSV audit, verbatim | E |
| `ReferenceType(expr)` | `chs_reference_type` | → widened reference type, `""` if none | E |
| `BuiltVersion()` | `chs_clickhouse_version` | the linked build's release (accessor — no caller can overwrite it) | — |
| `ABIRevision` | `CHS_ABI_REVISION` | header revision this package compiled against | — |
| `Timezone` | feeds `chs_init` | server TZ for bare DateTime; default `"UTC"`, set before first call | — |
| `CodeUnsupported` | `CHS_CODE_UNSUPPORTED` | the wire sentinel (−2); no error value carries it | — |
| `CompiledSchema.SetEngine(e, orderBy, opts...)` | `chs_schema_engine` | engine + sorting key (+ `WithMergeTreeSettings`) → error | S when rc>0 (server refusal, e.g. 115 unknown MergeTree setting), U when rc<0 (unmodelled engine/key, non-default MergeTree value) |
| `CompiledSchema.SetTTL(ttl)` | `chs_schema_ttl` | table rows-TTL expression → error | U (every refusal), E |
| `CompiledSchema.Row / RowWithSettings` | `chs_row` | format, raw bytes (+ settings) → `RowResult` | E only; verdict in `Outcome` |
| `CompiledSchema.Rows` | `chs_rows` | format, body bytes, settings → `BatchResult` | E only; verdict in `Outcome`/`EngineRows` |
| `CompiledSchema.RowsExport(f, body, settings, exportFormat, docFlags...)` | `chs_rows` — the same ONE call | adds the revision-3 export channel (`Payload`+`Spans`+`ExportDeclined` on the `BatchResult`) and the document groups (`DocValues`/`DocTransforms`/`DocDefaults`; none = lean). `ExportNone` = no bytes | E only; a bad export format / flag bit answers `Unsupported` in the result |
| `CompiledSchema.CompileFilter(expr, opts...)` | `chs_filter_compile` | one boolean expression over the schema's physical columns (+ `WithFilterParams` binding `{name:Type}` query parameters — values are STRINGS, never hand-escaped) → `*Filter` (WHERE-side semantics by construction) | S (the server's own refusal: 47 unknown identifier, **456** unbound param, **457** unparseable param value), U (clock reads), E |
| `Filter.Rows(f, body, settings)` | `chs_filter_rows` | per-row `Verdicts` (True/False/Error/Decline — the last two are NOT answers; fail closed) in a `FilterResult` | E only; call verdict in `FilterResult.Outcome` |
| `CompiledSchema.ParseBlock(f, body, settings)` | `chs_block_parse` | parse a body ONCE → `*Block`, evaluable by K filters with no re-parse; per-row parse failures live IN the block (they answer Decline), a call-level failure yields NO block | S (unknown setting 115, framing, decode fault), U, E |
| `Filter.Eval(block)` | `chs_filter_eval` | the same `FilterResult` `Filter.Rows` answers, over an already-parsed `Block` — `Eval(ParseBlock(body))` ≡ `Rows(body)`; a cross-schema (filter, block) pair answers `FilterRejected`/1002, loudly | E only (closed handles, cross-library pair) |
| `Filter.Close` / `Block.Close` | `chs_filter_free` / `chs_block_free` | releases the handle; the schema's `Close` frees its open filters AND blocks FIRST (the C-required order), finalizer backup | — |
| `CompiledSchema.Close` | `chs_schema_free` | releases the handle (finalizer backup; open filters and blocks closed first) | — |
| `NewRegistry(dir, opts...)` | dlopen + `manifest.json` scan | artifact dir (loaded now) or `""` (the §1 search path, loaded on demand) → `*Registry`; `WithAutoFetch`, `WithFetchOptions` | E |
| `Registry.Load(path)` | dlopen, `chs_abi_revision`, `chs_init` | one artifact → registered | E (ABI mismatch, missing core symbols) |
| `Registry.For(v)` / `ForContext(ctx, v)` | — | minor line or exact patch → `*Library`, from the loaded set, then the first search-path directory holding the line, then (AutoFetch) a fetch | `ErrArtifactMissing` (§7, `errors.Is`); a fetch's `*ArtifactError`; never nearest-match |
| `Registry.Versions()` | — | minor lines, numerically sorted (loaded, plus discovered on the search path) | — |
| `Ensure(ctx, line, opts)` | derived — no C call | fetch/verify/install one line (docs/fetch.md §2–§5) → `*Installed` | `*ArtifactError` with a §7 `Code` |
| `FetchAll(ctx, opts)` / `ListRelease(ctx, opts)` / `ListInstalled(dir)` / `VerifyInstalled(dir)` | derived | every published line; the verified `*ReleaseIndex`; the `[]Installed` under a registry; each re-hashed → `[]VerifyResult` | `*ArtifactError`, E |
| `FetchOptions`, `Installed`, `ReleaseIndex`, `ReleaseArtifact`, `VerifyResult` | — | the fetch's inputs and results | — |
| `ErrArtifactMissing` (+ `ErrArtifactUntrusted`, `ErrArtifactCorrupt`, `ErrArtifactPinned`, `ErrArtifactUnpublished`, `ErrSourceUnreachable`), `ArtifactError`, `ErrorCode`, `ExitCode(err)` | — | the §7 error, the shared codes, the §6 exit status | — |
| `RegistrySearchPath(explicit)` / `FetchRegistryDir(explicit)` / `DefaultRegistryDir()` / `DefaultRegistryDirFor(platform)` / `SystemRegistryDirs(platform)` / `HostPlatform()` / `ValidPlatform(p)` | — | the §1 search path, where a fetch writes, the platform key | — |
| `ReleasePublicKeyHex`, `ReleaseKeyID`, `ReleasePublicKey()`, `KeyID(pub)`, `ParseTrustedKeys(spec)`, `VerifySignature(msg, sig, keys)`, `ParseSignatureFile(b)` | — | the §4 signature: the embedded key and the checks | E |
| `LockFile`, `LockEntry`, `ReadLockFile(path)`, `LockFile.Write(path)`, `LockKey(platform, minor)`, `NewLockFile()` | — | the §5 pin file, schema 1 | E |
| `GoFetchCommand`, `DefaultArtifactsURL`, `DefaultReleaseTag`, `DefaultLockFile`, `LockSchema` | — | the spellings the contract fixes | — |
| `Library.CompileDDL / ValidateType` | as static twins | same contract, this version's answers | S, U, E |
| `Library.RegisteredFamilies / FunctionFlags / ReferenceType` | the introspection trio, per library | same answers as the static twins, from THIS artifact | U (artifact predates the symbol) |
| `Library.HasCompileSettings()` | dlsym probe | artifact exports settings-aware compile? | — |
| `Library.{Version, Minor, Path, ABIRevision}` | `chs_clickhouse_version`, `chs_abi_revision` | identity fields | — |
| `LoadedSchema.SetEngine / SetTTL / Row / RowWithSettings / Rows / RowsExport / CompileFilter / ParseBlock / Close` (+ `LoadedFilter.Rows / Eval / Close`, `LoadedBlock.Close`) | as `CompiledSchema` / `Filter` / `Block` | same contract; missing symbol → U ("predates … support") | S, U, E |
| `QueryServerVersion / QueryChangedSettings / QueryTableColumns` | derived — no C call | the three discovery SQL constants | — |
| `ParseVersionResult / ParseChangedSettingsResult / ParseColumnsResult` | derived — no C call | JSONEachRow bytes → version / map (duplicate names: last write wins) / `[]DiscoveredColumn` | E |
| `QuoteIdentifier(name)` | derived — no C call | ClickHouse DDL identifier spelling: bare where legal, else backticked with backticks doubled | — |
| `ReconstructDDL(cols)` | derived — no C call | discovered columns → column-declaration list | E |
| `ServerProfile`, `DiscoveredColumn` | derived | discovery result types | — |
| Result types: `RowResult`, `BatchResult` (+`Payload`/`Spans`/`ExportDeclined`), `Value`, `Transform` (+`Lossy`), `Substitution`, `Computed`, `Outcome`, `Span`, `DocFlags`, `FilterResult`, `Verdict` (+`Answered`), `FilterOutcome`, `Reason*` consts | parsed from the C result documents | see godoc — `stored`/`ref` are kept as raw JSON, numbers exact | — |
| `Schema`, `Column`, `DefaultKind`, `Format`, `Version`, `CompileMode`/`CompileDeclared`, option types | — | inputs; `Format`/mode integers are the frozen ABI codes | — |

Full doc: `go doc github.com/wave-rf/chtypes/go/chtypes` (or hover in
an IDE — every exported symbol carries its contract).

## Settings and precedence

Settings maps are `map[string]string`. **Values must be strings** — a 19-digit
nanosecond epoch does not survive an IEEE double, and a numeric value is
*silently ignored* by the C side. An unknown setting name is the server's own
**115** on every channel; `SetDefaultSettings` refuses its whole payload, so a
partial profile is never silently in force.

Row-path resolution order (later wins):

    ClickHouse defaults  <  SetDefaultSettings  <  compile profile  <  per-call map

One exception, the server's own: a **type gate** named in the compile profile
(`allow_experimental_json_type`, `allow_suspicious_low_cardinality_types`, …)
binds at compile — a refusing value fails the compile with the server's
455/44 — and thereafter the handle outranks even an explicit per-call value,
exactly as a real CREATE settles the gate for the table's life.

The six `chtypes_*` keys (the whole reserved namespace — anything else spelled
`chtypes_*` is an unknown name, code 115):

| per-CALL (clock) | per-PROCESS (`SetDefaultSettings` only) |
|---|---|
| `chtypes_now_epoch_nanos` — pin the batch instant | `chtypes_default_eval_memory_bytes` — DEFAULT admission ceiling (256 MiB) |
| `chtypes_clock_offset_nanos` — measured server−client offset | `chtypes_default_eval_wall_nanos` — wall ceiling (1 s) |
| `chtypes_max_clock_skew_nanos` — refuse substitution past this | `chtypes_custom_settings_prefixes` — mirror the server's config |

A per-process key sent per call comes back in `UnsupportedSettings` — a
decline, never an admission, and the row must not be scored as agreement.

## Batches: always Rows, and the two bad-row policies

**Call `Rows` for everything.** One row is a batch of one; the vendored
reader does all framing internally, so a bare JSON object, NDJSON/JSONL,
and a `[...]`-wrapped array are the same `JSONEachRow` body — never split
or sniff a payload in Go. (`Row` is sugar for call sites that *semantically*
expect exactly one row — config checks, tests. On a multi-row body it
returns the first row and ignores the rest, so it is the wrong call for
anything wire-facing.)

What happens at the first bad row is ClickHouse's own policy, declared like
any other setting — measured, both modes:

**Strict (the server default).** The batch aborts at the first unparseable
row, exactly like a real INSERT: `BatchResult.Rows` holds every examined
row — the failures itemized with ClickHouse's real code and message — and
rows after the failure are never examined (`Outcome` is `Rejected`).

**Skip-and-continue.** Declare it once at compile and it is the handle's
default for every later call:

```go
schema, err := lib.CompileDDL(ddl, chtypes.WithCompileSettings(map[string]string{
    "input_format_allow_errors_ratio": "1", // skip any number of bad rows
}))
batch, err := schema.Rows(chtypes.JSONEachRow, body, nil)
// batch.Outcome == Accepted; batch.Rows holds every input record in order:
// survivors as Accepted, each skipped record as Skipped with the server's
// own error. batch.RowsRead == len(batch.Rows);
// batch.RowsSkipped counts the Skipped entries.
```

Bad rows are skipped with the server's own machinery (resync is
line-based, byte-matched to real servers across all seven versions) and
**itemized** (2026-08-27): each skipped record keeps its place in `Rows`
with `Outcome == Skipped` and the exact error the vendored reader caught
before resyncing (`IRowInputFormat::generate` computes it for every skip
and then logs only a count — reporting it invents nothing). So one `Rows`
call answers, per input record and in input order: accepted (with coerced
`Values`) or skipped (with `ErrCode`/`ErrMsg`). A `Skipped` row is never
stored — filter on the row `Outcome` when rendering survivors.

Two pairing rules for a gateway/worker split: the settings chtypes sees
must be the settings the INSERT runs under — but `input_format_allow_errors_*`
is the exception that must **never** reach the real INSERT (server-side
skipping is silent data loss). Validate with it at the gate, forward only
accepted rows, and the worker's insert needs no error allowance at all.

## Export and filters (ABI revision 3)

**`RowsExport`** is `Rows` with the export and document-flag channels exposed
— still exactly ONE `chs_rows` call. Request `JSONCompactEachRow` (the one
format this revision serializes) and the accepted rows come back as wire
bytes in `BatchResult.Payload`, addressed per row by `Spans` (index-aligned
with `Rows`; `{0,0}` for a non-accepted row; slicing a span out of `Payload`
IS that row's line, `\n` included, and concatenating non-zero spans
reproduces `Payload` exactly). `Payload == nil` means no export / declined
(`ExportDeclined` names the reason) / a call-level verdict preempted it; a
non-nil empty `Payload` is the emitted-empty answer. The bytes are copied out
of the C buffer and freed before the call returns — no ownership crosses the
boundary. Doc flags (`DocValues`, `DocTransforms`, `DocDefaults`; none =
lean) thin the *description*, never the *verdict*; `Rows()` is the `DocAll`
spelling and remains byte-identical to revision 2.

**`CompileFilter`** compiles one boolean expression against the schema and
`Filter.Rows` answers per-row verdicts with **WHERE-side** semantics —
`x = 256` over `UInt8` promotes (false for every row), never wraps — computed
by ClickHouse's own comparison functions. Four verdicts: `VerdictTrue` /
`VerdictFalse` are answers; `VerdictError` (the predicate threw on this row —
a real server fails the whole query) and `VerdictDecline` (this library
declines) are NOT, and an enforcing caller MUST fail closed on both. Clock
reads are refused at compile (`*UnsupportedError`). A `Filter` must not
outlive its schema: this binding enforces the order structurally (the
schema's `Close` frees open filters first, and finalizers run in dependency
order), and a filter call is also a use of its schema handle, so two filters
over one schema never run concurrently. **Enforcement gate**: no read-side
security may be enforced on this surface until the WHERE-truth rig gates
green — until then it is shadow/replay only (`spec/c-abi.md` §Filters).

## Query parameters and the block twin (ABI revision 4)

**Params.** The expression may contain `{name:Type}` query parameters, bound
with `WithFilterParams(map[string]string{...})` — values are **strings**,
exactly as the server's own parameter channels carry them. Substitution is
the server's own `ReplaceQueryParameterVisitor`: each value is deserialized
by the DECLARED type's own reader and injected as a typed literal AFTER SQL
parsing, so a value is never SQL text and **must never be hand-escaped into
the expression** — injection safety is by construction, and a hostile value
(`' OR 1=1 --`) compares as exactly that literal. An UNBOUND parameter is
the server's own **456** ("Substitution `name` is not set"), an unparseable
value the server's own **457** — both `*SchemaError`, verbatim; a bound name
the expression never uses is ignored. The compiled handle bakes the values
in — identity is per (schema, expr, params) — so **a caller compiling
filters from tenant-influenced values MUST bound its cache and its compile
rate**: a bounded LRU keyed on (schema generation, expr, params-hash) plus a
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
(unreachable through this SDK's unique-keyed map, stated for completeness).

One naming trap, the transport's rather than this library's: on a real
server's **TCP** channel a parameter *named* `limit` or `offset` fails at
the protocol layer with code 26 even when unused (params ride in a Settings
block there); HTTP is fine, and this library matches the HTTP/substitution
semantics. Avoid those two names for anything that will ever cross TCP.

**The block twin.** `ParseBlock(format, body, settings)` parses a body ONCE
into a `*Block` — the parse half of `Filter.Rows` — and `Filter.Eval(block)`
answers the same `FilterResult` with no re-parse: the live-SSE hot path is K
filters × 1 event, and the re-parse is shed. Normative equivalence:
`Eval(ParseBlock(body))` ≡ `Rows(body)` for every verdict class (volatile
DEFAULTs resolve against the PARSE call's clock instant — pin
`chtypes_now_epoch_nanos` for cross-call identity). Evaluation is a pure
function of (filter, block), takes no settings, and never consumes or
mutates the block. Filter and block MUST come from the SAME schema handle: a
mismatched same-library pair answers `FilterRejected`/1002 loudly, and a
cross-library pair is refused by this binding before any C call. A `Block`
follows the filter's lifetime rules exactly (schema `Close` frees open
blocks first; an eval is a use of BOTH handles). Parse-once does not open
enforcement earlier: the twin is under the same enforcement gate.

## Discovery

The library **never connects to ClickHouse**. It ships three SQL constants the
*application* runs at connect time with whatever client it already has, plus
typed parsers:

| constant | answers | feeds |
|---|---|---|
| `QueryServerVersion` | the exact release | `Registry.For` |
| `QueryChangedSettings` | every setting changed from default | `WithCompileSettings` + per-call settings |
| `QueryTableColumns` | one table's columns, kinds, expressions | `ParseColumnsResult` → `ReconstructDDL` → `CompileDDL` |

```go
version, _  := chtypes.ParseVersionResult(run(chtypes.QueryServerVersion))
settings, _ := chtypes.ParseChangedSettingsResult(run(chtypes.QueryChangedSettings))
profile := chtypes.ServerProfile{Version: version, Settings: settings}

lib, _ := reg.For(chtypes.Version(profile.Version))
cs, _  := lib.CompileDDL(ddl, chtypes.WithCompileSettings(profile.Settings))
res, _ := cs.Rows(chtypes.JSONEachRow, body, profile.Settings)
```

Cache the profile per deployment (or per tenant). Never ask the customer for
their settings — ask their server; a typo in the declared profile is the
server's own 115 at compile, not silence. `ReconstructDDL` carries
`default_kind`/`default_expression`, without which DEFAULT/MATERIALIZED
semantics are silently lost.

## Multi-version use and thread-safety

`NewRegistry` dlopens every artifact with `RTLD_NOW | RTLD_LOCAL` — that
isolation is what lets two builds that both define `DB::DataTypeFactory` share
one process. Cost: ~120 MB resident per loaded version. `Registry.For`
resolves a minor line or an exact patch and **never falls back** to a nearest
version; failure names the versions that are loaded. Version behaviour is
non-monotonic across releases, so nothing is inferred from a neighbour.

Locking (the C contract: concurrent calls are safe on *distinct* handles; one
handle is single-threaded; init/settings writes exclude everything):

- one mutex per `CompiledSchema` / `LoadedSchema` — one handle, one goroutine
  at a time; distinct schemas run in parallel;
- a per-`Library` RWMutex read-held by every call (`chs_init` runs exactly
  once per artifact path, before the `Library` is visible);
- `SetDefaultSettings` takes the static path's lock exclusively; a dlopen'd
  `Library` *structurally cannot* make that call — the symbol is deliberately
  not in its function-pointer table;
- no shutdown/teardown surface, deliberately: Go never `dlclose`s a loaded
  artifact, so `chs_shutdown` is never owed (spec/bindings.md §Teardown).

## Known limitations

- **macOS is a dev floor, not an oracle**: its 53-bit `long double` makes
  float parses diverge from real servers (Linux matches 395/395 of the float
  corpus; macOS 0/395). Float expectations must come from Linux or a live
  server.
- **EPHEMERAL columns**: an INSERT naming one in an explicit column list
  cannot be previewed through a format stream — decline it, never mispreview
  (spec/bindings.md §EPHEMERAL).
- Engines/sorting keys beyond the modelled MergeTree family, `WHERE`/`GROUP
  BY`/`TO DISK`/`RECOMPRESS` TTL forms, clock-reading TTLs, server-property
  DEFAULTs (`hostName()`, …) and blocking DEFAULTs are **declines**
  (`UnsupportedError` / `Outcome == Unsupported`), by design — fall back to
  the server, never guess.
- Predicate constants are not payloads: never reuse insert-side coercion to
  fold a `WHERE` literal (spec/bindings.md §Constants are not payloads).
- ~~Skipped rows are counted, not itemized~~ — **resolved 2026-08-27**:
  skipped rows now appear in `Rows`, in input order, as `Outcome == Skipped`
  with the caught error verbatim (see §Batches). The entry stood from
  2026-08-27 (measured) to 2026-08-27 (itemization cycle).
- Full error model and result-document contract: `spec/c-abi.md` §Error
  model; per-topic details under `docs/`; runnable tours under
  `playground/go/`.
