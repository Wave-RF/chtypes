//! [`RawText`] — ClickHouse's own rendering of one value, as the bytes a server
//! would emit.
//!
//! A ClickHouse `String` column holds **arbitrary bytes**: an
//! `AggregateFunction` state, a protobuf blob, a mangled UTF-8 sequence a
//! producer sent by accident. The C ABI contract is explicit that the result
//! document's `stored` and `ref` must be handled as raw bytes, "not decoded into
//! the binding's native string and number types before comparison", because
//! "decoding invalid UTF-8 into a language string replaces it with U+FFFD, which
//! then reads downstream as a silent transformation that never happened".
//!
//! Go gets that for free — a Go string is a byte slice — and the reference SDK
//! keeps `json.RawMessage`. A Rust `String` cannot hold those bytes at all, so
//! the byte-carrying type has to be explicit. That is this type: the bytes are
//! authoritative ([`RawText::as_bytes`]) and the UTF-8 view is *fallible*
//! ([`RawText::as_str`]), so a caller can never silently receive a repaired
//! approximation of a value it asked for exactly.

use std::borrow::Cow;
use std::fmt;

/// ClickHouse's own rendering of a value, as bytes.
///
/// This is the JSON text ClickHouse itself would write for the value — `0`,
/// `"2023-11-14 22:13:20"`, `[1,0,3]`, `null` — passed through verbatim. It is
/// **not** a Rust string, because a `String` column's rendering need not be
/// valid UTF-8, and re-encoding it would invent a change that never happened.
///
/// ```no_run
/// # fn f(v: &chtypes::Value) {
/// // Authoritative: exactly the bytes a server would emit for this value.
/// let bytes: &[u8] = v.text.as_bytes();
/// // Fallible: `None` when the stored value is not valid UTF-8.
/// match v.text.as_str() {
///     Some(s) => println!("{s}"),
///     None => println!("{} raw bytes, not UTF-8", bytes.len()),
/// }
/// # }
/// ```
///
/// [`Display`](std::fmt::Display) is deliberately the *only* lossy view, and it
/// exists for diagnostics: it prints U+FFFD for bytes that are not valid UTF-8,
/// the way `String::from_utf8_lossy` does. Anything that must round-trip — a
/// value spliced back into an INSERT, a byte-for-byte comparison against a real
/// server's output — must use [`as_bytes`](RawText::as_bytes).
#[derive(Clone, Default, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct RawText(Vec<u8>);

impl RawText {
    /// The bytes, verbatim. **This is the authoritative form.**
    pub fn as_bytes(&self) -> &[u8] {
        &self.0
    }

    /// The bytes as UTF-8, or `None` when they are not valid UTF-8.
    ///
    /// There is deliberately no lossy accessor that looks like an exact one:
    /// `None` is the signal that this value cannot be represented as Rust text,
    /// which is precisely the fact a caller must not have hidden from it.
    pub fn as_str(&self) -> Option<&str> {
        std::str::from_utf8(&self.0).ok()
    }

    /// Whether the bytes are valid UTF-8, i.e. whether [`as_str`](Self::as_str)
    /// will answer.
    pub fn is_utf8(&self) -> bool {
        std::str::from_utf8(&self.0).is_ok()
    }

    /// Whether there is no rendering at all. Empty means "no value": a poisoned
    /// column, or a column the document carried no `stored` key for. A stored
    /// JSON null is **not** empty — it renders as the four bytes `null`.
    pub fn is_empty(&self) -> bool {
        self.0.is_empty()
    }

    /// The number of bytes in the rendering.
    pub fn len(&self) -> usize {
        self.0.len()
    }

    /// A lossy UTF-8 view, with U+FFFD for any byte that is not valid UTF-8.
    /// For reporting to a human; never for a value that has to round-trip.
    pub fn to_lossy(&self) -> Cow<'_, str> {
        String::from_utf8_lossy(&self.0)
    }

    /// Take the bytes.
    pub fn into_bytes(self) -> Vec<u8> {
        self.0
    }
}

impl AsRef<[u8]> for RawText {
    fn as_ref(&self) -> &[u8] {
        &self.0
    }
}

impl From<Vec<u8>> for RawText {
    fn from(b: Vec<u8>) -> RawText {
        RawText(b)
    }
}

