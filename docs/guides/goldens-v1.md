# The v1 goldens — one document per release, one comparator, four thin runners

> **Status: live.** Every binding's runner has landed; `.github/workflows/v1-goldens.yml` fails a leg whose runner is missing rather than skipping it.

A goldens document is what the release's own library returned for a fixed set of inputs, published by the artifact producer once per release (one exact ClickHouse version, one build) and attached unchanged as an OCI referrer of each platform manifest (artifactType `application/vnd.wavehouse.chtypes.goldens.v1`). It proves one thing: that a binding **delivers the library's documents faithfully**. Whether the library matches a ClickHouse server is the producer's rigs' job, not this page's.

The contract has four parts, all in this repository:

| part                                                          | where                                                                            |
| ------------------------------------------------------------- | -------------------------------------------------------------------------------- |
| the document's schema, copied byte for byte from the producer | [`spec/goldens/v1/schema.json`](../../spec/goldens/v1/schema.json)               |
| the runner report's schema, this repository's                 | [`spec/goldens/v1/report.schema.json`](../../spec/goldens/v1/report.schema.json) |
| the comparator, normative                                     | [`scripts/goldens-v1/compare.py`](../../scripts/goldens-v1/compare.py)           |
| the dispatch-only CI job                                      | [`.github/workflows/v1-goldens.yml`](../../.github/workflows/v1-goldens.yml)     |

## 1. What a document is

- **One document per release.** It carries the exact `clickhouse_version` and `build`, plus a `revision` that is also in the signed predicate. A corrected set for the same build is a new referrer with a higher revision, and a reader uses the highest verified revision.
- **`setups[]`.** Each setup is `{id, image_zone_b64, defaults_b64, cases[]}` and is run in **one process**: `chs_initialize(image_zone)`, then `chs_set_defaults(defaults)`, once, before its first case. A per-call zone is the `session_timezone` key of a case's settings.
- **A case** has an `id` (stable across revisions, lines and platforms), a `kind` (`schema`, `row`, `batch`, `filter` or `wire`), a `call` naming the call sequence (`schema_create`, `preview_row`, `preview_batch` or `filter_eval_body`), and an explicit `platforms` list, with `platforms_reason` when it is narrower than the document's `generated.checked_on`. Every input is base64.
- **`expect`** is `{at, status, run_varying[]}` plus either an `error` (`ch_code` and base64 name, message and column), or exactly one of `document` (the whole output document) or `compare` (narrow RFC 6901 pointer checks), plus `export_b64` for a batch export.

## 2. The runner contract

A runner is a test in the binding's own tree. It does five things and nothing else:

