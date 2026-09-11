//! The discovery kit: declare the server's own profile, never ask the customer.
//!
//! This library NEVER talks to ClickHouse; it ships the queries. At connect
//! time a caller (a gateway, WaveHouse) runs these three queries against the
//! deployment's own server with whatever client it already has, feeds the
//! results to the typed parsers below, and then declares what it learned:
//!
//! * `profile.version`  -> [`crate::Registry::for_version`] / the artifact to load
//! * `profile.settings` -> [`crate::Library::compile`]'s `.settings(...)`
//!   (compile-time) and per-call settings
//! * columns            -> [`reconstruct_ddl`] -> [`crate::Library::compile`]
//!
//! The connect-time pattern, in full (`docs/reference/bindings.md` §Discovery):
//!
//! ```no_run
//! use chtypes::{Format, Registry};
//!
//! # fn query(_: &str) -> Vec<u8> { Vec::new() }
//! # fn main() -> Result<(), Box<dyn std::error::Error>> {
//! // 1. run the discovery queries ONCE per connection/tenant, with the
//! //    ClickHouse client the host already has; cache the profile per
//! //    deployment.
//! let version = chtypes::parse_version_result(&query(chtypes::QUERY_SERVER_VERSION))?;
//! let settings = chtypes::parse_changed_settings_result(&query(chtypes::QUERY_CHANGED_SETTINGS))?;
//!
//! // 2. resolve the artifact from the server's own version string.
//! let registry = Registry::from_env_or_default()?;
//! let lib = registry.for_version(&version)?;
//!
//! // 3. compile every schema with the declared profile. The settings gate
//! //    answers the server's own 115 for anything the profile spells that
//! //    this version's server would refuse — a typo is caught at declare
//! //    time, not swallowed.
//! let columns = chtypes::parse_columns_result(&query(chtypes::QUERY_TABLE_COLUMNS))?;
//! let ddl = chtypes::reconstruct_ddl(&columns)?;
//! let schema = lib.compile(&ddl).settings(settings.clone()).compile()?;
//!
//! // 4. pass the same profile (plus per-INSERT overrides) per call.
//! let batch = schema.rows(Format::JsonEachRow, br#"{"x":1}"#, &settings)?;
//! # let _ = batch; Ok(()) }
//! ```
//!
//! The queries return exactly what the server believes, spelled the way the
//! server spells it, which is what the settings gate (the core repository's C ABI specification,
//! Settings rule 2) validates against.

use std::collections::BTreeMap;

use crate::error::{Error, Result};

/// [`QUERY_SERVER_VERSION`]'s result: the deployment's exact release — the
/// string [`crate::Registry::for_version`] resolves (minor line or exact patch
/// both work). One row: `{"version":"25.8.28.1"}`.
pub const QUERY_SERVER_VERSION: &str = "SELECT version() AS version FORMAT JSONEachRow";

/// Every query setting the deployment runs at a NON-default value — the whole
/// declared profile, from the server itself. One row per setting:
/// `{"name":"flatten_nested","value":"0"}`. `system.settings.value` is already
/// a String; the parsed pairs feed [`crate::Library::compile`]'s
/// `.settings(...)` and per-call settings verbatim.
pub const QUERY_CHANGED_SETTINGS: &str =
    "SELECT name, value FROM system.settings WHERE changed FORMAT JSONEachRow";

/// One existing table, in declaration order, with everything a
/// column-declaration list needs — INCLUDING `default_kind` and
/// `default_expression`, without which a reconstructed schema silently loses
/// its DEFAULT/MATERIALIZED semantics. Uses ClickHouse's own query
/// parameters: send `param_db` / `param_table` (HTTP) or bind `{db}`/`{table}`
/// (native).
pub const QUERY_TABLE_COLUMNS: &str = "SELECT name, type, default_kind, default_expression, position \
     FROM system.columns WHERE database = {db:String} AND table = {table:String} \
     ORDER BY position FORMAT JSONEachRow";

