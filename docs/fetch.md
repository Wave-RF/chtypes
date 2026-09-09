# Fetching, verifying and installing artifacts — the contract every SDK implements

`docs/artifacts.md` says what a release is. This page says what every binding
does about it, identically: the same commands, the same function, the same
search path, the same verification chain, the same error, so a user who
learned one SDK has learned all four. `scripts/fetch.sh` is the reference
implementation and stays; the four in-package implementations replace the
need to have this repository checked out.

## 1. Where artifacts are looked for (the registry search path)

A registry is a directory holding `<minor>/manifest.json` entries. Lookup
tries, in order, and takes the **first directory that contains the requested
line**:

1. a path given explicitly to the registry constructor;
2. `CHTYPES_REGISTRY`;
3. `${XDG_CACHE_HOME:-~/.cache}/chtypes/artifacts/<os>-<arch>` — the per-user cache, where fetch installs;
4. `/usr/local/share/chtypes/artifacts/<os>-<arch>` and then `/opt/chtypes/artifacts/<os>-<arch>` — system locations, empty today, reserved for the deferred system packages (§8) and for images that bake artifacts in.

Fetch **writes** to the first of (1), (2), (3) that is set; never to (4).
`<os>` is `linux` or `darwin`, `<arch>` is `arm64` or `amd64`, in those
spellings.

## 2. Where artifacts come from

`CHTYPES_ARTIFACTS_URL` (default `https://artifacts.wavehouse.dev`) plus a
release tag (default the rolling `artifacts`; `--tag v1.2.0` for a frozen
one) gives `<url>/<tag>/`; `--url <base>` names any other base, including a
local directory or a `file://` path. A line (`25.8`) resolves to the one
patch the release publishes for it; an exact patch (`25.8.28.1-lts`) is a
hard requirement and fails if absent.

## 3. The verification chain, in order

Nothing is a verdict but the chain; no exit code, no `Content-Length`, no
"download finished" ever is.

0. **Signature.** `SHA256SUMS.sig` is fetched and its ed25519 signature is
   verified over the exact bytes of `SHA256SUMS` with a trusted public key
   (§4). Failure stops everything: an unsigned or mis-signed release is
   reported as `CHTYPES_ARTIFACT_UNTRUSTED`, never downloaded around.
1. `index.json` names the asset file for the line/platform and records its
   sha256.
2. `SHA256SUMS` — now known-authentic — must list the same file with the
   same sha256. A disagreement is a broken release, reported, not repaired.
3. The tarball is hashed **before** it is unpacked and must equal that sha256.
4. `manifest.json` inside names the library and its sha256; after the move
   into `<registry>/<minor>/`, the installed library is hashed again in place.

The install is atomic: unpack into a temporary sibling, rename into place.
An already-installed line that hashes what `SHA256SUMS` says is reported as
installed and nothing is downloaded (`--force` re-downloads).

**Not covered, by design, and said out loud:** freshness. A host serving an
older *signed* release is accepted; §5 pins are how a consumer refuses that.
Build provenance (which workflow built which commit) is a later, separate
layer (Sigstore attestations, once every build runs in CI).

## 4. The signature

`SHA256SUMS.sig` is two lines:

```
untrusted comment: chtypes artifacts, ed25519 key deb275922dbff76e
<base64 of the 64-byte ed25519 signature over the bytes of SHA256SUMS>
```

The key id is the first 16 hex characters of sha256 over the raw 32-byte
public key. The release key today:

| | |
|---|---|
| public key (raw, hex) | `fdb5f06a8d4c9918d049a5f1748fa2e3b3238c3f2000986d5bb9e31beff778fc` |
| key id | `deb275922dbff76e` |
| private half | Phase app `chtypes`, `/infra`, `CHTYPES_SIGNING_KEY`; only `dist/publish.sh` in the core repository signs |

Every SDK embeds that public key as a constant and verifies with it. Policy,
same in all four:

- `CHTYPES_TRUSTED_KEYS=<hex>[,<hex>…]` **replaces** the embedded list — for a
  mirror or a custom registry signed by someone else.
- `CHTYPES_ALLOW_UNSIGNED=1` skips step 0 and prints one loud warning naming
  the source. Never the default; never silent.
- Once files are in a registry directory, the loader trusts the directory.
  Verification is a fetch-time policy, not a load-time gate — exactly as a
  runtime trusts `node_modules`. Custom or modified artifacts are installed
  by copying them in.

