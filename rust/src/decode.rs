//! The document decoders (`docs/reference/bindings-v1.md` §5): one to one,
//! with `serde_json` as the only parser.
//!
//! * **Stock JSON only.** Every document is read by `serde_json`; the library
//!   guarantees RFC 8259 documents (no bare `inf`/`nan`, integers beyond 2^53
//!   as strings, names that are not UTF-8 in their `_b64` form).
//! * **Absent is the default; unknown keys are ignored** at every object level
//!   (ABI v2 rule r2), so the library can add a field without breaking this
//!   crate.
//! * **An unlisted vocabulary value is its `Unknown`** (rule r3): kept for that
//!   field alone, it never fails the document, the row or the batch.
//! * **A duplicate key, or a value of the wrong JSON type, is an
//!   [`Error::Internal`]** naming the document and the key.
//! * **Nothing is computed.** A vocabulary fact is read from the generated
//!   tables, keyed by the value the document carries.
//! * **Names are bytes.** A name-carrying key is `<key>` (valid UTF-8) or
//!   `<key>_b64` (standard base64 of the raw bytes), exactly one of the two;
//!   either surfaces as [`RawText`]. The same two spellings are read for every
//!   data-derived string field (`stored`, `input`, `err`, ...), where neither
//!   present means empty.

use std::collections::BTreeMap;

use base64::Engine as _;
use serde::de::{Deserialize, Deserializer, MapAccess, SeqAccess, Visitor};
use serde_json::{Map, Value as Json};

use crate::abi2::vocab_gen::{
    BYTES_SUFFIX, DeclinedLayer, DeclinedTier, DefaultKind, FilterOutcome, MergeReason, Outcome,
    Reason, Source, VALUE_BYTES, Verdict,
};
use crate::error::{Error, Result};
use crate::raw::RawText;
use crate::result::{
    AtMergeEntry, BatchResult, BuildInfo, Capabilities, Column, Computed, DeclinedSetting,
    DiscoveredColumn, Discovery, EngineCell, ErrorCodeEntry, ErrorCodeTable, FilterResult,
    FilterRowError, Framing, Header, RowResult, SchemaDescription, SchemaReplicated, SchemaServer,
    Span, Transform, Value,
};

// -------------------------------------------------------------- strict parse

/// A JSON value parsed with an explicit duplicate-key check at every depth:
/// `serde_json`'s own `Value` deserialization lets a later key silently win.
struct Strict(Json);

impl<'de> Deserialize<'de> for Strict {
    fn deserialize<D: Deserializer<'de>>(d: D) -> std::result::Result<Strict, D::Error> {
        struct V;
        impl<'de> Visitor<'de> for V {
            type Value = Strict;

            fn expecting(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                f.write_str("a JSON value with no duplicate object key")
            }

            fn visit_bool<E>(self, v: bool) -> std::result::Result<Strict, E> {
                Ok(Strict(Json::Bool(v)))
            }

            fn visit_i64<E>(self, v: i64) -> std::result::Result<Strict, E> {
                Ok(Strict(Json::from(v)))
            }

            fn visit_u64<E>(self, v: u64) -> std::result::Result<Strict, E> {
                Ok(Strict(Json::from(v)))
            }

            fn visit_f64<E: serde::de::Error>(self, v: f64) -> std::result::Result<Strict, E> {
                serde_json::Number::from_f64(v)
                    .map(|n| Strict(Json::Number(n)))
                    .ok_or_else(|| E::custom("a number that is not finite"))
            }

            fn visit_str<E>(self, v: &str) -> std::result::Result<Strict, E> {
                Ok(Strict(Json::String(v.to_string())))
            }

            fn visit_string<E>(self, v: String) -> std::result::Result<Strict, E> {
                Ok(Strict(Json::String(v)))
            }

            fn visit_unit<E>(self) -> std::result::Result<Strict, E> {
                Ok(Strict(Json::Null))
            }

            fn visit_seq<A: SeqAccess<'de>>(
                self,
                mut seq: A,
            ) -> std::result::Result<Strict, A::Error> {
                let mut out = Vec::new();
                while let Some(Strict(v)) = seq.next_element()? {
                    out.push(v);
                }
                Ok(Strict(Json::Array(out)))
            }

            fn visit_map<A: MapAccess<'de>>(
                self,
                mut map: A,
            ) -> std::result::Result<Strict, A::Error> {
                let mut out = Map::new();
                while let Some(key) = map.next_key::<String>()? {
                    if out.contains_key(&key) {
                        return Err(serde::de::Error::custom(format!(
                            "duplicate object key {key:?}"
                        )));
                    }
                    let Strict(v) = map.next_value()?;
                    out.insert(key, v);
                }
                Ok(Strict(Json::Object(out)))
            }
        }
        d.deserialize_any(V)
    }
}

/// Parse one document, strictly. A document that does not parse, or repeats a
/// key, is a library bug: [`Error::Internal`].
pub(crate) fn parse(doc: &'static str, bytes: &[u8]) -> Result<Json> {
    serde_json::from_slice::<Strict>(bytes)
        .map(|s| s.0)
        .map_err(|e| Error::internal(format!("the {doc} document does not decode: {e}")))
}

// -------------------------------------------------------------------- reader

/// A JSON object being read: the document it belongs to and where in it, so
/// every refusal names both.
struct Obj<'a> {
    doc: &'static str,
    at: String,
    map: &'a Map<String, Json>,
}

