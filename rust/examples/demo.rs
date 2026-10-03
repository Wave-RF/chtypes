//! What the product does, in one screen.
//!
//! ```text
//! cargo run --example demo [version]         # the installed build, e.g. 26.8
//! CHTYPES_AUTOFETCH=1 cargo run --example demo
//! ```

use chtypes::{CompileOptions, Format, Registry, RegistryOptions, RowsOptions};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let registry = Registry::new(RegistryOptions::default())?;
    let version = std::env::args()
        .nth(1)
        .unwrap_or_else(|| "26.8".to_string());
    let lib = registry.for_version(&version)?;
    println!("ClickHouse {} ({})\n", lib.version(), lib.minor());

    // A silent change. ClickHouse returns success and stores 0.
    let schema = lib.compile_table(
        "CREATE TABLE t (ts DateTime, seq UInt8) ENGINE = MergeTree ORDER BY ts",
        &CompileOptions::default(),
    )?;
    for c in &schema.describe()?.columns {
        println!("  column {:<4} {}", c.name, c.r#type);
    }
    let body = br#"{"ts":"2026-01-15 10:30:00","seq":256}"#;
    let batch = schema.rows(Format::JsonEachRow, body, &RowsOptions::default())?;
    println!("\n  outcome: {}", batch.outcome);
    for v in &batch.rows[0].values {
        println!("  stored   {:<4} {}", v.column, v.text);
    }
    for t in &batch.transformed {
        println!(
            "  changed  row {} {:<4} {} -> {}  ({}{})",
            t.row,
            t.column,
            t.input,
            t.stored,
            t.reason,
            if t.lossy { ", lossy" } else { "" }
        );
    }
    Ok(())
}
