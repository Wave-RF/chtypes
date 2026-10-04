# Several versions in one process, and thread-safety

One process can hold as many ClickHouse versions as you have artifacts for, each answering with its own semantics, at the same time. That is the whole reason chtypes is a `dlopen`'d artifact rather than a linked library — a fleet does not run one ClickHouse version, and a gateway in front of it needs to answer for each tenant's actual server.

## How it works, and what it costs

Every artifact is opened with `RTLD_NOW | RTLD_LOCAL` ([`reference/abi-v1.md`](../reference/abi-v1.md#loading-a-library)). `RTLD_LOCAL` is the entire mechanism: it keeps each library's ClickHouse symbols private, so two builds that both define `DB::DataTypeFactory` never collide. A loader that used `RTLD_GLOBAL` would appear to work and then answer with the wrong version's semantics — which is worse than failing.

The cost, measured on 0.x artifacts: **about 120 MB resident per loaded version** (160–300 MB on disk). Load the lines you serve, not every line published. Seven artifacts opened from one process took **1.096 s and 351 MB resident**, against **1 ms and 66.8 MB** to open none. Those figures predate the 1.0 artifacts and are `unverified` for them.

**Each patch is its own library, not only each line.** A process that follows its servers through upgrades loads one library for every EXACT patch it is asked for — two patches of one line, `26.8.14.3` and `26.8.15.10` say, are two separate `dlopen`s, each with its own DateLUT and refuse-list, at about the same ~120 MB each — and **no binding ever unloads one**. A registry's `libraries()` lists what is actually open, not deduplicated by line. Budget for the patches you actually serve at the same time, not the lines, and after a fleet-wide upgrade completes, restart or roll long-lived processes so the patches nobody asks for any more are released. That a second patch of the SAME line costs about the same as a different line is **inferred** — it is the same build class — not separately measured.

**One image per file, process-wide.** The loader keys an image on its resolved path plus device and inode, so two registries, two spellings or a hardlink of one artifact share one image and one `Library` object ([`reference/bindings-v1.md` §3](../reference/bindings-v1.md#the-objects-their-lifetimes-and-how-each-one-closes)).

**Loading is lazy per request, in every binding and in every constructor.** Constructing a registry opens nothing. **Nothing opens an artifact except a request for a specific version, or an explicit `preload`.** Not `installed()`, not `libraries()`, not formatting the registry. This is a single-sourced statement of fact; nothing else in the docs restates it.

### Opening a pinned set up front: `preload`

A deployment that knows which versions it serves can open them at construction, and find out at construction if one is missing. It is a registry option in all four, it takes a **list of requests** rather than "everything installed" — a cache is whatever fetches left behind — and an entry no installed build answers raises the ordinary artifact-missing error, earlier than it otherwise would. It never fetches, even with autofetch on: fetch first (`chtypes fetch 26.8 26.7`), then preload.

| SDK        | spelling                                                                                               |
| ---------- | ------------------------------------------------------------------------------------------------------ |
| Go         | `chtypes.NewRegistry(chtypes.WithPreload("26.8", "26.7"))`                                             |
| Python     | `Registry(preload=["26.8", "26.7"])`                                                                   |
| TypeScript | `await Registry.open({ preload: ['26.8', '26.7'] })`                                                   |
| Rust       | `Registry::new(RegistryOptions { preload: vec!["26.8".into(), "26.7".into()], ..Default::default() })` |

The per-library checksum option of 0.x is gone: the fetch layer guarantees on return that the library file's digest equals the signed statement's, and the loader checks the open library against the same statement ([`artifacts.md`](artifacts.md#what-is-checked)). There is no `WithVerifyChecksums` or `verify_hashes` to set.

`installed()` lists every build the cache and system directories hold for this platform, open or not; `libraries()` lists the ones the registry has actually opened.

<details open><summary><b>Go</b></summary>

```go
reg, _ := chtypes.NewRegistry()   // nothing dlopen'd yet
old, _ := reg.For("24.8")         // loads 24.8
recent, _ := reg.For("26.7")      // loads 26.7, beside it
installed, _ := reg.Installed()   // every build in the cache
open := reg.Libraries()           // the two above
```

</details>

<details><summary><b>Python</b></summary>

```python
registry = Registry()                   # nothing dlopen'd yet
old = registry.for_version("24.8")      # loads 24.8
recent = registry.for_version("26.7")   # loads 26.7, beside it
print(registry.installed())             # every build in the cache
print(registry.libraries())             # the two above
```

`registry["26.7"]` is gone: a convenience that existed in one binding only was deleted, so the four stay one shape.

</details>

<details><summary><b>TypeScript</b></summary>

```ts
const registry = await Registry.open();
const old = await registry.for('24.8');
const recent = await registry.for('26.7');
console.log(await registry.installed(), registry.libraries());
```

`registry.for` is asynchronous in 1.0, because the fetch layer's own resolution is.

</details>

<details><summary><b>Rust</b></summary>

```rust
let registry = Registry::new(RegistryOptions::default())?;  // opens nothing
let old = registry.for_version("24.8")?;                    // Arc<Library>
let recent = registry.for_version("26.7")?;
println!("{:?} {}", registry.installed()?, registry.libraries().len());
```

</details>

## Resolution: the exact patch or its line — never another line

A request is a **line** (`26.8`), three parts (`26.8.15`) or an **exact patch** (`26.8.15.10`): two, three or four parts, no `v` prefix and no channel suffix. `For` / `for_version` / `for` return the `Library`, and its `resolved` record says which signed build answered ([`fetch-v1.md` §3](fetch-v1.md#3-resolve) is the resolution contract, and the fetch layer does it, not the binding).

**An exact four-part request is exact or it fails.** 0.x fell back from an unpublished patch to the newest installed patch of its line, flagged `Exact = false`, and warned. That fallback, and the `Resolve` call and `Resolution` type that reported it, are deleted ([`reference/bindings-v1.md` §7](../reference/bindings-v1.md#7-what-v0-api-is-deleted-and-why)): a request is answered only by a build whose signed `clickhouse_version` equals it, or lies within a floating one ([`fetch-v1.md` §4](fetch-v1.md#4-trust)). A patch nothing answers is `CHTYPES_ARTIFACT_MISSING`, or `CHTYPES_ARTIFACT_UNPUBLISHED` when a fetch asks the registry and it does not publish it.

**A floating request never moves mid-process.** `26.8` resolves once per registry and is memoized for that registry's life: a newer `26.8` patch installed later is picked up by a new registry, not by the old one. Which installed build a floating request means is the fetch layer's rule, and the binding orders and matches no versions itself.

<details open><summary><b>Go</b></summary>

```go
lib, _ := reg.For("26.8")
fmt.Println(lib.Version, lib.Resolved().Source) // 26.8.15.10 cache
```

</details>

<details><summary><b>Python</b></summary>

```python
library = registry.for_version("26.8")
print(library.version, library.resolved.source)   # 26.8.15.10 cache
```

</details>

<details><summary><b>TypeScript</b></summary>

```ts
const lib = await registry.for('26.8');
console.log(lib.version, lib.resolved?.source);   // 26.8.15.10 cache
```

</details>

<details><summary><b>Rust</b></summary>

```rust
let lib = registry.for_version("26.8")?;
println!("{} {:?}", lib.version(), lib.resolved().map(|r| &r.source)); // 26.8.15.10 Some("cache")
```

</details>

`resolved` is empty for a library opened with `open_unverified` or linked in Go, neither of which a registry hands out.

There is deliberately **no cross-line fallback, ever**, and the reason is worth stating plainly: version behavior is not monotonic. 25.10 rejects a DEFAULT that both 25.8 and 26.6 accept. A different line's answer is not an approximation of the right one — it is a different answer, and handing it back quietly would make chtypes the thing it exists to prevent. Patch-to-patch drift within one line is real too (a DateTime64/Time64 saturation backport changed a CAST mid-line), which is exactly why an exact request is exact.

## Registry per request, with autofetch

A registry is cheap to construct, opens nothing, and is safe to share, so **where it lives is a deployment choice**:

- **A process-wide registry** with `preload` of the versions you serve and `autofetch` off is the production shape: nothing downloads inside a request, and a missing version fails at startup.
- **A registry per request or per tenant**, with `autofetch` on, is the development and bring-your-own-ClickHouse shape: the first request for a version fetches and verifies it, and every later one hits the cache. Because a loaded image is process-wide and keyed by file, a second registry over the same cache costs no second load.

`autofetch` defaults to the value of `CHTYPES_AUTOFETCH`, and is off when that is unset. See [`artifacts.md`](artifacts.md#lazy-fetch-is-opt-in) for why.

## Thread-safety

**Concurrent calls are safe everywhere the library can make them so, and the library makes every call on a compiled handle safe to run concurrently with any other call on the same or another handle.** 0.x said a single handle was single-threaded; that rule is gone. The public layer takes no lock around a call. It keeps two pieces of synchronization of its own: a close guard per handle, so that `close` waits for the calls already inside that object and refuses later ones, and a setup guard ([`reference/bindings-v1.md` §3](../reference/bindings-v1.md#what-is-safe-to-share-across-threads)).

|                | what is safe                                                                                                                                                                                                        |
| -------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Go**         | `Registry`, `Library`, `Schema`, `Filter` and `Block` are safe for concurrent use by any number of goroutines. `Close` waits for in-flight calls on that handle.                                                    |
| **Python**     | everything is safe across threads. `ctypes` releases the GIL for the duration of each call, so calls on one handle run in parallel. `close` waits for in-flight calls on that object.                               |
| **TypeScript** | every call is synchronous on one thread, so within an isolate there is nothing to share. A handle cannot cross to another worker: each worker opens its own objects over the shared image.                          |
| **Rust**       | `Registry`, `Library` (shared as `Arc<Library>`), `Schema`, `Filter` and `Block` are `Send + Sync`. `&self` methods run concurrently with no lock, and `Drop` cannot race a call because a call borrows the handle. |

**There is no runtime settings setter.** 0.x had `set_default_settings`, which replaced a process-global the row path read by reference, so it needed an exclusive lock and Go's dlopen path could not call it at all. 1.0 deleted it: the default settings and the image time zone are fixed once, at `setup`, before any traffic, and never change ([`settings.md`](settings.md#the-four-channels-and-which-wins)). That is why 0.x's per-image reader-writer locks, mutexes and reentrancy tripwires are deleted too.

Measured under contention on 0.x, in Python's own suite: 27,770 batch reads across 8 threads against 566 concurrent settings swaps, every answer byte-identical to the uncontended one. That test exercised a setter that no longer exists and is `unverified` for 1.0.

**Parallelism still comes from handles.** Several handles scale; sharing one handle is now safe, but it is not where throughput comes from.

### ParseBlock throughput scales with handle count again, on every line (chtypes#78)

_Updated 2026-09-30 — a 0.x throughput note, kept for what it measured; the safety guarantee is the one stated above._

A dated note stood here through 2026-09-29 saying that, on Linux, ClickHouse lines above 26.2 had stopped gaining ParseBlock throughput from adding handles inside one process. That no longer holds for current artifacts. **chtypes#78** has the artifact producer's fix and its full measurement — cited here rather than restated: their ParseBlock probe (`measured`, the producer's numbers) showed 26.6's N8/N1 throughput going from 3.60× to 7.95× after the fix, with every affected line's post-fix ratio in the 7.74×–8.02× range.

A downstream consumer's own, independent measurement adds a second, more recent data point: **5.92× at N=8 with its own handles, against a 0.99× process-wide-mutex gate, on revision-6 26.6 artifacts, linux-arm64** (`measured (reporter)`; an OrbStack Linux VM on Apple silicon, ~3.5 host cores busy elsewhere, a process ceiling of ~6× in that environment — amd64 not measured).

Size concurrency inside one process by handles, on every line — there is no longer a reason to size it by process count instead.

### One boundary worth naming: `worker_threads`

In Node, two JS threads share one dlopen'd image and one set of C globals, which no per-isolate counter can see. **`setup` is per isolate, so every worker must call it identically**: a worker whose zone differs from the one the image was set up with is refused by the library at its first open, as a `UsageError` naming both. Do not share a `Schema` across workers.

### Go: sizing concurrent handles

Nothing in chtypes caps how many handles a caller runs at once — size a caller-side semaphore to `runtime.GOMAXPROCS(0)` yourself. Parallelism beyond the number of OS threads Go will actually schedule for your goroutines buys queueing, not throughput.

`GOMEMLIMIT`, if set, sees only Go's own live heap. The C++ library behind a `dlopen`'d artifact keeps its own heap — the compiled schema, open filters, and the DEFAULT evaluator's working set — entirely invisible to it, so a `GOMEMLIMIT` sized from `runtime.MemStats` or `pprof` alone will trip GC far too late, or never, against memory the Go runtime cannot see. Leave headroom for it, sized from the figures below rather than from Go's own heap alone.

**Measured (reporter)**, linux-arm64, a 6-column DDL with a per-claim `DEFAULT`: opening a library costs +92.7 MiB resident (ClickHouse 26.6) / +89.7 MiB (25.8); a compiled-but-unused handle, 39–43 KiB; first use of each of the first hundred handles, 89–115 KiB; 256 warm handles (100 of them with a filter attached), +23.9/+24.3 MiB total (~96–97 KiB each); 1000 warm handles, +40.6/+55.5 MiB (~41–57 KiB each). darwin-arm64, a 5-column DDL: ~45 KiB per warm handle. `CompileTable`'s own cost (measured on 0.x's `CompileDDL`): a median of ~971 µs for 40 columns, ~12 µs for 4 columns.

**`Close` does not lower RSS.** The same measurement closed all 1000 handles above and recovered only 0.7–0.8 MiB — the C++ allocator keeps its arena. Size a long-lived process for its peak handle count, not its steady-state one.

**Benchmark the library that is actually loaded, not the version you asked for.** A line request means the newest installed build of that line, and a registry built after a fetch can pick up a newer patch (see [Resolution](#resolution-the-exact-patch-or-its-line--never-another-line) above). Assert `Library.Version` in every cell of a benchmark table, so a number can never silently mix builds.

## Teardown

**There is none.** No binding ever unloads a library, and none exposes `shutdown`: a loaded image lives until the process exits, and unloading or re-initializing one in-process is unsupported. A registry and a library hold nothing to close ([`reference/bindings-v1.md` §3](../reference/bindings-v1.md#the-objects-their-lifetimes-and-how-each-one-closes)). 0.x's `Registry.shutdown`, `Library.shutdown`, Python's `Library.close` and `Registry` context manager, and TypeScript's `registry.close` are deleted, and with them the reopen crash their rules guarded against.

What you do close is the handles you open: a schema, a filter or a block, whose `close` is idempotent and order-free. For sizing, the consequence is the one above: budget for the peak number of loaded patches and handles, and roll long-lived processes after a fleet-wide upgrade.

## Next

- [`artifacts.md`](artifacts.md#loading) — what the loader checks before a library is usable.
- [`discovery.md`](discovery.md) — how you learn which version a given deployment actually needs.
- [`../support-v1.md`](../support-v1.md) — what is known about which lines exist to be loaded.
