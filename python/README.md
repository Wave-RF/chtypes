# chtypes — Python SDK

ClickHouse's own type system, per version, from Python: *if this row were
inserted into this table on this ClickHouse version, what would happen?* The
answer comes from ClickHouse's real C++ machinery, vendored per release into a
shared library behind the `chs_*` C ABI (`spec/c-abi.md`; pre-1.0 only the
names and the format integers are frozen — signatures can still change by
deliberate cycle, which is why the loader checks `chs_abi_revision` before
trusting them). Nothing here
reimplements a coercion rule. One peer SDK among `{go,python,ts,rust}`;
no language is privileged (`spec/bindings.md`).

Three outcomes travel through this API, and conflating any two is an error:

- **Rejected** — the server itself would refuse this row or DDL. Rows:
  `Outcome.REJECTED` with ClickHouse's own code. Schema calls: `SchemaError`
  with the server's own code and message (including the engine path's code 115
  for an unknown MergeTree setting name).
- **Unsupported** — this build declines to answer; a real server might well
  have accepted it. Rows: `Outcome.UNSUPPORTED`. Schema calls:
  `UnsupportedError`. The caller must fall back to the server (validate
  cautiously, forward unpreviewed), never tell the tenant they are wrong.
- **Accepted** — possibly with silent coercions, which is the product:
  `transformed` reports every change ClickHouse made without saying so,
  `substituted` the volatile DEFAULTs resolved here, `computed` the
  MATERIALIZED values.

## Install

```bash
uv add chtypes            # in a uv project
uv pip install chtypes    # in a bare venv
```

Pure Python — stdlib `ctypes`, zero dependencies, no build step. To work
against a checkout instead, install it editable:

```bash
uv add --editable /path/to/chtypes/python
uv pip install -e /path/to/chtypes/python
```

## Getting artifacts

The native code is a per-version prebuilt artifact this package `dlopen`s at
runtime; nothing here builds one. The package carries the fetch command every
SDK spells the same way ([`docs/fetch.md`](../docs/fetch.md)), so nothing else
from this repository is needed:

```sh
python -m chtypes fetch 25.8         # or `chtypes fetch 25.8` (console script), or --all
python -m chtypes verify             # re-hash every installed line against its manifest
python -m chtypes list               # what is installed, what the release offers
python -m chtypes where              # the registry directory fetch writes to
```

```python
import chtypes
chtypes.ensure("25.8")               # the same, from code: idempotent, returns the directory
```

A fetch is a **verification chain, not a download** — nothing is a verdict but
the chain: the release's `SHA256SUMS` must carry a valid ed25519 signature by
the embedded release key (pure-stdlib verifier; `CHTYPES_TRUSTED_KEYS=<hex>,…`
replaces the key list for a mirror, `CHTYPES_ALLOW_UNSIGNED=1` skips the check
with one loud warning); `index.json` must agree with the signed sums; the
tarball is hashed **before** it is unpacked; the manifest inside must agree
with the index; the installed library is hashed again in place. The install is
atomic (a verified temporary sibling renamed into place), an already-installed
line that hashes right is a no-op (`--force` re-downloads), `--offline` never
touches the network, and `--lock chtypes.lock` / `--frozen` pin what was
installed the way a package manager's lock file does. Exit codes: 0 ok · 1
verification failed · 2 usage · 3 source unreachable · 4 not published for
this platform/line. A refused release installs nothing:
`CHTYPES_ARTIFACT_UNTRUSTED`, `CHTYPES_ARTIFACT_CORRUPT`,
`CHTYPES_ARTIFACT_PINNED`, `CHTYPES_ARTIFACT_UNPUBLISHED`,
`CHTYPES_SOURCE_UNREACHABLE` — each a typed `ArtifactError` with that `.code`.
Sources: `CHTYPES_ARTIFACTS_URL` (default `https://artifacts.wavehouse.dev`) plus
`--tag` (default the rolling `artifacts`), or `--url <base>` for any host, a
`file://` URL or a plain directory.