/// What discovery learns about one deployment: the exact release and the
/// settings it runs changed from defaults. Cache one per deployment (or per
/// tenant on bring-your-own-ClickHouse) and declare it.
///
/// `settings` is the crate's settings shape — name/value string pairs, sorted
/// by name with a later duplicate winning — so it passes to
/// [`crate::Library::compile`]'s `.settings(...)` and [`crate::Schema::rows`]
/// directly, as `profile.settings.clone()` and `&profile.settings`
/// respectively.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct ServerProfile {
    /// The exact release, e.g. `"25.8.28.1"` — feed
    /// [`crate::Registry::for_version`].
    pub version: String,
    /// The changed settings — feed [`crate::Library::compile`]'s
    /// `.settings(...)` and per-call settings.
    pub settings: Vec<(String, String)>,
}

/// One row of [`QUERY_TABLE_COLUMNS`].
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct DiscoveredColumn {
    /// The column name, exactly as the server spells it — the flattened-Nested
    /// idiom (`n.a`) included.
    pub name: String,
    /// The column type, the server's own text, passed through verbatim.
    pub r#type: String,
    /// `""` | `"DEFAULT"` | `"MATERIALIZED"` | `"ALIAS"` | `"EPHEMERAL"`.
    pub default_kind: String,
    /// The DEFAULT/MATERIALIZED/ALIAS expression, the server's own text; empty
    /// when there is none.
    pub default_expression: String,
    /// 1-based declaration position, from the query's `ORDER BY position`.
    pub position: u64,
}

fn discovery_err<T>(message: impl Into<String>) -> Result<T> {
    Err(Error::Discovery {
        message: message.into(),
    })
}

/// A short prefix of a line, for error messages — mirrors the reference kit's
/// `%.60s`.
fn excerpt(line: &[u8]) -> String {
    let head = &line[..line.len().min(60)];
    String::from_utf8_lossy(head).into_owned()
}

/// Split a JSONEachRow body into one parsed object per non-empty line.
fn json_each_row_docs(body: &[u8]) -> Result<Vec<serde_json::Map<String, serde_json::Value>>> {
    let mut out = Vec::new();
    for line in body.split(|&b| b == b'\n') {
        let line = trim_ascii(line);
        if line.is_empty() {
            continue;
        }
        if line[0] != b'{' {
            return discovery_err(format!("not a JSONEachRow line: {}", excerpt(line)));
        }
        match serde_json::from_slice::<serde_json::Value>(line) {
            Ok(serde_json::Value::Object(map)) => out.push(map),
            Ok(_) => return discovery_err(format!("not a JSON object: {}", excerpt(line))),
            Err(e) => {
                return discovery_err(format!("bad JSONEachRow line: {e}: {}", excerpt(line)));
            }
        }
    }
    Ok(out)
}

fn trim_ascii(mut b: &[u8]) -> &[u8] {
    while let Some((f, rest)) = b.split_first() {
        if f.is_ascii_whitespace() {
            b = rest;
        } else {
            break;
        }
    }
    while let Some((l, rest)) = b.split_last() {
        if l.is_ascii_whitespace() {
            b = rest;
        } else {
            break;
        }
    }
    b
}

/// A JSON value that may arrive quoted or bare (ClickHouse quotes 64-bit
/// integers in JSON output by default), as text with digits preserved exactly
/// — an integer's digits are never routed through a float.
fn json_text(v: &serde_json::Value) -> String {
    match v {
        serde_json::Value::String(s) => s.clone(),
        other => other.to_string(),
    }
}

fn text_field(row: &serde_json::Map<String, serde_json::Value>, key: &str) -> String {
    row.get(key).map(json_text).unwrap_or_default()
}

