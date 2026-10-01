# ABI v1: the description, the header and the generator

ABI v1 is the next generation of the C ABI every binding speaks. It is built on the `v1` branch and is not released. Its declarations are not written by hand anywhere: one JSON description, `spec/abi-v1/abi.json`, states the whole ABI, and `scripts/abi-v1/gen.py` generates the C header, `include/v1/chtypes.h`, the export list, the reference block at the end of this page and, as each lands, every binding's declaration layer, the test stub and the conformance cases. The SDK owns the description and the header; the artifact producer builds libraries from the header.

Until the ABI is confirmed, entries in the description are FIRM or PROVISIONAL. A FIRM entry follows from a decision below and changes only if that decision does. A PROVISIONAL entry is the proposed form of something still being decided; it carries one or more markers naming what it waits on, and the generated reference below lists every marker and what it covers. Either way a change is an edit to the description plus `gen.py --write`.

## The decisions

The artifact producer and the SDK decided these for ABI v1. The description cites them by number.

| decision | what it fixes                                                                                                                                                                                                                                                                                                                                                                                                                           |
| -------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| D1       | Identity. A JSON description generates the header and the bindings' declarations; identity is `CHS_ABI_VERSION` (the generation, 1) plus `CHS_ABI_FINGERPRINT`, matched exactly. D1.1: the fingerprint's form. D1.2: the handshake set, frozen forever. D1.3: the library name and the `chs_` prefix are frozen; reusing a v0 name. D1.4: `chs_format` 0 to 11 carries over, frozen. D1.5: the SDK owns the description and the header. |
| D2       | Ownership. Counted bytes in, owned opaque buffers out, no borrowed pointers, reference-counted handles tagged with their kind and their image, any free order safe.                                                                                                                                                                                                                                                                     |
| D3       | Errors. One call shape, `chs_status f(..., T **out, chs_error **err)`, with a closed five-value status and an owned error carrying ClickHouse's own code, name and message.                                                                                                                                                                                                                                                             |
| D4       | Provenance. A static `chs_build_info()`, and a mandatory cross-check of it against the verified signed statement after the library is opened.                                                                                                                                                                                                                                                                                           |
| D5       | What v1 drops from v0: the presence probes and degradation, the sentinel codes, NUL-terminated inputs, `chs_free`, the borrowed column accessors, the init-time unsafe-families list, and the per-binding format probes. The three tooling calls stay exported, unwrapped.                                                                                                                                                              |
| D6       | Derived results move into the library: the `Transformed` classifier and outcome promotion are computed in C, and a binding decodes the documents one to one.                                                                                                                                                                                                                                                                            |

## The description

| file                                 | what it holds                                                                                                                                                                                | fingerprinted |
| ------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------- |
| `spec/abi-v1/abi.json`               | The ABI: handles, enums and document vocabularies, constants, documents, the `build_info` shape, and every function with its parameters, return, statuses, class and thread class. No prose. | yes           |
| `spec/abi-v1/schema/abi.schema.json` | Its JSON Schema.                                                                                                                                                                             | no            |
| `spec/abi-v1/sdk.json`               | SDK policy: each status's error class per binding, the loader's refusal reasons, the `build_info` cross-check map, `reuse_v0_names`, the marker legend, per-binding naming overrides.        | no            |
| `spec/abi-v1/docs.md`                | The prose, one section per symbol, copied into the header and into this page.                                                                                                                | no            |
| `spec/abi-v1/v0-symbols.json`        | Every v0 prototype at every released tag, for the name-reuse check. Written once by `gen.py --extract-v0`.                                                                                   | no            |
| `spec/abi-v1/generated/exports.txt`  | Every exported symbol, sorted: generated, for an export list.                                                                                                                                | no            |

Anything a binding or the artifact producer must act on is structured in `abi.json`, never only stated in prose: a parameter's nullability, an output's owner, the statuses a call may return, a thread class, a vocabulary and its per-value facts. That is what lets prose stay outside the fingerprint.

The description is ASCII, has no floating-point numbers, keeps every integer below 2^53 in magnitude and never repeats an object key. `gen.py` refuses anything else, and those restrictions are what make the canonical form exact.

Every counted input and every output buffer is a byte string, never a C string, and each one names its content (the generated Contents table). A column name is a byte string: ClickHouse accepts and round-trips names containing NUL and invalid UTF-8 (measured on live servers on every supported line), so a binding's type for a name never assumes UTF-8. How such a name is carried inside a JSON document is not decided yet (marker A12).

## The fingerprint

`CHS_ABI_FINGERPRINT` is `sha256:` followed by the 64 lowercase hex digits of the sha256 of the RFC 8785 (JCS) canonical form of `spec/abi-v1/abi.json`. `scripts/abi-v1/jcs.py` computes it with the standard library, refusing any input outside the subset where that is exact, and CI recomputes it with an independent RFC 8785 implementation and requires the same value.

It is computed in one place and copied everywhere else, byte for byte: the header's macro; `chs_build_info()`'s `abi_fingerprint`, which the artifact producer takes from the macro; the signed statement and the manifest, which take it from the library; and each binding's compiled-in constant. A loader compares it by byte equality and never interprets it. Any edit to `abi.json` changes it, which invalidates every library built from an earlier description, so once the ABI is confirmed a change to the description waits for the artifact producer's agreement.

## Loading a library

Every binding loads a library in the same steps, and refuses at the first that fails, naming what it expected and what it found.

1. On Linux, before the library is opened: the process's glibc version, read with `gnu_get_libc_version`, must be at least the signed statement's `glibc_floor`, compared as dotted integers. No glibc (musl) refuses, and so does a Linux statement without `glibc_floor`. There is no check on macOS.
2. Open it with `RTLD_NOW | RTLD_LOCAL`, so a missing dependency refuses here rather than at a later call.
3. Resolve and call `chs_abi_version`. A library without it is not an ABI v1 artifact; any value other than `CHS_ABI_VERSION` refuses.
4. Parse `chs_build_info()` and compare its `abi_fingerprint` with the compiled-in `CHS_ABI_FINGERPRINT`, byte for byte.
5. Compare the `build_info` fields `spec/abi-v1/sdk.json` lists under `cross_check` with the verified signed statement.
6. Resolve every described symbol; all are mandatory.
7. Then, and only then, anything the provisional initialization contract adds.

