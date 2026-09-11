//! Exact JSON handling: the required bare-denormal repair, and a reader that
//! never routes a value through a float.
//!
//! Two things in the C ABI's result documents defeat an off-the-shelf parse:
//!
//! 1. ClickHouse's own `serializeTextJSON` writes IEEE denormals as the bare
//!    tokens `inf`, `-inf` and `nan` unless `output_format_json_quote_denormals`
//!    is set. That is faithful ClickHouse output and it is not valid JSON — one
//!    `QBit` column full of infinities took a whole document down and cost 18
//!    arbiter cases at once. [`quote_bare_denormals`] is the spec'd repair and
//!    it is required, not optional.
//! 2. ClickHouse integers go to 2^256 and a `String` column holds arbitrary
//!    bytes. Decoding either into a language number or string loses information
//!    that this crate would then report as a silent transformation that never
//!    happened. So the transformation detectors compare through [`Json`], which
//!    keeps numbers as their literal text and strings as bytes, and numbers are
//!    compared by [`num_eq`], which is exact.
//!
//! That is also why `serde_json` reads no result document at all: `crate::doc`
//! reads them over `&[u8]`, keeping every `stored` / `ref` leaf as the bytes
//! ClickHouse wrote, and the leaves that have to be *compared* are parsed here.
//! (`arbitrary_precision` was never enabled either, for the same reason.)

use std::borrow::Cow;

/// Make a wrapper result document strictly valid JSON by quoting ClickHouse's
/// bare denormal tokens. The stored text is preserved exactly — only quoting is
/// added, and never inside a JSON string — so downstream consumers see the same
/// bytes ClickHouse would have written.
pub(crate) fn quote_bare_denormals(b: &[u8]) -> Cow<'_, [u8]> {
    // Fast path: nothing that could be a bare denormal token.
    if !contains(b, b"inf") && !contains(b, b"nan") {
        return Cow::Borrowed(b);
    }
    let mut out: Vec<u8> = Vec::with_capacity(b.len() + 16);
    let mut in_str = false;
    let mut i = 0;
    while i < b.len() {
        let c = b[i];
        if in_str {
            out.push(c);
            if c == b'\\' && i + 1 < b.len() {
                i += 1;
                out.push(b[i]);
            } else if c == b'"' {
                in_str = false;
            }
            i += 1;
            continue;
        }
        if c == b'"' {
            in_str = true;
            out.push(c);
            i += 1;
            continue;
        }
        // A value can only start after one of these structural bytes.
        if let Some(&prev) = out.last() {
            if !matches!(prev, b':' | b',' | b'[' | b' ' | b'\t' | b'\n') {
                out.push(c);
                i += 1;
                continue;
            }
        }
        let rest = &b[i..];
        let tok = if rest.starts_with(b"-inf") {
            4
        } else if rest.starts_with(b"inf") || rest.starts_with(b"nan") {
            3
        } else {
            0
        };
        if tok > 0 && (rest.len() == tok || matches!(rest[tok], b',' | b'}' | b']')) {
            out.push(b'"');
            out.extend_from_slice(&rest[..tok]);
            out.push(b'"');
            i += tok;
            continue;
        }
        out.push(c);
        i += 1;
    }
    Cow::Owned(out)
}

fn contains(haystack: &[u8], needle: &[u8]) -> bool {
    haystack.windows(needle.len()).any(|w| w == needle)
}

/// One JSON value, decoded exactly: numbers keep their literal text and strings
/// keep their bytes.
#[derive(Debug, Clone, PartialEq)]
pub(crate) enum Json {
    Null,
    Bool(bool),
    /// The number's literal text, never parsed into a float.
    Number(String),
    /// The decoded string bytes. A ClickHouse `String` is arbitrary bytes, so
    /// this is deliberately not a `String`.
    Str(Vec<u8>),
    Array(Vec<Json>),
    /// Members in document order. Order and duplicate keys are preserved
    /// because a duplicate-key row is a real test case.
    Object(Vec<(Vec<u8>, Json)>),
}

