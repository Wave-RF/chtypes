//! The result-document reader: byte-exact, and the only reader of `chs_row` /
//! `chs_rows` output.
//!
//! Why this is hand-written rather than `serde_json` with `RawValue` leaves —
//! two facts, both from `docs/reference/c-abi.md`:
//!
//! 1. **A result document need not be valid UTF-8.** ClickHouse's
//!    `serializeTextJSON` writes a `String` column's bytes through unescaped, so
//!    a column holding an `AggregateFunction` state (or any non-UTF-8 blob) puts
//!    raw bytes inside a JSON string. `serde_json` cannot parse that at all —
//!    which is how this crate came to retry through `String::from_utf8_lossy`
//!    and destroy the very bytes the spec says are authoritative. A reader over
//!    `&[u8]` has no such failure mode.
//! 2. **`null` and "absent" are different answers.** `Option<Box<RawValue>>`
//!    maps a present-and-null `stored` onto `None`, so a genuine stored null
//!    became indistinguishable from a column the document carried no value for.
//!    Here a present value is `Some`, whatever it is: a stored null arrives as
//!    `Some("null")`, exactly as the reference SDK's `json.RawMessage` does.
//!
//! Everything else the reader does is what serde did: every field is optional
//! with a default, unknown fields are ignored (`docs/reference/bindings.md`: "unknown
//! request fields are ignored"), and no number is ever routed through a float.

use crate::error::{Error, Result};
use crate::json::{decode_string_at, value_extent};
use crate::raw::RawText;
use crate::result::{
    BatchDoc, ColDoc, CompDoc, FilterDoc, FilterRowError, RowDoc, Span, StorageTransformDoc,
};

/// Read one `chs_row` document.
pub(crate) fn row_doc(b: &[u8]) -> Result<RowDoc> {
    let mut r = Rdr { b, i: 0 };
    let d = r.row()?;
    r.end()?;
    Ok(d)
}

/// Read one `chs_rows` document.
pub(crate) fn batch_doc(b: &[u8]) -> Result<BatchDoc> {
    let mut r = Rdr { b, i: 0 };
    let d = r.batch()?;
    r.end()?;
    Ok(d)
}

/// Read one `chs_filter_rows` document.
pub(crate) fn filter_doc(b: &[u8]) -> Result<FilterDoc> {
    let mut r = Rdr { b, i: 0 };
    let d = r.filter()?;
    r.end()?;
    Ok(d)
}

struct Rdr<'a> {
    b: &'a [u8],
    i: usize,
}

impl<'a> Rdr<'a> {
    fn bad(&self, what: &str) -> Error {
        Error::BadDocument {
            message: what.to_string(),
            offset: self.i,
        }
    }

    fn ws(&mut self) {
        while self.i < self.b.len() && matches!(self.b[self.i], b' ' | b'\t' | b'\n' | b'\r') {
            self.i += 1;
        }
    }

    fn peek(&mut self) -> Option<u8> {
        self.ws();
        self.b.get(self.i).copied()
    }

    fn eat(&mut self, c: u8) -> bool {
        if self.peek() == Some(c) {
            self.i += 1;
            true
        } else {
            false
        }
    }

    fn end(&mut self) -> Result<()> {
        self.ws();
        if self.i == self.b.len() {
            Ok(())
        } else {
            Err(self.bad("trailing bytes after the document"))
        }
    }

    // ---------------------------------------------------------------- values

    /// The next value's bytes, verbatim.
    fn raw(&mut self) -> Result<RawText> {
        self.ws();
        let (start, end) = value_extent(self.b, self.i).ok_or_else(|| self.bad("value"))?;
        self.i = end;
        Ok(RawText::from(&self.b[start..end]))
    }

    fn skip(&mut self) -> Result<()> {
        self.raw().map(|_| ())
    }

    /// A JSON string's decoded bytes. A `String` column's input text can hold
    /// arbitrary bytes, so this does not go through `str`.
    fn string_bytes(&mut self) -> Result<RawText> {
        self.ws();
        if self.peek() == Some(b'n') {
            // A defensive null: the wrapper never writes one for a text field,
            // but a missing field and a null one must mean the same thing.
            self.skip()?;
            return Ok(RawText::default());
        }
        let (bytes, next) =
            decode_string_at(self.b, self.i).ok_or_else(|| self.bad("expected a string"))?;
        self.i = next;
        Ok(RawText::from(bytes))
    }

