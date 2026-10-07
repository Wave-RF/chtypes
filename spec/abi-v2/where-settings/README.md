# The WHERE-affecting settings lists

For each supported ClickHouse line, a list of the settings that can change which rows a `SELECT ... WHERE` returns. They are generated per line from the ClickHouse source at the tag that line is pinned to, by the artifact producer, and copied here byte for byte. They are data, outside `spec/abi-v2/abi.json`, so they never move `CHS_ABI_FINGERPRINT`.

A consumer that streams rows (instead of asking the server to run the SELECT) needs to know when a server setting could make the streamed answer differ from the server's. These lists, together with the rule below, are that knowledge.

## Files

| file                                               | what it is                                                                                                                            |
| -------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| `26.3.json`, `26.7.json`, `26.8.json`, `26.9.json` | one list per line: `schema`, `line`, `tag`, `version`, `generator`, and `settings` sorted by `name`                                   |
| `union.json`                                       | every name that is in any line: `name`, `tier`, `kind`, `scopes` (the union over the lines) and `tags` (the pinned tags that list it) |

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

A consumer passes the server's changed settings in the server profile (`settings`, see `input:server_profile` in `spec/abi-v2/docs.md`). It refuses to stream when:

1. any of those settings is one the library DECLINES BY NAME, or
2. any `result-content` setting is set to a non-default value.

A `result-truncate` setting never makes a consumer refuse: it is outside a row verdict's scope, since a stream can deliver rows that a paged SELECT would cut off.

**The lists are documentation of which settings matter. They are not the refuse key.** What the library declines is decided by the library, by name.

<!-- remove-at-v2-lock -->

**Dev-only note.** v2-dev builds that come BEFORE the build that declines by name (its release notice names it) do not decline by name. A consumer testing those builds treats a setting whose `tier` is `predicate`, `predicate-unflipped` or `result-content` as a reason to refuse.

<!-- /remove-at-v2-lock -->

## The method

The lists are an over-approximation by source scope, which is the safe direction: a setting is listed when the server reads it in a scope a WHERE can reach. The lists were then validated against real servers by a flip test (`measured`): each listed setting was set to other values and the WHERE's row verdicts compared. Of a random sample of unlisted settings, 0 changed a verdict (`measured`). The known positives are listed.

## The blind spot

About 63 to 81 settings per line are free strings or enums for which the flip test had no value it could try. They stay in their source-scope tier (`predicate-unflipped`) rather than being proved safe.

## Not covered

- Server configuration that is not a setting: the server's timezone, row policies, quotas and users.
- The insert side. `input_format_*` settings change what is stored, not what a WHERE answers.

## Provenance and changes

Produced by the artifact producer at its commit `495f7a702460561611b93837762e1e24f82cf705`, generator `chtypes where-settings generator`. Entries per line: 26.3 has 390, 26.7 has 446, 26.8 has 477 and 26.9 has 480; the union has 495.

The sha256 of each file, computed by `scripts/abi-v2/check-where-settings.py --print-table` and checked by `--check`:

- `26.3.json`: `d049ab91129549b9283cd399018f7931e697c920627ca60eac35a7f52546934c`
- `26.7.json`: `2c01a341755d23602a4f7cc2ae318ea6301fbf0b61ec65dddb9d818d00e471d6`
- `26.8.json`: `290d6da61bdba5398387f09018668251c37e2b63d617dfd397363f88b1e8c0b8`
- `26.9.json`: `db6e64b7f9aca7ce3c5e7479f665fdb08dda0e721700a879447a3b39e4e8af1e`
- `union.json`: `34e8f06e9582046384fbf05c95018250e7a8d9506c1fd48969e5e38248e2f992`

The producer regenerates the lists per pinned tag. A pin that adds or drops a name is a new copy here, with this table updated in the same change.