impl Json {
    /// The JSON type name, so a change of spelling between two equal values
    /// stays visible.
    pub(crate) fn kind(&self) -> &'static str {
        match self {
            Json::Null => "null",
            Json::Bool(_) => "bool",
            Json::Number(_) => "number",
            Json::Str(_) => "string",
            Json::Array(_) => "array",
            Json::Object(_) => "object",
        }
    }

    /// The scalar's comparable text plus whether it was a JSON string, mirroring
    /// the reference implementation's `scalarText`.
    pub(crate) fn scalar_text(&self) -> Option<(Cow<'_, str>, bool)> {
        match self {
            Json::Number(n) => Some((Cow::Borrowed(n.as_str()), false)),
            Json::Str(s) => Some((String::from_utf8_lossy(s), true)),
            _ => None,
        }
    }
}

/// Parse exactly one JSON value. `None` when the text is not a single JSON
/// value — a TSV field or a bare `\N` never becomes a comparable value, which is
/// what the null-input branch of the classifier is for.
pub(crate) fn parse_exact(text: &str) -> Option<Json> {
    // trim_json_ws, not str::trim: the spec rule (docs/reference/bindings.md §detectors)
    // says whitespace is exactly JSON's four. str::trim also strips NBSP and
    // friends, so "\u{00A0}42" would have parsed as a number here while a
    // real server stores the bytes verbatim — inventing a transform.
    let bytes = trim_json_ws(text.as_bytes());
    if bytes.is_empty() {
        return None;
    }
    let mut p = Parser { b: bytes, i: 0 };
    let v = p.value()?;
    p.ws();
    if p.i != p.b.len() {
        return None; // trailing bytes: not a single JSON value
    }
    Some(v)
}

/// [`parse_exact`] over bytes, for a rendering that need not be valid UTF-8 — a
/// `String` column holds arbitrary bytes, and both sides of the supplied-vs-stored
/// comparison have to be read the same way or the asymmetry itself invents a
/// change.
///
/// Valid UTF-8 is routed through [`parse_exact`] unchanged, deliberately: that
/// keeps every answer this crate already gives byte for byte, including
/// `str::trim`'s slightly-wider-than-JSON idea of whitespace. The bytes path
/// trims exactly JSON's four whitespace bytes, which is the rule
/// `docs/reference/bindings.md` fixes.
pub(crate) fn parse_exact_bytes(b: &[u8]) -> Option<Json> {
    if let Ok(text) = std::str::from_utf8(b) {
        return parse_exact(text);
    }
    let b = trim_json_ws(b);
    if b.is_empty() {
        return None;
    }
    let mut p = Parser { b, i: 0 };
    let v = p.value()?;
    p.ws();
    if p.i != p.b.len() {
        return None;
    }
    Some(v)
}

/// JSON's four whitespace bytes, trimmed from both ends. Not `str::trim`: a BOM
/// or U+00A0 is not JSON whitespace, so a value padded with one is not a bare
/// JSON value (`docs/reference/bindings.md`, measured against the reference SDK).
fn trim_json_ws(mut b: &[u8]) -> &[u8] {
    while let [first, rest @ ..] = b {
        if matches!(first, b' ' | b'\t' | b'\n' | b'\r') {
            b = rest;
        } else {
            break;
        }
    }
    while let [rest @ .., last] = b {
        if matches!(last, b' ' | b'\t' | b'\n' | b'\r') {
            b = rest;
        } else {
            break;
        }
    }
    b
}

/// Decode the JSON string that starts at `at` (leading whitespace skipped):
/// the decoded **bytes**, and the offset just past the closing quote.
///
/// Shared with the result-document reader so that escape handling — including
/// Go's lone-surrogate-to-U+FFFD behavior — exists exactly once.
pub(crate) fn decode_string_at(b: &[u8], at: usize) -> Option<(Vec<u8>, usize)> {
    let mut p = Parser { b, i: at };
    p.ws();
    let s = p.string()?;
    Some((s, p.i))
}

