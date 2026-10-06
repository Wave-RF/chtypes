# testdata

## batch-100-flags0.json and batch-100-flags7.json

Real batch documents: the exact bytes `chs_preview_batch` returned for one 100-row JSONEachRow body, one at `doc_flags` 0 and one at `doc_flags` 7 (`DocAll`). They are the input of `BenchmarkDecodeBatch` and one of the document sets of `TestDecodeBatchEquivalence`, both in `decode_bench_test.go` and `decode_equivalence_test.go`.

- **Library:** the signed `darwin-arm64` library for ClickHouse 26.8.15.10 (channel `lts`, build `20261003.231921`, core commit `eb60bf77d9c2a267cf15b81158dedd1b353e0585`), opened unverified from the local cache.
- **Schema:** `captureDDL` in `capture_batch_test.go`. It has an integer, a plain string, a `DateTime`, a `Float64`, an `Array(String)`, a `Nullable(String)`, an `Int8`, a `LowCardinality(String)` with a default, and a `MATERIALIZED` column.
- **Body:** `captureBody()` in the same file: 100 rows, with `\"` and non-ASCII text in the strings, NULLs, an `Int8` overflow every 17th row (an `overflow_wrap` transform) and a missing `label` on every fourth row (a `default_filled` transform). Every row is accepted, so the document carries all 100 rows.
- **Produced by:** `TestCaptureBatchDocuments`, which calls the generated `PreviewBatch` wrapper directly because the public `Rows` returns the decoded result, not the document:

```sh
cd go
CHTYPES_ALLOW_UNVERIFIED_LIBRARY=1 \
CHTYPES_CAPTURE_LIBRARY=$HOME/.cache/chtypes/v1/unpacked/sha256/<manifest digest>/libchtypes.dylib \
CHTYPES_CAPTURE_BATCH_DIR=chtypes/testdata \
go test -run TestCaptureBatchDocuments ./chtypes
```

The files are never hand-edited. A different build or a changed body yields different bytes; nothing compares against these bytes except the two decoders with each other, so regenerating them is safe.
