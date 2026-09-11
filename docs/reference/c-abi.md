# The C ABI

The C ABI is the product's ground truth. Everything else — the Go package, the
oracle, and the Python, Rust and TypeScript SDKs — is a shape wrapped around these
functions. `include/chtypes.h` is the authoritative declaration; this document
states the contract a non-Go caller needs and documents the result documents in
full, which the header only sketches.

## ABI identity

| Property | Value | Frozen? |
|---|---|---|
| Base name | `libchtypes` (`.so` on Linux, `.dylib` on macOS) | Yes |
| Symbol prefix | `chs_` | Yes |
| Exported symbols | **only** `chs_*` — **28 functions** (22 through revision 2; the filter trio arrived at revision 3; the block twin — `chs_block_parse` / `chs_block_free` / `chs_filter_eval` — at revision 4) | Names: yes. Signatures: see *Stability* below |
| `enum chs_format` values | `0`–`9` as declared | Yes — bindings pass integers |
| Visibility | built `-fvisibility=hidden` plus an exported-symbols list | Yes |
| Runtime deps | libc, and CoreFoundation on macOS. libc++ is static, inside. | — |
| Header guard / include | `CHTYPES_H`, `#include "chtypes.h"` | — |
| ABI revision | `CHS_ABI_REVISION` (header) == `chs_abi_revision()` (artifact) | Increments by one per consolidation cycle |

### The ABI revision

Symbol PRESENCE proves a function exists, never that its signature matches the
header a caller compiled against. `chs_abi_revision()` closes that gap, and it
is the mechanism the whole pre-1.0 "signatures are breakable" policy below
rests on.

* `CHS_ABI_REVISION` is what the CALLER compiled against (`chtypes.h`).
* `chs_abi_revision()` is what the loaded ARTIFACT was built from.
* **Equal** — the declarations match; call freely.
* **Different (both nonzero)** — a binding MUST refuse to load the artifact and
  MUST name both numbers. Calling through mismatched declarations is undefined.
* **Symbol ABSENT** — the artifact predates the probe. Report revision `0` and
  keep the per-symbol degradation rules of `docs/reference/artifact.md` §Loading step 5.
  Absence is a claim of ignorance, not of incompatibility.

It increments by exactly one per consolidation cycle that changes ANY existing
declaration, and also on a purely additive cycle. Revision **1** is the ABI as
of the settings-shape cycle, 2026-08-25. Revision **2** is the Buffers cycle of
the same day: `CHS_BUFFERS = 9` joined `enum chs_format` and nothing existing
was renumbered, changed or deleted — the purely additive case, which increments
for honesty rather than safety. Revision **3** is the export/filter cycle,
2026-08-31: `chs_rows` gained three parameters (`export_format`, `doc_flags`,
`out_bytes` — see §Rows), the `chs_bytes` struct, the `CHS_EXPORT_NONE` and
`CHS_DOC_*` constants and the `chs_filter_compile` / `chs_filter_free` /
`chs_filter_rows` trio joined the header (see §Filters), and nothing was
renumbered or deleted. This is the signature-change case the rule exists for:
a caller compiled against either header calling the other artifact's
`chs_rows` is undefined behavior, so the revision gate is what stands between
them. Revision **4** is the filter phase-2 cycle, 2026-08-31 (same day,
second cycle): `chs_filter_compile` gained a `params_json` parameter (query
parameters — the vendored `ReplaceQueryParameterVisitor` substitution; see
§Filters), and the block-parse twin — the `chs_block` handle type,
`chs_block_parse`, `chs_block_free` and `chs_filter_eval` — joined the
surface (see §Blocks). 28 exported functions. Both cases at once: a changed
signature (`chs_filter_compile`) and additions, so the gate refuses either
pairing of header and artifact across the boundary.

Note what that means and do not soften it: a caller compiled against revision 2
loading a revision 1 artifact takes the **Different** branch above, not the
**Symbol ABSENT** one, so it MUST refuse. Degradation is for ignorance
(revision `0`) alone; a revision that is merely OLDER is still a refusal. That
is why a cycle which bumps this number relinks EVERY artifact in the registry in
the same cycle — otherwise it would strand every artifact it did not touch.

There is no revision-0 artifact: `0` is reserved for "the symbol was absent".

A binding that dlopens rather than compiling against the header keeps a
hand-kept mirror of the constant (`chtypes.ABIRevision` in Go,
`chtypes.ABI_REVISION` in Python/Rust/TypeScript) and MUST bump it in the same
cycle the header is bumped.

Verified with `nm` on 2026-08-26, against the artifacts of that day's relink:
the `darwin-arm64/25.8` artifact exports exactly 22 symbols, all `chs_*`; the
`linux-arm64/25.8` artifact exports the same 22 names (21 plus
`chs_abi_revision`; the Buffers cycle added an enum value and no function, so
the count did not move). No ClickHouse C++ symbol escapes into the host process, which is
what makes several versions loadable at once.

Re-verified 2026-08-31 against the revision-3 relink of `darwin-arm64/25.8`:
`nm -gU` shows exactly **25** exported symbols, all `chs_*` (the 22 plus
`chs_filter_compile`, `chs_filter_free`, `chs_filter_rows`), and zero
non-`chs_` exports. The `linux-arm64` fleet re-verifies when the cycle's
relink-all reaches it.

