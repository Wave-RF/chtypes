# chtypes ABI v2 (UNSTABLE): the prose for the description

This file holds the prose for `spec/abi-v2/abi.json`, one `###` section per handle, enum, function and (optionally) constant or document. `scripts/abi-v1/gen.py --major 2` copies each section into the comment above its declaration in `include/v2/chtypes.h` and into the generated block of `docs/reference/abi-v2.md`, and `gen.py --major 2 --check` fails when a symbol has no section or a section names no symbol. The `## Rules` section below is copied into that block too.

Prose is deliberately outside the fingerprint: editing this file never changes `CHS_ABI_FINGERPRINT`, so it never invalidates a built library. The rule that follows is that anything a binding or the artifact producer must act on is structured in `abi.json` (a nullability, an ownership, a status a call may return, a thread class, a vocabulary), never only stated here.

The WHERE-affecting settings lists for each supported ClickHouse line are in `spec/abi-v2/where-settings/` (with a README); they are documentation, outside the fingerprint. The consumer rule (v2.0 and later) is in that README: refuse a tenant if `filter_declined_settings` is non-empty, or a `result-content` setting is changed.

The catalog of every reason the library declines or refuses a filter, per supported line, is in `spec/abi-v2/declines/` (with a README); it is documentation, outside the fingerprint. A consumer maps a decline it receives to its entry there; the verdict model is `filter_verdict` below.

Each section is copied into a C comment, so it may not contain a comment opener or closer, or two question marks in a row.

