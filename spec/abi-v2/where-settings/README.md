# The WHERE-affecting settings lists

For each supported ClickHouse line, a list of the settings that can change which rows a `SELECT ... WHERE` returns. They are generated per line from the ClickHouse source at the tag that line is pinned to, by the artifact producer, and copied here byte for byte. They are data, outside `spec/abi-v2/abi.json`, so they never move `CHS_ABI_FINGERPRINT`.

A consumer that streams rows (instead of asking the server to run the SELECT) needs to know when a server setting could make the streamed answer differ from the server's. These lists, together with the rule below, are that knowledge.

## Files

| file                                               | what it is                                                                                                                            |
| -------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| `26.3.json`, `26.7.json`, `26.8.json`, `26.9.json` | one list per line: `schema`, `line`, `tag`, `version`, `generator`, and `settings` sorted by `name`                                   |
| `union.json`                                       | every name that is in any line: `name`, `tier`, `kind`, `scopes` (the union over the lines) and `tags` (the pinned tags that list it) |

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

A consumer passes the server's changed settings in the server profile (`settings`, see `input:server_profile` in `spec/abi-v2/docs.md`). Then it must **refuse a tenant if `filter_declined_settings` is non-empty, or a `result-content` setting is changed**.

`filter_declined_settings` is the top-level member of `chs_schema_describe` on the tenant's schema, one check at bind time. It lists every setting that this build's filters do not honor in a WHERE, over every layer known when the schema was compiled: the `chs_set_defaults` snapshot, the server profile and the schema's own settings. Each entry carries its tier from these lists and its layer. On such a schema, `chs_filter_create` declines (`CHS_DECLINED`): this is the backstop for a consumer that did not read the list. A filter's own settings and an evaluation's are not in it; a declined one is named at evaluation, with every verdict `d`.

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

Under some settings values a ClickHouse server refuses **every** filter query, so no row is ever returned, while the library answers the rows. A server configured with one of these values is outside the filter guarantee: **a consumer refuses such a profile or call.** Neither the library nor the filter's row verdicts will tell you the server would have refused.

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

Two examples from the data: `max_expanded_ast_elements=1` is refused with server code 36 on every supported line, and `page=1` is refused with code 36 on the 26.8 and 26.9 lines.

The producer's differential gate counts these cells under their own heading and does not block on them. Nothing else is excused: the list grows only by sign-off, and a listed cell that stops failing must have its row removed. Treat a value that is not on the list as unknown, not as safe.

## Not covered

- Server configuration that is not a setting: the server's timezone, row policies, quotas and users.
- The insert side. `input_format_*` settings change what is stored, not what a WHERE answers.

## Provenance and changes

The five JSON files were produced by the artifact producer at its commit `495f7a702460561611b93837762e1e24f82cf705`, generator `chtypes where-settings generator`. `documented-pathological.tsv` is the producer's file at its commit `8f629c5e5e8682feed490c90d79956e231915e26` (14 rows). Entries per line: 26.3 has 390, 26.7 has 446, 26.8 has 477 and 26.9 has 480; the union has 495.

The sha256 of each file (the five lists and the TSV), computed by `scripts/abi-v2/check-where-settings.py --print-table` and checked by `--check`:

- `26.3.json`: `d049ab91129549b9283cd399018f7931e697c920627ca60eac35a7f52546934c`
- `26.7.json`: `2c01a341755d23602a4f7cc2ae318ea6301fbf0b61ec65dddb9d818d00e471d6`
- `26.8.json`: `290d6da61bdba5398387f09018668251c37e2b63d617dfd397363f88b1e8c0b8`
- `26.9.json`: `db6e64b7f9aca7ce3c5e7479f665fdb08dda0e721700a879447a3b39e4e8af1e`
- `union.json`: `34e8f06e9582046384fbf05c95018250e7a8d9506c1fd48969e5e38248e2f992`
- `documented-pathological.tsv`: `51feb8bb5ae41297b6f5a597d8b59551e83ffaff9efd31854a2717d9997a0c47`

The producer regenerates the lists per pinned tag. A pin that adds or drops a name is a new copy here, with this table updated in the same change.