Re-verified 2026-08-31 against the revision-4 relink of `darwin-arm64/25.8`
(the filter phase-2 cycle's smoke artifact): `nm -gU` shows exactly **28**
exported symbols, all `chs_*` (the 25 plus `chs_block_parse`,
`chs_block_free`, `chs_filter_eval`), zero non-`chs_` exports. Every other
artifact in the registry still answers revision 3 at that point and is
REFUSED by a revision-4 binding — measured: the Go census records 17 skips,
every one the gate's own message ("reports ABI revision 3, this package
speaks 4; refusing to call through mismatched declarations"), zero test
failures. The fleet relink that clears those refusals is the cycle's own
follow-up, per the relink-all rule above.

> The figure "16-function C API" appeared in `README.md` and `CLAUDE.md`
> (both corrected 2026-08-17): it predated `chs_shutdown`, the engine/TTL
> entry points and the column-introspection group. The count is 22. The
> *names* are what is frozen, not the count.

### Stability, pre-1.0

Nothing has published this library yet, so **this ABI is not additive-only.**
Until the first publish, a deliberate consolidation cycle MAY change any
signature, delete any function, and rebuild every artifact in the same cycle.
Versioned duplicates are explicitly *not* how this ABI evolves: the
`chs_schema_compile_v2` / `chs_schema_engine_v2` pair that existed between
2026-08-18 and 2026-08-24 was folded back into the primary names, because
additive versioning before anyone consumes the library is pure debt (the cost
that made a frozen ABI worth paying — hours of ClickHouse C++ per artifact —
is the *cached compile*; a wrapper relink is minutes per artifact).

What that costs a loader, stated plainly: **symbol presence proves a function
exists, never that its signature matches this header.** An artifact and the
header a caller compiles against MUST come from the same revision. The
presence probe stays anyway, because a Registry may someday load an artifact
somebody else built, and because it is the honest answer to "can this artifact
do X at all". After the first publish this section is replaced by a
compatibility policy and ordinary semantic versioning begins.

| what | who decides | still true after 1.0? |
|---|---|---|
| base name `libchtypes`, prefix `chs_`, "nothing else is exported" | frozen — identity | yes |
| `enum chs_format` numbering | frozen — bindings pass integers | yes |
| function signatures, parameter order, return conventions | breakable by a deliberate pre-publish cycle | no — they freeze at 1.0 |
| result-document field names | additive, and absent-means-default (see below) | yes |

**Multi-version coexistence.** Artifacts MUST be loaded with `RTLD_LOCAL`
(`dlopen(path, RTLD_NOW | RTLD_LOCAL)`). That is the whole mechanism by which
two builds that both define `DB::DataTypeFactory` can live in one process. A
loader that uses `RTLD_GLOBAL` will appear to work and then answer with the
wrong version's semantics. Cost, measured: roughly 120 MB resident per loaded
version.

## The function table

Return-value conventions differ per function and are not guessable; each row
states its own.

### Identity and process setup

```c
const char * chs_clickhouse_version(void);
int  chs_abi_revision(void);
int  chs_init(const char *timezone, const char *unsafe_families, char **out_err);
int  chs_set_default_settings(const char *settings_json, char **out_err);
void chs_shutdown(void);
void chs_free(char *p);
```

| Function | Returns | Ownership | Notes |
|---|---|---|---|
| `chs_clickhouse_version` | borrowed `const char *`, never NULL | do **not** free | A pointer to a string constant compiled in, e.g. `"25.8.28.1-lts"`. Cheap; callable before `chs_init`. This is how a library names itself — a loader MUST NOT infer the version from the directory or file name. |
| `chs_abi_revision` | `int`, never fails | — | The ABI revision this artifact was BUILT from — `CHS_ABI_REVISION` at build time, returned verbatim. Cheap; callable before `chs_init`. See §The ABI revision: a binding compares it with its own and refuses a different NONZERO value; an ABSENT symbol is revision `0` and means "predates the probe", never "incompatible". |
| `chs_init` | `0` on success, ClickHouse's own code on failure | `*out_err` with `chs_free` | One-time process (per loaded library) setup. See the init contract below. |
| `chs_set_default_settings` | `0` on success, nonzero on failure (`115` for a refused setting name) | `*out_err` with `chs_free` | Seeds the settings every later call starts from. Process-wide (per library). Per-call settings still win. A payload naming a setting the version's server would refuse is refused WHOLESALE — code `115` back, nothing committed (see the Settings rules). |

| `chs_shutdown` | — | — | Joins the DEFAULT evaluator's background threads. See below. |
| `chs_free` | — | frees | Releases any `char *` returned by any `chs_*` entry point. |

**Both `out_err` parameters (`chs_init`, `chs_set_default_settings`) are
optional (`NULL` is legal) and both are new in the 2026-08-24 consolidation.**
They exist because these were the only two error-returning entry points on the
ABI with no message channel, and the message they were dropping is the one that
matters: the server's `115` carries its own did-you-mean hint *naming the
setting it refused*, which a bare code cannot. A binding SHOULD surface it —
the reference bindings put it in the error they raise.

**`chs_free` discipline.** Every `char *` a `chs_*` function returns is
malloc'd by the library and MUST be released with `chs_free`. In a multi-version
process a caller MUST call **the same library's** `chs_free` on that library's
string; the reference implementation resolves one `chs_free` function pointer per
`dlopen`'d library and never crosses them. Borrowed `const char *` returns
(marked above and below) MUST NOT be freed.

### The init contract

```c
int chs_init(const char *timezone, const char *unsafe_families, char **out_err);
```

`chs_init` MUST be called before anything except `chs_clickhouse_version` and
`chs_set_default_settings`, exactly once per loaded library, and MUST be
serialized against everything else if the caller is multithreaded.

- **`timezone`** — the *server* timezone assumed for bare `DateTime` /
  `DateTime64` columns. `NULL` means `"UTC"`, which is what a stock ClickHouse
  container uses. This parameter exists to stop the host's `TZ` from leaking into
  results: a binding MUST default it to `UTC` and MUST NOT read the process
  environment for it. The reference default is the package variable
  `chtypes.Timezone = "UTC"`.
- **`unsafe_families`** — a comma-separated list of type families this build
  must refuse to *construct*, because they dereference a global `Context` that
  does not exist outside a server and would segfault the process. `NULL`
  disables the guard. The list is not hand-written and MUST NOT be hard-coded by
  a binding: it is generated at build time by `lib/tools/gen_unsafe_families.py`
  probing the build's own registry, and ships next to the library as
  `unsafe_families.txt` (see `artifact.md`). A binding reads that file, trims
  whitespace, and passes the contents; an absent or empty file means an empty
  list. Every artifact in the current matrix ships an empty list, i.e. no family
  needs refusing on these builds — a binding that treats "empty" as "file
  missing, bail out" would be wrong.
- **`out_err`** — optional. The one reachable failure is an unknown `timezone`,
  and the code alone does not say which name was refused.

Ordering note: `chs_set_default_settings` may be called *before* `chs_init` and
the reference implementation does exactly that (it applies `CHTYPES_SETTINGS`
from the environment first, then calls `chs_init`).

**`chs_shutdown`.** Resolving a DEFAULT expression constructs `Context`'s
external loaders, whose constructors start a periodic-reload thread on
ClickHouse's global pool. Nothing joins that thread until the pool's static
destructor runs, at which point it is still looping — and the process **hangs
after printing everything**, which reads as a test-harness bug rather than a
teardown bug. `chs_init` registers `chs_shutdown` with `atexit()`, so an
ordinary process needs no call. A caller MUST call it explicitly when it
`dlclose()`s the library, when it controls its own teardown order, or from a test
that must not depend on `atexit`. It is idempotent and safe to call when
`chs_init` was never reached.

> **Exit-abort after `chs_function_flags`, re-diagnosed and fixed 2026-08-17
> (darwin):** exercising `chs_function_flags()` and exiting via the `atexit`
> path alone aborted — `libc++abi: … mutex lock failed: Invalid argument`,
> exit 134, *after* every correct answer was printed. Originally recorded as
> a multi-version (N ≥ 2) hazard; bisection showed it is **26.7-specific and
> reproduces with the 26.7 artifact alone** (every earlier failing set
> happened to include 26.7; per-image statics are independent, so N never
> mattered). Root cause, symbolicated: 26.7 introduced
> `UntrackedMemoryRegistry`, a function-local static first constructed by
> the first `ThreadStatus` in the process. `chs_function_flags` resolves the
> region functions, which constructs `EmbeddedDictionaries` and starts its
> `DictReload` thread; at exit the atexit `Context::shutdown()` woke that
> thread *after* the registry's destructor had already run, and its
> `~ThreadStatus → ~UntrackedMemoryCounter` locked a destroyed mutex inside
> a noexcept destructor — terminate, SIGABRT. Fixed in the wrapper
> (chs_init now primes the thread-teardown statics before registering the
> shutdown handler, and registers the handler last): all six artifacts then
> in the matrix in one process, `chs_function_flags` on each, atexit-only
> teardown, exit 0.
> `chs_shutdown` remains belt-and-braces and is still REQUIRED before
> `dlclose` (the atexit handler cannot run in an unloaded image).

### Introspection

```c
char * chs_registered_families(void);   /* free with chs_free */
char * chs_function_flags(void);        /* free with chs_free; requires chs_init */
char * chs_reference_type(const char *type_expr);  /* free with chs_free */
```

- `chs_registered_families` — newline-separated list of every type family in
  this build's runtime registry. 139 entries on the `25.8` artifact. This is the
  answer to "does this build track upstream type families without a table to
  maintain?" — it does, so there is no per-release family list to update.
- `chs_function_flags` — TSV audit of every registered function's volatility,
  one per line:
  `name \t deterministic \t deterministic_in_query \t server_constant \t stateful \t resolver_error_code`.
  These are ClickHouse's own answers off this build's own registry. It drives
  `lib/tools/gen_function_flags.py`, which fails the build unless the admitted
  volatile set is exactly the four clock reads. That gate is the statelessness
  guarantee; it is build-enforced and MUST be kept.
- `chs_reference_type` — diagnostic: the widened reference type this build would
  use for a type expression, e.g. `UInt8 → Int256`, `DateTime →
  DateTime64(0, 'UTC')`, `Decimal(18,4) → Decimal(76,24)`, `UUID → String`,
  `IPv4 → String`, `Array(UInt8) → Array(Int256)`. Returns the **empty string**
  for types with no wider type to compare against (`String`, `Float64`). Bindings
  do not need it to function — the reference type is already applied inside
  `chs_row` and reported per column as `ref` / `ref_type` — but exposing it makes
  transformation findings explainable.

### Types and schemas

```c
enum chs_format { CHS_JSON_EACH_ROW = 0, CHS_CSV = 1, CHS_TSV = 2, CHS_VALUES = 3,
                  CHS_JSON_COMPACT_EACH_ROW = 4, CHS_ROW_BINARY = 5,
                  CHS_ROW_BINARY_WITH_DEFAULTS = 6,
                  CHS_ROW_BINARY_WITH_NAMES_AND_TYPES_AND_DEFAULTS = 7,
                  CHS_NATIVE = 8, CHS_BUFFERS = 9 };

#define CHS_CODE_UNSUPPORTED (-2)

enum chs_compile_mode { CHS_COMPILE_DECLARED = 0 };

int  chs_validate_type(const char *type_expr, char **out_canonical,
                       int *out_code, char **out_err);
typedef struct chs_schema chs_schema;
chs_schema * chs_schema_compile(const char *columns_sql, const char *settings_json,
                                int mode, int *out_code, char **out_err);
void chs_schema_free(chs_schema *s);
int  chs_schema_engine(chs_schema *s, const char *engine, const char *order_by,
                       const char *merge_tree_settings_json, char **out_err);
int  chs_schema_ttl(chs_schema *s, const char *ttl_sql, char **out_err);
int          chs_schema_column_count(const chs_schema *s);
const char * chs_schema_column_name(const chs_schema *s, int i);
const char * chs_schema_column_type(const chs_schema *s, int i);
const char * chs_schema_column_default_kind(const chs_schema *s, int i);
const char * chs_schema_column_default_expr(const chs_schema *s, int i);
int          chs_schema_column_default_is_literal(const chs_schema *s, int i);
```

**The format codes are part of the ABI.** Bindings pass integers across the
boundary, so the numbers above are normative and MUST NOT be renumbered. The
same is true of `enum chs_compile_mode`, whose one defined value is
`CHS_COMPILE_DECLARED = 0`; the parameter is declared `int` for the same reason
`format` is.

| Function | Returns | Ownership |
|---|---|---|
| `chs_validate_type` | `0` on success (sets `*out_canonical`); on failure **the error code** — a ClickHouse code, or `CHS_CODE_UNSUPPORTED` — and sets `*out_code` / `*out_err` | both out-strings are malloc'd; free with `chs_free`. **Every** out param is optional and MAY be `NULL` — the return value carries the code |
| `chs_schema_compile` | handle, or `NULL` on failure (sets `*out_code`, `*out_err`) | handle freed with `chs_schema_free`; `*out_err` with `chs_free` |
| … its `settings_json` / `mode` | compile under a DECLARED settings profile fixed into the handle — see §Compile-time vs per-call settings. `NULL`/`"{}"` settings take a code path that makes no `Context` copy at all, i.e. "no profile" is structural rather than a special case. An unknown setting name fails with the server's own `115` in `*out_code`; a known name with an unparseable value fails with the server's own code for that; a `mode` other than `CHS_COMPILE_DECLARED` fails with `-2` | |
| `chs_schema_engine` | `0` accepted. `-2` (`CHS_CODE_UNSUPPORTED`) for an engine or sorting key this build does not model, or a MergeTree setting declared at a non-default value. `115` — the server's own **rejection** of an unknown MergeTree setting name, with its own message; a real ClickHouse code, not a decline. `-1` for a guarded exception (measured 2026-08-17: `TTL now() + INTERVAL 1 DAY` → `-1`). `*out_err` says why in every nonzero case | `*out_err` with `chs_free` |
| … its `merge_tree_settings_json` | the table's **MergeTree-namespace** settings (the `SETTINGS` clause after the engine — `allow_nullable_key` and friends, a namespace `DB::Settings` cannot carry). `NULL`/`"{}"` declares none. Names are validated by the server's own `MergeTreeSettings` object; a known name declared at a **non-default** value is `-2` naming it (no MergeTree setting's behavior is modeled yet — declared values are refused, never silently ignored); declared **at** the default is inert and proceeds | |
| `chs_schema_ttl` | `0` on success; **any nonzero** means unsupported (`-2` for a refused TTL form, `-1` for a guarded exception) | `*out_err` with `chs_free` |
| … which settings it validates under | the HANDLE's declared compile profile when it has one, the process defaults otherwise (revised 2026-08-25 — it previously used the process-global `Context` regardless). It takes no settings parameter and never will: the handle IS the CREATE, and a `TTL` clause is validated by the CREATE. This is load-bearing, not symmetry — `allow_suspicious_ttl_expressions` is read inside `TTLDescription::getTTLFromAST` on every vendored era (24.8 `TTLDescription.cpp:346`, 25.8 `:355`, 26.7 `:334`; 24.8 reads it a second time in `TTLTableDescription::parse` itself, `:438`), any `CAST` in the expression reaches `CastOverloadResolver` and thus `cast_keep_nullable` and the whole `DataTypeValidationSettings` type-gate bundle, and on 26.7 `FunctionConvertSettings` takes `getFormatSettings(context)` wholesale (`FunctionsConversion.h:152`), putting `date_time_input_format` on the TTL path for that era | |
| `chs_schema_column_count` | column count | — |
| `chs_schema_column_{name,type,default_kind,default_expr}` | **borrowed** `const char *`, valid until `chs_schema_free` | do **not** free |
| `chs_schema_column_default_is_literal` | `1` when the DEFAULT is a plain literal applicable without the expression interpreter, else `0` | — |

> **`chs_schema_engine` is the one function whose nonzero returns are not all
> the same kind of answer**, and a binding MUST NOT flatten them. The rule is
> **the SIGN of the return**, not any particular code:
>
> * **`rc > 0` is a REJECTION the server itself makes** — a real ClickHouse
>   error code, meaning this DDL can never exist and the tenant has to be
>   told. `*out_err` carries the server's own message verbatim. `115`
>   (unknown MergeTree setting name) is the only positive code this revision
>   returns; a binding MUST key on the sign so that a code a later era adds
>   here cannot silently become a decline.
>
>   On the **`DB::Settings`** channels (a compile profile, a per-call map,
>   `chs_set_default_settings`) a `115` message also carries upstream's own
>   `Maybe you meant …` hint, because those names run `gateSettingName`,
>   which appends `Settings::getHints` exactly as `SettingsConstraints::
>   checkImpl` does. The **MergeTree namespace** has no such hint upstream:
>   measured 2026-08-25 on all seven relinked artifacts, the message is
>   `Unknown setting 'totally_made_up_mt'` and nothing more, because
>   `MergeTreeSettings::loadFromQuery` decorates its `UNKNOWN_SETTING` with
>   `"for storage " + engine name` rather than with hints (24.8
>   `MergeTreeSettings.cpp:98-99`, 26.7 `:2432-2433`). A binding must pass
>   through whatever message it is given and invent neither.
> * **`rc < 0` is a DECLINE** — `-2` "a real server might well have accepted
>   this; I will not guess", `-1` a guarded exception. A caller must
>   validate cautiously, not report a tenant error.
>
> Mapping a positive code onto "unsupported" loses a genuine refusal; mapping
> either decline onto a rejection manufactures an over-reject. (An earlier
> revision of this spec said "bindings MUST treat any nonzero as unsupported".
> That was written before the MergeTree-settings channel existed and is
> corrected here. A later revision said `115` specifically; 2026-08-25
> generalized it to the sign, because "which codes are positive" is upstream's
> decision and not this ABI's.)

`chs_schema_compile` takes a ClickHouse **column-declaration list**, not a
`CREATE TABLE` — `"a UInt8, b Nullable(String) DEFAULT 'x', c DateTime
MATERIALIZED now()"`. It is parsed by ClickHouse's own
`ParserColumnDeclarationList`, so DEFAULT expressions are validated as real SQL.
Column-level `TTL` clauses belong in this string and are captured here;
`chs_schema_ttl` is only for the table-level rows TTL.

`chs_schema_column_default_kind` returns one of `""`, `"DEFAULT"`,
`"MATERIALIZED"`, `"ALIAS"`, `"EPHEMERAL"`.

**Canonicalization is schema-aware, and it is not a spelling normalizer.**
Observed on the `25.8` artifact:

| Input | Canonical |
|---|---|
| `DECIMAL(18,4)`, `Decimal64(4)` | `Decimal(18, 4)` |
| `Nullable(Decimal(18,4))` | `Nullable(Decimal(18, 4))` |
| `Enum8('a'=1,'b'=2)` | `Enum8('a' = 1, 'b' = 2)` |
| `Map(String,Array(UInt8))` | `Map(String, Array(UInt8))` |
| `LowCardinality( String )` | `LowCardinality(String)` |
| `BIGINT` / `INT` | `Int64` / `Int32` |
| `Variant(UInt8, String)` | `Variant(String, UInt8)` — members are **sorted** |
| `Int8(3)` | `Int8` — surplus parameters are dropped, not rejected |
| `NotAType` | error, code `50`, `Unknown data type family: NotAType` |

Note the space after each comma inside a parameter list. A binding that
string-compares canonical types MUST use the library's spelling verbatim and MUST
NOT normalize whitespace of its own.

And the case `chs_validate_type` structurally cannot answer — which is why
`ValidateType` alone is insufficient and a schema-aware compile exists:

```
DDL:       x Int64 DEFAULT NULL
canonical: x Nullable(Int64)  DEFAULT  NULL   (default_is_literal = true)
```

The declared type is rewritten by the DEFAULT. Similarly an `ALIAS` column's
type is *inferred*: `a UInt8, al ALIAS a + 1` compiles `al` as `UInt16`.

### Rows

```c
typedef struct chs_bytes { char * data; size_t len; } chs_bytes;

#define CHS_EXPORT_NONE (-1)

#define CHS_DOC_VALUES     0x1u
#define CHS_DOC_TRANSFORMS 0x2u
#define CHS_DOC_DEFAULTS   0x4u
#define CHS_DOC_ALL (CHS_DOC_VALUES | CHS_DOC_TRANSFORMS | CHS_DOC_DEFAULTS)

char * chs_row (const chs_schema *s, int format, const char *raw,  size_t raw_len,  const char *settings_json);
char * chs_rows(const chs_schema *s, int format, const char *body, size_t body_len, const char *settings_json,
                int export_format, unsigned doc_flags, chs_bytes *out_bytes);
```

Both return a malloc'd JSON document (free with `chs_free`), never `NULL` in
normal operation; a `NULL` return means the loaded artifact does not export the
function (see `artifact.md`). `raw` / `body` are counted, not NUL-terminated —
binary formats contain NUL bytes, and a binding MUST pass the length rather than
relying on `strlen`. `settings_json` may be `NULL` or `"{}"`.

`chs_rows` is **not `chs_row` in a loop**, and a binding MUST NOT implement it as
one: row separation is format-specific (a quoted CSV field can contain a
newline), and `input_format_allow_errors_num` / `_ratio` decide whether a bad row
is skipped or aborts the batch. `chs_rows` is also the unit of the volatile-DEFAULT
clock guarantee: **one batch is one clock instant.**

The three revision-3 parameters (`docs/proposals/rows-export.md` is the
settled proposal; this section is normative):

* **`export_format`** — `CHS_EXPORT_NONE` (`-1`), or an `enum chs_format`
  value this artifact can SERIALIZE. This revision serializes exactly one:
  `CHS_JSON_COMPACT_EACH_ROW` (`4`). Any other value — including format
  integers the enum defines but the export path does not yet write — answers
  the whole call `{"outcome":"unsupported","code":-2,…,"rows":[]}` naming the
  value, and processes nothing. Loud, never silent: proceeding without bytes
  would let a caller believe an empty export was an empty batch.
* **`doc_flags`** — a bitmask choosing which document GROUPS the per-row
  documents carry (§Document flags below). `CHS_DOC_ALL` reproduces the
  revision-2 document byte-for-byte. A bit outside `CHS_DOC_ALL` answers the
  same `unsupported` shape as an unknown `export_format` — a future flag must
  never silently mean nothing.
* **`out_bytes`** — the export channel. When non-`NULL` it is always
  initialized to `{NULL, 0}` at entry. Required (non-`NULL`) whenever
  `export_format != CHS_EXPORT_NONE`; `NULL` with an export format requested
  is the same loud `unsupported` answer. On success `out_bytes->data` is
  malloc'd by the library — free it with **the same library's** `chs_free` —
  and `out_bytes->len` counts it. `data` is NOT NUL-terminated as a contract
  (the length is the contract), and a binding MUST copy by length.

`chs_row` keeps the revision-2 signature and behaves exactly as
`doc_flags = CHS_DOC_ALL`, no export. A single row has no batch to export.

#### The export channel (`export_format != CHS_EXPORT_NONE`)

The exported bytes are **the batch's accepted rows, serialized once by the
transcription of ClickHouse's own `JSONCompactEachRow` output writer, into one
contiguous buffer**. The writer driven is
`JSONCompactEachRowRowOutputFormat` — vendored
`src/Processors/Formats/Impl/JSONCompactEachRowRowOutputFormat.cpp`, whose row
machine is byte-identical on every vendored era (24.8 `:28-56`, 25.8 and 26.7
`:27-55`): `writeRowStartDelimiter` = `[`, `writeFieldDelimiter` = `", "`,
`writeRowEndDelimiter` = `]\n`, and `writeField` =
`serialization.serializeTextJSON(column, row, out, settings)` — or, under
`settings.json.serialize_as_strings`, `serializeText` into a buffer and
`writeJSONString` of it (`:29-37` on 25.8/26.7, `:30-38` on 24.8). The values
go through ClickHouse's own serializers under the call's resolved
`FormatSettings` (so `output_format_json_quote_64bit_integers` and friends
apply exactly as a server's `SELECT … FORMAT JSONCompactEachRow` applies
them); the frame is those four punctuation tokens, transcribed with the
citations above — the same posture as `splitBody`'s reader transcriptions
(`docs/json-framing.md`). Nothing is hand-formatted; the fixture rig
(`chtypes-core/tests/fixtures/export/`, this cycle) asserts byte-identity against
server-written bytes.

Emission rules, all normative:

* **Columns are `wireColumnOrder`**: the declared columns minus
  `MATERIALIZED` / `ALIAS` / `EPHEMERAL`, in declaration order — the same
  wire tuple every positional format addresses. The exported bytes are
  directly INSERT-able with no column list.
* **Volatile DEFAULTs are already substituted** (one clock per batch), so
  preview == stored by construction; the bytes carry the substituted values.
* **The stored block is always collected when an export is requested** — the
  batch loop's collect conditional is `engine || TTL || export`.
* **Bytes are emitted only when the batch verdict is exactly `"accepted"`.**
  A `rejected` or `unsupported` batch stores nothing; an `accepted_poisoned`
  batch contains a value ClickHouse itself cannot read back, which no writer
  can honestly serialize. In every withheld case the document says why (below)
  and `out_bytes` stays `{NULL, 0}`. Skipped rows under
  `input_format_allow_errors_*` do NOT withhold the batch — the verdict is
  still `accepted` and the skipped rows occupy zero-length spans.
* **The full-arity guard, fail-closed**: if any accepted row's stored block
  lost a wire column (the known poison arms can do this), the batch exports
  NO bytes and the document carries the reason — the same posture as the
  engine path's heterogeneous-columns guard (`rows materialized different
  column sets`). Never emit a row with silently absent columns.
* A serialization failure while writing, and
  `output_format_json_validate_utf8 = 1` (which re-shapes the byte stream
  through `WriteBufferValidUTF8`, outside the transcribed frame), also
  withhold the bytes with the reason. Declines here are `-2`-class honesty,
  not server verdicts.

**Row addressing** — when bytes are emitted the batch document gains
`"row_spans"`: one `{"off":N,"len":N}` object per `rows[]` entry,
**index-aligned** with the verdict entries. A non-accepted row
(rejected / skipped / unsupported) carries `{"off":0,"len":0}`. A span covers
the row's complete output line *including* its terminating `\n`, so slicing
spans out of `out_bytes` IS the per-row payload, each slice is itself a valid
one-row `JSONCompactEachRow` body, and the concatenation of all non-zero
spans reproduces `out_bytes` exactly (batches merge by byte concatenation).
Under `allow_errors` resync tails, span↔input-row correspondence follows the
document's row order — the itemized-skips contract (§`"outcome":"skipped"`).

When an export was requested and withheld, the document instead carries
`"export_declined": "<reason>"` and no `row_spans` key. When no export was
requested, neither key exists and the document is **byte-identical** to
revision 2's (the zero-movement guarantee). Two edges, stated: a CALL-level
rejection (an unknown setting's `115`, an unsplittable body, a binary decode
fault) rejects before any export machinery runs — the batch verdict itself is
the reason, no `export_declined` key is added, and `out_bytes` stays the
`{NULL,0}` it was initialized to. And an accepted batch with zero accepted
rows (an empty body, or every row skipped) is **emitted-empty**: `row_spans`
carries its zero-length entries, and `out_bytes` comes back with non-`NULL`
`data` and `len == 0` — distinguishable from a decline's `{NULL,0}`.

#### Document flags (`doc_flags`)

The **verdict channel is always emitted and is not a flag**: the batch
`outcome`/`code`/`err`, `rows_read`, `rows_skipped`, per-row
`outcome`/`code`/`err`, `unsupported_settings` (it changes how an answer may
be scored), `engine_rows` and `storage_transforms` (a row the storage layer
drops is a verdict about that row). Flags thin the *description*, never the
*verdict*.

| bit | carries |
|---|---|
| `CHS_DOC_VALUES` | the full `cols[]` array (per-column stored text and value provenance) and `unknown_fields` |
| `CHS_DOC_TRANSFORMS` | the change-detection channel: the reference second-parse (`ref`/`ref_type`) and the TSV wire round trip (`wire`) run and are emitted; with `CHS_DOC_VALUES` off, `cols[]` is filtered to the entries a change detector could fire on (below) |
| `CHS_DOC_DEFAULTS` | `computed[]` (MATERIALIZED values) and, with `CHS_DOC_VALUES` off, the `cols[]` entries with `src == "default_substituted"` (the volatile-DEFAULT substitutions a caller must echo into its INSERT) |

`doc_flags = CHS_DOC_ALL` is today's document, byte-for-byte.
`doc_flags = 0` is "lean": `"cols":[]`, no `unknown_fields`, no `computed`,
verdicts intact. Absent keys are the additive-safe shape
(`docs/reference/bindings.md` §RowResult: every field is optional-with-a-default).

**`CHS_DOC_TRANSFORMS` without `CHS_DOC_VALUES` — the retention rule.** The
document keeps a `cols[]` entry unless it is **provably change-free by raw
byte equality**: `src == "input"`, not poisoned, not `dup_dropped`, no
`ref_unclassified`, no `wire` disagreement, the input bytes equal the stored
JSON text exactly, and the reference parse (when one applies) equals the
stored text exactly. Entries with `src == "skipped"` are never retained (no
value exists to have changed). Everything else — every other `src`, every
byte difference, every case the equality test cannot vouch for — is retained
with its **full field set**, so an SDK derives `Transformed` from the
retained entries with the same detectors it runs on a full document
(`docs/reference/bindings.md` §Transformed): nothing is classified C-side, no reason
strings move into the library, and over-retention costs bytes while
under-retention is forbidden. The guarantee a binding may rely on: **a column
any spec'd detector would fire on is always retained.**

**The cost asymmetry, both directions** (the proposal's own statement, kept):
`CHS_DOC_VALUES` without `CHS_DOC_TRANSFORMS` **skips the reference
second-parse and the wire round trip** — real C-side compute saved; such a
document carries `"ref":null,"ref_type":""` and no `wire`, and detector 2/3
cannot run over it. `CHS_DOC_TRANSFORMS` without `CHS_DOC_VALUES` still
performs the reference parse and the byte comparisons internally (detection
needs the rendered values); it saves only document size and SDK-side parsing,
never C-side compute.

### Filters (`chs_filter_compile` / `chs_filter_free` / `chs_filter_rows`) — phase 2, revision 4

```c
typedef struct chs_filter chs_filter;
chs_filter * chs_filter_compile(const chs_schema *s, const char *expr_sql,
                                const char *params_json,
                                int *out_code, char **out_err);
void         chs_filter_free(chs_filter *f);
char *       chs_filter_rows(const chs_filter *f, int format, const char *body,
                             size_t body_len, const char *settings_json);
```

One boolean SQL expression over one compiled schema's columns, evaluated per
row of a body in any input format the schema parses. The engine is not new
machinery: `chs_filter_compile` runs **the same two-stage pipeline the
CONSTRAINT … CHECK path already runs** — and cites — in
`lib/csrc/chtypes.cpp` (§CONSTRAINT compile, the `TreeRewriter(
g_global_context).analyze(expr, phys)` + `ExpressionAnalyzer(…).getActions(
false)` pair over the schema's PHYSICAL columns, ordinary + MATERIALIZED),
which is itself the server's own constraint pipeline (vendored
`src/Storages/ConstraintsDescription.cpp`, `:145-148` on 25.8; 26.7
restructures to `getActionsDAG` with the same old analyzer, `:156-166`). An
expression naming an `ALIAS` or `EPHEMERAL` column therefore fails at compile
with ClickHouse's own `UNKNOWN_IDENTIFIER`, exactly where a real CREATE
fails. The expression text is parsed by ClickHouse's own `ParserExpression`
under the server's own limits (`DBMS_DEFAULT_MAX_QUERY_SIZE`,
`DBMS_DEFAULT_MAX_PARSER_DEPTH`, `DBMS_DEFAULT_MAX_PARSER_BACKTRACKS`).

Because comparison semantics come from executing ClickHouse's own comparison
functions (`FunctionsComparison.h` — supertype promotion, const-string
conversion, the whole per-version trichotomy), the filter answers
**WHERE-side** semantics by construction: `x = 256` over a `UInt8` column is
`'f'` for every row — the constant promotes, it never wraps to `x = 0`. This
is the same measured behavior the CHECK path already exhibits on 24.8 / 25.8
/ 26.7, and it is why a caller MUST NOT fold predicate constants through the
insert-side coercion instead (`docs/reference/bindings.md` §Constants are not payloads).

#### Query parameters (`params_json`) — revision 4

`expr_sql` may contain `{name:Type}` query parameters. `params_json` is a
JSON object of parameter name → **value string** (the `settings_json`
convention — every value a string, exactly as the server's own parameter
channels carry them); `NULL` or `"{}"` declares none. Substitution is the
server's own, called as an object rather than transcribed: the vendored
`ReplaceQueryParameterVisitor` (`src/Interpreters/ReplaceQueryParameterVisitor.cpp` —
constructor `(const NameToNameMap &)` + `visit(ASTPtr &)`, the same public
surface on every vendored tag 24.8–26.7) runs over the parsed AST **before
analysis**, exactly where `executeQuery.cpp` runs it on a real server
(25.8 `:1164`). Per parameter, the visitor deserializes the value string via
the **declared type's own `deserializeTextEscaped`** and injects the result
as a typed literal (`addTypeConversionToAST`; a plain literal for `String`) —
25.8 `ReplaceQueryParameterVisitor.cpp:111-168`.

Consequences, each the server's, none invented:

* **Injection safety is by construction, not by escaping.** A value is
  parsed as the declared type and becomes a literal *after* SQL parsing; it
  is never SQL text. A hostile `String` value (`' OR 1=1 --`, backticks,
  quotes) compares as exactly that string (probed live, 25.8.28.1,
  2026-08-31: `s = {v:String}` with that value answers 1 against a column
  holding the literal, and the expression is untouched).
* **WHERE-side semantics survive substitution**: `x = {limit:UInt8}` with
  `limit=200` over a `UInt8 x` answers true only where `x` is 200 (probed
  live over HTTP, same day: x=200 → 1, x=1 → 0).
* **An unbound parameter** is the server's own refusal: the visitor throws
  `UNKNOWN_QUERY_PARAMETER` (**456**, "Substitution `name` is not set") —
  probed live, byte-identical message. The compile returns `NULL` with 456
  in `*out_code`, like any other server refusal. The revision-3 behavior
  (every `{name:Type}` declined `-2` "render literals") is **replaced**: an
  expression with parameters and no binding now gets the server's 456, not
  a decline.
* **A value the declared type cannot parse completely** is the server's own
  `BAD_QUERY_PARAMETER` (**457**, "Value … cannot be parsed as … because it
  isn't parsed completely") or the serialization's own error — probed live:
  `abc` and `5x` as `UInt8` both answer 457.
* **A bound name the expression never uses is ignored**, as a live server
  ignores an unused `param_*` (probed on both channels: TCP
  `--param_unused=5` and HTTP `?param_unused=5`, both silently fine).

One **transport caveat**, documented rather than modeled: on a real server's
**TCP** channel, query parameters travel inside a `Settings` block
(`TCPHandler.cpp:2112-2114` on 25.8), so a parameter whose *name* collides
with a builtin setting name (`limit`, `offset`) fails at the protocol layer
with code 26 — even when the query never uses it (probed live). Over HTTP
the same name works normally. This library takes name→value pairs directly
and therefore behaves like the HTTP/substitution semantics; the TCP quirk is
the transport's, not the substitution's, and stays a documented divergence.

**Handle identity is per-(schema, expr, params).** The compiled handle bakes
the substituted values in (its canonical expression text is formatted
*after* substitution); changing a value means compiling a new filter. That
is the server's own model — it substitutes then re-analyzes per value set —
and it prices per-principal filters at ~13 µs / ~2 KiB each. **A caller
compiling filters from tenant-influenced values MUST bound its cache and its
compile rate**: the (schema, expr, params) key is attacker-influencable
through claim values, so an unbounded cache is a memory DoS and an unmetered
compile path is a CPU DoS. Normative guidance: a bounded LRU keyed on
(schema generation, expr, params-hash) plus a per-principal compile
throttle; the reference SDKs adopt this shape when they grow a params
surface.

#### Refused at compile

Refused (`-2`, `CHS_CODE_UNSUPPORTED`), never guessed:

* **Non-deterministic expressions** — the same `scanDefaultExpr` scan the
  CHECK and TTL paths run: clock reads (`now()`, `today()` — so
  `now() > ts` is `-2`), `rand()`, server-constants (`hostName()`),
  stateful/insertion-order functions. The server would evaluate these with
  ITS values; any verdict computed here could silently disagree. The scan
  runs AFTER parameter substitution, so a parameter value can never smuggle
  one in (a value is a literal by construction).
* A subquery-shaped expression (`x IN (SELECT …)`) fails compile with a real
  ClickHouse error (there is no database here); JOIN-shaped filters are
  permanently out of scope — this library has no storage.

Genuine expression errors (unknown identifier, unknown function, a
`NO_COMMON_TYPE` the analyzer itself raises) return `NULL` with ClickHouse's
own code and message in `*out_code` / `*out_err` (free with `chs_free`), like
`chs_schema_compile`.

**Handle lifetime — normative.** A `chs_filter` REFERENCES its `chs_schema`
handle; it does not copy it and does not refcount it. The caller MUST keep
the schema handle alive for the whole life of every filter compiled from it,
and MUST free filters (`chs_filter_free`) before freeing their schema
(`chs_schema_free`). Freeing the schema first is use-after-free — undefined
behavior, not a reported error. Refcounting was considered and rejected for
this revision: it would change `chs_schema_free`'s semantics for every
existing caller in the same cycle that changes `chs_rows`, and the SDKs
(which own both lifetimes) can enforce the ordering structurally.
Invalidation on schema refresh is therefore the caller's job: recompile
filters when the schema handle is replaced.

**Thread-safety.** The per-handle rule, twice over: a single `chs_filter *`
MUST NOT be used from two threads at once, and a `chs_filter_rows` call **is
also a use of the filter's schema handle** (row parsing and DEFAULT
resolution run through it) — so two `chs_filter_rows` calls on *different*
filters over the *same* schema handle must not run concurrently either.
`chs_filter_compile` likewise uses the schema handle. Distinct schema handles
remain fully concurrent.

Evaluation runs under the **same admission envelope as DEFAULT/CHECK/TTL
evaluation** (`EvalBudgetScope`; `chtypes_default_eval_memory_bytes`,
`chtypes_default_eval_wall_nanos`): a filter is tenant-authored code exactly
like a DEFAULT, and a row that trips the envelope declines rather than
evaluates unboundedly in the gateway.

#### The filter result document (`chs_filter_rows`, and `chs_filter_eval` — see §Blocks)

```json
{"outcome":"ok","code":0,"err":"","rows_read":4,
 "unsupported_settings":[],
 "verdicts":"tfde",
 "errors":[{"row":2,"code":27,"err":"Cannot parse input: …"},
           {"row":3,"code":386,"err":"There is no supertype for types String, UInt16 …"}]}
```

| Field | Meaning |
|---|---|
| `outcome`, `code`, `err` | the CALL verdict: `"ok"` when evaluation completed (per-row failures live in `verdicts`, not here); `"rejected"` with ClickHouse's code for a call-level failure (an unknown setting name's `115`, an unsplittable body's framing error, a binary decode fault, an unknown format); `"unsupported"` (`-2`) for a call-level decline. On anything but `"ok"`, `verdicts` is `""` and `errors` is `[]` — a malformed body yields no partial answers. |
| `rows_read` | rows the splitter/decoder yielded |
| `unsupported_settings` | as everywhere: non-empty ⇒ the answer MUST NOT be scored as agreement |
| `verdicts` | one character per row, **in input order**, index-aligned with the rows the reader consumed |
| `errors` | one entry per `'e'` or `'d'` row, carrying that row's code and message verbatim; `[]` otherwise |

The verdict characters — transcribed from server behavior, each with its
source:

* **`'t'` — the predicate is true for this row.** Truth is the CHECK path's
  own rule, executed by the same code: the result column's value is non-NULL
  and non-zero (`lib/csrc/chtypes.cpp` per-row CONSTRAINT eval — "Nullable
  (UInt8) results: NULL is not true"; the server's
  `CheckConstraintsTransform` applies the identical rule to a `Nullable`
  CHECK result, and `WHERE` treats NULL as not-true).
* **`'f'` — false OR NULL.** SQL's three-valued logic collapsed at the WHERE
  boundary, exactly as a `SELECT … WHERE` hides a NULL-predicate row. A
  filter over `x = 1` with `x` NULL answers `'f'`; `NOT (x = 1)` with `x`
  NULL also answers `'f'` (NOT propagates NULL) — the vendored functions
  compute this, nothing here models it.
* **`'e'` — the predicate evaluation THREW on this row's values.** The class
  the adversarial review measured: `s String` with `s = 257` **compiles**
  (the analyzer is happy) and then throws `NO_COMMON_TYPE` (386) on every
  row; a date compare against an unparseable constant throws 41. **On a real
  server, a `WHERE` whose predicate throws on any row fails the WHOLE query
  with that error** — there is no per-row error channel in a SELECT. The
  honest per-row mapping is this distinct verdict carrying the server's real
  code and message in `errors[]`: a caller enforcing security MUST fail
  closed on `'e'` (hide the row / fail the request), because the server
  would have answered nothing at all here, not `false`.
* **`'d'` — this library declines to answer for this row.** Three causes,
  each itemized in `errors[]`: the row cannot be parsed/coerced under the
  schema (its `chs_rows` outcome would be `rejected`, `skipped`,
  `unsupported` or `accepted_poisoned` — a row that cannot exist in the
  table has no WHERE truth, and a poisoned value is unreadable by the server
  itself); the evaluation tripped the admission envelope (memory/wall — the
  server would have evaluated it, so this is `-2`, never a fabricated `'e'`
  or `'f'`); or the row's parse declined for a chtypes-side reason. A
  security-enforcing caller fails closed on `'d'` exactly as on `'e'`.

**Batch semantics differ from `chs_rows`, deliberately.** A filter has no
INSERT to abort: every row is evaluated independently, a bad row declines
(`'d'`) and evaluation continues — `input_format_allow_errors_*` does not
apply (declines are already itemized). After a parse-failed TEXT row the
tail is re-split from where the reader stopped, by the SAME `syncAfterError`
transcription the `chs_rows` skip path runs (the JSONEachRow continuation
machine included), so verdict indexes keep matching input rows — the
itemized-skips addressing contract. The framing-suffix verdict (`readSuffix`
— an unclosed array) is pronounced after the row loop, as the server
pronounces it, and fails the whole call: a malformed body yields no partial
answers. Binary/Native/Buffers bodies whose decode faults reject the whole
call (there is no row addressing inside a broken binary stream), mirroring
`chs_rows`.

Volatile DEFAULTs resolve against **one clock instant per call**, the same
rule as `chs_rows`, with the same `chtypes_*` clock keys honored.

### Blocks (`chs_block_parse` / `chs_block_free` / `chs_filter_eval`) — phase 2, revision 4

```c
typedef struct chs_block chs_block;
chs_block * chs_block_parse(const chs_schema *s, int format, const char *body,
                            size_t body_len, const char *settings_json,
                            int *out_code, char **out_err);
void        chs_block_free(chs_block *b);
char *      chs_filter_eval(const chs_filter *f, const chs_block *b);
```

The parse-once / eval-many twin — the call shape that makes live SSE viable:
the hot path's batchable dimension is K filters × 1 event, and phase 1's
`chs_filter_rows` re-parses the body per filter, so the ~20 µs parse
dominated. `chs_block_parse` runs **the same machinery `chs_filter_rows`
runs** (literally: since this revision `chs_filter_rows` is implemented as
parse-into-block + evaluate, and `chs_block_parse` / `chs_filter_eval` are
those two halves exported) — body split, per-row parse + coercion, the
`syncAfterError` resync after a bad text row, MATERIALIZED columns filled
(`evalMissing`, the CHECK path's pre-eval), per-row parse failures recorded
IN the block with their code and message — once, minus document emission.
`chs_filter_eval` then evaluates one compiled filter over the already-parsed
block and returns **the same result document `chs_filter_rows` returns**,
same fields, same verdict characters `'t'`/`'f'`/`'e'`/`'d'`, same `errors[]`
rule (a row recorded as unparseable at parse time answers `'d'` with the
recorded error).

**The equivalence contract, normative:**
`chs_filter_eval(f, chs_block_parse(s, format, body, len, settings)) ≡
chs_filter_rows(f, format, body, len, settings)` for every verdict class,
with one stated qualification: volatile DEFAULTs resolve against **the parse
call's** clock instant (a block is parsed once, under one instant, exactly
as one `chs_filter_rows` call resolves once), so across separate calls the
two sides agree exactly when the clock is pinned
(`chtypes_now_epoch_nanos`) or the schema has no volatile DEFAULT.

Call-level failures — an unknown setting name's 115, an unsplittable body,
a binary/Native/Buffers decode fault, the deferred JSONEachRow framing
verdict — fail `chs_block_parse` (returns `NULL`, code and the server's own
message in `*out_code` / `*out_err`, both optional, message freed with
`chs_free`), mirroring the calls `chs_filter_rows` rejects wholesale: a
malformed body yields no block and no partial answers. `settings_json` here
is the PARSE-side settings map (format settings, clock keys); evaluation
itself takes no settings — it is a pure function of (filter, block).

**Lifetime.** A `chs_block` REFERENCES its schema handle — the same
non-owning rule as filters: keep the schema alive for every block parsed
from it, free blocks (`chs_block_free`) before their schema. A block may be
evaluated by MANY filters, sequentially, and evaluation does not consume or
mutate it. Filter and block MUST come from the SAME schema handle:
`chs_filter_eval` on a (filter, block) pair from different schema handles
answers a rejected document (code 1002) — refused loudly, never undefined.

**Threads.** A `chs_filter_eval` call is a use of BOTH handles: the filter's
per-handle rule applies (and a filter use is also a use of its schema
handle, as everywhere), and the block must not be in use by another thread —
one block must not be evaluated by two threads at once, and K filters over
one block run sequentially. `chs_block_parse` is a use of the schema handle,
like any parse. Distinct schema handles (with their own filters and blocks)
remain fully concurrent.

Measured on this cycle's smoke (macOS dev floor, 25.8 artifact, K=8 filters
over a 1-row block, 2026-08-31): **~11-13 µs per `chs_filter_eval`**
(10.85 µs warm long-running process; 12.7-13.2 µs C driver under residual
build load) versus ~34-80 µs per `chs_filter_rows` on the same one-row
event — the re-parse is shed as designed (measured speedup 2.6-7.4×,
load-dependent). Stated honestly rather than rounded down: per-eval is NOT
the ~1.5 µs bare per-CHECK `execute` delta the phase-1 analysis measured,
because each eval carries fixed per-call costs the bare delta does not —
the admission-budget scope (thread MemoryTracker setup), the Block copy,
and the result-document malloc + emission. A caller needing the critique's
~0.8 µs/subscriber-class parity bar does not get it from this call shape
yet; the candidate next step (NOT this revision) is a K-filters-per-call
batched eval or a verdict-only output channel, which would amortize those
fixed costs too.

#### The enforcement gate

**NOTHING may enforce read-side security on this API until the WHERE-truth
rig gates green** — zero over-admit (the library shows a row the server's
WHERE hides: a confidentiality breach) and zero over-hide (silent
suppression) budgets, per version, baseline-gated like every other rig.
Until that run of record exists, `chs_filter_rows` AND the block twin
(`chs_filter_eval`) are a shadow/replay surface: log disagreements, enforce
with whatever enforced yesterday. The twin makes the live-SSE call shape
*available* this revision; it does not open enforcement one day earlier. One
former blind spot is now measured rather than feared: a non-UTC truth column
(servers pinned to America/New_York, the library's timezone plumbed to
match) joined the rig on 2026-08-31 — the tz dependence proved real (14/79
date-comparison truths move, and the NY truth itself shifts at 26.6) and the
library matched it 100.00% on both platforms, per era; the residue on the
rig's ledger is DST-transition instants, additional zones, and wiring
server-timezone discovery into filter compile so a caller cannot fail to
pass the zone. The other known limit stands: the WHERE
path on a modern server runs the new analyzer while this pipeline is the old
one — both bottom out in the identical function objects, divergence lives in
rewrite passes, and **a divergence family found by the rig gets documented,
not patched with a hand-written rule** (the standing divergence rule; there
is no analyzer "fallback").

## Thread-safety

Stated by the header and honored by the reference implementation:

- The library is thread-safe for concurrent `chs_row` / `chs_rows` calls **on
  distinct handles**.
- A single `chs_schema *` MUST NOT be used from two threads at once.
- A `chs_filter *` follows the same per-handle rule, and a filter call is
  also a use of its schema handle — see §Filters.
- A `chs_block *` follows the same per-handle rule; `chs_block_parse` is a
  use of the schema handle, and a `chs_filter_eval` call is a use of BOTH
  its handles (filter — and through it the schema — and block) — see
  §Blocks.
- `chs_init` and `chs_set_default_settings` mutate per-library process state and
  MUST be serialized against all other calls.

The reference implementation is deliberately more conservative than the ABI
requires, and a binding may want to be too: the statically linked path
(`CompiledSchema`) takes one mutex per schema handle, and the `dlopen`'d path
(`Library` / `LoadedSchema` in `multiversion.go`) takes one mutex **per loaded
library** around every call including `chs_schema_compile` and `chs_free`. That
coarser lock is a correctness-first choice, not an ABI requirement; a binding
that wants per-handle parallelism inside one version MUST first satisfy itself
that its own allocator interactions are safe, and MUST prove it by running the
rigs, not by reasoning.

## Error model

Four distinct outcomes travel in the same fields, and conflating any two is a
scoring error:

| Situation | How it appears |
|---|---|
| ClickHouse rejects | `code` = a real ClickHouse error code (`27`, `117`, `376`, `455`, `50`, …), `outcome` = `"rejected"` |
| ClickHouse accepts but the value cannot be read back | `outcome` = `"accepted_poisoned"`, `code` = `691`. This is an **accepted** insert. |
| This build refuses to answer | `code` = `CHS_CODE_UNSUPPORTED` = **`-2`**, `outcome` = `"unsupported"` |
| ClickHouse skips the row and continues the batch (`input_format_allow_errors_*`) | `outcome` = `"skipped"` on the row's entry inside a batch's `rows`, `code`/`err` = the caught error, verbatim. Batch-level only — see §The batch result document. |

`CHS_CODE_UNSUPPORTED` is `-2` — defined in `chtypes.h`, surfaced in Go as
`chtypes.CodeUnsupported`, and it is **never** a real ClickHouse error code. It
means "a real server might well have accepted this; I decline to guess". A
binding MUST expose it as a distinguishable sentinel and MUST NOT map it onto a
rejection: mapping it to a rejection manufactures an over-reject that the product
never made, and mapping it to an acceptance manufactures an over-accept. Both
manufactured verdicts are zero-budget failures and neither is ranked behind the
other — the over-accept is a phantom accept the real insert catches loudly, the
over-reject drops a row the server would have taken and tells nobody.

Things that are `unsupported` rather than rejected, by design:

- A DEFAULT that is a property of the **server** (`hostName`, `version`,
  `uptime`, `timezone`, `serverUUID`, `getMacro`, `getScalar` — everything
  ClickHouse itself marks `isServerConstant`), of the **session**
  (`currentUser`, `currentDatabase`, `getSetting`), or of **insertion order**
  (`blockNumber`, `rowNumberInAllBlocks`).
- A DEFAULT that would block (`sleep`, `sleepEachRow`). Refused rather than
  awaited — a tenant-authored `DEFAULT sleep(3)` is otherwise a one-line denial
  of service (measured 2.18 s of real time per case). Bounded by upstream's own
  `function_sleep_max_microseconds_per_block = 1`, so nothing is ever parked.
- A DEFAULT whose evaluation exceeds an admission budget (below).
- A table engine or sorting key `chs_schema_engine` does not model, and a TTL
  form `chs_schema_ttl` refuses (`WHERE` / `GROUP BY` TTLs, `TO DISK`/`VOLUME`
  moves, `RECOMPRESS`, and any clock-reading TTL expression).
- A volatile DEFAULT the caller's clock-skew budget forbids substituting.

Observed, verbatim, on `25.8`:

```
DDL: h String DEFAULT hostName()
→ code -2, "DEFAULT hostname() is a property of the ClickHouse server, not of
   the client: resolving it here would store the gateway's answer"

DDL: b UInt8 DEFAULT range(400000000)[1]
→ code -2, "DEFAULT for column b exceeded the admission memory budget
   (268435456 bytes, chtypes_default_eval_memory_bytes); the schema is refused
   at admission so no row ever evaluates it"

TTL: now() + INTERVAL 1 DAY
→ code -2, "TTL expression now() + toIntervalDay(1) does not depend on any of
   the columns of the table"
```

## Settings

`settings_json` is a JSON **object** of ClickHouse format/query settings, passed
through to the vendored server code, plus the **six** keys chtypes itself
recognizes — a closed set, enumerated below, not an open `chtypes_` namespace
(see Settings rule 2).

### Rules

1. **All values MUST be JSON strings.** The reference implementation marshals a
   `map[string]string`, so every value crosses as a string, and the C side parses
   from text. This is not cosmetic: `chtypes_now_epoch_nanos` is a 19-digit
   nanosecond epoch, which does not survive an IEEE double. A caller that
   serializes `1700000000123456789` as a JSON *number* through a float — which is
   what a JavaScript `number`, a Python `json.dumps` of a float, or Go's
   `fmt.Sprint` of a decoded `any` will do — sends `1.7e+18`, and the setting is
   **silently ignored**. Verified both ways: as a JSON number the batch instant
   stayed the real wall clock; as a JSON string it pinned exactly.
2. **An unknown setting name rejects the whole call with the server's own
   code 115** (changed 2026-08-18; it was previously swallowed silently). The
   name gate is the server's own — `AccessControl::checkSettingNameIsAllowed`
   behind `SettingsConstraints::checkImpl`'s alias resolution, `profile`
   exemption and did-you-mean hint — so `{"made_up_setting_xyz":"1"}` returns
   the CALL-level rejection a real server answers for the whole query:
   `{"outcome":"rejected","code":115,"err":"Setting made_up_setting_xyz is
   neither a builtin setting nor started with the prefix 'SQL_' registered for
   user-defined settings","cols":[]}` (`"rows":[]` from `chs_rows`). Details
   that are the server's, mirrored exactly:
   - **Obsolete names are still declared**, so servers accept them silently —
     the library does too (e.g. `allow_experimental_geo_types`).
   - **Custom-prefixed names are legal and inert.** The registered prefixes
     start as the version's own shipped `config.xml` value — `SQL_` on every
     vendored era, the config the stock oracle containers run — and a
     deployment whose servers register different prefixes declares the same
     comma-separated string once via
     `chs_set_default_settings({"chtypes_custom_settings_prefixes":"custom_"})`,
     mirroring the server's `custom_settings_prefixes` element (absent key =
     registration unchanged, exactly like `setUpFromMainConfig`).
   - `chs_set_default_settings` runs the same gate over its whole payload and
     **refuses wholesale**: nonzero (the ClickHouse code, `115`) back and
     nothing committed — a gateway can never believe a default profile is in
     force when part of it never applied.
   - `session_id` / `session_timeout` are exempt on both channels: they are
     HTTP-interface parameters that ride the same map, not query settings.
   - **The `chtypes_` prefix is NOT an exemption; the six documented keys are.**
     A name in the library's own reserved namespace that is not one of the six
     — `chtypes_clock_offset_nano` for `..._nanos`, say — is a name nothing
     knows, and it takes the same 115 as any other unknown name, on every
     channel (`chs_set_default_settings` refuses the payload wholesale;
     `chs_row` / `chs_rows` reject the call; `chs_schema_compile` fails the
     compile, where *every* `chtypes_*` key already landed). Corrected
     2026-08-26: the branches that consumed the six used to test the prefix, so
     an unrecognized reserved name was filed under `unsupported_settings` and
     the row **admitted** — an over-accept the arbiter measured on all 35
     (implementation, version) pairs. See
     `docs/proposals/chtypes-prefix-overaccept.md`.