impl From<&[u8]> for RawText {
    fn from(b: &[u8]) -> RawText {
        RawText(b.to_vec())
    }
}

impl From<String> for RawText {
    fn from(s: String) -> RawText {
        RawText(s.into_bytes())
    }
}

impl From<&str> for RawText {
    fn from(s: &str) -> RawText {
        RawText(s.as_bytes().to_vec())
    }
}

/// Lossy, and only for diagnostics — see the type's documentation.
impl fmt::Display for RawText {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.to_lossy())
    }
}

/// Never lossy: a byte that is not valid UTF-8 is printed as `\xNN`, so a debug
/// print of a value can be trusted to show what the value actually is.
impl fmt::Debug for RawText {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("\"")?;
        let mut rest: &[u8] = &self.0;
        while !rest.is_empty() {
            match std::str::from_utf8(rest) {
                Ok(s) => {
                    write_escaped(f, s)?;
                    break;
                }
                Err(e) => {
                    let good = e.valid_up_to();
                    // SAFETY-free: from_utf8 already validated this prefix.
                    if let Ok(s) = std::str::from_utf8(&rest[..good]) {
                        write_escaped(f, s)?;
                    }
                    let bad = e.error_len().unwrap_or(rest.len() - good);
                    for b in &rest[good..good + bad] {
                        write!(f, "\\x{b:02x}")?;
                    }
                    rest = &rest[good + bad..];
                }
            }
        }
        f.write_str("\"")
    }
}

fn write_escaped(f: &mut fmt::Formatter<'_>, s: &str) -> fmt::Result {
    for c in s.chars() {
        match c {
            '"' => f.write_str("\\\"")?,
            '\\' => f.write_str("\\\\")?,
            '\n' => f.write_str("\\n")?,
            '\r' => f.write_str("\\r")?,
            '\t' => f.write_str("\\t")?,
            c => write!(f, "{c}")?,
        }
    }
    Ok(())
}

// Comparison against text, so a caller (and a test) can say what it means
// without reaching for the bytes when the value is ordinary ASCII.
impl PartialEq<str> for RawText {
    fn eq(&self, other: &str) -> bool {
        self.0 == other.as_bytes()
    }
}

impl PartialEq<&str> for RawText {
    fn eq(&self, other: &&str) -> bool {
        self.0 == other.as_bytes()
    }
}

impl PartialEq<String> for RawText {
    fn eq(&self, other: &String) -> bool {
        self.0 == other.as_bytes()
    }
}

impl PartialEq<RawText> for str {
    fn eq(&self, other: &RawText) -> bool {
        self.as_bytes() == other.0
    }
}

impl PartialEq<RawText> for &str {
    fn eq(&self, other: &RawText) -> bool {
        self.as_bytes() == other.0
    }
}

impl PartialEq<RawText> for String {
    fn eq(&self, other: &RawText) -> bool {
        self.as_bytes() == other.0
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn non_utf8_bytes_survive_and_say_so() {
        // A valid ClickHouse String, and not valid UTF-8.
        let v = RawText::from(vec![b'"', 0xc3, b'(', b'"']);
        assert_eq!(v.as_bytes(), [b'"', 0xc3, b'(', b'"']);
        assert_eq!(v.as_str(), None);
        assert!(!v.is_utf8());
        // The lossy views are lossy, and only where asked for.
        assert_eq!(v.to_lossy(), "\"\u{fffd}(\"");
        assert_eq!(v.to_string(), "\"\u{fffd}(\"");
        // Debug never lies about the bytes.
        assert_eq!(format!("{v:?}"), r#""\"\xc3(\"""#);
    }

    #[test]
    fn utf8_bytes_read_as_text() {
        let v = RawText::from("\"2023-11-14 22:13:20\"");
        assert_eq!(v.as_str(), Some("\"2023-11-14 22:13:20\""));
        assert!(v.is_utf8());
        assert_eq!(v, "\"2023-11-14 22:13:20\"");
        assert_eq!("\"2023-11-14 22:13:20\"", v);
        assert_eq!(v.len(), 21);
        assert!(!v.is_empty());
    }

    #[test]
    fn empty_is_no_value_and_a_stored_null_is_not_empty() {
        assert!(RawText::default().is_empty());
        assert_eq!(RawText::default(), "");
        let null = RawText::from("null");
        assert!(!null.is_empty());
        assert_eq!(null, "null");
    }
}