/// The extent of the single JSON value that starts at `at`, as
/// `(start, end)` with leading whitespace skipped: `&b[start..end]` is that
/// value's bytes, verbatim.
///
/// This is how a `stored` value keeps ClickHouse's own spelling — its escapes,
/// its digits, its raw bytes — instead of this crate's idea of how to write it
/// again. It allocates nothing; `scan_agrees_with_the_parser` pins it against
/// [`Parser::value`], which is the reader that has to agree with it.
pub(crate) fn value_extent(b: &[u8], at: usize) -> Option<(usize, usize)> {
    let mut s = Scan { b, i: at };
    s.ws();
    let start = s.i;
    s.value()?;
    Some((start, s.i))
}

/// A non-allocating walk over one JSON value, used only for its end offset.
struct Scan<'a> {
    b: &'a [u8],
    i: usize,
}

impl Scan<'_> {
    fn ws(&mut self) {
        while self.i < self.b.len() && matches!(self.b[self.i], b' ' | b'\t' | b'\n' | b'\r') {
            self.i += 1;
        }
    }

    fn peek(&self) -> Option<u8> {
        self.b.get(self.i).copied()
    }

    fn lit(&mut self, word: &[u8]) -> Option<()> {
        if self.b[self.i..].starts_with(word) {
            self.i += word.len();
            Some(())
        } else {
            None
        }
    }

    fn value(&mut self) -> Option<()> {
        self.ws();
        match self.peek()? {
            b'n' => self.lit(b"null"),
            b't' => self.lit(b"true"),
            b'f' => self.lit(b"false"),
            b'"' => self.string(),
            b'[' => self.seq(b']'),
            b'{' => self.seq(b'}'),
            c if c == b'-' || c.is_ascii_digit() => self.number(),
            _ => None,
        }
    }

    /// One string, escapes included. Bytes that are not valid UTF-8 pass through
    /// untouched — that is the whole point.
    fn string(&mut self) -> Option<()> {
        if self.peek()? != b'"' {
            return None;
        }
        self.i += 1;
        loop {
            let c = self.peek()?;
            self.i += 1;
            match c {
                b'"' => return Some(()),
                b'\\' => {
                    self.peek()?;
                    self.i += 1;
                }
                _ => {}
            }
        }
    }

    /// An array or an object: both are "values and commas until the closer",
    /// and an object's keys are themselves values by this walk.
    fn seq(&mut self, close: u8) -> Option<()> {
        self.i += 1; // the opener
        self.ws();
        if self.peek()? == close {
            self.i += 1;
            return Some(());
        }
        loop {
            self.value()?;
            self.ws();
            match self.peek()? {
                b',' => self.i += 1,
                b':' => self.i += 1,
                c if c == close => {
                    self.i += 1;
                    return Some(());
                }
                _ => return None,
            }
        }
    }

    /// The same grammar [`Parser::number`] accepts, so the two never disagree
    /// about where a number ends.
    fn number(&mut self) -> Option<()> {
        if self.peek() == Some(b'-') {
            self.i += 1;
        }
        match self.peek()? {
            b'0' => self.i += 1,
            c if c.is_ascii_digit() => self.digits(),
            _ => return None,
        }
        if self.peek() == Some(b'.') {
            self.i += 1;
            if !self.peek().is_some_and(|c| c.is_ascii_digit()) {
                return None;
            }
            self.digits();
        }
        if matches!(self.peek(), Some(b'e') | Some(b'E')) {
            self.i += 1;
            if matches!(self.peek(), Some(b'+') | Some(b'-')) {
                self.i += 1;
            }
            if !self.peek().is_some_and(|c| c.is_ascii_digit()) {
                return None;
            }
            self.digits();
        }
        Some(())
    }

    fn digits(&mut self) {
        while self.peek().is_some_and(|c| c.is_ascii_digit()) {
            self.i += 1;
        }
    }
}

struct Parser<'a> {
    b: &'a [u8],
    i: usize,
}

