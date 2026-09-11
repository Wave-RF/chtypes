# goldens — the SDK's public smoke proof, served rather than tracked

There is no cases file in this directory any more, and there will not be one.
Core publishes the golden set in the rolling release beside the artifacts:

    https://artifacts.wavehouse.dev/artifacts/sdk-goldens.json

It is a row in the signed `SHA256SUMS`, exactly like a tarball, so it verifies
through the same chain — the ed25519 signature covers the sums, the sums name
its sha256, and the bytes on disk must hash to it (`docs/fetch.md` §3).
`scripts/fetch.sh` installs it as `<registry>/sdk-goldens.json`, so every
binding's golden test reads it **offline** after a fetch, exactly as it reads an
artifact.

    scripts/fetch.sh 25.8          # installs the artifact AND the golden set

`CHTYPES_GOLDENS` overrides the path, for local generation.

## What a case promises

Every expectation was **produced by the library**, never typed in, and was
**identical on every ClickHouse line in `generated.versions`**. A case any line
answered differently is refused by the generator and listed under
`generated.refused`, because a golden that is true on one line and false on
another is not a golden.

That is why the set **shrinks** as lines are added, and why a shrinking set is
not lost coverage: a case that stops being version-agnostic moves to core's
per-line behaviour goldens, where the answer is recorded per version instead of
pretended to be universal.

It is **not** the corpus. The differential proof — tens of thousands of cases
scored against real ClickHouse servers on every supported version — lives with
the library that produces the artifacts, and stays there.

## The version gate

`generated.exact` maps a line to the **exact** ClickHouse version the
expectations were generated against (`"25.8": "25.8.33.6-lts"`). A golden test
runs a case against an artifact only when that artifact's exact version equals
it, and **skips loudly by name** otherwise, naming the version it wanted and the
one it found.

This matters because the rolling index keeps every patch row ever published, so
a machine can be holding an older patch than the set was generated on. That is a
skip, never a failure: an expectation produced on one build says nothing about
another.

## Reading it

`schema` is `1`. A reader must refuse a schema it does not know. Beyond
`cases`, the `generated` header carries `at`, `by`, `note`, `platform`,
`versions`, `exact`, `refused`, `core_commit` and `builds` (line -> wrapper
build).