**Where artifacts are looked for** — the registry search path, in order: the
path given to `Registry(...)`, `$CHTYPES_REGISTRY`, the per-user cache
`${XDG_CACHE_HOME:-~/.cache}/chtypes/artifacts/<os>-<arch>/`, then the reserved
system locations `/usr/local/share/chtypes/artifacts/<os>-<arch>` and
`/opt/chtypes/artifacts/<os>-<arch>`. A line is served from the **first**
directory that holds it; fetch writes to the first of the first three
(`chtypes.registry_search_path()`, `chtypes.fetch_destination()`). A line no
directory holds is **one** error, `ArtifactMissingError` (a `RegistryError`,
`.code == "CHTYPES_ARTIFACT_MISSING"`), with the message every SDK prints:

```
chtypes: no artifact for ClickHouse 25.8 (darwin-arm64). Looked in: /Users/me/.cache/chtypes/artifacts/darwin-arm64, /usr/local/share/chtypes/artifacts/darwin-arm64, /opt/chtypes/artifacts/darwin-arm64.
Install it:  python -m chtypes fetch 25.8
or set CHTYPES_AUTOFETCH=1 to fetch on first use.
```

**Lazy fetch on first open** is opt-in — `Registry(autofetch=True)` or
`CHTYPES_AUTOFETCH=1` — and runs `ensure` for a missing line before opening it,
once per process per line under one lock, so concurrent opens fetch once. Off
by default: a production process must not begin a 250 MB download inside a
request.

- **Layout**: a registry directory holds one directory per minor line
  — `25.8/manifest.json`, the shared library it names, `CH_VERSION`,
  `unsafe_families.txt`. `manifest.json`'s `library` field is the loader's only
  source of truth for the file name (Linux artifacts still ship the historical
  `libchtypes_s1.so`; nothing is inferred from file or directory names).
- **Loading is lazy, per line**: `Registry(...)` reads manifests and `dlopen`s
  nothing; `for_version` loads the one line asked for, once (`libraries()`
  loads every line). Once files are in a registry directory the loader trusts
  the directory — verification is a fetch-time policy, exactly as a runtime
  trusts `node_modules`; pass `Registry(..., verify_hashes=True)` to re-hash
  each line against its manifest at load.
- **Loader checks, in order**: dlopen `RTLD_NOW | RTLD_LOCAL` → mandatory
  symbols present → `chs_abi_revision()` equals `chtypes.ABI_REVISION` (a
  different nonzero revision is refused with `RegistryError`; `0` means the
  artifact predates the probe and per-symbol degradation applies) → the
  library's self-reported version matches the manifest → `chs_init(timezone,
  unsafe_families)` once per image.

## Quickstart

```python
from chtypes import Format, Outcome, Registry

registry = Registry()  # the search path: $CHTYPES_REGISTRY, the per-user cache, the system dirs
library = registry.for_version("25.8")  # minor line, or exact patch "25.8.28.1-lts"

# Compile under the deployment's declared settings profile (see Discovery).
with library.compile_ddl("ts DateTime, seq UInt8", settings={"flatten_nested": "1"}) as schema:
    good = schema.rows(Format.JSON_EACH_ROW, b'{"ts":"2026-01-15 10:30:00","seq":256}\n')
    bad = schema.rows(Format.JSON_EACH_ROW, b'{"ts": abc}\n')

