# chtypes ABI v1: the prose for the description

This file holds the prose for `spec/abi-v1/abi.json`, one `###` section per handle, enum, function and (optionally) constant or document. `scripts/abi-v1/gen.py` copies each section into the comment above its declaration in `include/v1/chtypes.h` and into the generated block of `docs/reference/abi-v1.md`, and `gen.py --check` fails when a symbol has no section or a section names no symbol.

Prose is deliberately outside the fingerprint: editing this file never changes `CHS_ABI_FINGERPRINT`, so it never invalidates a built library. The rule that follows is that anything a binding or the artifact producer must act on is structured in `abi.json` (a nullability, an ownership, a status a call may return, a thread class, a vocabulary), never only stated here.

Each section is copied into a C comment, so it may not contain a comment opener or closer, or two question marks in a row.

## Preamble

This header is generated from `spec/abi-v1/abi.json` by `scripts/abi-v1/gen.py`. Edit the description and regenerate; never edit this file. `docs/reference/abi-v1.md` is the normative reference.

Identity. `CHS_ABI_VERSION` is the generation and `CHS_ABI_FINGERPRINT` is sha256 over the canonical form of the description. A loader opens a library with `RTLD_NOW | RTLD_LOCAL`, calls only the handshake set (`chs_abi_version`, `chs_build_info`, `chs_clickhouse_version`) before its checks pass, and refuses any library whose generation or fingerprint differs from the ones it was compiled with. Nothing degrades: every described symbol is mandatory.

Ownership. Every input string, statement, document and body is counted bytes, `(const uint8_t *p, size_t n)`: length 0 is none and the pointer may then be NULL, and a NULL pointer with a nonzero length is `CHS_INVALID_ARGUMENT`. Every output is an owned, opaque `chs_buf`, read with `chs_buf_data` and `chs_buf_len` and released with `chs_buf_free`. Bytes pass through unmodified, NUL and invalid UTF-8 included: a column name may contain either (measured on live servers), so no name, statement or message is ever a C string. The only static strings are the handshake's. Handles are opaque, reference-counted and tagged with their kind and the image that made them; every entry point checks both, a child holds a strong reference to what it needs, and each free drops only the caller's reference, so any free order is safe and freeing NULL does nothing.

Errors. A fallible call returns a `chs_status` and, through an optional last `chs_error **err`, an owned error carrying the status, ClickHouse's own code and name, the message and the column. The status alone carries the verdict, so `err` may be NULL.

### chs_buf

An owned, immutable byte buffer: every output of the library. Read it with `chs_buf_data` and `chs_buf_len`; the bytes are exactly what the library produced, with no terminator and no encoding promise beyond what the producing parameter's content says.

### chs_error

The error a fallible call reports when its status is not `CHS_OK`: the status, ClickHouse's own error code and name, a message and the column concerned. Created only by the library, through a call's `err` out-parameter.

### chs_schema

A compiled table: the columns, engine, keys and settings of one `CREATE TABLE` statement, immutable once created.

### chs_filter

A compiled boolean SQL expression over a schema's columns, with its query parameters bound.

### chs_block

A body parsed once under a schema, for evaluating many filters without parsing again.

### chs_status

The verdict of every fallible call, in five values whose numbering is frozen.

- `CHS_OK`: the call succeeded and its outputs are set.
- `CHS_REJECTED`: ClickHouse's own refusal, with its own code, name and message, which a server would also give.
- `CHS_DECLINED`: this build will not answer; a server might accept. Never scored as agreement.
- `CHS_INVALID_ARGUMENT`: caller misuse, such as a NULL pointer with a nonzero length, a wrong-kind, freed or cross-library handle, or a NULL required out-parameter.
- `CHS_INTERNAL`: a guarded exception inside the library, which is a library bug.

The set is closed within this generation: any new condition maps onto one of these five. A binding maps each status to one error class (`spec/abi-v1/sdk.json`), and treats a value outside the set as an internal error naming it.

### chs_format

The input and export formats, numbered as they have been since the first release; the numbers are frozen and a binding passes the integer. `ch_name` is ClickHouse's own name for the format, which is how `chs_build_info` lists the formats a build supports. Being in this enum never proves a build supports a format: the build says so in its `capabilities`.

### transform_reason

