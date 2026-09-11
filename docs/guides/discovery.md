# Discovery — ask the server, never the customer

chtypes **never connects to ClickHouse**. It has no client, no connection string, no network code on the answer path. What it ships instead is three SQL constants, which your application runs at connect time with whatever client it already has, plus typed parsers for the results.

That split is deliberate. A library that opened its own connection would need credentials, a pool, a TLS story and a retry policy, all of which your application already has and has already tuned.

## The three queries

| constant | answers | feeds |
|---|---|---|
| `QUERY_SERVER_VERSION` | the exact release | which artifact to resolve |
| `QUERY_CHANGED_SETTINGS` | every setting changed from default | the compile profile **and** the per-call settings |
| `QUERY_TABLE_COLUMNS` | one table's columns, kinds and expressions | reconstructing DDL to compile |

In Go the constants are `QueryServerVersion`, `QueryChangedSettings` and `QueryTableColumns`; the other three bindings spell them in screaming snake case.

## The shape of it

Run the first two once per connection, cache the profile per deployment (or per tenant, on bring-your-own-ClickHouse), then declare what you learned at every compile and every call.

<details open><summary><b>Go</b></summary>

```go
version, _ := chtypes.ParseVersionResult(run(chtypes.QueryServerVersion))
settings, _ := chtypes.ParseChangedSettingsResult(run(chtypes.QueryChangedSettings))
profile := chtypes.ServerProfile{Version: version, Settings: settings}

lib, _ := reg.For(chtypes.Version(profile.Version))
schema, _ := lib.CompileDDL(ddl, chtypes.WithCompileSettings(profile.Settings))
res, _ := schema.Rows(chtypes.JSONEachRow, body, profile.Settings)
```

</details>

<details><summary><b>Python</b></summary>

```python
import chtypes

profile = chtypes.ServerProfile(
    version=chtypes.parse_version_result(run(chtypes.QUERY_SERVER_VERSION)),
    settings=chtypes.parse_changed_settings_result(run(chtypes.QUERY_CHANGED_SETTINGS)),
)

library = registry.for_version(profile.version)
schema = library.compile_ddl(ddl, settings=profile.settings)
res = schema.rows(chtypes.Format.JSON_EACH_ROW, body, settings=profile.settings)
```

</details>

<details><summary><b>TypeScript</b></summary>

```ts
const profile: ServerProfile = {
  version: parseVersionResult(await run(QUERY_SERVER_VERSION)),
  settings: parseChangedSettingsResult(await run(QUERY_CHANGED_SETTINGS)),
};

const lib = registry.for(profile.version);
const schema = lib.compileDdl(ddl, { settings: profile.settings });
const res = schema.rows(Format.JSONEachRow, body, profile.settings);
```

</details>

<details><summary><b>Rust</b></summary>

```rust
let version = chtypes::parse_version_result(&query(chtypes::QUERY_SERVER_VERSION)?)?;
let settings = chtypes::parse_changed_settings_result(&query(chtypes::QUERY_CHANGED_SETTINGS)?)?;

let lib = registry.for_version(&version)?;
let schema = lib.compile(ddl).settings(settings.clone()).compile()?;
let batch = schema.rows(Format::JsonEachRow, body, &settings)?;
```

Rust's `parse_changed_settings_result` hands back a `Vec<(String, String)>` rather than a map, which is the shape the settings channels take anyway.

</details>

The settings go to **both** places on purpose: the compile profile is where a type gate binds, and the per-call map is where the row path reads everything else. [`settings.md`](settings.md) is why.

## Rebuilding DDL from an existing table

For a table that already exists, `QUERY_TABLE_COLUMNS` plus two parsers gives you the column-declaration list that `compile` takes. Bind `param_db` and `param_table` through your client's own query-parameter channel.

<details open><summary><b>Go</b></summary>

```go
cols, _ := chtypes.ParseColumnsResult(run(chtypes.QueryTableColumns))
ddl, _ := chtypes.ReconstructDDL(cols)
schema, _ := lib.CompileDDL(ddl, chtypes.WithCompileSettings(profile.Settings))
```

</details>

<details><summary><b>Python</b></summary>

```python
cols = chtypes.parse_columns_result(run(chtypes.QUERY_TABLE_COLUMNS))
schema = library.compile_ddl(chtypes.reconstruct_ddl(cols), settings=profile.settings)
```

</details>

<details><summary><b>TypeScript</b></summary>

```ts
const cols = parseColumnsResult(await run(QUERY_TABLE_COLUMNS));
const schema = lib.compileDdl(reconstructDdl(cols), { settings: profile.settings });
```

</details>

<details><summary><b>Rust</b></summary>

```rust
let cols = chtypes::parse_columns_result(&query(chtypes::QUERY_TABLE_COLUMNS)?)?;
let schema = lib.compile(&chtypes::reconstruct_ddl(&cols)?)
    .settings(settings.clone())
    .compile()?;
```

</details>

`reconstruct_ddl` carries `default_kind` and `default_expression` through. **Dropping them silently loses DEFAULT and MATERIALIZED semantics** — the columns are still there, they just stop behaving like themselves, and every preview after that is wrong in a way nothing reports. It is the reason the parsers exist rather than a suggestion to read `system.columns` yourself.

One thing to know about `system.columns`: it reports the table **as stored**, with nested columns already flattened under `flatten_nested=1`. Reconstruction is therefore shape-faithful exactly when the discovered profile is also declared at the compile. Discover both or neither.

## Why the parsers rather than your JSON library

All four read the `JSONEachRow` bytes through their own byte-exact reader, which is not a stylistic preference. `position` survives both quoted and bare spellings without a float round-trip, and numeric fields keep their exact text. A general-purpose JSON parser that turns every number into a double is the wrong tool for reading a report about exact values.

## The payoff: a typo is caught at declare time

Passing the discovered profile to the **compile** is what turns a misspelled setting into ClickHouse's own code 115 at the moment you declare it, rather than a silent difference between your preview and the server's behavior. That is the whole argument for discovery over configuration:

> **Never ask the customer for their settings — ask their server.** A customer describing their own deployment is a second source of truth, and it is the one that goes stale.

Cache the profile per deployment, and re-discover when a connection is re-established rather than per request. A server's version and changed settings do not move often, but they do move, and a cached profile from before an upgrade is a preview of a server that no longer exists.

## Next

- [`settings.md`](settings.md) — what the discovered profile actually does once you declare it.
- [`multi-version.md`](multi-version.md) — one process, several discovered deployments, several artifacts.
- [`quickstart.md`](../quickstart.md) — the version above was hard-coded; this is how it should have been found.