impl<'a> Obj<'a> {
    fn of(doc: &'static str, at: impl Into<String>, v: &'a Json) -> Result<Obj<'a>> {
        let at = at.into();
        match v.as_object() {
            Some(map) => Ok(Obj { doc, at, map }),
            None => Err(Error::internal(format!(
                "the {doc} document: {at} is not a JSON object"
            ))),
        }
    }

    fn fail(&self, key: &str, want: &str) -> Error {
        Error::internal(format!(
            "the {} document: {}{key} is not {want}",
            self.doc,
            if self.at.is_empty() {
                String::new()
            } else {
                format!("{}.", self.at)
            }
        ))
    }

    fn child(&self, key: &str) -> String {
        if self.at.is_empty() {
            key.to_string()
        } else {
            format!("{}.{key}", self.at)
        }
    }

    /// A key that is present and not JSON `null`.
    fn get(&self, key: &str) -> Option<&'a Json> {
        self.map.get(key).filter(|v| !v.is_null())
    }

    fn string(&self, key: &str) -> Result<String> {
        match self.get(key) {
            None => Ok(String::new()),
            Some(Json::String(s)) => Ok(s.clone()),
            Some(_) => Err(self.fail(key, "a string")),
        }
    }

    fn opt_string(&self, key: &str) -> Result<Option<String>> {
        match self.get(key) {
            None => Ok(None),
            Some(Json::String(s)) => Ok(Some(s.clone())),
            Some(_) => Err(self.fail(key, "a string")),
        }
    }

    fn boolean(&self, key: &str) -> Result<bool> {
        match self.get(key) {
            None => Ok(false),
            Some(Json::Bool(b)) => Ok(*b),
            Some(_) => Err(self.fail(key, "a boolean")),
        }
    }

    fn int32(&self, key: &str) -> Result<i32> {
        match self.get(key) {
            None => Ok(0),
            Some(v) => v
                .as_i64()
                .and_then(|n| i32::try_from(n).ok())
                .ok_or_else(|| self.fail(key, "a 32-bit integer")),
        }
    }

    /// An unsigned 64-bit integer: a JSON number, or the decimal string the
    /// document uses beyond 2^53.
    fn uint64_of(&self, key: &str, v: &Json) -> Result<u64> {
        match v {
            Json::Number(n) => n.as_u64(),
            Json::String(s) => s.parse::<u64>().ok(),
            _ => None,
        }
        .ok_or_else(|| self.fail(key, "an unsigned 64-bit integer"))
    }

    fn uint64(&self, key: &str) -> Result<u64> {
        match self.get(key) {
            None => Ok(0),
            Some(v) => self.uint64_of(key, v),
        }
    }

    fn opt_uint64(&self, key: &str) -> Result<Option<u64>> {
        match self.get(key) {
            None => Ok(None),
            Some(v) => self.uint64_of(key, v).map(Some),
        }
    }

    /// An array's elements; absent or `null` is empty.
    fn array(&self, key: &str) -> Result<&'a [Json]> {
        match self.get(key) {
            None => Ok(&[]),
            Some(Json::Array(a)) => Ok(a),
            Some(_) => Err(self.fail(key, "an array")),
        }
    }

    fn opt_array(&self, key: &str) -> Result<Option<&'a [Json]>> {
        match self.get(key) {
            None => Ok(None),
            Some(Json::Array(a)) => Ok(Some(a)),
            Some(_) => Err(self.fail(key, "an array")),
        }
    }

    fn object(&self, key: &str) -> Result<Option<Obj<'a>>> {
        match self.get(key) {
            None => Ok(None),
            Some(v) => Obj::of(self.doc, self.child(key), v).map(Some),
        }
    }

    /// An object whose values are plain JSON strings (a server's settings or
    /// macros, the caller's own values given back): `None` when absent, `Some`,
    /// even of an empty map, when present, so absent and `{}` stay apart.
    fn string_map(&self, key: &str) -> Result<Option<BTreeMap<String, String>>> {
        let Some(o) = self.object(key)? else {
            return Ok(None);
        };
        o.map
            .iter()
            .map(|(k, v)| match v {
                Json::String(s) => Ok((k.clone(), s.clone())),
                _ => Err(Error::internal(format!(
                    "the {} document: {}.{k} is not a string",
                    o.doc, o.at
                ))),
            })
            .collect::<Result<BTreeMap<_, _>>>()
            .map(Some)
    }

    fn strings(&self, key: &str) -> Result<Vec<String>> {
        self.array(key)?
            .iter()
            .map(|v| {
                v.as_str()
                    .map(str::to_string)
                    .ok_or_else(|| self.fail(key, "an array of strings"))
            })
            .collect()
    }

    /// The bytes under `key` or `key_b64`: `None` when neither is present, and
    /// an error when both are.
    fn bytes_opt(&self, key: &str) -> Result<Option<Vec<u8>>> {
        let b64_key = format!("{key}{BYTES_SUFFIX}");
        match (self.get(key), self.get(&b64_key)) {
            (None, None) => Ok(None),
            (Some(_), Some(_)) => Err(Error::internal(format!(
                "the {} document: {} carries both {key} and {b64_key}",
                self.doc,
                if self.at.is_empty() {
                    "the document"
                } else {
                    &self.at
                }
            ))),
            (Some(Json::String(s)), None) => Ok(Some(s.clone().into_bytes())),
            (Some(_), None) => Err(self.fail(key, "a string")),
            (None, Some(Json::String(s))) => base64::engine::general_purpose::STANDARD
                .decode(s)
                .map(Some)
                .map_err(|_| self.fail(&b64_key, "standard base64")),
            (None, Some(_)) => Err(self.fail(&b64_key, "a string")),
        }
    }

    /// A data-derived string: empty when absent.
    fn bytes(&self, key: &str) -> Result<RawText> {
        Ok(RawText::from(self.bytes_opt(key)?.unwrap_or_default()))
    }

    /// A name: exactly one of `key` and `key_b64` must be present.
    fn name(&self, key: &str) -> Result<RawText> {
        self.bytes_opt(key)?.map(RawText::from).ok_or_else(|| {
            Error::internal(format!(
                "the {} document: {} carries neither {key} nor {key}{BYTES_SUFFIX}",
                self.doc,
                if self.at.is_empty() {
                    "the document"
                } else {
                    &self.at
                }
            ))
        })
    }

    /// The raw bytes of a scalar `String` or `FixedString` value, from
    /// `value_b64` alone: absent when the entry carries none.
    fn value(&self) -> Result<Option<RawText>> {
        match self.get(VALUE_BYTES) {
            None => Ok(None),
            Some(Json::String(s)) => base64::engine::general_purpose::STANDARD
                .decode(s)
                .map(|b| Some(RawText::from(b)))
                .map_err(|_| self.fail(VALUE_BYTES, "standard base64")),
            Some(_) => Err(self.fail(VALUE_BYTES, "a string")),
        }
    }

    /// An array of name objects (`{"name"}` or `{"name_b64"}`), as bytes.
    fn name_objects(&self, array_key: &str) -> Result<Vec<RawText>> {
        self.names(array_key, "name")
    }

    /// Each element of an array is an object carrying a name under `key`.
    fn names(&self, array_key: &str, key: &str) -> Result<Vec<RawText>> {
        self.array(array_key)?
            .iter()
            .enumerate()
            .map(|(i, v)| {
                Obj::of(self.doc, format!("{}[{i}]", self.child(array_key)), v)?.name(key)
            })
            .collect()
    }
}

fn span(o: &Obj<'_>, key: &str) -> Result<Option<Span>> {
    match o.object(key)? {
        None => Ok(None),
        Some(s) => Ok(Some(Span {
            off: s.uint64("off")?,
            len: s.uint64("len")?,
        })),
    }
}

fn spans(o: &Obj<'_>, key: &str) -> Result<Vec<Span>> {
    o.array(key)?
        .iter()
        .enumerate()
        .map(|(i, v)| {
            let s = Obj::of(o.doc, format!("{}[{i}]", o.child(key)), v)?;
            Ok(Span {
                off: s.uint64("off")?,
                len: s.uint64("len")?,
            })
        })
        .collect()
}

// --------------------------------------------------------------------- rows

fn value_of(o: &Obj<'_>) -> Result<Value> {
    // An unlisted src is its `Unknown` (rule r3): kept, never a failure, and not
    // stored until the description says otherwise. An ABSENT src is no value at
    // all, and stays a malformed entry.
    if o.get("src").is_none() {
        return Err(o.fail("src", "present"));
    }
    let source = Source::from_wire(&o.string("src")?);
    let is_stored = source.is_stored();
    Ok(Value {
        column: o.name("name")?,
        text: o.bytes("stored")?,
        null: o.boolean("null")?,
        source,
        is_stored,
        value: o.value()?,
    })
}