The reason a stored value differs from the supplied one, as the library reports it per column. `lossy` says whether information was lost; exactly four reasons are lossless (a representation change, a value filled from a DEFAULT or the type's zero, and a DEFAULT the library resolved from its own clock), and they are still reported because a preview must show what the table will hold. A binding reads `lossy` from here and never keeps a list of its own.

### value_src

Where a column's value came from. `is_stored` says whether the row as stored carries a value for the column: a column the input named but the table never stores (an EPHEMERAL column, a skipped MATERIALIZED or ALIAS column) and a DEFAULT the library could not resolve carry none.

### row_outcome

The verdict on one row: accepted, accepted but unreadable afterwards (the insert succeeds and every later read fails), rejected, skipped under the server's error allowance with the batch continuing, or declined by this build.

### batch_outcome

The verdict on a whole body. A batch is never skipped; its rows carry their own outcomes.

### filter_outcome

Whether a filter evaluation completed at all. Per-row failures are verdicts, not outcomes.

### filter_verdict

One character per row of a filter evaluation. `t` and `f` are answers (true; false or NULL); `e` (the predicate raised an error on this row) and `d` (this build declines the row) are not, and a caller enforcing visibility must fail closed on both.

### CHS_ABI_REVISION_TOMBSTONE

What `chs_abi_revision` always returns: 1001, outside every revision a released v0 binding speaks.

### CHS_EXPORT_NONE

The `export_format` of a batch preview that exports nothing.

### CHS_DOC_VALUES

A `doc_flags` bit: the per-row documents carry each column's stored value and provenance.

### CHS_DOC_TRANSFORMS

A `doc_flags` bit: the per-row documents carry the transformations the library detected.

### CHS_DOC_DEFAULTS

A `doc_flags` bit: the per-row documents carry the values the library computed from DEFAULT and MATERIALIZED expressions.

### CHS_DOC_ALL

Every `doc_flags` bit. A bit outside it is refused.

### document:live_handles

A JSON object with one key per handle kind (its type name, such as `chs_schema`) and the number of live handles of that kind in this image.

### document:error_code_table

ClickHouse's own error-code table for this build: every code the vendored table names, with its name, in ascending order. Its exact JSON shape is open.

### document:schema_description

A schema's columns, in declared order, with each column's canonical type, its default kind and expression, and the facts a caller needs to build a row. Column names are byte strings; how a name that is not valid UTF-8 is carried inside JSON is open.

### document:row

One row's verdict and, as the flags ask, its columns' stored values, provenance and transformations. The transformations come from the library, never from a binding.

### document:batch

A body's verdict, counts and per-row documents, and, when an export was asked for, where each accepted row sits in the export bytes.

### document:filter_result

A filter evaluation: the call's outcome, one verdict character per row, and each error or declined row itemized.

### document:discovery

The column declarations reconstructed from a server's `system.columns` rows, formatted by ClickHouse's own formatter.

### chs_abi_version

The ABI generation this library implements: always 1 for this header. A loader resolves and calls it right after opening the library. A library without the symbol is not an ABI v1 artifact, and any other value is refused, naming both values.

### chs_build_info

What this library is, as static, NUL-terminated, ASCII-only JSON in the image's read-only data: never NULL, never freed, the same bytes on every call, and callable straight after opening the library. Its fields are listed under `build_info` in the reference, and `abi_fingerprint` is `CHS_ABI_FINGERPRINT` as the library was built, copied, never recomputed.

A loader parses it (ASCII only, duplicate keys refused, `schema` equal to 1, every required field of its type), refuses when `abi_fingerprint` differs from its own compiled-in `CHS_ABI_FINGERPRINT` byte for byte, and then compares the fields listed in `spec/abi-v1/sdk.json` (`cross_check`) with the verified signed statement it fetched the library under, refusing on the first mismatch and naming the field. That comparison is mandatory. The file's own size, digest and glibc floor are not in it, because the file cannot carry facts measured on itself; they are in the signed statement.

### chs_clickhouse_version

The ClickHouse release this library was built from, spelled as every v0 library spelled it (for example `26.8.15.10-lts`): static, never freed. It keeps its v0 signature and spelling so a v0 binding reaches its own refusal. A v1 binding reads `clickhouse_version` from `chs_build_info` instead.

### chs_abi_revision

The v0 tombstone. It always returns `CHS_ABI_REVISION_TOMBSTONE` (1001). It must never be removed, and its name must never be given another meaning.

A sweep of every released binding (every minor from 0.1 to 0.5, in all four languages; [the CI run](https://github.com/Wave-RF/chtypes/actions/runs/36939764841)) measured why: each one calls this symbol whenever it exists and refuses cleanly, naming both numbers, when it returns a revision other than its own, but treats a MISSING symbol as "revision 0, predates the probe" and goes on to call `chs_init`. So this tombstone is what turns every v0 binding away before it can reach any other symbol, and `gen.py --check` refuses a description that reuses a v0 name without it. A v1 binding never calls it; the loader resolves it only to prove it is present.

### chs_buf_data

The buffer's bytes. The pointer may be NULL when `chs_buf_len` is 0, and a binding never dereferences it then. A NULL, freed, wrong-kind or cross-library buffer gives NULL.

### chs_buf_len

The buffer's length in bytes. A NULL, freed, wrong-kind or cross-library buffer gives 0.

### chs_buf_free

Releases the caller's reference to a buffer. Freeing NULL does nothing.

### chs_error_status

The error's status, never `CHS_OK` for an error a call set. A NULL, freed, wrong-kind or cross-library error gives `CHS_INVALID_ARGUMENT`.

### chs_error_ch_code

ClickHouse's own error code, nonzero only when the status is `CHS_REJECTED`, and 0 otherwise or for an invalid error.

### chs_error_ch_name

A new buffer holding the name this build's vendored error table gives the code, the name a server prints after the code; empty when the build has none or the status is not `CHS_REJECTED`. The form of the three text accessors (a returned buffer, as here, or an out-parameter) is open.

### chs_error_message

A new buffer holding the message: ClickHouse's own text, verbatim, for `CHS_REJECTED`, and the library's explanation otherwise. A message may quote input bytes, so it is a byte string, not text.

### chs_error_column

A new buffer holding the name of the column the error concerns, empty when there is none. A column name is a byte string: NUL and invalid UTF-8 are legal in a name.

### chs_error_free

Releases the caller's reference to an error. Freeing NULL does nothing.

### chs_live_handles

A JSON document counting the live handles of each kind this image has made, taken before the document's own buffer exists. A test abandons handles, lets its runtime collect them and then asserts every count is zero, which is how all four bindings prove their finalizers release everything.

### chs_error_codes

ClickHouse's own error-code table for this build, as a JSON document: a passthrough over the vendored table, never a copy. The table belongs to the build, and one number can name different errors on two ClickHouse lines, so a caller that needs several lines asks each library. The name is v0's, with the v1 call shape; the tombstone keeps every v0 binding from reaching it.

### chs_registered_families

Every type family in this build's own type registry, one per line. A tooling export for the artifact producer's build gates: no binding wraps it, and a loader resolves it only for presence.

### chs_function_flags

A tab-separated audit of every registered function's volatility flags, one function per line, read off this build's own registry. A tooling export for the artifact producer's build gates, which derive the statelessness evidence from it; no binding wraps it.

### chs_reference_type

The widened reference type this build pairs with a type expression. A tooling export; no binding wraps it.

### chs_initialize

Process setup for this image. The proposed form takes nothing: the time zone becomes per call rather than per process, and the list of type families the build must refuse is embedded when the library is built. Whether an initialization call survives at all is open; nothing before it in the load sequence needs it.

### chs_set_defaults

Seeds the settings every later call starts from, as a JSON object whose values are JSON strings (a binding never rewrites a value, a boolean included). A setting name the server would refuse is refused here, with ClickHouse's own code and message, and nothing is committed.

### chs_shutdown

Stops the background work the library started and joins its threads. Safe to call when nothing was started. Whether teardown and unloading are supported at all is open; a loader never unloads a library.

### chs_type_validate

Parses one type expression with ClickHouse's own parser and returns its canonical spelling, or ClickHouse's own refusal.

### chs_back_quote

A name quoted the way ClickHouse's own `backQuote` quotes it: always quoted, every special byte escaped. A passthrough over the vendored function, and named after it; the name is a byte string.

### chs_back_quote_if_needed

A name quoted only where this build's own `backQuoteIfNeed` says it must be. Which names stay bare is a property of the ClickHouse release, so a caller that needs one answer for several releases asks each library.

### chs_quote_string

A byte string spelled as a ClickHouse string literal by the vendored `quoteString`. The input may contain NUL; the answer escapes it.

### chs_schema_create

Compiles one whole `CREATE TABLE` statement (columns, engine, keys, TTL, settings and constraints) with ClickHouse's own parser and the checks a server's CREATE runs, under a profile of settings given as a JSON object of string values. The result is immutable. The proposed form replaces v0's column-list compile and its engine, TTL and partition-key setters.

### chs_schema_free

Releases the caller's reference to a schema. Filters and blocks made from it keep it alive. Freeing NULL does nothing.

### chs_schema_describe

A JSON document describing the schema's columns, owned by the caller; it replaces v0's borrowed column accessors.

### chs_preview_row

Validates and coerces one row of `body` under the schema, as a server's INSERT would, and returns the row's document. `columns` is the INSERT column list, empty for none.

### chs_preview_batch

Validates and coerces a whole body, which may hold many rows, and returns the batch document. Row separation and the server's error allowance are ClickHouse's own. `filter`, when given, is evaluated over each stored row in the same parse. `export_format` is `CHS_EXPORT_NONE` or a `chs_format` the build can write; with an export, `out_export` receives the accepted rows serialized once, and it may be NULL when no export is asked for. `doc_flags` chooses which groups the per-row documents carry.

### chs_filter_create

Compiles a boolean SQL expression over the schema's columns, binding its query parameters (a JSON object of string values) by ClickHouse's own substitution, so a value is never SQL text. Non-deterministic expressions are declined.

### chs_filter_free

Releases the caller's reference to a filter. Freeing NULL does nothing.

### chs_filter_eval_body

Evaluates the filter over every row of a body, returning one verdict per row.

### chs_block_create

Parses a body once under the schema, for evaluating many filters over it.

### chs_block_free

Releases the caller's reference to a block. Freeing NULL does nothing.

### chs_filter_eval_block

Evaluates the filter over a parsed block, with the same answers `chs_filter_eval_body` gives for the same body. The filter and the block must come from the same schema.

### chs_discover_query

The query a caller runs against a server to read a table's `system.columns` rows, so that no binding holds SQL of its own.

### chs_discover_columns

Reads a server's `system.columns` rows (the JSONEachRow answer to `chs_discover_query`'s query) with ClickHouse's own reader and returns the column declarations, formatted by ClickHouse's own formatter.
