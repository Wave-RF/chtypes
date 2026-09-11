# Fetching, verifying and installing artifacts — the contract every SDK implements

> This is the normative contract, written for someone implementing or auditing it. If you only want an artifact on your machine, [`artifacts.md`](artifacts.md) is the shorter road and links back here for the details.

[`artifacts.md`](artifacts.md) says what a release is. This page says what every binding does about it, identically: the same commands, the same function, the same search path, the same verification chain, the same error, so a user who learned one SDK has learned all four. [`scripts/fetch.sh`](../../scripts/fetch.sh) is the reference implementation and stays; the four in-package implementations replace the need to have this repository checked out.

## 1. Where artifacts are looked for (the registry search path)

A registry is a directory holding `<minor>/manifest.json` entries. Lookup tries, in order, and takes the **first directory that contains the requested line**:

1. a path given explicitly to the registry constructor;
2. `CHTYPES_REGISTRY`;
3. `${XDG_CACHE_HOME:-~/.cache}/chtypes/artifacts/<os>-<arch>` — the per-user cache, where fetch installs;
4. `/usr/local/share/chtypes/artifacts/<os>-<arch>` and then `/opt/chtypes/artifacts/<os>-<arch>` — system locations, empty today, reserved for the deferred system packages (§8) and for images that bake artifacts in.

Fetch **writes** to the first of (1), (2), (3) that is set; never to (4). `<os>` is `linux` or `darwin`, `<arch>` is `arm64` or `amd64`, in those spellings.

## 2. Where artifacts come from

`CHTYPES_ARTIFACTS_URL` (default `https://artifacts.wavehouse.dev`) plus a release tag (default the rolling `artifacts`; `--tag v1.2.0` for a frozen one) gives `<url>/<tag>/`; `--url <base>` names any other base, including a local directory or a `file://` path. A line (`25.8`) resolves to the one patch the release publishes for it; an exact patch (`25.8.28.1-lts`) is a hard requirement and fails if absent.

A release holds four kinds of file:

| file                                          | what it is                                                                 |
| --------------------------------------------- | -------------------------------------------------------------------------- |
| `SHA256SUMS`, `SHA256SUMS.sig`                | the signed manifest of every other file (§3, §4)                           |
| `index.json`                                  | the listing: one row per artifact, per platform (schema 1)                 |
| `chtypes-<version>-<os>-<arch>[-b<N>].tar.gz` | the artifacts. `-b<N>` is the wrapper build; a name without one is build 0 |
| `sdk-goldens.json`                            | the **served golden set** the SDK suites run                               |

`sdk-goldens.json` is a row in `SHA256SUMS` like any tarball, so it verifies through the same chain, and a fetch installs it at `<registry>/sdk-goldens.json` — beside the artifacts, where every binding's golden test reads it offline (see the core repository's golden-set documentation; `CHTYPES_GOLDENS` overrides the path). A release that does not publish one predates the served set: that is a note, not a failure, and the golden tests skip loudly until it does.

**Rebuilds are new rows, never swaps.** The same ClickHouse version can be published more than once, each with a higher `build`, and the release keeps the two highest per version and platform. A line therefore resolves to its newest ClickHouse version and then to the **highest build** of that version — the row's `build` field when it has one, else the `-b<N>` in the name, else 0. A lock file pins a file name and sha256, which is exactly what keeps a pin valid across a rebuild.

## 3. The verification chain, in order

Nothing is a verdict but the chain; no exit code, no `Content-Length`, no "download finished" ever is.

0. **Signature.** `SHA256SUMS.sig` is fetched and its ed25519 signature is verified over the exact bytes of `SHA256SUMS` with a trusted public key (§4). Failure stops everything: an unsigned or mis-signed release is reported as `CHTYPES_ARTIFACT_UNTRUSTED`, never downloaded around.
1. `index.json` names the asset file for the line/platform and records its sha256.
2. `SHA256SUMS` — now known-authentic — must list the same file with the same sha256. A disagreement is a broken release, reported, not repaired.
3. The tarball is hashed **before** it is unpacked and must equal that sha256.
4. `manifest.json` inside names the library and its sha256; after the move into `<registry>/<minor>/`, the installed library is hashed again in place.

