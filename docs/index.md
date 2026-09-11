# chtypes

**If this row were inserted into this table on this ClickHouse version, what would happen?**

chtypes answers that question with ClickHouse's own code. Real C++ — `DataTypeFactory`, `ISerialization`, `ReadHelpers`, `evaluateMissingDefaults`, the MergeTree insert-time merge — is compiled per ClickHouse release into a native library and reached from Go, Python, TypeScript and Rust through a small frozen C ABI. Nothing semantic is reimplemented, so _"what does ClickHouse do with `256` into a `UInt8`?"_ is answered by ClickHouse, not by a model of it.

```text
row + schema + ClickHouse version  ─▶  accepted / rejected / unsupported
                                       the stored value, byte for byte
                                       every silent change, named
```

The library never connects to ClickHouse. It is a local, offline answer about what a server would do.

## Two things, always

Using chtypes means installing two things, and keeping them straight is most of what there is to learn.

|                  | what it is                                                | how big    | where it comes from                  |
| ---------------- | --------------------------------------------------------- | ---------- | ------------------------------------ |
| **the binding**  | your language's package: types, a loader, a fetch command | small      | your package manager                 |
| **the artifact** | one ClickHouse release, compiled                          | 160–300 MB | downloaded and verified, per version |

The binding contains no ClickHouse code. It `dlopen`s an artifact at runtime and speaks the ABI, which is why one process can hold several ClickHouse versions at once, each answering with its own semantics. It is also why a fresh install answers nothing until you fetch an artifact — and why the error you get when you have not is a deliberately loud one that names the command to fix it.

[`install.md`](install.md) does both steps in all four languages. [`quickstart.md`](quickstart.md) is the first program.

## Three outcomes, and conflating any two is a bug

Every answer chtypes gives is one of three things. This distinction is the product; a wrapper that flattens it into a boolean has thrown away the reason to use it.

- **Rejected** — the server itself would refuse this row or this DDL, and the answer carries ClickHouse's own error code and message. Not a guess: the vendored code refused it.
- **Unsupported** — _this build declines to answer_. A real server might well have accepted it. The caller must fall back to the server: validate cautiously, forward unpreviewed, and never tell a user they are wrong on the strength of a decline.
- **Accepted** — the insert would succeed, possibly after silent coercions, which is the interesting case.

A bad **row** is a verdict, not an exception. Exceptions (or the `Err` arm) are for the machinery — a missing artifact, an unreadable document — and for schema-level answers like a DDL the server refuses.

## `Transformed` is the product

ClickHouse never says _"I silently changed your value."_ It accepts the row and stores something else. `256` into a `UInt8` is stored as `0`, and the INSERT succeeds.

chtypes derives that report and it is not optional: every accepted row carries a list of the changes made to it, each named — `overflow_wrap`, `date_clamp`, `poisoned`, `ttl_expired`, and the rest. It is the one answer in the whole system that is _computed_ rather than merely relayed, and it is why the product exists. [`guides/transformations.md`](guides/transformations.md) is the guide.

## The guarantees behind the answers

- **Over-accepts and over-rejects are both budgeted at zero** in the differential proof the artifacts are built from. A row accepted here and rejected by the server ships before the insert fails; a row rejected here and accepted by the server is silent data loss. Neither is acceptable, so neither has a budget.
- **`unsupported` is an answer, never a guess.** A binding surfaces the library's decline and never papers over one.
- **The four bindings give one answer.** They run the same golden set, and each is scored as its own column against real ClickHouse servers. A behavior change lands in all four in one cycle.
- **A version is never a nearest match.** Version behavior is not monotonic — 25.10 rejects a DEFAULT that both 25.8 and 26.6 accept — so asking for a line you do not have fails loudly rather than answering from a neighbor.

## Where to go

### Start here

|                                                  |                                                                                                          |
| ------------------------------------------------ | -------------------------------------------------------------------------------------------------------- |
| [`install.md`](install.md)                       | both steps, in all four languages                                                                        |
| [`quickstart.md`](quickstart.md)                 | the same first program, four times                                                                       |
| [`../examples/README.md`](../examples/README.md) | a longer runnable tour, section for section in all four — a diff between two of them shows only spelling |
| [`support.md`](support.md)                       | which languages, platforms and ClickHouse lines — generated, so it cannot drift                          |

### Guides

|                                                          |                                                                       |
| -------------------------------------------------------- | --------------------------------------------------------------------- |
| [`guides/artifacts.md`](guides/artifacts.md)             | getting an artifact, where it lands, verifying and pinning it         |
| [`guides/batches.md`](guides/batches.md)                 | rows, and what happens at the first bad one                           |
| [`guides/transformations.md`](guides/transformations.md) | the silent-change report, and the DEFAULTs you must echo back         |
| [`guides/settings.md`](guides/settings.md)               | the four settings channels and which one wins                         |
| [`guides/discovery.md`](guides/discovery.md)             | asking a real server what profile to validate under                   |
| [`guides/filters.md`](guides/filters.md)                 | boolean expressions over rows, query parameters, the parse-once block |
| [`guides/multi-version.md`](guides/multi-version.md)     | several ClickHouse versions in one process; thread-safety             |
| [`guides/fetch.md`](guides/fetch.md)                     | the fetch and verification contract, normatively                      |
| [`limitations.md`](limitations.md)                       | what chtypes declines to answer, and why                              |

### Reference

|                                                                                                                                                                       |                                                                                                              |
| --------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| [`reference/go.md`](reference/go.md) · [`reference/python.md`](reference/python.md) · [`reference/ts.md`](reference/ts.md) · [`reference/rust.md`](reference/rust.md) | every public symbol, the C entry point under it, and what it returns or raises                               |
| [`reference/bindings.md`](reference/bindings.md)                                                                                                                      | the normative shape every binding implements — read this when porting, or when two bindings seem to disagree |
| [`reference/c-abi.md`](reference/c-abi.md)                                                                                                                            | the `chs_*` contract underneath all four, including the error model and the result documents                 |

The two normative pages are the deep layer. You should not need either to use chtypes; they are where a disagreement is settled, and where the answer is when a per-language reference page says "see the ABI contract".