impl<'a> Parser<'a> {
    fn ws(&mut self) {
        while self.i < self.b.len() && matches!(self.b[self.i], b' ' | b'\t' | b'\n' | b'\r') {
            self.i += 1;
        }
    }

    fn peek(&self) -> Option<u8> {
        self.b.get(self.i).copied()
    }

    fn eat(&mut self, c: u8) -> Option<()> {
        if self.peek() == Some(c) {
            self.i += 1;
            Some(())
        } else {
            None
        }
    }

    fn lit(&mut self, word: &[u8]) -> Option<()> {
        if self.b[self.i..].starts_with(word) {
            self.i += word.len();
            Some(())
        } else {
            None
        }
    }

    fn value(&mut self) -> Option<Json> {
        self.ws();
        match self.peek()? {
            b'n' => self.lit(b"null").map(|_| Json::Null),
            b't' => self.lit(b"true").map(|_| Json::Bool(true)),
            b'f' => self.lit(b"false").map(|_| Json::Bool(false)),
            b'"' => self.string().map(Json::Str),
            b'[' => self.array(),
            b'{' => self.object(),
            c if c == b'-' || c.is_ascii_digit() => self.number(),
            _ => None,
        }
    }

    fn array(&mut self) -> Option<Json> {
        self.eat(b'[')?;
        let mut items = Vec::new();
        self.ws();
        if self.eat(b']').is_some() {
            return Some(Json::Array(items));
        }
        loop {
            items.push(self.value()?);
            self.ws();
            if self.eat(b',').is_some() {
                continue;
            }
            self.eat(b']')?;
            return Some(Json::Array(items));
        }
    }

    fn object(&mut self) -> Option<Json> {
        self.eat(b'{')?;
        let mut members = Vec::new();
        self.ws();
        if self.eat(b'}').is_some() {
            return Some(Json::Object(members));
        }
        loop {
            self.ws();
            let k = self.string()?;
            self.ws();
            self.eat(b':')?;
            let v = self.value()?;
            members.push((k, v));
            self.ws();
            if self.eat(b',').is_some() {
                continue;
            }
            self.eat(b'}')?;
            return Some(Json::Object(members));
        }
    }

    fn string(&mut self) -> Option<Vec<u8>> {
        self.eat(b'"')?;
        let mut out = Vec::new();
        loop {
            let c = self.peek()?;
            self.i += 1;
            match c {
                b'"' => return Some(out),
                b'\\' => {
                    let e = self.peek()?;
                    self.i += 1;
                    match e {
                        b'"' => out.push(b'"'),
                        b'\\' => out.push(b'\\'),
                        b'/' => out.push(b'/'),
                        b'b' => out.push(0x08),
                        b'f' => out.push(0x0c),
                        b'n' => out.push(b'\n'),
                        b'r' => out.push(b'\r'),
                        b't' => out.push(b'\t'),
                        b'u' => {
                            let hi = self.hex4()?;
                            let ch = if (0xD800..0xDC00).contains(&hi) {
                                // A surrogate pair, if the low half follows.
                                let save = self.i;
                                let lo = (|| {
                                    self.eat(b'\\')?;
                                    self.eat(b'u')?;
                                    self.hex4()
                                })();
                                match lo {
                                    Some(lo) if (0xDC00..0xE000).contains(&lo) => char::from_u32(
                                        0x10000 + ((hi - 0xD800) << 10) + (lo - 0xDC00),
                                    ),
                                    _ => {
                                        self.i = save;
                                        None
                                    }
                                }
                            } else {
                                char::from_u32(hi)
                            };
                            // A lone surrogate is what Go's decoder replaces
                            // with U+FFFD; match that rather than failing.
                            let ch = ch.unwrap_or('\u{fffd}');
                            let mut buf = [0u8; 4];
                            out.extend_from_slice(ch.encode_utf8(&mut buf).as_bytes());
                        }
                        _ => return None,
                    }
                }
                _ => out.push(c),
            }
        }
    }