3. `unsupported_settings` in the result document is for settings the build
   *models but declines* — a KNOWN name whose value this build cannot honor —
   and when non-empty the answer MUST NOT be scored as agreement (the reference
   implementation promotes such a row to `Unsupported`).

### The six chtypes settings

**These six are the whole of the reserved namespace.** `lib/csrc/chtypes.cpp`'s
`isRecognisedChtypesSetting` is the list, and it is what each settings channel
tests — never the `chtypes_` prefix. Anything else spelled `chtypes_*` is an
unknown setting name and is answered as one (Settings rule 2). Three of the six
are per-CALL and three are per-PROCESS; a per-process key handed to `chs_row` /
`chs_rows` is recognized but not honored there, and comes back on
`unsupported_settings` — a decline, never an admission.

Volatile-DEFAULT clock control — the caller's entire interface to clock skew,
documented in the `chtypes` package doc and accepted on `settings_json` alongside
ClickHouse's own:

| Key | Meaning |
|---|---|
| `chtypes_now_epoch_nanos` | Pin the batch instant outright. Tests, replay, and anything that must be reproducible. |
| `chtypes_clock_offset_nanos` | The caller's *measured* `(server − client)` offset, added to every clock read. |
| `chtypes_max_clock_skew_nanos` | Refuse to substitute a volatile DEFAULT when `\|offset\|` exceeds this. `0` = no budget. |

