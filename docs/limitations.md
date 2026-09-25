# Known limitations

Every item here is a deliberate decline or a stated boundary, not a bug queue. The rule underneath all of them is the same one: **chtypes refuses to guess.** An answer it is not certain of is worth less than an honest `unsupported`, because a wrong answer about types is the failure the product exists to prevent.

When you get one, fall back to the server: validate cautiously, forward the row unpreviewed, and never tell a user they are wrong on the strength of a decline.

## macOS is a development floor, not an oracle

The darwin artifacts exist so you can develop and run the suites on a laptop. They are not an oracle.

macOS's `long double` is 53-bit, so some float parses diverge from a real server. Measured: on Linux the float corpus matches in full; on macOS none of it does. That is not a near miss to be tolerated — it is a systematic difference in a whole class of values.

**Float expectations must come from a Linux artifact or a live ClickHouse.** Everything else on a Mac is trustworthy for development.

## EPHEMERAL columns cannot be previewed through a format stream

An INSERT whose explicit column list names an EPHEMERAL column cannot be previewed here, because a format stream carries no column list. There is no way to tell, from the body alone, that the caller meant the ephemeral form.

Detect the intersection at compile time and decline it rather than mispreview:

|            |                                                          |
| ---------- | -------------------------------------------------------- |
| Go         | `schema.Columns`, `DefaultKind == chtypes.KindEphemeral` |
| Python     | `schema.columns`, `DefaultKind.EPHEMERAL`                |
| TypeScript | `schema.columns`, `defaultKind === 'EPHEMERAL'`          |
| Rust       | `schema.columns()`, `DefaultKind::Ephemeral`             |

A gateway that previews the intersection anyway shows a row that cannot exist.

## Declined by design

Each of these is `unsupported` — an `UnsupportedError`, `Error::Unsupported`, or the `unsupported` outcome. None of them is a rejection, and none is a defect.

- **Engines and sorting keys** beyond the modeled MergeTree family.
- **TTL forms** that are not a plain rows TTL: `WHERE` and `GROUP BY` TTLs, `TO DISK` and `TO VOLUME` moves, `RECOMPRESS`, and any clock-reading TTL expression.
- **MergeTree settings declared at a non-default value.** An unknown _name_ is the server's own 115, a rejection; a known name at a value this build does not model is a decline, never a silent ignore.
- **Server- and session-property DEFAULTs** — `hostName()`, `currentUser()` and the rest. Their value is a property of the server, and there is no server here.
- **Blocking DEFAULTs**, such as anything calling `sleep`.
- **A multi-row `Values` body into a deprecated `Object('json')` column that a server would commit in parts** (24.8–25.10). That happens only when the call's `max_insert_block_size` splits the body and `min_insert_block_size_rows` / `_bytes` keep the INSERT's squashing step from joining the pieces back. A server then writes several parts, each typed separately, and what the table reads back depends on the parts, not on the body.
- **DEFAULT expressions past the admission budgets** — 256 MiB and one second by default, both adjustable through the process-wide settings in [`guides/settings.md`](guides/settings.md).

## Constants are not payloads

Everything on the row path is **insert-side** coercion. Never reuse it to fold a `WHERE`-clause constant.

The rules genuinely differ: `256` into a `UInt8` column stores `0`, while `x = 256` over that column promotes and is false for every row. Refuse an operand outside the column type's domain instead — or use [`guides/filters.md`](guides/filters.md), which is the surface that answers comparison questions with ClickHouse's own comparison functions.

## Filters are shadow-only for now

No read-side security may be enforced on the filter surface until a release explicitly lifts this limitation — the CHANGELOG will say so, and until it does, assume it has not. Until then the surface is for shadow and replay: run it beside your existing enforcement and compare, do not replace.

The parse-once block twin does not change that — it is a performance shape, not a maturity signal, and sits under the same gate.

## Some formats depend on the artifact, not the binding

`RowBinary` and its family, `Native` and `Buffers` parse only on artifacts new enough to carry the reader. This is a property of the **loaded artifact's age**, not of your binding's version, so probe the artifact rather than assuming from the package version. Earlier artifacts answer 73 or 117 exactly as their own servers do.

`CSVWithNames` and `TSVWithNames` are text formats with the same property. They joined the format set inside ABI revision 5 without changing the revision number, so an artifact built before them can report revision 5 and still not know them. Probe for them the same way.

## The error model is normative

`unsupported` (`CODE_UNSUPPORTED`, the wire sentinel `-2`) is neither an acceptance nor a rejection, and treating it as either is the most expensive mistake available here.

Over-accepts and over-rejects have **no budget** in the differential proof the artifacts are built from: a row accepted here and rejected by the server ships before the insert fails, and a row rejected here and accepted by the server is silent data loss. Neither is acceptable, so neither gets an allowance. A decline is how the system stays honest about the cases it cannot reach that bar on.

**No budget is a rule about process, not a claim about state.** A non-zero count, in either direction, on any binding against any ClickHouse line, is refused unless a person has named that case and recorded why, with a tracking reference attached. Nothing non-zero passes quietly, and no threshold waves anything through.