1. **Fetch.** It fetches the release with the binding's own fetch layer (`ensure`) and the goldens document with `fetch_signed(…, goldens)` ([`fetch-v1.md`](fetch-v1.md) §9), so the goldens ride the same trust path as the library. It writes the document's exact bytes to the path in `CHTYPES_GOLDENS_DOCUMENT`.
2. **One process per setup.** A setup's `chs_initialize` and `chs_set_defaults` happen once, in a process of their own, before that setup's first case. The runner may spawn a child process per setup; it may not share one across setups.
3. **Execute and record, never compare.** For each case it runs the call sequence the case's `call` names and records the step the sequence stopped at (`at`: the first step that did not answer `CHS_OK`, else `call`), that step's `status`, and the `error` fields as base64 when the status is not `CHS_OK`. It writes no verdicts and no expected values.
4. **Never re-serialize the output.** `document_b64` is the exact byte string the library returned (for `schema_create`, `chs_schema_describe`'s), and `export_b64` the exact export bytes. A document that was parsed and written back out is a different document: the comparator parses the bytes itself.
5. **Run the public decoder.** For an OK case the runner hands the document to the binding's PUBLIC decoder, the call a user makes, and records `decoded_ok`: `true` if it was accepted without error. A decoder that throws on a document the library emitted is a binding defect, and the comparator fails the case.

The runner reads its inputs from the environment and writes two files:

| variable                        | meaning                                                                                            |
| ------------------------------- | -------------------------------------------------------------------------------------------------- |
| `CHTYPES_GOLDENS_REGISTRY_BASE` | the registry base to fetch from, repository path included                                          |
| `CHTYPES_GOLDENS_VERSION`       | the exact four-part version                                                                        |
| `CHTYPES_GOLDENS_PLATFORM`      | `linux-amd64` or `linux-arm64`                                                                     |
| `CHTYPES_GOLDENS_REPORT`        | where to write the report (JSON, [`report.schema.json`](../../spec/goldens/v1/report.schema.json)) |
| `CHTYPES_GOLDENS_DOCUMENT`      | where to write the goldens document's exact bytes                                                  |

The report carries the binding, its version, the platform (as `os/arch`, for example `linux/amd64`), the document's identity (`clickhouse_version`, `build`, `revision`, and the `sha256` of the bytes written to `CHTYPES_GOLDENS_DOCUMENT`), and the loaded artifact's `build_info_b64` and `abi_fingerprint`, read from the library itself. Every byte field in it is standard base64.

## 3. The skip rule

A case runs on every platform its `platforms` lists. A runner on a platform the case excludes records the case as `skipped` with a `skip_reason`, and the comparator reports it as skipped by platform, never as passed. **A skip is allowed only for a platform the case excludes.** Skipping a case the platform allows is a failure, so is running a case the platform excludes (its expectation is unproven there), and so is leaving a case out of the report altogether: a missing case is a FAIL, never a skip.

## 4. The comparison

[`compare.py`](../../scripts/goldens-v1/compare.py) holds the producer's rules unchanged. Comparison is on **decoded trees**, never on JSON text: every string becomes its UTF-8 bytes, an `F_b64` member becomes member `F` holding the decoded bytes, `value_b64` is decoded, every `run_varying` member is dropped from both sides, and what is left must be deeply equal, with array order significant and types strict (`1` is not `true`). A pointer may select an array element by name (`@<base64>`) or fan out (`*`). On a status other than `CHS_OK`, `run_varying` pointers address members of the `error` as `/error/<member>` (for example a message that quotes a per-run temporary name), and the comparator compares the rest of the error as a decoded tree. On top of that it applies the report-level rules in its header: `at`, `status` and the error's bytes must match; the export bytes must match; `decoded_ok` must be `true` for an OK case; and the report must name the document's own `clickhouse_version`, `build`, `revision`, document `sha256` and `abi_fingerprint`.

The whole run fails if any case fails, if a report ran zero cases, if a required binding has no report, or if two reports name one binding.

## 5. Running it locally

```sh
uv run --no-project --python 3.11 --with 'jsonschema==4.23.0' scripts/goldens-v1/compare.py --selftest --schema-validate
scripts/goldens-v1/compare.py --goldens tests/fixtures/goldens-v1/synthetic-goldens.json --report tests/fixtures/goldens-v1/synthetic-report.json
scripts/goldens-v1/compare.py --goldens G --report R1 --report R2 --kinds wire --require-bindings go,python,ts,rust
```

`--kinds` judges only the listed kinds. `--schema-validate` additionally validates the document and every report against their schemas and needs `jsonschema`; the selftest runs its schema cases only with that flag and says so when it does not. The output is one line per failure or skip, then a binding by kind table, then the verdict. The fixtures under `tests/fixtures/goldens-v1/` are **synthetic**: their values are invented for the selftest and are not a release's goldens.

## 6. The CI job

`v1-goldens` is dispatch-only (inputs `registry_base`, `version`, `platform`). Each of its four legs installs one binding and runs that binding's runner; a leg whose runner is missing fails by name. The verdict job downloads the four reports, validates the document against the schema, runs the comparator with `--require-bindings go,python,ts,rust`, and writes the table to the job summary.
