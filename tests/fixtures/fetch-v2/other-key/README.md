# The v1 fixture "other" key

**Test-only. Never trusted by anything.**

A second ed25519 key pair, generated the same way as `../test-key/`
(`openssl genpkey -algorithm ed25519`), used only to sign the
untrusted-signer fixtures: a bundle whose DSSE signature verifies
correctly, but under a key that is not on any trust list a conformance
case configures. This is what the `untrusted-key` case (see
`docs/guides/fetch-v1.md` §10 and `scripts/fetch-v1/genfixtures/`) exercises —
a valid signature is not the same claim as a signature from someone trusted.

- `private.pem`: the ed25519 private key, PKCS#8 PEM. Used only by the fixture
  generator, at generation time.
- `public.hex`: the raw 32-byte public key, lowercase hex, one line. Recorded
  for the generator's own provenance; no case ever lists it in `trust`.

Unlike `../test-key/public.hex`, no fixture, constants file or case ever adds
this key to a trust list. If a case's trust list is ever changed to include
it, that case has stopped testing what its name says.