With none of them set, **tolerated skew is unbounded** and the library stamps
whatever the local clock says. Past the budget the row comes back `unsupported`,
which is the only safe direction: a substituted timestamp too far in the *past*
under a `TTL` is accepted, previewed as accepted, and then **silently deleted at
merge time**, with no error at any point. Verified:

```
settings {"chtypes_clock_offset_nanos":"5000000000","chtypes_max_clock_skew_nanos":"1"}
→ outcome "unsupported", code -2,
  "client clock offset exceeds chtypes_max_clock_skew_nanos; refusing to
   substitute a volatile DEFAULT rather than store a timestamp the server would
   not have written"
  and the column's src is "default_volatile_unresolved"
```

Admission budgets — bounds on evaluating tenant-authored DEFAULT and TTL
expressions in this process. **Process-wide, via `chs_set_default_settings`
only**; admission deliberately takes no per-call settings:

| Key | Default | Effect |
|---|---|---|
| `chtypes_default_eval_memory_bytes` | 256 MiB (`268435456`); `0` disables | Enforced mid-allocation by ClickHouse's own thread-private `MemoryTracker` (code 241, cleanly unwound). |
| `chtypes_default_eval_wall_nanos` | 1 s; `0` disables | Detection after the fact. |
| `chtypes_custom_settings_prefixes` | the version's shipped `config.xml` value (`SQL_` on every vendored era) | Registers the custom-setting name prefixes the unknown-setting gate honors, comma-separated — the server's own `custom_settings_prefixes` config element, applied with the server's own parser. Absent key = registration unchanged. See Settings rule 2. |

