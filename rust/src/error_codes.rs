//! The error-code table (revision 6): one loaded build's own code → name map.
//!
//! There is no table in this crate, and there must never be one. The table is
//! a property of the BUILD: codes join and leave between ClickHouse lines, and
//! one number can name two different errors on two lines (903 is
//! `LICENSE_EXPIRED` on 25.3 and 25.8, absent on 25.10, and
//! `DISTRIBUTED_CACHE_REGISTRY_SHUTDOWN` on 26.2 through 26.9). Every answer
//! therefore comes from
//! [`crate::Library::error_codes`], i.e. from `chs_error_codes` of the library
//! being asked, and `scripts/check-no-error-code-table.py` fails the build if a
//! literal code → name table appears in any binding.

use std::collections::HashMap;
use std::sync::OnceLock;

use serde_json::Value;

use crate::error::{Error, Result};

/// One row of a build's error-code table: a ClickHouse error code and the name
/// THAT BUILD gives it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ErrorCodeEntry {
    /// The ClickHouse error code.
    pub code: i32,
    /// The name this build gives it, exactly as the server prints it.
    pub name: String,
}

/// One loaded library's own error-code table, from `chs_error_codes` —
/// obtained from [`crate::Library::error_codes`] and valid for that library's
/// ClickHouse line only. Immutable once built.
///
/// Lookups answer only what the build's own table holds: an unknown code, a
/// negative code (the ABI's `-1` and `-2` sentinels included — they are not
/// ClickHouse codes) or an unknown name is `None`, never a synthesized
/// spelling. Names match exactly and case-sensitively, as the server prints
/// them.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ErrorCodeTable {
    /// Ascending by code.
    entries: Vec<ErrorCodeEntry>,
    by_code: HashMap<i32, usize>,
    by_name: HashMap<String, i32>,
}

impl ErrorCodeTable {
    /// Build from entries as the library listed them. An entry with an empty
    /// name is not a name and is dropped; a negative code is not a ClickHouse
    /// code and is dropped; where a code or a name repeats, the first entry
    /// wins. The result is ordered by code, whatever order the entries came in.
    fn from_entries(entries: Vec<ErrorCodeEntry>) -> ErrorCodeTable {
        let mut seen_codes = std::collections::HashSet::new();
        let mut by_name = HashMap::new();
        let mut kept = Vec::new();
        for e in entries {
            if e.name.is_empty()
                || e.code < 0
                || seen_codes.contains(&e.code)
                || by_name.contains_key(&e.name)
            {
                continue;
            }
            seen_codes.insert(e.code);
            by_name.insert(e.name.clone(), e.code);
            kept.push(e);
        }
        kept.sort_by_key(|e| e.code);
        let by_code = kept.iter().enumerate().map(|(i, e)| (e.code, i)).collect();
        ErrorCodeTable {
            entries: kept,
            by_code,
            by_name,
        }
    }

    /// The name this build gives `code`, or `None` for an unknown or negative
    /// code.
    pub fn name(&self, code: i32) -> Option<&str> {
        if code < 0 {
            return None;
        }
        self.by_code
            .get(&code)
            .map(|&i| self.entries[i].name.as_str())
    }

    /// The code this build gives `name` — an exact, case-sensitive match — or
    /// `None`.
    pub fn code(&self, name: &str) -> Option<i32> {
        self.by_name.get(name).copied()
    }

    /// Every entry, in ascending code order.
    pub fn iter(&self) -> std::slice::Iter<'_, ErrorCodeEntry> {
        self.entries.iter()
    }

    /// How many codes this build names.
    pub fn len(&self) -> usize {
        self.entries.len()
    }

    /// Whether the table is empty.
    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }
}

impl<'a> IntoIterator for &'a ErrorCodeTable {
    type Item = &'a ErrorCodeEntry;
    type IntoIter = std::slice::Iter<'a, ErrorCodeEntry>;

    fn into_iter(self) -> Self::IntoIter {
        self.iter()
    }
}

