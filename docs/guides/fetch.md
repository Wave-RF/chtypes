# Fetching, verifying and installing artifacts — the contract every SDK implements

> This is the normative contract, written for someone implementing or auditing it. If you only want an artifact on your machine, [`artifacts.md`](artifacts.md) is the shorter road and links back here for the details.

[`artifacts.md`](artifacts.md) says what a release is. This page says what every binding does about it, identically: the same commands, the same function, the same search path, the same verification chain, the same error, so a user who learned one SDK has learned all four. [`scripts/fetch.sh`](../../scripts/fetch.sh) is the reference implementation and stays; the four in-package implementations replace the need to have this repository checked out.

## 1. Where artifacts are looked for (the registry search path)

A registry is a directory holding `<minor>/manifest.json` entries. Lookup tries, in order, and takes the **first directory that contains the requested line**:

1. a path given explicitly to the registry constructor;
2. `CHTYPES_REGISTRY`;
3. `${XDG_CACHE_HOME:-~/.cache}/chtypes/artifacts/abi<R>/<os>-<arch>` — the per-user cache, where fetch installs. `<R>` is the SDK's own ABI revision: each binding's pinned constant, and for `scripts/fetch.sh` the `CHS_ABI_REVISION` of its own tree's `include/chtypes.h` (or `--abi-revision N`);
4. `/usr/local/share/chtypes/artifacts/<os>-<arch>` and then `/opt/chtypes/artifacts/<os>-<arch>` — system locations, empty today, reserved for the deferred system packages (§8) and for images that bake artifacts in.

Fetch **writes** to the first of (1), (2), (3) that is set; never to (4). `<os>` is `linux` or `darwin`, `<arch>` is `arm64` or `amd64`, in those spellings. One machine set up once therefore serves all four bindings at the same ABI revision, and a locally built artifact belongs in the same `abi<R>/<os>-<arch>` directory.

**Each ABI revision has its own per-user cache.** Two SDK versions on one machine that speak different revisions never overwrite each other's artifacts, and neither ever finds the other's. An install from before this layout, directly under `…/chtypes/artifacts/<os>-<arch>/`, is neither read nor migrated: the first fetch after upgrading downloads again, and the old directory is simply unused. `CHTYPES_REGISTRY` and an explicit directory are taken exactly as given — the revision is added to the default only.

Once resolved, an artifact and the SDK opening it are matched by ABI revision: a mismatch is refused at load, naming both numbers. Fetch never installs a mismatched one in the first place (§2); the load check is what still protects a directory filled some other way.

### Two levels inside one registry directory (chtypes#284, "Layout rule")

Each registry directory above holds two levels of installs. **ONLY a line spelling (or `--all`) ever WRITES the flat `<registry>/<minor>/` slot** — exactly as before this change; every SDK through 0.4.x reads and writes only this level. **An exact-patch spelling ALWAYS installs at `<registry>/patches/<minor>/<clickhouse_version>/` instead, even when it happens to be the line's newest published patch and the flat slot is empty.** This is a rule about the REQUEST, never about the row: whether a patch lands flat depends only on whether it was asked for by line or by exact spelling, not on whether it is the newest one the release has. The one exception is not a write at all — when the requested exact patch is ALREADY installed flat and verifies, that is a no-op reporting the flat directory (below). `patches/` is a sibling tree released SDKs neither see nor touch — measured against v0.4.0 of all four bindings and `scripts/fetch.sh`, including their `list` and `verify` output: none of them lists, loads or installs into it.

- A **line** request resolves in the FIRST directory on the search path that holds ANY patch of that line, flat or nested, taking the newest one there.
- A **patch** request is searched on the WHOLE path: the first directory anywhere that holds a matching patch wins, even if an earlier directory holds only the line's OTHER patch. This extends Decision 4 (an explicit directory that lacks a line falls through) from lines to patches — the more specific request wins.

**Flat installs are read indefinitely, as a first-class layout, not a migration shim.** They come from SDKs 0.3.2 through 0.4.x, hand copies, images baked with `--dest`, and local builds (§4 documents "installed by copying them in" as the custom-artifact route). They are never moved, rewritten or deleted; a fetch only ever DEMOTES a flat install — moves it into `patches/<minor>/<its version>/`, atomically, on the same filesystem — when a LINE fetch is about to put a different patch in the flat slot, and never deletes it. That is also the no-op named above: an EXACT request whose patch is already the one sitting flat, verified, reports that flat directory and installs nothing — it never duplicates those bytes into `patches/`.

**A machine sharing this cache between an old and a new SDK.** The 0.4.x SDKs and this one share `abi<R>/` at the same ABI revision (an SDK at a different revision shares nothing — §1). Every 0.4.x scanner, and `fetch.sh` at v0.4.0, requires `<minor>/manifest.json` and silently skips a `<minor>/` without one, so a line held only in `patches/` is invisible to an old SDK: it reports the line missing, or fetches it if autofetch is on. Worse, every 0.4.x installer replaces the WHOLE `<minor>/` it fetches — so **when an old SDK fetches a line, every patch a new SDK demoted into `patches/<minor>/` under that line is deleted with it.** This costs availability, never correctness: a process that already mapped a library keeps using it, and the next request for that patch re-fetches (with autofetch) or falls back within the line, warned. Coexisting SDK versions that must not race each other use separate `--dest` / `CHTYPES_REGISTRY` directories, or are upgraded together.

