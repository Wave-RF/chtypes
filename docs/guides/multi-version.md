# Several versions in one process, and thread-safety

One process can hold as many ClickHouse versions as you have artifacts for, each answering with its own semantics, at the same time. That is the whole reason chtypes is a `dlopen`'d artifact rather than a linked library — a fleet does not run one ClickHouse version, and a gateway in front of it needs to answer for each tenant's actual server.

## How it works, and what it costs

Every artifact is opened with `RTLD_NOW | RTLD_LOCAL`. `RTLD_LOCAL` is the entire mechanism: it keeps each library's ClickHouse symbols private, so two builds that both define `DB::DataTypeFactory` never collide. A loader that used `RTLD_GLOBAL` would appear to work and then answer with the wrong version's semantics — which is worse than failing.

The cost, measured: **about 120 MB resident per loaded version** (160–300 MB on disk). Load the lines you serve, not every line published.

Loading is lazy per line. Constructing a registry reads manifests and dlopens nothing; asking for a version is what opens it.

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
let registry = Registry::from_search_path();   // lazy
let old = registry.for_version("24.8")?;       // Arc<Library>
let recent = registry.for_version("26.7")?;
println!("{:?}", registry.versions());
```

`Registry::new(dir)` is the other constructor: a single directory, loaded eagerly, answering `Error::NoSuchVersion` for a line it lacks. `from_search_path` is the one that walks the path.

</details>

## Resolution never falls back

A registry resolves a **minor line** (`25.8`) or an **exact patch** (`25.8.28.1-lts`). A patch inside a loaded minor resolves to that line. Anything else fails, naming what is available.

There is deliberately **no nearest-version fallback**, and the reason is worth stating plainly: version behavior is not monotonic. 25.10 rejects a DEFAULT that both 25.8 and 26.6 accept. A neighbor is not an approximation of the right answer — it is a different answer, and handing it back quietly would make chtypes the thing it exists to prevent.

## Thread-safety

The C contract underneath is short: concurrent calls are safe on **distinct** handles; a single handle is single-threaded; initialization and settings writes exclude everything else on that image.

`set_default_settings` is the sharp one. It replaces a process-global that the row path reads **by reference**, so an overlapping call is a use-after-free rather than a stale read. Every binding takes an exclusive lock for it.

|                | how it is enforced                                                                                                                                                                                                                                                                                                    |
| -------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Go**         | one mutex per schema handle; a per-`Library` RWMutex read-held by every call; `chs_init` runs exactly once per artifact path, before the `Library` is visible. A dlopen'd `Library` **structurally cannot** call `SetDefaultSettings` — the symbol is deliberately absent from its function-pointer table.            |
| **Python**     | `ctypes` releases the GIL for the whole duration of a foreign call, so the GIL is _not_ the exclusion. One writer-preferring readers-writer lock per loaded **image** (row calls, compiles and declarations hold it shared; `set_default_settings` and `close` hold it exclusively) plus one plain lock per `Schema`. |
| **TypeScript** | every call is synchronous on the JS thread, so ordinary single-threaded Node needs no locking. `setDefaultSettings` and `shutdown` carry a reentrancy tripwire and refuse loudly if another chtypes call is on the stack.                                                                                             |
| **Rust**       | one mutex per loaded image serializes every call into a `Library`. `Library` and `Registry` are `Send + Sync` — share them freely. `Schema` is `Send` and deliberately **not** `Sync`, so the type system refuses to let one handle reach two threads.                                                                |

Python's locks are interned per **image**, not per object: `dlopen` refcounts one mapping per file, so two `Registry` instances over one directory share the C globals. That is also the key `chs_init` deduplicates on, which is why a second init with a different timezone is refused rather than silently re-timezoning a live library.

Measured under contention in Python's own suite: 27,770 batch reads across 8 threads against 566 concurrent settings swaps, every answer byte-identical to the uncontended one, no exceptions and no deadlock.

**Parallelism comes from more schemas, not from sharing one.** That is true in every binding; Rust is simply the one that will not compile the alternative.

### One boundary worth naming: `worker_threads`

In Node, two JS threads share one dlopen'd image and one set of C globals, which no per-isolate counter can see. Seed default settings **before** starting workers, or serialize the seed yourself, and do not share a `Schema` across workers.

## Teardown

There is a `shutdown` in three of the four bindings, and it joins the DEFAULT evaluator's background threads. `chs_init` registers it with `atexit`, so **an ordinary process needs no call at all**.

It is required in exactly three situations: before any `dlclose`, when the host controls its own teardown order, and in tests that must not depend on `atexit`. Close every schema first; the calls are idempotent.

Python and Rust refcount it per image, so the **last** close per image runs `chs_shutdown` — two registries over one directory share every image, and closing one must not tear the evaluator down under the other.

Go has no teardown surface at all, deliberately: it never `dlclose`s a loaded artifact, so `chs_shutdown` is never owed.

## Next

- [`artifacts.md`](artifacts.md#loading) — what the loader checks before a library is usable.
- [`discovery.md`](discovery.md) — how you learn which version a given deployment actually needs.
- [`../support.md`](../support.md) — which lines exist to be loaded.
