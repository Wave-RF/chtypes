# The binding contract

Every language binding wraps the same C ABI, so every language binding should
present the same shape. This document fixes that shape: the names, the
semantics, the result types, the version-selection rules, and what a binding
must actually run before it may claim conformance.

Every language — go, python, ts, rust — is a peer SDK over the same C ABI; no
language is privileged. All four are scored arbiter columns
(`tests/arbiter/RESULTS.md`). The Go package `go/chtypes` is the
*working
reference* only in the sense that it is today's most complete SDK
(see `spec/README.md`). Where a language has a strong
idiom that conflicts with a name here — snake_case in Python, camelCase in
TypeScript, `Result`-returning methods in Rust — follow the idiom and keep
the *concept* identical. What must never drift is meaning.

## The object model

Four objects. Nothing else is required.

```
Registry ── For(version) ──▶ Library ── CompileDDL(ddl) ──▶ Schema ── Rows(...) ──▶ BatchResult
                                │                             │
                          ValidateType(expr)            SetEngine / SetTTL      Row(...)  ──▶ RowResult
```

| Concept | Go | Python | TypeScript | Notes |
|---|---|---|---|---|
| Artifact directory loader | `NewRegistry(dir)` | `Registry(dir)` | `new Registry(dir)` | Scans one subdirectory per version |
| One loaded version | `*Library` | `Library` | `Library` | Carries `Version`, `Minor`, `Path` |
| ABI revision (binding) | `chtypes.ABIRevision` | `chtypes.ABI_REVISION` | `ABI_REVISION` | Rust: `chtypes::ABI_REVISION`. The revision the binding was written against; cgo reads the header macro, the rest mirror it by hand |
| ABI revision (artifact) | `lib.ABIRevision` | `lib.abi_revision` | `lib.abiRevision` | Rust: `lib.abi_revision()`. `0` = predates the probe. A different nonzero value is refused at load — see `docs/reference/c-abi.md` §The ABI revision |
| Resolve a version | `r.For(v)` | `r.for_version(v)` / `r[v]` | `r.for(v)` | Minor line **or** exact patch |
| List versions | `r.Versions()` | `r.versions()` | `r.versions()` | Minor lines, sorted |
| Compile a column list | `lib.CompileDDL(ddl)` | `lib.compile_ddl(ddl)` | `lib.compileDdl(ddl)` | Rust: `lib.compile(ddl).compile()` (builder) |
| … under a declared settings profile | `lib.CompileDDL(ddl, WithCompileSettings(m))` | `lib.compile_ddl(ddl, settings=…, mode=…)` | `lib.compileDdl(ddl, {settings, mode})` | **ONE function per SDK, options optional** — see §One compile function. Rust: `lib.compile(ddl).settings(…).compile()`. `docs/reference/c-abi.md` §Compile-time vs per-call settings; declaring no settings behaves exactly as if the parameter did not exist |
| Canonicalise a type | `lib.ValidateType(expr)` | `lib.validate_type(expr)` | `lib.validateType(expr)` | |
| Declare the engine | `s.SetEngine(engine, orderBy)` | `s.set_engine(engine, order_by)` | `s.setEngine(...)` | |
| … + MergeTree settings | `s.SetEngine(engine, orderBy, WithMergeTreeSettings(m))` | `s.set_engine(..., merge_tree_settings=…)` | `s.setEngine(..., {mergeTreeSettings})` | Same one function. Unknown name ⇒ the server's **115 rejection**; a non-default declared value ⇒ **-2 unsupported** (never silently ignored); a binding MUST NOT flatten those two into one verdict — see rule 12 |
| Declare the rows TTL | `s.SetTTL(ttl)` | `s.set_ttl(ttl)` | `s.setTtl(ttl)` | |
| One row | `s.Row(format, raw)` | `s.row(format, raw)` | `s.row(format, raw)` | |
| One row + settings | `s.RowWithSettings(format, raw, settings)` | `s.row(..., settings=)` | `s.row(..., settings)` | A default argument is fine |
| A whole body | `s.Rows(format, body, settings)` | `s.rows(...)` | `s.rows(...)` | |
| Release | `s.Close()` | context manager / `close()` | `s.close()` / `Symbol.dispose` | |
| Process teardown | package-level only — `*Registry`/`*Library` expose none, deliberately | `Registry.close()` / `Library.close()`, context manager | `registry.close()` / `library.shutdown()`, `Symbol.dispose` | Rust: `Registry::shutdown()` / `Library::shutdown()`. **Required before `dlclose`** — see below |

A binding MAY additionally expose the statically linked, single-version shape
(the reference's package-level `ValidateType` / `CompileDDL`, linked against one
artifact). It is the fast path and it is optional; the Registry path is the
product.

### Revision 3: the `chs_rows` export/flags parameters, and the filter trio

At ABI revision 3 the C `chs_rows` gained `export_format`, `doc_flags` and
`out_bytes` (`docs/reference/c-abi.md` §Rows), and `chs_filter_compile` /
`chs_filter_free` / `chs_filter_rows` joined the surface (§Filters). What
that means for a binding:

* **`Rows()` keeps today's behavior exactly**: one `chs_rows` call with
  `export_format = CHS_EXPORT_NONE` (`-1`) and
  `doc_flags = CHS_DOC_ALL` (`7`), `out_bytes = NULL`. The document that
  comes back is byte-identical to revision 2's, so nothing downstream moves.
* **`RowsExport()`** (per `docs/proposals/rows-export.md`: flags 0 by
  default, the bitmask exposed, `Payload []byte` + `Spans` from
  `out_bytes` + `row_spans`) and the filter SDK surface
  (`CompileFilter` / verdict types) are **specified for the SDK cycle that
  follows the C surface** — this revision's bindings changes are the
  mechanical pass-through above and the hand-kept `ABI_REVISION` mirrors
  bumping to 3 in the same cycle, nothing more. One C call per SDK method,
  always; never a second call, never re-parsing.
* **Lean documents stay SDK-derivable.** Under `CHS_DOC_TRANSFORMS` without
  `CHS_DOC_VALUES` the C layer retains every `cols[]` entry any spec'd
  detector could fire on, with the full field set (the conservative
  byte-equality retention rule, `docs/reference/c-abi.md` §Document flags). A binding
  runs the SAME detectors of §Transformed over the retained entries — no new
  classifier exists on either side, and the reason vocabulary does not move
  into C. Under `CHS_DOC_VALUES` without `CHS_DOC_TRANSFORMS` the reference
  parse is skipped C-side (`ref` is `null`, no `wire`), so detectors 2 and 3
  have nothing to run on — that is the caller's explicit choice, not data
  loss.
* **Filter verdicts map to a four-state type, and two of the states are
  fail-closed.** `'t'`/`'f'` are answers; `'e'` (the predicate threw on this
  row — the server would have failed the whole query) and `'d'` (this
  library declines) are NOT answers, and a caller enforcing visibility MUST
  hide the row / fail the request on both. A binding MUST NOT collapse `'e'`
  or `'d'` into `false`-the-answer: under `NOT`, a decline-read-as-false
  inverts fail-closed into fail-open — the measured leak class. Unknown
  verdict characters degrade to the decline state, mirroring the unknown-
  `outcome` rule below. **No SDK may offer filter-backed read-side
  enforcement until the WHERE-truth rig gates green** (`docs/reference/c-abi.md`
  §Filters, the enforcement gate); until then the surface is shadow/replay.
* A filter handle wraps BOTH pointers' lifetimes: the SDK object MUST keep
  its schema object alive (a reference, not a copy) and free the filter
  before the schema — the C layer does not refcount
  (`docs/reference/c-abi.md` §Filters, handle lifetime).

### Revision 4: filter query parameters, and the block-parse twin