## 2. Where artifacts come from

`CHTYPES_ARTIFACTS_URL` (default `https://artifacts.wavehouse.dev`) plus a release tag (default the rolling `artifacts`; `--tag v1.2.0` for a frozen one) gives `<url>/<tag>/`; `--url <base>` names any other base, including a local directory or a `file://` path. A line (`26.8`) resolves to the one patch the release publishes for it; an exact patch (`26.8.15.10-lts`) is a hard requirement and fails if absent.

A release holds four kinds of file:

| file                                          | what it is                                                                 |
| --------------------------------------------- | -------------------------------------------------------------------------- |
| `SHA256SUMS`, `SHA256SUMS.sig`                | the signed manifest of every other file (§3, §4)                           |
| `index.json`                                  | the listing: one row per artifact, per platform (schema 1)                 |
| `chtypes-<version>-<os>-<arch>[-b<N>].tar.gz` | the artifacts. `-b<N>` is the wrapper build; a name without one is build 0 |
| `sdk-goldens.json`                            | the **served golden set** the SDK suites run                               |

`sdk-goldens.json` is a row in `SHA256SUMS` like any tarball, so it verifies through the same chain, and a fetch installs it at `<registry>/sdk-goldens.json` — beside the artifacts, where every binding's golden test reads it offline ( `CHTYPES_GOLDENS` overrides the path). A release that does not publish one predates the served set: that is a note, not a failure, and the golden tests skip loudly until it does.

**Only the SDK's own ABI revision is ever selected, and that comes first.** Before any other rule, the rows for the platform are narrowed to those whose `abi_revision` is the SDK's own revision, compared as integers. A row that carries no `abi_revision` never matches: the artifact producer records the field from the revision that introduced it onward, so a row without one was built before revisions were recorded, at an older revision. Every message below names such rows in those words — rows that record no ABI revision, built before revisions were recorded — and never says the release serves "none". Every rule below then applies within the rows that remain. Nothing left for the line, patch or platform is `CHTYPES_ARTIFACT_UNPUBLISHED` (exit 4, §7), naming the revision(s) the release does serve for it. A fetch never falls back to another revision's row: the SDK would refuse it at load anyway (§1). At a revision cutover, an older revision's build stays fetchable forever — the channel is append-only (below) — even after that line's next publish moves which patch a line request selects.

**The rolling channel is append-only.** Every tarball the artifact producer has published stays listed in `index.json` and the signed `SHA256SUMS`; stored tarballs are locked against overwrite and deletion. Versioned snapshot tags are fixed by construction. Nothing is ever evicted: a line's next publish changes which patch a LINE request selects (and, per the layout rule above, demotes the outgoing one to `patches/`), but every patch ever published stays fetchable by its exact spelling, at any ABI revision it was ever recorded at. `--frozen` (§5) relies on this: a pinned file stays fetchable for as long as the pin exists.

`--all` installs every line the release has at the SDK's revision and never skips one silently. For each line the platform has only at another revision, or only in rows that record none, it prints one loud warning line on stderr, then goes on:

```text
WARNING: ClickHouse line <L> on <os>-<arch> is not installed: the release has that line for <os>-<arch> only at ABI revision <S>, and this SDK speaks ABI revision <R>
WARNING: ClickHouse line <L> on <os>-<arch> is not installed: the release has that line for <os>-<arch> only in rows that record no ABI revision (built before revisions were recorded), and this SDK speaks ABI revision <R>
```

The exit status stays 0 when every line it could install did. Only a platform with nothing at all at the SDK's revision is `CHTYPES_ARTIFACT_UNPUBLISHED` (exit 4).

**Rebuilds are new rows, never swaps.** The same ClickHouse version can be published more than once, each with a higher `build`, and the release keeps every build it has ever published — the channel is append-only (above). A line therefore resolves — among the rows at the SDK's ABI revision — to its newest ClickHouse version and then to the **highest build** of that version — the row's `build` field when it has one, else the `-b<N>` in the name, else 0. A lock file pins a file name and sha256, which is exactly what keeps a pin valid across a rebuild.

## 3. The verification chain, in order

Nothing is a verdict but the chain; no exit code, no `Content-Length`, no "download finished" ever is.

0. **Signature.** `SHA256SUMS.sig` is fetched and its ed25519 signature is verified over the exact bytes of `SHA256SUMS` with a trusted public key (§4). Failure stops everything: an unsigned or mis-signed release is reported as `CHTYPES_ARTIFACT_UNTRUSTED`, never downloaded around.
1. `index.json` names the asset file for the line/platform and records its sha256.
2. `SHA256SUMS` — now known-authentic — must list the same file with the same sha256. A disagreement is a broken release, reported, not repaired.
3. The tarball is hashed **before** it is unpacked and must equal that sha256.
4. `manifest.json` inside names the library and its sha256; after the move into `<registry>/<minor>/` (flat) or `<registry>/patches/<minor>/<clickhouse_version>/` (any other patch — §1), the installed library is hashed again in place.