/// Build a table from a `chs_error_codes` document,
/// `{"error_codes":[{"code":N,"name":"…"}, …]}`.
///
/// Unknown keys are ignored, and an absent (or `null`) key is its default,
/// like every document this ABI hands back; anything else of the wrong SHAPE
/// is a bad document, never a guess. The shape is checked by hand rather than
/// by a serde derive, because a derived struct also deserializes from a JSON
/// ARRAY — a top-level `[]` would read as an empty table and `[252, "X"]` as an
/// entry — which no other binding accepts. `serde_json` is still the right
/// reader here, unlike for the row documents: this one carries no stored
/// value, only the build's own ASCII names.
pub(crate) fn parse(doc: &[u8]) -> Result<ErrorCodeTable> {
    let v: Value = serde_json::from_slice(doc).map_err(|e| Error::BadDocument {
        message: format!("chs_error_codes: {e}"),
        offset: byte_offset(doc, e.line(), e.column()),
    })?;
    let bad = |why: String| Error::BadDocument {
        message: format!("chs_error_codes: {why}"),
        offset: 0,
    };
    let Some(top) = v.as_object() else {
        return Err(bad("the document is not a JSON object".to_string()));
    };
    let no_rows: Vec<Value> = Vec::new();
    let rows: &[Value] = match top.get("error_codes") {
        None | Some(Value::Null) => no_rows.as_slice(),
        Some(Value::Array(rows)) => rows.as_slice(),
        Some(other) => return Err(bad(format!("error_codes is not an array: {other}"))),
    };
    let mut entries = Vec::with_capacity(rows.len());
    for row in rows {
        let Some(entry) = row.as_object() else {
            return Err(bad(format!("an entry is not an object: {row}")));
        };
        let code = match entry.get("code") {
            None | Some(Value::Null) => 0,
            Some(c) => c
                .as_i64()
                .and_then(|n| i32::try_from(n).ok())
                .ok_or_else(|| bad(format!("an entry's code is not an integer: {row}")))?,
        };
        let name = match entry.get("name") {
            None | Some(Value::Null) => String::new(),
            Some(Value::String(n)) => n.clone(),
            Some(_) => return Err(bad(format!("an entry's name is not a string: {row}"))),
        };
        entries.push(ErrorCodeEntry { code, name });
    }
    Ok(ErrorCodeTable::from_entries(entries))
}

/// serde_json reports a 1-based line and column; [`Error::BadDocument`] carries
/// a byte offset.
fn byte_offset(doc: &[u8], line: usize, column: usize) -> usize {
    let mut offset = 0;
    for (i, l) in doc.split(|&b| b == b'\n').enumerate() {
        if i + 1 == line {
            return offset + column.saturating_sub(1);
        }
        offset += l.len() + 1;
    }
    doc.len()
}

/// A library's table once it has been built, and only then. A `NULL` answer
/// from `chs_error_codes` is a guarded exception inside the library —
/// transient by definition — so it is [`Error::NoDocument`] and is NOT
/// remembered: the next call asks again. A missing symbol
/// ([`Error::PredatesFeature`]) is not remembered either.
pub(crate) fn cached(
    cell: &OnceLock<ErrorCodeTable>,
    fetch: impl FnOnce() -> Result<Option<Vec<u8>>>,
) -> Result<&ErrorCodeTable> {
    if let Some(table) = cell.get() {
        return Ok(table);
    }
    let doc = fetch()?.ok_or(Error::NoDocument {
        feature: "chs_error_codes",
    })?;
    let table = parse(&doc)?;
    // Two threads may both build it; the first one kept wins, and the answer
    // is the same either way — it never changes for a loaded library.
    Ok(cell.get_or_init(|| table))
}

#[cfg(test)]
mod tests {
    use super::*;

