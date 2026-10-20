//! The options documents (`docs/reference/bindings-v1.md` §2, "The options
//! documents"; rule r1 of `spec/abi-v2/docs.md`).
//!
//! Three calls take one: `insert_options` on the four calls that model an
//! INSERT of a body, `filter_options` on `chs_filter_create` and
//! `schema_options` on `chs_schema_create`. Each member is its own field of
//! the call's options struct, and the binding builds the document from the
//! members the caller set. It validates no member and no value: the library
//! refuses a key it does not know, or a value that is not one of the member's,
//! with `CHS_INVALID_ARGUMENT`, naming it. A member the binding has no name for
//! goes through the struct's `*_extra` list as JSON text under its key and is
//! passed through verbatim; a key set twice keeps the last value (an extra
//! after the named member), and text that is not valid JSON is
//! [`Error::Usage`] before any call. A document with no member set is not sent
//! (length 0, which the library reads as `{}`).

use std::collections::BTreeMap;

use serde::Serialize;
use serde::de::IgnoredAny;
use serde_json::{Map, Value as Json};

use crate::abi2::vocab_gen::Posture;
use crate::error::{Error, Result};

/// `insert_options`' `verdict_basis`: what row a filter verdict answers for.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum VerdictBasis {
    /// The default: the verdict answers for the input row.
    Input,
    /// The verdict answers for the row the export would store. A build that
    /// does not implement it declines it.
    Export,
}

impl VerdictBasis {
    /// The wire spelling.
    pub fn as_str(self) -> &'static str {
        match self {
            VerdictBasis::Input => "input",
            VerdictBasis::Export => "export",
        }
    }
}

/// `insert_options`' `defer_defaults`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum DeferDefaults {
    /// The default: no DEFAULT is deferred.
    None,
    /// Defer to the server each DEFAULT the build would otherwise decline. A
    /// build that does not implement it declines it.
    Server,
}

impl DeferDefaults {
    /// The wire spelling.
    pub fn as_str(self) -> &'static str {
        match self {
            DeferDefaults::None => "none",
            DeferDefaults::Server => "server",
        }
    }
}

/// `filter_options`' `param_encoding`: how a query parameter's value is read.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum ParamEncoding {
    /// The default: ClickHouse's escaped-text reader.
    Escaped,
    /// The bytes are the value. A build that does not implement it declines
    /// it.
    Raw,
}

impl ParamEncoding {
    /// The wire spelling.
    pub fn as_str(self) -> &'static str {
        match self {
            ParamEncoding::Escaped => "escaped",
            ParamEncoding::Raw => "raw",
        }
    }
}

/// One entry of `insert_options`' `column_defaults`: the value the call uses
/// in a cell the server would fill from the column's DEFAULT. Both fields are
/// byte strings: one that is not valid UTF-8 is sent in its `_b64` form.
#[derive(Debug, Clone, Default, PartialEq, Eq, Hash)]
pub struct ColumnDefault {
    /// The column's name, as bytes.
    pub name: Vec<u8>,
    /// The value, as bytes.
    pub value: Vec<u8>,
}

/// One byte string as the object member pair it travels as: `key` for valid
/// UTF-8, `key_b64` (standard base64, padded) for any other bytes.
fn put_bytes(o: &mut Map<String, Json>, key: &str, bytes: &[u8]) {
    use base64::Engine as _;
    match std::str::from_utf8(bytes) {
        Ok(s) => o.insert(key.to_string(), Json::String(s.to_string())),
        Err(_) => o.insert(
            format!("{key}_b64"),
            Json::String(base64::engine::general_purpose::STANDARD.encode(bytes)),
        ),
    };
}

/// A name as the name object the ABI takes: `{"name"}` or `{"name_b64"}`.
pub(crate) fn name_object(name: &[u8]) -> Json {
    let mut o = Map::new();
    put_bytes(&mut o, "name", name);
    Json::Object(o)
}

/// The members of an options document under construction: key to JSON text.
#[derive(Default)]
struct Doc {
    members: BTreeMap<String, Vec<u8>>,
}

impl Doc {
    fn set(&mut self, key: &str, value: &impl Serialize) -> Result<()> {
        let text = serde_json::to_vec(value)
            .map_err(|e| Error::internal(format!("the option {key:?} does not encode: {e}")))?;
        self.members.insert(key.to_string(), text);
        Ok(())
    }