The install is atomic: unpack into a temporary sibling, rename into place, inside whichever of the two directories it is landing in. When a line fetch is about to put a different patch in the flat slot, the outgoing patch is renamed into `patches/<minor>/<its version>/` FIRST — atomically, on the same filesystem — and never deleted (§1, "Two levels"). An already-installed line or patch that hashes what `SHA256SUMS` says is reported as installed and nothing is downloaded (`--force` re-downloads).

### 3a. The publish window, an edge cache widens it, and what retries

A publish into the rolling release is **three objects** — `SHA256SUMS`, `SHA256SUMS.sig`, `index.json` — plus, when a release-level file is being installed (`sdk-goldens.json`, or `--release-file`), a **fourth**. Object storage cannot swap any of them atomically. They are uploaded in that order, so an old `index.json` read against new sums still cross-checks at step 2; the narrowest unsafe window is between the sums and the signature that covers them — one small object, seconds long.

An edge cache in front of the artifacts host widens that window from "between two uploads" to "as long as any one object can still be served stale from cache". `measured` 2026-09-26: `SHA256SUMS`, `SHA256SUMS.sig`, `index.json` and `sdk-goldens.json` are all served `Cache-Control: public, max-age=60`, their names never change between publishes, and a republished `sdk-goldens.json` paired with the previous `SHA256SUMS` refused `CHTYPES_ARTIFACT_CORRUPT` a full minute later.

**Five** kinds of failure are worth reading, or downloading, again, and all five are retried, through the same budget:

| symptom                                                                                                                         | code                         |
| ------------------------------------------------------------------------------------------------------------------------------- | ---------------------------- |
| step 0: the signature verifies under no trusted key                                                                             | `CHTYPES_ARTIFACT_UNTRUSTED` |
| step 2: `index.json` and `SHA256SUMS` disagree                                                                                  | `CHTYPES_ARTIFACT_CORRUPT`   |
| a release-level file's hash disagrees with its own `SHA256SUMS` row, or the sums list it but the source does not (yet) serve it | `CHTYPES_ARTIFACT_CORRUPT`   |
| an HTTP `5xx`, `408` or `429` on `SHA256SUMS`, `SHA256SUMS.sig`, `index.json`, a release-level file, or a tarball               | `CHTYPES_SOURCE_UNREACHABLE` |
| a connection-level failure reaching the host at all — refused, reset, timed out, DNS — on any of those                          | `CHTYPES_SOURCE_UNREACHABLE` |

The third symptom covers `sdk-goldens.json` (§2, checked after the requested artifact installs, since it is best-effort and never blocks that install) and any `--release-file`. "The sums list it but the source does not serve it" is its own half: a new `SHA256SUMS` row can be visible before the file it describes is. The reverse — a file already visible whose row has not landed in `SHA256SUMS` yet — is not covered; that release-level file is read as simply not published yet, exactly as an older release that predates it reads.

**On every retry of a metadata object the WHOLE consistent set is re-read from scratch** — `SHA256SUMS`, its signature, `index.json`, and the release-level file being installed, together — never one freshly re-fetched object checked against another attempt's stale one. A stale `SHA256SUMS` paired with a fresh `sdk-goldens.json` and a fresh `SHA256SUMS` paired with a stale `sdk-goldens.json` are the same window, read the same way. A retry of the **tarball** is narrower: a fresh download, from the top, of that one asset — re-reading the metadata buys it nothing a 5xx or a dropped connection didn't already cost.

**The budget: 5 attempts, delays doubling from 4s — 4, 8, 16, 32 — 60 seconds of sleep, roughly 70 seconds of wall time with network latency.** That is chosen to outlast the edge cache's observed 60-second TTL plus margin, while a genuine few-second mid-publish window still clears on the second attempt. (A fixed doubling schedule was chosen over parsing the response's own `Cache-Control` header for the TTL: identical to implement across five languages, and it does not depend on the header continuing to say 60.) The tarball's retry uses this identical budget and schedule — one shared accounting, not a second allowance stacked on top of the first.

**A `Retry-After` the source sends on a `503` or `429` is honored, in place of that attempt's doubling-schedule delay, in both forms RFC 9110 §10.2.3 allows — delta-seconds and an HTTP-date — capped so the total sleep across every attempt never exceeds the budget above.** A requested wait that would not fit is not honored by blocking for it: the fetch fails at once with `CHTYPES_SOURCE_UNREACHABLE`, naming the requested delay and the number of attempts made, exactly as an exhausted budget does.

Nothing else retries, and neither do any of the five above once the attempts, or the budget, run out: the same code and the same exit status they have always had, with the message now naming how many attempts were made. A `404` or `410` (the asset is simply not there) and a tarball whose hash or size is wrong (step 3) are decided at once, on the first attempt — the release is lying about a byte, or the thing just isn't there, neither of which is a half-finished upload. **A retry buys time; it never converts a refusal into an install.**

