# Settings and precedence

ClickHouse's answers depend on the settings in force. `flatten_nested` changes a table's shape; `input_format_allow_errors_ratio` decides whether a bad row aborts a batch or is skipped; a type gate decides whether a column type is legal at all. A preview computed under a different profile from the INSERT is a preview of a different server.

So settings travel on four channels, and the rule is: **the settings chtypes sees must be the settings the INSERT runs under.** [`discovery.md`](discovery.md) is how you learn what those are without asking the customer.

## The four channels, and which wins

On the row path, the leftmost channel that names a setting wins:

```text
per-call settings  >  handle compile profile  >  setup defaults  >  ClickHouse's own defaults
```

| channel             | set by                                                | scope                                                     |
| ------------------- | ----------------------------------------------------- | --------------------------------------------------------- |
| per-call settings   | the `settings` option of `rows`, `row` and the others | that one call                                             |
| compile profile     | the `settings` option of `compile_table`              | that schema handle, for its life                          |
| setup defaults      | `setup(defaults=…)`                                   | the whole process, applied to every library when it opens |
| ClickHouse defaults | the vendored build                                    | everything                                                |

**The setup defaults latch.** `setup` is called once, before the first library opens, and the defaults it records are applied to each library as it loads and never again. A second `setup` with the same zone and defaults is a no-op; a different one is a `UsageError` naming both, and the first stands. If `setup` was never called, the first open commits the empty setup: no defaults, and the library's default zone, `UTC`. The latch closes only when a library completes its setup. Until then, an open that attempts a load and fails keeps the setup but unlocks it, whatever failed: the fetch, the signature, an artifact that will not load, or the library refusing the zone or a default. A retry with no new `setup` uses the recorded zone and defaults, and a different `setup` is accepted and replaces them. A refused version spelling, or an unverified open without its opt-ins, fails before any load and unlocks nothing ([`reference/bindings-v1.md` §6](../reference/bindings-v1.md#6-setup-and-from-a-request-to-a-loaded-library)). So call `setup` first. **There is no runtime setter in any binding.** 0.x's `set_default_settings` (and Go's `SetDefaultSettings`, which existed only on the linked build) is deleted ([`reference/bindings-v1.md` §7](../reference/bindings-v1.md#7-what-v0-api-is-deleted-and-why)); per-call settings and the compile profile carry everything that changes after setup. All four bindings reach all four channels now, Go included.

<details open><summary><b>Go</b></summary>

```go
_ = chtypes.Setup(chtypes.SetupOptions{ // once, first
	Defaults: map[string]string{"chtypes_default_eval_wall_nanos": "2000000000"},
})
schema, _ := lib.CompileTable(ddl, chtypes.WithSettings(profile)) // handle
batch, _ := schema.Rows(chtypes.JSONEachRow, body,
	chtypes.WithSettings(map[string]string{"date_time_input_format": "best_effort"})) // call
```

</details>

<details><summary><b>Python</b></summary>

```python
chtypes.setup(defaults={"chtypes_default_eval_wall_nanos": "2000000000"})   # process, once, first
schema = library.compile_table(ddl, settings=profile)                       # handle
batch = schema.rows(Format.JSON_EACH_ROW, body, settings={"date_time_input_format": "best_effort"})
```

</details>

<details><summary><b>TypeScript</b></summary>

```ts
setup({ defaults: { chtypes_default_eval_wall_nanos: '2000000000' } });   // process, once, first
const schema = lib.compileTable(ddl, { settings: profile });              // handle
const batch = schema.rows(Format.JSONEachRow, body, { settings: { date_time_input_format: 'best_effort' } });
```

</details>

<details><summary><b>Rust</b></summary>

```rust
chtypes::setup(SetupOptions {                                             // process, once, first
    defaults: vec![("chtypes_default_eval_wall_nanos".into(), "2000000000".into())],
    ..Default::default()
})?;
let schema = lib.compile_table(ddl, &CompileOptions { settings: profile.clone(), ..Default::default() })?; // handle
let call = RowsOptions {                                                  // call
    settings: vec![("date_time_input_format".into(), "best_effort".into())],
    ..Default::default()
};
let batch = schema.rows(Format::JsonEachRow, body, &call)?;
```

Each options struct has public fields and `Default`, so a call that sets nothing passes `&RowsOptions::default()`.

</details>

`defaults` exists in `setup` only while the library keeps its `chs_set_defaults` entry point: if a regenerated description drops it, the field is deleted with it, and the per-call settings and the compile profile carry everything ([`reference/bindings-v1.md` §6](../reference/bindings-v1.md#the-process-setup)).

## The one exception: type gates bind at compile

A **type gate** — `allow_experimental_json_type`, `allow_suspicious_low_cardinality_types`, and the rest of that family — behaves differently, and the difference is the server's own, not an invention here.

A gate named in the **compile profile** binds at compile. A refusing value fails the compile with the server's own 455 or 44, exactly as `CREATE TABLE` would. And from then on the handle **outranks even an explicit per-call value** for that name, because a real server checks a gate when a column is created and never again. A gate the profile does not name keeps the ordinary per-call re-check.

This is the one place the precedence table above does not hold, and it holds the way it does because a table that exists is a table that exists: the gate settled at CREATE, and no later INSERT reopens the question.

## Values cross as strings. Always

This has cost real bugs, so v1 makes it one rule in all four bindings: **a settings value is a string, and only a string.** The binding serializes the map into a JSON object of string values with the language's stock encoder and passes it verbatim, and it never rewrites a value: no boolean or integer spelling, no float. A non-string value is the language's own type error.

|            | how                                                                                  |
| ---------- | ------------------------------------------------------------------------------------ |
| Go         | `map[string]string` — structural, nothing else compiles                              |
| Rust       | `&[(K, V)]` or `Vec<(String, String)>` where both are strings — structural           |
| Python     | a non-string value raises `TypeError`, an `int` and a `bool` included                |
| TypeScript | a non-string value raises `TypeError` at runtime, a `number` and a `bigint` included |

0.x's friendlier encoders are deleted: Python no longer stringifies an `int` or rewrites a boolean, and TypeScript no longer accepts a `bigint`. Write `"1"`, not `1` or `True`.

The reason is one setting in particular. `chtypes_now_epoch_nanos` is a 19-digit nanosecond epoch, and 19 digits do not survive an IEEE double: `1700000000123456789` becomes `1.7e+18`. Sent as a JSON number, the setting would be **silently ignored** — the batch keeps stamping the real wall clock, and nothing anywhere says so. A loud `TypeError` is the whole point.

A value whose bytes are not valid UTF-8 cannot be passed as a setting or a query parameter in 1.0. Inline it in the SQL as `unhex('<hex>')`, built from hex digits only ([`limitations.md`](../limitations.md#binary-input-parameters-must-be-utf-8)).

## An unknown name refuses the whole call

Spell a setting wrong and you get ClickHouse's own **code 115**, did-you-mean hint included:

```text
[115] Setting nope_not_a_setting is neither a builtin setting nor started with
the prefix 'SQL_' registered for user-defined settings
```

`compile_table` fails the compile. `row` and `rows` reject the call. A typo in the `setup` defaults is refused by the library when the first open applies them (`unverified`: the refusal path for `chs_set_defaults` is not measured here), which is the reason to call `setup` at startup, not lazily.

Obsolete setting names are accepted silently, as real servers do. Custom-prefixed names are legal and inert — the default prefix is `SQL_`, and `chtypes_custom_settings_prefixes` mirrors whatever your server's `custom_settings_prefixes` is set to.

Catching a typo at **declare** time is the point of passing the discovered profile to the compile rather than only per call. A typo in a profile that is only ever passed per call is a typo you find on the hot path. Per-call values are not validated the way a server's `SET` validates them, so validate a value yourself before relying on a refusal ([`limitations.md`](../limitations.md#per-call-settings-values-are-not-validated-the-way-a-servers-set-validates-them)).

## The six `chtypes_*` keys

The reserved namespace is exactly six keys — not a `chtypes_` prefix. Anything else spelled `chtypes_*` is an unknown name and gets 115 like any other typo.

Three are **per-call**, and they control the clock:

| key                            | does                                                                                                    |
| ------------------------------ | ------------------------------------------------------------------------------------------------------- |
| `chtypes_now_epoch_nanos`      | pin the batch instant — the one to use in tests                                                         |
| `chtypes_clock_offset_nanos`   | a measured server-minus-client offset                                                                   |
| `chtypes_max_clock_skew_nanos` | refuse volatile-DEFAULT substitution past this budget, answering `unsupported` rather than substituting |

Three are **per-process**, settable only through the `defaults` of `setup`:

| key                                 | does                                  | default |
| ----------------------------------- | ------------------------------------- | ------- |
| `chtypes_default_eval_memory_bytes` | DEFAULT-expression admission ceiling  | 256 MiB |
| `chtypes_default_eval_wall_nanos`   | DEFAULT-expression wall-clock ceiling | 1 s     |
| `chtypes_custom_settings_prefixes`  | mirror the server's own               | `SQL_`  |

The functions a DEFAULT expression cannot use at all are listed, with the reason for each, in [`declined-functions.md`](../reference/declined-functions.md).

**Sending a per-process key on a per-call map is a decline, never an admission.** It comes back in the result's `unsupported_settings`, and the row is promoted to the `unsupported` outcome (the library applies that promotion itself in 1.0; no binding does). Do not score such a row as agreement: chtypes did not answer it.

## Time zones are two things

A bare `DateTime` column carries a zone, and ClickHouse resolves it in two different places. 1.0 gives each its own spelling, and they are not interchangeable:

- **The image zone** is process-wide, set once at `setup(timezone=…)`, before the first open. It governs compiled types: a bare `DateTime` column's zone, `MATERIALIZED` columns, `PARTITION BY` and `TTL`. Unset, it is `UTC`. It is ClickHouse's own process zone, which vendored MergeTree code reads directly, so it cannot vary per call.
- **The per-call zone** is the `session_timezone` key in a call's settings. It governs parsing, rendering, export and DEFAULT evaluation for that call. Each binding also offers it as an option, `WithSessionTimezone` in Go, `session_timezone=` in Python, `sessionTimezone` in TypeScript and `session_timezone` in Rust, so the zone is visible in every signature it affects. The option writes the one `session_timezone` key into the settings object and does nothing else with it. **Passing the option and a `session_timezone` key in `settings` together is a `UsageError`, even when the two agree**, so a call never has two spellings of its zone.

A zone name is valid exactly when ClickHouse's own `DateLUT` loads it, and a name it will not load is refused with ClickHouse's own error, whatever its bytes. `DateLUT` loads zone files from the host the library runs on, so a host missing some zone files refuses names a server accepts ([`limitations.md`](../limitations.md#zone-names-outside-clickhouses-embedded-table-follow-the-host)).

The zone follows the settings precedence above: a per-call `session_timezone` beats the one in the compile profile, which beats the one in the setup defaults, which beats the image zone. A zone in the compile profile is a default for later calls on that schema; a compiled type always takes the image zone, never the profile's.

**A filter's zone is its own**, fixed when it is compiled: its `WHERE` runs in the zone of the settings the filter was compiled with, and an evaluation's settings decide only how the body is parsed. [`reference/bindings-v1.md` §2](../reference/bindings-v1.md#the-call-options) has the rule and the per-binding spellings.

0.x declined a per-call `session_timezone` that named a different zone than the process's, because zone was resolved once at `chs_init`. That decline is gone with the model it protected.

## MergeTree settings are part of the statement

The `SETTINGS` clause after an engine declaration is its own namespace, and in 1.0 it is part of the one `CREATE TABLE` statement you compile: `... ENGINE = MergeTree ORDER BY k SETTINGS index_granularity = 4096`. There is no separate argument for them any more (`WithMergeTreeSettings`, `merge_tree_settings=` and `set_engine` are deleted, [`reference/bindings-v1.md` §7](../reference/bindings-v1.md#7-what-v0-api-is-deleted-and-why)).

The clause goes through the server's own MergeTree settings loader, and each setting gets one of four answers:

- an **unknown** name is the server's own 115, a schema error, and a value that does not parse is the server's own code (27 for `index_granularity = 'abc'`);
- a setting **at its default** is inert;
- a setting **only the storage layer reads**, such as a part lifetime or a merge timeout (`old_parts_lifetime = 100`), is accepted at any value, because the server stores the same rows with it as without it;
- a setting the server **reads while it creates the table or inserts into it**, at a **non-default** value (`index_granularity = 4096`), is `unsupported` — a decline, and so is `disk` at any value. It is not modeled, so it is not guessed at, and it is certainly not silently ignored. The decline's message names the setting.

The refusal and the decline are deliberately different kinds: a refusal means the server would refuse the statement too, and a decline means this library will not guess. Library builds before `20261007.044112` declined every non-default value.

## Next

- [`discovery.md`](discovery.md) — where a correct profile actually comes from.
- [`batches.md`](batches.md) — `input_format_allow_errors_*`, the settings with the sharpest consequences, including [how a malformed `UUID` can swallow the records behind it](batches.md#a-malformed-uuid-swallows-the-records-behind-it) and [input framing rules](batches.md#framing-bom-whitespace-and-line-ends) (BOM, `\f`/`\v`, `\n\r`).
- [`transformations.md`](transformations.md) — the clock settings in the context of substituted DEFAULTs.