fn transform_of(o: &Obj<'_>) -> Result<Transform> {
    // An unlisted reason is its `Unknown` (rule r3), and reports the fallback's
    // `lossy` fact.
    let reason = Reason::from_wire(&o.string("reason")?);
    Ok(Transform {
        column: o.bytes("column")?,
        input: o.bytes("input")?,
        stored: o.bytes("stored")?,
        lossy: reason.lossy(),
        reason,
        row: o.uint64("row")?,
    })
}

fn transforms(o: &Obj<'_>, key: &str) -> Result<Vec<Transform>> {
    o.array(key)?
        .iter()
        .enumerate()
        .map(|(i, v)| transform_of(&Obj::of(o.doc, format!("{}[{i}]", o.child(key)), v)?))
        .collect()
}

fn row_of(o: &Obj<'_>) -> Result<RowResult> {
    let mut columns = Vec::new();
    for (i, v) in o.array("cols")?.iter().enumerate() {
        columns.push(value_of(&Obj::of(
            o.doc,
            format!("{}[{i}]", o.child("cols")),
            v,
        )?)?);
    }
    let values = columns.iter().filter(|c| c.is_stored).cloned().collect();
    let mut computed = Vec::new();
    for (i, v) in o.array("computed")?.iter().enumerate() {
        let c = Obj::of(o.doc, format!("{}[{i}]", o.child("computed")), v)?;
        computed.push(Computed {
            column: c.name("name")?,
            kind: c.string("kind")?,
            text: c.bytes("stored")?,
            value: c.value()?,
        });
    }
    let mut unknown_fields = Vec::new();
    for (i, v) in o.array("unknown_fields")?.iter().enumerate() {
        // A name-like entry: a plain string, or an object carrying one.
        match v {
            Json::String(s) => unknown_fields.push(RawText::from(s.as_str())),
            Json::Object(_) => {
                let u = Obj::of(o.doc, format!("{}[{i}]", o.child("unknown_fields")), v)?;
                unknown_fields.push(u.name("name")?);
            }
            _ => return Err(o.fail("unknown_fields", "an array of names")),
        }
    }
    Ok(RowResult {
        outcome: Outcome::from_wire(&o.string("outcome")?),
        err_code: o.int32("code")?,
        err_msg: o.bytes("err")?,
        columns,
        values,
        transformed: transforms(o, "transformed")?,
        unknown_fields,
        unsupported_settings: o.name_objects("unsupported_settings")?,
        computed,
        verdict: o.opt_string("verdict")?.map(|v| Verdict::from_wire(&v)),
        verdict_code: o.int32("verdict_code")?,
        verdict_err: o.bytes("verdict_err")?,
        partition_id: o.bytes_opt("partition_id")?.map(RawText::from),
        input_span: span(o, "input_span")?,
    })
}

/// Decode one `row` document.
pub(crate) fn row(bytes: &[u8]) -> Result<RowResult> {
    let json = parse("row", bytes)?;
    row_of(&Obj::of("row", "", &json)?)
}

// ------------------------------------------------------------------- batches

/// Decode one `batch` document and the export buffer the call returned.
pub(crate) fn batch(bytes: &[u8], payload: Option<Vec<u8>>) -> Result<BatchResult> {
    let json = parse("batch", bytes)?;
    let o = Obj::of("batch", "", &json)?;
    let mut rows = Vec::new();
    for (i, v) in o.array("rows")?.iter().enumerate() {
        rows.push(row_of(&Obj::of("batch", format!("rows[{i}]"), v)?)?);
    }
    let engine_rows = match o.opt_array("engine_rows")? {
        None => None,
        Some(rows) => {
            let mut out = Vec::new();
            for (i, row) in rows.iter().enumerate() {
                let Json::Array(cells) = row else {
                    return Err(o.fail("engine_rows", "a list of rows, each a list of cells"));
                };
                let mut cells_out = Vec::new();
                for (j, cell) in cells.iter().enumerate() {
                    let c = Obj::of("batch", format!("engine_rows[{i}][{j}]"), cell)?;
                    cells_out.push(EngineCell {
                        column: c.name("name")?,
                        text: c.bytes("stored")?,
                        null: c.boolean("null")?,
                        value: c.value()?,
                    });
                }
                out.push(cells_out);
            }
            Some(out)
        }
    };
    let row_spans = match o.opt_array("row_spans")? {
        None => None,
        Some(_) => Some(spans(&o, "row_spans")?),
    };
    let framing = match o.object("framing")? {
        None => None,
        Some(f) => Some(framing_of(&f)?),
    };
    let mut at_merge = Vec::new();
    for (i, v) in o.array("at_merge")?.iter().enumerate() {
        at_merge.push(at_merge_of(&Obj::of(
            "batch",
            format!("at_merge[{i}]"),
            v,
        )?)?);
    }
    Ok(BatchResult {
        outcome: Outcome::from_wire(&o.string("outcome")?),
        err_code: o.int32("code")?,
        err_msg: o.bytes("err")?,
        rows,
        rows_read: o.uint64("rows_read")?,
        rows_skipped: o.uint64("rows_skipped")?,
        transformed: transforms(&o, "transformed")?,
        unsupported_settings: o.name_objects("unsupported_settings")?,
        engine_rows,
        at_merge,
        payload,
        spans: row_spans,
        export_declined: o.bytes("export_declined")?,
        rows_passed: o.uint64("rows_passed")?,
        rows_cut: o.uint64("rows_cut")?,
        partition_count: o.opt_uint64("partition_count")?,
        unconsumed: spans(&o, "unconsumed")?,
        framing,
    })
}

/// One entry of `at_merge`. An unlisted reason is its `Unknown` (rule r3): kept,
/// never a failure.
fn at_merge_of(o: &Obj<'_>) -> Result<AtMergeEntry> {
    let input_rows = match o.opt_array("input_rows")? {
        None => None,
        Some(items) => Some(
            items
                .iter()
                .map(|v| o.uint64_of("input_rows", v))
                .collect::<Result<Vec<u64>>>()?,
        ),
    };
    Ok(AtMergeEntry {
        row: o.uint64("row")?,
        reason: MergeReason::from_wire(&o.string("reason")?),
        column: o.bytes_opt("column")?.map(RawText::from),
        stored: o.bytes_opt("stored")?.map(RawText::from),
        input_rows,
    })
}

fn framing_of(f: &Obj<'_>) -> Result<Framing> {
    let bom_skipped = match f.get("bom_skipped") {
        None => None,
        Some(Json::Bool(b)) => Some(*b),
        Some(_) => return Err(f.fail("bom_skipped", "a boolean or null")),
    };
    let header = match f.object("header")? {
        None => None,
        Some(h) => Some(Header {
            consumed: h.boolean("consumed")?,
            lines: h.uint64("lines")?,
            names: h.names("names", "name")?,
        }),
    };
    Ok(Framing {
        bom_skipped,
        container: f.opt_string("container")?,
        header,
    })
}

// ------------------------------------------------------------------- filters

