# Install

Two steps, in this order: the **binding** for your language, then at least one **artifact** for it to load. The binding alone answers nothing — see [`index.md`](index.md) if that split is new to you.

## 1. The binding

<details open><summary><b>Go</b></summary>

```sh
go get github.com/wave-rf/chtypes/go
```

The module path is lowercase — the Go norm — and froze at the first tag. Import it as:

```go
import "github.com/wave-rf/chtypes/go/chtypes"
```

The default build is **dlopen-only**: it compiles with cgo (for `dlfcn`) but links nothing, includes no header, and needs no build tree. That is what `go get` gives a consumer, and it is all you need.

**cgo is required to build, and glibc to run.** The package's untagged `import "C"` is a thin layer of C shims over each artifact's `chs_*` function table, so a build needs `CGO_ENABLED=1` and a C compiler on `PATH`. Go switches cgo off by itself when it finds no compiler or is cross-compiling, and the build then fails with an undefined-name error that does not mention cgo. At run time the artifact is a shared object that needs only the C library and carries its C++ runtime inside. On Linux it needs a glibc at or above the floor the build records in its own signed statement; a host below it is refused at open with `CHTYPES_ARTIFACT_INCOMPATIBLE` ([`support-v1.md`](support-v1.md#platforms)). So a glibc image such as `debian:bookworm-slim` can run it, while `FROM scratch`, `gcr.io/distroless/static` and musl distributions such as Alpine cannot (`inferred` from the loader; musl is support unknown). Build in a glibc image that has a compiler, such as the Debian-based `golang` images.

</details>

<details><summary><b>Python</b></summary>

```sh
uv add chtypes            # in a uv project
uv pip install chtypes    # in a bare venv
pip install chtypes       # anywhere else
```

Python 3.11 or newer. The binding is stdlib `ctypes`, with no build step and no compiler. On Python before 3.14 it pulls in one dependency, `backports.zstd`, for the artifact's zstd layer.

</details>

<details><summary><b>TypeScript</b></summary>

```sh
pnpm add @wavehouse/chtypes
npm install @wavehouse/chtypes
```

ESM only, Node 22.21 or newer. Native calls go through `ffi-rs`, prebuilt for darwin arm64/x64 and linux arm64/x64 (gnu and musl), so there is no build step.

⚠️ That is the FFI loader's matrix, not the artifact's. chtypes artifacts are published for **darwin-arm64, linux-amd64 and linux-arm64 only** ([`support-v1.md`](support-v1.md#platforms)). On an Intel Mac or a musl distribution the package installs and `ffi-rs` resolves, and then `chtypes fetch <line>` answers `CHTYPES_ARTIFACT_UNPUBLISHED`.

</details>

<details><summary><b>Rust</b></summary>

```sh
cargo add chtypes
```

Rust 1.87 or newer, edition 2024. The fetch layer and the `chtypes` binary are part of the crate; no feature flag turns them on.

</details>

Version requirements per language and the platforms artifacts are published for are in [`support-v1.md`](support-v1.md). The ClickHouse lines that are available are whatever the registry publishes (`chtypes list`); the v1 channel carries no statement of which are supported, so a line's support reads **unknown**, never unsupported.

## 2. An artifact

Each binding ships the same fetch command, so you need nothing from this repository:

```sh
go run github.com/wave-rf/chtypes/go/cmd/chtypes@latest fetch 26.8   # Go
python -m chtypes fetch 26.8                                         # Python
npx @wavehouse/chtypes fetch 26.8                                    # TypeScript
cargo install chtypes && chtypes fetch 26.8                          # Rust
```

A spelling is two, three or four parts (`26.8`, `26.8.15`, `26.8.15.10`), with no `v` prefix and no channel suffix. The command installs into the per-user OCI cache `${XDG_CACHE_HOME:-~/.cache}/chtypes/v1/`, which every binding reads by default, so **one machine set up once serves all four**. `CHTYPES_CACHE` overrides the cache directory, and `chtypes where` prints the one in use. `CHTYPES_REGISTRY`, which the previous generation used, is retired: set, it warns once and is otherwise ignored.

Before anything lands, the command verifies a Sigstore bundle signed under the release key and the digest of every byte. `fetch --all` takes every line the registry publishes for this platform; `verify`, `list`, `where`, `resolve` (what a line resolves to, installing nothing) and `prune` (remove the builds newer ones supersede) are the other five subcommands. [`guides/artifacts.md`](guides/artifacts.md) has the whole story, including pinning for CI with `--lock` and `--frozen`.

Pick a line you actually need. Each artifact is 160–300 MB on disk and about 120 MB resident once loaded, so fetch the versions your deployments run rather than all of them.

**Or let the first open fetch it.** A registry fetches a missing version on demand when its `autofetch` option is on, or when `CHTYPES_AUTOFETCH=1` is set for the process. It is off by default, because a production process should not begin a 250 MB download inside a request.

## Check it worked

<details open><summary><b>Go</b></summary>

```go
reg, err := chtypes.NewRegistry() // opens nothing
if err != nil {
	log.Fatal(err)
}
installed, err := reg.Installed()
if err != nil {
	log.Fatal(err)
}
for _, r := range installed {
	fmt.Println(r.Version, r.Platform) // 26.8.15.10 linux-arm64
}
```

</details>

<details><summary><b>Python</b></summary>

```python
from chtypes import Registry

for r in Registry().installed():
    print(r.version, r.platform)   # 26.8.15.10 linux-arm64
```

</details>

<details><summary><b>TypeScript</b></summary>

```ts
import { Registry } from '@wavehouse/chtypes';

const registry = await Registry.open();   // opens nothing
for (const r of await registry.installed()) {
  console.log(r.version, r.platform);     // 26.8.15.10 linux-arm64
}
```

</details>

<details><summary><b>Rust</b></summary>

```rust
use chtypes::{Registry, RegistryOptions};

let registry = Registry::new(RegistryOptions::default())?;   // opens nothing
for r in registry.installed()? {
    println!("{} {}", r.version, r.platform);                // 26.8.15.10 linux-arm64
}
```

</details>

An empty list means no artifact is installed where the binding is looking. Asking for a version you do not have is a deliberately loud error naming the request and the platform, and the command that would fix it — see [the one error](guides/artifacts.md#the-one-error).

## Working against a checkout

If you are developing against this repository rather than consuming a release:

<details open><summary><b>Go</b></summary>

```text
require github.com/wave-rf/chtypes/go v0.0.0
replace github.com/wave-rf/chtypes/go => ../path/to/chtypes/go
```

The `require` version is never resolved once the module is replaced by a path, so `v0.0.0` is the honest placeholder — it is what `examples/go/go.mod` in this repository uses.

</details>

<details><summary><b>Python</b></summary>

```sh
uv add --editable /path/to/chtypes/python
uv pip install -e /path/to/chtypes/python
```

</details>

<details><summary><b>TypeScript</b></summary>

```jsonc
// your app's package.json
{ "dependencies": { "@wavehouse/chtypes": "file:../chtypes/ts" } }
```

Build the package's `dist/` first — that is what `package.json#exports` serves:

```sh
cd <repo>/ts && pnpm install && pnpm build
```

If a linked copy goes stale, note that `pnpm install --force` does **not** relink: remove `node_modules` and reinstall.

</details>

<details><summary><b>Rust</b></summary>

```toml
[dependencies]
chtypes = { path = "../chtypes/rust" }
```

</details>

A library you built yourself, for example the artifact producer's own unpublished build, is not reachable through a registry. Open it by path with `open_unverified`, which refuses unless you both pass its `allow` argument and set `CHTYPES_ALLOW_UNVERIFIED_LIBRARY=1` ([`reference/bindings-v1.md` §6](reference/bindings-v1.md#unverified-and-linked-opens)).

## Next

[`quickstart.md`](quickstart.md) — one artifact, one schema, one row, in whichever of the four you just installed. What 1.0 does not do yet is in [`limitations.md`](limitations.md#known-gaps-in-10).