A schema whose DEFAULT exceeds a ceiling is refused **at
`chs_schema_compile`**, naming the column and the budget, as
`CHS_CODE_UNSUPPORTED`. A row-dependent DEFAULT (`range(a)`) is cheap at
admission and bounded again per row; a row that trips comes back `unsupported`.
No admission probe can close that hole — `range(4000000000 - a)` inverts any
worst-case value a probe picks.

Timing note, measured 2026-08-17 (25.8, 26.7) and revised 2026-08-19: the
server-level *type gates* (e.g. `allow_experimental_json_type`,
`allow_suspicious_low_cardinality_types`) fire at **row** time when they arrive
by the per-call map or `chs_set_default_settings` — `j JSON` on 24.8 compiles
and then rejects with 44; `lc LowCardinality(UInt32)` compiles and rejects with
455. A binding must not assume a schema compiled with no profile proves the
types are enabled. When a gate is instead DECLARED in a `chs_schema_compile`
profile it fires at **compile**, which is where a real server fires it; see
"Compile-time vs per-call settings" below.

Platform caveat a binding must not paper over: the ceiling covers ClickHouse's
`Allocator`-routed memory (column buffers, arenas, hash tables) on every
platform. On **Linux** the mandatory `new_delete` archive additionally routes
plain `operator new` into the same tracker; on **macOS** those allocations stay
unseen. A DEFAULT that allocates without bound through plain heap objects is
therefore not bounded on macOS. `DEFAULT range(400000000)` was measured at
10.98 GB RSS before the envelope existed.

