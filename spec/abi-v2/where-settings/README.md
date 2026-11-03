# The WHERE-affecting settings lists

For each supported ClickHouse line, a list of the settings that can change which rows a `SELECT ... WHERE` returns. They are generated per line from the ClickHouse source at the tag that line is pinned to, by the artifact producer, and copied here byte for byte. They are data, outside `spec/abi-v2/abi.json`, so they never move `CHS_ABI_FINGERPRINT`.

A consumer that streams rows (instead of asking the server to run the SELECT) needs to know when a server setting could make the streamed answer differ from the server's. These lists, together with the rule below, are that knowledge.

## Files

| file                                                             | what it is                                                                                                                                                                       |
| ---------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `26.3.json`, `26.7.json`, `26.8.json`, `26.9.json`, `26.10.json` | one list per line: `schema`, `line`, `tag`, `version`, `generator`, and `settings` sorted by `name`. `26.7.json` is kept for a line that is still served but no longer supported |
| `union.json`                                                     | every name that is in any line: `name`, `tier`, `kind`, `scopes` (the union over the lines) and `tags` (the pinned tags that list it)                                            |

`documented-pathological.tsv` is a different kind of file: the settings values a server refuses outright (see "Settings the server may refuse outright" below).

Each entry of a per-line list carries `name`, `kind`, `tier`, `reason`, `scopes` (where in the server the setting is read) and `sources` (upstream `src/...:line function` locations). `kind` follows from `tier`: `result` for the two `result-*` tiers, `where` for the rest. There is no `refuse` field: the lists are not the refuse key (see the rule below).

## The tiers

- `predicate`: measured to change a verdict. Example: `aggregate_functions_null_for_empty`.
- `predicate-unflipped`: read in the WHERE's scope, but no value the flip test tried changed a verdict. Kept, as the safe default.
- `result-content`: selects rows by content. Examples: `additional_table_filters`, `additional_result_filter`, `apply_deleted_mask` and `final`.
- `result-truncate`: truncates or pages the result by position or count. Examples: `limit`, `offset` and an overflow mode of `break`.
- `execution`: resources and limits only, each with a cited reason.
- `output`: rendering only. One entry, `extremes`.

Output-format settings that reach a WHERE (through `formatRow`, `toJSONString` or a CAST to String) are `predicate` (or `predicate-unflipped`), not `output`.

## The consumer rule, for v2.0 and later

This is part of the v2 contract.

A consumer passes the server's changed settings in the server profile (`settings`, see `input:server_profile` in `spec/abi-v2/docs.md`). Then it must **refuse a tenant if `filter_declined_settings` is non-empty**. That is the whole rule: the list already carries every changed `result-content` setting, honored or not, and every cell of `documented-pathological.tsv` the schema's layers set (at tier `server-refuses`), so a consumer reads neither these lists nor that file to decide.

`filter_declined_settings` is the top-level member of `chs_schema_describe` on the tenant's schema, one check at bind time. It lists every setting that this build's filters do not honor in a WHERE, over every layer known when the schema was compiled: the `chs_set_defaults` snapshot, the server profile and the schema's own settings. Each entry carries its tier from these lists (or `server-refuses`, for a pathological cell) and its layer. On such a schema, `chs_filter_create` declines (`CHS_DECLINED`): this is the backstop for a consumer that did not read the list. A filter's own settings are not in it; a declined one is named at evaluation, with every verdict `d`. An evaluation's settings govern parsing the body only: they never reach the WHERE, so they are never named as WHERE settings, and a filter evaluated in the INSERT's own session is compiled with that session's WHERE-tier settings in the filter's own settings.

A `result-truncate` setting never makes a consumer refuse: it is outside a row verdict's scope, since a stream can deliver rows that a paged SELECT would cut off.

**The lists are documentation of which settings matter. They are not the refuse key.** What the library declines is decided by the library, by name, and listed in `filter_declined_settings`.

## The method

The lists are an over-approximation by source scope, which is the safe direction: a setting is listed when the server reads it in a scope a WHERE can reach. The lists were then validated against real servers by a flip test (`measured`): each listed setting was set to other values and the WHERE's row verdicts compared. Of a random sample of unlisted settings, 0 changed a verdict (`measured`). The known positives are listed.

