# Install

Two steps, in this order: the **binding** for your language, then at least one **artifact** for it to load. The binding alone answers nothing — see [`index.md`](index.md) if that split is new to you.

## 1. The binding

<details open><summary><b>Go</b></summary>

```sh
go get github.com/wave-rf/chtypes/go
```

The module path is lowercase — the Go norm — and freezes together with the function signatures at 1.0. Import it as:

```go
import "github.com/wave-rf/chtypes/go/chtypes"
```

The default build is **dlopen-only**: it compiles with cgo (for `dlfcn`) but links nothing, includes no header, and needs no build tree. That is what `go get` gives a consumer, and it is all you need.

</details>

<details><summary><b>Python</b></summary>

```sh
uv add chtypes            # in a uv project
uv pip install chtypes    # in a bare venv
pip install chtypes       # anywhere else
```

Pure Python: stdlib `ctypes`, zero dependencies, no build step and no compiler.

</details>

<details><summary><b>TypeScript</b></summary>

```sh
pnpm add @wavehouse/chtypes
npm install @wavehouse/chtypes
```

ESM only. Native calls go through `ffi-rs`, prebuilt for darwin arm64/x64 and linux arm64/x64 (gnu and musl), so there is no build step.

⚠️ That is the FFI loader's matrix, not the artifact's. chtypes artifacts are published for **darwin-arm64, linux-amd64 and linux-arm64 only** ([support.md](support.md)). On an Intel Mac or a musl distribution the package installs and `ffi-rs` resolves, and then `chtypes fetch <line>` has nothing to give you.

</details>

<details><summary><b>Rust</b></summary>

```sh
cargo add chtypes
```

or in `Cargo.toml`:

```toml
[dependencies]
chtypes = "0.1"
```

The `fetch` feature is on by default and carries the `chtypes` binary, `ensure` and autofetch. `default-features = false` drops it and every dependency it brings, leaving the loader alone.

</details>

Version requirements per language, the platforms artifacts are published for, and the ClickHouse lines available are all in [`support.md`](support.md), which is generated from this tree's own manifests and from the release index rather than typed by hand.

## 2. An artifact

Each binding ships the same fetch command, so you need nothing from this repository:

```sh
go run github.com/wave-rf/chtypes/go/cmd/chtypes@latest fetch 25.8   # Go
python -m chtypes fetch 25.8                                         # Python
npx @wavehouse/chtypes fetch 25.8                                    # TypeScript
cargo install chtypes && chtypes fetch 25.8                          # Rust
```

That installs into `${XDG_CACHE_HOME:-~/.cache}/chtypes/artifacts/<os>-<arch>/25.8/` — the per-user cache every binding reads by default, so **one machine set up once serves all four**. `$CHTYPES_REGISTRY` overrides where it goes and where it is looked for.

Before anything lands, the command checks an ed25519 signature over the release and the sha256 of every byte. `fetch --all` takes every line the release publishes for this platform; `verify`, `list` and `where` are the other three subcommands. [`guides/artifacts.md`](guides/artifacts.md) has the whole story, including pinning for CI.

Pick a line you actually need. Each artifact is 160–300 MB on disk and about 120 MB resident once loaded, so fetch the versions your deployments run rather than all of them.

## Check it worked

<details open><summary><b>Go</b></summary>

```go
reg, err := chtypes.NewRegistry("")   // "" = walk the search path
if err != nil {
	log.Fatal(err)
}
fmt.Println(reg.Versions())           // [24.8 25.3 25.8 …]
```

</details>

<details><summary><b>Python</b></summary>

```python
from chtypes import Registry

print(Registry().versions())   # ('24.8', '25.3', '25.8', …)
```

</details>

<details><summary><b>TypeScript</b></summary>

```ts
import { Registry } from '@wavehouse/chtypes';

console.log(new Registry().versions());   // [ '24.8', '25.3', '25.8', … ]
```

</details>

<details><summary><b>Rust</b></summary>

```rust
use chtypes::Registry;

println!("{:?}", Registry::from_search_path().versions());   // ["24.8", "25.3", "25.8", …]
```

</details>

An empty list means no artifact is installed where the binding is looking. Asking for a version you do not have is a deliberately loud error naming every directory it searched and the command that would fix it — see [the one error](guides/artifacts.md#the-one-error).

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

An artifact built in the core repository lands in the same per-user cache, so a local build and a fetched release are interchangeable to every binding.

## Next

[`quickstart.md`](quickstart.md) — one artifact, one schema, one row, in whichever of the four you just installed.