The install is atomic: unpack into a temporary sibling, rename into place. An already-installed line that hashes what `SHA256SUMS` says is reported as installed and nothing is downloaded (`--force` re-downloads).

### 3a. The publish window, and the only thing that retries

A publish into the rolling release is **three objects** — `SHA256SUMS`, `SHA256SUMS.sig`, `index.json` — and object storage cannot swap them atomically. They are uploaded in that order, so an old `index.json` read against new sums still cross-checks at step 2; the only genuinely unsafe window is between the sums and the signature that covers them. One small object wide, seconds long.

Exactly **two** of the failures above are symptoms of reading inside that window, and both are retried — three attempts about four seconds apart, roughly ten seconds in all:

| symptom                                             | code                         |
| --------------------------------------------------- | ---------------------------- |
| step 0: the signature verifies under no trusted key | `CHTYPES_ARTIFACT_UNTRUSTED` |
| step 2: `index.json` and `SHA256SUMS` disagree      | `CHTYPES_ARTIFACT_CORRUPT`   |

Nothing else retries. A tarball whose hash is wrong (step 3) is the release lying about a byte, not a half-finished upload, and refuses at once — as do the two above once the attempts run out, with the same code and the same exit status they have always had. **A retry buys ten seconds; it never converts a refusal into an install.**

Step 2 is therefore checked twice: once for the whole release as soon as the three objects are read — so a disagreement is seen while re-reading can still fix it — and again for the asset actually being installed.

Only an `http(s)` source can be mid-publish. A `file://` URL or a plain directory is read exactly once and refuses on the first look, which is also why the `tests/fixtures/fetch` suites stay instant.

**Not covered, by design, and said out loud:** freshness. A host serving an older _signed_ release is accepted; §5 pins are how a consumer refuses that. Build provenance (which workflow built which commit) is a later, separate layer (Sigstore attestations, once every build runs in CI).

## 4. The signature

`SHA256SUMS.sig` is two lines:

```text
untrusted comment: chtypes artifacts, ed25519 key deb275922dbff76e
<base64 of the 64-byte ed25519 signature over the bytes of SHA256SUMS>
```

The key id is the first 16 hex characters of sha256 over the raw 32-byte public key. The release key today:

|                       |                                                                                                           |
| --------------------- | --------------------------------------------------------------------------------------------------------- |
| public key (raw, hex) | `fdb5f06a8d4c9918d049a5f1748fa2e3b3238c3f2000986d5bb9e31beff778fc`                                        |
| key id                | `deb275922dbff76e`                                                                                        |
| private half          | Phase app `chtypes`, `/infra`, `CHTYPES_SIGNING_KEY`; only `dist/publish.sh` in the core repository signs |

Every SDK embeds that public key as a constant and verifies with it. Policy, same in all four:

- `CHTYPES_TRUSTED_KEYS=<hex>[,<hex>…]` **replaces** the embedded list — for a mirror or a custom registry signed by someone else.
- `CHTYPES_ALLOW_UNSIGNED=1` skips step 0 and prints one loud warning naming the source. Never the default; never silent.
- Once files are in a registry directory, the loader trusts the directory. Verification is a fetch-time policy, not a load-time gate — exactly as a runtime trusts `node_modules`. Custom or modified artifacts are installed by copying them in.

Reference vector (openssl, `-rawin`): message `68656c6c6f0a` ("hello\n") signs under the key above to `0fee686f7ed7c64b86a7dce0ffd66b15d1504178153c3b0cc118e2c9456afa6d3e2e55019eca8f75e44ab507d65b0714523e92c7f92452821930691212e76c04`; flipping one byte of the message fails.

## 5. Pinning

`fetch --lock chtypes.lock` records, per `<os>-<arch>/<minor>`, the asset file and sha256 that were installed; `fetch --frozen` (or `ensure(..., lock=…)`) refuses anything else with `CHTYPES_ARTIFACT_PINNED`. The file is JSON, schema 1:

```json
{"schema": 1, "artifacts": {"linux-arm64/25.8": {"file": "chtypes-25.8.28.1-lts-linux-arm64.tar.gz", "sha256": "…"}}}
```

That is the lockfile model every package manager uses: trust on first fetch, byte-identical thereafter, and CI fails on drift.

## 6. The commands and the function

One CLI surface, spelled identically:

```text
chtypes fetch <line>... [--all] [--platform <os-arch>] [--dest <dir>]
                        [--tag <t> | --url <base>] [--lock <file>] [--frozen]
                        [--force] [--offline]
chtypes verify [--dest <dir>]        re-hash every installed line against its manifest
chtypes list   [--dest <dir>]        what is installed, and what the release offers
chtypes where                        the registry directory fetch would write to
```

| SDK        | invocation                                                             | library call                        |
| ---------- | ---------------------------------------------------------------------- | ----------------------------------- |
| TypeScript | `npx @wavehouse/chtypes fetch 25.8` (`bin: chtypes`)                   | `ensure('25.8', opts)`              |
| Python     | `python -m chtypes fetch 25.8` and the `chtypes` console script        | `chtypes.ensure('25.8', **opts)`    |
| Rust       | `cargo install chtypes` → `chtypes fetch 25.8` (the crate's `[[bin]]`) | `chtypes::ensure("25.8", &opts)`    |
| Go         | `go run github.com/wave-rf/chtypes/go/cmd/chtypes@latest fetch 25.8`   | `chtypes.Ensure(ctx, "25.8", opts)` |

`ensure` is idempotent: installed-and-verified is a no-op, otherwise it fetches through §3. Exit codes: 0 ok · 1 verification failed · 2 usage · 3 source unreachable · 4 not published for this platform/line.

**Lazy fetch on first open** is opt-in: the registry constructor's `autofetch` option, or `CHTYPES_AUTOFETCH=1`. Off, a missing line is the error in §7. On, opening a missing line runs `ensure` first (one process-wide lock so concurrent opens fetch once). Off by default because a production process must not begin a 250 MB download inside a request.

## 7. The one error

Every SDK raises one identifiable error for a missing artifact — Go `ErrArtifactMissing` (works with `errors.Is`), Python `ArtifactMissingError`, TypeScript `ArtifactMissingError` with `code = 'CHTYPES_ARTIFACT_MISSING'`, Rust `Error::ArtifactMissing` — with this message, verbatim apart from the bracketed parts:

```text
chtypes: no artifact for ClickHouse <line> (<os>-<arch>). Looked in: <dir1>, <dir2>, ….
Install it:  <this SDK's fetch command> <line>
or set CHTYPES_AUTOFETCH=1 to fetch on first use.
```

Codes, shared: `CHTYPES_ARTIFACT_MISSING`, `CHTYPES_ARTIFACT_UNTRUSTED`, `CHTYPES_ARTIFACT_CORRUPT` (any hash mismatch), `CHTYPES_ARTIFACT_PINNED`, `CHTYPES_ARTIFACT_UNPUBLISHED`, `CHTYPES_SOURCE_UNREACHABLE`.

## 8. Deferred: system packages (brew, apt)

A `brew install chtypes` or a Debian package would pre-seed the system locations in §1 and keep them updated by the package manager's own mechanism, and would carry the fetch command as a standalone tool. Deferred on 2026-09-09 until the four in-package commands exist; nothing in §1–§7 needs to change to add it — the search path already has its slots.

## 9. Test vectors

`tests/fixtures/fetch/` holds miniature releases the four implementations are tested against through `--url file://…`: `signed/` (valid; tiny fake libraries whose manifests hash correctly), `bad-signature/`, `tampered-tarball/`, `sums-index-mismatch/`, `unsigned/`, `two-builds/`, and a `chtypes.lock` that pins `signed/`.

`two-builds/` is the rebuild case: one ClickHouse version published twice for each platform, so resolving a line exercises the build tie-break rather than the version alone. `expected.json` records it under `builds.cases` — deliberately apart from `verdicts`, which name one asset per line — and each case says which build must install and which it supersedes. The proof is the installed library's bytes: both rows share a `clickhouse_version`, so only `library_sha256` can tell them apart. They are generated by the core repository's tooling, the way `goldens/` is, and never edited by hand. An SDK's fetch suite must pass all of them with the same verdicts and codes.

## 10. Decisions (2026-09-09, when the four implementations merged)

Where the four implementations diverged, one rule was chosen and the odd ones out were changed the same day; where reconciling would have cost more than it is worth today, the difference is recorded here instead of hidden. These bind §1–§7.

1. **`--frozen` without `--lock` reads `./chtypes.lock`** (relative to the working directory), in the library call as well as the CLI. A lock file that does not exist under `--frozen` is `CHTYPES_ARTIFACT_PINNED` (exit 1): nothing is pinned, so nothing is installed — the same verdict as a line the lock does not pin. It is decided after the release loads, so an untrusted or unreachable source is reported ahead of it. (Python refused `--frozen` without `--lock` as a usage error, TypeScript's library call did too, and Rust's missing-lock error carried no code; all three changed.)
2. **An explicit destination is the only place "already installed" is looked for.** `--dest <dir>` / the `dest` option means that directory; an install elsewhere on the §1 path does not satisfy it (a container build's `--dest /opt/chtypes/artifacts` must not be satisfied by the builder's own cache). All four did this. _Known difference, not reconciled:_ without a `dest`, Rust also accepts an install anywhere on the §1 search path (what a registry would find); Go, Python and TypeScript look only in the directory fetch would write to. It shows only when a line sits in a later slot than the write directory — the system slots are empty today.
3. **A fetch for another platform never writes into `CHTYPES_REGISTRY`.** That variable names a directory this host dlopens from, so it is on the search path for the host's platform only; `fetch --platform <other>` writes to that platform's own per-user cache (`…/chtypes/artifacts/<os>-<arch>`), and `--dest` still wins. (Python wrote into `CHTYPES_REGISTRY`; changed.) _Known difference:_ Go and TypeScript also read `CHTYPES_TARGET` as the default `--platform` (see [`artifacts.md`](artifacts.md)); Python and Rust take `--platform` only.
4. **An explicit registry directory that lacks a line falls through** to the rest of the §1 path, in every SDK's search-path constructor. In Rust that constructor is `Registry::from_search_path()` / `Registry::from_search_path_with(RegistryOptions { dir, .. })`; `Registry::new(dir)` stays the single-directory loader (eager, that directory only, `Error::NoSuchVersion` for a line it lacks), by design. _Known difference:_ a named directory that does not exist — Python skips it silently; Go and TypeScript refuse unless autofetch is on (then it is the destination-to-be); Rust's single-directory constructor refuses.
5. **`CHTYPES_ALLOW_UNSIGNED=1` skips step 0 entirely**: `SHA256SUMS.sig` is not even fetched, so a present-but-wrong signature installs, behind the one loud warning naming the source. `SHA256SUMS` itself is still required and steps 1–4 still run — a tampered tarball is still refused. All four agree.
6. **"Installed" is decided against the signed release.** A plain `ensure` / `fetch` of an installed line reads `SHA256SUMS`, its signature and `index.json` — never the tarball — and reports installed when the library in place hashes what that listing says (§3). `--offline` is the one path that reads no source: an installed line that hashes what its own `manifest.json` says is the answer; anything else is `CHTYPES_SOURCE_UNREACHABLE` (a damaged install, `CHTYPES_ARTIFACT_CORRUPT`). (Rust's plain `ensure` was local-only; changed.)
7. **An exact patch spelled without its channel matches that patch on any channel**: `25.8.28.1` and `v25.8.28.1` take `25.8.28.1-lts`; a spelled channel (`25.8.28.1-stable`) matches only itself, and a miss is `CHTYPES_ARTIFACT_UNPUBLISHED`, never the neighboring patch. (Go required the exact string; changed.)
8. **`CHTYPES_ARTIFACT_PINNED` exits 1**, like `…_UNTRUSTED` and `…_CORRUPT`: a verification failure. Only `CHTYPES_SOURCE_UNREACHABLE` (3) and `CHTYPES_ARTIFACT_UNPUBLISHED` (4) have exit codes of their own. All four agree.
9. **The "Install it" line of §7, per SDK** — two spaces after the colon, then the SDK's own fetch command and the line: Go `go run github.com/wave-rf/chtypes/go/cmd/chtypes@latest fetch <line>` · Python `python -m chtypes fetch <line>` · TypeScript `npx @wavehouse/chtypes fetch <line>` · Rust `cargo install chtypes && chtypes fetch <line>`.
10. **Python's `Registry()` walks the §1 search path** like the other three. It once required an explicit path or `CHTYPES_REGISTRY`; that was the last constructor that did not, and it changed on the same day.
