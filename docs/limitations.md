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

**So it does not mean there are none.** Known cases exist in both directions today, and each is registered by name rather than absorbed into an allowance. The register records that a case is known and bounded; it is not an explanation of it, and it does not yet name the inputs involved. What the guarantee gives you is therefore narrower than it reads, and still worth having: a disagreement between this library and a real server is either absent or on the record by name.

Those cases have now been investigated one at a time, and most of them turned out not to be something a caller can reach — in some the answer this library gives is the correct one, and the disagreement was a property of how the comparison itself was run. The ones a caller **can** reach are listed under [Known divergences](#known-divergences) below.

In Python specifically, `UnsupportedError` is a **peer** of `SchemaError` rather than a subclass, so `except SchemaError` never catches a decline. Handle the two arms explicitly, or catch `ChtypesError` for both. The subtype was retired precisely because catching one and getting the other is a silent misclassification.

The ABI header is the full contract.

## Known divergences

Each entry here is a case where this library and a real ClickHouse server give different answers, and a caller can reach it. Every entry names the direction, the input, both answers, and **which half of the comparison was measured where** — there is no ClickHouse server in this repository, so the server half always comes from the differential proof the artifacts are built from.

An entry disappears when an artifact stops diverging. Pin nothing to this list.

### A trailing `-- comment` in a Values body, on 26.2 and 26.3

**Over-accept.** This library answers `accepted` for a row a real server refuses, so a gateway using it as a pre-flight check forwards the row and the insert then fails.

The input is a `Values` body whose row is followed by a SQL line comment — `(42) -- trailing comment` — into a numeric, `Date` or `DateTime` column, on ClickHouse **26.2** or **26.3**:

|               |                                                               |
| ------------- | ------------------------------------------------------------- |
| this library  | `accepted`                                                    |
| a real server | rejected, code **27** (`CANNOT_PARSE_INPUT_ASSERTION_FAILED`) |

Both sides agree on every other published line: the server refuses the comment on 25.10 and below, where this library refuses it too, and accepts it from 26.4, where this library accepts it too. A `String` column answers `unsupported` rather than accepting, and a `UUID` column refuses on every line — neither is affected.

**Measured**: this library's answer, in this repository, against the published artifacts. The server's answer, against pinned servers, by the differential proof the artifacts are built from — not measured here.

Remove the trailing comment from the body if you need a pre-flight answer you can rely on for those two lines.

## Pre-1.0

What is already frozen before 1.0, and how an artifact and the SDK opening it are matched, is in [`support.md`](support.md#pre-10).

Anything else may still move before 1.0. Each binding's own CHANGELOG carries its list.