good.outcome  # Outcome.ACCEPTED — the insert would succeed...
good.rows[0].value("seq").text  # '0'             — ...and silently store 0
good.transformed[0].reason  # 'overflow_wrap' — which is the product
bad.outcome, bad.err_code  # (Outcome.REJECTED, 27)
bad.err_msg  # ClickHouse's own parse error, verbatim
```

Volatile DEFAULTs (`now()`, `now64(n)`, `today()`, `yesterday()`) are resolved
here, once per batch, and reported in `RowResult.substituted`. **Send every one
as an explicit column in the INSERT** — otherwise the server re-evaluates the
expression and preview and stored differ. Pin the instant for tests with
`settings={"chtypes_now_epoch_nanos": "1700000000000000000"}`.

## API reference

Every public symbol, its underlying C entry point, and what it raises.
Row-level verdicts are **returned**, never raised: a rejected row is a
`RowResult`, not an exception.

| Symbol | C function | Returns | Raises |
|---|---|---|---|
| `Registry(dir=None, *, timezone="UTC", verify_hashes=False, autofetch=None)` | at load, per line: `chs_clickhouse_version`, `chs_abi_revision`, `chs_init` | `Registry` — walks the search path (`.search_path`, `.directory` = where fetch writes), loads nothing yet | `RegistryError` (an explicit directory that exists but cannot be read) |
| `Registry.versions()` | — derived | `tuple[str, ...]` minor lines on the search path, release order | — |
| `Registry.libraries()` | loads every line | `tuple[Library, ...]` | `RegistryError` (a line that has a manifest and does not load) |
| `Registry.for_version(v)` / `registry[v]` | loads the line on first use | `Library` (exact patch or minor line; no nearest fallback) | `ArtifactMissingError` (the §7 message, every directory searched; with `autofetch` on, `ensure` runs first and ITS error is raised instead); `RegistryError` (broken artifact, hash mismatch, ABI-revision mismatch, failed init); `ChtypesError` if closed |
| `v in registry`, `iter`, `len` | — derived | whether `for_version` would resolve without fetching / libraries / count | — |
| `ensure(line, *, dest=None, platform=None, url=None, tag=None, lock=None, frozen=False, force=False, offline=False, trusted_keys=None, allow_unsigned=None, progress=None)` | — fetch, no C call | `Path` of `<registry>/<minor>`, installed and verified (idempotent) | `ArtifactUntrustedError`, `ArtifactCorruptError`, `ArtifactPinnedError`, `ArtifactUnpublishedError`, `SourceUnreachableError` (each `.code`); `ValueError` bad option |
| `fetch_lines(lines, *, all_lines=False, …same)` | — fetch | `list[Path]`, the release read once | same |
| `registry_search_path(explicit=None)`, `fetch_destination(explicit=None)`, `host_platform()`, `default_registry_dir()` | — paths | the §1 search path / where fetch writes / `<os>-<arch>` / the per-user cache | — |
| `ArtifactError` and its six subclasses, `CODE_ARTIFACT_*`, `CODE_SOURCE_UNREACHABLE`, `UnsignedArtifactWarning` | error surface | `RegistryError` subclasses carrying `.code` | — |
| `RELEASE_PUBLIC_KEY`, `RELEASE_KEY_ID`, `ENV_AUTOFETCH` | — constants | the embedded release signing key (hex) and its id; `"CHTYPES_AUTOFETCH"` | — |
| `Registry.close()` | `chs_shutdown` per library, refcounted per image | `None` | — |
| `Library.version` / `.minor` / `.path` / `.manifest` / `.abi_revision` | `chs_clickhouse_version`, `chs_abi_revision` at load | `str` / `str` / `str` / `Manifest` / `int` | — |
| `Library.validate_type(expr)` | `chs_validate_type` | canonical `str` | `SchemaError` (e.g. 50 unknown family); `UnsupportedError` (a decline — e.g. an unsafe family this build refuses to construct) |
| `Library.reference_type(expr)` | `chs_reference_type` | widened type, `""` if none | `UnsupportedError` (artifact predates it) |
| `Library.registered_families()` | `chs_registered_families` | `list[str]` family names | `UnsupportedError` (artifact predates it) |
| `Library.function_flags()` | `chs_function_flags` | the volatility TSV audit, verbatim `str` | `UnsupportedError` (artifact predates it) |
| `Library.compile_ddl(ddl, *, settings=None, mode=COMPILE_DECLARED)` | `chs_schema_compile` + the `chs_schema_column_*` group | `Schema` | `SchemaError` (server refusal: bad DDL, 115 unknown setting name, 455/44 declared type gate); `UnsupportedError` (refused DEFAULT, admission budget, unknown `mode`) |
| `Library.has_compile_settings` | dlsym probe | `bool` | — |
| `Library.set_default_settings(settings)` | `chs_set_default_settings` | `None` | `ChtypesError` (refused wholesale, server's own 115 + hint) |
| `Library.close()` | `chs_shutdown` on the LAST close of the image (refcounted on the resolved path) | `None` | — |
| `Schema.columns` | `chs_schema_column_{count,name,type,default_kind,default_expr,default_is_literal}` at compile | `tuple[Column, ...]` canonicalised | — |
| `Schema.set_engine(engine, order_by, *, merge_tree_settings=None)` | `chs_schema_engine` | `None` | `SchemaError` (rc>0 — server refusal, e.g. 115); `UnsupportedError` (rc<0 — unmodelled engine/key, non-default MergeTree setting) |
| `Schema.set_ttl(ttl_sql)` | `chs_schema_ttl` | `None` | `UnsupportedError` (refused TTL form) |
| `Schema.row(fmt, raw, settings=None)` | `chs_row` | `RowResult` | `ChtypesError` closed; `TypeError` non-bytes / float setting |
| `Schema.rows(fmt, body, settings=None)` | `chs_rows` | `BatchResult` | same as `row` |
| `Schema.compile_filter(expr, *, params=None)` | `chs_filter_compile` | `Filter` (WHERE-side semantics; `params` binds `{name:Type}` — values are STRINGS, never hand-escaped) | `SchemaError` (47 unknown identifier, **456** unbound param, **457** unparseable param value); `UnsupportedError` (clock reads, artifact predates the trio) |
| `Filter.rows(fmt, body, settings=None)` | `chs_filter_rows` | `FilterResult` — per-row `Verdict`s; `ERROR`/`DECLINE` are NOT answers, fail closed | `ChtypesError` closed |
| `Schema.parse_block(fmt, body, settings=None)` | `chs_block_parse` | `Block` — one parse, evaluable by K filters; a call-level failure yields NO block | `SchemaError` (115 unknown setting, framing, decode fault); `UnsupportedError` |
| `Filter.eval(block)` | `chs_filter_eval` | the same `FilterResult` `rows` answers — `eval(parse_block(body))` ≡ `rows(body)`; a cross-schema pair answers REJECTED/1002 | `ChtypesError` (closed handles, cross-library pair) |
| `Filter.close()` / `Block.close()` / context managers | `chs_filter_free` / `chs_block_free` | `None` — `Schema.close()` frees open filters and blocks FIRST (the C-required order) | — |
| `Schema.close()` / context manager | `chs_schema_free` | `None` | — |
| `Format`, `Outcome`, `DefaultKind`, `Reason`, `LOSSLESS_REASONS`, `COMPILE_DECLARED` | ABI constants / vocabulary | enums & constants | — |
| `Column`, `Value`, `Transform`, `Substitution`, `Computed`, `RowResult`, `BatchResult` | result documents, parsed | frozen dataclasses | — |
| `ChtypesError`, `RegistryError`, `SchemaError`, `UnsupportedError`, `CODE_UNSUPPORTED` | error surface | — | `UnsupportedError` is a PEER of `SchemaError` (spec rule 12; the subtype was retired 2026-08-26): `except SchemaError` never catches a decline — handle the two arms explicitly, or catch `ChtypesError` for both |
| `QUERY_SERVER_VERSION`, `QUERY_CHANGED_SETTINGS`, `QUERY_TABLE_COLUMNS` | — SQL constants, no C call | `str` | — |
| `ServerProfile`, `DiscoveredColumn` | — derived | dataclasses | — |
| `parse_version_result`, `parse_changed_settings_result`, `parse_columns_result`, `reconstruct_ddl` | — derived, no C call | `str` / `dict[str, str]` / `list[DiscoveredColumn]` / DDL `str` | `ValueError` (malformed caller-supplied bytes) |
| `Manifest`, `read_manifest`, `verify_library`, `minor_of`, `ENV_REGISTRY` | — loader helpers | see docstrings | `RegistryError` from `verify_library` |
| `RawNumber`, `quote_bare_denormals` | — result-document plumbing | exact-text number; repaired bytes | — |
| `ABI_REVISION` | hand-kept mirror of `CHS_ABI_REVISION` | `int` (4) | — |

Every `chs_*` entry point is now bound (`chs_registered_families` and
`chs_function_flags` joined the surface with the 2026-08-26 introspection-parity
cycle — spec/bindings.md §Introspection).

## Filters, query parameters and the block twin (ABI revisions 3–4)

`Schema.compile_filter(expr)` compiles one boolean expression against the
schema's physical columns and `Filter.rows(...)` answers per-row verdicts
with **WHERE-side** semantics (`x = 256` over `UInt8` promotes — false for
every row — it never wraps), computed by ClickHouse's own comparison
functions. Four verdicts: `Verdict.TRUE`/`FALSE` are answers;
`Verdict.ERROR` (the predicate threw — a real server fails the whole query)
and `Verdict.DECLINE` (this library declines) are NOT, and an enforcing
caller MUST fail closed on both. **Enforcement gate**: no read-side security
may be enforced on this surface until the WHERE-truth rig gates green —
until then it is shadow/replay only (`spec/c-abi.md` §Filters).

**Params (revision 4).** The expression may contain `{name:Type}` query
parameters, bound with `compile_filter(expr, params={"name": "value"})` —
values are **strings**, exactly as the server's own parameter channels carry
them. Substitution is the server's own `ReplaceQueryParameterVisitor`: each
value is deserialized by the DECLARED type's own reader and injected as a
typed literal AFTER SQL parsing, so a value is never SQL text and **must
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
(unreachable through this SDK's unique-keyed dict, stated for completeness).

One naming trap, the transport's rather than this library's: on a real
server's **TCP** channel a parameter *named* `limit` or `offset` fails at
the protocol layer with code 26 even when unused (params ride in a Settings
block there); HTTP is fine, and this library matches the HTTP/substitution
semantics. Avoid those two names for anything that will ever cross TCP.

**The block twin (revision 4).** `Schema.parse_block(fmt, body, settings)`
parses a body ONCE into a `Block` — the parse half of `Filter.rows` — and
`Filter.eval(block)` answers the same `FilterResult` with no re-parse: the
live-SSE hot path is K filters × 1 event, and the re-parse is shed.
Normative equivalence: `eval(parse_block(body))` ≡ `rows(body)` for every
verdict class (volatile DEFAULTs resolve against the PARSE call's clock
instant — pin `chtypes_now_epoch_nanos` for cross-call identity).
Evaluation is a pure function of (filter, block), takes no settings, and
never consumes or mutates the block. Filter and block MUST come from the
SAME schema: a mismatched same-library pair answers REJECTED/1002 loudly,
and a cross-library pair raises `ChtypesError` before any C call. A `Block`
follows the filter's lifetime rules exactly (`Schema.close()` frees open
blocks first; an eval is a use of BOTH handles). Parse-once does not open
enforcement earlier: the twin is under the same enforcement gate.

## Settings and precedence

The row path resolves three settings channels in one order (later wins is the
same list read backwards):

    per-call map  >  handle compile profile  >  library defaults
    (set_default_settings)  >  ClickHouse's own build defaults

**One exception, and it is the server's own**: a type gate
(`allow_experimental_json_type`, `allow_suspicious_low_cardinality_types`, …)
**declared in the compile profile** binds at compile — a refusing value fails
`compile_ddl` with the server's own 455/44, exactly as CREATE would — and
thereafter the handle outranks even an explicit per-call value for that name,
because a real server checks a gate when the column is created and never again.
Gates the profile does not name stay per-call re-checked.

Rules the boundary enforces:

- **Values cross as strings.** `encode_settings` stringifies an `int` exactly
  (`str(int)`, never through a float) and **refuses a `float` with
  `TypeError`**: a 19-digit `chtypes_now_epoch_nanos` does not survive an IEEE
  double, and as a JSON number the setting is silently ignored.
- **An unknown setting name rejects the whole call with the server's own
  code 115**, did-you-mean hint included — on every channel
  (`set_default_settings` refuses its payload wholesale; `compile_ddl` fails
  the compile; `row`/`rows` reject the call). Obsolete names are accepted
  silently, as servers do; custom-prefixed names (`SQL_` by default, mirror
  your server's with `chtypes_custom_settings_prefixes`) are legal and inert.
- **The reserved namespace is exactly six keys**, not a `chtypes_` prefix —
  any other `chtypes_*` spelling is an unknown name (115). Per-**call** (clock
  control): `chtypes_now_epoch_nanos` (pin the batch instant),
  `chtypes_clock_offset_nanos` (measured server−client offset),
  `chtypes_max_clock_skew_nanos` (refuse volatile-DEFAULT substitution past
  the budget → `Outcome.UNSUPPORTED`). Per-**process** (via
  `set_default_settings` only): `chtypes_default_eval_memory_bytes` (256 MiB
  default), `chtypes_default_eval_wall_nanos` (1 s default),
  `chtypes_custom_settings_prefixes`. A per-process key handed to `row`/`rows`
  comes back on `unsupported_settings` — a decline, never an admission — and
  any row with a non-empty `unsupported_settings` is promoted to
  `Outcome.UNSUPPORTED`.
- `set_engine`'s `merge_tree_settings=` is a separate namespace (the `SETTINGS`
  clause after the engine): unknown name ⇒ `SchemaError` 115; a known name at a
  **non-default** value ⇒ `UnsupportedError`, never silently ignored.

## Discovery

The library **never connects to ClickHouse** — it ships the three canonical
queries; the app runs them at connect time with whatever client it already has:

```python
import chtypes