Until step 6 passes, only the handshake set may be called. The loader never unloads a library. Each refusal reason, and the error class it maps to in each binding, is in `spec/abi-v1/sdk.json`.

## Reusing a v0 name, and the tombstone

A v0 binding that loads a v1 library must be turned away before it calls anything it would call through a v0 declaration. A sweep of every released binding (every minor from 0.1 to 0.5, in all four languages) measured how they behave: each calls `chs_abi_revision` whenever it exists and refuses cleanly when it returns a revision other than its own, but treats a missing `chs_abi_revision` as "revision 0" and goes on to call `chs_init`.

So:

- `chs_abi_revision` is a mandatory v1 export: no parameters, always returning `CHS_ABI_REVISION_TOMBSTONE` (1001). It is never removed, and its name is never given another meaning. `gen.py --check` refuses a description that reuses any v0 name without it.
- With the tombstone in place every released binding stops before any other v0 name, so a v1 function may reuse a v0 name with a new signature. `reuse_v0_names` in `spec/abi-v1/sdk.json` records that, and is true. Were it false, `gen.py --check` would refuse every described function that reuses a v0 name with a different signature; a name reused with its v0 signature unchanged (`chs_clickhouse_version`, `chs_abi_revision`, `chs_shutdown`, the free functions) is never a collision either way.

## Generating, and the checks

```sh
scripts/abi-v1/gen.py --write        # regenerate every output after editing spec/abi-v1/
scripts/abi-v1/gen.py --check        # what CI runs: any drift, stale output or refusal fails
scripts/abi-v1/gen.py --fingerprint  # print CHS_ABI_FINGERPRINT
scripts/abi-v1/gen.py --selftest     # prove each refusal fires, on temporary copies
scripts/abi-v1/gen.py --render EMITTER --out DIR   # build-time files never committed, such as the test stub's source
```

Every generated file starts with a banner naming the fingerprint and saying not to edit it, and `--check` fails on any difference, naming the file and the first differing line. Each output family is one module under `scripts/abi-v1/emit/`, discovered by file name. `scripts/abi-v1/check-no-hand-decls.py` fails on any `chs_*` declaration or symbol lookup written by hand outside the generated files, so nothing hand-written ever touches a raw entry point.

The `v1-abi` workflow runs all of it: `v1-abi-gen` checks the description, the fingerprint (twice, independently), the header as C11 and as C++20, and the hand-declaration rule.

## Reference

<!-- BEGIN GENERATED: abi-v1 -->
<!-- GENERATED by scripts/abi-v1/gen.py from spec/abi-v1/abi.json (CHS_ABI_FINGERPRINT sha256:b691663f42fb9ca2a872909e73a06fb61836abf92ce552a95b21baf4e006c4ed) — DO NOT EDIT -->

### Identity

| item                  | value                                                                     |
| --------------------- | ------------------------------------------------------------------------- |
| `CHS_ABI_VERSION`     | 1                                                                         |
| `CHS_ABI_FINGERPRINT` | `sha256:b691663f42fb9ca2a872909e73a06fb61836abf92ce552a95b21baf4e006c4ed` |
| library               | `libchtypes`, exporting only `chs_*`                                      |
| functions             | 38: 31 api, 3 handshake, 1 tombstone, 3 tooling                           |
| FIRM functions        | 15                                                                        |
| provisional functions | 23                                                                        |
| `reuse_v0_names`      | true                                                                      |

### Provisional markers

| marker  | waits on                                                                                                         | entries                                                                                                                                                                                                                                                                                                                                                                    |
| ------- | ---------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| A4      | the content of build_info capabilities: which formats and document flags a build reports                         | build_info `capabilities`                                                                                                                                                                                                                                                                                                                                                  |
| A5      | process initialization and the time-zone contract: whether an init call survives, and where a zone is set        | `chs_initialize`                                                                                                                                                                                                                                                                                                                                                           |
| A6      | settings channels and process defaults: whether a context handle exists and which calls take it                  | `chs_set_defaults`, `chs_type_validate`, `chs_schema_create`, `chs_discover_query`, `chs_discover_columns`                                                                                                                                                                                                                                                                 |
| A8      | schema construction: one call over a whole CREATE TABLE statement, and its settings input                        | `chs_schema`, `chs_schema_create`, `chs_schema_free`, `chs_schema_describe`, `chs_preview_row`, `chs_preview_batch`, `chs_filter_create`, `chs_block_create`                                                                                                                                                                                                               |
| A9      | how filters and blocks bind to a schema                                                                          | `chs_filter`, `chs_block`, `filter_outcome`, `filter_verdict`, document `filter_result`, `chs_preview_batch`, `chs_filter_create`, `chs_filter_free`, `chs_filter_eval_body`, `chs_block_create`, `chs_block_free`, `chs_filter_eval_block`                                                                                                                                |
| A10     | the row, batch and filter result documents: fields, outcomes, vocabularies, and which refusals are call statuses | `transform_reason`, `value_src`, `row_outcome`, `batch_outcome`, `filter_outcome`, `filter_verdict`, `CHS_EXPORT_NONE`, `CHS_DOC_VALUES`, `CHS_DOC_TRANSFORMS`, `CHS_DOC_DEFAULTS`, `CHS_DOC_ALL`, document `row`, document `batch`, document `filter_result`, `chs_preview_row`, `chs_preview_batch`, `chs_filter_eval_body`, `chs_block_create`, `chs_filter_eval_block` |
| A11     | derived results computed in the library: the Transformed classifier and the discovery kit                        | `transform_reason`, `value_src`, document `row`, document `batch`, document `discovery`, `chs_preview_row`, `chs_preview_batch`, `chs_discover_query`, `chs_discover_columns`                                                                                                                                                                                              |
| A12     | column introspection, and how a column name that is not valid UTF-8 is carried inside a JSON document            | document `schema_description`, document `row`, document `batch`, document `discovery`, `chs_schema_describe`, `chs_preview_row`, `chs_preview_batch`, `chs_block_create`, `chs_discover_columns`                                                                                                                                                                           |
| A13     | teardown: shutdown, background threads, and unloading the library                                                | `chs_shutdown`                                                                                                                                                                                                                                                                                                                                                             |
| Q-c     | the form of the error accessors' text results, and the error-code table's buffer format                          | document `error_code_table`, `chs_error_ch_name`, `chs_error_message`, `chs_error_column`                                                                                                                                                                                                                                                                                  |
| Q-f     | whether the discovery call also returns the query text a caller runs against a server                            | `chs_discover_query`                                                                                                                                                                                                                                                                                                                                                       |
| confirm | outside the decided set: the SDK's proposed form, waiting on the confirmation of the whole ABI                   | document `live_handles`, `chs_back_quote`, `chs_back_quote_if_needed`, `chs_quote_string`                                                                                                                                                                                                                                                                                  |