    fn hex4(&mut self) -> Option<u32> {
        if self.i + 4 > self.b.len() {
            return None;
        }
        let s = std::str::from_utf8(&self.b[self.i..self.i + 4]).ok()?;
        let v = u32::from_str_radix(s, 16).ok()?;
        self.i += 4;
        Some(v)
    }

    fn number(&mut self) -> Option<Json> {
        let start = self.i;
        let _ = self.eat(b'-');
        // int
        match self.peek()? {
            b'0' => self.i += 1,
            c if c.is_ascii_digit() => {
                while self.peek().is_some_and(|c| c.is_ascii_digit()) {
                    self.i += 1;
                }
                let _ = c;
            }
            _ => return None,
        }
        // frac
        if self.peek() == Some(b'.') {
            self.i += 1;
            if !self.peek().is_some_and(|c| c.is_ascii_digit()) {
                return None;
            }
            while self.peek().is_some_and(|c| c.is_ascii_digit()) {
                self.i += 1;
            }
        }
        // exp
        if matches!(self.peek(), Some(b'e') | Some(b'E')) {
            self.i += 1;
            if matches!(self.peek(), Some(b'+') | Some(b'-')) {
                self.i += 1;
            }
            if !self.peek().is_some_and(|c| c.is_ascii_digit()) {
                return None;
            }
            while self.peek().is_some_and(|c| c.is_ascii_digit()) {
                self.i += 1;
            }
        }
        let text = std::str::from_utf8(&self.b[start..self.i]).ok()?;
        Some(Json::Number(text.to_string()))
    }
}

/// The canonical spelling of a denormal token, or `None` for anything finite.
/// ClickHouse renders these as text, and after the repair they arrive as JSON
/// strings.
pub(crate) fn denormal(s: &str) -> Option<&'static str> {
    match s.trim().to_ascii_lowercase().as_str() {
        "nan" | "__nan__" | "-nan" => Some("nan"),
        "inf" | "infinity" | "__inf__" | "+inf" => Some("inf"),
        "-inf" | "-infinity" | "__-inf__" => Some("-inf"),
        _ => None,
    }
}

/// Exact numeric equality on two decimal renderings — the arbitrary-precision
/// comparison the spec requires.
///
/// `Decimal(76, 24)`'s extra scale digits, a quoted 64-bit integer and an
/// exponent form all compare equal to their plain spellings; nothing is routed
/// through a float, so `18446744073709551615` never becomes
/// `18446744073709552000`.
pub(crate) fn num_eq(a: &str, b: &str) -> bool {
    if a == b {
        return true;
    }
    if denormal(a).is_some() || denormal(b).is_some() {
        return denormal(a) == denormal(b);
    }
    match (Decimal::parse(a), Decimal::parse(b)) {
        (Some(x), Some(y)) => x == y,
        _ => false,
    }
}

/// Whether the text is a number this crate can compare exactly (a denormal
/// counts, mirroring the reference's `isNumericValue`).
pub(crate) fn is_numeric(s: &str) -> bool {
    !s.is_empty() && (denormal(s).is_some() || Decimal::parse(s).is_some())
}

/// A decimal number in exact normalised form: significant digits plus a power
/// of ten. No floats are involved at any point.
#[derive(Debug, PartialEq, Eq)]
struct Decimal {
    neg: bool,
    /// Significant digits, leading and trailing zeros removed. Empty means zero.
    digits: Vec<u8>,
    /// Value is `digits * 10^exp`.
    exp: i64,
}