### Server-level type gates

Several type families are gated at column-creation time by settings that are
properties of *the server the table lives on*, not of the row —
`allow_suspicious_low_cardinality_types`, `allow_experimental_json_type`,
`allow_experimental_time_time64_type`, and friends. A gateway knows these once
and SHOULD declare them in the tenant's `chs_schema_compile` profile, which
is where a real server binds them; failing that it MAY seed them with
`chs_set_default_settings` or repeat them per call, and anything a per-call
settings map sets still wins over the process seed. Verified on `25.8`:

```
lc LowCardinality(UInt32)  with no settings           → rejected, code 455
lc LowCardinality(UInt32)  allow_suspicious_low_cardinality_types=1 → accepted
```

**Where the gate binds** (measured 2026-08-19 on live `25.10.7.6` and
`26.7.3.19` servers, `chprobe`): a server checks a type gate when the COLUMN IS
CREATED and never re-checks it while rows arrive. `CREATE TABLE a1 (x
LowCardinality(UInt32))` under `allow_suspicious_low_cardinality_types=1`
succeeds, and every subsequent `INSERT` — with the gate `0`, or absent, in
Values or JSONEachRow — is accepted. `CREATE` with the gate `0` or absent is
`455`; `ALTER … ADD COLUMN` is `455`. The one exception, also measured, is a
**user-written `CAST` the insert has to evaluate**: a stored `DEFAULT CAST(a AS
LowCardinality(UInt32))` inserted with only `a` supplied is `455`, while
supplying both columns is accepted. That is upstream's own call-site map —
`InterpreterCreateQuery.cpp`, `AlterCommands.cpp` ×2,
`parseColumnsListForTableFunction.cpp` ×2, none on the INSERT path, plus
`CastOverloadResolver.cpp`, reachable from an insert only through a CAST the
tenant wrote.

### Compile-time vs per-call settings (`chs_schema_compile`'s profile)

ClickHouse decides some things once, at `CREATE TABLE`, and other things per
`INSERT`. This ABI mirrors that split exactly, and the precedence rules are
normative:

1. **Compile-time settings are fixed into the handle.**
   `chs_schema_compile(ddl, settings_json, mode, …)` validates the
   declared profile's names through the same server gate every settings
   channel runs (rule 2 above — unknown name ⇒ the server's own `115`,
   nothing compiled; `chtypes_*` keys are per-call/process keys, not
   ClickHouse settings, and land on the same `115`; `session_id` /
   `session_timeout` are exempt as on every channel), then resolves the
   profile and fixes it into the handle. The handle is the compiled table; a
   table's CREATE settings are history and never change for the handle's
   life.
