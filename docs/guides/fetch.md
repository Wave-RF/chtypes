# Fetching and verifying artifacts: the 0.x contract

This page was the 0.x fetch contract: a release directory, `SHA256SUMS` with an ed25519 signature, `scripts/fetch.sh`, and an `abi<R>` cache layout. **That contract does not apply to 1.0.** No 1.0 binding speaks it, and `CHTYPES_REGISTRY` is retired.

The 1.0 contract is [`fetch-v1.md`](fetch-v1.md): an OCI registry, signed per-platform manifests, a cache at `${XDG_CACHE_HOME:-~/.cache}/chtypes/v1/`, and the `chtypes fetch`, `verify`, `list` and `where` commands every binding ships. What a consumer needs from it is in [`artifacts.md`](artifacts.md).

The 0.x text of this page remains in the 0.x release history, at the `go/v0.*`, `python/v0.*`, `ts/v0.*` and `rust/v0.*` tags. The file stays at this path only so that links to it keep resolving.

| if you were looking for             | it is now                                                                                  |
| ----------------------------------- | ------------------------------------------------------------------------------------------ |
| where artifacts are looked for      | [`fetch-v1.md` §1, The cache](fetch-v1.md#1-the-cache)                                     |
| the signature and what is verified  | [`fetch-v1.md` §4, Trust](fetch-v1.md#4-trust)                                             |
| pinning for CI                      | [`fetch-v1.md` §6, Lock schema 3](fetch-v1.md#6-lock-schema-3-and---frozen--offlineupdate) |
| the error codes and exit statuses   | [`fetch-v1.md` §8, Errors](fetch-v1.md#8-errors)                                           |
| the one error for a missing version | [`artifacts.md`, The one error](artifacts.md#the-one-error)                                |