    // Out of order on purpose, with unknown keys at both levels, an entry with
    // no name and a negative code — none of which may reach a lookup.
    const FAKE: &[u8] = br#"{
      "error_codes": [
        {"code": 252, "name": "TOO_MANY_PARTS", "since": "whatever"},
        {"code": 0, "name": "OK"},
        {"code": 1, "name": "UNSUPPORTED_METHOD"},
        {"code": 7, "name": ""},
        {"code": -2, "name": "NOT_A_CLICKHOUSE_CODE"},
        {"code": 47, "name": "UNKNOWN_IDENTIFIER"}
      ],
      "generator": "ignored"
    }"#;

    const KNOWN: [(i32, &str); 4] = [
        (0, "OK"),
        (1, "UNSUPPORTED_METHOD"),
        (47, "UNKNOWN_IDENTIFIER"),
        (252, "TOO_MANY_PARTS"),
    ];

    #[test]
    fn lookups_answer_the_document_and_nothing_else() {
        let t = parse(FAKE).expect("parse");
        for (code, name) in KNOWN {
            assert_eq!(t.name(code), Some(name));
            assert_eq!(t.code(name), Some(code));
        }
        for code in [2, 7, 999_999, -1, -2, -3] {
            assert_eq!(t.name(code), None, "code {code}");
        }
        for name in [
            "too_many_parts",
            "Too_Many_Parts",
            " TOO_MANY_PARTS",
            "TOO_MANY_PARTS ",
            "",
            "NOT_A_CLICKHOUSE_CODE",
            "NO_SUCH_ERROR",
        ] {
            assert_eq!(t.code(name), None, "name {name:?}");
        }
    }

    #[test]
    fn iteration_is_ascending() {
        let t = parse(FAKE).expect("parse");
        let got: Vec<(i32, &str)> = t.iter().map(|e| (e.code, e.name.as_str())).collect();
        assert_eq!(got, KNOWN.to_vec());
        let via_into: Vec<i32> = (&t).into_iter().map(|e| e.code).collect();
        assert_eq!(via_into, vec![0, 1, 47, 252]);
        assert_eq!(t.len(), 4);
    }

    #[test]
    fn the_first_entry_wins_on_a_repeat() {
        let t = parse(
            br#"{"error_codes":[{"code":5,"name":"A_NAME"},{"code":5,"name":"B_NAME"},{"code":6,"name":"A_NAME"}]}"#,
        )
        .expect("parse");
        assert_eq!(t.name(5), Some("A_NAME"));
        assert_eq!(t.name(6), None);
        assert_eq!(t.len(), 1);
    }

    #[test]
    fn absent_keys_are_an_empty_table() {
        let docs: [&[u8]; 4] = [
            br#"{}"#,
            br#"{"error_codes":null}"#,
            br#"{"error_codes":[]}"#,
            br#"{"something_else":[1,2,3]}"#,
        ];
        for doc in docs {
            let t = parse(doc).expect("parse");
            assert!(t.is_empty());
            assert_eq!(t.name(0), None);
        }
    }

    #[test]
    fn a_bad_document_is_bad_document() {
        // The same list every binding's bad-document test runs: a truncated
        // document, a top-level value that is not an object, error_codes that
        // is not an array, an entry that is not an object, and a field of the
        // wrong type.
        let docs: [&[u8]; 11] = [
            br#"{"error_codes":["#,
            br#"[]"#,
            br#"null"#,
            br#"42"#,
            br#""x""#,
            br#"{"error_codes":{}}"#,
            br#"{"error_codes":[null]}"#,
            br#"{"error_codes":[[252,"X"]]}"#,
            br#"{"error_codes":[{"code":"252","name":"X"}]}"#,
            br#"{"error_codes":[{"code":252.5,"name":"X"}]}"#,
            br#"{"error_codes":[{"code":1,"name":5}]}"#,
        ];
        for doc in docs {
            match parse(doc) {
                Err(Error::BadDocument { .. }) => {}
                other => panic!("{}: {other:?}", String::from_utf8_lossy(doc)),
            }
        }
    }

    #[test]
    fn the_cache_keeps_a_built_table_and_nothing_else() {
        let cell = OnceLock::new();
        // A NULL answer: the general error, not a decline, and not remembered.
        match cached(&cell, || Ok(None)) {
            Err(e @ Error::NoDocument { .. }) => assert!(!e.is_unsupported()),
            other => panic!("NULL: {other:?}"),
        }
        assert!(cell.get().is_none());
        // A missing symbol: the decline type, not remembered either.
        match cached(&cell, || {
            Err(Error::PredatesFeature {
                feature: "chs_error_codes",
            })
        }) {
            Err(e) => assert!(e.is_unsupported()),
            Ok(t) => panic!("missing symbol answered {t:?}"),
        }
        assert!(cell.get().is_none());
        let first = cached(&cell, || Ok(Some(FAKE.to_vec()))).expect("built");
        assert_eq!(first.name(252), Some("TOO_MANY_PARTS"));
        let again = cached(&cell, || {
            panic!("the cache asked the library again after a table was built")
        })
        .expect("cached");
        assert!(std::ptr::eq(first, again));
    }
}
