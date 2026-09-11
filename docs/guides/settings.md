# Settings and precedence

ClickHouse's answers depend on the settings in force. `flatten_nested` changes a table's shape; `input_format_allow_errors_ratio` decides whether a bad row aborts a batch or is skipped; a type gate decides whether a column type is legal at all. A preview computed under a different profile from the INSERT is a preview of a different server.

So settings travel on four channels, and the rule is: **the settings chtypes sees must be the settings the INSERT runs under.** [`discovery.md`](discovery.md) is how you learn what those are without asking the customer.

## The four channels, and which wins

On the row path, the leftmost channel that names a setting wins:

```
per-call map  >  handle compile profile  >  library defaults  >  ClickHouse's own defaults
```

| channel | set by | scope |
|---|---|---|
| per-call map | the third argument to `rows` / `row` | that one call |
| compile profile | the settings passed at compile | that schema handle, for its life |
| library defaults | `set_default_settings` | the whole process, per loaded artifact |
| ClickHouse defaults | the vendored build | everything |

**Go reaches only three of them.** `SetDefaultSettings` replaces a process-global that the row path reads *by reference*, so the ABI requires it to exclude every other call on the same artifact. Go's dlopen'd `Library` therefore does not carry the symbol in its function-pointer table at all — the call is structurally unavailable rather than merely discouraged, and `chtypes.SetDefaultSettings` exists only on the statically linked build (`-tags chtypes_linked`), which is a development and rig instrument. Put the settings a Go consumer needs in the compile profile and the per-call map, which is where they belong anyway.

<details open><summary><b>Go</b></summary>

```go
schema, _ := lib.CompileDDL(ddl, chtypes.WithCompileSettings(profile))   // handle
batch, _ := schema.Rows(chtypes.JSONEachRow, body, map[string]string{"date_time_input_format": "best_effort"})
```

Go has no process-wide channel on the dlopen path: `chtypes.SetDefaultSettings` is a package-level function on the statically linked build only. See the note below.

</details>

<details><summary><b>Python</b></summary>

```python
library.set_default_settings({"chtypes_default_eval_wall_nanos": "2000000000"})   # process
schema = library.compile_ddl(ddl, settings=profile)                              # handle
batch = schema.rows(Format.JSON_EACH_ROW, body, {"date_time_input_format": "best_effort"})
```

</details>

<details><summary><b>TypeScript</b></summary>

```ts
lib.setDefaultSettings({ chtypes_default_eval_wall_nanos: '2000000000' });   // process
const schema = lib.compileDdl(ddl, { settings: profile });                   // handle
const batch = schema.rows(Format.JSONEachRow, body, { date_time_input_format: 'best_effort' });
```

</details>

<details><summary><b>Rust</b></summary>

```rust
lib.set_default_settings(&[("chtypes_default_eval_wall_nanos", "2000000000")])?;  // process
let schema = lib.compile(ddl).settings(profile.clone()).compile()?;               // handle
let batch = schema.rows(Format::JsonEachRow, body, &[("date_time_input_format", "best_effort")])?;
```

Note the shapes: the compile builder's `.settings()` takes anything iterable, while `rows` takes a **slice reference**, `&[(k, v)]`. `NO_SETTINGS` is the spelled-out empty map for the common call.

</details>

## The one exception: type gates bind at compile

A **type gate** — `allow_experimental_json_type`, `allow_suspicious_low_cardinality_types`, and the rest of that family — behaves differently, and the difference is the server's own, not an invention here.

A gate named in the **compile profile** binds at compile. A refusing value fails the compile with the server's own 455 or 44, exactly as `CREATE TABLE` would. And from then on the handle **outranks even an explicit per-call value** for that name, because a real server checks a gate when a column is created and never again. A gate the profile does not name keeps the ordinary per-call re-check.

This is the one place the precedence table above does not hold, and it holds the way it does because a table that exists is a table that exists: the gate settled at CREATE, and no later INSERT reopens the question.

