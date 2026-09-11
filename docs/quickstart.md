# Quickstart

One artifact, one schema, one row — the same program in each language. It shows the thing chtypes exists for: the row is **accepted**, and `256` is silently stored as `0`.

You need a binding and an artifact for line 25.8. [`install.md`](install.md) if you have neither; any published line works, 25.8 is just the one spelled below.

## The program

<details open><summary><b>Go</b></summary>

```go
package main

import (
	"fmt"
	"log"

	"github.com/wave-rf/chtypes/go/chtypes"
)

func main() {
	reg, err := chtypes.NewRegistry("") // "" = walk the search path
	if err != nil {
		log.Fatal(err)
	}
	lib, err := reg.For("25.8") // a line or an exact patch; never a nearest match
	if err != nil {
		log.Fatal(err)
	}
	schema, err := lib.CompileDDL("x UInt8, ts DateTime DEFAULT now()")
	if err != nil {
		log.Fatal(err)
	}
	defer schema.Close()

	batch, err := schema.Rows(chtypes.JSONEachRow, []byte(`{"x":256}`), nil)
	if err != nil {
		log.Fatal(err)
	}
	row := batch.Rows[0]
	fmt.Println(batch.Outcome)             // accepted
	fmt.Println(row.Values[0].Text)        // 0             — what would actually be stored
	fmt.Println(row.Transformed[0].Reason) // overflow_wrap — which is the product
	fmt.Println(row.Substituted)           // ts: send it explicitly, or preview != stored

	bad, err := schema.Rows(chtypes.JSONEachRow, []byte(`{"x":"abc"}`), nil)
	if err != nil {
		log.Fatal(err)
	}
	fmt.Println(bad.Outcome, bad.ErrCode, bad.ErrMsg) // rejected 27 Cannot parse input: …
}
```

</details>

<details><summary><b>Python</b></summary>

```python
from chtypes import Format, Registry

registry = Registry()                   # walks the search path
library = registry.for_version("25.8")  # a line or an exact patch; never a nearest match

with library.compile_ddl("x UInt8, ts DateTime DEFAULT now()") as schema:
    batch = schema.rows(Format.JSON_EACH_ROW, b'{"x":256}\n')
    bad = schema.rows(Format.JSON_EACH_ROW, b'{"x":"abc"}\n')

row = batch.rows[0]
print(batch.outcome)             # accepted      — Outcome is a StrEnum; compare with Outcome.ACCEPTED
print(row.value("x").text)       # 0             — what would actually be stored
print(row.transformed[0].reason) # overflow_wrap — which is the product
print(row.substituted)           # ts: send it explicitly, or preview != stored

print(bad.outcome, bad.err_code, bad.err_msg)  # rejected 27 Cannot parse input: …
```

</details>

<details><summary><b>TypeScript</b></summary>

```ts
import { Format, Registry } from '@wavehouse/chtypes';

const registry = new Registry();     // walks the search path
const lib = registry.for('25.8');    // a line or an exact patch; never a nearest match
const schema = lib.compileDdl('x UInt8, ts DateTime DEFAULT now()');

const batch = schema.rows(Format.JSONEachRow, Buffer.from('{"x":256}'));
const row = batch.rows[0];
console.log(batch.outcome);              // accepted
console.log(row.values[0].text);         // 0             — what would actually be stored
console.log(row.transformed[0]?.reason); // overflow_wrap — which is the product
console.log(row.substituted[0]?.column); // ts — send it explicitly in the real INSERT

const bad = schema.rows(Format.JSONEachRow, Buffer.from('{"x":"abc"}'));
console.log(bad.outcome, bad.errCode, bad.errMsg); // rejected 27 Cannot parse input: …

schema.close();
```

</details>

<details><summary><b>Rust</b></summary>

```rust
use chtypes::{Format, Registry, NO_SETTINGS};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let registry = Registry::from_search_path();  // walks the search path
    let lib = registry.for_version("25.8")?;      // a line or an exact patch; never a nearest match
    let schema = lib.compile("x UInt8, ts DateTime DEFAULT now()").compile()?;

    let batch = schema.rows(Format::JsonEachRow, br#"{"x":256}"#, NO_SETTINGS)?;
    let row = &batch.rows[0];
    println!("{:?}", batch.outcome);           // Accepted
    println!("{}", row.values[0].text);        // 0             — what would actually be stored
    println!("{}", row.transformed[0].reason); // overflow_wrap — which is the product
    println!("{:?}", row.substituted);         // ts: send it explicitly, or preview != stored

    let bad = schema.rows(Format::JsonEachRow, br#"{"x":"abc"}"#, NO_SETTINGS)?;
    println!("{:?} {} {}", bad.outcome, bad.err_code, bad.err_msg); // Rejected 27 Cannot parse input: …
    Ok(())
}
```

</details>

## What it told you

Three separate answers, and each is worth reading for what it is.

**The row was accepted.** The INSERT would have succeeded. Nothing failed, nothing warned.

**And `256` is stored as `0`.** That is `overflow_wrap`, one of the named reasons in `transformed`, and it is the thing a validator written by hand almost never catches — the row is _valid_, it is just not the row you sent. [`guides/transformations.md`](guides/transformations.md) is the full report and the rest of the reasons.

**`ts` was substituted, not stored.** The column has a volatile DEFAULT (`now()`), so chtypes resolved it here, once for the batch, and told you in `substituted`. **Send every substituted column as an explicit value in the real INSERT** — otherwise the server re-evaluates `now()` at its own instant and the preview you showed a user is not what landed. Pin the instant for tests with the `chtypes_now_epoch_nanos` setting.

**The bad row is a verdict, not an exception.** `outcome` became `rejected` and carries ClickHouse's own code (27) and its own message. No binding raises on a bad row. Exceptions and the `Err` arm are for the machinery — a missing artifact, an unreadable document — and for schema-level answers such as a DDL the server refuses. There is a third outcome, `unsupported`, which means _this build declines to answer and a real server might well have accepted it_; never treat one as a rejection. [`index.md`](index.md#three-outcomes-and-conflating-any-two-is-a-bug) is the distinction in full.

## Freeing the handle

Each binding frees the schema its own way, and all four release the native handle deterministically:

|            |                                                                                          |
| ---------- | ---------------------------------------------------------------------------------------- |
| Go         | `defer schema.Close()` (a finalizer is the backup, not the contract)                     |
| Python     | the context manager above, or `schema.close()`                                           |
| TypeScript | `schema.close()`                                                                         |
| Rust       | on drop; a `Filter` or `Block` borrows its `Schema`, so the wrong order does not compile |

TypeScript also has `using schema = lib.compileDdl(…)`, which is nicer, but explicit resource management is a **syntax error in plain JavaScript on Node 22** — this package's own floor. `schema.close()` works everywhere, so portable examples use it. Reach for `using` once TypeScript downlevels it for you, or once you are on Node ≥ 24.

Closing a schema first closes any filters and blocks open on it, which is the order the C ABI requires.

## Next

- [`guides/batches.md`](guides/batches.md) — one row was a batch of one. Real bodies have many, and what happens at the first bad one is a policy you choose.
- [`guides/discovery.md`](guides/discovery.md) — this example guessed a version and a settings profile. Ask the server instead.
- [`guides/settings.md`](guides/settings.md) — why the compile call takes a settings profile, and which channel wins.
- [`../examples/`](../examples/README.md) — a longer runnable tour, the same sections in all four languages.
- Your language's [reference page](index.md#where-to-go) — every symbol, what it returns, and what it raises.
