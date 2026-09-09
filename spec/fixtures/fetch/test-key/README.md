# test-key — the key that signs the fetch fixtures, and nothing else

This is a **TEST key**. It exists so the four SDKs' fetch suites can verify
`../signed/` and friends with `CHTYPES_TRUSTED_KEYS=78da216574a56c57f6bef3054c2d234dc36936e6b8edbc2e340cc81ffb7d173d`.
Both halves are committed on purpose: the private half is derived from a
public seed, `sha256("chtypes spec/fixtures/fetch TEST key: derived from this public label, never the release key")`, by
chtypes-core's `tests/sdk/fetch-fixtures/gen.py`, so anyone can reproduce it
and nothing about it is secret.

| | |
|---|---|
| public key (raw, hex) | `78da216574a56c57f6bef3054c2d234dc36936e6b8edbc2e340cc81ffb7d173d` |
| key id | `d1251e468f9156ef` |
| private half | `private.pem` (PKCS#8), reproducible from the seed above |

It is **never the release key**. The release key's public half is in
`docs/fetch.md` §4 (`deb275922dbff76e`) and its private half lives only in
Phase; no SDK embeds this test key, and a release signed with it is refused by
every consumer that does not set `CHTYPES_TRUSTED_KEYS`.
