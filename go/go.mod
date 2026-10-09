module github.com/wave-rf/chtypes/go

go 1.27

toolchain go1.27.2

require github.com/klauspost/compress v1.20.1

// The 0.x line is retired: use v1. See CHANGELOG.md and public issue #431.
retract [v0.1.0, v0.5.2]
