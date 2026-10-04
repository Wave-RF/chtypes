# Quickstart

One artifact, one schema, one row — the same program in each language. It shows the thing chtypes exists for: the row is **accepted**, and `256` is silently stored as `0`.

You need a binding and a fetched artifact for line 26.8: [`install.md`](install.md) if you have neither, or `chtypes fetch 26.8` once the binding is installed. Any line the registry publishes works; 26.8 is just the one spelled below. What 1.0 does not do yet is in [`limitations.md`](limitations.md#known-gaps-in-10), and which lines and platforms exist is [`support-v1.md`](support-v1.md).

Three steps, in every language: **set up** the process (optional, and first), **open a library** for a version through a registry, then **compile one `CREATE TABLE` statement** and ask it about a body.

## The program

<details open><summary><b>Go</b></summary>

```go
package main

import (
	"fmt"
	"log"

	"github.com/wave-rf/chtypes/go/chtypes"
)

const ddl = "CREATE TABLE t (x UInt8, ts DateTime DEFAULT now()) ENGINE = MergeTree ORDER BY tuple()"

func main() {
	// Optional, and first: the image zone and default settings are fixed once per process.
	if err := chtypes.Setup(chtypes.SetupOptions{Timezone: "UTC"}); err != nil {
		log.Fatal(err)
	}
	reg, err := chtypes.NewRegistry() // opens nothing
	if err != nil {
		log.Fatal(err)
	}
	lib, err := reg.For("26.8") // a line or an exact patch; never a nearest match
	if err != nil {
		log.Fatal(err)
	}
	schema, err := lib.CompileTable(ddl)
	if err != nil {
		log.Fatal(err)
	}
	defer schema.Close()

	batch, err := schema.Rows(chtypes.JSONEachRow, []byte(`{"x":256}`))
	if err != nil {
		log.Fatal(err)
	}
	row := batch.Rows[0]
	fmt.Println(batch.Outcome)             // accepted
	fmt.Println(row.Values[0].Text)        // 0             — what would actually be stored
	fmt.Println(row.Transformed[0].Reason) // overflow_wrap — which is the product
	for _, c := range row.Columns {
		if c.Source == chtypes.SourceDefaultSubstituted {
			fmt.Println(c.Column, c.Text) // ts: send it explicitly, or preview != stored
		}
	}

	bad, err := schema.Rows(chtypes.JSONEachRow, []byte(`{"x":"abc"}`))
	if err != nil {
		log.Fatal(err)
	}
	fmt.Println(bad.Outcome, bad.ErrCode, bad.ErrMsg) // rejected 27 Cannot parse input: …
}
```

</details>

<details><summary><b>Python</b></summary>

```python
import chtypes
from chtypes import Format, Registry, Source

DDL = "CREATE TABLE t (x UInt8, ts DateTime DEFAULT now()) ENGINE = MergeTree ORDER BY tuple()"

# Optional, and first: the image zone and default settings are fixed once per process.
chtypes.setup(timezone="UTC")

registry = Registry()                   # opens nothing
library = registry.for_version("26.8")  # a line or an exact patch; never a nearest match

with library.compile_table(DDL) as schema:
    batch = schema.rows(Format.JSON_EACH_ROW, b'{"x":256}\n')
    bad = schema.rows(Format.JSON_EACH_ROW, b'{"x":"abc"}\n')

row = batch.rows[0]
print(batch.outcome)                # accepted      — Outcome is a StrEnum
print(row.values[0].text)           # b'0'          — what would actually be stored (bytes)
print(row.transformed[0].reason)    # overflow_wrap — which is the product
for c in row.columns:
    if c.source == Source.DEFAULT_SUBSTITUTED:
        print(c.column, c.text)     # ts: send it explicitly, or preview != stored

print(bad.outcome, bad.err_code, bad.err_msg)  # rejected 27 b'Cannot parse input: …'
```

</details>

<details><summary><b>TypeScript</b></summary>

```ts
import { Format, Registry, Source, setup } from '@wavehouse/chtypes';

const DDL = 'CREATE TABLE t (x UInt8, ts DateTime DEFAULT now()) ENGINE = MergeTree ORDER BY tuple()';

// Optional, and first: the image zone and default settings are fixed once per process.
setup({ timezone: 'UTC' });

const registry = await Registry.open();      // opens nothing
const lib = await registry.for('26.8');      // a line or an exact patch; never a nearest match
const schema = lib.compileTable(DDL);

const batch = schema.rows(Format.JSONEachRow, Buffer.from('{"x":256}'));
const row = batch.rows[0]!;                       // one record in, one row out
console.log(batch.outcome);                       // accepted
console.log(row.values[0]?.text.toString());       // 0             — what would actually be stored
console.log(row.transformed[0]?.reason);          // overflow_wrap — which is the product
for (const c of row.columns) {
  if (c.source === Source.DefaultSubstituted) {
    console.log(c.column.toString());             // ts — send it explicitly in the real INSERT
  }
}

const bad = schema.rows(Format.JSONEachRow, Buffer.from('{"x":"abc"}'));
console.log(bad.outcome, bad.errCode, bad.errMsg.toString()); // rejected 27 Cannot parse input: …

schema.close();
```

</details>

<details><summary><b>Rust</b></summary>

```rust
use chtypes::{source, CompileOptions, Format, Registry, RegistryOptions, RowsOptions, SetupOptions};

const DDL: &str = "CREATE TABLE t (x UInt8, ts DateTime DEFAULT now()) ENGINE = MergeTree ORDER BY tuple()";

fn main() -> Result<(), Box<dyn std::error::Error>> {
    // Optional, and first: the image zone and default settings are fixed once per process.
    chtypes::setup(SetupOptions { timezone: Some("UTC".into()), ..Default::default() })?;

    let registry = Registry::new(RegistryOptions::default())?;  // opens nothing
    let lib = registry.for_version("26.8")?;  // a line or an exact patch; never a nearest match
    let schema = lib.compile_table(DDL, &CompileOptions::default())?;

    let batch = schema.rows(Format::JsonEachRow, br#"{"x":256}"#, &RowsOptions::default())?;
    let row = &batch.rows[0];
    println!("{}", batch.outcome);              // accepted
    println!("{}", row.values[0].text);         // 0             — what would actually be stored
    println!("{}", row.transformed[0].reason);  // overflow_wrap — which is the product
    for c in row.columns.iter().filter(|c| c.source == source::DEFAULT_SUBSTITUTED) {
        println!("{}", c.column);               // ts: send it explicitly, or preview != stored
    }

    let bad = schema.rows(Format::JsonEachRow, br#"{"x":"abc"}"#, &RowsOptions::default())?;
    println!("{} {} {}", bad.outcome, bad.err_code, bad.err_msg); // rejected 27 Cannot parse input: …
    Ok(())
}
```

</details>

Every column name, rendering and message comes back as **bytes**, because ClickHouse accepts column names that are not UTF-8 and the library never assumes they are: `bytes` in Python, `Buffer` in TypeScript, `RawText` in Rust (which prints lossily), and a Go `string` that holds the raw bytes. The longer tours in [`../examples/`](../examples/README.md) show how each language reads them.

## What it told you

Three separate answers, and each is worth reading for what it is.

**The row was accepted.** The INSERT would have succeeded. Nothing failed, nothing warned.

**And `256` is stored as `0`.** That is `overflow_wrap`, one of the named reasons in `transformed`, and it is the thing a validator written by hand almost never catches — the row is _valid_, it is just not the row you sent. [`guides/transformations.md`](guides/transformations.md) is the full report and the rest of the reasons.

**`ts` was substituted, not stored.** The column has a volatile DEFAULT (`now()`), so chtypes resolved it here, once for the batch, and reported it in `row.columns` with `source` `default_substituted` (every column entry says where its value came from, stored or not). **Send every substituted column as an explicit value in the real INSERT** — otherwise the server re-evaluates `now()` at its own instant and the preview you showed a user is not what landed. Pin the instant for tests with the `chtypes_now_epoch_nanos` setting. A DEFAULT that draws a random or UUID value is stricter still: the library draws it, and the value is stored only if you insert the library's own output ([`reference/bindings-v1.md` §5](reference/bindings-v1.md#generated-defaults-insert-the-librarys-output-not-your-input)).

**The bad row is a verdict, not an exception.** `outcome` became `rejected` and carries ClickHouse's own code (27) and its own message. No binding raises on a bad row. Exceptions and the `Err` arm are for the machinery — a missing artifact, an unreadable document — and for schema-level answers such as a DDL the server refuses. There is a third outcome, `unsupported`, which means _this build declines to answer and a real server might well have accepted it_; never treat one as a rejection. [`index.md`](index.md#three-outcomes-and-conflating-any-two-is-a-bug) is the distinction in full.

## Freeing the handle

A schema (and a filter or a block) holds a native handle, and each binding releases it its own way. A registry and a library hold nothing to close: a loaded library lives until the process exits.

|            |                                                                                    |
| ---------- | ---------------------------------------------------------------------------------- |
| Go         | `defer schema.Close()` (a finalizer is the backup, not the contract)               |
| Python     | the context manager above, or `schema.close()`                                     |
| TypeScript | `schema.close()`, or `using schema = lib.compileTable(…)` where the runtime has it |
| Rust       | on drop                                                                            |

`using` is explicit resource management, which is a **syntax error in plain JavaScript on Node 22** — this package's own floor — so portable examples use `schema.close()`. Reach for `using` once TypeScript downlevels it for you, or once you are on Node 24 or newer.

`close` is idempotent and any order is safe: a filter or a block holds its own reference to its schema, so closing the schema first does not break them. Using an object after it is closed is a `UsageError`.

## Next

- [`guides/batches.md`](guides/batches.md) — one row was a batch of one. Real bodies have many, and what happens at the first bad one is a policy you choose.
- [`guides/discovery.md`](guides/discovery.md) — this example guessed a version and a settings profile. Ask the server instead.
- [`guides/settings.md`](guides/settings.md) — the settings channels, which one wins, and what is fixed at setup.
- [`../examples/`](../examples/README.md) — a longer runnable tour, the same sections in all four languages.
- Your language's [reference page](index.md#where-to-go) — every symbol, what it returns, and what it raises.
