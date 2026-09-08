//! What the product does, in one screen.
//!
//! ```text
//! cargo run --example demo                      # $CHTYPES_REGISTRY, else the per-user cache
//! CHTYPES_REGISTRY=/path/to/registry cargo run --example demo
//! ```

use std::path::PathBuf;

use chtypes::{Format, NO_SETTINGS, Outcome, Registry, SETTING_NOW_EPOCH_NANOS};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let fallback = chtypes::default_registry_dir();
    let registry = Registry::from_env_or(&fallback)?;
    println!(
        "registry {} -> {:?}\n",
        registry.dir().display(),
        registry.versions()
    );

    let version = std::env::args()
        .nth(1)
        .unwrap_or_else(|| "25.8".to_string());
    let lib = registry.for_version(&version)?;
    println!("ClickHouse {} ({})\n", lib.version(), lib.minor());

    // 1. A silent change. ClickHouse returns success and stores 0.
    let schema = lib.compile("ts DateTime, seq UInt8").compile()?;
    for c in schema.columns() {
        println!("  column {:<4} {}", c.name, c.ty);
    }
    let body = br#"{"ts":"2026-01-15 10:30:00","seq":256}"#;
    let batch = schema.rows(Format::JsonEachRow, body, NO_SETTINGS)?;
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
            if t.lossy() { ", lossy" } else { "" }
        );
    }

    // 2. A volatile DEFAULT, pinned. The caller must send `ts` explicitly.
    let schema = lib
        .compile("a UInt8, ts DateTime DEFAULT now()")
        .compile()?;
    let batch = schema.rows(
        Format::JsonEachRow,
        br#"{"a":1}"#,
        &[(SETTING_NOW_EPOCH_NANOS, "1700000000000000000")],
    )?;
    println!("\n  pinned now(): {:?}", batch.rows[0].values);
    for s in &batch.rows[0].substituted {
        println!(
            "  send explicitly: {} = {} (was {})",
            s.column, s.text, s.expr
        );
    }

    // 3. A TTL-expired row: accepted per row, not stored per batch.
    let mut schema = lib.compile("ts DateTime, v UInt8").compile()?;
    schema.set_ttl("ts + INTERVAL 1 DAY")?;
    let batch = schema.rows(
        Format::JsonEachRow,
        br#"{"ts":"2020-01-01 00:00:00","v":9}"#,
        &[(SETTING_NOW_EPOCH_NANOS, "1700000000000000000")],
    )?;
    println!(
        "\n  row {} but the part holds {:?}: {:?}",
        batch.rows[0].outcome,
        batch.engine_rows.as_deref().map(<[String]>::len),
        batch
            .transformed
            .iter()
            .map(|t| t.reason.as_str())
            .collect::<Vec<_>>()
    );

    // 4. A rejection carries ClickHouse's own code, and a decline carries -2.
    let schema = lib.compile("x UInt8").compile()?;
    let batch = schema.rows(Format::JsonEachRow, br#"{"x":"abc"}"#, NO_SETTINGS)?;
    assert_eq!(batch.outcome, Outcome::Rejected);
    println!("\n  rejected: [{}] {}", batch.err_code, batch.err_msg);

    let schema = lib.compile("h String DEFAULT hostName()").compile()?;
    let batch = schema.rows(Format::JsonEachRow, b"{}", NO_SETTINGS)?;
    println!(
        "  declined: [{}] {} ({})",
        batch.rows[0].err_code, batch.rows[0].outcome, batch.rows[0].err_msg
    );

    Ok(())
}