Step 2 is therefore checked twice: once for the whole release as soon as the three objects are read — so a disagreement is seen while re-reading can still fix it — and again for the asset actually being installed.

Only an `http(s)` source can be mid-publish. A `file://` URL or a plain directory is read exactly once and refuses on the first look, which is also why the `tests/fixtures/fetch` suites stay instant.

`scripts/fetch.sh` keeps its two env overrides, with a clarified meaning: `CHTYPES_METADATA_ATTEMPTS` is still the attempt count (default 5); `CHTYPES_METADATA_RETRY_DELAY` is now the **base** delay in seconds — each later attempt doubles it, where before it was the constant gap between every attempt. A test that wants every sleep near-instant sets it to `0`. Both also govern the tarball's own retry, the same budget as the metadata's. The four in-package bindings expose the equivalent knobs internally to their own test suites, not as part of this public contract.

**Not covered, by design, and said out loud:** freshness. A host serving an older _signed_ release is accepted; §5 pins are how a consumer refuses that. Build provenance (which workflow built which commit) is a later, separate layer (Sigstore attestations, once every build runs in CI).

## 4. The signature

`SHA256SUMS.sig` is two lines:

```text
untrusted comment: chtypes artifacts, ed25519 key deb275922dbff76e
<base64 of the 64-byte ed25519 signature over the bytes of SHA256SUMS>
```

The key id is the first 16 hex characters of sha256 over the raw 32-byte public key. The release key today:

|                       |                                                                       |
| --------------------- | --------------------------------------------------------------------- |
| public key (raw, hex) | `fdb5f06a8d4c9918d049a5f1748fa2e3b3238c3f2000986d5bb9e31beff778fc`    |
| key id                | `deb275922dbff76e`                                                    |
| private half          | held offline; only the release pipeline signs, and it never leaves it |

Every SDK embeds that public key as a constant and verifies with it. Policy, same in all four:

- `CHTYPES_TRUSTED_KEYS=<hex>[,<hex>…]` **replaces** the embedded list — for a mirror or a custom registry signed by someone else.
- `CHTYPES_ALLOW_UNSIGNED=1` skips step 0 and prints one loud warning naming the source. Never the default; never silent.
- Once files are in a registry directory, the loader trusts the directory. Verification is a fetch-time policy, not a load-time gate — exactly as a runtime trusts `node_modules`. Custom or modified artifacts are installed by copying them in.

Reference vector (openssl, `-rawin`): message `68656c6c6f0a` ("hello\n") signs under the key above to `0fee686f7ed7c64b86a7dce0ffd66b15d1504178153c3b0cc118e2c9456afa6d3e2e55019eca8f75e44ab507d65b0714523e92c7f92452821930691212e76c04`; flipping one byte of the message fails.

## 5. Pinning

