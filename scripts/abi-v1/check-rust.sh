#!/usr/bin/env bash
# check-rust.sh: the v1-abi-rust job body (.github/workflows/v1-abi.yml).
#
#     scripts/abi-v1/check-rust.sh
#     scripts/abi-v1/check-rust.sh --selftest
#
# --selftest is a FAST pre-flight (round 2's "selftest first" discipline),
# never a second run of the full clippy/fmt/test pass below: it proves this
# script's own prerequisites hold — cargo, rustfmt and clippy are on PATH,
# rust/Cargo.toml declares the abi-v1 feature and the abi1_conformance test
# gated on it, and the generated abi1 layer exists — so a broken toolchain or
# a dropped Cargo.toml edit is named here rather than surfacing as an opaque
# clippy/test failure a step later.
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
fail() { echo "check-rust.sh --selftest: FAIL: $*" >&2; exit 1; }

if [ "${1:-}" = "--selftest" ]; then
    command -v cargo >/dev/null 2>&1 || fail "cargo is not on PATH"
    cargo fmt --version >/dev/null 2>&1 || fail "cargo fmt (rustfmt) is not available"
    cargo clippy --version >/dev/null 2>&1 || fail "cargo clippy is not available"
    command grep -qE '^abi-v1[[:space:]]*=' "$RUST/Cargo.toml" \
        || fail "rust/Cargo.toml declares no abi-v1 feature"
    command grep -qE '^\[\[test\]\]' "$RUST/Cargo.toml" \
        || fail "rust/Cargo.toml declares no [[test]] entries"
    for f in decls.rs invoke_gen.rs loader.rs; do
        [ -f "$RUST/src/abi1/$f" ] || fail "rust/src/abi1/$f is missing"
    done
    [ -f "$RUST/tests/abi1_conformance.rs" ] || fail "rust/tests/abi1_conformance.rs is missing"
    echo "check-rust.sh --selftest: ok — cargo/rustfmt/clippy on PATH, Cargo.toml declares abi-v1, every abi1 source file is present"
    exit 0
fi

say "cargo fmt --check (rust/)"
( cd "$RUST" && cargo fmt --check )

say "cargo clippy --locked --features abi-v1 --all-targets -- -D warnings (rust/)"
( cd "$RUST" && cargo clippy --locked --features abi-v1 --all-targets -- -D warnings )

say "cargo test --locked --features abi-v1 (rust/) — CHTYPES_ABI1_STUBS is unset here; abi1_conformance skips loudly, by name"
( cd "$RUST" && cargo test --locked --features abi-v1 )

echo "check-rust.sh: ok — fmt, clippy -D warnings and the unit tests all passed with --features abi-v1"