At ABI revision 4 the C `chs_filter_compile` gained a `params_json`
parameter (`{name:Type}` query parameters, substituted by the vendored
`ReplaceQueryParameterVisitor` before analysis — `docs/reference/c-abi.md` §Filters,
Query parameters), and `chs_block_parse` / `chs_block_free` /
`chs_filter_eval` joined the surface (§Blocks: parse a body once, evaluate K
filters against the block). What that means for a binding:

* **This revision's bindings changes are mechanical**, exactly as revision
  3's were: the hand-kept `ABI_REVISION` mirrors bump to 4 in the same
  cycle, and the cgo reference passes `NULL` for `params_json` at its
  existing `CompileFilter(expr)` call site — behavior-preserving for every
  expression revision 3 accepted. The params SDK surface (a name → value
  string map argument) and the block SDK surface (a `Block` object;
  `Filter.Eval(block)`) are **specified for the SDK cycle that follows the C
  surface** (one C call per SDK method, as always).
* **One expectation moves, and a test suite must move with it**: an
  expression containing `{name:Type}` with no binding for `name` is no
  longer a `-2` decline ("render literals") — it is the server's own
  `UNKNOWN_QUERY_PARAMETER` **456**, the refusal type (`SchemaError` class),
  because the substitution now runs and the server's own visitor throws it.
* **Handle-pairing refusals split by ownership, deliberately** (delivered
  SDK cycle, 2026-08-31): a (filter, block) pair from two different
  dlopen'd LIBRARIES must be refused by the BINDING before any C call —
  `chs_filter_eval` takes no library identity and dereferencing a foreign
  image's block is undefined, so each SDK checks image identity its own
  way (runtime check in Go/Python/TS; a `CrossLibrary` error in Rust). A
  same-library pair over two different SCHEMAS is C's job and stays C's:
  the library answers its own rejected document, code 1002, and a binding
  MUST pass that pair through rather than pre-empt it.
  A value the declared type cannot parse is the server's own **457**.
* **When an SDK grows the params surface** it MUST pass values as strings
  (the `settings_json` convention), MUST NOT hand-escape values into the
  expression text (injection safety comes from typed substitution, not
  escaping), and MUST adopt the caller-side cache discipline of
  `docs/reference/c-abi.md` §Filters: filter handles are per-(schema, expr, params),
  so a cache keyed on tenant-influenced values needs a bounded LRU and a
  per-principal compile throttle.
* **A block handle wraps its schema's lifetime** exactly as a filter does
  (reference, not copy; free blocks before the schema), a block may be
  evaluated by many filters sequentially, and an eval call is a use of both
  handles — an SDK enforcing with the schema's own lock (as the reference
  does for filters) gets the block rules for free.
* **Verdict semantics do not move**: `chs_filter_eval` returns the same
  document `chs_filter_rows` returns, with the same four-state verdict rule
  above — `'e'`/`'d'` stay fail-closed, and the enforcement gate
  (`docs/reference/c-abi.md` §Filters) still applies to the twin. Parse-once does not
  mean enforce-earlier.

### Introspection — the same three questions in every SDK (added 2026-08-26)

The ABI's introspection group is three functions, and every SDK exposes all
three **per `Library`** (the Go binding additionally mirrors them on its
optional static path). A missing symbol degrades to the DECLINE type at call
time — never a load failure, never a crash.

| C entry point | Go (`*Library`) | Python | TypeScript | Rust | shape |
|---|---|---|---|---|---|
| `chs_registered_families` | `RegisteredFamilies()` | `registered_families()` | `registeredFamilies()` | `registered_families()` | the family names, one string per registry entry |
| `chs_function_flags` | `FunctionFlags()` | `function_flags()` | `functionFlags()` | `function_flags()` | the TSV audit **verbatim** — one function per line, six tab-separated fields (`name`, `deterministic`, `deterministic_in_query`, `server_constant`, `stateful`, `resolver_error_code`); a binding MUST NOT re-model it |
| `chs_reference_type` | `ReferenceType(expr)` | `reference_type(expr)` | `referenceType(expr)` | `reference_type(expr)` | the widened reference type as ClickHouse spells it; the empty answer (`""` / `None`) for a type with no wider type |

None of the three is needed to *function* — the reference parse already runs
inside `chs_row` — but an uneven surface means only some SDKs can explain a
transformation finding or audit statelessness, which is why parity is a rule
rather than a nicety. Before 2026-08-26 the matrix was ragged (Go exposed
only static `RegisteredFamilies`; Python exposed only `reference_type`; TS
lacked `functionFlags`); Rust was the complete column the others were brought
up to.

### Teardown, and the binding that deliberately has none

`chs_shutdown` joins the DEFAULT evaluator's background threads. `chs_init`
registers it with `atexit`, so an ordinary process needs no call at all; it is
**required before `dlclose`**, and only then — the thread it joins is still
looping otherwise, and unmapping the library under it is a crash.

**A binding that never `dlclose`s a loaded artifact therefore satisfies this
row by construction, and MAY omit the entry point entirely.** Go's `*Registry`
and `*Library` do exactly that, and it is a design decision rather than a gap
(`go/chtypes/multiversion.go`, the locking commentary): the `dlopen`'d
path resolves a fixed function-pointer table that deliberately does **not**
`dlsym` `chs_shutdown` — nor `chs_set_default_settings`, "the one genuinely
dangerous call in the ABI … a `Library` structurally cannot make it". Omitting
the symbol is what buys that. Go unmaps a library only when it fails the
mandatory-symbol check *before* `chs_init` runs, and never afterwards, so no
`chs_shutdown` is ever owed.

Python, TypeScript and Rust do expose it — they also expose
`set_default_settings` on the same object — and for them the row is a real
requirement, not a vacuous one. And because `dlopen` refcounts ONE image per
file, a binding whose wrappers are not interned MUST refcount its teardown on
the resolved path, exactly as rule 3's lock is keyed (§Concurrency): two
`Registry` instances over one artifact share the C globals `chs_shutdown`
tears into, so closing the first must be a no-op at the C boundary while the
second still holds the image, and the LAST close is the one that calls
`chs_shutdown`. Python's `Library.close()` does exactly this (2026-08-26 —
before it, closing one Registry joined the DEFAULT evaluator's threads under
the other's live libraries). TS's `shutdown()` and Rust's `shutdown()` are
not refcounted: they are documented as the caller's own explicit teardown
decision (Rust deliberately has no `Drop` for the same shared-image reason),
and a host that runs two registries over one artifact owns the ordering of
its explicit teardown calls. The known residue: TS's `using`/`Symbol.dispose`
sugar makes an accidental early `close()` easier to write than in Rust; if
that ever bites, the fix is the same resolved-path refcount Python carries. This paragraph replaced a table row that read
"`chs_shutdown` via the loader | same | same | Required before `dlclose`" for
all three columns, which the Go implementation had never matched; found by the
four `examples/` tours, finding 2. The spec row was the wrong one.