/// Decode one `filter_result` document.
pub(crate) fn filter_result(bytes: &[u8]) -> Result<FilterResult> {
    let json = parse("filter_result", bytes)?;
    let o = Obj::of("filter_result", "", &json)?;
    let verdicts = o
        .string("verdicts")?
        .chars()
        .map(|c| Verdict::from_wire(c.encode_utf8(&mut [0u8; 4])))
        .collect();
    let mut errors = Vec::new();
    for (i, v) in o.array("errors")?.iter().enumerate() {
        let e = Obj::of("filter_result", format!("errors[{i}]"), v)?;
        errors.push(FilterRowError {
            row: e.uint64("row")?,
            code: e.int32("code")?,
            msg: e.bytes("err")?,
        });
    }
    Ok(FilterResult {
        outcome: FilterOutcome::from_wire(&o.string("outcome")?),
        err_code: o.int32("code")?,
        err_msg: o.bytes("err")?,
        rows_read: o.uint64("rows_read")?,
        unsupported_settings: o.name_objects("unsupported_settings")?,
        verdicts,
        errors,
    })
}

// ---------------------------------------------------- schema, discovery, ...

/// A schema description's `filter_declined_settings`: absent is empty. An
/// unlisted tier or layer is its `Unknown` (rule r3): kept, never a failure.
fn declined_settings(o: &Obj<'_>) -> Result<Vec<DeclinedSetting>> {
    let key = "filter_declined_settings";
    let mut out = Vec::new();
    for (i, v) in o.array(key)?.iter().enumerate() {
        let e = Obj::of(o.doc, format!("{}[{i}]", o.child(key)), v)?;
        out.push(DeclinedSetting {
            name: e.name("name")?,
            tier: DeclinedTier::from_wire(&e.string("tier")?),
            layer: DeclinedLayer::from_wire(&e.string("layer")?),
        });
    }
    Ok(out)
}

/// Decode one `schema_description` document.
pub(crate) fn schema_description(bytes: &[u8]) -> Result<SchemaDescription> {
    let json = parse("schema_description", bytes)?;
    let o = Obj::of("schema_description", "", &json)?;
    let mut columns = Vec::new();
    for (i, v) in o.array("columns")?.iter().enumerate() {
        let c = Obj::of("schema_description", format!("columns[{i}]"), v)?;
        // An unlisted default_kind is its `Unknown` (rule r3): kept, never a failure.
        let default_kind = DefaultKind::from_wire(&c.string("default_kind")?);
        columns.push(Column {
            name: c.name("name")?,
            r#type: c.bytes("type")?,
            default_kind,
            default_expr: c.bytes("default_expression")?,
        });
    }
    // Both are absent on a schema compiled without a server, so that document
    // decodes exactly as it did before servers existed.
    let server = match o.object("server")? {
        None => None,
        Some(sm) => Some(SchemaServer {
            timezone: sm.string("timezone")?,
            settings: sm.string_map("settings")?.unwrap_or_default(),
            macros: sm.string_map("macros")?,
        }),
    };
    let replicated = match o.object("replicated")? {
        None => None,
        Some(rm) => Some(SchemaReplicated {
            zookeeper_path: rm.bytes("zookeeper_path")?,
            replica_name: rm.bytes("replica_name")?,
        }),
    };
    Ok(SchemaDescription {
        columns,
        // Over every layer known at schema compile, with or without a server.
        filter_declined_settings: declined_settings(&o)?,
        server,
        replicated,
    })
}

/// Decode one `discovery` document.
pub(crate) fn discovery(bytes: &[u8]) -> Result<Discovery> {
    let json = parse("discovery", bytes)?;
    let o = Obj::of("discovery", "", &json)?;
    let mut columns = Vec::new();
    for (i, v) in o.array("columns")?.iter().enumerate() {
        let c = Obj::of("discovery", format!("columns[{i}]"), v)?;
        columns.push(DiscoveredColumn {
            name: c.name("name")?,
            declaration: c.bytes("declaration")?,
        });
    }
    Ok(Discovery {
        columns,
        columns_sql: o.bytes("columns_sql")?,
    })
}

/// Decode one `error_code_table` document: a JSON array of `{code, name}`.
pub(crate) fn error_code_table(bytes: &[u8]) -> Result<ErrorCodeTable> {
    let json = parse("error_code_table", bytes)?;
    let Json::Array(items) = &json else {
        return Err(Error::internal(
            "the error_code_table document is not a JSON array",
        ));
    };
    let mut entries = Vec::with_capacity(items.len());
    for (i, v) in items.iter().enumerate() {
        let e = Obj::of("error_code_table", format!("[{i}]"), v)?;
        entries.push(ErrorCodeEntry {
            code: e.int32("code")?,
            name: e.string("name")?,
        });
    }
    Ok(ErrorCodeTable::from_entries(entries))
}

/// Decode one `live_handles` document: handle kind to live count.
pub(crate) fn live_handles(bytes: &[u8]) -> Result<BTreeMap<String, u64>> {
    let json = parse("live_handles", bytes)?;
    let o = Obj::of("live_handles", "", &json)?;
    o.map
        .keys()
        .map(|k| Ok((k.clone(), o.uint64(k)?)))
        .collect()
}

// ---------------------------------------------------------------- build_info