**So it does not mean there are none.** What is true today is narrower, and worth stating exactly: **no case is currently known in which this library and a real server disagree about whether to accept a row.** Both directions are at zero across every line the artifacts are proved against — `measured` by the differential proof those artifacts are built from, not by this repository, and a state rather than a promise: it is what the rule has produced so far, not something the rule guarantees will hold tomorrow.

⚠️ **Zero in both directions is a statement about the verdict, not about the value or the error.** There are two other ways to disagree: both sides accept a row and **store different values**, or both refuse it and **report different codes**. Neither is an accept-or-reject disagreement, so the no-budget rule above does not cover them.

They are measured all the same, and as of the current published artifacts **both are also at zero on every line, for every binding**. That's `measured` by the same differential proof, not by this repository, and it's the same kind of statement as the one above: a state of what the proof covers, not a promise about every input. A case outside that coverage can still disagree, and one is known: it's listed under [Known divergences](#known-divergences). Getting there meant investigating the cases one at a time. Some were fixed in the library. Others turned out not to be something a caller can reach, because the disagreement was a property of how the comparison itself was run. Any that a caller **can** reach are listed under [Known divergences](#known-divergences) below.

In Python specifically, `UnsupportedError` is a **peer** of `SchemaError` rather than a subclass, so `except SchemaError` never catches a decline. Handle the two arms explicitly, or catch `ChtypesError` for both. The subtype was retired precisely because catching one and getting the other is a silent misclassification.

The ABI header is the full contract.

## An out-of-domain Enum DEFAULT answers differently by line

This is by design, because the server does too. On 24.8–25.10 such a schema compiles, and a row relying on the default is `accepted_poisoned`. On 26.x, compiling refuses it (`691`, or `70`). The poisoned row's code is the server's readback code, which also differs by line. The conformance suite compares that code on every line and binding, and finds no mismatch. The rule and the codes are in [`transformations.md`](guides/transformations.md#an-out-of-domain-enum-default-follows-the-server).

## Known divergences

Each entry here is a case where this library and a real ClickHouse server give different answers, and a caller can reach it. Every entry names the direction, the input, both answers, and **which half of the comparison was measured where** — there is no ClickHouse server in this repository, so the server half always comes from the differential proof the artifacts are built from.

An entry disappears when an artifact stops diverging, or when the disagreement turns out to have been a property of how it was measured rather than of the library. Pin nothing to this list.

⚠️ **"A real server" means a real table engine** — a MergeTree table, the kind a tenant writes to. A `CREATE TEMPORARY TABLE` is `ENGINE=Memory`, has no parts, and accepts values that every ordinary table refuses at part-write time. If you reproduce an entry against a temporary table you will not get the answer recorded here, and the temporary table is the one that is wrong about your production write.

Every entry below has a machine-checkable twin in [`docs/divergences.json`](divergences.json): `scripts/check-divergences.py` drives each one against loaded artifacts and, non-blocking in CI (the same volume as `scripts/support-matrix.sh`, for the same reason — see `.github/workflows/ci.yml`'s `docs` job), flags an entry the artifacts no longer support. A red there means "update this page", not "the library regressed" — read the check's own message before assuming either.

### A CHECK-failing row before a malformed row, with no error budget

**Same verdict, different code.** Both sides refuse the body, so nothing is over-accepted and nothing is lost, but the code a caller receives isn't the server's.

The input is a text-format body (`JSONEachRow` or `CSV` measured) into a table with a `CHECK` constraint, where a row that violates the constraint comes **before** a row that doesn't parse, sent with no error budget (`input_format_allow_errors_num` 0, the default). For example, with `k UInt8, s String, CONSTRAINT c CHECK k < 5`, this `JSONEachRow` body:

```text
{"k":9,"s":"a"}
{"k":"x","s":"b"}
{"k":1,"s":"c"}
```

|               |                                                                             |
| ------------- | --------------------------------------------------------------------------- |
| this library  | rejected, code **469** (`VIOLATED_CONSTRAINT`), at the first row            |
| a real server | rejected, code **27** (`CANNOT_PARSE_INPUT_ASSERTION_FAILED`), at the parse |

A server reads the whole body before it checks constraints, so the later parse error wins. This library checks each row as it goes and stops at the first `CHECK` failure. It happens on every published line. Both sides answer the same when the error budget is non-zero (both **469**), when the malformed row comes first (both **27**), and for a `Values` body (both **6**, for the whole body).

**Measured**: this library's answer, in this repository, against the published artifacts on 25.10, 26.3 and 26.9. The artifact producer measured it on 24.8 too. The server's answer, against pinned servers on 24.8, 25.10 and 26.3, by the artifact producer, not measured here.

Branch on the outcome, not the code. If a rejected body's code is **469**, don't take it as proof that the rest of the body parses.

## Pre-1.0

What is already frozen before 1.0, and how an artifact and the SDK opening it are matched, is in [`support.md`](support.md#pre-10).

Anything else may still move before 1.0. Each binding's own CHANGELOG carries its list.
