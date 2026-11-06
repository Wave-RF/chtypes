# The WHERE-affecting settings lists

For each supported ClickHouse line, a list of the settings that can change which rows a `SELECT ... WHERE` returns. They are generated per line from the ClickHouse source at the tag that line is pinned to, by the artifact producer, and copied here byte for byte by `scripts/abi-v2/regen-where-settings.py`, never by hand. They are data, outside `spec/abi-v2/abi.json`, so they never move `CHS_ABI_FINGERPRINT`.

A consumer that streams rows (instead of asking the server to run the SELECT) needs to know when a server setting could make the streamed answer differ from the server's. These lists, together with the rule below, are that knowledge.

## Files

| file                                                | what it is                                                                                                                                                                                                                                                                                           |
| --------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `lines.json`                                        | the supported lines: `schema`, `source` (the channel document it was taken from) and `lines`, in numeric order (`26.3` before `26.10`). A list with no line here, or a line here with no list, fails `scripts/abi-v2/check-where-settings.py`                                                        |
| `26.3.json`, `26.8.json`, `26.9.json`, `26.10.json` | one list per line: `schema`, `line`, `tag`, `version`, `generator`, and `settings` sorted by `name`                                                                                                                                                                                                  |
| `union.json`                                        | every name that is in any line: `name`, `tier`, `kind`, `scopes` (the union over the lines) and `tags` (the pinned tags that list it, in `lines.json`'s order). It is derived from the lists: `regen-where-settings.py --control` proves the derivation reproduces the producer's own, byte for byte |

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

<!-- remove-at-v2-lock -->

**Dev-only note.** v2-dev builds before the first one at fingerprint `sha256:d60a681ea0e249ab9e2c83b50640ad9cef040261ddf61805106c93e2c68774e4` write no top-level `filter_declined_settings`. From fingerprint `sha256:28c7445ce6b5c6d0ae33177fd60eb7b3b5654a966c842b8d812aea622824dc72` they list the server profile's declined settings only, under `server.filter_declined_settings`, and name a declined setting from the defaults or the schema's own settings only at evaluation; before it they list none. A dev SDK at `sha256:d60a681e…` or later refuses every one of them (rule r6). A consumer testing those builds treats a setting at any layer whose `tier` is `predicate`, `predicate-unflipped` or `result-content` as a reason to refuse.

<!-- /remove-at-v2-lock -->

## The method

The lists are an over-approximation by source scope, which is the safe direction: a setting is listed when the server reads it in a scope a WHERE can reach. The lists were then validated against real servers by a flip test (`measured`): each listed setting was set to other values and the WHERE's row verdicts compared. Of a random sample of unlisted settings, 0 changed a verdict (`measured`). The known positives are listed.

## The blind spot

About 63 to 81 settings per line are free strings or enums for which the flip test had no value it could try. They stay in their source-scope tier (`predicate-unflipped`) rather than being proved safe.

## Settings the server may refuse outright

Under some settings values a ClickHouse server refuses **every** filter query, so no row is ever returned, while the library answers the rows. A server configured with one of these values is outside the filter guarantee, and **the library says so**: for a cell set at the `chs_set_defaults`, server-profile or schema layer, `filter_declined_settings` lists it at tier `server-refuses`, so the consumer rule above refuses it with no lookup here. A filter's own settings are not in that list, and need no lookup either: a cell set there declines `chs_filter_create` (`CHS_DECLINED`, reason `setting-server-refuses`).

The exact list is the data file `documented-pathological.tsv`, the artifact producer's bytes without any row on a line `lines.json` does not name. A leading block of `#` comment lines explains it, the last comment line names the columns, and each following row is one exact cell, with these tab-separated columns:

| column    | meaning                                                                                                                                      |
| --------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| `setting` | the setting's name                                                                                                                           |
| `value`   | the exact value, as the filter, the schema or the server profile sets it                                                                     |
| `line`    | the ClickHouse line (`major.minor`)                                                                                                          |
| `layer`   | where the value is set: `profile` (the server profile), `schema` (the schema's settings) or `filter` (the filter's own settings)             |
| `server`  | the server's answer, `e:<code>`                                                                                                              |
| `rule`    | why the server refuses: `refuses-every-query` (a pathological configuration) or `refuses-by-query-size` (a size limit on the resolved query) |
| `ruling`  | who signed the row off, and when                                                                                                             |

Two examples from the data: `max_expanded_ast_elements=1` in a filter's own settings is refused with server code 36 on every supported line, and `page=1` is refused with code 36 on the 26.8, 26.9 and 26.10 lines.

The producer's differential gate counts these cells under their own heading and does not block on them. Nothing else is excused: the list grows only by sign-off, and a listed cell that stops failing must have its row removed. A value not on the list is one no measurement has found the server refusing; the producer's gate is what finds a new cell, and a consumer never extends the list itself.

## Not covered

- Server configuration that is not a setting: the server's timezone, row policies, quotas and users.
- The insert side. `input_format_*` settings change what is stored, not what a WHERE answers.

## Provenance and changes

The four lists were produced by the artifact producer at its commit `a448fae69e2cd79c1b359eb430e8d0b45303a4c9`, generator `chtypes where-settings generator`. `documented-pathological.tsv` is the producer's file at its commit `9edf2d6222516e5be34fa2147e07e3726a98a186` without its row on the retired line 26.7 (13 rows). `lines.json` is from the producer's dry-run channel document until the first publication. Entries per line: 26.3 has 390, 26.8 has 478, 26.9 has 481 and 26.10 has 484; the union has 508.

The sha256 of each data file (the four lists, the union, the TSV and `lines.json`), computed by `scripts/abi-v2/check-where-settings.py --print-table` and checked by `--check`:

- `26.3.json`: `8f2af776b27dc0c6b3b3b2226fc76e7615805c7a1de07c9b3bcd01473b940c6d`
- `26.8.json`: `458cd177a80457e1332f6ae771ed2874a073816c0f0467d79d99f3a305ad0ce1`
- `26.9.json`: `ef538a3655cfa8190766008bf56d4f50b16b74280638b3124cddd6c70b15f8de`
- `26.10.json`: `81fafa54ff63ae3faf7571cb4a91d269078dd66b4338f42553f8f7ff835e7e2d`
- `union.json`: `c97450f8688ad92425537815885bd70ab7d1b8316533911766bd445db4b62b7d`
- `documented-pathological.tsv`: `be603f3553f39895369b8a3dd096f668004e3c3dc7a402b5b6e1c82d3a4f854b`
- `lines.json`: `63a7ca2d02c33ad7e664c2f318026b37af29bdf9c9c84a2112361ff2b8ae17d8`

The producer regenerates the lists per pinned tag. A pin that adds or drops a name, or a line added or retired, is a new run of `regen-where-settings.py`, which updates this table in the same change.
