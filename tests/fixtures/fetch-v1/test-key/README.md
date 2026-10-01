# The v1 fixture test key

**Test-only. Never trusted by default.**

This is an ed25519 key pair generated for this repository's v1 conformance
fixtures (`scripts/fetch-v1/genfixtures/`). It signs the Sigstore bundles that
accompany every fixture manifest, exactly the shape the real release key signs
(`docs/design` on the delivery side; `docs/guides/fetch-v1.md` §4 here).

- `private.pem`: the ed25519 private key, PKCS#8 PEM (`openssl genpkey
  -algorithm ed25519`). Used only by the fixture generator, at generation
  time. It never ships in any binding and is never read at runtime.
- `public.hex`: the raw 32-byte public key, lowercase hex, one line. This is
  the value a test opts into trusting — see below.

A conformance case trusts this key only when it explicitly asks to: setting
`request.trust` to `"test"` in the case table (under `tests/fixtures/fetch-v1/`,
written by the fixtures/server/parity lane), which the runner turns into the
`CHTYPES_TRUSTED_KEYS` environment variable (or the equivalent in-process
option) naming this key's `public.hex` value. The
default trust list (`spec/fetch-v1/constants.json`'s `trust.release_keys`)
never contains this key; a fetch that does not opt in refuses a bundle signed
with it exactly as it would refuse one signed by anyone else unrecognized.

See also `../other-key/`, a second key fixtures use for the untrusted-signer
cases (a valid signature under a key nothing trusts).