    /// A member the binding has no name for: its JSON text, passed through as
    /// given. A later one replaces an earlier one under the same key.
    fn extras(&mut self, what: &str, extra: &[(String, String)]) -> Result<()> {
        for (key, text) in extra {
            if serde_json::from_str::<IgnoredAny>(text).is_err() {
                return Err(Error::usage(format!(
                    "{what}: the value for key {key:?} is not valid JSON"
                )));
            }
            self.members.insert(key.clone(), text.clone().into_bytes());
        }
        Ok(())
    }

    /// The document, or empty when no member was set.
    fn finish(self) -> Result<Vec<u8>> {
        if self.members.is_empty() {
            return Ok(Vec::new());
        }
        let mut out = vec![b'{'];
        for (i, (key, text)) in self.members.iter().enumerate() {
            if i > 0 {
                out.push(b',');
            }
            let k = serde_json::to_vec(key)
                .map_err(|e| Error::internal(format!("an option key does not encode: {e}")))?;
            out.extend_from_slice(&k);
            out.push(b':');
            out.extend_from_slice(text);
        }
        out.push(b'}');
        Ok(out)
    }
}

/// The members of `insert_options` a call's options struct carries.
pub(crate) struct InsertMembers<'a> {
    pub(crate) insertable_columns: Option<&'a [Vec<u8>]>,
    pub(crate) column_defaults: Option<&'a [ColumnDefault]>,
    pub(crate) verdict_basis: Option<VerdictBasis>,
    pub(crate) defer_defaults: Option<DeferDefaults>,
    pub(crate) extra: &'a [(String, String)],
}

impl InsertMembers<'_> {
    /// The `insert_options` document (empty for none).
    pub(crate) fn document(&self) -> Result<Vec<u8>> {
        let mut d = Doc::default();
        if let Some(names) = self.insertable_columns {
            let list: Vec<Json> = names.iter().map(|n| name_object(n)).collect();
            d.set("insertable_columns", &list)?;
        }
        if let Some(defaults) = self.column_defaults {
            let list: Vec<Json> = defaults
                .iter()
                .map(|c| {
                    let mut o = Map::new();
                    put_bytes(&mut o, "name", &c.name);
                    put_bytes(&mut o, "value", &c.value);
                    Json::Object(o)
                })
                .collect();
            d.set("column_defaults", &list)?;
        }
        if let Some(b) = self.verdict_basis {
            d.set("verdict_basis", &b.as_str())?;
        }
        if let Some(b) = self.defer_defaults {
            d.set("defer_defaults", &b.as_str())?;
        }
        d.extras("insert options", self.extra)?;
        d.finish()
    }
}

/// The `filter_options` document (empty for none).
pub(crate) fn filter_options(
    param_encoding: Option<ParamEncoding>,
    extra: &[(String, String)],
) -> Result<Vec<u8>> {
    let mut d = Doc::default();
    if let Some(e) = param_encoding {
        d.set("param_encoding", &e.as_str())?;
    }
    d.extras("filter options", extra)?;
    d.finish()
}