## The blind spot

About 63 to 81 settings per line are free strings or enums for which the flip test had no value it could try. They stay in their source-scope tier (`predicate-unflipped`) rather than being proved safe.

## Settings the server may refuse outright

Under some settings values a ClickHouse server refuses **every** filter query, so no row is ever returned, while the library answers the rows. A server configured with one of these values is outside the filter guarantee, and **the library says so**: for a cell set at the `chs_set_defaults`, server-profile or schema layer, `filter_declined_settings` lists it at tier `server-refuses`, so the consumer rule above refuses it with no lookup here. A filter's own settings are not in that list, and need no lookup either: a cell set there declines `chs_filter_create` (`CHS_DECLINED`, reason `setting-server-refuses`).

The exact list is the data file `documented-pathological.tsv`, copied byte for byte from the artifact producer. A leading block of `#` comment lines explains it, the last comment line names the columns, and each following row is one exact cell, with these tab-separated columns:

| column    | meaning                                                                                                                                      |
| --------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| `setting` | the setting's name                                                                                                                           |
| `value`   | the exact value, as the filter, the schema or the server profile sets it                                                                     |
| `line`    | the ClickHouse line (`major.minor`)                                                                                                          |
| `layer`   | where the value is set: `profile` (the server profile), `schema` (the schema's settings) or `filter` (the filter's own settings)             |
| `server`  | the server's answer, `e:<code>`                                                                                                              |
| `rule`    | why the server refuses: `refuses-every-query` (a pathological configuration) or `refuses-by-query-size` (a size limit on the resolved query) |
| `ruling`  | who signed the row off, and when                                                                                                             |

Two examples from the data: `max_expanded_ast_elements=1` is refused with server code 36 on every supported line, and `page=1` is refused with code 36 on the 26.8, 26.9 and 26.10 lines.

The producer's differential gate counts these cells under their own heading and does not block on them. Nothing else is excused: the list grows only by sign-off, and a listed cell that stops failing must have its row removed. A value not on the list is one no measurement has found the server refusing; the producer's gate is what finds a new cell, and a consumer never extends the list itself.

## Not covered

- Server configuration that is not a setting: the server's timezone, row policies, quotas and users.
- The insert side. `input_format_*` settings change what is stored, not what a WHERE answers.

## Provenance and changes

The six JSON files were produced by the artifact producer at its commit `74bba4fb47c46eb6578796308d3f2e6e1cc6c559`, generator `chtypes where-settings generator`, from the tags `v26.3.60.2-lts`, `v26.7.19.5-stable`, `v26.8.34.13-lts`, `v26.9.25.15-stable` and `v26.10.2.103-stable`. `documented-pathological.tsv` is the producer's file at the same commit (14 rows). Entries per line: 26.3 has 390, 26.7 has 446, 26.8 has 478, 26.9 has 481 and 26.10 has 484; the union has 509.

The sha256 of each file (the six lists and the TSV), computed by `scripts/abi-v2/check-where-settings.py --print-table` and checked by `--check`:

- `26.3.json`: `8f2af776b27dc0c6b3b3b2226fc76e7615805c7a1de07c9b3bcd01473b940c6d`
- `26.7.json`: `c2ebc38baaf42e8ad300cf41c99d9735dac075231e288578cfda90bc634d45b9`
- `26.8.json`: `458cd177a80457e1332f6ae771ed2874a073816c0f0467d79d99f3a305ad0ce1`
- `26.9.json`: `ef538a3655cfa8190766008bf56d4f50b16b74280638b3124cddd6c70b15f8de`
- `26.10.json`: `81fafa54ff63ae3faf7571cb4a91d269078dd66b4338f42553f8f7ff835e7e2d`
- `union.json`: `6af331bb3ec8f5689f6c1082dfdd8b6874139f3723bfc87607059bc5ef88e315`
- `documented-pathological.tsv`: `c6dfbcb1512f08d3db1241e87a64b0988dbe4f29ab4a7788c2701c6768becd6f`

The producer regenerates the lists per pinned tag. A pin that adds or drops a name is a new copy here, with this table updated in the same change.