**Reopening after a full close is not promised, and does not work.** Once the
LAST holder of an image has closed it (`chs_shutdown` ran, `dlclose` followed),
constructing a new Registry over the same artifact in the same process is
undefined — measured 2026-08-31: an immediate reopen SEGFAULTS (the C image's
statics do not survive the teardown/reload cycle). Teardown is
end-of-process-shaped: close once, at the end, never mid-lifecycle with intent
to return. A host that needs the artifact again later should simply not close
it (the Go binding's no-teardown design is this rule made structural).

### One compile function

**A binding MUST expose exactly one compile entry point and one engine entry
point**, with the settings profile optional on each. Not two functions, not a
`…V2` twin: the C ABI itself carries the settings as ordinary parameters whose
`NULL` case is structurally the profile-less path (`c-abi.md`
§Compile-time vs per-call settings, point 7), so a second SDK function would be
inventing a distinction the library does not have. The 2026-08-24 consolidation
removed exactly that duplication from all four reference bindings.

*How* the options are spelled is each language's own business, and the
reference bindings deliberately differ rather than transliterating Go:

| SDK | spelling | why this one |
|---|---|---|
| Go | variadic functional options — `CompileDDL(ddl, WithCompileSettings(m))` | keeps the common call one argument, names the option at the call site, and is the idiom Go's own libraries use for exactly this shape |
| Python | keyword-only arguments — `compile_ddl(ddl, *, settings=None, mode=COMPILE_DECLARED)` | the language has named optional arguments; anything else would be ceremony |
| TypeScript | a trailing options object — `compileDdl(ddl, {settings, mode})` | idiomatic, and it grows without a signature change |
| Rust | a builder — `lib.compile(ddl).settings(…).compile()` | Rust has neither named nor optional arguments; the alternative was `Option` arguments at every call site, which puts `None, CompileMode::Declared` in the common path |

The engine call has ONE optional parameter rather than two, so it needs no
builder anywhere: Go and TypeScript reuse their option carrier, Python its
keyword, and Rust takes the settings slice positionally
(`s.set_engine(engine, order_by, NO_SETTINGS)` when there are none), matching
the slice convention its `rows` / `set_default_settings` already use.

A binding MUST expose the compile mode as a named constant equal to the C
`CHS_COMPILE_DECLARED` (`0`) rather than a bare literal, and MUST pass an
unrecognized mode through to the library rather than validating it locally —
the refusal (`-2`) is the library's to make.

### Values a binding must accept and reject

- `format` MUST be the integer `chs_format` code, exposed as a named
  enum/constant set with those exact numbers: `JSONEachRow=0`, `CSV=1`, `TSV=2`,
  `Values=3`, `JSONCompactEachRow=4`, `RowBinary=5`, `RowBinaryWithDefaults=6`,
  `RowBinaryWithNamesAndTypesAndDefaults=7`, `Native=8`, `Buffers=9`.
- **A binding MUST NOT declare a format it has not asked the ARTIFACT about.**
  Support for `RowBinary`, `Native` and `Buffers` depends on when the loaded
  artifact was linked, not on the SDK's own version, and declaring one an
  artifact cannot parse turns a neutral `not_offered` into scored divergences.
  The probe is one payload through `chs_rows`: an artifact that has the reader
  answers `accepted`, an older one rejects. See `chtypes-core/tests/conformance/*` for the
  four reference probes.

  **`Buffers` needs one refinement of that probe, and it is not optional.** The
  format arrives at ClickHouse 26.5, so on 24.8-25.10 an artifact that DOES
  have the decoder correctly answers `rejected` with **73 `UNKNOWN_FORMAT`** —
  the server's own answer. An artifact that does NOT have the decoder does not
  know the format integer at all and answers **117**, *"unknown format 9"*,
  from the text splitter's default arm. Treating both as "not supported" would
  discard 126 correctly-answered cases per version as `not_offered`; treating
  both as "supported" would score an old artifact's 117 against the server's
  73. A binding MUST therefore accept `accepted` **or** `rejected` with code 73
  as proof the decoder is present, and only code 117 as proof it is absent.
  Both codes come from the artifact, so this remains a question asked rather
  than assumed.
- `raw` / `body` MUST be a byte sequence, never the language's text type, and MUST
  be passed with an explicit length. Binary formats contain NUL bytes, and text
  rows can contain invalid UTF-8 on purpose.
- `settings` MUST be a mapping whose **values are strings** at the boundary. See
  `c-abi.md` §Settings: a 64-bit nanosecond epoch does not survive an IEEE
  double, and passing it as a JSON number silently disables the setting. A
  binding that accepts a native integer MUST stringify it exactly (Python `str(int)`,
  JS `BigInt.toString()`), never through a float. A binding that additionally
  accepts a native **boolean** MUST encode it as `"1"` / `"0"` — the spelling
  the server's own settings parser treats as canonical — never as the
  language's `True`/`false` text (documented 2026-08-26; the reference Python
  binding's `encode_settings` is the model: `bool` → `"1"`/`"0"`, `int` →
  `str(int)`, `float` refused loudly).

## Result types

### `RowResult`

| Field | Type | Meaning |
|---|---|---|
| `Values` | `Value[]` | the coerced stored row, positional, **excluding** `src == "skipped"` columns |
| `Transformed` | `Transform[]` | every silent change — see below, and it is not optional |
| `Outcome` | enum | `Accepted` \| `Rejected` \| `AcceptedPoisoned` \| `Unsupported` \| `Skipped` (2026-08-27: only ever seen on rows inside a batch — the row was dropped under `input_format_allow_errors_*` and the batch continued; `ErrCode`/`ErrMsg` carry the caught error verbatim, `Values` is empty. Never a batch verdict, and never returned by the single-row call) |
| `ErrCode` | int | ClickHouse code when rejected; `691` when poisoned. **Measured 2026-08-17 (25.8, 26.7): a row-level `unsupported` outcome carries `code: 0`** — the sentinel is the `Outcome` itself; `-2` appears only on the compile / engine / TTL error paths, where the ABI really returns it. Pass the document's code through verbatim; never synthesize `-2` at row level. |
| `ErrMsg` | string | ClickHouse's message |
| `UnknownFields` | string[] | input fields with no matching column |
| `UnsupportedSettings` | string[] | non-empty ⇒ the answer MUST NOT be scored as agreement |
| `Substituted` | `Substitution[]` | columns whose **volatile** DEFAULT this library resolved itself |
| `Computed` | `Computed[]` | `MATERIALIZED` values — durable, but not part of `SELECT *` |

`Value` is `{Column, Text, Null, Source}`, where `Text` is ClickHouse's own JSON
rendering of the stored value and `Source` is the `src` string from the result
document. `Null` is true only for a genuine stored null — a poisoned column is
`Null == false` with an empty `Text`, because there is no value, not a null one.

`Substitution` is `{Column, Expr, Text}`. `Computed` is `{Column, Kind, Text}`.

A binding MUST promote a row whose `unsupported_settings` list is non-empty to
`Unsupported` unless it was already `Rejected`. The reference does this in
`rowResultOf`; skipping it turns a declined setting into a scored answer.

**An `outcome` string the binding does not recognize MUST map to
`Unsupported`, never to `Rejected`** (rule added 2026-08-26; all four
reference bindings previously defaulted the unknown arm to `Rejected`). A
future artifact's new verdict is by definition an answer this binding cannot
interpret: `Unsupported` is the arm that is never scored as agreement and
sends the caller to the server, while a default of `Rejected` manufactures an
over-reject — the zero-budget failure — out of pure vocabulary drift. The
five known spellings (`accepted`, `rejected`, `accepted_poisoned`,
`unsupported`, `skipped`) map exactly; only genuinely unknown text degrades.
The rule is also what made adding `skipped` (2026-08-27) additive-safe with
no `CHS_ABI_REVISION` bump: a binding that predates the spelling degrades a
skipped row to `Unsupported` — a decline, never scored as agreement, never a
manufactured rejection — which is the designed safe path for vocabulary
growth.

### `BatchResult`

| Field | Type | Meaning |
|---|---|---|
| `Rows` | `RowResult[]` | one per row the reader consumed, in input order — skipped rows included as `Skipped` entries (2026-08-27), so for an accepted batch `RowsRead == len(Rows)`. A binding rendering survivors MUST filter on the row outcome (the reference `asJSONEachRow` and the conformance oracle both do) |
| `Outcome`, `ErrCode`, `ErrMsg` | | the batch verdict |
| `RowsRead` | int | rows consumed, failed ones included |
| `RowsSkipped` | int | rows dropped under `input_format_allow_errors_*` |
| `Transformed` | `Transform[]` | **every** transform in the batch, each tagged with its `Row` index |
| `EngineRows` | raw JSON object[] \| null | the stored preview after the engine's insert-time merge |

Two obligations that are easy to miss and both were paid for in this repo:

1. **`Transform.Row` must be filled in.** A batch of ten rows with one bad value
   is useless to a tenant without the index. The reference sets it while folding
   per-row transforms into the batch.
2. **`storage_transforms` must be folded into `Transformed`.** A TTL-expired row
   is `accepted` at the row level and *not stored* at the batch level. A binding
   that reports per-row outcomes and drops `storage_transforms` will tell a
   tenant a row was stored that the table deletes at merge time.

And one that is easy to get backwards: when `EngineRows` is present it is the
stored truth, not `Rows`. It can be shorter (a `SummingMergeTree` dropping an
all-zero row) or reordered (the block is sorted by the sorting key first).

### `ref_unclassified` — the conservative-degradation flag (added 2026-08-17)

An additive per-column result-document field, **absent unless true**:
`"ref_unclassified": true` marks a leaf that is numeric/temporal by the
artifact's own type identity (`isValueRepresentedByNumber`) yet had no
reference-ladder entry, so the precise detector never ran for it. A binding's
classifier MUST then upgrade a would-be `reformat` on that column to
`value_changed` (lossy) — when the precise detector is bypassed, a visible
change cannot be vouched non-lossy, and the conservative direction is to
claim loss rather than hide one. On every current artifact the flag fires for
zero families; it exists so a FUTURE upstream family that slips past the
build-time ladder gate degrades to noise, never to silence. A binding that
ignores the field silently under-reports lossy changes on exactly the
families nobody has audited yet.

### `Transformed` is a core product guarantee

ClickHouse has no notion of "I changed your value": `readIntText` wraps mod
2^N, `SerializationDateTime` truncates, `parseUUID` maps every non-hex byte to
`0xff`, and all of it returns success. `Transformed` is the one part of chtypes
that is *derived* rather than executed by ClickHouse, and it is the reason the
product exists: it is what lets a gateway warn a tenant before a row ships to
subscribers. **It is not optional, and a binding that omits it scores 0 %
recall on the arbiter's transformation axis regardless of how well it does on
everything else.**

`Transform` is `{Column, Input, Stored, Reason, Row}` plus a `Lossy()`
predicate. The reason strings are stable — the harness groups on them — and a
binding MUST use exactly these spellings:

```
overflow_wrap      null_to_default   null_loss        decimal_truncate
date_clamp         datetime_wrap     date_shift       uuid_mangle
ip_mangle          float_precision   lossy_numeric    fixedstring_pad
emptied            element_changed   enum_coerce      value_changed
poisoned           duplicate_key_dropped
reformat           default_filled    zero_filled      default_materialised
ttl_expired        ttl_column_expired
```

`Lossy()` is **false** for exactly four of them — `reformat`,
`default_filled`, `zero_filled`, `default_materialised` — and true for
everything else. All of them are still *reported*: a preview must show the
tenant what the table will actually hold, and `1700000000` becoming
`"2023-11-14 22:13:20"` is a visible change even though nothing was lost. Only
the lossy ones are a warning.

**How a binding may implement it.** Two independent detectors run and their
union is reported (`go/chtypes/transform.go`):

1. *Supplied vs stored* — compare the raw input text against ClickHouse's
   rendering of what it kept. Models nothing; catches changes a widened type
   makes identically (a calendar roll-over, `2024-02-30 → 2024-03-01`) and types
   with no wider type to compare against (`Float64`, `Int256`, `String`).
2. *Reference type* — the C layer already parsed each field a second time
   through a structurally identical type with widened leaves and reported it as
   `ref` / `ref_type`. This one names the *reason* precisely (an overflow wrap
   versus a decimal truncation) and still fires where the supplied text is not
   comparable (a CSV field, a base64 blob).
3. *Wire round trip* — for a **text format**, where detector 1 goes silent
   because the field is not a JSON value and detector 2 can be blind too
   (`Array(String)` has no wider leaf, and a `DateTime` spelling survives the
   widening to `DateTime64`), the C layer emits `wire`: the stored value
   written back out by **ClickHouse's own serializer for that field's own text
   vocabulary**. `wire != input` is a change ClickHouse made, established by
   comparing the format's reader against the format's writer — nothing is
   modeled. Text alone cannot separate a re-spelling from a loss, so a
   difference claims the lossy reason and lets detector 2 override it.
   Without this, TSV `[NULL]` into `Array(String)` stored `[""]` and TSV
   `2026/01/15 10:30:00` into `DateTime` stored `2026-01-15 10:30:00`, and
   neither was reported (measured: 65 acceptance cases per version, 2026-08-19).

   **`wire` is emitted only where `input` is in the SAME vocabulary as the
   writer's output.** In the C ABI today that is `TSV`/`TabSeparated` alone:
   its `input` is the field's bytes verbatim and `serializeTextEscaped` writes
   in exactly that escaping. A CSV field's `input` has already had its quotes
   stripped by the row splitter, so comparing it against `serializeTextCSV`
   would report a difference that is the *splitter's*, not the server's. A
   binding must not synthesize `wire` when the C layer did not send it.

Prefer the reference detector's reason, but **never let it downgrade a lossy
finding to `reformat`**. Neither detector reimplements a coercion rule: the
first compares two values already in hand, the second compares two ClickHouse
answers. Numeric comparison MUST be exact (big rational / arbitrary-precision
decimal), and container comparison MUST recurse — `[{"a": 1}]` stored as
`[{"a": "1"}]` is the same data with a different wire type, which a subscriber
reading the preview would see and the table would not have.

> **"Is this supplied text one JSON value?" is a spec rule, not a language
> default (measured 2026-08-17, TS-vs-Go differential, 43 cases).** For
> positional-format fields the detectors must first decide whether the
> supplied bytes parse as a single JSON value, and the SDKs' native parsers
> disagree at the edges: Go's streaming parser accepts a numeric *prefix*
> (`1.2.3.4` reads as `1.2` — wrong: the field is not a JSON value), while a
> `trim()`-based approach may treat U+FEFF as whitespace (a BOM-prefixed
> number is NOT a bare JSON value — Go is right there). The rule: the text
> is a JSON value only if a strict parse consumes **all** of it, with
> whitespace being exactly JSON's four (space, tab, LF, CR) — no prefixes,
> no Unicode-whitespace extensions. These 43 cases were score-invisible when
> the rule was written; detector 3 above is what a text field falls through
> to once the strict parse refuses it, so the rule now decides which detector
> runs rather than whether any does.

### Constants are not payloads

Everything above is **insert-side** coercion: what ClickHouse does to a value on
its way into a column, which is why `256` into `UInt8` is stored as `0` and
reported as `overflow_wrap` rather than refused. A comparison constant in a
`WHERE` clause is a different question with different rules — depending on the
type pair, the operator and the version, the server may **error** on the
out-of-range literal, **promote** the comparison to a wider type and answer it
honestly (`x = 256` over a `UInt8` column being false for every row), or
**wrap** it the way an insert would. Only the third matches what this library
computes, so a caller that folds a predicate constant through `chs_row` /
`chs_rows` — or caches one it derived that way — is reusing the answer to a
question it did not ask: `WHERE x = 256` rewritten to `WHERE x = 0` silently
matches every row that legitimately holds zero, and returns them as if the
tenant had asked for them. A binding or gateway that canonicalises comparison
constants MUST therefore **refuse an operand outside the column type's domain**
rather than reuse the insert coercion: check the literal against the domain
first, and where it does not fit, decline the predicate or forward it unfolded
to the server, which owns the comparison rules. This library models the storage
path only, and no rig can catch a mistake here — they drive format streams, so
every verdict they score is insert-side.

## Discovery — declare the server's own profile (added 2026-08-18)

This library NEVER talks to ClickHouse. It ships, in every SDK, the three
canonical queries a caller runs at connect time with whatever client it
already has, plus typed parsers for their results — so a gateway discovers a
deployment's identity from the deployment itself and never asks the customer:

| query constant | SQL (each SDK carries it verbatim, `FORMAT JSONEachRow` included) | feeds |
|---|---|---|
| `QueryServerVersion` | `SELECT version() AS version` | `Registry.For(version)` |
| `QueryChangedSettings` | `SELECT name, value FROM system.settings WHERE changed` | the compile-with-settings profile AND per-call settings |
| `QueryTableColumns` | `SELECT name, type, default_kind, default_expression, position FROM system.columns WHERE database = {db:String} AND table = {table:String} ORDER BY position` | the DDL-reconstruction helper → compile |

The connect-time pattern, normative for a consumer:

1. Run `QueryServerVersion` + `QueryChangedSettings` once per
   connection/tenant; parse into the SDK's `ServerProfile` (version string +
   `map[string]string` of changed settings); **cache per deployment**.
2. Resolve the artifact: `registry.For(profile.Version)` (minor line or exact
   patch both resolve — §Version selection).
3. Compile every schema with the profile:
   `lib.CompileDDL(ddl, WithCompileSettings(profile.Settings))` (per-SDK
   spelling above).
   The settings gate answers the server's own 115 for anything the profile
   spells that this version's server would refuse — a typo is caught at
   declare time, not swallowed.
4. Pass the same profile (plus per-INSERT overrides) as the per-call settings
   on every `rows` call.

Two obligations on the parsers, both paid for elsewhere in this spec: numeric
fields (`position`) must survive both quoted and bare spellings (stock HTTP
output quotes 64-bit integers) without ever routing through a float, and
`default_kind`/`default_expression` MUST be carried — a reconstruction that
drops them silently loses DEFAULT/MATERIALIZED/EPHEMERAL semantics. A third,
made explicit 2026-08-26: should the changed-settings body ever carry one
`name` twice (a caller-joined profile, a modified query), the parser MUST
resolve duplicates **last-write-wins** — the later row replaces the earlier.
All four reference parsers already do this (a map insert), and each asserts
it; the rule exists because two SDKs resolving duplicates differently would
discover two different profiles from one server answer, which is a divergence
no rig would ever see. The
DDL-reconstruction helper is a spelling exercise only (identifiers backticked
where needed, types and expressions passed through verbatim); the library's
own compile is the judge of the result. `system.columns` reports the table AS
STORED — flattened columns under `flatten_nested=1`, the single Nested column
under `0` — so reconstruction is shape-faithful exactly when the discovered
profile is declared to the compile, which is the whole pattern.

Declaring the profile at compile also settles the **type gates** the way a real
server settles them (added 2026-08-19). `QueryChangedSettings` returns the
gates the deployment actually changed; a gate named in the compile profile is
checked once, at compile — a refusing value fails the compile with the server's
own `455`/`44`, exactly as that server's `CREATE` would — and thereafter the
handle outranks the per-call map for that name, so step 4's per-call settings
cannot fabricate a rejection for a table the deployment legitimately holds. A
caller that skips step 3 and passes the gates per call only still gets the old,
per-row behavior, which is correct but stricter than the server.

## Concurrency — what a binding owes the ABI (added 2026-08-26)

`docs/reference/c-abi.md` §Thread-safety states three rules. This section says what each
one costs a binding, because three of the four reference bindings had answered
one of them differently and none of them had said so.

1. **`chs_row` / `chs_rows` are safe together on DISTINCT handles.** A binding
   MAY serialize them anyway; a binding that does not MUST hold rule 2.
2. **One `chs_schema *` MUST NOT be used from two threads at once.** This is a
   per-handle lock, not a per-library one, and a binding that lets row calls run
   concurrently owes it.
3. **`chs_init` and `chs_set_default_settings` MUST be serialized against all
   other calls.** `chs_set_default_settings` replaces a process-global that the
   row path reads **by reference**; replacing it under a reader is a
   use-after-free, not a stale read. **A binding that exposes it on a
   `dlopen`'d library MUST exclude it against every other call on that
   library**, and the exclusion MUST be keyed on the loaded IMAGE, not on the
   wrapper object: `dlopen` refcounts one image per file, so two wrappers over
   one artifact share the C globals and two separate locks would exclude
   nothing.

How the reference bindings satisfy this, and it is deliberately not the same
shape in each:

| SDK | rules 1 + 2 | rule 3 |
|---|---|---|
| Go | `Library.mu` (`RWMutex`) read-held per call; `LoadedSchema.mu` per handle | vacuous on the `dlopen`'d path — the function-pointer table does not `dlsym` `chs_set_default_settings` at all, so a `Library` cannot make the call. The static path guards it with `defaultSettingsMu` |
| Python | `_native._RWLock` per loaded image, read-held per call; `Schema._mu` per handle | the same lock, **write**-held by `set_default_settings` and `close` |
| Rust | one `Mutex` per `Library`, held for every call | the same mutex — `set_default_settings` takes `self.lock()` |
| TypeScript | Node's event loop: every `chs_*` call in the binding is synchronous, nothing awaits, and the library never calls back into JS | **exposure is nil**, and a reentrancy counter (`NativeLibrary#inCall`) is the proof rather than the assumption: `setDefaultSettings` and `shutdown` refuse loudly if a library call is on the stack below them |

**The TypeScript exemption has a boundary, and it is `worker_threads`.** Two JS
threads in one process share one `dlopen`'d image and one set of C globals, and
a per-isolate counter cannot see across them. A binding whose calls are
synchronous on a single thread satisfies rule 3 *for that thread*; it does not
make a multi-worker setup safe, and this spec does not claim it does. A
TypeScript caller that fans work across workers MUST seed the settings before
starting them, or serialize the seed itself.

A binding that moves from "serialize everything" to "concurrent readers" is
making a claim about its own allocator interactions and MUST prove it by
running the rigs, not by reasoning (`docs/reference/c-abi.md` §Thread-safety).

## Version selection

Two spellings resolve, and both MUST:

- a **minor line** — `"25.8"`, which is what the rigs and callers ask in;
- an **exact patch** — `"25.8.28.1-lts"`, which is what a library reports about
  itself.

Rules:

1. A `Library` MUST name itself by calling `chs_clickhouse_version()`. Nothing
   is inferred from the directory or file name.
2. `Minor` is the first two dot-separated components of the reported version:
   `"25.8.28.1-lts" → "25.8"`, `"25.10.7.6-stable" → "25.10"`. Note `25.10` is a
   *later* minor than `25.8`; string comparison of minor lines is meaningless and
   MUST NOT be used for ordering. **Every ordered surface a binding exposes —
   `Versions()`, a `libraries()` list, the not-found error's "have […]" text —
   uses this numeric release order**, never directory or lexical order
   (restated 2026-08-26, after two SDKs' `libraries()` lists were found in
   directory order: `25.10` sorting before `25.8`).
3. A registry indexes each library under **both** its exact version and its
   minor line. Lookup tries the given string first, then its minor line — so
   asking for `"25.8.30.16"` when the loaded artifact is `25.8.28.1-lts` resolves
   to the `25.8` line. This is deliberate: docker tags drift, and an
   exact-match-only lookup silently loses a whole version column. `PROTOCOL.md`
   spells out the trap: "an exact-match lookup against `25.8.28.1` will not find
   `25.8` and silently costs the whole column."
4. Resolution failure MUST be an error naming the versions that *are* loaded,
   never a fallback to the nearest one. Answering 26.7 semantics from a 25.8
   artifact is a lie, and the rigs score silent wrongness hardest.
5. An empty version string means "whatever this build is" on the statically
   linked path, and MUST NOT mean "pick one" on the registry path.

## Conformance

### Level 1 — ABI conformance (offline, minutes)

Load an artifact directory and assert, on outputs:

- The library file name came from `manifest.json`'s `library` field.
- `chs_clickhouse_version()` matches the manifest's `clickhouse_version`.
- `chs_init` returned 0, was called once, and was passed `UTC` plus the contents
  of `unsafe_families.txt` (empty is a valid list, not a missing file).