/// Read [`QUERY_SERVER_VERSION`]'s JSONEachRow body.
///
/// # Errors
///
/// * [`Error::Discovery`] — the body is not one JSONEachRow row carrying a
///   non-empty `version` field. Client-side, with no ClickHouse code: the
///   server never saw a question it could reject.
pub fn parse_version_result(body: &[u8]) -> Result<String> {
    let docs = json_each_row_docs(body)?;
    if docs.len() != 1 {
        return discovery_err(format!(
            "version query returned {} rows, want 1",
            docs.len()
        ));
    }
    let Some(v) = docs[0].get("version") else {
        return discovery_err("version query row has no `version` field");
    };
    let out = json_text(v);
    if out.is_empty() {
        return discovery_err("version query returned an empty version");
    }
    Ok(out)
}

/// Read [`QUERY_CHANGED_SETTINGS`]' JSONEachRow body.
///
/// An empty body is a stock server: an empty list, not an error. Pairs come
/// back sorted by name, a later duplicate winning, ready to pass to
/// [`crate::Library::compile`]'s `.settings(...)` and, by reference, to
/// [`crate::Schema::rows`].
///
/// # Errors
///
/// * [`Error::Discovery`] — a line is not a JSON object, or a row lacks its
///   `name`/`value` field.
pub fn parse_changed_settings_result(body: &[u8]) -> Result<Vec<(String, String)>> {
    let docs = json_each_row_docs(body)?;
    let mut map = BTreeMap::new();
    for row in &docs {
        let Some(name) = row.get("name") else {
            return discovery_err("settings row has no `name` field");
        };
        let Some(value) = row.get("value") else {
            return discovery_err("settings row has no `value` field");
        };
        map.insert(json_text(name), json_text(value));
    }
    Ok(map.into_iter().collect())
}

/// Read [`QUERY_TABLE_COLUMNS`]' JSONEachRow body, in the query's
/// `ORDER BY position` order.
///
/// `position` must survive both the quoted spelling (stock HTTP output,
/// `output_format_json_quote_64bit_integers=1`) and the bare one — digits
/// parsed from text, never through a float. No rows is an error: an empty
/// table description means a wrong database/table, or no access.
///
/// # Errors
///
/// * [`Error::Discovery`] — a malformed line, a row missing `name`/`type`,
///   or an empty result (wrong database/table, or no access).
pub fn parse_columns_result(body: &[u8]) -> Result<Vec<DiscoveredColumn>> {
    let docs = json_each_row_docs(body)?;
    let mut out = Vec::with_capacity(docs.len());
    for row in &docs {
        let col = DiscoveredColumn {
            name: text_field(row, "name"),
            r#type: text_field(row, "type"),
            default_kind: text_field(row, "default_kind"),
            default_expression: text_field(row, "default_expression"),
            position: row
                .get("position")
                .and_then(|v| json_text(v).parse::<u64>().ok())
                .unwrap_or(0),
        };
        if col.name.is_empty() || col.r#type.is_empty() {
            return discovery_err(format!(
                "columns row missing name/type: {}",
                excerpt(
                    &serde_json::Value::Object(row.clone())
                        .to_string()
                        .into_bytes()
                )
            ));
        }
        out.push(col);
    }
    if out.is_empty() {
        return discovery_err(
            "columns query returned no rows — wrong database/table, or no access",
        );
    }
    Ok(out)
}

/// Quote an identifier the way ClickHouse DDL requires: plain
/// `[A-Za-z_][A-Za-z0-9_]*` stays bare, anything else is backticked with
/// backticks doubled. `system.columns` can return anything — the flattened
/// Nested idiom (`n.a`), spaces, keywords.
fn backquote_if_needed(name: &str) -> String {
    let bytes = name.as_bytes();
    let plain = !bytes.is_empty()
        && !bytes[0].is_ascii_digit()
        && bytes
            .iter()
            .all(|&c| c == b'_' || c.is_ascii_alphanumeric());
    if plain {
        return name.to_string();
    }
    format!("`{}`", name.replace('`', "``"))
}

