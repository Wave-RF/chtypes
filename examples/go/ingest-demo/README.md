# ingest-demo — chtypes shaped like a real ingest worker

**This is the OPTIONAL, ONLINE demo.** Unlike everything else under `examples/`, it needs a **reachable ClickHouse server** (`CH_ADDR`), and `../../chplay.sh` never runs it. The offline tours' section 11 walks the same discovery flow against canned bytes; come here when you want to see it against the real thing.

A runnable miniature of the whole path: connect to a real ClickHouse, learn what it is, compile its table, and push a batch of rows through — printing, for each row, **what an ingest worker would publish to subscribers**.

It exists because the interesting parts of the integration are not the API calls, they are the four or five decisions the caller still has to make afterwards. Each row in the batch is chosen to force one of them.

## Run it

Bring up a server of your own — **do not use another project's container**, and do not publish ports (parallel stacks collide):

```sh
docker run -d --name chguide-ch --label com.docker.compose.project=chguide \
    -e CLICKHOUSE_PASSWORD=chguide clickhouse/clickhouse-server:26.8

IP=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' chguide-ch)
cd examples/go && CH_ADDR=http://$IP:8123 go run ./ingest-demo

docker rm -f chguide-ch          # when you are done
```

On OrbStack the container IP is routable from the host directly, which is why no port publishing is needed. Environment: `CH_ADDR`, `CH_USER` (`default`), `CH_PASSWORD` (`chguide`), `CHTYPES_CACHE` (defaults to the per-user v1 cache). The library for the server's version is fetched, signature-verified and installed on first use (autofetch is on in this demo).

The demo creates `chguide_demo.events` itself, so it is self-contained. It needs cgo (the whole library is a cgo `dlopen` of a vendored ClickHouse build) and network access to the artifact registry.

## What each step shows

| Step                      | What it demonstrates                                                                                                                                                                  |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1. Discovery              | The server's version and changed settings, asked with **your own** HTTP client. chtypes never opens a socket, and holds no SQL for these two reads.                                   |
| 2. Registry               | One library per ClickHouse version; `For()` resolves the server's exact version, fetching and verifying it on first use. No nearest-version fallback.                                 |
| 3. `DiscoverQuery`        | The library's own `system.columns` query, run with your client, and `DiscoverColumns` turning the rows into declarations with ClickHouse's own reader and formatter.                  |
| 4. `CompileTable`         | One `CREATE TABLE` compiled under the **discovered** settings, not a guessed profile.                                                                                                 |
| 5a. Per-row admission     | `Row()`: one verdict per record, the shape a per-record ingest path already has.                                                                                                      |
| 5b. The same rows batched | `Rows()`: under stock settings **one unparseable row ends the batch** and the rows after it never get a verdict.                                                                      |
| 6. Unknown setting        | A refusal carrying ClickHouse's own code `115`, as distinct from a chtypes _decline_.                                                                                                 |

## The five rows, and the decision each one forces

**Row 0 — clean accept.** Publish `Value.Text` for each column. Every published value is the **stored** text, not the payload text.

**Row 1 — accepted, silently changed.** `seq: 256` into `UInt8` is stored as `0`. ClickHouse does not refuse this and neither does chtypes; it reports a `Transform` with reason `overflow_wrap`, and `Lossy` is true. Publishing the _payload_ here is the payload-vs-stored asymmetry — a subscriber is told `256` while the table holds `0`. Publishing `Value.Text` closes it.

**Row 2 — rejected.** Code `27`, ClickHouse's own. Drop it, 400 the producer, publish nothing. This is a **refusal** (the server really says no), not a decline.

**Row 3 — volatile DEFAULT substituted.** `ts DateTime64(3) DEFAULT now64(3)` was resolved locally, and `Value.Source` is `default_substituted`. The value is only the truth **if the server is never asked to evaluate the expression**: preview and insert never agree on `now64(3)`. So the caller must send every column whose source is `default_substituted` (or `default_generated`) as an explicit field in the INSERT, or insert the library's export. Note the row also shows `seq` filled from a literal DEFAULT (`Source: "default"`), which carries no such obligation.

**Row 4 — unknown field.** `extra` has no column. On this server it is _accepted_ (`input_format_skip_unknown_fields` defaults on), and chtypes reports it in `UnknownFields` rather than deciding for you. Whether that is a 400 is a policy choice the gateway makes, on data chtypes hands it.

Throughout, `payload_len` appears as `Computed`, never in `Values`. It is `MATERIALIZED`: durable once written, but absent from `SELECT *`, so putting it in the published row would make the preview disagree with what a subscriber reading the table actually sees.

## Two things this demo does not do

- **It does not compare anything.** Everything here is insert-side coercion. A `WHERE`-clause constant is a different question with different rules — see the C ABI contract on filters before folding a predicate operand through this API.
- **It does not prove float precision on other hosts.** This walkthrough was recorded on macOS, whose artifacts are for development (Linux is the reference): macOS `long double` is 53-bit, so float parses diverge from real servers. Nothing in this batch is float-sensitive, but do not take float results from a Mac.