**UNSTABLE.** `spec/abi-v2/abi.json` declares `stability` `unstable`: generation 2 is being designed, and its fingerprint moves with every change to the description until the lock. It was seeded as generation 1's surface at generation 2; the additions land one pull request at a time. The first is the server profile: the `chs_server` handle, `chs_server_create` and `chs_server_free`, a server and an options document on `chs_schema_create`, and the `server` and `replicated` members of `schema_description`. The second is the batch document's `at_merge` list, with its `merge_reason` vocabulary, and a top-level `unsupported_settings` (public issue #544). The third is the settings a server profile sets that this build's filters do not honor in a WHERE: `schema_description`'s `server.filter_declined_settings`, with its `declined_tier` vocabulary, `chs_filter_create`'s decline on a schema whose server lists any, and the declined names from the other settings layers at evaluation. The fourth makes that one list over every layer known at schema compile (public issue #588): `schema_description`'s top-level `filter_declined_settings`, each entry with its `declined_layer` (`defaults`, `server` or `schema`), replaces `server.filter_declined_settings`, which is removed. **Changed by the fourth:** a declined setting from the `chs_set_defaults` or the schema layer, which the third named at evaluation, now declines `chs_filter_create`, at create time; a filter's own settings and an evaluation's stay evaluation-time. It also lists `CHS_REJECTED` (306, a deep input) in `chs_schema_describe`'s statuses. What generation 2 adds, and what the lock needs, are in public issue #511.

**Platform requirement.** A caller runs the library on the same operating system and CPU architecture as the ClickHouse server it models: every answer is guaranteed relative to a server on the same platform. On 26.7 and later, every platform agrees. On 26.3, ClickHouse's own parse of Float values from text input formats differs across platforms (macOS, Linux amd64, Linux arm64; mostly 1 ULP), so stored Float values, and comparisons involving them, can differ across architectures (`docs/limitations.md`).

## Rules

These rules bind generation 2 from its first draft: the library, every binding and the artifact producer. They are normative; `MUST`, `MUST NOT` and `SHOULD` are RFC 2119. `scripts/abi-v1/gen.py --major 2 --check` enforces the part of each rule that a description can be checked against, as each rule says. Rules (r5), (r6) and (r7) are binding behavior, which the dev bindings implement in their own pull requests.

**(r1) A growable function takes an options document.** A function that may gain inputs after it first ships MUST take them in one options document, a `bytes_in` JSON object, never as more positional parameters. The library MUST validate that document and MUST refuse a key it does not know with `CHS_INVALID_ARGUMENT`, naming the key; it never ignores one. So a caller that sets an option the library does not implement is told so, instead of silently getting the old behavior. A binding passes the caller's options through and never drops or rewrites a key. The description marks such a function `growable`, and lists its documents under `inputs`; an options document is the entry with `options` true, and a document of fixed purpose (a server profile) has `options` false. Checked by `gen.py`: a function marked `growable` takes exactly one options document (a `bytes_in` parameter whose content is `input:<name>`); a function not marked `growable` takes no input document at all; every input document is a JSON object whose schema closes every object, with `additionalProperties: false` on a record, or an `additionalProperties` schema and no `properties` on a map whose keys are data (setting or macro names); and every input document is taken by some function. The first growable functions are `chs_server_create` and `chs_schema_create`.

**(r2) Readers ignore unknown fields.** Every reader of a result document (each `document:<name>` output, and `chs_build_info()`) MUST ignore a member it does not know, at every object level, `_b64` members included, and decode the rest as if it were absent. Checked by `gen.py`: no document schema and no `build_info` schema in the description closes an object (`additionalProperties: false`). The released 1.x readers already behave this way: measured on the 1.0.4 bindings, all four on 12 CI legs, for every result document at every object level ([`v1-abi` run 37504999630](https://github.com/Wave-RF/chtypes/actions/runs/37504999630), the conformance legs of the unmerged probe, public pull request #509).

**(r3) Every enum has an `unknown(n)` member.** Every enum the description defines, an `int32` enum and a string vocabulary alike (`chs_status`, `chs_format`, `value_src`, `default_kind`, the three outcomes, `filter_verdict`, `transform_reason`, every one), MUST have in every binding an explicit member `unknown(n)` carrying the raw value `n`: the integer of an `int32` enum, the exact string of a vocabulary. A reader MUST map a value its description does not list to that member, for that field alone, and go on decoding. An unlisted value never fails the document, the row or the batch that carries it. Checked by `gen.py`: no described value is spelled `unknown`, `CHS_UNKNOWN` or `CHS_<name>_UNKNOWN`, the name readers reserve for that member.

- **A `fallback` changes meaning.** It no longer names the value an unlisted one is read as: the reader keeps `unknown(n)`. It names whose per-value facts `unknown(n)` reports (`lossy` for `transform_reason`, `answered` for `filter_verdict`) and how a question about the value is answered, so the fail-closed reading stays: an unknown outcome is never accepted, and an unknown verdict is never answered.
- **Facts without a fallback.** An enum whose values carry facts and which names no `fallback` MUST say what `unknown(n)` reports before the description is locked. Today that is `value_src`, whose `is_stored` has no answer for an unknown source.
- **A call status.** A status outside the closed set reads as `unknown(n)` too. The call still fails, as an internal error naming `n` (the `unknown` entry of `errors.status` in `spec/abi-v2/sdk.json`), because a status a binding cannot read is never a success.
- **Schemas constrain the writer.** An `enum` inside a document schema (`default_kind` in `schema_description`, `framing.container` in `batch`) constrains what the library writes, never what a reader accepts.
- **Why.** Measured on the released 1.0.4 bindings, all four on 12 CI legs ([`v1-abi` run 37506595472](https://github.com/Wave-RF/chtypes/actions/runs/37506595472), the conformance legs of public pull request #509): a reader survives an unlisted value only where its vocabulary has a fallback (an outcome reads as `unsupported`, a verdict as `d`, a transform reason stays raw and reads as lossy), and an unknown call status is an internal error. An unlisted `value_src` fails the WHOLE document with an internal error, so one row's unknown source loses the entire batch; an unlisted `default_kind` fails `schema_description` the same way.

**(r4) Error codes are stable once published.** An error code a caller can match on keeps its name and its number from the first release that carries it, a `2.0.0-dev.N` pre-release included, in every later release of every generation, and is never reused for another meaning. That covers the `chs_status` values, the `CHTYPES_*` codes (`errors.codes` in `sdk.json`, and the fetch layer's table in `docs/guides/fetch-v1.md` §8) and the exit status each code maps to. A new failure gets a new code. Checked by `gen.py`: every `chs_status` value generation 1 published keeps its name and number, and every `errors.codes` entry of `spec/abi-v1/sdk.json` is kept and none is reused under another key. The fetch layer's table is checked once a generation-2 fetch specification exists.

**(r5) The cache.** A generation-2 binding's cache MUST be one no released 1.x reader ever reads.

- **Record schema 2.** Its `verified.json` records are `schema: 2`. Released 1.x readers accept exactly `schema: 1` and treat any other record as absent (`docs/guides/fetch-v1.md` §1; the `cache-record-foreign-schema2` conformance cases hold every 1.x binding to it).
- **The default root.** It is `${XDG_CACHE_HOME:-~/.cache}/chtypes/v2-dev/` while the description is unstable, and `${XDG_CACHE_HOME:-~/.cache}/chtypes/v2/` after the lock. Generation 1's is `.../chtypes/v1/`, so the two never share a default root.
- **An explicit cache.** For ANY explicit cache directory, from the environment (`CHTYPES_CACHE`) or from the binding's own cache-directory option (Go `FetchOptions.CacheDir` and its counterparts in the other bindings), a generation-2 binding uses the subroot `<cache>/v2-dev/` (`<cache>/v2/` after the lock), never `<cache>` itself, which a 1.x binding uses as its whole layout. A caller that inspects or pre-populates a cache it named looks in the subroot. The subroot is a MUST, not a SHOULD. The API that reports where a lookup reads (Go `CacheRoot` and `SearchDirs`, and their counterparts) returns the subrooted path, and so does `chtypes where`. A 1.x reader that met a record it cannot read inside its own layout treats it as absent and then re-verifies that entry from the cache's blobs (`docs/guides/fetch-v1.md` §1). A build that only the staging key signed fails that check, as an error, not a miss (`inferred` from that guide, not measured). In its own subroot, a generation-2 build is never in a 1.x reader's path.
- **The result.** A released 1.x reader never picks a v2 build, in the default cache or a shared one, and no 1.x change is needed for that (`inferred` from the record rule and the layout above; the decision is recorded on public issue #508).

**(r6) The dev channel.** While the description is unstable, generation 2 ships only as pre-release SDKs, `2.0.0-dev.N`, which a package manager never installs by default: npm's `dev` dist-tag, PyPI `2.0.0.devN`, crates.io `2.0.0-dev.N`, and Go `go/v2.0.0-dev.N` under the module path `github.com/wave-rf/chtypes/go/v2`. Each one:

- **fetches** ONLY from `https://registry-staging.wavehouse.dev/chtypes/v2-dev`;
- **trusts** ONLY the staging key, the ed25519 public key `5cd30c53c65a1ebc2d85836a41deb06661bb0ae7b658adb9eb116ec2db8e9b1c`, key id `824345f9bcf8e5bf` (`sha256-first16hex`, the algorithm the release key's id uses in `docs/guides/fetch-v1.md`). The delivery side trusts that key only for the `chtypes/v<N>-dev` repositories and never in production, and the release key is not in a dev SDK's trust list;
- **has no override:** neither a base nor a trust list comes from the caller or the environment. `CHTYPES_ARTIFACTS_URL`, `CHTYPES_TRUSTED_KEYS`, `CHTYPES_ALLOW_UNSIGNED` and the API options that set a base or a trust list are not honored, and a dev SDK says so, once and loudly, when one is set;
- **refuses `--lock` and `--frozen`,** and their API equivalents, before any network call: a dev build is replaceable and a superseded one expires, so nothing may pin one;
- **pins its dev fingerprint** and refuses a library with any other, as `CHTYPES_ARTIFACT_INCOMPATIBLE`, with exactly this message, where X and Y are the two full `sha256:` fingerprints: `this SDK speaks dev fingerprint X; the library has Y — update your dev SDK`.

**The offline mode.** Every binding has an offline mode, and its meaning is the same in every generation and after the lock: a fetch reads the cache and the system directories only and makes no request. An installed, verified library is used as it is; with nothing installed the fetch fails as `CHTYPES_ARTIFACT_MISSING`, the code 1.x gives (r4), not as an unreachable source. The mode is the `offline` option (Go `FetchOptions.Offline` and its counterparts, and the command line's `--offline`) or the environment variable `CHTYPES_OFFLINE=1`, its twin: a binding reads it once per call, with the other fetch environment variables, and only the value `1` turns it on. Offline mode is on when the binding's option sets it or `CHTYPES_OFFLINE=1` is set; neither turns the other off. The mode names no source, so it cannot point an SDK anywhere and r6 holds.

**(r7) A closed handle is never passed as NULL.** Some handle parameters may be NULL, and NULL then means something of its own: `chs_schema_create`'s `server`, where NULL is the image's own server, and `chs_preview_batch`'s `filter`, where NULL is no filter. At such a parameter the library cannot tell a freed handle from a deliberate NULL, so the binding is the only place a closed handle can be caught.

- **Refuse, never substitute.** Where a function takes a handle parameter that may be NULL and whose NULL has a meaning of its own, a binding MUST refuse a handle the caller has CLOSED, with its own usage error, before any call. It MUST NEVER pass NULL in its place: a binding that frees a handle by setting its pointer to NULL would otherwise compile with no server and answer `CHS_OK`, a silent wrong answer.
- **A foreign handle is the library's to refuse.** A handle made by another library image is not the binding's to judge. The binding passes it through, and the library refuses it with `CHS_INVALID_ARGUMENT`. This rule covers a CLOSED handle only.
- **Unreachable by construction.** A binding whose type system makes a closed handle unreachable (Rust: a server is freed only on its last drop, and a compile borrows it) satisfies the rule by construction, and says so in its reference.

Checked by two shared conformance cases (`tests/fixtures/abi-v2/setup-cases.json`, generated by `scripts/abi-v1/emit/setup_cases.py`), which every binding that can express them runs through its public API over the stub library: `server.a_closed_server_is_never_passed_as_null` and `filter.a_closed_filter_is_never_passed_as_null`. Each compiles or previews with an open handle (the stub sees a non-NULL handle), with no handle (the stub sees NULL), and with a closed handle (the binding's usage error, where a NULL passed in its place would have succeeded). The server case reads the stub's live-handle counts; a batch call mints no handle, so the filter case runs on the stub variant `filter-observable`, which answers `rows_passed` 1 when a filter reached it and 0 when it received NULL. Rust declares both cases not expressible, with the reason, in the case itself. `gen.py` checks only that this rule is stated (`GENERATION_RULES`), since which parameters give NULL a meaning is prose, not structure.

At the lock (public issue #511 says when), the description's `stability` becomes `locked`. That moves the fingerprint one last time, so a released 2.0.0 SDK never accepts a dev build and a dev SDK never accepts a 2.0.0 library. SDK 2.0.0 then ships from the production `chtypes/v2` repository, signed with the release key.

## Preamble

This header is generated from `spec/abi-v2/abi.json` by `scripts/abi-v1/gen.py --major 2`. Edit the description and regenerate; never edit this file. `docs/reference/abi-v2.md` is the normative reference, and the rules generation 2 is written under (r1 to r7) are in it.

Stability. Generation 2 is UNSTABLE: its description is still being designed, so `CHS_ABI_FINGERPRINT` moves with every change until the description is locked. Nothing built from an unstable description is a release. Only pre-release SDKs (`2.0.0-dev.N`) speak it, each pinned to one fingerprint, and they fetch only from the staging dev channel. ABI v1 (`include/chtypes.h`) is frozen and unaffected.

Identity. `CHS_ABI_VERSION` is the generation and `CHS_ABI_FINGERPRINT` is sha256 over the canonical form of the description. A loader opens a library with `RTLD_NOW | RTLD_LOCAL`, calls only the handshake set (`chs_abi_version`, `chs_build_info`, `chs_clickhouse_version`) before its checks pass, and refuses any library whose generation or fingerprint differs from the ones it was compiled with. Nothing degrades: every described symbol is mandatory.

Ownership. Every input string, statement, document and body is counted bytes, `(const uint8_t *p, size_t n)`: length 0 is none and the pointer may then be NULL, and a NULL pointer with a nonzero length is `CHS_INVALID_ARGUMENT`. Every output is an owned, opaque `chs_buf`, read with `chs_buf_data` and `chs_buf_len` and released with `chs_buf_free`. Bytes pass through unmodified, NUL and invalid UTF-8 included: a column name may contain either (measured on live servers), so no name, statement or message is ever a C string. The only static strings are the handshake's. Handles are opaque, reference-counted and tagged with their kind and the image that made them; every entry point checks both, a child holds a strong reference to what it needs, and each free drops only the caller's reference, so any free order is safe and freeing NULL does nothing.

Errors. A fallible call returns a `chs_status` and, through an optional last `chs_error **err`, an owned error carrying the status, ClickHouse's own code and name, the message and the column. The status alone carries the verdict, so `err` may be NULL.

Threads. Every function has a thread class, listed in the reference. A handle is immutable once made, so every call that only reads handles is `shared`: any number of threads may call it at once, on the same handles too. The one thing a caller must never overlap with a call is a free of a handle that call uses. Process setup (`chs_initialize`, then `chs_set_defaults`) runs before the traffic it configures.

Time zones. A library image has one zone of its own, set once by `chs_initialize`: the image zone. A schema has a home zone, its server's `timezone` (`chs_server_create`), or else the image zone. The schema's types bind its home zone, as a server's table binds the server's zone after a restart, and so do the functions its DEFAULT, MATERIALIZED, ALIAS and CHECK expressions apply to its columns, its PARTITION BY expressions, and a Date-valued TTL under a merge. Each call runs in a zone too, the first of these that is set: the call's own `session_timezone` (a key in its settings, which ClickHouse applies through its own query context); the `session_timezone` in the schema's own settings; the one in its server's `settings`; the one set by `chs_set_defaults`; its server's `timezone`; the image zone. An empty `session_timezone` at any of these means the server's zone, the last two. The call's zone governs parsing and rendering a zone-less `DateTime` or `DateTime64`, export, a DEFAULT literal's fold, `toDateTimeOrZero` and a filter's literals. A `Date` partition id is the civil date in every zone. Every zone name is validated by ClickHouse's own `DateLUT`, which reads the library host's time zone database, on every platform. On darwin the file system is case-insensitive, so `DateLUT` there accepts a spelling such as `utc` that a Linux server refuses; that difference is documented, not patched.

Settings. Every settings input is a JSON object whose values are JSON strings, and a binding never rewrites one. A call's settings are layered, highest first: the call's own `settings`; the schema's own (`chs_schema_create`); its server's `settings` (`chs_server_create`); the defaults set by `chs_set_defaults`; the build's own. A filter's WHERE takes the same layers, lowest first: the build's own, `chs_set_defaults`, the server profile, the schema's own, and the filter's own (`chs_filter_create`), with `session_timezone` at every layer staying on the zone path (Time zones, above). One exception: a type gate in the schema's own settings binds the CREATE only, and a WHERE's CAST validates under the session layers, every layer but the schema's own. Each setting that reaches a WHERE is honored (applied to it), passed through (accepted and ignored), or declined (`chs_filter_create` says how). A server resource limit (an `execution`-tier setting, e.g. a small `max_columns_to_read`) can make the server refuse a query the library answers; that is resource policy, not row access, and the library does not model it. A server's settings are the ones its profile applies to every query, and they never change how a schema compiles. No call reads a server's version or its changed settings itself: that discovery is a recorded gap in this generation, so a caller that wants a server's settings passes them explicitly, in a server profile, through `chs_set_defaults` or in each call's settings.

Documents. Every output document is valid JSON that a stock parser reads: every ClickHouse rendering (a stored value, an input value, an engine's row) is a JSON string, a number written bare is within plus or minus 2^53 (anything larger is a string), and there is never a bare `inf`, `-inf` or `nan`. Every column entry of a row carries `null`, the library's verdict that the stored value is NULL, poisoned cells included.

Byte strings in JSON. Every data-derived string a JSON document carries is a byte string: a column name, a value's rendering, a type (an Enum label or a named Tuple element comes from the caller's DDL), SQL text, and a message (ClickHouse's messages quote input bytes). One rule carries them all, names included, in every output document and in the input column list. The member `F` is a JSON string when the bytes are valid UTF-8, a NUL written as the escape `\u0000` as RFC 8259 allows. Otherwise the member `F_b64` holds the raw bytes in standard base64, padded. The two are never both present, and a member that names an entry (a column name) is always present in one of its two forms. So every document is valid UTF-8 JSON that a strict parser reads, and no byte is lost or replaced. Where a list or a map would hold a bare data-derived string, it holds an object instead, so the rule applies to that object's members: a list of names is a list of `{name}` or `{name_b64}` objects, and an engine's row is a list of cells. Each document lists its data-derived fields (`byte_fields` in the reference); a binding decodes each one to bytes and never assumes UTF-8. A server accepts every kind of name (measured on every supported line). The byte-returning accessors (`chs_error_message`, `chs_error_column`, `chs_error_ch_name`) and the export bytes are raw buffers, not JSON, and are not affected.

String values. Every entry that reports a stored value (a row's `cols`, `computed`, an engine row's cells, `storage_transforms`) carries `stored` or `stored_b64`, which is always ClickHouse's rendering of the value. For a scalar `String` or `FixedString` value, including one inside `Nullable` or `LowCardinality`, the entry also carries `value_b64`, the value's raw bytes in standard base64, whenever the value is not NULL, whether or not the bytes are valid UTF-8. A value nested in another type (`Array(String)`, `Map`, `Tuple`, and the like) carries no `value_b64`: that is a recorded gap in this generation, and its rendering is the only form.

Input values. A JSON input (`settings`, `query_params`, an input document) cannot carry a value that is not valid UTF-8 in this generation. The workaround is to inline the value in the SQL as `unhex('<hex>')`, built from hex digits only, which keeps it injection-safe because hex digits cannot break out of the quoted literal. A later version would add an object form, `{"<key>": {"b64": ...}}`, never a `<key>_b64` member, which could collide with a real key.

Input documents. A counted input whose content is `input:<name>` is a JSON object that the library validates against the document of that name (rule r1). A key the document does not define is `CHS_INVALID_ARGUMENT`, naming the key, and so is a document that is not a JSON object or a member of the wrong JSON type. Length 0 is `{}`, and a repeated member keeps its last value. The library never ignores a key, so a caller that sets an option a build does not implement is told so instead of silently getting the old behavior.

Teardown. `chs_shutdown` stops what `chs_initialize` started. A loader never unloads a library: unloading or initializing an image again in one process is unsupported.

### chs_buf

An owned, immutable byte buffer: every output of the library. Read it with `chs_buf_data` and `chs_buf_len`; the bytes are exactly what the library produced, with no terminator and no encoding promise beyond what the producing parameter's content says.

### chs_error

The error a fallible call reports when its status is not `CHS_OK`: the status, ClickHouse's own error code and name, a message and the column concerned. Created only by the library, through a call's `err` out-parameter.

### chs_server

One ClickHouse server, as a profile describes it: its `timezone`, the settings its profile applies to every query, and its `<macros>`. Immutable once created, so any number of threads may use it at once. A schema created on it holds a counted reference to it, so the caller may free the server whenever it likes.

### chs_schema

A compiled table: the columns, engine, keys, TTL, settings and constraints of exactly one `CREATE TABLE` statement, on the server it was created on, if any. Immutable once created, so any number of threads may use it at once.

### chs_filter

A compiled boolean SQL expression over a schema's columns, with its query parameters bound. It holds a counted reference to its schema, so the schema stays alive for as long as the filter does, whatever order the caller frees them in.

### chs_block

A body parsed once under a schema, for evaluating many filters without parsing again. Like a filter, it holds a counted reference to its schema.

### chs_status

The verdict of every fallible call, in five values whose numbering is frozen.

- `CHS_OK`: the call succeeded and its outputs are set.
- `CHS_REJECTED`: ClickHouse's own refusal, with its own code, name and message, which a server would also give. A deep input the server's stack check refuses (an expression, a statement, a body, a document, a settings or profile object nested deeper than the calling thread's stack allows) is `CHS_REJECTED` with ch_code 306, TOO_DEEP_RECURSION, at every entry point that takes one, never `CHS_INTERNAL`.
- `CHS_DECLINED`: this build will not answer; a server might accept. Never scored as agreement.
- `CHS_INVALID_ARGUMENT`: caller misuse, such as a NULL pointer with a nonzero length, a wrong-kind, freed or cross-library handle, or a NULL required out-parameter.
- `CHS_INTERNAL`: a guarded exception inside the library, which is a library bug.

The set is closed within this generation: any new condition maps onto one of these five. A binding maps each status to one error class (`spec/abi-v2/sdk.json`), and treats a value outside the set as an internal error naming it, whose status is that value's `unknown(n)` (rule r3).

### chs_format

The input and export formats, numbered as they have been since the first release; the numbers are frozen and a binding passes the integer. `ch_name` is ClickHouse's own name for the format, which is how `chs_build_info` lists the formats a build supports. Being in this enum never proves a build supports a format: the build says so in its `capabilities`.

### transform_reason

The reason a stored value differs from the supplied one, as the library reports it per column. `lossy` says whether information was lost; exactly four reasons are lossless (a representation change, a value filled from a DEFAULT or the type's zero, and a DEFAULT the library resolved from its own clock), and they are still reported because a preview must show what the table will hold. A binding reads `lossy` from here and never keeps a list of its own.

### merge_reason

What a merge would do to one row of the part an `INSERT` writes, as the batch document's `at_merge` reports it: `ttl_delete`, the table's rows TTL removes the row; `ttl_column_reset`, a column's TTL resets the column's value. A reason added later is a new value, which a reader reads as its `unknown(n)` (rule r3).

### default_kind

A column's default kind, as ClickHouse names it: `DEFAULT`, `MATERIALIZED`, `ALIAS` or `EPHEMERAL`, or the empty string for a column with none. The schema description carries one per column.

### declined_tier

The tier of a setting a settings layer sets and this build's filters do not honor in a WHERE, as `schema_description`'s `filter_declined_settings` reports it, spelled as this build line's WHERE-settings list (`spec/abi-v2/where-settings/<line>.json`) spells it: `predicate` (measured to change a verdict), `predicate-unflipped` (read in the WHERE's scope, though no value the flip test tried changed a verdict) or `result-content` (selects rows by content). The lists' other tiers (`execution`, `result-truncate` and `output`) never appear here: those settings are passed through and ignored. A tier added later is a new value, which a reader reads as its `unknown(n)` (rule r3).

### declined_layer

The settings layer a `schema_description` `filter_declined_settings` entry comes from, the highest one that sets the name: `defaults` (the defaults `chs_set_defaults` set, as `chs_schema_create` captured them), `server` (the server profile's `settings`, `chs_server_create`) or `schema` (`chs_schema_create`'s own `settings`). The values are in layer order, lowest first, which is the list's order. A layer added later is a new value, which a reader reads as its `unknown(n)` (rule r3).

### discover_query_param

The query parameters in the SQL `chs_discover_query` returns, each with its ClickHouse type. The caller binds them when it runs the query (over HTTP, as `param_database` and `param_table`), so no binding builds SQL.

### value_src

Where a column's value came from. `is_stored` says whether the row as stored carries a value for the column: a column the input named but the table never stores (an EPHEMERAL column, a skipped MATERIALIZED or ALIAS column) and a DEFAULT the library could not resolve carry none.

`default_generated` marks a DEFAULT column whose expression calls a random or ID generator the build admits; a build that does this lists `default_generators` in `chs_build_info`'s `capabilities.features`. The label means: generated by chtypes, and it becomes the stored value when you insert this output. The library drew the value itself, with ClickHouse's own vendored function. So insert the library's output (the export of `chs_preview_batch`, or the stored values its documents report), not your original input: a server evaluates a DEFAULT only for a column the INSERT does not supply, and the original input, which omits the column, would make the server draw a different value. Generators are admitted only in DEFAULT expressions: a MATERIALIZED column is always computed by the server, and an EPHEMERAL column is not stored. No binding logic is involved; a binding reports the source as it reads it.

### row_outcome

The verdict on one row: accepted, accepted but unreadable afterwards (the insert succeeds and every later read fails), rejected, skipped under the server's error allowance with the batch continuing, or declined by this build.

### batch_outcome

The verdict on a whole body. A batch is never skipped; its rows carry their own outcomes.

The batch answers the first of these that applies:

1. **A call-level rejection is `rejected`.** The call's own settings are refused (a value the settings constraints or the setter refuse), or the INSERT column list's verdict, a CREATE-time type gate or a contract violation refuses the call. The server refuses these before it reads a byte of the body, so no row changes the answer.
2. **A call-level decline is `unsupported`,** with the setting named in the batch document's top-level `unsupported_settings`. This holds whatever the rows say, refusing rows and an empty body included: every row was read in a context the call did not ask for, so neither an acceptance nor a refusal there is the server's answer (a DateTime past the type's range in the schema's own zone can be in range in the call's zone). Every row reads `unsupported`, filter rows read `d`, the row preview answers the same, and no export bytes are produced.
3. **Otherwise the first row, in body order, that is neither accepted nor skipped** gives its outcome (`rejected` or `unsupported`) and its code. An `unsupported` row ends the read, so a refused row after it is not reached and the batch stays `unsupported`. After the rows, the whole-body steps run in the server's order (the Object and Dynamic block steps, the DEFAULT step over the reader's chunks, the partition split, the writer's index expressions, the engine merge, the TTL, the projections), only on a batch the rows left accepted, and each may still refuse or decline it.
4. **`accepted_poisoned`:** a stored value the server itself cannot read back. The INSERT succeeds, but nothing is exported.
5. **`accepted`,** the only verdict that exports bytes.

A skipped row never changes the verdict. Steps 2 and 3 depart from "rejected if any row is refused" for one reason: never a refusal that is not definite. Both answer `unsupported`, which a caller never scores as agreement.

A filter's verdicts follow the batch. When the batch's outcome is not `accepted`, by any of steps 1 to 4, every row's filter verdict is `d`. A row the parse accepted carries `verdict_code` -2 and `verdict_err` `batch outcome is '<outcome>': only a fully accepted batch has filter verdicts`; a refused row carries its own code and message. Nothing from such a batch is stored for a WHERE to read, exported or counted, so no verdict of it is an answer.

### filter_outcome

Whether a filter evaluation answers its body. `ok`: every row carries its own verdict, and per-row failures are verdicts, not outcomes. Any non-`ok` outcome, including `unsupported`, means: treat every verdict as `d`. A caller MUST do so whatever the verdict string holds.

`chs_filter_eval_body` and `chs_filter_eval_block` answer `ok` only for a body a server would accept, that is, when `chs_preview_batch`'s verdict over the same body and settings, with no filter, is `accepted`. Otherwise the outcome mirrors that verdict: `rejected` with its code and message; `unsupported` with its code and message, a call-level decline included; and `unsupported` with code -2 for `accepted_poisoned`, which this vocabulary does not hold and which is no refusal of the INSERT. A body the parse itself refuses answers `rejected` or `unsupported` with no verdicts.

### filter_verdict

One character per row of a filter evaluation. `t` and `f` are answers (true; false or NULL); `e` (the predicate raised an error on this row) and `d` (this build declines the row) are not, and a caller enforcing visibility must fail closed on both. A verdict is an answer only for a body a server would accept: in a batch whose outcome is not `accepted` every verdict is `d`, and in a filter document whose outcome is not `ok` (any non-`ok` outcome, including `unsupported`) a caller MUST treat every verdict as `d` (`batch_outcome`, `filter_outcome`).

**What a filter verdict is.** A filter verdict answers one question per row: would a ClickHouse server of the pinned version, on the same OS and architecture, return this row from `SELECT … FROM <table> WHERE <filter>`, if the table held exactly the row this INSERT would store? It is the server's `WHERE` over that one row, as the table stores it and the server reads it back.

- **`t`:** that query returns the row. Truth follows the server's own `WHERE` rule: a non-zero number keeps the row, and NULL never does. A `t` is a security claim. A `t` where the server would not return the row is an over-accept, a security-severity defect.
- **`f`:** the predicate is false or NULL, so the query does not return the row.
- **`e`:** the predicate threw on this row's values. The verdict carries the server's own code and message. A server fails the whole query in that case, because a `SELECT` has no per-row error channel.
- **`d`:** the library declines to answer for this row because it cannot be sure the server would answer the same. Examples:
  - the row cannot be stored;
  - the filter reads a value the server computes for itself, such as a clock read or a generator;
  - the call carries a setting the library cannot honor;
  - the filter has a shape on the decline list.

A caller that authorizes rows with filters treats anything but a definite `t` as no: `f`, `e`, `d` and `unknown(n)` all mean no.

**What the model leaves out, by design:**

- **Other rows.** A verdict does not depend on which other rows the table holds. If the predicate throws on a different row of the same part, a real query fails as a whole, but this row's verdict is unchanged: each row carries its own `t`, `f` or `e`.
- **What storage does later.** Engine merges, TTL expiry, mutations and deletes change what the table holds over time. The verdict is the `WHERE` over the record as stored by this INSERT.
- **Other query shapes.** A planning refusal that only another query shape reaches is not a row verdict; an example on some versions is an aggregate `SELECT count() … WHERE`. Where the row shape itself reaches such a refusal, the library declines the filter (`d`).
- **The server's platform.** The library answers for a server on its own OS and architecture. On 26.3, ClickHouse's own parse of Float values from text input formats differs across macOS, Linux amd64 and Linux arm64 (mostly one ULP), so stored Float values, and comparisons involving them, can differ between servers of different architectures. Run the library on the server's platform; on 26.7 and later, every platform agrees.

**A body the INSERT does not accept has no filter verdicts** (`batch_outcome`, `filter_outcome`). Any outcome other than `accepted` (a batch) or `ok` (a filter document) means every verdict is `d`.

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

A JSON object with one key per handle kind (its type name, such as `chs_schema`) and the number of live handles of that kind in this image, counted before the document's own buffer exists.

### document:error_code_table

ClickHouse's own error-code table for this build: a JSON array of `{"code": int, "name": string}`, one entry per code the vendored table names, in ascending code order. A passthrough over the vendored table, never a copy.

### document:schema_description

A schema's columns, in declared order, with each column's canonical type, its `default_kind` (a value of the `default_kind` vocabulary) and default expression, and the facts a caller needs to build a row. The document is `{"columns": [...]}`, and each entry carries `name`, `type`, `default_kind` (a value of the `default_kind` vocabulary), `default_expression` (empty when there is none) and `default_is_literal`; the name, the type and the expression follow the rule for byte strings in JSON.

Two members report the server a schema was created on. Both are absent on a schema created with `server` NULL, so that document is byte for byte what it was before servers existed.

- `server`, on every schema created on a server, even one described by `{}`: `timezone` is the zone the schema's types bind (the profile's, or else the image zone, exactly as `chs_initialize` spelled it), so a reader never needs the image zone separately; `settings` is the server's settings as the profile gave them, `{}` when it gave none; `macros` is the server's macro set as the library holds it, present exactly when the profile carried `macros`. These are the caller's own JSON strings given back, not data-derived, so they are plain strings. The member no longer carries `filter_declined_settings`: that list is the top-level one, below, over every layer.
- `replicated`, on a schema created on a server whose engine is a Replicated one: `zookeeper_path` and `replica_name`, the path and the replica name ClickHouse's own `TableZnodeInfo` resolved, fully expanded. The database and table names expanded into them are DDL bytes, so both follow the rule for byte strings in JSON. A caller can compare them with the server's own `system.replicas`.

`filter_declined_settings` is required on every schema description, with or without a server: an array, `[]` when nothing is declined. It lists the settings the schema compiles under that this build's filters do not honor in a WHERE, over every layer known when `chs_schema_create` ran, and `chs_schema_create` computes it. The layers, lowest first, are the `declined_layer` values:

- `defaults`: the defaults `chs_set_defaults` set, as `chs_schema_create` captured them. This is the snapshot the schema compiles under, never a later call (which the latch refuses anyway once a schema exists).
- `server`: the server profile's `settings` (`chs_server_create`), when the schema was created on a server.
- `schema`: `chs_schema_create`'s own `settings`.

Each entry is `{name, tier, layer}`: `name` follows the rule for byte strings in JSON (`name` or `name_b64`), `tier` is a `declined_tier` value, the tier this build line's WHERE-settings list gives the name, and `layer` is a `declined_layer` value. The entries are:

- every name set at any of the three layers that the line's list carries at tier `predicate`, `predicate-unflipped` or `result-content` and that is not in this build's honor set (the settings it applies to a WHERE, each with a measured case where the server's verdict moves and the library's tracks it);
- `compatibility`, at tier `predicate`, when its value at the highest layer that sets it moves a listed setting outside the honor set.

A name the list does not carry is never an entry, and neither is a setting of the `execution`, `result-truncate` or `output` tiers: those are passed through and ignored. The test is presence, not value: any value at any layer counts, a declined name set to its default value included, because some of ClickHouse's reads are `isChanged()`, which asks only whether a setting was set. A higher layer that sets the same name never takes the entry away; it only moves the entry's `layer`.

The order is deterministic. There is one entry per name, at the highest layer that sets it (`defaults`, then `server`, then `schema`). The entries are ordered by layer, `defaults` first, and within a layer by name as raw bytes, ascending; a `name_b64` entry sorts by its decoded bytes.

The list holds only the layers known at schema compile. A filter's own `settings` (`chs_filter_create`) and the settings an evaluation brings (the call's, and the body's) are never in it: a declined one is still named at evaluation (`filter_result`'s `unsupported_settings`, every verdict `d`). A schema whose list is not empty is one `chs_filter_create` declines.

### document:row

One row's verdict and, as the flags ask, its columns' stored values, provenance and transformations. Every entry of `cols` carries its name, `null` (whether the stored value is NULL, poisoned cells included), its renderings (`input`, `stored`, `ref`, `wire`) and its types (`type`, `base`, `ref_type`), all by the rule for byte strings in JSON, and `value_b64` for a String or FixedString value. `unknown_fields` and `unsupported_settings` are lists of name objects; `computed` entries carry a name, a kind and a stored value; `err`, `verdict_err` and `partition_id` follow the rule too. The transformations come from the library, never from a binding: each is the `transformed` entry the SDK's parity fixtures pin, with its `reason` from `transform_reason`. `input_span` is `{off, len}`, the bytes of the input body the reader consumed for this record, read from the vendored reader's own position.

### document:batch

A body's verdict, counts and per-row documents (each a row document, with its own `input_span`), and, when an export was asked for, where each accepted row sits in the export bytes. `engine_rows`, present when the table's engine merges rows at insert, is a list of rows, each a list of cells: `{name, stored, null}`, plus `value_b64` for a String or FixedString value. `storage_transforms` entries carry `row`, `column`, `reason` and `stored`, plus `value_b64`; `transformed` entries carry `row`, `column`, `input`, `stored`, `reason` and `lossy`. Every name, rendering, `err` and `export_declined` follows the rule for byte strings in JSON.

`unsupported_settings`, at the top level, is a list of name objects with the same shape as a row document's: the call's own settings that the library declined. It is empty when none was, and when it is not empty the batch's outcome is at least `unsupported` (see `batch_outcome`).

`at_merge` lists what an `OPTIMIZE TABLE ... FINAL` of the part this `INSERT` writes would do, at the call's clock instant. It is absent when nothing would happen. `engine_rows` stays exactly what the `INSERT` writer produces, so the two never contradict each other. Each entry is `{row, reason, column, stored, input_rows}`:

- `row` indexes `engine_rows`: the part's row, not the input body's. An engine's insert-time merge (Collapsing, Replacing, Summing, Aggregating) runs first, inside the `INSERT` writer, so `engine_rows` already excludes the rows it merged away, and after a Summing or Aggregating merge the part's rows do not map one to one to the input's.
- `reason` is a `merge_reason` value: `ttl_delete` (the rows TTL removes the row) or `ttl_column_reset` (a column TTL resets the column's value). A row that is removed and also carries column resets lists only `ttl_delete`: it is removed in the same merge pass, so its column values are never observable.
- `column` names the column, by the rule for byte strings in JSON, and is present only for `ttl_column_reset`.
- `stored` is the column's value after the reset, by the same rule. It is present when that value is decided at the `INSERT`: the column's `DEFAULT` is a constant or a deterministic expression of the row, or the column has no `DEFAULT` (the type's default). It is absent when the `DEFAULT` reads the clock (`now()`) or a generator (`rand()`, `generateUUIDv4()`), and the entry then means that the value is reset at the merge and decided then.
- `input_rows`, when present, lists the input rows, each by its index in the body, that formed the part's row.

The list is "at least these", not "only these": a later background merge can remove more rows as more of them expire. Under `ttl_only_drop_parts = 1`, `ttl_delete` is still reported per row against `OPTIMIZE TABLE ... FINAL`, which drops an expired row from a part that also holds rows that have not expired (measured by the artifact producer). Whether a background merge does the same under that setting is unverified, and the reference stays `OPTIMIZE TABLE ... FINAL`.

Two further fields come from the vendored reader's own state, never from a tokenizer of the library's:

- `unconsumed`: the byte ranges `{off, len}` of the input that the reader's error recovery skipped. It does not account for every record: a **skipped** row's `input_span` can cover more than one input record, when recovery resumed past the end of the record that failed, so the verdicts can be fewer than the body's records while `unconsumed` is empty (measured: one skipped row spanning a three-record body). A record can also be lost while the number of verdicts still equals the number of records, so comparing the verdict count with an independent count of the body's records can be fooled (measured in every format, for example with a multi-line quoted CSV field or a JSON object). In every masked body measured, `unconsumed` was non-empty. Until a batch-level signal from the reader's own framing exists (#478), a caller that needs every record accounted for declines a body that has any skipped row or any `unconsumed` range.
- `framing`: `bom_skipped` (whether the reader skipped a leading byte-order mark), `container` (`array` or `stream` for JSONEachRow, `null` for every other format), and `header` (`{consumed, lines, names}`, `names` as name objects). `bom_skipped` and `header` are `null` where the vendored reader does not expose the decision, and there `null` means not observable, never "no header": the TSV, TSVWithNames and Values readers keep both private (measured by the artifact producer in the source at every supported line). CSV, JSONEachRow and JSONCompactEachRow fill both from the reader's own hooks.

### document:filter_result

A filter evaluation: the call's outcome, one verdict character per row (`filter_verdict`), and each error or declined row itemized as `{row, code, err}`. `err`, at the top level and per row, follows the rule for byte strings in JSON, and `unsupported_settings` is a list of name objects.

`unsupported_settings` (after `rows_read`, before `verdicts`) names the settings the parse itself declined, and then, after them and without repeats, every setting of the filter's own `settings` that this build's filters do not honor in a WHERE. When it names one of those, every verdict is `d` and every row is itemized in `errors` as `{row, code, err}` with `code` -2. `outcome` follows its own rules all the same: `ok` for a body a server would accept, and the mirror of `chs_preview_batch`'s verdict otherwise (`filter_outcome`). A declined setting from the `chs_set_defaults`, server or schema layer never reaches an evaluation, because `chs_filter_create` declines first (`schema_description`'s `filter_declined_settings`).

### document:discovery

The column declarations reconstructed from a server's `system.columns` rows, formatted by ClickHouse's own formatter: `{"columns": [...], "columns_sql"}`, where each entry carries `name`, `type`, `default_kind` and `default_expression` as the server spelled them and `declaration`, the formatted declaration, and `columns_sql` joins the declarations for a `CREATE TABLE`. Every one of these is server or DDL text, so each follows the rule for byte strings in JSON.

### input:server_profile

A server's profile, `{"timezone": "<zone>", "settings": {"<name>": "<value>"}, "macros": {"<name>": "<value>"}}`. Every member is optional, so `{}` describes a server with nothing known about it. The profile holds server facts only, in the shape a server's own tables report them, so a caller can fill it from the server it describes; library knobs go in `chs_server_create`'s options instead.

- `timezone`: the server's own zone, the zone its tables bind. An empty string, or no member, means the image zone. The name is validated by ClickHouse's own `DateLUT`, which reads the library host's time zone database.
- `settings`: the settings the server's profile applies to every query, each value a JSON string, never rewritten. The library accepts exactly what the server's own SET check accepts. `session_timezone` here is the default session zone of the server's user.
- `macros`: the server's macros, as its `system.macros` lists them (`macro`, `substitution`). Absent, they are unknown; present, even as `{}`, they are the complete set (see `chs_server_create`). A macro name that ClickHouse's own macro reader refuses (for example `a.b`) is refused with `CHS_INVALID_ARGUMENT` naming it.

### input:server_options

The options of `chs_server_create` (rule r1). It defines no member yet, so `{}` (or length 0) is the only valid document, and any key is `CHS_INVALID_ARGUMENT`, naming the key. A library knob that is not a server fact lands here, as a new member.

### input:schema_options

The options of `chs_schema_create` (rule r1). It defines no member yet, so `{}` (or length 0) is the only valid document, and any key is `CHS_INVALID_ARGUMENT`, naming the key. A choice about how one statement is compiled lands here, as a new member: for example, compiling a server's own `create_table_query`, read back from the server, rather than a caller's CREATE.

### chs_abi_version

The ABI generation this library implements: always 2 for this header. A loader resolves and calls it right after opening the library. A library without the symbol is not a chtypes ABI artifact of generation 1 or later, and any other value is refused, naming both values. An absent handshake symbol other than this one is refused as `missing_symbol:<name>` instead, at whichever step first resolves it — only this symbol's absence means the library is not such an artifact at all.

### chs_build_info

What this library is, as static, NUL-terminated, ASCII-only JSON in the image's read-only data: never NULL, never freed, the same bytes on every call, and callable straight after opening the library. Its fields are listed under `build_info` in the reference, and `abi_fingerprint` is `CHS_ABI_FINGERPRINT` as the library was built, copied, never recomputed.

`capabilities` is read from the build itself, never from a version number: `input_formats` and `export_formats` by ClickHouse's own format names from the build's FormatFactory, `doc_flags` as the document groups it honors, and `features`, an open list of build-level features, where a new feature is a new value and never a new field. `default_generators` in `features` means the library fills admitted generator DEFAULTs and the caller inserts the library's output (`default_generated` under `value_src`).

A loader parses it (ASCII only, duplicate keys refused, `schema` equal to 1, every required field of its type), refuses when `abi_fingerprint` differs from its own compiled-in `CHS_ABI_FINGERPRINT` byte for byte, and then compares the fields listed in `spec/abi-v2/sdk.json` (`cross_check`) with the verified signed statement it fetched the library under, refusing on the first mismatch and naming the field. That comparison is mandatory. The file's own size, digest and glibc floor are not in it, because the file cannot carry facts measured on itself; they are in the signed statement.

### chs_clickhouse_version

The ClickHouse release this library was built from, spelled as every v0 library spelled it (for example `26.8.15.10-lts`): static, never freed. It keeps its v0 signature and spelling so a v0 binding reaches its own refusal. A binding of generation 1 or later reads `clickhouse_version` from `chs_build_info` instead.

### chs_abi_revision

The v0 tombstone. It always returns `CHS_ABI_REVISION_TOMBSTONE` (1001). It must never be removed, and its name must never be given another meaning.

A sweep of every released binding (every minor from 0.1 to 0.5, in all four languages; [the CI run](https://github.com/Wave-RF/chtypes/actions/runs/36939764841)) measured why: each one calls this symbol whenever it exists and refuses cleanly, naming both numbers, when it returns a revision other than its own, but treats a MISSING symbol as "revision 0, predates the probe" and goes on to call `chs_init`. So this tombstone is what turns every v0 binding away before it can reach any other symbol, and `gen.py --check` refuses a description that reuses a v0 name without it. A binding of generation 1 or later never calls it; the loader resolves it only to prove it is present.

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

A new buffer holding the name this build's vendored error table gives the code, the name a server prints after the code; empty when the build has none or the status is not `CHS_REJECTED`. Like the other two text accessors, it returns NULL only for a NULL or invalid error.

### chs_error_message

A new buffer holding the message: ClickHouse's own text, verbatim, for `CHS_REJECTED`, and the library's explanation otherwise. A message may quote input bytes, so it is a byte string, not text.

### chs_error_column

A new buffer holding the name of the column the error concerns, as raw bytes, empty when there is none. A column name is a byte string: NUL and invalid UTF-8 are legal in a name.

### chs_error_free

Releases the caller's reference to an error. Freeing NULL does nothing.

### chs_live_handles

A JSON document counting the live handles of each kind this image has made, taken before the document's own buffer exists. A test abandons handles, lets its runtime collect them and then asserts every count is zero, which is how all four bindings prove their finalizers release everything.

### chs_error_codes

ClickHouse's own error-code table for this build, as a JSON array of `{"code", "name"}` in ascending code order: a passthrough over the vendored table, never a copy. The table belongs to the build, and one number can name different errors on two ClickHouse lines, so a caller that needs several lines asks each library. The name is v0's, with the v1 call shape; the tombstone keeps every v0 binding from reaching it.

### chs_registered_families

Every type family in this build's own type registry, one per line. A tooling export for the artifact producer's build gates: no binding wraps it, and a loader resolves it only for presence.

### chs_function_flags

A tab-separated audit of every registered function's volatility flags, one function per line, read off this build's own registry. A tooling export for the artifact producer's build gates, which derive the statelessness evidence from it; no binding wraps it.

### chs_reference_type

The widened reference type this build pairs with a type expression. A tooling export; no binding wraps it.

### chs_initialize

Sets this image's server zone, once per process: the zone every compiled type binds, and the zone a call without its own `session_timezone` runs in. `timezone` is counted bytes, and length 0 means `UTC`. The name is validated by ClickHouse's own `DateLUT`, so a name it cannot load is `CHS_REJECTED` with `DateLUT`'s own code and message. A second call with the same spelling is a no-op that answers `CHS_OK`; a different spelling is `CHS_INVALID_ARGUMENT`, naming both. The spelling is compared byte for byte and never canonicalized, because it is observable: `timezoneOf` over a column reports the image zone exactly as spelled.

The zone is process state, not a per-call parameter, because ClickHouse's own MergeTree code reads the server zone directly (`DateLUT::serverTimezoneInstance`). A per-call zone is the `session_timezone` setting instead. A loader calls this once, after the handshake checks and before any other call; the list of type families the build must refuse is embedded when the library is built, so nothing else is passed in.

### chs_set_defaults

Seeds the settings every later call starts from, as a JSON object whose values are JSON strings (a binding never rewrites a value, a boolean included). Each successful call replaces the previous defaults whole. A setting name the server would refuse is refused here, with ClickHouse's own code and message, and nothing is committed. `session_timezone` here is the default zone for later calls; it does not change the image zone that compiled types bind.

Setup only. The defaults are immutable once this image has created its first `chs_schema` (a filter or a block needs one): from then on the call changes nothing and answers `CHS_INVALID_ARGUMENT`. No call ever reads defaults that change under it, so no binding needs a lock around them.

### chs_shutdown

Stops what `chs_initialize` started (its background threads) and joins them. It is idempotent and safe before `chs_initialize`. After it, no call is valid except `chs_shutdown` again. Unloading the library, or initializing it again in the same process, is unsupported: a loader never unloads a library.

### chs_type_validate

Parses one type expression with ClickHouse's own parser and returns its canonical spelling, or ClickHouse's own refusal.

### chs_back_quote

A name quoted the way ClickHouse's own `backQuote` quotes it: always quoted, every special byte escaped. A passthrough over the vendored function, and named after it; the name is a byte string.

### chs_back_quote_if_needed

A name quoted only where this build's own `backQuoteIfNeed` says it must be. Which names stay bare is a property of the ClickHouse release, so a caller that needs one answer for several releases asks each library.

### chs_quote_string

A byte string spelled as a ClickHouse string literal by the vendored `quoteString`. The input may contain NUL; the answer escapes it.

### chs_server_create

Describes one ClickHouse server, from a profile document (`input:server_profile`): its `timezone`, judged by ClickHouse's own `DateLUT`; its `settings`, judged by the server's own SET check; and its `macros`, read by ClickHouse's own `Macros` configuration reader, as a server reads `<macros>`. Every member is optional, and a member the profile omits is not described: the image zone applies, there is no server settings layer, and the server's macros are unknown. `options` (`input:server_options`) defines no member yet.

Everything is validated here, once, and the server never changes after it. A key the profile or the options document does not define is `CHS_INVALID_ARGUMENT`, naming the key, and so is a document that is not a JSON object, a member of the wrong JSON type, a NUL in the zone, an empty macro name, a NUL in a macro's name or value, or a macro set that ClickHouse's own reader does not read back exactly as given (a `.` in a name is one). A zone `DateLUT` cannot load is `CHS_REJECTED` with `DateLUT`'s own code and message (36, BAD_ARGUMENTS). A setting the server's SET check refuses is `CHS_REJECTED` with the server's own code and message: 115 (UNKNOWN_SETTING) for a name the server does not know, a library knob such as a `chtypes_*` setting included, because a server's profile holds ClickHouse settings only. Nothing is created on any refusal.

`CHS_DECLINED` is reserved: a build that will not describe a profile declines it, which is never a wrong answer. The one decline foreseen is a setting outside the set that shapes an input, at a value other than its default, once that set is generated from the vendored source.

The macros are the server's whole `system.macros` when `macros` is present, even as `{}`: a macro a Replicated engine's arguments name that the set lacks is then the server's own refusal (`CHS_REJECTED`, 139, NO_ELEMENTS_IN_CONFIG). When `macros` is absent the server's macros are unknown, and a schema whose engine reads one is declined, as it is without a server. A build that validates a profile's macros but does not yet apply them declines such a schema in both cases.

A setting this build's filters do not honor in a WHERE is no decline here, and the call succeeds. So does `chs_schema_create` on the server, because a profile never changes a schema compile: it lists the setting in the schema's `filter_declined_settings` (`schema_description`), at layer `server`.

The call reads no defaults and leaves `chs_set_defaults`' latch open. Like every call that is not the handshake, it is valid only after `chs_initialize`.

### chs_server_free

Releases the caller's reference to a server. Schemas created on it keep it alive. Freeing NULL does nothing.

### chs_schema_create

Compiles exactly one `CREATE TABLE` statement (columns, engine, keys, TTL, settings and constraints) with ClickHouse's own parser and the checks a server's CREATE runs, on `server`, under a profile of settings given as a JSON object of string values. A trailing semicolon is allowed. A second statement is refused the way ClickHouse's own parser refuses one in a single query (`CHS_REJECTED`, with its code and message), and a statement of any other kind is `CHS_INVALID_ARGUMENT`.

`server` is the server the table is on (`chs_server_create`), or NULL. On a server, the schema's home zone is the server's `timezone`: its zone-less `DateTime` and `DateTime64` columns bind it, as a restarted server binds its own, and every call on the schema that names no `session_timezone` runs in it. The server's `settings` layer under the schema's own, and a Replicated engine's ZooKeeper path and replica name expand the server's `macros`. With `server` NULL the schema is on the image's own server, exactly as before servers existed: the image zone, no server settings layer, and macros unknown. A freed server, a handle of another kind or a server from another library image is `CHS_INVALID_ARGUMENT`. The schema holds a counted reference to its server, so the caller may free the server at any time.

`timezone()` (and its alias `timeZone()`) in a DEFAULT, MATERIALIZED or CHECK expression is admitted on a schema created on a server whose profile names a `timezone`. On a server schema, `timezone()` answers the server's zone when no session zone is in force; a session zone that would change the answer is declined (named in `unsupported_settings`). That applies equally to a profile whose `settings` carry `session_timezone`. On a schema with no server, or a server whose profile names no `timezone`, it stays refused as a server constant. `serverTimezone()` is refused on every schema.

A setting this build's filters do not honor in a WHERE, from the `chs_set_defaults` snapshot this call captures, the server profile or this call's own `settings`, is no decline here: the call succeeds and lists it in the schema's `filter_declined_settings` (`schema_description`), and `chs_filter_create` on the schema declines.

`options` (`input:schema_options`) defines no member yet, so any key in it is `CHS_INVALID_ARGUMENT`, naming the key.

A build that does not yet compile on a server declines a non-NULL `server`, and a schema whose compile needs its server's zone is declined on a host thread that already runs a ClickHouse query of its own. Each is `CHS_DECLINED`, which is never a wrong answer.

The result is immutable: nothing changes a schema after this call, so it replaces v0's column-list compile and its engine, TTL and partition-key setters, and any number of threads may use it at once. Its types bind its home zone, never the `session_timezone` in its settings, which is the zone of this call and of the later calls on the schema that name none of their own.

### chs_schema_free

Releases the caller's reference to a schema. Filters and blocks made from it keep it alive. Freeing NULL does nothing.

### chs_schema_describe

A JSON document describing the schema's columns, owned by the caller; it replaces v0's borrowed column accessors.

A deep input the server's stack check refuses is `CHS_REJECTED` with ch_code 306. Here the input is the schema's own statement, which describing walks again; it is never `CHS_INTERNAL`.

### chs_preview_row

Validates and coerces one row of `body` under the schema, as a server's INSERT would, and returns the row's document. `columns` is the INSERT column list, a JSON array of name objects (`{name}` or `{name_b64}`), and empty for none. `session_timezone` in `settings` is this call's zone.

An INSERT whose input block has no column at all (every insertable column EPHEMERAL, and no column list) is declined: a server answers it (code 90, EMPTY_LIST_OF_COLUMNS_PASSED) from a statement inside its INSERT interpreter that the library cannot call, and a decline is never a wrong answer.

### chs_preview_batch

Validates and coerces a whole body, which may hold many rows, and returns the batch document, with the same declines as `chs_preview_row`. Row separation and the server's error allowance are ClickHouse's own, and the document's `unconsumed` and `framing` say what the reader skipped and how it framed the body. `filter`, when given, is evaluated over each stored row in the same parse. When that filter's WHERE would read a setting this build's filters do not honor, from the filter's own `settings`, the name joins the batch document's top-level `unsupported_settings` and the batch is `unsupported` by `batch_outcome`'s step 2: every row `unsupported`, filter rows `d`, and no export. The same call without the filter is unchanged. `export_format` is `CHS_EXPORT_NONE` or a `chs_format` the build can write; with an export, `out_export` receives the accepted rows serialized once, and it may be NULL when no export is asked for. `doc_flags` chooses which groups the per-row documents carry.

### chs_filter_create

Compiles a boolean SQL expression over the schema's columns, binding its query parameters (a JSON object of string values) by ClickHouse's own substitution, so a value is never SQL text. `settings` is the profile the expression is compiled under, so `session_timezone` there is the zone of its literals. Non-deterministic expressions are declined.

The WHERE's settings are layered, lowest first: the build's own, `chs_set_defaults`, the schema's server profile, the schema's own settings, and then this call's `settings`; `session_timezone` at every layer stays on the zone path. A type gate in the schema's own settings binds the CREATE only, so a WHERE's CAST validates under the session layers, every layer but the schema's own.

On a schema whose `filter_declined_settings` is not empty (`chs_schema_describe`), from any layer, the call is `CHS_DECLINED` with ch_code 0. It is checked after the argument checks and before the expression is parsed, because `dialect` can change the parse, and the message names each setting and its layer, in the list's order: `chs_filter_create: the settings this schema was compiled under set <a> (<layer>), <b> (<layer>), which this build's filters do not honor in a WHERE (listed in chs_schema_describe's filter_declined_settings); declined rather than answered`, where each `<layer>` is the entry's `declined_layer` value. The server and the schema were created all the same: no settings layer changes a schema compile.

**Changed (public issue #588).** A declined setting from the `chs_set_defaults` or the schema layer moves from evaluation time to create time: it was named at evaluation (`filter_result`'s `unsupported_settings`), and it now declines this call. A server-profile setting already declined it.

A key of this call's own `settings` other than `session_timezone` is never `CHS_DECLINED` here, and it is never in `schema_description`. A key in this build's honor set is applied to the WHERE; a key passed through is accepted and ignored; and a key this build's filters do not honor is reported at evaluation (`filter_result`'s `unsupported_settings`, every verdict `d`, and `chs_preview_batch`'s), as a declined setting from an evaluation's own settings is. Every key still meets the server's own SET check first, so a name the server does not know is `CHS_REJECTED` with ch_code 115.

A filter under a per-call `session_timezone` answers as the artifact producer has measured against live servers, including how the zone in an evaluation's settings combines with the zone the filter was compiled under. Until a path is measured to match a server it declines, which is never a wrong answer. The filter holds a counted reference to the schema.

### chs_filter_free

Releases the caller's reference to a filter. Freeing NULL does nothing.

### chs_filter_eval_body

Evaluates the filter over every row of a body, returning one verdict per row. `settings` governs parsing the body, and `session_timezone` there is this call's zone. When a server would not accept an INSERT of the body, because `chs_preview_batch`'s verdict over the same body and settings, with no filter, is not `accepted`, every verdict is `d` and the outcome mirrors that verdict (`filter_outcome`). The call reads an accepted body once, for its verdicts and that batch verdict together.

### chs_block_create

Parses a body once under the schema, for evaluating many filters over it. `settings` governs the parse, `session_timezone` there included, and `columns` is read as `chs_preview_row` reads it. The block holds a counted reference to the schema. It also records `chs_preview_batch`'s verdict over the same body, settings and columns, with no filter, which `chs_filter_eval_block` answers by. An accepted body is read once, for the block and that verdict together.

### chs_block_free

Releases the caller's reference to a block. Freeing NULL does nothing.

### chs_filter_eval_block

Evaluates the filter over a parsed block. Over a body a server would accept, it gives the same answers `chs_filter_eval_body` gives for that body. Over one it would not (the verdict `chs_block_create` recorded is not `accepted`), the outcome mirrors that verdict as for `chs_filter_eval_body`, and every verdict from the first refused row on is `d`. The first refused row is the first row of that batch whose outcome is neither `accepted` nor `skipped`. When the refusal belongs to no row (a whole-body step, a poisoned value, the call's own settings), it is the first row of the block. The rows before it keep their evaluated verdicts. A caller treats those as `d` too: any non-`ok` outcome, including `unsupported`, means every verdict is `d`. It takes no settings: the filter brings the settings it was compiled under and the block those it was parsed under. The filter and the block must come from the same schema; a pair from two schemas is `CHS_INVALID_ARGUMENT`.

### chs_discover_query

The query a caller runs against a server to read a table's `system.columns` rows, so that no binding holds SQL of its own. It is the connect-time way to rebuild a table's columns from a server; whether a server's `SHOW CREATE TABLE` output compiles under `chs_schema_create` instead is not yet measured. It takes no input. The SQL names the table through the query parameters in `discover_query_param` (`{database:String}` and `{table:String}`), which the caller binds when it runs the query; it selects exactly the `system.columns` fields `chs_discover_columns` reads, `FORMAT JSONEachRow`.

### chs_discover_columns

Reads a server's `system.columns` rows (the JSONEachRow answer to `chs_discover_query`'s query) with ClickHouse's own reader and returns the column declarations, formatted by ClickHouse's own formatter.
