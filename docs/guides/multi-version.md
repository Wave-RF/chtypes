# Several versions in one process, and thread-safety

One process can hold as many ClickHouse versions as you have artifacts for, each answering with its own semantics, at the same time. That is the whole reason chtypes is a `dlopen`'d artifact rather than a linked library — a fleet does not run one ClickHouse version, and a gateway in front of it needs to answer for each tenant's actual server.

## How it works, and what it costs

Every artifact is opened with `RTLD_NOW | RTLD_LOCAL`. `RTLD_LOCAL` is the entire mechanism: it keeps each library's ClickHouse symbols private, so two builds that both define `DB::DataTypeFactory` never collide. A loader that used `RTLD_GLOBAL` would appear to work and then answer with the wrong version's semantics — which is worse than failing.

The cost, measured: **about 120 MB resident per loaded version** (160–300 MB on disk). Load the lines you serve, not every line published. Seven artifacts in one directory, opened from one process: **1.096 s and 351 MB resident** to open them all, against **1 ms and 66.8 MB** to open none.

**Each patch is its own library, not only each line (chtypes#284).** A process that follows its servers through upgrades loads one library for every EXACT patch it is asked for — two patches of one line, `26.8.14.3-lts` and `26.8.15.10-lts` say, are two separate `dlopen`s, each with its own `chs_init`, DateLUT and refuse-list, at about the same ~120 MB each — and no binding ever unloads one on its own. `Libraries()` lists what is actually open, in full numeric version order, not deduplicated by line. Budget for the patches you actually serve at the same time, not the lines, and after a fleet-wide upgrade completes, restart or roll long-lived processes so the patches nobody asks for any more are released. That a second patch of the SAME line costs about the same as a different line is **inferred** — it is the same build class — not separately measured.

**Loading is lazy per line, in every binding and in every constructor.** Constructing a registry — with a directory or without one — reads `manifest.json` files and dlopens nothing. **Nothing in these libraries opens an artifact except a request for a specific version, or an explicit `preload`.** Not `versions()`, not `libraries()`, not a membership test, not formatting the registry.

This is a single-sourced statement of fact; nothing else in the docs restates it.

### Opening a pinned set up front: `preload`

A deployment that knows which lines it serves can open them at construction, and find out at construction if one is missing. It is a constructor option in all four, it takes a **list of lines** rather than "everything in the directory" — a registry directory is whatever a fetch left behind — and an entry no directory on the search path holds raises the ordinary artifact-missing error, earlier than it otherwise would. It never fetches, even with autofetch on.

| SDK        | spelling                                                                                      |
| ---------- | --------------------------------------------------------------------------------------------- |
| Go         | `chtypes.NewRegistry(dir, chtypes.WithPreload("26.8", "26.7"))`                               |
| Python     | `Registry(dir, preload=["26.8", "26.7"])`                                                     |
| TypeScript | `new Registry(dir, { preload: ['26.8', '26.7'] })`                                            |
| Rust       | `Registry::open(dir, RegistryOptions { preload: vec!["26.8".into()], ..Default::default() })` |

Checksum verification (`WithVerifyChecksums` / `verify_hashes` / `verifyChecksums` / `verify_checksums`) is a policy on the registry and not a property of that list: **a library's checksum is computed immediately before that library is dlopen'd, and at no other time** — at construction for the preloaded lines, at first use for the rest, never for a line nobody asks for.

`versions()` lists every line the registry can answer for, loaded or discovered; `libraries()` lists the ones it has actually opened.

<details open><summary><b>Go</b></summary>

```go
reg, _ := chtypes.NewRegistry("")   // the search path; nothing dlopen'd yet
old, _ := reg.For("24.8")           // loads 24.8
new, _ := reg.For("26.7")           // loads 26.7, beside it
fmt.Println(reg.Versions())         // every line on the search path
```

</details>

<details><summary><b>Python</b></summary>

```python
registry = Registry()                 # the search path; nothing dlopen'd yet
old = registry.for_version("24.8")    # loads 24.8
new = registry["26.7"]                # __getitem__ is the same call
print(registry.versions())
```

</details>

<details><summary><b>TypeScript</b></summary>

```ts
const registry = new Registry();
const old = registry.for('24.8');
const recent = registry.for('26.7');
console.log(registry.versions());
```

</details>

<details><summary><b>Rust</b></summary>

```rust
let registry = Registry::from_search_path()?;  // scans manifests, opens nothing
let old = registry.for_version("24.8")?;       // Arc<Library>
let recent = registry.for_version("26.7")?;
println!("{:?}", registry.versions());
```

`Registry::new(dir)` is the other constructor: a single directory, lazy in the same way, answering `Error::ArtifactMissing` for a line it lacks. `from_search_path` is the one that walks the path, and it is fallible because the scan it runs can be — an unreadable directory, or nothing installed anywhere.

</details>

## Resolution: the exact patch, else its line — never another line

A registry resolves a **minor line** (`26.8`) or an **exact patch** (`26.8.15.10-lts`). `For`/`for_version`/`for`/`open` return the `Library`; the new `Resolve`/`resolve`/`resolve`/`resolve` (chtypes#284) return a small `Resolution` carrying the same `Library` plus what was requested, what actually loaded, and whether it was exact:

<details open><summary><b>Go</b></summary>

```go
r, _ := reg.Resolve("26.8.16.1")   // not built yet; the release has 26.8.15.10-lts
fmt.Println(r.Requested, r.Version, r.Exact)   // 26.8.16.1 26.8.15.10-lts false
```

</details>

<details><summary><b>Python</b></summary>

```python
r = registry.resolve("26.8.16.1")
print(r.requested, r.version, r.exact)   # 26.8.16.1 26.8.15.10-lts False
```

</details>

<details><summary><b>TypeScript</b></summary>

```ts
const r = registry.resolve('26.8.16.1');   // synchronous, never fetches
console.log(r.requested, r.version, r.exact);   // 26.8.16.1 26.8.15.10-lts false
```

</details>

<details><summary><b>Rust</b></summary>

```rust
let r = registry.resolve("26.8.16.1")?;
println!("{} {} {}", r.requested, r.version, r.exact);   // 26.8.16.1 26.8.15.10-lts false
```

</details>

**A LINE request never falls back — it names what it wants and that never changes mid-process.** The first time a line is resolved on a registry, that resolution — the newest patch of the line found on the search path — is PINNED for the life of that registry; a later `Load` or fetch elsewhere on the path does not move it. `Exact` is always `true` for a line request: it asked for "a patch of this line" and got one.

**A PATCH request resolves to that exact patch when it is installed, or published with autofetch on. Otherwise it falls back to the newest installed or published patch of the SAME line — never another line — flagged `Exact = false`, and warns once per (requested, actual) pair per process:**

```text
ClickHouse 26.8.16.1 is not installed for darwin-arm64; using 26.8.15.10-lts, the newest installed patch of 26.8. Behavior can differ between patches. If 26.8.16.1 is published, install it with: python -m chtypes fetch 26.8.16.1
```

The fallback is re-checked, not memoized: a patch installed later — by `fetch`, or by another process — is picked up from the next call on, without a restart. `fetch`/`ensure` never do this substitution themselves: a fetch for an unpublished exact patch is still `CHTYPES_ARTIFACT_UNPUBLISHED` ([`fetch.md`](fetch.md#7-the-one-error)); only the registry's resolution falls back, and only for a load.

There is deliberately **no cross-line fallback, ever**, and the reason is worth stating plainly: version behavior is not monotonic. 25.10 rejects a DEFAULT that both 25.8 and 26.6 accept. A different line's answer is not an approximation of the right one — it is a different answer, and handing it back quietly would make chtypes the thing it exists to prevent. Patch-to-patch drift within one line is real too (a DateTime64/Time64 saturation backport changed a CAST mid-line), which is exactly why the same-line fallback above is flagged and warned rather than silent.

## Thread-safety

The C contract underneath is short: concurrent calls are safe on **distinct** handles; a single handle is single-threaded; initialization and settings writes exclude everything else on that image.

`set_default_settings` is the sharp one. It replaces a process-global that the row path reads **by reference**, so an overlapping call is a use-after-free rather than a stale read. Every binding takes an exclusive lock for it.

|                | how it is enforced                                                                                                                                                                                                                                                                                                                                                                                             |
| -------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Go**         | one mutex per schema handle; a per-`Library` RWMutex read-held by every call; `chs_init` runs exactly once per artifact path, before the `Library` is visible. A dlopen'd `Library` **structurally cannot** call `SetDefaultSettings` — the symbol is deliberately absent from its function-pointer table.                                                                                                     |
| **Python**     | `ctypes` releases the GIL for the whole duration of a foreign call, so the GIL is _not_ the exclusion. One writer-preferring readers-writer lock per loaded **image** (row calls, compiles and declarations hold it shared; `set_default_settings` and `close` hold it exclusively) plus one plain lock per `Schema`.                                                                                          |
| **TypeScript** | every call is synchronous on the JS thread, so ordinary single-threaded Node needs no locking. `setDefaultSettings` and `shutdown` carry a reentrancy tripwire and refuse loudly if another chtypes call is on the stack.                                                                                                                                                                                      |
| **Rust**       | a per-image `RwLock`, held SHARED by every ordinary call and EXCLUSIVE only by `set_default_settings`/`shutdown` (chtypes#364). `Library` and `Registry` are `Send + Sync` — share them freely. `Schema` is `Send` and deliberately **not** `Sync`, so the type system refuses to let one handle reach two threads — this crate's own per-handle lock, enforced structurally rather than with a runtime mutex. |

Python's locks are interned per **image**, not per object: `dlopen` refcounts one mapping per file, so two `Registry` instances over one directory share the C globals. That is also the key `chs_init` deduplicates on, which is why a second init with a different timezone is refused rather than silently re-timezoning a live library.

Measured under contention in Python's own suite: 27,770 batch reads across 8 threads against 566 concurrent settings swaps, every answer byte-identical to the uncontended one, no exceptions and no deadlock. Measured under the same shape in Rust's own suite (`concurrency_contention.rs`): concurrent `rows()` reads across 8 threads against concurrent `set_default_settings` swaps, every answer byte-identical to the uncontended one, no panics and no deadlock.

**chtypes#364 — Rust's per-image lock, before and after.** The crate used a single `Mutex` per image, serializing every call regardless of handle, until this was measured directly: 8 distinct `Schema` handles on one image ran `parse_block` (a fixed CPU-bound workload, no I/O) at **N=1 24,716/s → N=8 18,288/s, 0.74×** — contention, not parallelism (`measured`, a GitHub-hosted 4-core `linux-amd64` runner, ClickHouse 25.8.33.6-lts, ABI revision 6). Replacing the `Mutex` with the per-image `RwLock` above — shared by every ordinary call, exclusive only by `set_default_settings`/`shutdown` — measured **N=1 54,812/s → N=8 140,271/s, 2.56×** on the same runner shape immediately after (`measured`; N=1's own throughput differed between the two separate CI runs — noise between runs, not a property of either lock — so the RATIO is the load-bearing number). 2.56× on 4 cores is consistent with Go's own process-ceiling note elsewhere in this file: a handle-bound workload does not clear `cores×` on a box with that many cores also busy with other work.

**Parallelism comes from more schemas, not from sharing one.** That is true in every binding; Rust is simply the one that will not compile the alternative.

### ParseBlock throughput scales with handle count again, on every line (chtypes#78)

_Updated 2026-09-30 — not a safety change; the guarantee above still holds exactly as written: concurrent calls on distinct handles are safe, one handle is single-threaded._

A dated note stood here through 2026-09-29 saying that, on Linux, ClickHouse lines above 26.2 had stopped gaining ParseBlock throughput from adding handles inside one process. That no longer holds for current artifacts. **chtypes#78** has the artifact producer's fix and its full measurement — cited here rather than restated: their ParseBlock probe (`measured`, the producer's numbers) showed 26.6's N8/N1 throughput going from 3.60× to 7.95× after the fix, with every affected line's post-fix ratio in the 7.74×–8.02× range.

A downstream consumer's own, independent measurement adds a second, more recent data point: **5.92× at N=8 with its own handles, against a 0.99× process-wide-mutex gate, on revision-6 26.6 artifacts, linux-arm64** (`measured (reporter)`; an OrbStack Linux VM on Apple silicon, ~3.5 host cores busy elsewhere, a process ceiling of ~6× in that environment — amd64 not measured).

Size concurrency inside one process by handles, on every line — there is no longer a reason to size it by process count instead.

### One boundary worth naming: `worker_threads`

In Node, two JS threads share one dlopen'd image and one set of C globals, which no per-isolate counter can see. Seed default settings **before** starting workers, or serialize the seed yourself, and do not share a `Schema` across workers.

### Go: sizing concurrent handles

Nothing in chtypes caps how many handles a caller runs at once — size a caller-side semaphore to `runtime.GOMAXPROCS(0)` yourself. Parallelism beyond the number of OS threads Go will actually schedule for your goroutines buys queueing, not throughput.

`GOMEMLIMIT`, if set, sees only Go's own live heap. The C++ library behind a `dlopen`'d artifact keeps its own heap — the compiled schema, open filters, and the DEFAULT evaluator's working set — entirely invisible to it, so a `GOMEMLIMIT` sized from `runtime.MemStats` or `pprof` alone will trip GC far too late, or never, against memory the Go runtime cannot see. Leave headroom for it, sized from the figures below rather than from Go's own heap alone.

**Measured (reporter)**, linux-arm64, a 6-column DDL with a per-claim `DEFAULT`: opening a library costs +92.7 MiB resident (ClickHouse 26.6) / +89.7 MiB (25.8); a compiled-but-unused handle, 39–43 KiB; first use of each of the first hundred handles, 89–115 KiB; 256 warm handles (100 of them with a filter attached), +23.9/+24.3 MiB total (~96–97 KiB each); 1000 warm handles, +40.6/+55.5 MiB (~41–57 KiB each). darwin-arm64, a 5-column DDL: ~45 KiB per warm handle. `CompileDDL`'s own cost: a median of ~971 µs for 40 columns, ~12 µs for 4 columns.

**`Close` does not lower RSS.** The same measurement closed all 1000 handles above and recovered only 0.7–0.8 MiB — the C++ allocator keeps its arena. Size a long-lived process for its peak handle count, not its steady-state one.

**Benchmark the library that is actually loaded, not the version you asked for.** A patch request can fall back to a different patch of the same line (see [Resolution](#resolution-the-exact-patch-else-its-line--never-another-line) above), and a line request can pick up a newer patch after a fetch elsewhere on the path. Assert `Library.Version` (or `Resolution.Version`) in every cell of a benchmark table, so a number can never silently mix builds.

## Teardown

There is a `shutdown` in three of the four bindings, and it joins the DEFAULT evaluator's background threads. `chs_init` registers it with `atexit`, so **an ordinary process needs no call at all**.

It is required in exactly three situations: before any `dlclose`, when the host controls its own teardown order, and in tests that must not depend on `atexit`. Close every schema first; the calls are idempotent.

Python and Rust refcount it per image, so the **last** close per image runs `chs_shutdown` — two registries over one directory share every image, and closing one must not tear the evaluator down under the other.

Go has no teardown surface at all, deliberately: it never `dlclose`s a loaded artifact, so `chs_shutdown` is never owed.

## Next

- [`artifacts.md`](artifacts.md#loading) — what the loader checks before a library is usable.
- [`discovery.md`](discovery.md) — how you learn which version a given deployment actually needs.
- [`../support.md`](../support.md) — which lines exist to be loaded.