2. **The profile is an overlay, not a replacement** (`mode = 0`, DECLARED —
   the only mode defined). Every name the profile declares takes the caller's
   value; every name it does not declare keeps the library's own *compile
   base*: ClickHouse's build defaults plus the derived permissive type-gate
   list (`lib/tools/gen_type_gate_settings.py`) plus the evaluation envelope.
   A partial profile can therefore admit a schema the server might refuse —
   the row path still re-checks the type gates the profile did NOT declare,
   under per-call settings — but it can never fabricate a rejection.
   `mode != 0` is refused loudly (`-2`) **unconditionally — including when the
   profile is `NULL` or `"{}"`**; a future COMPLETE mode (undeclared names
   taking the build's own server defaults) is reserved and unimplemented.
   (Corrected 2026-08-26: the empty-profile fast path used to return before the
   mode was examined, so `compile(ddl, mode=7)` compiled while
   `compile(ddl, {flatten_nested:0}, mode=7)` declined — the same argument
   answered two ways. The bindings were right to pass the mode through
   unvalidated, per `docs/reference/bindings.md`; the library simply never looked. Found
   by the four `examples/` tours, finding 1.)
3. **The row path resolves THREE settings channels, in ONE order**
   (normative; revised 2026-08-25):

       per-call map  >  handle profile  >  library defaults  >  ClickHouse defaults

   `chs_row` / `chs_rows` build their `Settings` by applying the library
   defaults (`chs_set_default_settings`) first, then the settings the
   handle's compile profile declared, then the per-call map — later writes
   win, so the list above IS the precedence. Format settings,
   `input_format_*`, `date_time_input_format`, the `chtypes_*` clock keys
   are per-INSERT decisions on a real server, so a per-call value always
   beats a declared one; but a caller that declared its gateway's settings
   ONCE at `chs_schema_compile` now gets them on every row call without
   repeating them, which is what "settings settable at DDL" means.

   Before this revision the middle channel did not exist: the row path
   rebuilt its `Settings` from the library defaults and the per-call map
   alone, so a profile declaring `input_format_null_as_default` or
   `date_time_input_format` changed how the schema COMPILED and nothing
   about how a row PARSED — a caller trusting its declared profile silently
   got the build's defaults instead. The two narrow channels that did carry
   the profile to the row path (the declared type gates, rule 5; the
   `Context` the DEFAULT evaluator runs under) are unchanged.

   A per-call settings map still cannot re-shape a compiled handle:
   compile-shape settings passed per call are accepted by the name gate
   (they are legal query settings) and have no row-path consumer. And the
   handle's profile is applied by NAME AND VALUE, exactly as the caller
   wrote it — a name it did not declare is untouched, so the profile can no
   more fabricate a verdict on the row path than it can at compile.
4. **What the profile changes at compile, this revision: `flatten_nested`.**
   At `1` (the default) a `Nested(a,b)` column compiles to its flattened
   `n.a`/`n.b` Array columns, exactly as a profile-less compile does; at `0`
   it stays one `Array(Tuple(…))` column named `n` — the gate is upstream's
   own (`InterpreterCreateQuery` gating `ColumnsDescription::flattenNested`;
   v26.7.3.19 `InterpreterCreateQuery.cpp:721`, same guard on every vendored
   era back to 24.8's `:723`). Every downstream shape follows the compiled
   column list: the introspection accessors, JSONEachRow name lookup (the
   group key matches the one column; dotted `n.a` keys become ordinary
   unknown fields), positional arity, and the RowBinary wire. CREATE-time
   type validation (`checkAllTypesAreAllowedInTable`, the code-370 gate)
   runs on the column set **as the flatten decision shapes it**, as the
   server validates it — `x Nested(n Array(Nothing))` is 370 under
   flatten_nested=1 and compiles under 0, matching the server's CREATE in
   both directions.
5. **A declared TYPE GATE binds at compile, and then outranks the per-call
   map** (added 2026-08-19) — the one documented exception to rule 3's
   order, and it is the server's own: a gate binds when the COLUMN IS
   CREATED and is never re-read for an arriving row, so for those names the
   handle wins even against an explicit per-call value, while every other
   declared setting is only a default the per-call map may beat. A gate
   named in the profile is checked once, at
   compile, over the schema's columns with upstream's own `validateDataType` —
   so a profile declaring it at a refusing value fails the compile with
   upstream's own code and message (`455`, `44`), exactly as the server's
   `CREATE` fails. For those names the handle is then the authority for the
   handle's life: a later `chs_rows`/`chs_row` whose settings map omits the
   gate, or sets it to `0`, is still accepted, because the table's CREATE
   already settled it. Gates the profile does NOT name are unchanged — the
   per-call map remains the only evidence about them and the per-row re-check
   stays. A DEFAULT carrying a **user-written CAST** to a gated type is still
   re-checked per call, under the caller's settings, which is the one place a
   real server re-checks anything (see "Server-level type gates").
6. **The admission budgets stay process policy.** The DEFAULT-evaluation
   envelope (`chtypes_default_eval_*`, the sleep refusal) is re-pinned after
   the profile is applied; a declared profile cannot lift it.
7. **An empty profile is not a special case, it is a shorter path.** `NULL` /
   `"{}"` never reaches the profile machinery: `profile.any` is false and the
   compile body makes no `Context` copy at all. "No settings declared behaves
   exactly as if this parameter did not exist" is therefore a property of the
   implementation rather than a test's claim — which is what made folding the
   two entry points into one a rename rather than a behavior change.

Verified on the relinked 24.8 and 25.8 darwin artifacts, 2026-08-18
(`n Nested(a Int64, b String)`): a profile-less compile and a `{}` profile
both compile 2 columns
`n.a`/`n.b`; under `{"flatten_nested":"0"}` the same DDL compiles 1 column `n` of type
`Nested(a Int64, b String)`, accepts `{"n":[{"a":1,"b":"x"}]}` storing
`[{"a":1,"b":"x"}]`, skips dotted keys as unknown fields (code 117 under
`input_format_skip_unknown_fields=0`), and rejects `{"n":1}` with the
server's 130 — 21/21 recorded arbiter ground-truth cases in exact agreement.

## The row result document (`chs_row`)

`chs_row` returns one JSON object. Captured verbatim from the `25.8` artifact
(`x UInt8, s String` / `{"x":256,"s":"hi"}` / JSONEachRow), reformatted here for
reading:

```json
{"outcome":"accepted","code":0,"err":"",
 "unknown_fields":[],"unsupported_settings":[],"computed":[],
 "cols":[
   {"name":"x","type":"UInt8","base":"UInt8","src":"input","input":"256",
    "stored":0,"ref":256,"ref_type":"Int256",
    "nullable":false,"poison":false,"null_input":false,"dup_dropped":false},
   {"name":"s","type":"String","base":"String","src":"input","input":"\"hi\"",
    "stored":"hi","ref":null,"ref_type":"",
    "nullable":false,"poison":false,"null_input":false,"dup_dropped":false}]}
```

### Top-level fields

| Field | Type | Meaning |
|---|---|---|
| `outcome` | string | `"accepted"` \| `"rejected"` \| `"accepted_poisoned"` \| `"unsupported"`. A fifth spelling, `"skipped"`, appears **only** on entries inside a batch document's `rows` (added 2026-08-27, see §The batch result document) — `chs_row` itself never returns it, because a single row has no batch to continue. |
| `code` | int | `0` when accepted; a ClickHouse code when rejected; `691` on `accepted_poisoned`. **Not universal for `unsupported`** (measured 2026-08-17): a row-level decline (e.g. `DEFAULT hostName()`) carries `code: 0`; the clock-skew decline carries `-2`; compile/engine/TTL error paths return `-2`. Key on `outcome`, never on the code alone |
| `err` | string | ClickHouse's message, `""` when none |
| `unknown_fields` | string[] | input field names with no matching column |
| `unsupported_settings` | string[] | requested settings this build models but declines. Non-empty ⇒ the answer MUST NOT be scored as agreement |
| `computed` | object[] | `MATERIALIZED` values: `{"name","kind","stored"}` |
| `cols` | object[] | one entry per **declared** column, in declaration order |

**Keys may be absent.** A rejected row's document omits `unknown_fields`,
`unsupported_settings` and `computed` entirely and carries `"cols":[]`:

```json
{"outcome":"rejected","code":27,
 "err":"Cannot parse input: expected '\"' before: 'abc\"}'","cols":[]}
```

A binding MUST treat every field as optional-with-a-default (empty list, empty
string, `false`) rather than requiring it.

### Per-column fields (`cols[]`)

| Field | Type | Meaning |
|---|---|---|
| `name` | string | column name |
| `type` | string | the column's canonical declared type, e.g. `Nullable(UInt8)`, `Enum8('a' = 1, 'b' = 2)` |
| `base` | string | the type with `Nullable`/`LowCardinality` peeled off, e.g. `UInt8`. This is what the reason classifier switches on. |
| `src` | string | where the value came from — see the table below |
| `input` | string | the raw input **text** for this field, as a string. `"256"`, `"\"hi\""`, `"\\N"`. For a DEFAULT-sourced column it holds the **expression** (`"now()"`, `"e + 1"`). Empty for `RowBinary` (there is no text) and for absent columns. |
| `stored` | raw JSON value | **ClickHouse's own JSON text of the stored value.** A number is a number, a string is a string, `null` is a real stored null. Authoritative. |
| `ref` | raw JSON value | the same input bytes parsed a second time through the widened reference type. `null` when no reference parse applies. |
| `ref_type` | string | the reference type used, `""` when the type has none (`String`, `Float64`). May be non-empty while `ref` is `null`. |
| `nullable` | bool | the declared type is `Nullable(...)` |
| `poison` | bool | ClickHouse stored a value it cannot read back. When true, `stored` carries no renderable value. |
| `null_input` | bool | the input for this field was a null (JSON `null`, or CSV/TSV `\N`). Distinct from `stored == null`. |
| `dup_dropped` | bool | the row named this column more than once and ClickHouse kept the **first** value, discarding the rest with no signal |
| `wire` | string | **present only for a text format whose `input` is in the writer's own vocabulary — `TSV` today.** The stored value written back out by ClickHouse's own serializer for that vocabulary (`serializeTextEscaped`). `wire != input` is a change ClickHouse made, and it is how the supplied-vs-stored detector works at all where the field is not a JSON value (`docs/reference/bindings.md`, detector 3). ABSENT — not empty — for every other format, so a document that never carried it is byte-identical to what it was before this field existed. A binding MUST NOT synthesize it. |

`stored` and `ref` MUST be handled as **raw bytes / raw JSON**, not decoded into
the binding's native string and number types before comparison. Two reasons,
both paid for:

- A ClickHouse `String` column holds arbitrary bytes. Decoding invalid UTF-8
  into a language string replaces it with U+FFFD, which then reads downstream as
  a silent transformation that never happened. The reference implementation keeps
  `json.RawMessage`.
- ClickHouse integers go to 2^256. Round-tripping through a double turns
  `18446744073709551615` into `18446744073709552000` and an `Int256` into
  `-5.78960446186581e+76` — a value difference no scorer can tell from a real
  coercion defect. Bindings MUST use an exact-decimal/bignum path (Go
  `json.Number`, Python `int`/`Decimal` via `parse_int`/`parse_float`,
  JavaScript `BigInt` or the raw text).

### `src` values

All eight, from the emitter in `lib/csrc/chtypes.cpp`:

| `src` | Meaning |
|---|---|
| `input` | the row supplied this value |
| `default` | a DEFAULT expression this library evaluated through ClickHouse's own CAST path |
| `default_substituted` | a **volatile** DEFAULT (`now()`, `now64(n)`, `today()`, `yesterday()`, or an expression over one) that this library resolved from its own clock, once for the whole batch. The caller MUST send this column as an explicit value in the INSERT. |
| `absent` | no DEFAULT declared; the value is the type's own zero |
| `skipped` | `MATERIALIZED` / `ALIAS` / `EPHEMERAL` — never read from an input row |
| `default_volatile_unresolved` | a volatile DEFAULT the clock-skew budget forbade substituting; the row is `unsupported` |
| `default_pending` | an internal intermediate state; treat as "no value yet" |
| `default_expr_unsupported` | the DEFAULT expression is one this build refuses to evaluate |

The reference implementation drops `skipped` columns from its `Values` list, and
emits no transformation for `skipped`, `default_expr_unsupported`,
`default_volatile_unresolved` or `default_pending`.

### `computed` and what is deliberately absent

`computed` carries `MATERIALIZED` values, as `{"name","kind","stored"}` — e.g.
`{"name":"m","kind":"MATERIALIZED","stored":11}`.

> **Format disagreement resolved, 2026-08-17.** For
> `a UInt8, m UInt8 MATERIALIZED a + 1, b String`, live servers (24.8.14.39,
> 25.8.28.1, 26.7.3.19 — identical) store `m = 8` for **every** format:
> JSONEachRow, CSV, TSV, Values and JSONCompactEachRow all feed the supplied
> `a` into the MATERIALIZED expression. The earlier observation that CSV
> computed `m = 1` (the type's zero plus one) was a wrapper defect — the
> positional readers (CSV/TSV/Values) never handed their parsed columns to
> the DEFAULT/MATERIALIZED evaluation pass — fixed in `chtypes.cpp` the same
> day. The same root cause also starved row-dependent DEFAULTs (CSV `7,`
> under `a UInt8, d UInt8 DEFAULT a + 1` now stores 8, as every server does)
> and CHECK constraints on positionally-supplied rows.

They are **not** in `cols`'
stored row, because `SELECT *` does not return them and a preview that mixed
them in would disagree with what a subscriber reading the table sees. They are
reported *separately* because they are nonetheless durable: an `ALTER ... MODIFY
COLUMN m MATERIALIZED <new expr>` does not touch rows already written.

`ALIAS` is deliberately **never** reported as a value. It is computable by the
same machinery, but `ALTER ... MODIFY COLUMN a ALIAS <new expr>` *retroactively*
changes what already-inserted rows read back as, so an ALIAS is a fact about the
schema at read time, not about the row. A binding MUST NOT present one as a
stored value. It still appears in `cols` with `src: "skipped"` and its inferred
type.

`EPHEMERAL` has no value at all (Code 16 in both paths). Its effect is meant to
be visible only through the DEFAULT columns that reference it.

> **Confirmed against live servers, 2026-08-17 (24.8.14.39, 25.8.28.1,
> 26.7.3.19 — identical answers), and now specified.** Both earlier
> observations on `id UInt32, e UInt8 EPHEMERAL, d UInt8 DEFAULT e + 1` are
> exactly what a real `INSERT INTO t FORMAT …` (no column list) does:
>
> | Format | Body | Server = library |
> |---|---|---|
> | JSONEachRow | `{"id":1,"e":5}` | `e` is an **unknown field** (skipped under the default `input_format_skip_unknown_fields=1`; code 117 with it set to 0); `d` = **1**, computed from `e`'s type zero |
> | CSV | `7,5` | field 2 lands in **`d`** (stored `5`); `e` occupies no field position; `7,5,6` is 117 "Expected end of line" |
>
> The reason: an INSERT without a column list targets
> `getSampleBlockNonMaterialized()` — ordinary + DEFAULT only. `EPHEMERAL` is
> in `getInsertable()`, but that block is reachable **only through an
> explicit column list**: `INSERT INTO t (id, e) FORMAT JSONEachRow` with
> `{"id":3,"e":5}` accepts the value and stores `d = 6` (measured, all three
> versions; same for CSV `8,5` → `d = 6`). A format stream carries no column
> list, so this API cannot express that shape — a gateway that forwards
> INSERTs naming EPHEMERAL columns in an explicit column list cannot preview
> them with `chs_row`, and MUST NOT expect the supplied `e` to influence `d`
> here. The `format()` table function agrees with the no-column-list INSERT
> (`SELECT * FROM format(JSONEachRow, <this schema>, '{"id":1,"e":5}')`
> returns `id=1, d=1`). Not an over- or under-accept in either direction:
> every verdict above matches the storage path a tenant's data takes.

### Positional formats

`CSV`, `TSV`, `Values` and `JSONCompactEachRow` address the *k*-th column of
the block an INSERT **without a column list** targets —
`getSampleBlockNonMaterialized()`: ordinary + `DEFAULT`, i.e. everything
except `MATERIALIZED`, `ALIAS` **and `EPHEMERAL`**. Those three occupy no
field position and are not counted in the expected arity. (`EPHEMERAL` is in
`getInsertable()`, but that block only exists behind an explicit
`INSERT INTO t (id, e)` column list, which no format stream carries — see the
EPHEMERAL note above; measured on live 24.8 / 25.8 / 26.7.) `JSONEachRow` is
name-addressed and unaffected. Verified: `a UInt8, m UInt8 MATERIALIZED a+1, b
String` with the CSV line `7,"hey"` accepts, `a=7`, `b="hey"`, and `m` appears
in `computed` with `stored: 8` — computed from the supplied `a`, identically
in every format, as live servers do (fixed 2026-08-17; the artifact previously
computed `stored: 1` here from `a`'s type zero).

### `Native` (`CHS_NATIVE = 8`)

`Native` is the odd one out on this axis: it is **column-oriented**, it is
**self-describing**, and it is what every ClickHouse client library sends on
INSERT. All three matter to a caller.

**The wire contract, at the revision the FORMAT path uses.**
`NativeInputFormat` builds its `NativeReader` with a hard-coded
`server_revision = 0` in every vendored tag
(`Impl/NativeFormat.cpp:20-25` on 24.8, `:20-25` on 26.7). At revision 0 the
body is, per block:

```
varuint  n_columns
varuint  n_rows
n_columns x {
    string  name                       (varuint length + bytes)
    string  type                       (a type EXPRESSION: "Nullable(Decimal(18, 4))")
    bytes   the whole column, via ISerialization::
            deserializeBinaryBulkWithMultipleStreams
}
```

and the body is a sequence of such blocks, terminated by end-of-input **or** by
a block declaring zero columns. Two prefixes that a reader at a *nonzero*
revision would expect are **absent** here, both gated on the revision:

* the `BlockInfo` preamble (`if (server_revision > 0) res.info.read(...)`,
  `NativeReader.cpp:128` on 24.8, `:154` on 26.7);
* the per-column custom-serialization flag byte, gated on
  `server_revision >= DBMS_MIN_REVISION_WITH_CUSTOM_SERIALIZATION` (54454) —
  `NativeReader.cpp:182` on 24.8, `:211` on 26.7. That byte is written for
  every column, including in zero-row blocks, so mistaking one contract for
  the other desynchronizes immediately rather than subtly.

**Blocks captured off a live TCP connection are therefore NOT this format.**
`TCPHandler` passes the *negotiated* `client_tcp_protocol_version`
(`Server/TCPHandler.cpp:2095-2098` on 24.8, `:2869-2874` on 26.7), which in
practice is ≥ 54454, so those blocks carry both prefixes. `chs_rows` with
`CHS_NATIVE` models the FORMAT path — an HTTP/`INSERT … FORMAT Native` body —
and nothing else. A gateway that terminates the native TCP protocol itself has
a different, revision-parameterized job — one chtypes setting carrying the
negotiated revision, passed to the `NativeReader` constructor — which is a
deliberate future decision, not part of this contract.

**A declared/stream disagreement is not automatically an error, and this is the
whole reason the format needs the library.** `NativeReader` matches the stream's
columns to the declared ones **by name**, and then:

| disagreement | what a stock server does | source |
|---|---|---|
| stream type ≠ declared type | **CASTs**, silently — `input_format_native_allow_types_conversion` defaults to **`true`** on every tag 24.8-26.7 | `NativeReader.cpp:228/232` (24.8), `:274/278` (26.7); default at `FormatSettings.h:470` (24.8), `:611` (26.7) |
| …with that setting off | `recursiveLowCardinalityTypeConversion` only — a genuine mismatch becomes `TYPE_MISMATCH` (53) | `DataTypeLowCardinalityHelpers.cpp:208` (24.8), `:299` (26.7) |
| stream has a column the schema does not | `INCORRECT_DATA` (117), *"Unknown column with name X …"* — unless `input_format_skip_unknown_fields` is on, which defaults off | `NativeReader.cpp:256` (24.8), `:301` (26.7) |
| schema has a column the block omits, `n_rows > 0` | **not an error**: the column is filled and marked in `BlockMissingValues`, which is how `markFormatSupportsSubsetOfColumns("Native")` works. chtypes routes it into the identical absent-column path a missing `JSONEachRow` key takes | `NativeReader.cpp:324-346` (26.7) |
| …the same, `n_rows == 0` | **an error**, and a surprising one: the header-alignment pass is guarded `if (rows && !header.empty())`, so a zero-row block escapes unaligned and `assertBlocksHaveEqualStructure` raises `LOGICAL_ERROR` (49) | `NativeReader.cpp:324`; `Impl/NativeFormat.cpp:50` |
| `n_columns == 0`, `n_rows > 0` | **not an error** with a non-empty schema: an N-row all-DEFAULT block | `NativeReader.cpp:177-178`, `:324-346` |
| truncation mid-column | `CANNOT_READ_ALL_DATA` (33) for a short bulk column, `ATTEMPT_TO_READ_AFTER_EOF` (32) for a short varint | `NativeReader.cpp:122-127`; `IO/VarInt.h:82` |
| absurd `n_columns` / `n_rows` | `TOO_LARGE_ARRAY_SIZE` (128) above 10⁶ columns / 10¹² rows | `NativeReader.cpp:139-142` (24.8), `:166-169` (26.7) |

Every one of those verdicts is produced by driving the vendored
`DB::NativeReader` itself — the wrapper reimplements none of them.

**Batch semantics.** `Native` is not row-oriented at all: `NativeInputFormat`
is an `IInputFormat`, not an `IRowInputFormat`, so there is no per-row error
boundary, `input_format_allow_errors_num` / `_ratio` never apply, and a fault
anywhere in a block rejects the whole body — the same all-or-nothing rule the
RowBinary family follows, for a different reason. An empty body is zero rows,
not an error. As with every binary format, `raw`/`body` are counted, not
NUL-terminated.

**Column addressing.** The block addresses the same wire tuple the positional
formats do — `MATERIALIZED`, `ALIAS` and `EPHEMERAL` take no place in it — but
by *name*, not by position, so a `Nested(a, b)` declaration under
`flatten_nested=1` is addressed as the two storage columns `n.a` and `n.b`.

### `Buffers` (`CHS_BUFFERS = 9`)

`Native`'s column encoding under a different frame, and the difference is the
entire point of modeling it separately. Per block
(`src/Formats/BuffersReader.h:13-24`, byte-identical on 26.5, 26.6 and 26.7):

```
uint64le  n_columns
uint64le  n_rows
n_columns x {
    uint64le  byte size of this column's serialized data
    bytes     the column, EXACTLY as the Native format writes it
}
```

**Arrival: 26.5.** `factory.registerInputFormat("Buffers")` first appears at
`Impl/BuffersFormat.cpp:88` on 26.5 (`:89` on 26.6 and 26.7), reached
unconditionally from `registerFormats.cpp:179` / `:184` / `:185`. No tag at
25.10 or earlier contains the register function, the `"Buffers"` literal, or
`src/Formats/BuffersReader.h`. On those four lines both the server and this
library answer **73 `UNKNOWN_FORMAT`** — measured on all four, and asserted by
`chtypes-core/tests/fixtures/buffers/`.

**The schema is out of band, and that is the contract.** `BuffersReader.h:22-23`
states it: *"The schema (names, types, order) is taken from the header passed in
the constructor; the stream itself does not contain names or types."* Where
`Native` can reconcile a producer against a consumer — matching by name,
CASTing a mismatched type, filling an omitted column — `Buffers` has nothing to
reconcile with. `BuffersReader.cpp:76-84` compares only the DECLARED byte size
against what the declared type actually consumed. So:

| the stream vs the declared schema | what happens | source |
|---|---|---|
| same width, different type | **silently reinterpreted**. A `UInt32` producer's `4294967295` is stored as `-1` by an `Int32` consumer; a `Float32` `1.5` as `1069547520` by a `UInt32` one. No check can fire — there is no name, no type, and the size matches | `BuffersReader.cpp:76-84` (the only check) |
| different width | `INCORRECT_DATA` (117) when the size accounting disagrees, or `CANNOT_READ_ALL_DATA` (33) when the reader runs off the end first | `BuffersReader.cpp:77-84`; `NativeReader::readData` at `:70` |
| block's `n_columns` ≠ the header's | `INCORRECT_DATA` (117) — checked **before** the too-large guard, so an absurd column count is 117 and never 128 | `BuffersReader.cpp:41-43`, guard at `:45-46` |
| absurd `n_rows` | `TOO_LARGE_ARRAY_SIZE` (128) — this one **does** reach its guard, because the column count has already matched | `BuffersReader.cpp:52-53` |
| a column the block omits | **not representable.** Every header column is filled positionally (`:56-87`), there is no name to miss and no `BlockMissingValues`, so the DEFAULT machinery never runs for this format | `BuffersReader.cpp:56-87` |
| empty body | zero rows, not an error | `BuffersReader.cpp:34-35` |

That first row is the class this format exists to probe, and it is why the
`Buffers` corpus and fixtures compare **stored bytes**, not just verdicts:
there is no error code to compare, because there is no error. A library that
reinterpreted differently from the server would be silently wrong, which is the
worst failure this project scores.

Every one of those verdicts is produced by driving the vendored
`DB::BuffersReader` itself — a 92-line file whose per-column work is one call
to `NativeReader::readData` (`:70`), the same static the `Native` path already
reaches. The wrapper reimplements none of it.

**Batch semantics.** Identical to `Native`, and for the identical reason:
`BuffersInputFormat` is an `IInputFormat`, not an `IRowInputFormat`
(`BuffersFormat.cpp:13`), so there is no per-row error boundary,
`input_format_allow_errors_num` / `_ratio` never apply, and a fault anywhere in
a block rejects the whole body. The wrapper makes the same two post-read
assertions the format itself makes, in its order —
`assertBlocksHaveEqualStructure` then `checkNumberOfRows` (`:37-38`).

**Column addressing.** Purely POSITIONAL, against the same wire tuple the
positional text formats address: `MATERIALIZED`, `ALIAS` and `EPHEMERAL` take
no place in it. Unlike `Native`, reordering the columns cannot be detected as a
reordering — only as whatever the size accounting makes of it.

## The batch result document (`chs_rows`)

Same document, wrapped:

```json
{"outcome":"accepted","code":0,"err":"",
 "engine_rows":[ … ],
 "storage_transforms":[ … ],
 "rows_read":2,"rows_skipped":0,
 "rows":[ <one chs_row document per row> ]}
```

| Field | Type | Meaning |
|---|---|---|
| `outcome`, `code`, `err` | | the batch verdict, same vocabulary as a row |
| `rows_read` | int | rows the reader consumed, failed ones included |
| `rows_skipped` | int | rows dropped under `input_format_allow_errors_num` / `_ratio` |
| `rows` | object[] | one row document per row the reader consumed, **in input order** — accepted, poisoned, batch-aborting rejected, and (since 2026-08-27) skipped rows alike. |
| `engine_rows` | raw JSON object[] | present only when a specialized engine or a TTL forced the storage path. What the part will hold **after** the engine's insert-time merge. |
| `storage_transforms` | object[] | what the storage layer did to rows the type layer accepted |
| `row_spans` | object[] | **only when an export was requested AND emitted** (revision 3): one `{"off","len"}` per `rows[]` entry, index-aligned; `len` 0 for non-accepted rows. See §Rows, the export channel. |
| `export_declined` | string | **only when an export was requested and withheld**: the reason (batch not `accepted`, the full-arity guard, a serialization failure, `output_format_json_validate_utf8`). `out_bytes` stays `{NULL,0}`. |

### `"outcome":"skipped"` — itemized skips (added 2026-08-27)

A row dropped under `input_format_allow_errors_*` keeps its place in `rows`
as the documented short form of a rejected row's document, with the fifth
outcome spelling:

```json
{"outcome":"skipped","code":27,
 "err":"Cannot parse input: expected '\"' before: 'oops\",\"b\":\"y\"}'","cols":[]}
```

- **The error is ClickHouse's own, verbatim.** `IRowInputFormat::generate`
  catches the row's exception *before* resyncing (IRowInputFormat.cpp:161;
  the skip budget :167-180; `syncAfterError()` :194 — byte-identical
  24.8→26.7), so the server computes exactly this code and message for every
  row it skips — and then discards them, logging only a count (:233-236,
  *"Skipped {} rows with errors"*). Reporting the caught error itemizes what
  the server already decided; nothing here is a new validation rule.
  Equivalence property, asserted by the fixture replay rather than by
  hand-written expectations: a skipped row's `code`/`err` equal what strict
  mode (no error allowance) reports for that row's bytes in isolation — same
  reader, same bytes.
- **Position is the entry's index in `rows`.** The document has never carried
  a per-row index field — order is the addressing — so for an accepted batch
  `rows_read == len(rows)` and the i-th entry is the i-th record the reader
  consumed. Consumers that render survivors MUST filter on the row `outcome`,
  not assume every entry is stored.
- **The batch verdict is untouched.** `outcome`/`code`/`err`,
  `rows_read` and `rows_skipped` are exactly what they were when skips were
  silent: the batch stays `accepted` while the budget holds, and the
  budget-exceeding row still aborts it with that row's error.
- **No `CHS_ABI_REVISION` bump, deliberately.** This is a vocabulary addition
  to the result *document*, not a signature change: no function, argument or
  `enum chs_format` moved. The unknown-outcome rule (docs/reference/bindings.md
  §RowResult, 2026-08-26) is what makes it additive-safe: a binding that
  predates the spelling degrades a `skipped` row to `Unsupported` — a
  decline, never scored as agreement, never a manufactured rejection — which
  is the designed safe path for exactly this kind of drift. A revision bump
  would instead refuse every older binding outright to protect them from an
  entry they already handle safely.

`engine_rows` may legitimately be **shorter** than `rows` (a `SummingMergeTree`
dropping an all-zero row, a TTL-expired row) or **reordered** (the block is
sorted by the sorting key before the part is written). When `engine_rows` is
present it — not `rows` — is the stored truth, and a binding MUST prefer it when
reporting what the table will hold. `nil`/absent means no engine semantics were
applied.

`storage_transforms` entries are `{"row", "column", "reason", "stored"}`, with
`stored` and `column` omitted where they do not apply:

```json
{"row":0,"column":"","reason":"ttl_expired"}
{"row":0,"column":"v","reason":"ttl_column_expired","stored":0}
```

- `ttl_expired` — the whole row is past the table TTL: **not stored**. Reported
  so a gateway can tell the tenant rather than previewing a row the table will
  never hold.
- `ttl_column_expired` — the value is past a column TTL and was reset to the
  column's DEFAULT; `stored` is the post-reset value.

Both are lossy by construction. A binding MUST fold them into its
transformation list (the reference implementation appends them to
`BatchResult.Transformed`).

Verified, `ts DateTime, v UInt8` with `SetTTL("ts + INTERVAL 1 DAY")`, one row at
`2020-01-01`, instant pinned to `1700000000000000000`:

```json
{"outcome":"accepted","code":0,"err":"","engine_rows":[],
 "storage_transforms":[{"row":0,"column":"","reason":"ttl_expired"}],
 "rows_read":1,"rows_skipped":0,"rows":[ … the row, accepted … ]}
```

The row's own document says `accepted`. **The batch is where "not stored"
lives.** A binding that reports per-row outcomes and ignores
`storage_transforms` will tell a tenant a row was stored that the table deleted
at merge time — the exact failure the field exists to prevent.

Engine semantics, verified: `SummingMergeTree` over `(day, key)` with two rows
`v=5` and `v=7` produced `engine_rows: [{"day":"2026-01-01","key":1,"v":12}]`
while `rows` still held both inputs. `CollapsingMergeTree(sign)` with `sign = 3`
was rejected with code `117`, `"Incorrect data: Sign = 3 (must be 1 or -1)."` —
before anything is stored, exactly as the server does under
`optimize_on_insert = 1`.

Empty body: `{"outcome":"accepted","code":0,"err":"","rows_read":0,
"rows_skipped":0,"rows":[]}`. Accepted with zero rows is not an error.

## The bare-denormal quirk (`quoteBareDenormals`)

**This is a required repair, not an optional nicety.** ClickHouse's own
`serializeTextJSON` writes IEEE denormals as the *bare tokens* `inf`, `-inf` and
`nan` unless `output_format_json_quote_denormals` is set. That is faithful
ClickHouse output and it is **not valid JSON**. One `QBit` column full of
infinities took an entire result document down with `invalid character 'i'` —
18 arbiter cases lost at once.

Every binding MUST therefore repair the document before parsing it. The
reference repair is `quoteBareDenormals` in `go/chtypes/chtypes.go`:

1. Fast path: if the bytes contain neither `inf` nor `nan`, return unchanged.
2. Otherwise scan bytes, tracking whether you are inside a JSON string
   (honoring `\` escapes). **Never rewrite inside a string.**
3. Outside a string, and only where a value may start — immediately after `:`,
   `,`, `[`, or whitespace — match `-inf`, `inf`, or `nan`, and only when the
   next byte is `,`, `}`, `]`, or end-of-input.
4. Wrap the matched token in double quotes.

The stored *text* is preserved exactly; only quoting is added, so downstream
consumers see the same bytes ClickHouse would have written. After the repair a
denormal arrives as the JSON string `"inf"` / `"-inf"` / `"nan"`, and that is
what a binding MUST emit onward. It MUST NOT convert to a language float (which
loses the distinction between `inf` and a large finite value, and cannot survive
a JSON round-trip at all).

Verified: `f Float64` given `1e400` yields `"stored":"inf"` after the repair,
classified `lossy_numeric`.

## Statelessness

Every entry point is a pure function of five inputs:

```
(build version, schema text, row bytes, settings, clock instant)
```

There is no cross-call state. A `chs_schema` is derived data — recompiling the
same DDL in another process yields a plan producing byte-identical answers. The
process-wide pieces (the global `Context`, the evaluation-guard pool, interned
registries) are caches whose contents never influence a verdict or a value. Two
instances given the same five inputs agree without ever having met, which is what
makes the edge horizontally scalable with no coordination tier.

DEFAULT semantics cannot smuggle state in, and this is **verified per build**
rather than believed. ClickHouse has no auto-increment or sequence DEFAULT; a
DEFAULT can reference only same-row columns and functions;
`lib/tools/gen_function_flags.py` enumerates every function in the build's own
registry via `chs_function_flags` and **fails the build** unless the volatile set
the classifier admits is exactly the four clock reads. `generateSerialID`
(Keeper-backed) and `generateSnowflakeID` — the closest things to a sequence —
are refused by the same flags that refuse `rand()`. `AUTO_INCREMENT` as a column
clause is a `SYNTAX_ERROR` in ClickHouse's own parser.

The only impurity is the clock, and the clock is an explicit **input**.

## Appendix — the schema-level `Enum` DEFAULT refusal

*Appended 2026-08-17, moved here from the retired `chtypes-core/docs/api.md`
(its AMENDMENT's first required contract change), which was the only place it was
written down. Retained because it is a **normative refusal on the compile path**
and the rest of the spec did not carry it.*

`chs_schema_compile` MUST refuse a schema declaring
`Enum… DEFAULT <integer outside the declared domain>`, on **every** ClickHouse
line, even where that line's own DDL accepts it. This is the one place the library
is deliberately *stricter* than an older server, and the reason is that the server
is not strict enough to be safe: on 24.8 / 25.3 / 25.10 the `CREATE TABLE`
succeeds, the first insert returns rc=0, and **every later `SELECT` fails** — code
36 on 24.8, code 691 from 25.3 on (`docs/defaults.md` §1.5(1) measured the DDL and
readback per version). That is schema-level poisoning: a table that is already
unreadable before any row of a tenant's data arrives, with nothing in the insert
path to signal it. A validator that accepted the schema would be handing back
*accepted* for rows the customer can never read.

Observed on the `darwin-arm64` artifacts through
`chtypes-core/lib/build/chtypes-oracle --registry <registry> --version <v>`, 2026-08-17, for
`e Enum8('a'=1,'b'=2) DEFAULT 9` — with the value supplied *and* with it absent, so
the refusal is a property of the schema rather than of the row:

| version | answer |
|---|---|
| 24.8 | `code 691` — `DEFAULT for column e is outside the Enum domain: Unexpected value 9 in enum` (the wrapper's own guard; the server would have accepted the DDL) |
| 25.8 | `code 691`, same message and same guard |
| 26.6 | `code 691` — ClickHouse's own message: `Unexpected value 9 in enum … default expression and column type are incompatible`, i.e. the server refuses at DDL from 26.6 |

The uniform answer is therefore a refusal with code `691`, whichever layer
produces it. A binding does nothing special here: this is a compile-time error
like any other, surfaced through the normal `chs_schema_compile` error path, and
MUST NOT be softened into a row-level `unsupported`.