/// Decode `build_info`, already strictly parsed by the loader, into its public
/// form. A field of the wrong type is a malformed `build_info`, which the
/// caller maps to the artifact-corrupt class.
pub(crate) fn build_info(
    map: &Map<String, Json>,
    raw: &[u8],
) -> std::result::Result<BuildInfo, String> {
    let o = Obj {
        doc: "build_info",
        at: String::new(),
        map,
    };
    let text = |k: &str| o.string(k).map_err(|e| e.to_string());
    let int = |k: &str| {
        o.get(k)
            .and_then(Json::as_i64)
            .ok_or_else(|| format!("{k} is not an integer"))
    };
    let caps = match o.object("capabilities").map_err(|e| e.to_string())? {
        None => return Err("capabilities is missing".to_string()),
        Some(c) => Capabilities {
            input_formats: c.strings("input_formats").map_err(|e| e.to_string())?,
            export_formats: c.strings("export_formats").map_err(|e| e.to_string())?,
            doc_flags: c.strings("doc_flags").map_err(|e| e.to_string())?,
            features: c.strings("features").map_err(|e| e.to_string())?,
        },
    };
    Ok(BuildInfo {
        schema: int("schema")?,
        abi: int("abi")?,
        abi_fingerprint: text("abi_fingerprint")?,
        clickhouse_version: text("clickhouse_version")?,
        channel: text("channel")?,
        clickhouse_minor: text("clickhouse_minor")?,
        clickhouse_commit: text("clickhouse_commit")?,
        core_commit: text("core_commit")?,
        build: text("build")?,
        inputs_sha256: text("inputs_sha256")?,
        os: text("os")?,
        arch: text("arch")?,
        toolchain: map.get("toolchain").cloned().unwrap_or(Json::Null),
        capabilities: caps,
        raw: raw.to_vec(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::abi2::vocab_gen::{
        DefaultKind, FilterOutcome, MergeReason, Outcome, Reason, Source, Verdict,
    };

    fn internal(e: Error) -> String {
        match e {
            Error::Internal(c) => c.message.to_lossy().into_owned(),
            other => panic!("want an internal error, got {other:?}"),
        }
    }

    #[test]
    fn a_duplicate_key_is_internal_at_any_depth() {
        let msg = internal(row(br#"{"outcome":"accepted","cols":[{"name":"a","null":false,"src":"input","null":true}]}"#).unwrap_err());
        assert!(msg.contains("duplicate"), "{msg}");
        let msg = internal(row(br#"{"outcome":"accepted","outcome":"rejected"}"#).unwrap_err());
        assert!(msg.contains("duplicate") && msg.contains("row"), "{msg}");
    }

    #[test]
    fn a_wrong_json_type_names_the_document_and_the_key() {
        let msg = internal(row(br#"{"outcome":"accepted","cols":"x"}"#).unwrap_err());
        assert!(msg.contains("row") && msg.contains("cols"), "{msg}");
        let msg = internal(
            row(br#"{"outcome":"accepted","cols":[{"name":"a","src":"input","null":"no"}]}"#)
                .unwrap_err(),
        );
        assert!(msg.contains("null"), "{msg}");
    }

    #[test]
    fn a_name_arrives_as_bytes_whether_plain_or_base64() {
        // "AP8=" is the two bytes 00 FF: not valid UTF-8.
        let r = row(br#"{"outcome":"accepted","cols":[
                {"name":"plain\u0000nul","null":false,"src":"input","stored":"1"},
                {"name_b64":"AP8=","null":false,"src":"input","stored":"2"}]}"#)
        .unwrap();
        assert_eq!(r.columns[0].column.as_bytes(), b"plain\0nul");
        assert_eq!(r.columns[1].column.as_bytes(), &[0x00, 0xff]);
        assert!(!r.columns[1].column.is_utf8());
    }

    #[test]
    fn a_name_with_both_spellings_or_neither_is_internal() {
        let both = row(
            br#"{"outcome":"accepted","cols":[{"name":"a","name_b64":"YQ==","null":false,"src":"input"}]}"#,
        )
        .unwrap_err();
        assert!(internal(both).contains("both"));
        let neither =
            row(br#"{"outcome":"accepted","cols":[{"null":false,"src":"input"}]}"#).unwrap_err();
        assert!(internal(neither).contains("neither"));
    }

    #[test]
    fn a_data_derived_string_surfaces_either_spelling_as_bytes() {
        let r = row(
            br#"{"outcome":"rejected","code":27,"err_b64":"/w==","cols":[
                {"name":"a","null":false,"src":"input","stored_b64":"/gA=","value_b64":"/gA="}]}"#,
        )
        .unwrap();
        assert_eq!(r.err_msg.as_bytes(), &[0xff]);
        assert_eq!(r.columns[0].text.as_bytes(), &[0xfe, 0x00]);
        assert_eq!(
            r.columns[0].value.as_ref().map(RawText::as_bytes),
            Some(&[0xfe, 0x00][..])
        );
        // Both spellings of one field is a document that breaks its own schema.
        let msg =
            internal(row(br#"{"outcome":"accepted","err":"x","err_b64":"eA=="}"#).unwrap_err());
        assert!(msg.contains("both"), "{msg}");
    }

    #[test]
    fn values_is_the_subset_whose_source_is_stored() {
        let r = row(br#"{"outcome":"accepted","cols":[
                {"name":"a","null":false,"src":"input","stored":"1"},
                {"name":"b","null":false,"src":"ephemeral_input"},
                {"name":"c","null":false,"src":"default_generated","stored":"9"},
                {"name":"d","null":false,"src":"skipped"}]}"#)
        .unwrap();
        assert_eq!(r.columns.len(), 4);
        let stored: Vec<&[u8]> = r.values.iter().map(|v| v.column.as_bytes()).collect();
        assert_eq!(stored, vec![&b"a"[..], &b"c"[..]]);
        assert!(r.columns[2].is_stored && !r.columns[1].is_stored);
    }

    /// Rule r3: an unlisted source is its `Unknown`, for that column alone, not
    /// stored, and the row decodes on; an absent one is still malformed.
    #[test]
    fn an_unlisted_source_is_its_unknown_and_the_row_decodes_on() {
        let r = row(br#"{"outcome":"accepted","cols":[
                {"name":"a","null":false,"src":"from_the_future","stored":"1"},
                {"name":"b","null":false,"src":"input","stored":"2"}]}"#)
        .unwrap();
        assert_eq!(
            r.columns[0].source,
            Source::Unknown("from_the_future".into())
        );
        assert!(!r.columns[0].source.is_known() && !r.columns[0].is_stored);
        assert_eq!(r.columns[0].text.as_bytes(), b"1");
        assert_eq!(r.columns[1].source, Source::Input);
        assert_eq!(r.values.len(), 1);
        let msg = internal(
            row(br#"{"outcome":"accepted","cols":[{"name":"a","null":false}]}"#).unwrap_err(),
        );
        assert!(msg.contains("src"), "{msg}");
    }

    /// Rules r2 and r3: an unknown member is ignored, and an unlisted outcome or
    /// verdict is its `Unknown`, never accepted and never an answer.
    #[test]
    fn an_unknown_outcome_is_its_unknown_and_unknown_keys_are_ignored() {
        let r =
            row(br#"{"outcome":"verdict_from_the_future","brand_new_key":{"x":[1,2]}}"#).unwrap();
        assert_eq!(
            r.outcome,
            Outcome::Unknown("verdict_from_the_future".into())
        );
        assert!(!r.outcome.is_known() && r.outcome != Outcome::Accepted);
        let f = filter_result(br#"{"outcome":"nope","verdicts":"tfedz"}"#).unwrap();
        assert_eq!(f.outcome, FilterOutcome::Unknown("nope".into()));
        assert_eq!(
            f.verdicts,
            vec![
                Verdict::True,
                Verdict::False,
                Verdict::Error,
                Verdict::Decline,
                Verdict::Unknown("z".into())
            ]
        );
        assert!(f.verdicts[2..].iter().all(|v| !v.answered()));
        assert_eq!(f.verdicts[4].as_char(), 'z');
    }

    #[test]
    fn a_transform_reads_its_lossy_fact_from_the_vocabulary() {
        let r = row(br#"{"outcome":"accepted","transformed":[
                {"column":"x","input":"256","stored":"0","reason":"overflow_wrap","row":3},
                {"column":"y","input":"a","stored":"b","reason":"reformat"},
                {"column":"z","input":"a","stored":"b","reason":"some_new_reason"}]}"#)
        .unwrap();
        assert!(r.transformed[0].lossy);
        assert_eq!(r.transformed[0].row, 3);
        assert!(!r.transformed[1].lossy);
        // A reason the description does not list is its `Unknown`, keeps its
        // spelling and takes the fallback's fact (value_changed: lossy).
        assert_eq!(r.transformed[0].reason, Reason::OverflowWrap);
        assert_eq!(
            r.transformed[2].reason,
            Reason::Unknown("some_new_reason".into())
        );
        assert_eq!(r.transformed[2].reason.as_str(), "some_new_reason");
        assert!(r.transformed[2].lossy);
    }

    #[test]
    fn framing_keeps_unknown_as_none_never_false_or_empty() {
        let b = batch(
            br#"{"outcome":"accepted","rows":[],"unconsumed":[{"off":4,"len":2}],
                "framing":{"bom_skipped":null,"container":null,"header":null}}"#,
            None,
        )
        .unwrap();
        let f = b.framing.expect("framing is present");
        assert_eq!(f.bom_skipped, None);
        assert_eq!(f.header, None);
        assert_eq!(f.container, None);
        assert_eq!(b.unconsumed, vec![Span { off: 4, len: 2 }]);

        let b = batch(
            br#"{"outcome":"accepted","rows":[],"unconsumed":[],
                "framing":{"bom_skipped":false,"container":"array",
                           "header":{"consumed":true,"lines":1,"names":[{"name":"a"},{"name_b64":"AP8="}]}}}"#,
            None,
        )
        .unwrap();
        let f = b.framing.unwrap();
        assert_eq!(f.bom_skipped, Some(false));
        assert_eq!(f.container.as_deref(), Some("array"));
        let h = f.header.unwrap();
        assert!(h.consumed);
        assert_eq!(h.names[1].as_bytes(), &[0x00, 0xff]);
    }

    #[test]
    fn a_batch_carries_its_payload_spans_and_counts() {
        let b = batch(
            br#"{"outcome":"accepted","rows_read":"9007199254740993","rows_skipped":1,
                "rows_passed":2,"rows_cut":1,"partition_count":null,
                "engine_rows":[[{"name":"a","stored":"1","null":false,"value_b64":"MQ=="}]],"row_spans":[{"off":0,"len":5}],
                "rows":[{"outcome":"accepted","partition_id":"202601","input_span":{"off":0,"len":3}}],
                "unconsumed":[],"framing":{"bom_skipped":null,"container":null,"header":null}}"#,
            Some(b"{\"a\":1}\n".to_vec()),
        )
        .unwrap();
        // Beyond 2^53 it travels as a string and is read without a float.
        assert_eq!(b.rows_read, 9_007_199_254_740_993);
        assert_eq!(b.partition_count, None);
        assert_eq!(b.payload.as_deref(), Some(&b"{\"a\":1}\n"[..]));
        assert_eq!(b.spans, Some(vec![Span { off: 0, len: 5 }]));
        assert_eq!(
            b.rows[0].partition_id.as_ref().map(RawText::as_bytes),
            Some(&b"202601"[..])
        );
        assert_eq!(b.rows[0].input_span, Some(Span { off: 0, len: 3 }));
        let cell = &b.engine_rows.as_ref().expect("engine rows")[0][0];
        assert_eq!(cell.column.as_bytes(), b"a");
        assert_eq!(cell.text.as_bytes(), b"1");
        assert!(!cell.null);
        assert_eq!(cell.value.as_ref().map(RawText::as_bytes), Some(&b"1"[..]));
    }

    #[test]
    fn a_schema_description_reads_the_default_kind_vocabulary() {
        let d = schema_description(
            br#"{"columns":[
                {"name":"a","type":"UInt8","default_kind":"","default_expression":""},
                {"name":"b","type":"DateTime","default_kind":"MATERIALIZED","default_expression":"now()"},
                {"name_b64":"AP8=","type":"String","default_kind":"EPHEMERAL","default_expression":""}]}"#,
        )
        .unwrap();
        assert_eq!(d.columns[0].default_kind, DefaultKind::None);
        assert_eq!(d.columns[1].default_kind, DefaultKind::Materialized);
        assert_eq!(d.columns[1].default_expr.as_bytes(), b"now()");
        assert_eq!(d.columns[2].name.as_bytes(), &[0x00, 0xff]);
        // Rule r3: an unlisted kind is its `Unknown`, and the column decodes on.
        let d = schema_description(
            br#"{"columns":[{"name":"a","type":"X","default_kind":"MAYBE"},{"name":"b","type":"Y"}]}"#,
        )
        .unwrap();
        assert_eq!(
            d.columns[0].default_kind,
            DefaultKind::Unknown("MAYBE".into())
        );
        assert!(!d.columns[0].default_kind.is_known());
        assert_eq!(d.columns[0].r#type.as_bytes(), b"X");
        assert_eq!(d.columns[1].default_kind, DefaultKind::None);
    }

    #[test]
    fn a_schema_description_reads_the_server_and_replicated_members() {
        const COLS: &str =
            r#""columns":[{"name":"k","type":"UInt8","default_kind":"","default_expression":""}]"#;
        let doc = |extra: &str| schema_description(format!("{{{COLS}{extra}}}").as_bytes());
        let map = |p: &[(&str, &str)]| -> BTreeMap<String, String> {
            p.iter()
                .map(|(k, v)| (k.to_string(), v.to_string()))
                .collect()
        };

        // Absent: the document is the one from before servers existed.
        let d = doc("").unwrap();
        assert!(d.server.is_none() && d.replicated.is_none() && d.columns.len() == 1);

        // A server described by {}: the image zone, settings {}, macros absent.
        let d = doc(r#","server":{"timezone":"UTC","settings":{}}"#).unwrap();
        let s = d.server.expect("server");
        assert_eq!(s.timezone, "UTC");
        assert!(s.settings.is_empty());
        assert_eq!(s.macros, None, "absent macros are unknown, not empty");

        // Macros present but empty: the complete set, empty; never read as absent.
        let d = doc(
            r#","server":{"timezone":"Asia/Tokyo","settings":{"max_threads":"8"},"macros":{}}"#,
        )
        .unwrap();
        let s = d.server.expect("server");
        assert_eq!(s.timezone, "Asia/Tokyo");
        assert_eq!(s.settings, map(&[("max_threads", "8")]));
        assert_eq!(s.macros, Some(BTreeMap::new()));

        // Macros and a Replicated engine's resolved path, a name in its _b64
        // form, and members no description names (rule r2), all ignored.
        let d = doc(
            r#","server":{"timezone":"UTC","settings":{},"macros":{"shard":"01","replica":"r1"},"x_future":1},
            "replicated":{"zookeeper_path":"/clickhouse/tables/01/db/t","replica_name_b64":"/3Ix","x_future":{"a":[1]}},
            "x_future_top":[1]"#,
        )
        .unwrap();
        assert_eq!(
            d.server.expect("server").macros,
            Some(map(&[("shard", "01"), ("replica", "r1")]))
        );
        let r = d.replicated.expect("replicated");
        assert_eq!(r.zookeeper_path.as_bytes(), b"/clickhouse/tables/01/db/t");
        assert_eq!(r.replica_name.as_bytes(), b"\xffr1");

        // Refusals: each an Error::Internal naming the document.
        for (name, extra) in [
            ("server not an object", r#","server":"UTC""#),
            (
                "a setting that is not a string",
                r#","server":{"timezone":"UTC","settings":{"max_threads":8}}"#,
            ),
            (
                "macros not an object",
                r#","server":{"timezone":"UTC","settings":{},"macros":[]}"#,
            ),
            (
                "timezone not a string",
                r#","server":{"timezone":1,"settings":{}}"#,
            ),
            (
                "a path in both forms",
                r#","replicated":{"zookeeper_path":"/a","zookeeper_path_b64":"L2E=","replica_name":"r"}"#,
            ),
            (
                "a replica name not base64",
                r#","replicated":{"zookeeper_path":"/a","replica_name_b64":"*"}"#,
            ),
        ] {
            let err = doc(extra).unwrap_err();
            assert!(
                matches!(err, Error::Internal(_)) && err.to_string().contains("schema_description"),
                "{name}: {err:?}"
            );
        }
    }

    #[test]
    fn the_error_code_table_looks_up_by_code_and_exact_name() {
        let t = error_code_table(
            br#"[{"code":0,"name":"OK"},{"code":27,"name":"CANNOT_PARSE_INPUT_ASSERTION_FAILED"}]"#,
        )
        .unwrap();
        assert_eq!(t.name(27), Some("CANNOT_PARSE_INPUT_ASSERTION_FAILED"));
        assert_eq!(t.code("OK"), Some(0));
        assert_eq!(t.name(5), None);
        assert_eq!(t.code("ok"), None);
        assert_eq!(t.iter().count(), 2);
        assert!(error_code_table(br#"{"not":"an array"}"#).is_err());
    }

    #[test]
    fn live_handles_decodes_into_a_map() {
        let m = live_handles(br#"{"chs_schema":2,"chs_filter":0}"#).unwrap();
        assert_eq!(m.get("chs_schema"), Some(&2));
        assert_eq!(m.get("chs_filter"), Some(&0));
    }

    /// ABI v2's `at_merge`, in every shape the spec gives an entry, and the
    /// batch's own `unsupported_settings` (public issue #544).
    const AT_MERGE_BATCH: &[u8] = br#"{
        "outcome": "accepted",
        "unsupported_settings": [{"name": "session_timezone"}, {"name_b64": "/w=="}],
        "at_merge": [
            {"row": 0, "reason": "ttl_delete"},
            {"row": 1, "reason": "ttl_column_reset", "column": "c", "stored": "0"},
            {"row": 2, "reason": "ttl_column_reset", "column": "t"},
            {"row": 3, "reason": "ttl_column_reset", "column_b64": "/wA=", "stored_b64": "/w=="},
            {"row": 4, "reason": "ttl_delete", "input_rows": [4, 7]},
            {"row": 5, "reason": "a_reason_nobody_listed", "x_future": {"a": [1]}, "x_future_b64": "/w=="},
            {"row": 6, "reason": "ttl_column_reset", "column": "e", "stored": "", "input_rows": []}
        ]
    }"#;

    #[test]
    fn a_batch_decodes_at_merge_one_to_one() {
        let b = batch(AT_MERGE_BATCH, None).unwrap();
        let bytes = |t: &Option<RawText>| t.as_ref().map(|r| r.as_bytes().to_vec());
        let got: Vec<_> = b
            .at_merge
            .iter()
            .map(|e| {
                (
                    e.row,
                    e.reason.clone(),
                    e.reason.is_known(),
                    bytes(&e.column),
                    bytes(&e.stored),
                    e.input_rows.clone(),
                )
            })
            .collect();
        assert_eq!(
            got,
            vec![
                (0, MergeReason::TtlDelete, true, None, None, None),
                (
                    1,
                    MergeReason::TtlColumnReset,
                    true,
                    Some(b"c".to_vec()),
                    Some(b"0".to_vec()),
                    None
                ),
                // stored absent: the column's DEFAULT is decided at the merge
                (
                    2,
                    MergeReason::TtlColumnReset,
                    true,
                    Some(b"t".to_vec()),
                    None,
                    None
                ),
                // both by their _b64 form
                (
                    3,
                    MergeReason::TtlColumnReset,
                    true,
                    Some(vec![0xff, 0x00]),
                    Some(vec![0xff]),
                    None
                ),
                (
                    4,
                    MergeReason::TtlDelete,
                    true,
                    None,
                    None,
                    Some(vec![4, 7])
                ),
                // an unlisted reason is its Unknown, and the unknown members are ignored (r2, r3)
                (
                    5,
                    MergeReason::Unknown("a_reason_nobody_listed".into()),
                    false,
                    None,
                    None,
                    None
                ),
                // an empty stored is not an absent one
                (
                    6,
                    MergeReason::TtlColumnReset,
                    true,
                    Some(b"e".to_vec()),
                    Some(vec![]),
                    Some(vec![])
                ),
            ]
        );
    }

    #[test]
    fn a_batch_carries_its_own_unsupported_settings() {
        let b = batch(AT_MERGE_BATCH, None).unwrap();
        let names: Vec<&[u8]> = b
            .unsupported_settings
            .iter()
            .map(RawText::as_bytes)
            .collect();
        assert_eq!(names, vec![&b"session_timezone"[..], &[0xff][..]]);
    }

    #[test]
    fn absent_at_merge_and_unsupported_settings_are_empty() {
        let docs: [&[u8]; 3] = [
            br#"{"outcome":"accepted"}"#,
            br#"{"outcome":"accepted","at_merge":null,"unsupported_settings":null}"#,
            br#"{"outcome":"accepted","at_merge":[],"unsupported_settings":[]}"#,
        ];
        for doc in docs {
            let b = batch(doc, None).unwrap();
            assert!(
                b.at_merge.is_empty() && b.unsupported_settings.is_empty(),
                "{b:?}"
            );
        }
    }

    #[test]
    fn an_at_merge_that_breaks_its_schema_is_internal() {
        let docs: [&[u8]; 7] = [
            br#"{"at_merge":[{"row":0,"reason":"ttl_column_reset","column":"c","column_b64":"Yw=="}]}"#,
            br#"{"at_merge":[{"row":0,"reason":"ttl_column_reset","column":"c","stored_b64":"!!"}]}"#,
            br#"{"at_merge":[{"row":0,"reason":"ttl_delete","input_rows":["x"]}]}"#,
            br#"{"at_merge":[{"row":0,"reason":"ttl_delete","input_rows":[-1]}]}"#,
            br#"{"at_merge":[{"row":0,"reason":7}]}"#,
            br#"{"at_merge":{"row":0}}"#,
            br#"{"unsupported_settings":["session_timezone"]}"#,
        ];
        for doc in docs {
            internal(batch(doc, None).unwrap_err());
        }
    }

    #[test]
    fn unsupported_settings_are_name_objects_and_surface_as_bytes() {
        let r = row(
            br#"{"outcome":"accepted","unsupported_settings":[{"name":"a_setting"},{"name_b64":"AP8="}]}"#,
        )
        .unwrap();
        assert_eq!(r.unsupported_settings[0].as_bytes(), b"a_setting");
        assert_eq!(r.unsupported_settings[1].as_bytes(), &[0x00, 0xff]);
        let f = filter_result(
            br#"{"outcome":"ok","verdicts":"t","unsupported_settings":[{"name":"x"}]}"#,
        )
        .unwrap();
        assert_eq!(f.unsupported_settings[0].as_bytes(), b"x");
        // A bare string is not a name object.
        assert!(row(br#"{"outcome":"accepted","unsupported_settings":["x"]}"#).is_err());
    }

    #[test]
    fn the_raw_value_comes_from_value_b64_alone() {
        let r = row(br#"{"outcome":"accepted","cols":[
                {"name":"s","null":false,"src":"input","stored":"\"a\"","value_b64":"YQ=="},
                {"name":"t","null":false,"src":"input","stored":"1"}],
              "computed":[{"name":"m","kind":"materialized","stored":"2","value_b64":"Mg=="}]}"#)
        .unwrap();
        assert_eq!(
            r.columns[0].value.as_ref().map(RawText::as_bytes),
            Some(&b"a"[..])
        );
        assert_eq!(r.columns[1].value, None);
        assert_eq!(
            r.computed[0].value.as_ref().map(RawText::as_bytes),
            Some(&b"2"[..])
        );
        // Base64 outside the standard alphabet is a document that breaks its own schema.
        let msg = internal(
            row(br#"{"outcome":"accepted","cols":[{"name":"s","null":false,"src":"input","value_b64":"!!"}]}"#)
                .unwrap_err(),
        );
        assert!(msg.contains("value_b64"), "{msg}");
    }

    #[test]
    fn a_discovery_document_carries_its_columns_and_the_joined_sql() {
        let d = discovery(
            br#"{"columns":[{"name":"a","declaration":"`a` UInt8","type":"UInt8"},
                           {"name_b64":"AP8=","declaration_b64":"/g=="}],
                 "columns_sql":"`a` UInt8"}"#,
        )
        .unwrap();
        assert_eq!(d.columns[0].name.as_bytes(), b"a");
        assert_eq!(d.columns[0].declaration.as_bytes(), b"`a` UInt8");
        assert_eq!(d.columns[1].name.as_bytes(), &[0x00, 0xff]);
        assert_eq!(d.columns[1].declaration.as_bytes(), &[0xfe]);
        assert_eq!(d.columns_sql.as_bytes(), b"`a` UInt8");
    }

    #[test]
    fn a_document_that_is_not_json_is_internal() {
        assert!(internal(row(b"{not json").unwrap_err()).contains("does not decode"));
        assert!(row(br#"{"outcome":"accepted","x":inf}"#).is_err());
    }

    /// A schema_description, on a server, whose top-level
    /// filter_declined_settings is `list`.
    fn declined(list: &str) -> Result<SchemaDescription> {
        let doc = format!(
            r#"{{"columns":[{{"name":"k","type":"UInt8","default_kind":"","default_expression":""}}],"filter_declined_settings":{list},"server":{{"timezone":"UTC","settings":{{"final":"1"}}}}}}"#
        );
        schema_description(doc.as_bytes())
    }

    #[test]
    fn filter_declined_settings_empty_and_absent_are_empty() {
        let d = declined("[]").unwrap();
        assert!(d.filter_declined_settings.is_empty());
        let absent =
            schema_description(br#"{"columns":[],"server":{"timezone":"UTC","settings":{}}}"#)
                .unwrap();
        assert!(absent.filter_declined_settings.is_empty());
    }

    #[test]
    fn filter_declined_settings_keep_the_document_order_bytes_and_unknown_values() {
        let d = declined(
            r#"[{"name":"aggregate_functions_null_for_empty","tier":"predicate","layer":"defaults","x_future":1},
                {"name":"final","tier":"result-content","layer":"server"},
                {"name":"additional_result_filter","tier":"result-content","layer":"schema"},
                {"name_b64":"eP95","tier":"predicate-unflipped","layer":"schema"},
                {"name":"x_future_setting","tier":"x_future_tier","layer":"x_future_layer","x_future_obj":{"a":[1]}}]"#,
        )
        .unwrap();
        let got = d.filter_declined_settings;
        // The document's own order, never re-sorted (here not name order).
        let names: Vec<&[u8]> = got.iter().map(|e| e.name.as_bytes()).collect();
        assert_eq!(
            names,
            vec![
                &b"aggregate_functions_null_for_empty"[..],
                &b"final"[..],
                &b"additional_result_filter"[..],
                &b"x\xffy"[..],
                &b"x_future_setting"[..],
            ]
        );
        let tiers: Vec<DeclinedTier> = got.iter().map(|e| e.tier.clone()).collect();
        assert_eq!(
            tiers,
            vec![
                DeclinedTier::Predicate,
                DeclinedTier::ResultContent,
                DeclinedTier::ResultContent,
                DeclinedTier::PredicateUnflipped,
                DeclinedTier::Unknown("x_future_tier".into()),
            ]
        );
        let layers: Vec<DeclinedLayer> = got.iter().map(|e| e.layer.clone()).collect();
        assert_eq!(
            layers,
            vec![
                DeclinedLayer::Defaults,
                DeclinedLayer::Server,
                DeclinedLayer::Schema,
                DeclinedLayer::Schema,
                DeclinedLayer::Unknown("x_future_layer".into()),
            ]
        );
        assert_eq!(got[4].tier.as_str(), "x_future_tier");
        assert_eq!(got[4].layer.as_str(), "x_future_layer");
        assert!(!got[4].tier.is_known() && got[0].tier.is_known());
        assert!(!got[4].layer.is_known() && got[0].layer.is_known());
    }

    #[test]
    fn filter_declined_settings_without_a_server_and_the_removed_server_list() {
        // A schema with no server lists its own layers all the same.
        let own = schema_description(
            br#"{"columns":[],"filter_declined_settings":[{"name":"final","tier":"result-content","layer":"schema"}]}"#,
        )
        .unwrap();
        assert!(own.server.is_none());
        assert_eq!(own.filter_declined_settings.len(), 1);
        assert_eq!(
            own.filter_declined_settings[0].name.as_bytes(),
            &b"final"[..]
        );
        assert_eq!(own.filter_declined_settings[0].layer, DeclinedLayer::Schema);
        // The removed server-level list is an unknown member now (rule r2):
        // the top-level list is the one decoded.
        let old = schema_description(
            br#"{"columns":[],"filter_declined_settings":[],"server":{"timezone":"UTC","settings":{},"filter_declined_settings":[{"name":"final","tier":"result-content"}]}}"#,
        )
        .unwrap();
        assert!(old.filter_declined_settings.is_empty());
        assert_eq!(old.server.unwrap().timezone, "UTC");
    }

    #[test]
    fn a_malformed_filter_declined_settings_is_internal() {
        for list in [
            r#"{"name":"final","tier":"predicate","layer":"server"}"#,
            r#"["final"]"#,
            r#"[{"name":"final","name_b64":"ZmluYWw=","tier":"predicate","layer":"server"}]"#,
            r#"[{"tier":"predicate","layer":"server"}]"#,
            r#"[{"name_b64":"*","tier":"predicate","layer":"server"}]"#,
            r#"[{"name":"final","tier":1,"layer":"server"}]"#,
            r#"[{"name":"final","tier":"predicate","layer":2}]"#,
        ] {
            let msg = internal(declined(list).unwrap_err());
            assert!(msg.contains("schema_description"), "{list}: {msg}");
        }
    }
}