- Every returned string was freed with the *same library's* `chs_free`; run under
  a leak checker and assert zero growth over a few thousand `chs_rows` calls.
- A symbol the artifact does not export degrades to `unsupported`, and does not
  fail the load.
- `chs_shutdown` is called before `dlclose`, and the process exits rather than
  hanging.

### Level 2 — surface conformance

Reproduce the worked examples below and the `bindings.md` result-type
obligations. This is a unit-test suite, not a rig.

### Level 3 — semantic conformance (the only one that means correctness)

Drive the **JSONL subprocess-oracle protocol**: the base protocol in
`tests/fuzz/README.md` plus the nine extensions in `tests/arbiter/PROTOCOL.md`.
`chtypes-core/tests/conformance/go/cmd/chtypes-oracle` is the reference driver and
`tests/fuzz/stub_oracle.py` is the minimal one.

One JSON object per line on stdin, one per line on stdout, **in order**:

```jsonc
// in
{"id":"fz-…","mode":"value","schema":"x UInt8","rows":[…markers-v0…],
 "settings":{"input_format_null_as_default":"0"},"input_format":"CSV",
 "payload_hex":"01","engine":"SummingMergeTree","order_by":"(day, key)",
 "ttl":"ts + INTERVAL 30 DAY"}

// out, accepted
{"id":"fz-…","status":"ok","value":"[{\"x\": 0}]","transformed":[…],"poisoned":false}
// out, rejected
{"id":"fz-…","status":"error","code":27,"msg":"…"}
// out, declined
{"id":"fz-…","status":"error","unsupported":true,"scope":"why"}
```

Non-negotiable protocol rules:

1. **One line out per line in, in order.** A reply whose `id` does not match the
   request is scored `crash` and the process is restarted.
2. **Never exit on a bad case.** A case you cannot handle is
   `{"status":"error"}`, not a panic. For an ingest validator a panic is the
   request going down with the row, so a death costs coverage and is called out
   by name.
3. Unknown request fields are ignored; unknown reply fields are preserved but
   not interpreted.
4. Answer the `{"id":"__caps__","mode":"caps"}` handshake with **exactly one**
   line, or the stream desynchronises for every case after it.
5. **Declare only formats you actually parse.** `caps.formats` is load-bearing
   for fairness: the arbiter does not send a case in a format you did not
   declare, and records it `not_offered` — missing coverage, never a divergence.
   Declaring a format you cannot parse converts neutral cases into scored wrong
   answers. Probe the loaded artifact at startup rather than trusting the source
   tree: the reference driver feeds one byte into `x UInt8` as `RowBinary` and
   only declares the RowBinary family if the artifact accepts it.
6. `payload_hex`, when present, **replaces `rows` entirely** — feed exactly
   those bytes to the parser.
7. `value` is the canonical row-list form: a JSON list of row objects, keys
   sorted, integral floats collapsed, compared after re-parsing (whitespace and
   key order irrelevant, row order compared as a multiset). **`0` versus `"0"`
   does matter.** Emit ClickHouse's own numeric text verbatim — never round-trip
   an integer through a float; `18446744073709551615` becomes
   `18446744073709552000` and an `Int256` becomes `-5.78960446186581e+76`, which
   a scorer cannot distinguish from a real coercion defect.
8. `transformed` may be a list of column names or the richer
   `[{"column","input","stored","reason"}]` form; the arbiter reduces the latter
   to column names. Omitting it scores 0 % recall.
9. **Emit `computed` and `constraint_violated`, and declare both**
   (extension 8, added 2026-08-17). `computed` is
   `[{"name","kind","stored","row"}]` — the `MATERIALIZED` values from
   `RowResult.Computed`, with `stored` carried as ClickHouse's own JSON *text*
   for rule 7's reason and `row` the 0-based index into the batch, as
   `transformed` does. They can never ride in `value`, because `SELECT *` does
   not return them, so a driver that omits the field leaves a wrong
   MATERIALIZED value scoring as exact agreement. `constraint_violated` is
   `true` when the batch was rejected with ClickHouse code 469
   (`VIOLATED_CONSTRAINT`) — the implementation's own attribution of the
   rejection to a table-level `CONSTRAINT … CHECK`. Declare both in the
   handshake (`caps.computed`, `caps.constraints`): as with every capability,
   silence costs coverage (`not_declared`) and is never scored as a wrong
   answer, but a driver that stays silent leaves those two axes unmeasured for
   its SDK.
10. **Decline volatile-DEFAULT cases by default.** A row whose value came from
   this library's own clock cannot be scored against a *recorded* truth: the
   truth file holds the instant the arbiter's server stamped, yours is the
   instant you read, and pinning yours to theirs would be gaming the harness
   rather than measuring it. The reference driver's default is
   `-volatile=decline`, which answers `unsupported` naming the substituted
   columns; `-volatile=answer` is for a harness that echoes the substituted
   values back as the INSERT payload (which is what a gateway does, and what
   makes preview == stored). A binding SHOULD offer the same switch and SHOULD
   default to declining.
11. **Compile with the case's compile-time settings, and declare it**
   (extension 9, added 2026-08-18). The truth rigs apply a case's settings to
   the server's CREATE as well as its INSERT, so a case declaring
   `flatten_nested: 0` has its ground truth recorded against an unflattened
   table. A driver whose artifact exports `chs_schema_compile` MUST
   compile under the compile-relevant subset of the case's settings (the
   normative list is PROTOCOL.md extension 9's table; this revision:
   `flatten_nested`) and declare `caps.compile_settings`; one that cannot —
   an implementation with no compile-time settings channel at all, such as
   the `stub` control — MUST stay silent, and the arbiter then withholds
   those cases as `not_offered` instead of scoring answers about a
   differently-shaped table. Declaring without compiling-with-settings
   converts withheld cases into scored wrong answers, the same trap as
   declaring a format you cannot parse.
