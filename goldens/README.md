# goldens — the SDK's public smoke proof

`cases.json` is a few dozen cases every SDK in this repository runs against
whatever artifacts a machine has — `go/chtypes/golden_test.go`,
`python/tests/test_golden.py`, `ts/test/golden.test.ts`, `rust/tests/golden.rs`
— so that four bindings are held to one answer, and so that a fresh clone with
a fetched artifact can prove the SDK works before reading anything else.

It is **not** the corpus. The differential proof — tens of thousands of cases
scored against real ClickHouse servers on every supported version — lives with
the library that produces the artifacts, and stays there.

## What a case promises

Every expectation in this file was **produced by the library**, never typed
in, and was **identical on every ClickHouse version** in the generating
registry (listed under `generated.versions`). A case any version answered
differently is refused by the generator and listed under `generated.refused`,
because a golden that is true on one line and false on another is not a golden.
The set is deliberately broad rather than deep: one case per text format, per
type family, per verdict class, one filter, two schema refusals. No case reads
a clock, and no case parses a float with a fractional part (the artifacts'
float parsing matches real servers on Linux only; macOS is a development floor
whose `long double` diverges).

`schema` is `1`. A reader must refuse a schema it does not know.

## Running it

Each SDK's own test suite includes its golden test; point `CHTYPES_REGISTRY`
at a registry directory, or fetch artifacts into the per-user cache with
`scripts/fetch.sh` and the tests find them there. `CHTYPES_GOLDENS` overrides
the file's location.

## Where it comes from

**This file is delivered, not regenerated here, and never hand-edited.**

Core's `certify` workflow regenerates the set against every line the artifacts
host serves, compares it case by case with the copy in this repository, and when
the two differ it uploads the new file as the run artifact `sdk-goldens-cases`
and opens an ops issue on `Wave-RF/chtypes-core` under the key `sdk-goldens`.
The whole job here is:

1. Open the issue, follow it to the linked run.
2. Download the `sdk-goldens-cases` artifact.
3. Drop it in as `goldens/cases.json` and land it.

Expect the set to **shrink** as lines are added, not grow. The generator keeps
only cases every line answers identically, so a case that becomes
version-dependent is dropped rather than recorded twice — which is the point of
a golden, and why a shrinking set is not a loss of coverage. The behaviour a
dropped case used to pin lives on in core's per-line behaviour goldens, and in
whichever binding suite gates it on the artifact's version.

Inputs live in core (`tests/sdk/goldens/cases.in.json`): a missing case is an
issue against core, not an edit here. A delivery that changes an existing
expectation is a library change and needs the same scrutiny as one.