## Values cross as strings. Always.

This has cost real bugs, so each binding enforces it as hard as its type system allows.

| | how |
|---|---|
| Go | `map[string]string` — structural, nothing else compiles |
| Rust | `&[(K, V)]` where both are `AsRef<str>` — structural |
| Python | stringifies an `int` exactly (`str(int)`, never through a float) and **raises `TypeError` on a `float`** |
| TypeScript | accepts `string` and `bigint`; **rejects a JS `number` at runtime**, because TypeScript is not present at a JS consumer's call site |

The reason is one setting in particular. `chtypes_now_epoch_nanos` is a 19-digit nanosecond epoch, and 19 digits do not survive an IEEE double: `1700000000123456789` becomes `1.7e+18`. Sent as a JSON number, the setting is **silently ignored** — the batch keeps stamping the real wall clock, and nothing anywhere says so. A loud `TypeError` is the whole point.

## An unknown name refuses the whole call

Spell a setting wrong and you get ClickHouse's own **code 115**, did-you-mean hint included, on every channel:

```
[115] Setting nope_not_a_setting is neither a builtin setting nor started with
the prefix 'SQL_' registered for user-defined settings
```

`compile_ddl` fails the compile. `row` and `rows` reject the call. `set_default_settings` refuses its payload **wholesale** — nothing is committed, so a partial profile is never silently in force. That last one matters most: a half-applied process seed would be a server whose behavior you cannot describe.

Obsolete setting names are accepted silently, as real servers do. Custom-prefixed names are legal and inert — the default prefix is `SQL_`, and `chtypes_custom_settings_prefixes` mirrors whatever your server's `custom_settings_prefixes` is set to.

Catching a typo at **declare** time is the point of passing the discovered profile to the compile rather than only per call. A typo in a profile that is only ever passed per call is a typo you find on the hot path.

## The six `chtypes_*` keys

The reserved namespace is exactly six keys — not a `chtypes_` prefix. Anything else spelled `chtypes_*` is an unknown name and gets 115 like any other typo.

Three are **per-call**, and they control the clock:

| key | does |
|---|---|
| `chtypes_now_epoch_nanos` | pin the batch instant — the one to use in tests |
| `chtypes_clock_offset_nanos` | a measured server-minus-client offset |
| `chtypes_max_clock_skew_nanos` | refuse volatile-DEFAULT substitution past this budget, answering `unsupported` rather than substituting |

Three are **per-process**, settable only through `set_default_settings`:

| key | does | default |
|---|---|---|
| `chtypes_default_eval_memory_bytes` | DEFAULT-expression admission ceiling | 256 MiB |
| `chtypes_default_eval_wall_nanos` | DEFAULT-expression wall-clock ceiling | 1 s |
| `chtypes_custom_settings_prefixes` | mirror the server's own | `SQL_` |

**Sending a per-process key on a per-call map is a decline, never an admission.** It comes back in the result's `unsupported_settings`, and the row is promoted to the `unsupported` outcome. Do not score such a row as agreement: chtypes did not answer it.

## MergeTree settings are a different namespace

The `SETTINGS` clause after an engine declaration is its own namespace, and it is passed separately — `WithMergeTreeSettings` in Go, `merge_tree_settings=` in Python, `mergeTreeSettings` in TypeScript, the third argument of `set_engine` in Rust.

The two failure modes there are deliberately different from each other:

- an **unknown** MergeTree setting name is the server's own 115, a schema error;
- a **known** name at a **non-default** value is `unsupported` — a decline. It is not modelled, so it is not guessed at, and it is certainly not silently ignored.

## Next

- [`discovery.md`](discovery.md) — where a correct profile actually comes from.
- [`batches.md`](batches.md) — `input_format_allow_errors_*`, the settings with the sharpest consequences.
- [`transformations.md`](transformations.md) — the clock settings in the context of substituted DEFAULTs.