    /// A JSON string as Rust text. Invalid UTF-8 becomes U+FFFD, which is what
    /// the reference SDK's own decoder does with these fields (`name`, `src`,
    /// `err`, ...) — see [`crate::Transform`] for where that is visible and why
    /// it is the reported *text* rather than a value that must round-trip.
    fn text(&mut self) -> Result<String> {
        Ok(self.string_bytes()?.to_lossy().into_owned())
    }

    fn int(&mut self) -> Result<i64> {
        let v = self.raw()?;
        let s = v.as_str().ok_or_else(|| self.bad("number"))?;
        if s == "null" {
            return Ok(0);
        }
        s.parse::<i64>()
            .map_err(|_| self.bad(&format!("not an integer: {s}")))
    }

    fn i32_field(&mut self) -> Result<i32> {
        Ok(self.int()? as i32)
    }

    fn usize_field(&mut self) -> Result<usize> {
        Ok(self.int()?.max(0) as usize)
    }

    fn boolean(&mut self) -> Result<bool> {
        let v = self.raw()?;
        match v.as_bytes() {
            b"true" => Ok(true),
            b"false" | b"null" => Ok(false),
            _ => Err(self.bad("expected true or false")),
        }
    }

    // ------------------------------------------------------------ containers

    /// Walk an object's members, handing each key to `f`. `f` reads that
    /// member's value; anything it does not recognize is skipped.
    fn object<F>(&mut self, what: &'static str, mut f: F) -> Result<()>
    where
        F: FnMut(&mut Rdr<'a>, &[u8]) -> Result<bool>,
    {
        if !self.eat(b'{') {
            return Err(self.bad(what));
        }
        if self.eat(b'}') {
            return Ok(());
        }
        loop {
            self.ws();
            let (key, next) =
                decode_string_at(self.b, self.i).ok_or_else(|| self.bad("object key"))?;
            self.i = next;
            if !self.eat(b':') {
                return Err(self.bad("expected ':'"));
            }
            if !f(self, &key)? {
                self.skip()?;
            }
            if self.eat(b',') {
                continue;
            }
            if self.eat(b'}') {
                return Ok(());
            }
            return Err(self.bad("expected ',' or '}'"));
        }
    }

    /// Walk an array's items, handing the reader to `f` for each. A JSON `null`
    /// where an array was expected reads as no items, which is how every list
    /// field in these documents may be spelled.
    fn array<F>(&mut self, what: &'static str, mut f: F) -> Result<()>
    where
        F: FnMut(&mut Rdr<'a>) -> Result<()>,
    {
        if self.peek() == Some(b'n') {
            return self.skip();
        }
        if !self.eat(b'[') {
            return Err(self.bad(what));
        }
        if self.eat(b']') {
            return Ok(());
        }
        loop {
            f(self)?;
            if self.eat(b',') {
                continue;
            }
            if self.eat(b']') {
                return Ok(());
            }
            return Err(self.bad("expected ',' or ']'"));
        }
    }

    fn text_list(&mut self) -> Result<Vec<String>> {
        let mut out = Vec::new();
        self.array("expected an array of strings", |r| {
            out.push(r.text()?);
            Ok(())
        })?;
        Ok(out)
    }

    // ------------------------------------------------------------- documents

    fn row(&mut self) -> Result<RowDoc> {
        let mut d = RowDoc::default();
        self.object("expected a row document", |r, key| {
            match key {
                b"outcome" => d.outcome = r.text()?,
                b"code" => d.code = r.i32_field()?,
                b"err" => d.err = r.text()?,
                b"unknown_fields" => d.unknown_fields = r.text_list()?,
                b"unsupported_settings" => d.unsupported_settings = r.text_list()?,
                b"cols" => {
                    r.array("expected cols[]", |r| {
                        d.cols.push(r.col()?);
                        Ok(())
                    })?;
                }
                b"computed" => {
                    r.array("expected computed[]", |r| {
                        d.computed.push(r.computed()?);
                        Ok(())
                    })?;
                }
                _ => return Ok(false),
            }
            Ok(true)
        })?;
        Ok(d)
    }

    fn col(&mut self) -> Result<ColDoc> {
        let mut c = ColDoc::default();
        self.object("expected a column document", |r, key| {
            match key {
                b"name" => c.name = r.text()?,
                b"type" => c.declared_type = r.text()?,
                b"base" => c.base = r.text()?,
                b"src" => c.src = r.text()?,
                b"input" => c.input = r.string_bytes()?,
                b"ref_type" => c.ref_type = r.text()?,
                // `stored` and `ref` keep ClickHouse's own bytes. A `null` here
                // is a VALUE, so it is `Some("null")` and not `None`.
                b"stored" => c.stored = Some(r.raw()?),
                b"ref" => c.reference = Some(r.raw()?),
                b"nullable" => c.nullable = r.boolean()?,
                b"poison" => c.poison = r.boolean()?,
                b"null_input" => c.null_input = r.boolean()?,
                b"dup_dropped" => c.dup_dropped = r.boolean()?,
                b"ref_unclassified" => c.ref_unclassified = r.boolean()?,
                _ => return Ok(false),
            }
            Ok(true)
        })?;
        Ok(c)
    }

    fn computed(&mut self) -> Result<CompDoc> {
        let mut m = CompDoc::default();
        self.object("expected a computed document", |r, key| {
            match key {
                b"name" => m.name = r.text()?,
                b"kind" => m.kind = r.text()?,
                b"stored" => m.stored = Some(r.raw()?),
                _ => return Ok(false),
            }
            Ok(true)
        })?;
        Ok(m)
    }

    fn batch(&mut self) -> Result<BatchDoc> {
        let mut d = BatchDoc::default();
        self.object("expected a batch document", |r, key| {
            match key {
                b"outcome" => d.outcome = r.text()?,
                b"code" => d.code = r.i32_field()?,
                b"err" => d.err = r.text()?,
                b"rows_read" => d.rows_read = r.usize_field()?,
                b"rows_skipped" => d.rows_skipped = r.usize_field()?,
                b"rows" => {
                    r.array("expected rows[]", |r| {
                        d.rows.push(r.row()?);
                        Ok(())
                    })?;
                }
                b"engine_rows" => {
                    // Present-and-null means "no engine semantics ran", which is
                    // NOT the same as an engine that stored zero rows.
                    if r.peek() == Some(b'n') {
                        r.skip()?;
                    } else {
                        let mut rows = Vec::new();
                        r.array("expected engine_rows[]", |r| {
                            rows.push(r.raw()?);
                            Ok(())
                        })?;
                        d.engine_rows = Some(rows);
                    }
                }
                b"storage_transforms" => {
                    r.array("expected storage_transforms[]", |r| {
                        d.storage_transforms.push(r.storage_transform()?);
                        Ok(())
                    })?;
                }
                b"row_spans" => {
                    // Present exactly when export bytes were emitted; a null
                    // reads as absent, like every other list here.
                    if r.peek() == Some(b'n') {
                        r.skip()?;
                    } else {
                        let mut spans = Vec::new();
                        r.array("expected row_spans[]", |r| {
                            spans.push(r.span()?);
                            Ok(())
                        })?;
                        d.row_spans = Some(spans);
                    }
                }
                b"export_declined" => d.export_declined = r.text()?,
                _ => return Ok(false),
            }
            Ok(true)
        })?;
        Ok(d)
    }

    fn span(&mut self) -> Result<Span> {
        let mut s = Span::default();
        self.object("expected a span", |r, key| {
            match key {
                b"off" => s.off = r.usize_field()?,
                b"len" => s.len = r.usize_field()?,
                _ => return Ok(false),
            }
            Ok(true)
        })?;
        Ok(s)
    }

    fn filter(&mut self) -> Result<FilterDoc> {
        let mut d = FilterDoc::default();
        self.object("expected a filter document", |r, key| {
            match key {
                b"outcome" => d.outcome = r.text()?,
                b"code" => d.code = r.i32_field()?,
                b"err" => d.err = r.text()?,
                b"rows_read" => d.rows_read = r.usize_field()?,
                b"unsupported_settings" => d.unsupported_settings = r.text_list()?,
                b"verdicts" => d.verdicts = r.text()?,
                b"errors" => {
                    r.array("expected errors[]", |r| {
                        d.errors.push(r.filter_error()?);
                        Ok(())
                    })?;
                }
                _ => return Ok(false),
            }
            Ok(true)
        })?;
        Ok(d)
    }

    fn filter_error(&mut self) -> Result<FilterRowError> {
        let mut e = FilterRowError::default();
        self.object("expected a filter error", |r, key| {
            match key {
                b"row" => e.row = r.usize_field()?,
                b"code" => e.code = r.i32_field()?,
                b"err" => e.err = r.text()?,
                _ => return Ok(false),
            }
            Ok(true)
        })?;
        Ok(e)
    }

    fn storage_transform(&mut self) -> Result<StorageTransformDoc> {
        let mut st = StorageTransformDoc::default();
        self.object("expected a storage transform", |r, key| {
            match key {
                b"row" => st.row = r.usize_field()?,
                b"column" => st.column = r.text()?,
                b"reason" => st.reason = r.text()?,
                b"stored" => st.stored = Some(r.raw()?),
                _ => return Ok(false),
            }
            Ok(true)
        })?;
        Ok(st)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_string_columns_bytes_survive_the_document_parse() {
        // Exactly what the 25.8 artifact emits for `x String` fed the two bytes
        // 0xC3 0x28 as RowBinary: raw bytes inside a JSON string.
        let mut doc: Vec<u8> = br#"{"outcome":"accepted","code":0,"err":"","rows_read":1,"rows_skipped":0,"rows":[{"outcome":"accepted","cols":[{"name":"x","type":"String","base":"String","src":"input","input":"","stored":""#.to_vec();
        doc.extend_from_slice(&[0xc3, b'(']);
        doc.extend_from_slice(br#"","ref":null,"ref_type":"","nullable":false,"poison":false,"null_input":false,"dup_dropped":false}]}]}"#);

        let d = batch_doc(&doc).expect("parse");
        let col = &d.rows[0].cols[0];
        assert_eq!(
            col.stored_raw().as_bytes(),
            [b'"', 0xc3, b'(', b'"'],
            "the stored bytes must be ClickHouse's own, not a repair"
        );
        assert!(!col.stored_raw().is_utf8());
        // Everything else in the document is still exact.
        assert_eq!(d.rows_read, 1);
        assert_eq!(d.rows[0].cols[0].base, "String");
    }

    #[test]
    fn a_present_null_is_not_an_absent_value() {
        let d = row_doc(
            br#"{"outcome":"accepted","cols":[
                 {"name":"a","stored":null,"nullable":true},
                 {"name":"b","nullable":true}]}"#,
        )
        .unwrap();
        assert!(d.cols[0].stored_is_json_null(), "present null");
        assert_eq!(d.cols[0].stored_raw(), "null");
        assert!(!d.cols[1].stored_is_json_null(), "absent key");
        assert_eq!(d.cols[1].stored_raw(), "");
    }

    #[test]
    fn every_field_is_optional_and_unknown_fields_are_ignored() {
        let d = row_doc(
            br#"{"outcome":"rejected","code":27,"err":"nope","cols":[],"future":{"a":[1,2]}}"#,
        )
        .unwrap();
        assert_eq!(d.outcome, "rejected");
        assert_eq!(d.code, 27);
        assert!(d.cols.is_empty());
        assert!(d.unknown_fields.is_empty());
        let d = batch_doc(br#"{"rows":[{}],"engine_rows":null,"nope":"x"}"#).unwrap();
        assert_eq!(d.rows.len(), 1);
        assert!(d.engine_rows.is_none());
    }

    #[test]
    fn an_engine_that_stored_nothing_is_not_the_absence_of_an_engine() {
        let d = batch_doc(
            br#"{"engine_rows":[],"storage_transforms":[{"row":0,"reason":"ttl_expired"}]}"#,
        )
        .unwrap();
        assert_eq!(d.engine_rows.as_deref().map(<[RawText]>::len), Some(0));
        assert_eq!(d.storage_transforms[0].reason, "ttl_expired");
        assert!(d.storage_transforms[0].stored.is_none());
    }

    #[test]
    fn ints_never_route_through_a_float() {
        // 2^53 + 1: a double would answer 9007199254740992.
        let d = batch_doc(br#"{"rows_read":9007199254740993}"#).unwrap();
        assert_eq!(d.rows_read, 9007199254740993);
    }

    #[test]
    fn a_broken_document_is_an_error_and_not_a_repair() {
        let e = batch_doc(br#"{"rows":[{"outcome":"accepted"}"#).unwrap_err();
        assert!(matches!(e, Error::BadDocument { .. }), "{e:?}");
        assert!(e.to_string().contains("bad result document"), "{e}");
        assert!(batch_doc(b"").is_err());
        assert!(batch_doc(br#"{"code":"twenty-seven"}"#).is_err());
    }
}