`fetch --lock chtypes.lock` records, per `<os>-<arch>/<clickhouse_version>` (the EXACT patch installed or found installed — chtypes#284), the asset file and sha256, and the ABI revision the selected row carried. `fetch --frozen` (or `ensure` given both a lock and `frozen`) refuses anything else with `CHTYPES_ARTIFACT_PINNED`. Without `--frozen`, `--lock` only records: an existing entry is replaced by what the fetch installed, and nothing is removed — that is how a lock is re-pinned, and how it accumulates one entry per exact patch a project has ever locked. The file is JSON, schema 2:

```json
{"schema": 2, "artifacts": {"linux-arm64/26.8.15.10-lts": {"file": "chtypes-26.8.15.10-lts-linux-arm64.tar.gz", "sha256": "…", "abi_revision": 6}}}
```

That is the lockfile model every package manager uses: trust on first fetch, byte-identical thereafter, and CI fails on drift — now keyed so that several patches of one line coexist in the same lock file.

**Schema 2 is keyed by exact patch, not by line, because a lock that can only say "linux-arm64/26.8" cannot pin two patches of 26.8 at once** — and pinning an older patch while a newer one is served (the headline case below) is exactly what this schema exists for. `abi_revision` is always written now (it was optional and additive under schema 1). A schema-2 entry that carries none — a hand edit — is treated as schema 1's "records no ABI revision" case, below.

**Reading schema 1.** Every SDK still reads it: a schema-1 entry `<os>-<arch>/<minor>` is read as if it were `<os>-<arch>/<clickhouse_version>`, where `<clickhouse_version>` comes from the entry's own `file`, under the asset-name grammar ([`artifacts.md`](artifacts.md), `chtypes-<version>-<os>-<arch>[-b<N>].tar.gz`) — and that version's line must equal the key's `<minor>`, or the entry does not parse (the lock is then unreadable, naming the bad entry, exactly as a malformed entry does today). **The SDK always writes schema 2**: the first write into a schema-1 file converts every entry to schema-2 keys in the same write, because schema 1 cannot record two patches of one line and writing schema 1 "when it still fits" would make the file's format depend on its content. Any schema number but 1 or 2 is refused, naming both accepted numbers.

**`--frozen` checks the revision FIRST, before comparing file and sha256**, for every entry a request might select. Three cases, per entry:

- **The entry names an ABI revision, and it is not this SDK's own** — `CHTYPES_ARTIFACT_PINNED` (exit 1, the same code as always), naming both numbers and the remedy, e.g.:

  ```text
  chtypes.lock pins linux-arm64/26.8.15.10-lts at ABI revision 5; this SDK speaks ABI revision 6 — re-lock with: python -m chtypes fetch 26.8.15.10-lts --lock chtypes.lock
  ```

  This fires before the release is consulted. Without it, a lock made for another revision fails as a drifted pin (`CHTYPES_ARTIFACT_PINNED`, when the release has a row at this SDK's revision) or as an unpublished patch (`CHTYPES_ARTIFACT_UNPUBLISHED`, when it has none), and neither message names the cause.
- **The entry names no ABI revision at all** (written by an older SDK, or by hand): the file/sha256 pin enforces as before, but whenever that ends in `CHTYPES_ARTIFACT_PINNED` or `CHTYPES_ARTIFACT_UNPUBLISHED`, one sentence is appended naming the gap: `chtypes.lock records no ABI revision (written by an older SDK); this SDK speaks ABI revision 6 — re-lock with: …`. A revision mismatch is never silently accepted just because the lock predates knowing about one.
- **The entry names this SDK's own ABI revision** — installs exactly as it always has.

**Selecting a candidate under `--frozen`.** The lock is the candidate set, in place of the release's rows: a patch spelling takes the entry whose key matches it (Decision 7 applies — a channel-less spelling matches a pinned patch on any channel); a line spelling takes the newest PINNED patch of that line; `--all` takes the newest pinned patch of every line the lock pins for the platform. If there is no candidate for the request, that is `CHTYPES_ARTIFACT_PINNED`, naming the platform and the spelling — the lock pins nothing for it.

Once a candidate is chosen and its revision checked, its `file` is looked up in the verified release exactly as today (`index.json` and `SHA256SUMS` must both list it, at this SDK's own ABI revision, with the entry's own sha256 — any disagreement is `CHTYPES_ARTIFACT_UNPUBLISHED` or `CHTYPES_ARTIFACT_CORRUPT` as before) and that EXACT row installs — **even while a newer patch, or a higher build of the same patch, is served.** The refusal "pins X but the release offers Y" is gone: selection never looks at unpinned rows any more, so a newer release never becomes drift in something that was pinned. `--all --frozen` installs the newest pinned patch of each line the lock pins for the platform; a line the release has that the lock does not pin is simply not installed (named in one progress note), which is the `npm ci` reading of a frozen install rather than a refusal.

`--offline --frozen` is unaffected: offline never reads the lock at all, in any of the four bindings (chtypes#284 fixed TypeScript, which used to). A line spelling is satisfied by the newest INSTALLED patch of the line in the destination; a patch spelling by that patch (Decision 7 applies). Each is verified against its own manifest. To check a pinned patch offline, spell the patch.

**Upgrading to an SDK at a new ABI revision: re-lock.** The remedy is always the same fetch `--lock` invocation the message names — run it once, without `--frozen`, and commit the new lock.

**What a 0.4.x SDK does with a schema-2 lock: refuses it, never misreads it.** Released SDKs through 0.4.x speak only schema 1 — none of them treats a schema-2 file as an empty or partial lock, and none silently accepts a shape it does not understand:

| Binding    | Message                                                             | When                                                                                                                                                                                                                        |
| ---------- | ------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Go         | `chtypes: <lock>: lock schema 2 is not 1 — this SDK cannot read it` | Under `--frozen`, before any download (`CHTYPES_ARTIFACT_PINNED`). Under `--lock` without `--frozen`, the lock is read only after the install, so the artifact installs and then the command fails with the lock unchanged. |
| Python     | `ValueError: chtypes: <lock> is not a chtypes lock file (schema 1)` | Before any download; the CLI exits 2.                                                                                                                                                                                       |
| TypeScript | `ChtypesError: chtypes: lock file <lock> is not schema 1`           | Before any download.                                                                                                                                                                                                        |
| Rust       | (see the binding's own CHANGELOG)                                   | Before any download.                                                                                                                                                                                                        |

Teams sharing one lock file across SDK versions upgrade together, or keep separate lock files per SDK major.

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
| TypeScript | `npx @wavehouse/chtypes fetch 26.8` (`bin: chtypes`)                   | `ensure('26.8', opts)`              |
| Python     | `python -m chtypes fetch 26.8` and the `chtypes` console script        | `chtypes.ensure('26.8', **opts)`    |
| Rust       | `cargo install chtypes` → `chtypes fetch 26.8` (the crate's `[[bin]]`) | `chtypes::ensure("26.8", &opts)`    |
| Go         | `go run github.com/wave-rf/chtypes/go/cmd/chtypes@latest fetch 26.8`   | `chtypes.Ensure(ctx, "26.8", opts)` |

`ensure` is idempotent: installed-and-verified is a no-op, otherwise it fetches through §3. It returns the directory the patch landed in — `<registry>/<minor>/` for the patch a line request selects, `<registry>/patches/<minor>/<clickhouse_version>/` for any other exact patch (§1). Exit codes: 0 ok · 1 verification failed · 2 usage · 3 source unreachable · 4 not published for this platform/line/patch.

`list` and `verify` report **per installed patch**, not per line: the flat install at `<minor>/` and every other patch nested at `patches/<minor>/<clickhouse_version>/` are each their own row (chtypes#284; §1's "Two levels"). Of what the release offers, `list` shows only the rows at the SDK's own ABI revision — what a fetch could install (§2). When that hides any of the platform's rows, it adds one line naming them, in all four SDKs the same:

```text
<N> row(s) at ABI revision(s) <S>[, <S>…] not shown; this SDK speaks <R>
<M> row(s) that record no ABI revision (built before revisions were recorded) not shown; this SDK speaks <R>
<N> row(s) at ABI revision(s) <S>[, <S>…] and <M> row(s) that record no ABI revision (built before revisions were recorded) not shown; this SDK speaks <R>
```

The first form is for rows at other revisions, the second for rows that record none, the third for both. When nothing was hidden, the line is not printed.

**Lazy fetch on first open** is opt-in: the registry constructor's `autofetch` option, or `CHTYPES_AUTOFETCH=1`. Off, a missing line or patch is the error in §7 (a patch instead falls back within its line, warned — [`multi-version.md`](multi-version.md#resolution-the-exact-patch-else-its-line--never-another-line) has the resolution order). On, opening a missing line or patch tries the PATCH first, then the line, then runs `ensure` (one process-wide lock so concurrent opens fetch once). Off by default because a production process must not begin a 250 MB download inside a request.

## 7. The one error

The message a missing artifact produces, the directories it names and the rule that a version is never a nearest match are in [`artifacts.md` → The one error](artifacts.md#the-one-error). What belongs here is the code every binding carries with it:

Codes, shared: `CHTYPES_ARTIFACT_MISSING`, `CHTYPES_ARTIFACT_UNTRUSTED`, `CHTYPES_ARTIFACT_CORRUPT` (any hash mismatch), `CHTYPES_ARTIFACT_PINNED`, `CHTYPES_ARTIFACT_UNPUBLISHED`, `CHTYPES_SOURCE_UNREACHABLE`.

`CHTYPES_ARTIFACT_UNPUBLISHED` for a line, an exact patch or `--all` that the release has only at another ABI revision (§2) names the line or patch, the platform, the SDK's own revision, and what the release does serve for it — the revision(s) its rows carry, that its rows record no ABI revision (built before revisions were recorded), or that it has no row for it at all — for example, from an SDK at revision `<R>` against a release that has the line only at `<S>`:

```text
chtypes: no artifact for ClickHouse line 26.8 on linux-arm64 at ABI revision <R> (this SDK's) at <source>: the release has that line for linux-arm64 only at ABI revision <S>; at ABI revision <R> the release has: nothing. [CHTYPES_ARTIFACT_UNPUBLISHED]
```

and, when the line's only rows record no revision at all — darwin-arm64 24.8 on the rolling release today:

```text
chtypes: no artifact for ClickHouse line 24.8 on darwin-arm64 at ABI revision <R> (this SDK's) at <source>: the release has that line for darwin-arm64 only in rows that record no ABI revision (built before revisions were recorded); at ABI revision <R> the release has: … [CHTYPES_ARTIFACT_UNPUBLISHED]
```

The wording differs a little per binding (Rust folds the same facts into its `offered` field); the facts it names do not.

Under `--frozen`, both `CHTYPES_ARTIFACT_PINNED` and this `CHTYPES_ARTIFACT_UNPUBLISHED` can carry one more sentence about the lock's own recorded ABI revision — §5 has the messages.

**`fetch`/`ensure` (and `scripts/fetch.sh`) never fall back to a same-line patch, even after chtypes#284: a hard requirement stays a hard requirement.** `fetch 26.8.15.10-lts` that the release does not publish is `CHTYPES_ARTIFACT_UNPUBLISHED`, exactly as above, whether or not the release publishes some OTHER patch of 26.8 — never a quiet substitute. Only the **registry's** `For`/`resolve` falls back, and only for a load, never for a fetch: [`multi-version.md`](multi-version.md#resolution-the-exact-patch-else-its-line--never-another-line) has that rule and the warning it prints.

## 8. Deferred: system packages (brew, apt)

A `brew install chtypes` or a Debian package would pre-seed the system locations in §1 and keep them updated by the package manager's own mechanism, and would carry the fetch command as a standalone tool. Deferred on 2026-09-09 until the four in-package commands exist; nothing in §1–§7 needs to change to add it — the search path already has its slots.

## 9. Test vectors

`tests/fixtures/fetch/` holds miniature releases the four implementations are tested against through `--url file://…`: `signed/` (valid; tiny fake libraries whose manifests hash correctly), `bad-signature/`, `tampered-tarball/`, `sums-index-mismatch/`, `unsigned/`, `two-builds/`, `two-revisions/`, `two-patches/`, and a `chtypes.lock` that pins `signed/` — committed before `abi_revision` existed, so its entries carry none; a suite that needs one at a revision (matching or mismatched) builds it at test time rather than editing this fixture.

`two-builds/` is the rebuild case: one ClickHouse version published twice for each platform, so resolving a line exercises the build tie-break rather than the version alone. `expected.json` records it under `builds.cases` — deliberately apart from `verdicts`, which name one asset per line — and each case says which build must install and which it supersedes. The proof is the installed library's bytes: both rows share a `clickhouse_version`, so only `library_sha256` can tell them apart. They are generated by the build tooling and never edited by hand. An SDK's fetch suite must pass all of them with the same verdicts and codes.

The fixtures carry whatever ABI revision they were generated at, which is not always the SDK's own: during a revision cutover the SDK moves first. `expected.json` declares it, as `fixtures_abi_revision` — the revision every fixture's rows carry except `two-revisions/`'s — and each suite reads that field (failing loudly when it is missing) and selects at it through an internal, test-only override, never a typed number and never part of this contract. The override changes which rows are eligible and nothing else; the default directory stays `abi<R>/` for the SDK's own revision. `scripts/fetch.sh` needs no override: `--abi-revision N` is its documented flag.

`two-revisions/` is the revision case: one release whose two lines each carry two builds at two ABI revisions, mirrored — on one line the higher build sits at the higher revision, on the other at the lower — so at EACH revision one line resolves differently filtered than unfiltered. `expected.json`'s `revisions` records, per platform and line, the row that must install at `low_revision`, at `high_revision`, and the row an unfiltered resolve would take, and names the `discriminating_lines`. Every suite drives each case at both revisions through its override, asserts that on the discriminating lines the pick differs from the unfiltered one, and checks that one revision past `high_revision` is `CHTYPES_ARTIFACT_UNPUBLISHED`. `scripts/fetch.sh`'s cases run from the Python suite, through `--abi-revision`.

`two-patches/` is the exact-patch case: one line published at two exact patches at the same ABI revision on every platform, each with its own `library_sha256`. `expected.json`'s `patches` block names both patches, the newest one, and an exact miss that lies strictly between them and resolves to the newest served patch of the line. `scripts/fetch.sh`'s suite (`python/tests/test_fetch_sh.py`) drives it directly: an exact request for EITHER served patch installs nested under `patches/<line>/<version>/` — even the newest, into an empty destination, since only a line spelling or `--all` ever writes the flat slot (§1) — except when that exact patch already sits flat and verifies, which is a no-op reporting the flat directory; a channel-less spelling of either matches under Decision 7; the exact miss is `CHTYPES_ARTIFACT_UNPUBLISHED`, never a same-line substitute (§7); and a line fetch that changes the flat occupant demotes the outgoing patch into `patches/<line>/<its version>/`, byte-identical, still resolvable with no re-fetch. Each binding's own suite drives the same fixture for its registry-level fallback and warning (`Resolve`/`resolve`).

## 10. Decisions (2026-09-09, when the four implementations merged)

Where the four implementations diverged, one rule was chosen and the odd ones out were changed the same day; where reconciling would have cost more than it is worth today, the difference is recorded here instead of hidden. These bind §1–§7.

1. **`--frozen` without `--lock` reads `./chtypes.lock`** (relative to the working directory), in the library call as well as the CLI. A lock file that does not exist under `--frozen` is `CHTYPES_ARTIFACT_PINNED` (exit 1): nothing is pinned, so nothing is installed — the same verdict as a line the lock does not pin. It is decided after the release loads, so an untrusted or unreachable source is reported ahead of it. (Python refused `--frozen` without `--lock` as a usage error, TypeScript's library call did too, and Rust's missing-lock error carried no code; all three changed.)
2. **An explicit destination is the only place "already installed" is looked for.** `--dest <dir>` / the `dest` option means that directory; an install elsewhere on the §1 path does not satisfy it (a container build's `--dest /opt/chtypes/artifacts` must not be satisfied by the builder's own cache). All four did this. _Known difference, not reconciled:_ without a `dest`, Rust also accepts an install anywhere on the §1 search path (what a registry would find); Go, Python and TypeScript look only in the directory fetch would write to. It shows only when a line sits in a later slot than the write directory — the system slots are empty today.
3. **A fetch for another platform never writes into `CHTYPES_REGISTRY`.** That variable names a directory this host dlopens from, so it is on the search path for the host's platform only; `fetch --platform <other>` writes to that platform's own per-user cache (`…/chtypes/artifacts/abi<R>/<os>-<arch>`), and `--dest` still wins. (Python wrote into `CHTYPES_REGISTRY`; changed.) _Known difference:_ Go and TypeScript also read `CHTYPES_TARGET` as the default `--platform` (see [`artifacts.md`](artifacts.md)); Python and Rust take `--platform` only.
4. **An explicit registry directory that lacks a line falls through** to the rest of the §1 path, in every SDK's search-path constructor. In Rust that constructor is `Registry::from_search_path()` / `Registry::from_search_path_with(RegistryOptions { dir, .. })`; `Registry::new(dir)` stays the single-directory loader (that directory only, `Error::ArtifactMissing` for a line it lacks), by design. **A named directory that does not exist is refused at construction in all four**, unless autofetch is on, in which case it is the destination-to-be. Construction reads manifests and `dlopen`s nothing everywhere; see [`multi-version.md`](multi-version.md#how-it-works-and-what-it-costs).
5. **`CHTYPES_ALLOW_UNSIGNED=1` skips step 0 entirely**: `SHA256SUMS.sig` is not even fetched, so a present-but-wrong signature installs, behind the one loud warning naming the source. `SHA256SUMS` itself is still required and steps 1–4 still run — a tampered tarball is still refused. All four agree.
6. **"Installed" is decided against the signed release.** A plain `ensure` / `fetch` of an installed line reads `SHA256SUMS`, its signature and `index.json` — never the tarball — and reports installed when the library in place hashes what that listing says (§3). `--offline` is the one path that reads no source: an installed line that hashes what its own `manifest.json` says is the answer; anything else is `CHTYPES_SOURCE_UNREACHABLE` (a damaged install, `CHTYPES_ARTIFACT_CORRUPT`). (Rust's plain `ensure` was local-only; changed.)
7. **An exact patch spelled without its channel matches that patch on any channel**: `26.8.15.10` and `v26.8.15.10` take `26.8.15.10-lts`; a spelled channel (`26.8.15.10-stable`) matches only itself, and a miss is `CHTYPES_ARTIFACT_UNPUBLISHED`, never the neighboring patch. (Go required the exact string; changed.)
8. **`CHTYPES_ARTIFACT_PINNED` exits 1**, like `…_UNTRUSTED` and `…_CORRUPT`: a verification failure. Only `CHTYPES_SOURCE_UNREACHABLE` (3) and `CHTYPES_ARTIFACT_UNPUBLISHED` (4) have exit codes of their own. All four agree.
9. **The "Install it" line of §7, per SDK** — two spaces after the colon, then the SDK's own fetch command and the line: Go `go run github.com/wave-rf/chtypes/go/cmd/chtypes@latest fetch <line>` · Python `python -m chtypes fetch <line>` · TypeScript `npx @wavehouse/chtypes fetch <line>` · Rust `cargo install chtypes && chtypes fetch <line>`.
10. **Python's `Registry()` walks the §1 search path** like the other three. It once required an explicit path or `CHTYPES_REGISTRY`; that was the last constructor that did not, and it changed on the same day.
11. **(2026-09-30, chtypes#284, exact-patch resolution.) Decision 7 applies everywhere a patch is matched, not only at the registry's online resolution.** `scripts/fetch.sh`'s own selector used to compare a patch spelling by string equality — `26.8.15.10-lts` installed, `26.8.15.10` was `CHTYPES_ARTIFACT_UNPUBLISHED` — and Python's offline path refused a channel-less spelling against an installed channeled patch (`SourceUnreachableError`, "…holds ClickHouse 26.8.15.10-lts, not the 26.8.15.10 asked for"). Both are now Decision 7, same as the other three always were.
12. **Autofetch of a patch spelling fetches that patch first, in all four.** Go, TypeScript and Rust used to fetch the LINE even for a patch request — quietly serving the release's newest patch instead of the one actually asked for; Python already fetched the patch and simply failed `CHTYPES_ARTIFACT_UNPUBLISHED` if the release did not have it. All four now try the exact patch, then fall back to the newest patch of the line only once the patch is CONFIRMED unpublished — never on a verification or network failure, which retries instead.
13. **A lock is enforced only under `--frozen`, in all four.** Python used to refuse `ensure(..., lock=…)` against an existing entry even without `--frozen`, so #282's own printed re-lock remedy — run the same command again, without `--frozen` — was itself refused by the check it was meant to fix. `--lock` without `--frozen` only ever records now, in every binding (the fix landed ahead of this work, in #288).
14. **Offline never reads the lock, in any of the four.** TypeScript's `--offline --frozen` used to require a lock file and refuse an unpinned line even offline (`CHTYPES_ARTIFACT_PINNED`); Go, Python and Rust never did. Decision 6 — offline fails only with `CHTYPES_SOURCE_UNREACHABLE` or `CHTYPES_ARTIFACT_CORRUPT` — now holds without exception.
15. **The autofetch memo is the same shape in all four**: in-flight dedupe (concurrent opens of one spelling fetch once), plus remembering a success and a confirmed `CHTYPES_ARTIFACT_UNPUBLISHED` for the rest of the process, keyed by (destination, the exact spelling parsed). A transient failure — `…_UNTRUSTED`, `…_CORRUPT`, `…_PINNED`, `CHTYPES_SOURCE_UNREACHABLE` — is never remembered, so the next call retries. Before this: Go remembered destination+line and forgot failures; Python remembered (destination, minor) successes only; TypeScript deduplicated in-flight only; Rust made one attempt per line ever, with no retry after a transient failure.