# 1. Once per connection; cache the profile per deployment/tenant.
profile = chtypes.ServerProfile(
    version=chtypes.parse_version_result(run(chtypes.QUERY_SERVER_VERSION)),
    settings=chtypes.parse_changed_settings_result(run(chtypes.QUERY_CHANGED_SETTINGS)),
)
# 2. Resolve the artifact from the server's own version string.
library = registry.for_version(profile.version)
# 3. Compile every schema under the declared profile. A typo'd setting name is
#    caught HERE with the server's own code 115, at declare time — not swallowed.
schema = library.compile_ddl(ddl, settings=profile.settings)
# 4. Pass the same profile (plus per-INSERT overrides) as per-call settings.
schema.rows(chtypes.Format.JSON_EACH_ROW, body, settings=profile.settings)
```

For an existing table,
`reconstruct_ddl(parse_columns_result(run(QUERY_TABLE_COLUMNS)))` (bind
`param_db`/`param_table`) turns `system.columns` back into the
column-declaration list `compile_ddl` takes — `default_kind` and
`default_expression` carried, without which DEFAULT/MATERIALIZED semantics are
silently lost. `system.columns` reports the table AS STORED (flattened columns
under `flatten_nested=1`), so reconstruction is shape-faithful exactly when the
discovered profile is declared to the compile. Never ask the customer for their
settings — ask their server.

## Multi-version use and thread-safety

Artifacts are loaded `RTLD_NOW | RTLD_LOCAL` — the whole mechanism by which
several ClickHouse builds coexist in one process, at ~120 MB resident each. A
`Registry` indexes each library under both its exact version and its minor
line (`25.8.30.16` resolves to the loaded `25.8`); resolution failure names
the versions that ARE loaded and never falls back to the nearest — answering
26.7 semantics from a 25.8 artifact is a lie.

**`ctypes` releases the GIL for the whole duration of a foreign call**, so the
GIL is not the exclusion. Two locks, mirroring the Go reference:

- **One writer-preferring readers-writer lock per loaded IMAGE**
  (`_native._RWLock`). Row calls, compiles, type validation and engine/TTL
  declarations hold it *shared* (concurrent on distinct handles, as the ABI
  permits); `Library.set_default_settings` and `Library.close` hold it
  *exclusively*, as the ABI requires — `chs_set_default_settings` replaces a
  process-global the row path reads **by reference**, so overlap is a
  use-after-free, not a stale read.
- **One plain lock per `Schema`**: a single `chs_schema *` must never be used
  from two threads at once.

Per **image**, not per object: `dlopen` refcounts one mapping per file, so two
`Registry` instances over one directory share the C globals; the lock is
interned on the resolved path, the same key `chs_init` is deduplicated on (a
second init with a different timezone is refused rather than silently
re-timezoning live libraries).

Teardown: `Registry.close()` / `Library.close()` release this wrapper's hold
on the loaded image, and the LAST close per image runs `chs_shutdown`, which
joins the DEFAULT evaluator's background threads — `dlopen` refcounts one
image per file, so two Registries over one directory share every image, and
closing one must not tear the evaluator down under the other
(spec/bindings.md §Teardown, 2026-08-26). `chs_init` registers it with
`atexit`, so an ordinary process needs no call; it is **required** before any
`dlclose`, when the host controls its own teardown order, or in tests that
must not depend on `atexit`. Close every `Schema` first. Both are idempotent.

Measured under contention (`tests/test_registry.py`): 27,770 batch reads across
8 threads against 566 concurrent settings swaps, every answer byte-identical to
the uncontended one, no exceptions, no deadlock.

## Known limitations

- **The error model is normative** — `spec/c-abi.md` §Error model. In
  particular `Outcome.UNSUPPORTED` / `UnsupportedError` (`CODE_UNSUPPORTED`,
  `-2`) is never a rejection: fall back to the server.
- **EPHEMERAL columns**: an INSERT whose explicit column list names an
  EPHEMERAL column cannot be previewed by a format stream — decline it rather
  than mispreview (`spec/bindings.md` §EPHEMERAL). Detect at compile time via
  `Schema.columns` (`DefaultKind.EPHEMERAL`).
- **Engines and TTL forms this build does not model** raise
  `UnsupportedError` (WHERE/GROUP BY TTLs, TO DISK/VOLUME, RECOMPRESS,
  clock-reading TTL expressions, unmodelled engines/sorting keys).
- **macOS artifacts are a dev floor, not an oracle**: `long double` is 53-bit,
  so float parses diverge from real servers (Linux 395/395, macOS 0/395).
  Float expectations must come from a Linux artifact or a live server.
- **Insert-side only**: never reuse these coercions for `WHERE`-clause
  constants (`spec/bindings.md` §Constants are not payloads).
- **Conformance**: Levels 1–2 (ABI, surface) are covered by `tests/`;
  Level 3 — being scored by the rigs — has run: `chtypes-py` is a scored
  arbiter column (held-out agreement 99.90 % on the 2026-08-26 run of record,
  `tests/arbiter/RESULTS.md`).

## More

- `playground/python/` — a runnable nine-section tour
  (`cd playground/python && uv run demo.py`); the go/, ts/ and rust/ tours
  beside it print the same nine sections, so a diff shows only spelling.
- `uv run pytest -q` — the unit suite and the golden set; needs a real registry
  on the search path (`$CHTYPES_REGISTRY`, else the per-user cache), and skips
  loudly without one. The fetch suite (`tests/test_fetch.py`, `tests/test_cli.py`)
  runs offline against the miniature releases in `spec/fixtures/fetch/` through
  `file://` sources and the test key, and skips loudly if they are absent.

## Pre-1.0 changes

- **2026-09-09 — `Registry()` walks the search path** (docs/fetch.md §1, §7;
  breaking). `Registry()` with nothing set no longer raises for a missing
  `$CHTYPES_REGISTRY`: it searches the explicit path, the environment, the
  per-user cache and the system locations, in that order. An empty registry
  is no longer an error at construction; opening a line no directory holds is
  `ArtifactMissingError` (a `RegistryError`) with the shared message, in place
  of the old `RegistryError` that listed the loaded versions. Loading is lazy
  per line (`libraries()` loads all); `Registry.directory` is the directory
  fetch writes to and `Registry.search_path` every directory consulted;
  `verify_hashes=True` applies at load. New: `python -m chtypes` / `chtypes`
  (fetch, verify, list, where), `chtypes.ensure`, `chtypes.fetch_lines`,
  `Registry(autofetch=)` / `CHTYPES_AUTOFETCH`, the `ArtifactError` family,
  the embedded release key and a pure-Python ed25519 verifier.
- `spec/bindings.md` — the shape every SDK implements; `spec/c-abi.md` — the
  `chs_*` contract underneath.