Reference vector (openssl, `-rawin`): message `68656c6c6f0a` ("hello\n")
signs under the key above to
``0fee686f7ed7c64b86a7dce0ffd66b15d1504178153c3b0cc118e2c9456afa6d3e2e55019eca8f75e44ab507d65b0714523e92c7f92452821930691212e76c04``; flipping one byte of the message fails.

## 5. Pinning

`fetch --lock chtypes.lock` records, per `<os>-<arch>/<minor>`, the asset
file and sha256 that were installed; `fetch --frozen` (or
`ensure(..., lock=…)`) refuses anything else with `CHTYPES_ARTIFACT_PINNED`.
The file is JSON, schema 1:

```json
{"schema": 1, "artifacts": {"linux-arm64/25.8": {"file": "chtypes-25.8.28.1-lts-linux-arm64.tar.gz", "sha256": "…"}}}
```

That is the lockfile model every package manager uses: trust on first fetch,
byte-identical thereafter, and CI fails on drift.

## 6. The commands and the function

One CLI surface, spelled identically:

```
chtypes fetch <line>... [--all] [--platform <os-arch>] [--dest <dir>]
                        [--tag <t> | --url <base>] [--lock <file>] [--frozen]
                        [--force] [--offline]
chtypes verify [--dest <dir>]        re-hash every installed line against its manifest
chtypes list   [--dest <dir>]        what is installed, and what the release offers
chtypes where                        the registry directory fetch would write to
```

| SDK | invocation | library call |
|---|---|---|
| TypeScript | `npx @wavehouse/chtypes fetch 25.8` (`bin: chtypes`) | `ensure('25.8', opts)` |
| Python | `python -m chtypes fetch 25.8` and the `chtypes` console script | `chtypes.ensure('25.8', **opts)` |
| Rust | `cargo install chtypes` → `chtypes fetch 25.8` (the crate's `[[bin]]`) | `chtypes::ensure("25.8", &opts)` |
| Go | `go run github.com/wave-rf/chtypes/go/cmd/chtypes@latest fetch 25.8` | `chtypes.Ensure(ctx, "25.8", opts)` |

`ensure` is idempotent: installed-and-verified is a no-op, otherwise it
fetches through §3. Exit codes: 0 ok · 1 verification failed · 2 usage ·
3 source unreachable · 4 not published for this platform/line.

**Lazy fetch on first open** is opt-in: the registry constructor's
`autofetch` option, or `CHTYPES_AUTOFETCH=1`. Off, a missing line is the
error in §7. On, opening a missing line runs `ensure` first (one process-wide
lock so concurrent opens fetch once). Off by default because a production
process must not begin a 250 MB download inside a request.

## 7. The one error

Every SDK raises one identifiable error for a missing artifact — Go
`ErrArtifactMissing` (works with `errors.Is`), Python
`ArtifactMissingError`, TypeScript `ArtifactMissingError` with
`code = 'CHTYPES_ARTIFACT_MISSING'`, Rust `Error::ArtifactMissing` — with
this message, verbatim apart from the bracketed parts:

```
chtypes: no artifact for ClickHouse <line> (<os>-<arch>). Looked in: <dir1>, <dir2>, ….
Install it:  <this SDK's fetch command> <line>
or set CHTYPES_AUTOFETCH=1 to fetch on first use.
```

Codes, shared: `CHTYPES_ARTIFACT_MISSING`, `CHTYPES_ARTIFACT_UNTRUSTED`,
`CHTYPES_ARTIFACT_CORRUPT` (any hash mismatch), `CHTYPES_ARTIFACT_PINNED`,
`CHTYPES_ARTIFACT_UNPUBLISHED`, `CHTYPES_SOURCE_UNREACHABLE`.

Python note: `Registry()` today requires an explicit path or the environment
variable; it moves to the §1 search path like the other three (pre-1.0, so a
breaking change is acceptable and is noted in the changelog).

## 8. Deferred: system packages (brew, apt)

A `brew install chtypes` or a Debian package would pre-seed the system
locations in §1 and keep them updated by the package manager's own
mechanism, and would carry the fetch command as a standalone tool. Deferred
on 2026-09-09 until the four in-package commands exist; nothing in §1–§7
needs to change to add it — the search path already has its slots.

## 9. Test vectors

`spec/fixtures/fetch/` holds miniature releases the four implementations
are tested against through `--url file://…`: `signed/` (valid; tiny fake
libraries whose manifests hash correctly), `bad-signature/`,
`tampered-tarball/`, `sums-index-mismatch/`, `unsigned/`, and a `chtypes.lock`
that pins `signed/`. They are generated by the core repository's tooling,
the way `goldens/` is, and never edited by hand. An SDK's fetch suite must
pass all of them with the same verdicts and codes.