### Parameter kinds

| kind         | C spelling                                               | rule                                                                                                                                      |
| ------------ | -------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| `scalar`     | `int32_t`, `uint32_t`, `int64_t`, `uint64_t` or `size_t` | passed by value                                                                                                                           |
| `enum`       | the enum's `int32_t` typedef                             | the binding passes the integer                                                                                                            |
| `bytes_in`   | `const uint8_t *<name>, size_t <name>_len`               | length 0 is none and the pointer may then be NULL; NULL with a nonzero length is CHS_INVALID_ARGUMENT; NUL and invalid UTF-8 pass through |
| `handle`     | `[const] chs_K *`                                        | kind and image tags checked; NULL only where marked nullable                                                                              |
| `out_handle` | `chs_K **`                                               | the caller owns the result and frees it with the handle's free, in any order                                                              |
| `out_error`  | `chs_error **`                                           | always optional; set only when the status is not CHS_OK                                                                                   |
| `out_scalar` | `int32_t *` and the like                                 | reserved; no function uses it                                                                                                             |

### Contents

Every counted input and every `chs_buf` is a byte string; the content says what it carries.

| content                     | carries                                                                                     |
| --------------------------- | ------------------------------------------------------------------------------------------- |
| `bytes`                     | arbitrary bytes: a body, a string value, an export                                          |
| `name`                      | a ClickHouse column name: NUL and invalid UTF-8 are legal, so never assume UTF-8            |
| `sql`                       | SQL text (a statement, an expression, a type, a quoted name or literal): may carry any byte |
| `message`                   | a ClickHouse message: may quote input bytes, so may carry any byte                          |
| `ascii`                     | guaranteed ASCII                                                                            |
| `json_object_string_values` | a JSON object whose values are JSON strings (settings, query parameters)                    |
| `json_array_names`          | a JSON array of column names (the encoding of a non-UTF-8 name is provisional: A12)         |
| `document:<name>`           | a JSON document, listed under Documents below                                               |

### Handles

| handle       | freed by          | holds        | status |
| ------------ | ----------------- | ------------ | ------ |
| `chs_buf`    | `chs_buf_free`    | -            | FIRM   |
| `chs_error`  | `chs_error_free`  | -            | FIRM   |
| `chs_schema` | `chs_schema_free` | -            | A8     |
| `chs_filter` | `chs_filter_free` | `chs_schema` | A9     |
| `chs_block`  | `chs_block_free`  | `chs_schema` | A9     |

#### `chs_buf`

An owned, immutable byte buffer: every output of the library. Read it with `chs_buf_data` and `chs_buf_len`; the bytes are exactly what the library produced, with no terminator and no encoding promise beyond what the producing parameter's content says.

#### `chs_error`

The error a fallible call reports when its status is not `CHS_OK`: the status, ClickHouse's own error code and name, a message and the column concerned. Created only by the library, through a call's `err` out-parameter.

#### `chs_schema`

A compiled table: the columns, engine, keys and settings of one `CREATE TABLE` statement, immutable once created.

#### `chs_filter`

A compiled boolean SQL expression over a schema's columns, with its query parameters bound.

#### `chs_block`

A body parsed once under a schema, for evaluating many filters without parsing again.

### Enums

#### `chs_status`

The verdict of every fallible call, in five values whose numbering is frozen.

- `CHS_OK`: the call succeeded and its outputs are set.
- `CHS_REJECTED`: ClickHouse's own refusal, with its own code, name and message, which a server would also give.
- `CHS_DECLINED`: this build will not answer; a server might accept. Never scored as agreement.
- `CHS_INVALID_ARGUMENT`: caller misuse, such as a NULL pointer with a nonzero length, a wrong-kind, freed or cross-library handle, or a NULL required out-parameter.
- `CHS_INTERNAL`: a guarded exception inside the library, which is a library bug.

The set is closed within this generation: any new condition maps onto one of these five. A binding maps each status to one error class (`spec/abi-v1/sdk.json`), and treats a value outside the set as an internal error naming it.

`int32_t`; closed, frozen; FIRM.

| name                   | value |
| ---------------------- | ----- |
| `CHS_OK`               | 0     |
| `CHS_REJECTED`         | 1     |
| `CHS_DECLINED`         | 2     |
| `CHS_INVALID_ARGUMENT` | 3     |
| `CHS_INTERNAL`         | 4     |

#### `chs_format`

The input and export formats, numbered as they have been since the first release; the numbers are frozen and a binding passes the integer. `ch_name` is ClickHouse's own name for the format, which is how `chs_build_info` lists the formats a build supports. Being in this enum never proves a build supports a format: the build says so in its `capabilities`.

`int32_t`; frozen; FIRM.

| name                                               | value | ch_name                               |
| -------------------------------------------------- | ----- | ------------------------------------- |
| `CHS_JSON_EACH_ROW`                                | 0     | JSONEachRow                           |
| `CHS_CSV`                                          | 1     | CSV                                   |
| `CHS_TSV`                                          | 2     | TSV                                   |
| `CHS_VALUES`                                       | 3     | Values                                |
| `CHS_JSON_COMPACT_EACH_ROW`                        | 4     | JSONCompactEachRow                    |
| `CHS_ROW_BINARY`                                   | 5     | RowBinary                             |
| `CHS_ROW_BINARY_WITH_DEFAULTS`                     | 6     | RowBinaryWithDefaults                 |
| `CHS_ROW_BINARY_WITH_NAMES_AND_TYPES_AND_DEFAULTS` | 7     | RowBinaryWithNamesAndTypesAndDefaults |
| `CHS_NATIVE`                                       | 8     | Native                                |
| `CHS_BUFFERS`                                      | 9     | Buffers                               |
| `CHS_CSV_WITH_NAMES`                               | 10    | CSVWithNames                          |
| `CHS_TSV_WITH_NAMES`                               | 11    | TSVWithNames                          |

### Document vocabularies

#### `transform_reason`

