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

## Regenerating it

The generator lives with the proof, in the core repository:

    CHTYPES_REGISTRY=<registry> \
      go run ./cmd/goldens-gen --in ../../sdk/goldens/cases.in.json --out <this repo>/goldens/cases.json

(run from `chtypes-core/tests/conformance/go`). Inputs are in
`chtypes-core/tests/sdk/goldens/cases.in.json`; add a case there, regenerate,
and commit the result here. A regeneration that changes an existing
expectation is a library change and needs the same scrutiny as one.