/// Turn [`QUERY_TABLE_COLUMNS`]' rows back into the column-declaration list
/// [`crate::Library::compile`] takes. It is a spelling exercise, not a
/// semantic one: types and expressions are the server's own text, passed
/// through verbatim, and the library's own compile is the judge of the
/// result.
///
/// Two facts a caller must know, both properties of the server rather than of
/// this function:
///
/// * `system.columns` reports the table AS STORED. Under the default
///   `flatten_nested=1` a `Nested(a, b)` column appears as its flattened
///   `n.a`/`n.b` Array columns and reconstructs to exactly those — which is
///   the same table. Under `flatten_nested=0` it appears as one column of type
///   `Nested(...)`, which also reconstructs directly. Reconstruction is
///   therefore shape-faithful either way, PROVIDED the same `flatten_nested`
///   value is declared via [`crate::Library::compile`]'s `.settings(...)`
///   that the table was created under — which is what the discovered
///   [`ServerProfile::settings`] carries.
/// * a MATERIALIZED/ALIAS column reconstructs with its expression; an
///   EPHEMERAL column may legitimately have an empty `default_expression`.
///
/// # Errors
///
/// * [`Error::Discovery`] — no columns, a column without name/type, a
///   DEFAULT/MATERIALIZED/ALIAS column without its expression, an expression
///   with no kind (which would silently drop semantics), or an unknown
///   `default_kind`.
pub fn reconstruct_ddl(cols: &[DiscoveredColumn]) -> Result<String> {
    if cols.is_empty() {
        return discovery_err("no columns to reconstruct");
    }
    let mut out = String::new();
    for (i, c) in cols.iter().enumerate() {
        if c.name.is_empty() || c.r#type.is_empty() {
            return discovery_err(format!("column {i} has no name/type"));
        }
        if i > 0 {
            out.push_str(", ");
        }
        out.push_str(&backquote_if_needed(&c.name));
        out.push(' ');
        out.push_str(&c.r#type);
        match c.default_kind.as_str() {
            "" => {
                if !c.default_expression.is_empty() {
                    return discovery_err(format!(
                        "column {} has a default_expression but no default_kind",
                        c.name
                    ));
                }
            }
            kind @ ("DEFAULT" | "MATERIALIZED" | "ALIAS") => {
                if c.default_expression.is_empty() {
                    return discovery_err(format!(
                        "column {} is {kind} but has no default_expression",
                        c.name
                    ));
                }
                out.push(' ');
                out.push_str(kind);
                out.push(' ');
                out.push_str(&c.default_expression);
            }
            "EPHEMERAL" => {
                out.push_str(" EPHEMERAL");
                if !c.default_expression.is_empty() {
                    out.push(' ');
                    out.push_str(&c.default_expression);
                }
            }
            other => {
                return discovery_err(format!(
                    "column {} has unknown default_kind {other:?}",
                    c.name
                ));
            }
        }
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    // The discovery-kit parsers, against canned server output — mirroring the
    // reference kit's tests. The compile-through-the-library half lives in
    // tests/integration.rs, where a real artifact judges the reconstruction.

    #[test]
    fn the_query_texts_are_the_specs_verbatim() {
        // docs/reference/bindings.md §Discovery: "each SDK carries it verbatim, FORMAT
        // JSONEachRow included". Byte-identical to the reference kit.
        assert_eq!(
            QUERY_SERVER_VERSION,
            "SELECT version() AS version FORMAT JSONEachRow"
        );
        assert_eq!(
            QUERY_CHANGED_SETTINGS,
            "SELECT name, value FROM system.settings WHERE changed FORMAT JSONEachRow"
        );
        assert_eq!(
            QUERY_TABLE_COLUMNS,
            "SELECT name, type, default_kind, default_expression, position \
             FROM system.columns WHERE database = {db:String} AND table = {table:String} \
             ORDER BY position FORMAT JSONEachRow"
        );
    }

    #[test]
    fn parse_version_result_reads_one_row_and_rejects_the_rest() {
        let v = parse_version_result(b"{\"version\":\"25.8.28.1\"}\n").unwrap();
        assert_eq!(v, "25.8.28.1");
        assert!(parse_version_result(b"").is_err(), "empty body must error");
        assert!(
            parse_version_result(b"{\"version\":\"a\"}\n{\"version\":\"b\"}").is_err(),
            "two rows must error"
        );
        assert!(
            parse_version_result(b"{\"nope\":\"x\"}").is_err(),
            "missing field must error"
        );
    }

    #[test]
    fn parse_changed_settings_result_reads_the_profile() {
        let body = b"{\"name\":\"flatten_nested\",\"value\":\"0\"}\n\
                     {\"name\":\"date_time_input_format\",\"value\":\"best_effort\"}\n\
                     {\"name\":\"max_block_size\",\"value\":\"65409\"}\n";
        let m = parse_changed_settings_result(body).unwrap();
        assert_eq!(m.len(), 3);
        let get = |k: &str| m.iter().find(|(n, _)| n == k).map(|(_, v)| v.as_str());
        assert_eq!(get("flatten_nested"), Some("0"));
        assert_eq!(get("date_time_input_format"), Some("best_effort"));
        assert_eq!(get("max_block_size"), Some("65409"));

        // A stock server: no changed settings is an empty list, not an error.
        assert_eq!(parse_changed_settings_result(b"\n").unwrap(), vec![]);
    }

    #[test]
    fn parse_changed_settings_duplicates_are_last_write_wins() {
        // The spec rule (docs/reference/bindings.md §Discovery, 2026-08-26): a
        // duplicated name resolves to the LATER row, in every SDK, so one
        // server answer can never discover two different profiles.
        let body = b"{\"name\":\"flatten_nested\",\"value\":\"1\"}\n\
                     {\"name\":\"max_block_size\",\"value\":\"65409\"}\n\
                     {\"name\":\"flatten_nested\",\"value\":\"0\"}\n";
        let m = parse_changed_settings_result(body).unwrap();
        assert_eq!(
            m,
            vec![
                ("flatten_nested".to_string(), "0".to_string()),
                ("max_block_size".to_string(), "65409".to_string()),
            ]
        );
    }

    #[test]
    fn parse_columns_result_survives_quoted_and_bare_positions() {
        // position arrives quoted on stock HTTP output
        // (output_format_json_quote_64bit_integers=1) and bare when a client
        // unsets that; both must parse, digits preserved exactly.
        let body = br#"{"name":"id","type":"UInt64","default_kind":"","default_expression":"","position":"1"}
{"name":"ts","type":"DateTime","default_kind":"DEFAULT","default_expression":"now()","position":2}
{"name":"n.a","type":"Array(Int64)","default_kind":"","default_expression":"","position":"3"}
"#;
        let cols = parse_columns_result(body).unwrap();
        assert_eq!(cols.len(), 3);
        assert_eq!(cols[0].position, 1);
        assert_eq!(cols[1].position, 2);
        assert_eq!(cols[2].position, 3);
        assert_eq!(cols[1].default_kind, "DEFAULT");
        assert_eq!(cols[1].default_expression, "now()");
        assert!(
            parse_columns_result(b"").is_err(),
            "no rows must error — an empty table description is a wrong database/table"
        );
    }

    #[test]
    fn a_19_digit_position_never_routes_through_a_float() {
        // 2^64 - 1: exactly representable as a u64, not as an f64. Both the
        // quoted and the bare spelling must keep every digit.
        for body in [
            &br#"{"name":"x","type":"UInt8","position":"18446744073709551615"}"#[..],
            &br#"{"name":"x","type":"UInt8","position":18446744073709551615}"#[..],
        ] {
            let cols = parse_columns_result(body).unwrap();
            assert_eq!(cols[0].position, 18446744073709551615, "body {body:?}");
        }
    }

    #[test]
    fn reconstruct_ddl_spells_kinds_and_backquotes_only_where_needed() {
        let cols = [
            DiscoveredColumn {
                name: "id".into(),
                r#type: "UInt64".into(),
                ..Default::default()
            },
            DiscoveredColumn {
                name: "ts".into(),
                r#type: "DateTime".into(),
                default_kind: "DEFAULT".into(),
                default_expression: "now()".into(),
                ..Default::default()
            },
            DiscoveredColumn {
                name: "n.a".into(),
                r#type: "Array(Int64)".into(),
                ..Default::default()
            },
            DiscoveredColumn {
                name: "e".into(),
                r#type: "UInt8".into(),
                default_kind: "EPHEMERAL".into(),
                ..Default::default()
            },
            DiscoveredColumn {
                name: "m".into(),
                r#type: "UInt64".into(),
                default_kind: "MATERIALIZED".into(),
                default_expression: "id + 1".into(),
                ..Default::default()
            },
        ];
        let ddl = reconstruct_ddl(&cols).unwrap();
        assert!(
            ddl.contains("`n.a` Array(Int64)"),
            "flattened-Nested name not backquoted: {ddl}"
        );
        assert!(ddl.contains("ts DateTime DEFAULT now()"), "{ddl}");
        assert!(ddl.contains("e UInt8 EPHEMERAL"), "{ddl}");
        assert!(ddl.contains("m UInt64 MATERIALIZED id + 1"), "{ddl}");

        // A backtick inside a name is doubled, and a leading digit backquotes.
        assert_eq!(backquote_if_needed("a`b"), "`a``b`");
        assert_eq!(backquote_if_needed("1x"), "`1x`");
        assert_eq!(backquote_if_needed("with space"), "`with space`");
        assert_eq!(backquote_if_needed("plain_Name9"), "plain_Name9");
    }

    #[test]
    fn reconstruct_ddl_error_surfaces() {
        // DEFAULT/MATERIALIZED/ALIAS require an expression.
        for kind in ["DEFAULT", "MATERIALIZED", "ALIAS"] {
            assert!(
                reconstruct_ddl(&[DiscoveredColumn {
                    name: "x".into(),
                    r#type: "UInt8".into(),
                    default_kind: kind.into(),
                    ..Default::default()
                }])
                .is_err(),
                "{kind} without expression must error"
            );
        }
        // An unknown kind errors rather than passing through.
        assert!(
            reconstruct_ddl(&[DiscoveredColumn {
                name: "x".into(),
                r#type: "UInt8".into(),
                default_kind: "WEIRD".into(),
                ..Default::default()
            }])
            .is_err()
        );
        // An expression with no kind errors: it would silently drop semantics.
        assert!(
            reconstruct_ddl(&[DiscoveredColumn {
                name: "x".into(),
                r#type: "UInt8".into(),
                default_expression: "1".into(),
                ..Default::default()
            }])
            .is_err()
        );
        // No columns is a wrong table, not an empty DDL.
        assert!(reconstruct_ddl(&[]).is_err());
        // EPHEMERAL may omit its expression, and may carry one.
        assert_eq!(
            reconstruct_ddl(&[DiscoveredColumn {
                name: "e".into(),
                r#type: "UInt8".into(),
                default_kind: "EPHEMERAL".into(),
                default_expression: "7".into(),
                ..Default::default()
            }])
            .unwrap(),
            "e UInt8 EPHEMERAL 7"
        );
    }

    #[test]
    fn discovery_errors_carry_no_clickhouse_code() {
        let err = parse_version_result(b"").unwrap_err();
        assert!(matches!(err, Error::Discovery { .. }), "{err:?}");
        assert_eq!(err.code(), None);
        assert!(!err.is_unsupported());
    }
}