impl Decimal {
    fn parse(s: &str) -> Option<Decimal> {
        let s = s.trim();
        let bytes = s.as_bytes();
        if bytes.is_empty() {
            return None;
        }
        let mut i = 0;
        let mut neg = false;
        match bytes[0] {
            b'-' => {
                neg = true;
                i = 1;
            }
            b'+' => i = 1,
            _ => {}
        }
        let mut digits: Vec<u8> = Vec::new();
        let mut seen_digit = false;
        while i < bytes.len() && bytes[i].is_ascii_digit() {
            digits.push(bytes[i] - b'0');
            seen_digit = true;
            i += 1;
        }
        let mut exp: i64 = 0;
        if i < bytes.len() && bytes[i] == b'.' {
            i += 1;
            while i < bytes.len() && bytes[i].is_ascii_digit() {
                digits.push(bytes[i] - b'0');
                seen_digit = true;
                exp -= 1;
                i += 1;
            }
        }
        if !seen_digit {
            return None;
        }
        if i < bytes.len() && (bytes[i] == b'e' || bytes[i] == b'E') {
            i += 1;
            let mut esign = 1i64;
            if i < bytes.len() && (bytes[i] == b'+' || bytes[i] == b'-') {
                if bytes[i] == b'-' {
                    esign = -1;
                }
                i += 1;
            }
            let start = i;
            let mut e: i64 = 0;
            while i < bytes.len() && bytes[i].is_ascii_digit() {
                e = e
                    .saturating_mul(10)
                    .saturating_add((bytes[i] - b'0') as i64);
                i += 1;
            }
            if i == start {
                return None;
            }
            exp = exp.saturating_add(esign * e);
        }
        if i != bytes.len() {
            return None; // trailing junk: not a number
        }
        // Normalise: drop leading zeros, then trailing zeros (raising exp).
        let first = digits.iter().position(|&d| d != 0);
        match first {
            None => Some(Decimal {
                neg: false,
                digits: Vec::new(),
                exp: 0,
            }),
            Some(f) => {
                digits.drain(..f);
                while digits.last() == Some(&0) {
                    digits.pop();
                    exp += 1;
                }
                Some(Decimal { neg, digits, exp })
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn denormals_are_quoted_outside_strings_only() {
        let doc = br#"{"stored":inf,"a":[nan,-inf],"s":"inf and nan","t":"x"}"#;
        let got = quote_bare_denormals(doc);
        assert_eq!(
            std::str::from_utf8(&got).unwrap(),
            r#"{"stored":"inf","a":["nan","-inf"],"s":"inf and nan","t":"x"}"#
        );
    }

    #[test]
    fn documents_without_denormals_are_untouched() {
        let doc = br#"{"stored":0,"x":"hi"}"#;
        assert!(matches!(quote_bare_denormals(doc), Cow::Borrowed(_)));
    }

    #[test]
    fn identifiers_containing_inf_are_not_rewritten() {
        let doc = br#"{"info":1,"err":"infinite loop"}"#;
        let got = quote_bare_denormals(doc);
        assert_eq!(
            std::str::from_utf8(&got).unwrap(),
            r#"{"info":1,"err":"infinite loop"}"#
        );
    }

    #[test]
    fn numbers_keep_their_exact_text() {
        let v = parse_exact("18446744073709551615").unwrap();
        assert_eq!(v, Json::Number("18446744073709551615".into()));
        let v = parse_exact(
            "-57896044618658097711785492504343953926634992332820282019728792003956564819968",
        )
        .unwrap();
        assert!(matches!(v, Json::Number(ref n) if n.len() == 78));
    }

    #[test]
    fn trailing_bytes_are_not_a_single_value() {
        assert!(parse_exact("1 2").is_none());
        assert!(parse_exact("\\N").is_none());
        assert!(parse_exact("").is_none());
        // A numeric PREFIX is not a JSON value either. `docs/reference/bindings.md` fixes
        // this as a spec rule rather than a language default, and names the
        // reference SDK's streaming parser as the wrong side of it: Go reads the
        // TSV field `4,2` as the number 4 and then reports a `reformat` against
        // the stored `"4,2"` — a transform on a field that is not a JSON value at
        // all. Measured 2026-08-17, 5 replies of the standing differential.
        assert!(parse_exact("4,2").is_none());
        assert!(parse_exact("1.2.3.4").is_none());
    }

    #[test]
    fn containers_and_escapes_decode() {
        let v = parse_exact(r#"{"a":[1,{"b":"xé"}],"c":null}"#).unwrap();
        match v {
            Json::Object(m) => {
                assert_eq!(m.len(), 2);
                assert_eq!(m[0].0, b"a");
            }
            other => panic!("expected object, got {other:?}"),
        }
        assert_eq!(
            parse_exact(r#""😀""#).unwrap(),
            Json::Str("😀".as_bytes().to_vec())
        );
    }

    #[test]
    fn exact_numeric_equality() {
        assert!(num_eq("2.50", "2.5000000"));
        assert!(num_eq("1", "1.000"));
        assert!(num_eq("0", "-0.0"));
        assert!(num_eq("1e3", "1000"));
        assert!(num_eq("18446744073709551615", "1.8446744073709551615e19"));
        assert!(!num_eq("18446744073709551615", "18446744073709552000"));
        assert!(!num_eq("0", "256"));
        assert!(num_eq("inf", "Infinity"));
        assert!(!num_eq("inf", "-inf"));
        assert!(!num_eq("abc", "abc2"));
    }

    #[test]
    fn non_utf8_bytes_parse_as_a_string_value() {
        // A ClickHouse String holding 0xC3 0x28 — a valid String, not valid
        // UTF-8. Both sides of a comparison must read it the same way.
        let v = parse_exact_bytes(&[b'"', 0xc3, b'(', b'"']).unwrap();
        assert_eq!(v, Json::Str(vec![0xc3, b'(']));
        // Two DIFFERENT invalid sequences must not collapse into one value.
        let w = parse_exact_bytes(&[b'"', 0xc4, b'(', b'"']).unwrap();
        assert_ne!(v, w);
        // The UTF-8 path is unchanged.
        assert_eq!(parse_exact_bytes(b"256"), parse_exact("256"));
        assert!(parse_exact_bytes(&[0xff, 0xfe]).is_none());
    }

    #[test]
    fn scan_agrees_with_the_parser_about_where_a_value_ends() {
        let samples: Vec<&[u8]> = vec![
            b"0",
            b"-1.5e-3",
            b"18446744073709551615",
            b"null",
            b"true",
            b"\"hi\"",
            b"\"a\\\"b\"",
            b"\"\\u0041\"",
            b"[]",
            b"[1,2,[3,{\"a\":\"b\"}]]",
            b"{}",
            b"{\"1\":1,\"1\":2}",
            b"  {\"a\": [1, 2], \"b\": null}  ",
            &[b'"', 0xc3, b'(', b'"'],
        ];
        for s in samples {
            let (start, end) = value_extent(s, 0).expect("extent");
            let mut p = Parser { b: s, i: 0 };
            p.ws();
            let pstart = p.i;
            p.value().expect("parse");
            assert_eq!(
                (start, end),
                (pstart, p.i),
                "disagreement on {:?}",
                String::from_utf8_lossy(s)
            );
        }
        // Trailing bytes are the caller's business: the extent is the value only.
        assert_eq!(value_extent(b"1 2", 0), Some((0, 1)));
        assert_eq!(value_extent(b"", 0), None);
    }

    #[test]
    fn a_string_decodes_to_bytes_at_an_offset() {
        let doc = br#"{"k":"v\n"}"#;
        let (bytes, next) = decode_string_at(doc, 5).unwrap();
        assert_eq!(bytes, b"v\n");
        assert_eq!(doc[next], b'}');
    }

    #[test]
    fn json_whitespace_is_exactly_the_four() {
        assert_eq!(trim_json_ws(b" \t\n\r1 \t\n\r"), b"1");
        // U+00A0 is not JSON whitespace, so this is not a bare JSON value.
        assert_eq!(trim_json_ws("\u{a0}1".as_bytes()), "\u{a0}1".as_bytes());
    }

    #[test]
    fn is_numeric_rejects_text() {
        assert!(is_numeric("-12.5"));
        assert!(is_numeric("nan"));
        assert!(!is_numeric("2023-11-14 22:13:20"));
        assert!(!is_numeric(""));
    }
}