12. **On `chs_schema_engine`, the SIGN of the return decides the KIND of
   error** (added 2026-08-25; before this, bindings keyed on the literal
   `115`). Two answers travel down one integer and a binding MUST NOT flatten
   them:
   * **`rc > 0` — the SERVER refused.** A real ClickHouse error code out of
     the server's own engine validation: this DDL can never exist, no retry
     and no different data will change that, and the tenant has to be told.
     A binding MUST surface it as **the same error type it uses for a server
     refusal on any other schema call** — `SchemaError` (Go, Python, TS),
     `Error::Schema` (Rust) — carrying the server's own code *and* the
     server's own message, passed through verbatim. Today `115` (unknown
     MergeTree setting name) is the only positive code the ABI returns here;
     key on the sign anyway, so a code a later era adds cannot silently be
     demoted. (The `Maybe you meant …` hint appears on the `DB::Settings`
     channels — a compile profile, a per-call map — not on the MergeTree
     namespace, which upstream decorates with `for storage <engine>` instead.
     A binding passes through whatever it is given and invents neither.)
   * **`rc < 0` — this LIBRARY declined.** `-2` "a real server might well
     have accepted this; I will not guess", `-1` a guarded exception, and a
     binding's own missing-symbol sentinel. It MUST surface as a **DISTINCT
     ERROR TYPE**, not as the refusal type carrying a sentinel code —
     `UnsupportedError` (Go, Python, TS), `Error::Unsupported` /
     `Error::PredatesFeature` (Rust) — and a caller must validate cautiously
     rather than blame the tenant.

     **The decline type is a PEER of the refusal type, not a subtype**
     (Go and TS, 2026-08-26; Python completed the same day; Rust always was).
     A binding MUST NOT let a decline satisfy
     `errors.As(&SchemaError{})` / `instanceof SchemaError` /
     `except SchemaError`, because a caller who handles only the refusal arm
     would then convert every decline into a rejection SILENTLY — a
     manufactured over-reject, zero-budget. As a peer, the same omission
     produces an unhandled error, which is loud. (A shared BASE type —
     Python's `ChtypesError`, TS's `ChtypesError` — is fine: catching the
     base is an explicit choice to handle both arms, which is not the
     forget-to-check failure this rule exists to prevent.) The refusal type
     MUST NOT carry an `.unsupported` predicate: the type IS the answer, and
     a predicate is the sentinel wearing a method. The wire sentinel stays
     exported (`CodeUnsupported` / `CODE_UNSUPPORTED`) because row-level
     results carry it, but no error VALUE carries it. (An earlier revision
     grandfathered Python's `UnsupportedError(SchemaError)` subtype; the
     split was completed in the 2026-08-26 SDK-fix cycle and no subtype
     remains.)

     The decline type's rendered message keeps the `[-2]` shape the refusal
     type renders its code with. That is a frozen rendering, not a field: the
     conformance drivers put it on the protocol wire verbatim as an
     `unsupported` scope, so changing the text moves rig records without
     changing a verdict. The rendered code is ALWAYS the header's
     `CHS_CODE_UNSUPPORTED` (`-2`), whatever negative integer the binding saw
     internally — `-1` (a guarded exception), `-2`, or a binding's own
     missing-symbol sentinel (Go's `dlopen` shim uses `-3` internally).
     Internal sentinels MUST NOT leak into the rendering.

     **Neither error type may carry a GUESSED column.** A binding attributes
     `column` only when the C layer's own structured answer names one — and
     today no schema-path entry point does (`chs_schema_compile`,
     `chs_schema_engine`, `chs_schema_ttl` and `chs_validate_type` return a
     code and a message, nothing more), so the field stays empty on those
     paths and a message that names a column rides through verbatim inside
     `msg`. The TS binding's measured guess-removal is the precedent
     (2026-08-26: zero attributed renders in the recorded runs, scope strings
     unaffected); the Go static path's longest-declared-name-in-the-message
     guess was removed in the same cycle's follow-up. The field itself stays,
     for callers — e.g. a gateway's EPHEMERAL decline, which names columns it
     KNOWS (§EPHEMERAL below).

   The two failure modes this rule exists to prevent are the same pair the
   whole product is budgeted at zero for: reporting a decline as a refusal is
   a manufactured over-reject, and hiding a refusal behind a decline lets a
   DDL that can never exist look merely unmodelled. `docs/reference/c-abi.md` §Error
   model is the normative source; this rule is its binding-side restatement.

Then run the rigs and quote the run, not your intent:

```
cd tests/acceptance && ./run.sh          # full; ./run.sh report re-judges in ~2 s
cd tests/arbiter    && ./run.sh rescore  # ~2 min
```

Never hand-edit a `RESULTS.md`. Its verdicts are computed from runs, and a
hand-edited number is indistinguishable from a lie.

## Worked examples

All six were produced on 2026-08-17 by
`chtypes-core/lib/build/chtypes-oracle --registry <registry> --version <v>` with JSONL on
stdin, against the **`darwin-arm64`** artifacts of that day — six versions
were loaded then (26.5 landed later and is absent from the version tables
below), and the artifacts have been relinked several times since (2026-08-25
×2, 2026-08-26, 2026-08-27, and the 2026-08-31 revision-3 cycle). Outputs are
verbatim from that capture; re-capturing at seven versions on a current
artifact set remains the standing follow-up.

> **Platform caveat, stated because it changes answers.** macOS's `long double`
> is 53-bit, so *float parses* diverge from a real server (Linux matches
> 395/395 of the float corpus; macOS 0/395). None of the six examples below is a
> float-parse boundary case, but any float expectation a binding writes into a
> test MUST come from a Linux artifact or a live server.

### 1. Overflow wrap — the canonical silent change

```jsonc
// in
{"id":"ex-overflow","schema":"x UInt8","rows":[{"x":256}],"input_format":"JSONEachRow"}
// out
{"id":"ex-overflow","status":"ok","value":"[{\"x\": 0}]","body_b64":"eyJ4IjowfQo=",
 "transformed":[{"column":"x","input":"256","stored":"0","reason":"overflow_wrap","row":0}]}
```

`256` into `UInt8` is stored as `0` and ClickHouse returns success. The insert
would be accepted; the value would be wrong; nothing in ClickHouse says so.
`transformed` is the entire product in one line.

### 2. DDL in, canonical schema out — canonicalisation is schema-aware

Compiled through the C ABI (`chs_schema_compile` + the column-introspection
group) on the `25.8` artifact:

```
in:  a UInt8, b Nullable(String) DEFAULT 'x', c DateTime MATERIALIZED now()
out: a  UInt8              kind=""             expr=""       literal=false
     b  Nullable(String)   kind="DEFAULT"      expr="'x'"    literal=true
     c  DateTime           kind="MATERIALIZED"  expr="now()"  literal=false

in:  x Int64 DEFAULT NULL
out: x  Nullable(Int64)    kind="DEFAULT"      expr="NULL"   literal=true
```

The second is why `validate_type` alone is insufficient: the DEFAULT rewrote the
declared type. Note also the canonical spelling of parameter lists —
`Decimal(18, 4)`, `Enum8('a' = 1, 'b' = 2)`, `Map(String, Array(UInt8))`, with a
space after each comma — and that `Variant(UInt8, String)` canonicalises to
`Variant(String, UInt8)` with members **sorted**. A binding MUST pass the
library's spelling through verbatim.

### 3. A volatile DEFAULT, pinned — the one impurity, as an input

```jsonc
// in  (note: the setting value is a STRING; as a JSON number it is silently ignored)
{"id":"pin","schema":"a UInt8, ts DateTime DEFAULT now()","rows":[{"a":1}],
 "input_format":"JSONEachRow",
 "settings":{"chtypes_now_epoch_nanos":"1700000000000000000"}}
// out  (driver run with -volatile=answer)
{"id":"pin","status":"ok","value":"[{\"a\": 1, \"ts\": \"2023-11-14 22:13:20\"}]",
 "transformed":[{"column":"ts","input":"","stored":"\"2023-11-14 22:13:20\"",
                 "reason":"default_materialised","row":0}]}
```

The library resolved `now()` itself, once for the whole batch, and reported it as
a substitution. **The caller MUST send `ts` as an explicit column in the
INSERT.** That is the mechanism, not a nicety: if the server evaluates the
expression instead, preview and stored differ *always* at `now64` resolution
(2–60 ms apart even back to back) and sometimes at `now()` resolution, because
ClickHouse reads the clock once per *block* — a 200-row insert at
`max_insert_block_size=10` stamps 20 distinct `now64(9)` values.

Supplying the column also **disarms** whatever was attached to the expression —
measured, `x Int64 DEFAULT throwIf(1,'boom')` rejects with 395 when absent and is
*accepted* when supplied — which is why the substitution is reported as a
transform rather than passed over in silence.

And with a skew budget that the offset exceeds, the honest answer is a decline:

```jsonc
// in
{"settings":{"chtypes_clock_offset_nanos":"5000000000","chtypes_max_clock_skew_nanos":"1"}}
// out
{"status":"error","unsupported":true,
 "scope":"client clock offset exceeds chtypes_max_clock_skew_nanos; refusing to
          substitute a volatile DEFAULT rather than store a timestamp the server
          would not have written"}
```

### 4. A TTL-expired row — accepted per row, not stored per batch

```jsonc
// in
{"id":"ex-ttl","schema":"ts DateTime, v UInt8","order_by":"ts",
 "ttl":"ts + INTERVAL 1 DAY","rows":[{"ts":"2020-01-01 00:00:00","v":9}],
 "input_format":"JSONEachRow"}
// out
{"id":"ex-ttl","status":"ok","value":"[]","body_b64":"Cg==",
 "transformed":[{"column":"","input":"","stored":"","reason":"ttl_expired","row":0}]}
```

The row's own document says `accepted`; the batch says the part holds nothing.
Raw batch document for the same case:

```json
{"outcome":"accepted","code":0,"err":"","engine_rows":[],
 "storage_transforms":[{"row":0,"column":"","reason":"ttl_expired"}],
 "rows_read":1,"rows_skipped":0,"rows":[ … the row, accepted … ]}
```

A binding that reads only `rows` previews a row the table will silently delete.

### 5. Version behavior is non-monotonic — the registry is the point

Same case, all six artifacts then loaded (the registry holds seven today;
26.5 is not in this capture), one process:

```jsonc
// in
{"id":"ifmixed","schema":"a UInt8, x Int64 DEFAULT if(1,2,'a')","rows":[{"a":1}],
 "input_format":"JSONEachRow"}
```

| Version | Answer |
|---|---|
| 24.8 | `ok`, `[{"a": 1, "x": 2}]`, `default_filled` |
| 25.3 | `ok`, same |
| 25.8 | `ok`, same |
| **25.10** | **`error`, code `386`** — "There is no supertype for types UInt8, String … default expression and column type are incompatible." |
| 26.6 | `ok`, same |
| 26.7 | `ok`, same |

Newer is **not** always more permissive. A binding MUST NOT infer one version's
answer from another's, and MUST NOT treat a version it has no artifact for as
"probably like the nearest one". Two more from the same sweep: `j JSON` is
rejected on 24.8 with code `44` (`allow_experimental_json_type`) and accepted
from 25.3 onward; `RowBinaryWithNamesAndTypesAndDefaults` answers code `73`
`Unknown format` on 24.8 through 25.10 and parses on 26.6 / 26.7 — exactly as
those servers do. (26.5 was not in this capture and is unmeasured here.)

### 6. Accept-then-poison — an accepted insert that destroys the value

```jsonc
// in
{"id":"enum-poison","schema":"e Enum8('a'=1,'b'=2)","rows":[{"e":null}],
 "input_format":"JSONEachRow",
 "settings":{"input_format_defaults_for_omitted_fields":"0"}}
// out  (identical on all six versions in the capture)
{"id":"enum-poison","status":"ok","value":"[{\"e\": null}]","poisoned":true,
 "transformed":[{"column":"e","input":"null","stored":"<unreadable>","reason":"poisoned","row":0}]}
```

The insert returns rc=0 and every later `SELECT` fails with code 691 — still
present on 26.6 and 26.7. In the result document this is
`"outcome":"accepted_poisoned"`, `"code":691`, with the column carrying
`"poison":true`. A binding MUST report it as **accepted** with the poison flag,
never as a rejection: the contract's definition and the arbiter's ground truth
both come from a real `CREATE`/`INSERT`/`SELECT` cycle, and `format()` fusing it
into a rejection is an artifact of that probe rather than the semantics.

## Reproducing these

```bash
# read-only; requires an artifact registry ($CHTYPES_REGISTRY)
printf '%s\n' '{"id":"ex-overflow","schema":"x UInt8","rows":[{"x":256}],"input_format":"JSONEachRow"}' \
  | chtypes-core/lib/build/chtypes-oracle --registry <registry> --version 25.8
```

`chtypes-core/tests/conformance/go/cmd/chtypes-oracle/main.go` is the input-format reference;
`chtypes-core/tests/conformance/go/cmd/chtypes-oracle/markers.go` is the `markers-v0` encoder
(`{"$jsonraw":…}` splices verbatim text, `{"$b64":…}` splices raw bytes,
`{"$missing":true}` omits the key, `{"$rowraw":…}` replaces the whole row) and
is about 200 lines — port it, do not approximate it. Key order and duplicate
keys are preserved deliberately, because a duplicate-key row is a real test
case.

## Two things the oracle protocol does not carry

> **Both were closed on 2026-08-17 by `tests/arbiter/PROTOCOL.md` extension 8**,
> which adds `computed` and `constraint_violated` to the reply and
> `caps.computed` / `caps.constraints` to the handshake. The obligation on a
> driver is rule 9 above. The paragraphs below are kept as the statement of the
> gap that motivated it, and each carries what actually remains.

Worth knowing before a binding is designed around the protocol rather than the
library:

- **`computed`.** `MATERIALIZED` values are in the library's `RowResult.Computed`
  and in the raw result document, but the oracle reply had no field for them, so
  they were not scored. A binding that only ever speaks the protocol will not
  exercise that path. *Now carried* — but scored only once a truth capture
  records the server's own readback of those columns, which
  `bake/truth.py` collects and which the committed truth files predate.
- **Engine, TTL and constraints as *ground truth*.** `engine`, `order_by` and
  `ttl` are request fields the reference driver honors, but the acceptance
  corpus notes that a divergence caused by a `CONSTRAINT` is something no
  implementation can currently be *asked* about — that is a protocol gap, not a
  library gap. *Now asked* for `CONSTRAINT … CHECK`, on the arbiter's
  `constraint_axis`. `engine` and `ttl` still have no axis of their own, and the
  acceptance rig still excludes engine/TTL divergences by control probe.

## `EPHEMERAL` columns: decline to preview, never mispreview

*Appended 2026-08-17. The measurement is [`c-abi.md`](c-abi.md) § EPHEMERAL —
confirmed on live 24.8.14.39 / 25.8.28.1 / 26.7.3.19, identical answers. This
section is the binding-surface obligation that follows from it, and it is the one
place where a correct answer from this library is the wrong answer for a caller.*

An `EPHEMERAL` column is reachable **only** through an INSERT that names it in an
explicit column list. `chs_row` / `chs_rows` take a *format stream*, a stream
carries no column list, so this API cannot express that shape at all. For
`id UInt32, e UInt8 EPHEMERAL, d UInt8 DEFAULT e + 1` the library answers exactly
as a real `INSERT INTO t FORMAT …` (no column list) does — `e` is an unknown field
in JSONEachRow, occupies no field position in CSV, and `d` is computed from `e`'s
type zero, so `d = 1`. Against the INSERT it models, that is right. Against
`INSERT INTO t (id, e)`, which stores `d = 6`, it is a **mispreview**: the row a
subscriber would be shown is not the row the table will hold.

A gateway that forwards INSERTs therefore MUST NOT present a `chs_row` /
`chs_rows` result as the preview of an INSERT whose column list names an
`EPHEMERAL` column. It MUST decline, and:

1. **Detect it at compile time, not per row.** `chs_schema_column_default_kind`
   returns `"EPHEMERAL"`, and such a column appears in `cols` with
   `src: "skipped"`. Whether the *statement* names one is known from the column
   list the gateway is already parsing to forward it. The decline condition is the
   intersection of those two facts, and it costs nothing per row.
2. **Scope the decline to that intersection.** A schema containing an `EPHEMERAL`
   column is fully previewable for INSERTs that do **not** name it — that is the
   no-column-list shape, where library and server agree on all three measured
   versions. Declining every schema with an ephemeral column would pay coverage
   for a risk that is not there.
3. **Report it as `unsupported`, naming the column(s)** — never as `accepted` with
   the computed row. `unsupported` is a first-class answer here for the usual
   reason: the decline is repeatable, and a guess that happens to be right is not.
   Forward the INSERT unpreviewed rather than previewing it wrongly.
4. **Do not synthesize the missing effect.** Feeding the supplied `e` into `d`
   yourself means reimplementing a coercion rule — the one thing this product does
   not do — and `d`'s expression can be arbitrary SQL over several ephemeral
   columns.
5. **Count the declines.** This is a decline that real traffic can hit, so it
   belongs in the `unsupported`-rate metric a consumer keeps
   (the core repository's `docs/production-plan.md` §9), not only in a
   log line.

This is not an over-accept or an over-reject in the rigs' sense, and no rig can
currently catch it: the rigs drive format streams, so every verdict they score
matches the storage path a tenant's data takes. The exposure is entirely on the
gateway's side of the boundary, which is why the rule lives here.
