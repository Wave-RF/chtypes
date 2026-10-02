#!/usr/bin/env bash
# check-rust.sh: the v1-abi-rust job body (.github/workflows/v1-abi.yml).
#
#     scripts/abi-v1/check-rust.sh
#
# clippy, fmt and the unit tests with the `abi-v1` feature, STABLE ONLY —
# this job's own name is one of the spelled-exactly required check contexts,
# and a stable/MSRV matrix here would split it into two context names for no
# proof this job itself needs: MSRV coverage of the real conformance cases
# already runs in v1-abi-conformance's `rust msrv` leg
# (scripts/abi-v1/conformance/rust.sh), which resolves rust-version from
# rust/Cargo.toml at run time.
#
# No $CHTYPES_ABI1_STUBS is exported here (that is v1-abi-conformance's own
# env, set per leg): rust/tests/abi1_conformance.rs reads its absence as the
# signal to skip loudly, by name, and `cargo test` still exits 0 — the suite
# that passes "with nothing exercised" catch applies only to a suite missing
# its own summary line (scripts/check-suite.sh's rule for v0), and a single
# skipped test naming itself on stderr is not that: the real assertion this
# job makes is clippy and fmt, over the whole feature-gated source tree.
#
# Toolchain setup (stable, with clippy and rustfmt) is this workflow's own
# step, same as every other binding's toolchain setup in v1-abi.yml; this
# script assumes `cargo`, `cargo clippy` and `cargo fmt` are already on PATH.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
RUST="$ROOT/rust"

say() { printf '\033[1m==> %s\033[0m\n' "$*" >&2; }

say "cargo fmt --check (rust/)"
( cd "$RUST" && cargo fmt --check )

say "cargo clippy --locked --features abi-v1 --all-targets -- -D warnings (rust/)"
( cd "$RUST" && cargo clippy --locked --features abi-v1 --all-targets -- -D warnings )

say "cargo test --locked --features abi-v1 (rust/) — CHTYPES_ABI1_STUBS is unset here; abi1_conformance skips loudly, by name"
( cd "$RUST" && cargo test --locked --features abi-v1 )

echo "check-rust.sh: ok — fmt, clippy -D warnings and the unit tests all passed with --features abi-v1"
