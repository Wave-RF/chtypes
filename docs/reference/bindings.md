# The binding contract: the 0.x shape

This page was the 0.x binding contract: the object model, the result types, and the revision-numbered ABI behind them. **That contract does not apply to 1.0.** The 1.0 public API is rebuilt over the ABI v1 C layer, and several 0.x surfaces were deleted outright.

The 1.0 contract is [`bindings-v1.md`](bindings-v1.md). Its [§7](bindings-v1.md#7-what-v0-api-is-deleted-and-why) lists every 0.x name that is gone, and what replaces it. The fetch half is [`../guides/fetch-v1.md`](../guides/fetch-v1.md), and the C layer is [`abi-v1.md`](abi-v1.md).

The 0.x text of this page remains in the 0.x release history, at the `go/v0.*`, `python/v0.*`, `ts/v0.*` and `rust/v0.*` tags. The file stays at this path only so that links to it keep resolving.

| if you were looking for        | it is now                                                                                                 |
| ------------------------------ | --------------------------------------------------------------------------------------------------------- |
| the object model and lifetimes | [`bindings-v1.md` §3](bindings-v1.md#the-objects-their-lifetimes-and-how-each-one-closes)                 |
| the error split and classes    | [`bindings-v1.md` §4, Errors](bindings-v1.md#4-errors)                                                    |
| the result types               | [`bindings-v1.md` §5, Documents and decoders](bindings-v1.md#5-documents-and-decoders)                    |
| the error-code table           | [`bindings-v1.md` §5, `ErrorCodeTable`](bindings-v1.md#errorcodetable-from-the-error_code_table-document) |
| quoting                        | [`bindings-v1.md` §2, The library](bindings-v1.md#the-library)                                            |
| discovery                      | [`../guides/discovery.md`](../guides/discovery.md)                                                        |
| threads and teardown           | [`bindings-v1.md` §3](bindings-v1.md#what-is-safe-to-share-across-threads)                                |
| version selection              | [`bindings-v1.md` §6, The sequence](bindings-v1.md#the-sequence)                                          |
| one compile function           | [`bindings-v1.md` §2](bindings-v1.md#2-the-operations): `compile_table`, one `CREATE TABLE` statement     |
