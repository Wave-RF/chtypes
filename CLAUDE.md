# chtypes (SDK repository) — orientation for Claude sessions

This is the **SDK half** of chtypes, Apache 2.0, public: `go/ python/ ts/ rust/`
over the frozen `chs_*` C ABI (`include/chtypes.h`, ABI revision 4, 28
functions), the normative `spec/`, four side-by-side `playground/` tours, and
`goldens/` — the public golden set every binding runs. The bindings contain no
ClickHouse code; they `dlopen` per-version artifacts and speak the ABI.

The other half is the sibling repository `../chtypes-core` (wrapper, build,
artifacts, rigs, corpus, runs of record; licence pending). Its `CLAUDE.md`
carries the rules that were paid for; the ones that bind here:

- **Never rebuild a ClickHouse rule in an SDK.** A binding is a thin passthrough
  to the artifact; scalar, comparison, coercion and timestamp logic never live
  in Go/Python/TS/Rust. The one derived result is `Transformed`, per spec.
- **The four bindings give one answer.** A behaviour change lands in all four
  in one cycle (`spec/bindings.md` is the shape), and the golden set must stay
  green in all four. The golden set is **served, not tracked**: core publishes
  `sdk-goldens.json` in the rolling release as a row in the signed
  `SHA256SUMS`, `scripts/fetch.sh` installs it at `<registry>/sdk-goldens.json`,
  and each binding's golden test reads it offline from there
  (`CHTYPES_GOLDENS` overrides). There is no cases file in this repository. A
  case runs only against the EXACT ClickHouse version `generated.exact` names
  for its line and skips loudly otherwise, and the set shrinks as lines are
  added, because a case that stops being version-agnostic is dropped rather
  than recorded twice.
- **The header is owned here.** An ABI change: `include/chtypes.h` + the four
  bindings' pinned constants (`ABIRevision` / `ABI_REVISION`) in one commit;
  the core repository then pulls the header (`ci/steps/header-sync.sh --pull`)
  and relinks every artifact. CI asserts the header and the bindings agree.
- **Never trust exit codes or self-reports.** Every test here skips LOUDLY
  without a registry and refuses a zero-run; `scripts/check-standalone.sh`
  reads its verdict off a `go test -json` census and `scripts/check-suite.sh`
  off each runner's own summary line, colour stripped.
- **The Go package is dlopen-only by default.** The linked path (package-level
  `CompileDDL`, `BuiltVersion`) is behind `-tags chtypes_linked` and needs a
  core build tree via `CGO_LDFLAGS`; `undefined: chtypes.CompileDDL` means the
  tag is missing. `go/chtypes/linked_abi_check.go` pins the hardcoded ABI
  numbers to the header at compile time (tagged build only).
- **Artifacts live in the per-user cache**, `~/.cache/chtypes/artifacts/<os>-<arch>/`
  (`$CHTYPES_REGISTRY` overrides): `scripts/fetch.sh` installs there, a core
  build lands there, every SDK's registry default resolves there.
- macOS artifacts are a dev floor, not an oracle (float parses diverge).
  Float expectations come from Linux or a live server.

Gates: `scripts/check-standalone.sh` (Go, from a bare copy) and
`scripts/check-suite.sh python|ts|rust`; per language `go test ./...`,
`uv run pytest -q`, `pnpm test`, `cargo test`. With a registry they run
everything; without one every artifact test skips by name and the rest still
runs, and a suite that ran nothing fails. `.github/workflows/ci.yml` runs on
hosted runners with no variable and no secret: once with no artifact
(`--no-artifacts`), once with two published lines fetched by
`scripts/fetch.sh` (`--require-artifacts`: the goldens must run). The
server-truth suites for these SDKs live in `../chtypes-core/tests/sdk/` and
run from there (`just test` in the core) against this tree as a sibling; the
artifact-backed proof is the core repository's `sdk-suites` workflow (named
`certify` until 2026-09-11).

Pre-1.0, and published: all four bindings are live at **0.1.0** —
`github.com/wave-rf/chtypes/go` (lowercase, the Go norm), PyPI `chtypes`,
npm `@wavehouse/chtypes`, crates.io `chtypes`. The module path and the
function signatures froze at that first tag. Nothing publishes without a
`<dir>/v*` tag, and each release workflow refuses a tag whose version differs
from the manifest's; the registry setup and the order are in `RELEASING.md`
(the one manual first publish per registry is done).