/// The `schema_options` document (empty for none).
pub(crate) fn schema_options(
    posture: Option<&Posture>,
    extra: &[(String, String)],
) -> Result<Vec<u8>> {
    let mut d = Doc::default();
    if let Some(p) = posture {
        d.set("posture", &p.as_str())?;
    }
    d.extras("schema options", extra)?;
    d.finish()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn members<'a>() -> InsertMembers<'a> {
        InsertMembers {
            insertable_columns: None,
            column_defaults: None,
            verdict_basis: None,
            defer_defaults: None,
            extra: &[],
        }
    }

    fn text(v: Vec<u8>) -> String {
        String::from_utf8(v).unwrap()
    }

    #[test]
    fn no_member_set_is_no_document() {
        assert!(members().document().unwrap().is_empty());
        assert!(filter_options(None, &[]).unwrap().is_empty());
        assert!(schema_options(None, &[]).unwrap().is_empty());
    }

    #[test]
    fn every_named_member_is_written_under_its_key() {
        let names = vec![b"a".to_vec(), vec![0x00, 0xff]];
        let defaults = vec![
            ColumnDefault {
                name: b"c".to_vec(),
                value: b"1".to_vec(),
            },
            ColumnDefault {
                name: vec![0xff],
                value: vec![0xfe],
            },
        ];
        let doc = InsertMembers {
            insertable_columns: Some(&names),
            column_defaults: Some(&defaults),
            verdict_basis: Some(VerdictBasis::Export),
            defer_defaults: Some(DeferDefaults::Server),
            extra: &[],
        }
        .document()
        .unwrap();
        assert_eq!(
            text(doc),
            concat!(
                r#"{"column_defaults":[{"name":"c","value":"1"},{"name_b64":"/w==","value_b64":"/g=="}],"#,
                r#""defer_defaults":"server","#,
                r#""insertable_columns":[{"name":"a"},{"name_b64":"AP8="}],"#,
                r#""verdict_basis":"export"}"#
            )
        );
    }

    #[test]
    fn column_defaults_wire_form_utf8_b64_empty_and_order() {
        let doc = |defaults: Vec<ColumnDefault>| {
            text(
                InsertMembers {
                    column_defaults: Some(&defaults),
                    ..members()
                }
                .document()
                .unwrap(),
            )
        };
        let cd = |n: &[u8], v: &[u8]| ColumnDefault {
            name: n.to_vec(),
            value: v.to_vec(),
        };
        assert_eq!(
            doc(vec![cd(b"a", "h\u{e9}llo".as_bytes())]),
            "{\"column_defaults\":[{\"name\":\"a\",\"value\":\"h\u{e9}llo\"}]}"
        );
        assert_eq!(
            doc(vec![cd(b"a", &[0xff, 0x00])]),
            r#"{"column_defaults":[{"name":"a","value_b64":"/wA="}]}"#
        );
        assert_eq!(
            doc(vec![cd(&[0xfe], b"v")]),
            r#"{"column_defaults":[{"name_b64":"/g==","value":"v"}]}"#
        );
        // An empty value is a supplied value: sent as "", never omitted.
        assert_eq!(
            doc(vec![cd(b"a", b"")]),
            r#"{"column_defaults":[{"name":"a","value":""}]}"#
        );
        // The list keeps its order.
        assert_eq!(
            doc(vec![cd(b"z", b"1"), cd(b"a", b"2")]),
            r#"{"column_defaults":[{"name":"z","value":"1"},{"name":"a","value":"2"}]}"#
        );
    }

    #[test]
    fn an_empty_list_is_sent_as_an_empty_list_and_none_is_left_out() {
        let none: Vec<Vec<u8>> = Vec::new();
        let doc = InsertMembers {
            insertable_columns: Some(&none),
            ..members()
        }
        .document()
        .unwrap();
        assert_eq!(text(doc), r#"{"insertable_columns":[]}"#);
    }

    #[test]
    fn an_extra_is_passed_through_verbatim_and_the_last_value_of_a_key_wins() {
        let extra = vec![
            (
                "x_future".to_string(),
                r#"{"a": [1, 2.50, 1e3]}"#.to_string(),
            ),
            ("verdict_basis".to_string(), r#""export""#.to_string()),
            ("x_future".to_string(), "7".to_string()),
        ];
        let doc = InsertMembers {
            verdict_basis: Some(VerdictBasis::Input),
            extra: &extra,
            ..members()
        }
        .document()
        .unwrap();
        // The extra after the named member replaces it; the repeated extra
        // keeps its last value; no value is re-encoded.
        assert_eq!(text(doc), r#"{"verdict_basis":"export","x_future":7}"#);
        let doc = filter_options(
            Some(ParamEncoding::Raw),
            &[("y".to_string(), r#"{"a": [1, 2.50, 1e3]}"#.to_string())],
        )
        .unwrap();
        assert_eq!(
            text(doc),
            r#"{"param_encoding":"raw","y":{"a": [1, 2.50, 1e3]}}"#
        );
    }

    #[test]
    fn text_that_is_not_json_is_misuse_before_any_call() {
        for bad in ["", "{", "1 2", "nope", "'x'"] {
            let extra = vec![("k".to_string(), bad.to_string())];
            let err = InsertMembers {
                extra: &extra,
                ..members()
            }
            .document()
            .unwrap_err();
            assert!(matches!(err, Error::Usage(_)), "{bad:?}: {err:?}");
            assert!(matches!(
                filter_options(None, &extra).unwrap_err(),
                Error::Usage(_)
            ));
            assert!(matches!(
                schema_options(None, &extra).unwrap_err(),
                Error::Usage(_)
            ));
        }
    }

    #[test]
    fn posture_and_param_encoding_use_the_wire_spelling_and_keep_an_unknown() {
        assert_eq!(
            text(schema_options(Some(&Posture::Load), &[]).unwrap()),
            r#"{"posture":"load"}"#
        );
        // An unlisted posture is passed through as given: the library refuses it.
        assert_eq!(
            text(schema_options(Some(&Posture::Unknown("later".into())), &[]).unwrap()),
            r#"{"posture":"later"}"#
        );
        assert_eq!(
            text(filter_options(Some(ParamEncoding::Escaped), &[]).unwrap()),
            r#"{"param_encoding":"escaped"}"#
        );
    }
}
