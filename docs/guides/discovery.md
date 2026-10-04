# Discovery — ask the server, never the customer

chtypes **never connects to ClickHouse**. It has no client, no connection string, no network code on the answer path. What it gives you instead is one query, which your application runs at connect time with whatever client it already has, plus a typed reader for the answer.

That split is deliberate. A library that opened its own connection would need credentials, a pool, a TLS story and a retry policy, all of which your application already has and has already tuned. And no binding holds or builds SQL: the query comes from the loaded library, and the caller binds its two parameters the client's own way.

> **1.0 narrowed discovery to a table's columns.** 0.x also shipped queries for a server's version and its changed settings, plus a `ServerProfile` and a DDL reconstructor. Those are deleted ([`reference/bindings-v1.md` §7](../reference/bindings-v1.md#7-what-v0-api-is-deleted-and-why)), because a binding that holds SQL or rebuilds DDL is binding-side logic. Reading the version and the changed settings is yours to do on your own connection, and is a known gap: [`limitations.md`](../limitations.md#no-call-for-a-servers-version-or-settings).

## The calls

Two calls on a loaded `Library`:

| call                 | answers                                                                                                                                     | feeds                      |
| -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------- |
| `discover_query()`   | the query that reads one table's `system.columns` rows, `FORMAT JSONEachRow`                                                                | your client, which runs it |
| `discover_columns()` | the column declarations, written as ClickHouse's own formatter writes them, and `columns_sql`, the declarations joined for a `CREATE TABLE` | the statement you compile  |

In Go they are `DiscoverQuery` and `DiscoverColumns`, in TypeScript `discoverQuery` and `discoverColumns`, and in Python and Rust `discover_query` and `discover_columns`. The query takes two ClickHouse query parameters, `{database:String}` and `{table:String}`; over HTTP they are `param_database` and `param_table`. No binding splices a database or table name into the text.

## What to run at connect time

Three things, once per connection, cached per deployment (or per tenant, on bring-your-own-ClickHouse):

1. **The version.** `SELECT version()` on your connection, then ask the registry for it. A request is a bare two, three or four-part spelling; a `v` prefix or a `-lts` suffix is a `UsageError`, so pass the numeric parts only. The registry fetches a version it does not have if `autofetch` is on ([`guides/artifacts.md`](artifacts.md#lazy-fetch-is-opt-in)).
2. **The changed settings.** Your own query over `system.settings`. They become the profile you declare at every compile and every call.
3. **The table.** Either of two ways:
   - **Compile the server's own `SHOW CREATE TABLE` text.** Compile takes exactly one `CREATE TABLE` statement, and the text a server prints is one. This is measured on live 26.3, 26.7, 26.8 and 26.9 servers for every MergeTree-family shape tried (28 of 32); `ENGINE = Memory` tables are declined ([`limitations.md`](../limitations.md#show-create-of-a-memory-table-is-declined)).
   - **Use the discovery calls** below, which also cover those Memory tables.

## The shape of it

<details open><summary><b>Go</b></summary>

```go
query, _ := lib.DiscoverQuery()
rows := run(query, map[string]string{"database": "default", "table": "events"}) // your client; JSONEachRow bytes
found, _ := lib.DiscoverColumns(rows)

ddl := "CREATE TABLE events (" + found.ColumnsSQL + ") ENGINE = Memory"
schema, _ := lib.CompileTable(ddl, chtypes.WithSettings(profile))
res, _ := schema.Rows(chtypes.JSONEachRow, body, chtypes.WithSettings(profile))
```

</details>

<details><summary><b>Python</b></summary>

```python
rows = run(library.discover_query(), {"database": "default", "table": "events"})  # your client; JSONEachRow bytes
found = library.discover_columns(rows)

ddl = b"CREATE TABLE events (" + found.columns_sql + b") ENGINE = Memory"
schema = library.compile_table(ddl, settings=profile)
res = schema.rows(Format.JSON_EACH_ROW, body, settings=profile)
```

</details>

<details><summary><b>TypeScript</b></summary>

```ts
const rows = await run(lib.discoverQuery(), { database: 'default', table: 'events' }); // your client; JSONEachRow bytes
const found = lib.discoverColumns(rows);

const ddl = Buffer.concat([Buffer.from('CREATE TABLE events ('), found.columnsSql, Buffer.from(') ENGINE = Memory')]);
const schema = lib.compileTable(ddl, { settings: profile });
const res = schema.rows(Format.JSONEachRow, body, { settings: profile });
```

</details>

<details><summary><b>Rust</b></summary>

```rust
let rows = run(lib.discover_query()?.as_bytes(), &[("database", "default"), ("table", "events")])?; // your client; JSONEachRow bytes
let found = lib.discover_columns(&rows)?;

let ddl = [b"CREATE TABLE events (".as_slice(), found.columns_sql.as_bytes(), b") ENGINE = Memory"].concat();
let compile = CompileOptions { settings: profile.clone(), ..Default::default() };
let schema = lib.compile_table(ddl, &compile)?;
let call = RowsOptions { settings: profile.clone(), ..Default::default() };
let batch = schema.rows(Format::JsonEachRow, body, &call)?;
```

</details>

Here `run` is your own function and `profile` is the changed-settings map you read: a `map[string]string`, a `Settings` mapping, a `Record<string, string>`, or a `Vec<(String, String)>`, with string values only ([`settings.md`](settings.md#values-cross-as-strings-always)). The ENGINE clause is yours to supply when you build the statement from `columns_sql`: the discovery answer is the column list, and it does not carry the table's engine (`inferred` from the `system.columns` fields it reads).

The settings go to **both** places on purpose: the compile profile is where a type gate binds, and the per-call map is where the row path reads everything else. [`settings.md`](settings.md) is why.

## Why the declarations carry their DEFAULTs

`discover_columns` writes each column as ClickHouse's own formatter writes it, and `columns_sql` joins them, so a `DEFAULT`, `MATERIALIZED` or `ALIAS` expression travels with its column. **Dropping them silently loses DEFAULT and MATERIALIZED semantics** — the columns are still there, they just stop behaving like themselves, and every preview after that is wrong in a way nothing reports. It is the reason the reader exists rather than a suggestion to build a column list from `system.columns` yourself.

One thing to know about `system.columns`: it reports the table **as stored**, with nested columns already flattened under `flatten_nested=1`. Compiling from it is therefore shape-faithful exactly when the discovered profile is also declared at the compile. Discover both or neither.

## Why the reader rather than your JSON library

The answer is `JSONEachRow` bytes with ClickHouse's own renderings (UInt64 positions arrive as quoted strings, and a column name can be any bytes), and the library reads them: you hand the bytes through untouched. Parsing the rows yourself and rebuilding a declaration is exactly the binding-side logic 1.0 deletes. Each name and declaration comes back as bytes ([`reference/bindings-v1.md` §5, `Discovery`](../reference/bindings-v1.md#discovery-from-the-discovery-document)).

## The payoff: a typo is caught at declare time

Passing the discovered profile to the **compile** is what turns a misspelled setting into ClickHouse's own code 115 at the moment you declare it, rather than a silent difference between your preview and the server's behavior. That is the whole argument for discovery over configuration:

> **Never ask the customer for their settings — ask their server.** A customer describing their own deployment is a second source of truth, and it is the one that goes stale.

Cache the profile per deployment, and re-discover when a connection is re-established rather than per request. A server's version and changed settings do not move often, but they do move, and a cached profile from before an upgrade is a preview of a server that no longer exists.

## Next

- [`settings.md`](settings.md) — what the discovered profile actually does once you declare it.
- [`multi-version.md`](multi-version.md) — one process, several discovered deployments, several artifacts.
- [`quickstart.md`](../quickstart.md) — the version above was hard-coded; this is how it should have been found.