The reason a stored value differs from the supplied one, as the library reports it per column. `lossy` says whether information was lost; exactly four reasons are lossless (a representation change, a value filled from a DEFAULT or the type's zero, and a DEFAULT the library resolved from its own clock), and they are still reported because a preview must show what the table will hold. A binding reads `lossy` from here and never keeps a list of its own.

A10, A11; an unrecognized value is read as `value_changed`.

| value                   | lossy |
| ----------------------- | ----- |
| `overflow_wrap`         | true  |
| `null_to_default`       | true  |
| `null_loss`             | true  |
| `decimal_truncate`      | true  |
| `date_clamp`            | true  |
| `datetime_wrap`         | true  |
| `date_shift`            | true  |
| `uuid_mangle`           | true  |
| `ip_mangle`             | true  |
| `float_precision`       | true  |
| `lossy_numeric`         | true  |
| `fixedstring_pad`       | true  |
| `emptied`               | true  |
| `element_changed`       | true  |
| `enum_coerce`           | true  |
| `value_changed`         | true  |
| `poisoned`              | true  |
| `duplicate_key_dropped` | true  |
| `reformat`              | false |
| `default_filled`        | false |
| `zero_filled`           | false |
| `default_materialized`  | false |
| `ttl_expired`           | true  |
| `ttl_column_expired`    | true  |

#### `value_src`

Where a column's value came from. `is_stored` says whether the row as stored carries a value for the column: a column the input named but the table never stores (an EPHEMERAL column, a skipped MATERIALIZED or ALIAS column) and a DEFAULT the library could not resolve carry none.

A10, A11.

| value                         | is_stored |
| ----------------------------- | --------- |
| `input`                       | true      |
| `default`                     | true      |
| `default_substituted`         | true      |
| `absent`                      | true      |
| `materialized_input`          | true      |
| `skipped`                     | false     |
| `ephemeral_input`             | false     |
| `default_expr_unsupported`    | false     |
| `default_volatile_unresolved` | false     |
| `default_pending`             | false     |

#### `row_outcome`

The verdict on one row: accepted, accepted but unreadable afterwards (the insert succeeds and every later read fails), rejected, skipped under the server's error allowance with the batch continuing, or declined by this build.

A10; an unrecognized value is read as `unsupported`.

| value               |
| ------------------- |
| `accepted`          |
| `accepted_poisoned` |
| `rejected`          |
| `skipped`           |
| `unsupported`       |

#### `batch_outcome`

The verdict on a whole body. A batch is never skipped; its rows carry their own outcomes.

A10; an unrecognized value is read as `unsupported`.

| value               |
| ------------------- |
| `accepted`          |
| `accepted_poisoned` |
| `rejected`          |
| `unsupported`       |

#### `filter_outcome`

Whether a filter evaluation completed at all. Per-row failures are verdicts, not outcomes.

A9, A10; an unrecognized value is read as `unsupported`.

| value         |
| ------------- |
| `ok`          |
| `rejected`    |
| `unsupported` |

#### `filter_verdict`

One character per row of a filter evaluation. `t` and `f` are answers (true; false or NULL); `e` (the predicate raised an error on this row) and `d` (this build declines the row) are not, and a caller enforcing visibility must fail closed on both.

A9, A10; an unrecognized value is read as `d`.

| value | answered |
| ----- | -------- |
| `t`   | true     |
| `f`   | true     |
| `e`   | false    |
| `d`   | false    |

### Constants

| constant                     | type   | value | status |
| ---------------------------- | ------ | ----- | ------ |
| `CHS_ABI_REVISION_TOMBSTONE` | int32  | 1001  | FIRM   |
| `CHS_EXPORT_NONE`            | int32  | -1    | A10    |
| `CHS_DOC_VALUES`             | uint32 | 1     | A10    |
| `CHS_DOC_TRANSFORMS`         | uint32 | 2     | A10    |
| `CHS_DOC_DEFAULTS`           | uint32 | 4     | A10    |
| `CHS_DOC_ALL`                | uint32 | 7     | A10    |

### Documents

| document             | carries names | status        |
| -------------------- | ------------- | ------------- |
| `live_handles`       | no            | confirm       |
| `error_code_table`   | no            | Q-c           |
| `schema_description` | yes           | A12           |
| `row`                | yes           | A10, A11, A12 |
| `batch`              | yes           | A10, A11, A12 |
| `filter_result`      | no            | A9, A10       |
| `discovery`          | yes           | A11, A12      |

### build_info

| field                | shape                                        | required | cross-checked | content |
| -------------------- | -------------------------------------------- | -------- | ------------- | ------- |
| `schema`             | integer = 1                                  | yes      | -             | FIRM    |
| `abi`                | integer                                      | yes      | int           | FIRM    |
| `abi_fingerprint`    | string `^sha256:[0-9a-f]{64}$`               | yes      | bytes         | FIRM    |
| `clickhouse_version` | string `^[0-9]+[.][0-9]+[.][0-9]+[.][0-9]+$` | yes      | bytes         | FIRM    |
| `channel`            | string                                       | yes      | bytes         | FIRM    |
| `clickhouse_minor`   | string `^[0-9]+[.][0-9]+$`                   | yes      | -             | FIRM    |
| `clickhouse_commit`  | string `^[0-9a-f]{40}$`                      | yes      | -             | FIRM    |
| `core_commit`        | string `^[0-9a-f]{40}$`                      | yes      | bytes         | FIRM    |
| `build`              | string `^[0-9]{8}[.][0-9]{6}$`               | yes      | bytes         | FIRM    |
| `inputs_sha256`      | string `^[0-9a-f]{64}$`                      | yes      | bytes         | FIRM    |
| `os`                 | string `^[a-z0-9]+$`                         | yes      | bytes         | FIRM    |
| `arch`               | string `^[a-z0-9]+$`                         | yes      | bytes         | FIRM    |
| `toolchain`          | object                                       | yes      | -             | FIRM    |
| `capabilities`       | object                                       | yes      | -             | A4      |

### Functions

| function                   | class     | thread         | returns           | status                |
| -------------------------- | --------- | -------------- | ----------------- | --------------------- |
| `chs_abi_version`          | handshake | any            | `int32_t`         | FIRM                  |
| `chs_build_info`           | handshake | any            | `const char *`    | FIRM                  |
| `chs_clickhouse_version`   | handshake | any            | `const char *`    | FIRM                  |
| `chs_abi_revision`         | tombstone | any            | `int`             | FIRM                  |
| `chs_buf_data`             | api       | any            | `const uint8_t *` | FIRM                  |
| `chs_buf_len`              | api       | any            | `size_t`          | FIRM                  |
| `chs_buf_free`             | api       | handle_serial  | `void`            | FIRM                  |
| `chs_error_status`         | api       | any            | `chs_status`      | FIRM                  |
| `chs_error_ch_code`        | api       | any            | `int32_t`         | FIRM                  |
| `chs_error_ch_name`        | api       | any            | `chs_buf *`       | Q-c                   |
| `chs_error_message`        | api       | any            | `chs_buf *`       | Q-c                   |
| `chs_error_column`         | api       | any            | `chs_buf *`       | Q-c                   |
| `chs_error_free`           | api       | handle_serial  | `void`            | FIRM                  |
| `chs_live_handles`         | api       | any            | status            | FIRM                  |
| `chs_error_codes`          | api       | any            | status            | FIRM                  |
| `chs_registered_families`  | tooling   | process_serial | status            | FIRM                  |
| `chs_function_flags`       | tooling   | process_serial | status            | FIRM                  |
| `chs_reference_type`       | tooling   | process_serial | status            | FIRM                  |
| `chs_initialize`           | api       | process_once   | status            | A5                    |
| `chs_set_defaults`         | api       | process_serial | status            | A6                    |
| `chs_shutdown`             | api       | process_serial | `void`            | A13                   |
| `chs_type_validate`        | api       | any            | status            | A6                    |
| `chs_back_quote`           | api       | any            | status            | confirm               |
| `chs_back_quote_if_needed` | api       | any            | status            | confirm               |
| `chs_quote_string`         | api       | any            | status            | confirm               |
| `chs_schema_create`        | api       | any            | status            | A6, A8                |
| `chs_schema_free`          | api       | handle_serial  | `void`            | A8                    |
| `chs_schema_describe`      | api       | handle_serial  | status            | A8, A12               |
| `chs_preview_row`          | api       | handle_serial  | status            | A8, A10, A11, A12     |
| `chs_preview_batch`        | api       | handle_serial  | status            | A8, A9, A10, A11, A12 |
| `chs_filter_create`        | api       | handle_serial  | status            | A8, A9                |
| `chs_filter_free`          | api       | handle_serial  | `void`            | A9                    |
| `chs_filter_eval_body`     | api       | handle_serial  | status            | A9, A10               |
| `chs_block_create`         | api       | handle_serial  | status            | A8, A9, A10, A12      |
| `chs_block_free`           | api       | handle_serial  | `void`            | A9                    |
| `chs_filter_eval_block`    | api       | handle_serial  | status            | A9, A10               |
| `chs_discover_query`       | api       | any            | status            | A6, A11, Q-f          |
| `chs_discover_columns`     | api       | any            | status            | A6, A11, A12          |

#### `chs_abi_version`

```c
CHS_API int32_t chs_abi_version(void);
```

- Class `handshake`, thread `any`, FIRM.

The ABI generation this library implements: always 1 for this header. A loader resolves and calls it right after opening the library. A library without the symbol is not an ABI v1 artifact, and any other value is refused, naming both values.

#### `chs_build_info`

```c
CHS_API const char *chs_build_info(void);
```

- Class `handshake`, thread `any`, FIRM.

What this library is, as static, NUL-terminated, ASCII-only JSON in the image's read-only data: never NULL, never freed, the same bytes on every call, and callable straight after opening the library. Its fields are listed under `build_info` in the reference, and `abi_fingerprint` is `CHS_ABI_FINGERPRINT` as the library was built, copied, never recomputed.

A loader parses it (ASCII only, duplicate keys refused, `schema` equal to 1, every required field of its type), refuses when `abi_fingerprint` differs from its own compiled-in `CHS_ABI_FINGERPRINT` byte for byte, and then compares the fields listed in `spec/abi-v1/sdk.json` (`cross_check`) with the verified signed statement it fetched the library under, refusing on the first mismatch and naming the field. That comparison is mandatory. The file's own size, digest and glibc floor are not in it, because the file cannot carry facts measured on itself; they are in the signed statement.

#### `chs_clickhouse_version`

```c
CHS_API const char *chs_clickhouse_version(void);
```

- Class `handshake`, thread `any`, FIRM.

The ClickHouse release this library was built from, spelled as every v0 library spelled it (for example `26.8.15.10-lts`): static, never freed. It keeps its v0 signature and spelling so a v0 binding reaches its own refusal. A v1 binding reads `clickhouse_version` from `chs_build_info` instead.

#### `chs_abi_revision`

```c
CHS_API int chs_abi_revision(void);
```

- Class `tombstone`, thread `any`, FIRM.
- Always returns `CHS_ABI_REVISION_TOMBSTONE`.

The v0 tombstone. It always returns `CHS_ABI_REVISION_TOMBSTONE` (1001). It must never be removed, and its name must never be given another meaning.

A sweep of every released binding (every minor from 0.1 to 0.5, in all four languages; [the CI run](https://github.com/Wave-RF/chtypes/actions/runs/36939764841)) measured why: each one calls this symbol whenever it exists and refuses cleanly, naming both numbers, when it returns a revision other than its own, but treats a MISSING symbol as "revision 0, predates the probe" and goes on to call `chs_init`. So this tombstone is what turns every v0 binding away before it can reach any other symbol, and `gen.py --check` refuses a description that reuses a v0 name without it. A v1 binding never calls it; the loader resolves it only to prove it is present.

#### `chs_buf_data`

```c
CHS_API const uint8_t *chs_buf_data(const chs_buf *buf);
```

- Class `api`, thread `any`, FIRM.
- The pointer is borrowed from `buf`.
- `buf`: handle, `chs_buf`.

The buffer's bytes. The pointer may be NULL when `chs_buf_len` is 0, and a binding never dereferences it then. A NULL, freed, wrong-kind or cross-library buffer gives NULL.

#### `chs_buf_len`

```c
CHS_API size_t chs_buf_len(const chs_buf *buf);
```

- Class `api`, thread `any`, FIRM.
- `buf`: handle, `chs_buf`.

The buffer's length in bytes. A NULL, freed, wrong-kind or cross-library buffer gives 0.

#### `chs_buf_free`

```c
CHS_API void chs_buf_free(chs_buf *buf);
```

- Class `api`, thread `handle_serial`, FIRM.
- `buf`: handle, `chs_buf`, nullable.

Releases the caller's reference to a buffer. Freeing NULL does nothing.

#### `chs_error_status`

```c
CHS_API chs_status chs_error_status(const chs_error *error);
```

- Class `api`, thread `any`, FIRM.
- `error`: handle, `chs_error`.

The error's status, never `CHS_OK` for an error a call set. A NULL, freed, wrong-kind or cross-library error gives `CHS_INVALID_ARGUMENT`.

#### `chs_error_ch_code`

```c
CHS_API int32_t chs_error_ch_code(const chs_error *error);
```

- Class `api`, thread `any`, FIRM.
- `error`: handle, `chs_error`.

ClickHouse's own error code, nonzero only when the status is `CHS_REJECTED`, and 0 otherwise or for an invalid error.

#### `chs_error_ch_name`

```c
CHS_API chs_buf *chs_error_ch_name(const chs_error *error);
```

- Class `api`, thread `any`, Q-c.
- Returns a new `chs_buf` the caller owns (ascii), freed with `chs_buf_free`.
- `error`: handle, `chs_error`.

A new buffer holding the name this build's vendored error table gives the code, the name a server prints after the code; empty when the build has none or the status is not `CHS_REJECTED`. The form of the three text accessors (a returned buffer, as here, or an out-parameter) is open.

#### `chs_error_message`

```c
CHS_API chs_buf *chs_error_message(const chs_error *error);
```

- Class `api`, thread `any`, Q-c.
- Returns a new `chs_buf` the caller owns (message), freed with `chs_buf_free`.
- `error`: handle, `chs_error`.

A new buffer holding the message: ClickHouse's own text, verbatim, for `CHS_REJECTED`, and the library's explanation otherwise. A message may quote input bytes, so it is a byte string, not text.

#### `chs_error_column`

```c
CHS_API chs_buf *chs_error_column(const chs_error *error);
```

- Class `api`, thread `any`, Q-c.
- Returns a new `chs_buf` the caller owns (name), freed with `chs_buf_free`.
- `error`: handle, `chs_error`.

A new buffer holding the name of the column the error concerns, empty when there is none. A column name is a byte string: NUL and invalid UTF-8 are legal in a name.

#### `chs_error_free`

```c
CHS_API void chs_error_free(chs_error *error);
```

- Class `api`, thread `handle_serial`, FIRM.
- `error`: handle, `chs_error`, nullable.

Releases the caller's reference to an error. Freeing NULL does nothing.

#### `chs_live_handles`

```c
CHS_API chs_status chs_live_handles(chs_buf **out, chs_error **err);
```

- Class `api`, thread `any`, FIRM.
- May return `CHS_OK`, `CHS_INVALID_ARGUMENT`, `CHS_INTERNAL`.
- `out`: out_handle, `chs_buf`, `document:live_handles`.
- `err`: out_error.

A JSON document counting the live handles of each kind this image has made, taken before the document's own buffer exists. A test abandons handles, lets its runtime collect them and then asserts every count is zero, which is how all four bindings prove their finalizers release everything.

#### `chs_error_codes`

```c
CHS_API chs_status chs_error_codes(chs_buf **out, chs_error **err);
```

- Class `api`, thread `any`, FIRM.
- May return `CHS_OK`, `CHS_INVALID_ARGUMENT`, `CHS_INTERNAL`.
- `out`: out_handle, `chs_buf`, `document:error_code_table`.
- `err`: out_error.

ClickHouse's own error-code table for this build, as a JSON document: a passthrough over the vendored table, never a copy. The table belongs to the build, and one number can name different errors on two ClickHouse lines, so a caller that needs several lines asks each library. The name is v0's, with the v1 call shape; the tombstone keeps every v0 binding from reaching it.

#### `chs_registered_families`

```c
CHS_API chs_status chs_registered_families(chs_buf **out, chs_error **err);
```

- Class `tooling`, thread `process_serial`, FIRM.
- May return `CHS_OK`, `CHS_INVALID_ARGUMENT`, `CHS_INTERNAL`.
- `out`: out_handle, `chs_buf`, `ascii`.
- `err`: out_error.

Every type family in this build's own type registry, one per line. A tooling export for the artifact producer's build gates: no binding wraps it, and a loader resolves it only for presence.

#### `chs_function_flags`

```c
CHS_API chs_status chs_function_flags(chs_buf **out, chs_error **err);
```

- Class `tooling`, thread `process_serial`, FIRM.
- May return `CHS_OK`, `CHS_INVALID_ARGUMENT`, `CHS_INTERNAL`.
- `out`: out_handle, `chs_buf`, `ascii`.
- `err`: out_error.

A tab-separated audit of every registered function's volatility flags, one function per line, read off this build's own registry. A tooling export for the artifact producer's build gates, which derive the statelessness evidence from it; no binding wraps it.

#### `chs_reference_type`

```c
CHS_API chs_status chs_reference_type(const uint8_t *type_expr, size_t type_expr_len, chs_buf **out, chs_error **err);
```

- Class `tooling`, thread `process_serial`, FIRM.
- May return `CHS_OK`, `CHS_REJECTED`, `CHS_DECLINED`, `CHS_INVALID_ARGUMENT`, `CHS_INTERNAL`.
- `type_expr`: bytes_in, `sql`.
- `out`: out_handle, `chs_buf`, `sql`.
- `err`: out_error.

The widened reference type this build pairs with a type expression. A tooling export; no binding wraps it.

#### `chs_initialize`

```c
CHS_API chs_status chs_initialize(chs_error **err);
```

- Class `api`, thread `process_once`, A5.
- May return `CHS_OK`, `CHS_INVALID_ARGUMENT`, `CHS_INTERNAL`.
- `err`: out_error.

Process setup for this image. The proposed form takes nothing: the time zone becomes per call rather than per process, and the list of type families the build must refuse is embedded when the library is built. Whether an initialization call survives at all is open; nothing before it in the load sequence needs it.

#### `chs_set_defaults`

```c
CHS_API chs_status chs_set_defaults(const uint8_t *settings, size_t settings_len, chs_error **err);
```

- Class `api`, thread `process_serial`, A6.
- May return `CHS_OK`, `CHS_REJECTED`, `CHS_DECLINED`, `CHS_INVALID_ARGUMENT`, `CHS_INTERNAL`.
- `settings`: bytes_in, `json_object_string_values`.
- `err`: out_error.

Seeds the settings every later call starts from, as a JSON object whose values are JSON strings (a binding never rewrites a value, a boolean included). A setting name the server would refuse is refused here, with ClickHouse's own code and message, and nothing is committed.

#### `chs_shutdown`

```c
CHS_API void chs_shutdown(void);
```

- Class `api`, thread `process_serial`, A13.

Stops the background work the library started and joins its threads. Safe to call when nothing was started. Whether teardown and unloading are supported at all is open; a loader never unloads a library.

#### `chs_type_validate`

```c
CHS_API chs_status chs_type_validate(const uint8_t *type_expr, size_t type_expr_len, chs_buf **out, chs_error **err);
```

- Class `api`, thread `any`, A6.
- May return `CHS_OK`, `CHS_REJECTED`, `CHS_DECLINED`, `CHS_INVALID_ARGUMENT`, `CHS_INTERNAL`.
- `type_expr`: bytes_in, `sql`.
- `out`: out_handle, `chs_buf`, `sql`.
- `err`: out_error.

Parses one type expression with ClickHouse's own parser and returns its canonical spelling, or ClickHouse's own refusal.

#### `chs_back_quote`

```c
CHS_API chs_status chs_back_quote(const uint8_t *name, size_t name_len, chs_buf **out, chs_error **err);
```

- Class `api`, thread `any`, confirm.
- May return `CHS_OK`, `CHS_INVALID_ARGUMENT`, `CHS_INTERNAL`.
- `name`: bytes_in, `name`.
- `out`: out_handle, `chs_buf`, `sql`.
- `err`: out_error.

A name quoted the way ClickHouse's own `backQuote` quotes it: always quoted, every special byte escaped. A passthrough over the vendored function, and named after it; the name is a byte string.

#### `chs_back_quote_if_needed`

```c
CHS_API chs_status chs_back_quote_if_needed(const uint8_t *name, size_t name_len, chs_buf **out, chs_error **err);
```

- Class `api`, thread `any`, confirm.
- May return `CHS_OK`, `CHS_INVALID_ARGUMENT`, `CHS_INTERNAL`.
- `name`: bytes_in, `name`.
- `out`: out_handle, `chs_buf`, `sql`.
- `err`: out_error.

A name quoted only where this build's own `backQuoteIfNeed` says it must be. Which names stay bare is a property of the ClickHouse release, so a caller that needs one answer for several releases asks each library.

#### `chs_quote_string`

```c
CHS_API chs_status chs_quote_string(const uint8_t *text, size_t text_len, chs_buf **out, chs_error **err);
```

- Class `api`, thread `any`, confirm.
- May return `CHS_OK`, `CHS_INVALID_ARGUMENT`, `CHS_INTERNAL`.
- `text`: bytes_in, `bytes`.
- `out`: out_handle, `chs_buf`, `sql`.
- `err`: out_error.

A byte string spelled as a ClickHouse string literal by the vendored `quoteString`. The input may contain NUL; the answer escapes it.

#### `chs_schema_create`

```c
CHS_API chs_status chs_schema_create(const uint8_t *create_table, size_t create_table_len, const uint8_t *settings, size_t settings_len, chs_schema **out, chs_error **err);
```

- Class `api`, thread `any`, A6, A8.
- May return `CHS_OK`, `CHS_REJECTED`, `CHS_DECLINED`, `CHS_INVALID_ARGUMENT`, `CHS_INTERNAL`.
- `create_table`: bytes_in, `sql`.
- `settings`: bytes_in, `json_object_string_values`.
- `out`: out_handle, `chs_schema`.
- `err`: out_error.

Compiles one whole `CREATE TABLE` statement (columns, engine, keys, TTL, settings and constraints) with ClickHouse's own parser and the checks a server's CREATE runs, under a profile of settings given as a JSON object of string values. The result is immutable. The proposed form replaces v0's column-list compile and its engine, TTL and partition-key setters.

#### `chs_schema_free`

```c
CHS_API void chs_schema_free(chs_schema *schema);
```

- Class `api`, thread `handle_serial`, A8.
- `schema`: handle, `chs_schema`, nullable.

Releases the caller's reference to a schema. Filters and blocks made from it keep it alive. Freeing NULL does nothing.

#### `chs_schema_describe`

```c
CHS_API chs_status chs_schema_describe(const chs_schema *schema, chs_buf **out, chs_error **err);
```

- Class `api`, thread `handle_serial`, A8, A12.
- May return `CHS_OK`, `CHS_INVALID_ARGUMENT`, `CHS_INTERNAL`.
- `schema`: handle, `chs_schema`.
- `out`: out_handle, `chs_buf`, `document:schema_description`.
- `err`: out_error.

A JSON document describing the schema's columns, owned by the caller; it replaces v0's borrowed column accessors.

#### `chs_preview_row`

```c
CHS_API chs_status chs_preview_row(const chs_schema *schema, chs_format format, const uint8_t *body, size_t body_len, const uint8_t *settings, size_t settings_len, const uint8_t *columns, size_t columns_len, chs_buf **out, chs_error **err);
```

- Class `api`, thread `handle_serial`, A8, A10, A11, A12.
- May return `CHS_OK`, `CHS_REJECTED`, `CHS_DECLINED`, `CHS_INVALID_ARGUMENT`, `CHS_INTERNAL`.
- `schema`: handle, `chs_schema`.
- `format`: enum, `chs_format`.
- `body`: bytes_in, `bytes`.
- `settings`: bytes_in, `json_object_string_values`.
- `columns`: bytes_in, `json_array_names`.
- `out`: out_handle, `chs_buf`, `document:row`.
- `err`: out_error.

Validates and coerces one row of `body` under the schema, as a server's INSERT would, and returns the row's document. `columns` is the INSERT column list, empty for none.

#### `chs_preview_batch`

```c
CHS_API chs_status chs_preview_batch(const chs_schema *schema, chs_format format, const uint8_t *body, size_t body_len, const uint8_t *settings, size_t settings_len, const uint8_t *columns, size_t columns_len, const chs_filter *filter, int32_t export_format, uint32_t doc_flags, chs_buf **out, chs_buf **out_export, chs_error **err);
```

- Class `api`, thread `handle_serial`, A8, A9, A10, A11, A12.
- May return `CHS_OK`, `CHS_REJECTED`, `CHS_DECLINED`, `CHS_INVALID_ARGUMENT`, `CHS_INTERNAL`.
- `schema`: handle, `chs_schema`.
- `format`: enum, `chs_format`.
- `body`: bytes_in, `bytes`.
- `settings`: bytes_in, `json_object_string_values`.
- `columns`: bytes_in, `json_array_names`.
- `filter`: handle, `chs_filter`, nullable.
- `export_format`: scalar, `int32`.
- `doc_flags`: scalar, `uint32`.
- `out`: out_handle, `chs_buf`, `document:batch`.
- `out_export`: out_handle, `chs_buf`, `bytes`, nullable.
- `err`: out_error.

Validates and coerces a whole body, which may hold many rows, and returns the batch document. Row separation and the server's error allowance are ClickHouse's own. `filter`, when given, is evaluated over each stored row in the same parse. `export_format` is `CHS_EXPORT_NONE` or a `chs_format` the build can write; with an export, `out_export` receives the accepted rows serialized once, and it may be NULL when no export is asked for. `doc_flags` chooses which groups the per-row documents carry.

#### `chs_filter_create`

```c
CHS_API chs_status chs_filter_create(const chs_schema *schema, const uint8_t *expr, size_t expr_len, const uint8_t *query_params, size_t query_params_len, chs_filter **out, chs_error **err);
```

- Class `api`, thread `handle_serial`, A8, A9.
- May return `CHS_OK`, `CHS_REJECTED`, `CHS_DECLINED`, `CHS_INVALID_ARGUMENT`, `CHS_INTERNAL`.
- `schema`: handle, `chs_schema`.
- `expr`: bytes_in, `sql`.
- `query_params`: bytes_in, `json_object_string_values`.
- `out`: out_handle, `chs_filter`.
- `err`: out_error.

Compiles a boolean SQL expression over the schema's columns, binding its query parameters (a JSON object of string values) by ClickHouse's own substitution, so a value is never SQL text. Non-deterministic expressions are declined.

#### `chs_filter_free`

```c
CHS_API void chs_filter_free(chs_filter *filter);
```

- Class `api`, thread `handle_serial`, A9.
- `filter`: handle, `chs_filter`, nullable.

Releases the caller's reference to a filter. Freeing NULL does nothing.

#### `chs_filter_eval_body`

```c
CHS_API chs_status chs_filter_eval_body(const chs_filter *filter, chs_format format, const uint8_t *body, size_t body_len, const uint8_t *settings, size_t settings_len, chs_buf **out, chs_error **err);
```

- Class `api`, thread `handle_serial`, A9, A10.
- May return `CHS_OK`, `CHS_REJECTED`, `CHS_DECLINED`, `CHS_INVALID_ARGUMENT`, `CHS_INTERNAL`.
- `filter`: handle, `chs_filter`.
- `format`: enum, `chs_format`.
- `body`: bytes_in, `bytes`.
- `settings`: bytes_in, `json_object_string_values`.
- `out`: out_handle, `chs_buf`, `document:filter_result`.
- `err`: out_error.

Evaluates the filter over every row of a body, returning one verdict per row.

#### `chs_block_create`

```c
CHS_API chs_status chs_block_create(const chs_schema *schema, chs_format format, const uint8_t *body, size_t body_len, const uint8_t *settings, size_t settings_len, const uint8_t *columns, size_t columns_len, chs_block **out, chs_error **err);
```

- Class `api`, thread `handle_serial`, A8, A9, A10, A12.
- May return `CHS_OK`, `CHS_REJECTED`, `CHS_DECLINED`, `CHS_INVALID_ARGUMENT`, `CHS_INTERNAL`.
- `schema`: handle, `chs_schema`.
- `format`: enum, `chs_format`.
- `body`: bytes_in, `bytes`.
- `settings`: bytes_in, `json_object_string_values`.
- `columns`: bytes_in, `json_array_names`.
- `out`: out_handle, `chs_block`.
- `err`: out_error.

Parses a body once under the schema, for evaluating many filters over it.

#### `chs_block_free`

```c
CHS_API void chs_block_free(chs_block *block);
```

- Class `api`, thread `handle_serial`, A9.
- `block`: handle, `chs_block`, nullable.

Releases the caller's reference to a block. Freeing NULL does nothing.

#### `chs_filter_eval_block`

```c
CHS_API chs_status chs_filter_eval_block(const chs_filter *filter, const chs_block *block, chs_buf **out, chs_error **err);
```

- Class `api`, thread `handle_serial`, A9, A10.
- May return `CHS_OK`, `CHS_REJECTED`, `CHS_DECLINED`, `CHS_INVALID_ARGUMENT`, `CHS_INTERNAL`.
- `filter`: handle, `chs_filter`.
- `block`: handle, `chs_block`.
- `out`: out_handle, `chs_buf`, `document:filter_result`.
- `err`: out_error.

Evaluates the filter over a parsed block, with the same answers `chs_filter_eval_body` gives for the same body. The filter and the block must come from the same schema.

#### `chs_discover_query`

```c
CHS_API chs_status chs_discover_query(chs_buf **out, chs_error **err);
```

- Class `api`, thread `any`, A6, A11, Q-f.
- May return `CHS_OK`, `CHS_INVALID_ARGUMENT`, `CHS_INTERNAL`.
- `out`: out_handle, `chs_buf`, `sql`.
- `err`: out_error.

The query a caller runs against a server to read a table's `system.columns` rows, so that no binding holds SQL of its own.

#### `chs_discover_columns`

```c
CHS_API chs_status chs_discover_columns(const uint8_t *rows, size_t rows_len, chs_buf **out, chs_error **err);
```

- Class `api`, thread `any`, A6, A11, A12.
- May return `CHS_OK`, `CHS_REJECTED`, `CHS_DECLINED`, `CHS_INVALID_ARGUMENT`, `CHS_INTERNAL`.
- `rows`: bytes_in, `bytes`.
- `out`: out_handle, `chs_buf`, `document:discovery`.
- `err`: out_error.

Reads a server's `system.columns` rows (the JSONEachRow answer to `chs_discover_query`'s query) with ClickHouse's own reader and returns the column declarations, formatted by ClickHouse's own formatter.

<!-- END GENERATED: abi-v1 -->
