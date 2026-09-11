//! Silent-transformation detection — a faithful port of
//! `go/chtypes/transform.go`, which is the reference implementation.
//!
//! ClickHouse has no notion of "I changed your value": `readIntText` wraps mod
//! 2^N, `SerializationDateTime` truncates, `parseUUID` maps every non-hex byte
//! to `0xff`, and all of it returns success. So the fact of a change has to be
//! established from outside, and this is the one part of the product ClickHouse
//! does not provide — the part a binding could get wrong without the C library
//! noticing.
//!
//! Two independent detectors run and their union is reported:
//!
//! 1. **Supplied vs stored.** The row supplied X, ClickHouse stored Y, and Y is
//!    not X. Both are already in hand, so this models nothing — it only compares
//!    exactly. It catches what detector 2 is blind to: changes a widened type
//!    makes identically (a calendar roll-over, `2024-02-30` -> `2024-03-01`) and
//!    types with no wider type to compare against (`Float64`, `Int256`,
//!    `String`).
//! 2. **Reference type.** The C layer already parsed each field a second time
//!    through a structurally identical type with widened leaves and reported it
//!    as `ref` / `ref_type`. Both parses are ClickHouse's. This one names the
//!    *reason* precisely — an overflow wrap versus a decimal truncation — and
//!    still fires where the supplied text is not comparable (a CSV field, a
//!    base64 blob).
//!
//! Nothing here reimplements a coercion rule: detector 1 compares two values
//! already in hand, detector 2 compares two ClickHouse answers. Numeric
//! comparison is exact ([`crate::json::num_eq`]) and container comparison
//! recurses, because `[{"a": 1}]` stored as `[{"a": "1"}]` is the same data with
//! a different wire type — visible to a subscriber reading the preview, and not
//! what the table holds.

use crate::json::{self, Json};
use crate::raw::RawText;
use crate::result::{ColDoc, Transform};

/// The stable reason strings. The harness groups on them, so these spellings
/// are fixed by the spec and must not drift.
pub mod reason {
    /// An integer wrapped mod 2^N: `256` into `UInt8` stored as `0`.
    pub const OVERFLOW_WRAP: &str = "overflow_wrap";
    /// A supplied null the format layer replaced with the column's default —
    /// the single largest lossy class.
    pub const NULL_TO_DEFAULT: &str = "null_to_default";
    /// A supplied value that came back as a stored null.
    pub const NULL_LOSS: &str = "null_loss";
    /// Digits past the declared scale were dropped.
    pub const DECIMAL_TRUNCATE: &str = "decimal_truncate";
    /// A date outside the type's range was clamped to its bound.
    pub const DATE_CLAMP: &str = "date_clamp";
    /// A `DateTime`/`DateTime64` value wrapped or truncated.
    pub const DATETIME_WRAP: &str = "datetime_wrap";
    /// The calendar day changed — a roll-over such as `2024-02-30` ->
    /// `2024-03-01`, which a widened type performs identically and only the
    /// supplied-vs-stored detector can see.
    pub const DATE_SHIFT: &str = "date_shift";
    /// `parseUUID` mapped non-hex bytes to `0xff` and returned success.
    pub const UUID_MANGLE: &str = "uuid_mangle";
    /// An `IPv4`/`IPv6` parse changed the address.
    pub const IP_MANGLE: &str = "ip_mangle";
    /// A float parse lost precision.
    pub const FLOAT_PRECISION: &str = "float_precision";
    /// The value changed numerically and the type has no wider type to name a
    /// more precise reason with (`Float64`, `Int256`).
    pub const LOSSY_NUMERIC: &str = "lossy_numeric";
    /// A `FixedString` was padded or truncated to its declared width.
    pub const FIXEDSTRING_PAD: &str = "fixedstring_pad";
    /// A non-empty supplied value was stored as empty.
    pub const EMPTIED: &str = "emptied";
    /// Something inside a container changed — the comparison recurses, because
    /// `[{"a": 1}]` stored as `[{"a": "1"}]` is the same data with a different
    /// wire type.
    pub const ELEMENT_CHANGED: &str = "element_changed";
    /// An `Enum` value was coerced to a different member.
    pub const ENUM_COERCE: &str = "enum_coerce";
    /// The value changed and none of the more specific reasons applies.
    pub const VALUE_CHANGED: &str = "value_changed";
    /// ClickHouse accepted the insert and stored a value it cannot read back:
    /// every later `SELECT` fails with code 691.
    pub const POISONED: &str = "poisoned";
    /// The row supplied this column twice and ClickHouse kept the **first**
    /// value, silently discarding the later one — the opposite of what most
    /// senders would assume, which is why it is lossy.
    pub const DUPLICATE_KEY_DROPPED: &str = "duplicate_key_dropped";
    /// A representation change with nothing lost (`1700000000` stored as
    /// `"2023-11-14 22:13:20"`). Still reported: a subscriber reading the
    /// preview would otherwise see a different JSON type than the table holds.
    pub const REFORMAT: &str = "reformat";
    /// A column the row never supplied that ClickHouse populated from the DDL
    /// DEFAULT.
    pub const DEFAULT_FILLED: &str = "default_filled";
    /// A column the row never supplied that took the type's own zero.
    pub const ZERO_FILLED: &str = "zero_filled";
    /// A **volatile** DEFAULT this library resolved from its own clock, which the
    /// caller must send as an explicit column. A separate reason from
    /// `default_filled` because the claim is different: the tenant is being
    /// shown a value the gateway invented, not one the server chose.
    pub const DEFAULT_MATERIALIZED: &str = "default_materialized";
    /// The whole row is past the table TTL: **not stored**.
    pub const TTL_EXPIRED: &str = "ttl_expired";
    /// The value is past a column TTL and was reset to the column's DEFAULT.
    pub const TTL_COLUMN_EXPIRED: &str = "ttl_column_expired";
}

/// Derive the transformations for one column document.
pub(crate) fn classify(c: &ColDoc) -> Vec<Transform> {
    match c.src.as_str() {
        // Never read from an input row, or no value at all: nothing to compare.
        "skipped"
        | "default_expr_unsupported"
        | "default_volatile_unresolved"
        | "default_pending" => return Vec::new(),
        _ => {}
    }

    // Accept-then-poison: ClickHouse stored a value it cannot read back. Not a
    // changed value — a destroyed one — but silent at insert time, which is what
    // this exists to surface.
    if c.poison {
        return vec![one(c, RawText::from("<unreadable>"), reason::POISONED)];
    }

    // Both renderings stay BYTES from here on: a `String` column holds arbitrary
    // bytes, and repairing one side of a comparison and not the other is how a
    // change that never happened gets reported.
    let stored = c.stored_raw();
    let reference = c.ref_raw();

    // A duplicate key whose later value ClickHouse discarded. Reported first
    // because the value comparison below sees only the value that survived.
    if c.dup_dropped {
        return vec![one(c, stored, reason::DUPLICATE_KEY_DROPPED)];
    }

    // A column the row did not supply that ClickHouse populated. `input` is
    // empty — an absent column has none — and the reason says where the value
    // came from; dropping these leaves a tenant unable to see what will be
    // stored for a field they never sent.
    let filled = match c.src.as_str() {
        "default" => Some(reason::DEFAULT_FILLED),
        "default_substituted" => Some(reason::DEFAULT_MATERIALIZED),
        "absent" => Some(reason::ZERO_FILLED),
        _ => None,
    };
    if let Some(r) = filled {
        return vec![Transform {
            column: c.name.clone(),
            input: String::new(),
            stored: stored.to_lossy().into_owned(),
            reason: r.to_string(),
            row: 0,
        }];
    }

    // ---- detector 2: reference type. Run first because it names the reason.
    let mut ref_reason = "";
    if !c.ref_type.is_empty() && !reference.is_empty() && !equivalent(&c.base, &stored, &reference)
    {
        // Only the reason NAME is picked here, from the type and the shape of the
        // two renderings, so a lossy view is enough for this one step.
        ref_reason = reason_for(&c.base, &stored.to_lossy(), &reference.to_lossy());
    }

    // ---- detector 1: supplied vs stored.
    let mut supplied_reason = "";
    if c.src == "input" {
        match (
            json::parse_exact_bytes(c.input.as_bytes()),
            json::parse_exact_bytes(stored.as_bytes()),
        ) {
            (Some(sup), Some(st)) => {
                if !same_value(&sup, &st) {
                    supplied_reason = severity(&sup, &st);
                } else if !same_kind_deep(&sup, &st) {
                    // Same value, different JSON type: "1024" -> 1024, or a
                    // number inside an Array(Map(String,String)) coming back as
                    // a string. Nothing is lost, but the preview and the table
                    // disagree on the type, and the check has to recurse.
                    supplied_reason = reason::REFORMAT;
                }
            }
            _ if c.null_input && !c.nullable => {
                // A null the format layer replaced with a default. In CSV/TSV the
                // raw text is `\N`, which is not JSON, so it never reaches the
                // branch above — and this is the single largest lossy class.
                supplied_reason = reason::NULL_TO_DEFAULT;
            }
            _ => {}
        }
    }

    if ref_reason.is_empty() && supplied_reason.is_empty() {
        return Vec::new();
    }
    // Prefer the reference detector's reason — it can tell an overflow wrap from
    // a decimal truncation, where supplied-vs-stored can only say "numeric" —
    // but never let it downgrade a lossy finding to `reformat`.
    let mut chosen = ref_reason;
    if chosen.is_empty() || (chosen == reason::REFORMAT && !supplied_reason.is_empty()) {
        chosen = supplied_reason;
    }
    // An unclassified numeric/temporal leaf (no reference-ladder entry in the
    // C layer): the precise detector never ran, so a visible change cannot be
    // vouched non-lossy. Claim lossy rather than hide a possible loss.
    if c.ref_unclassified && chosen == reason::REFORMAT {
        chosen = reason::VALUE_CHANGED;
    }
    vec![one(c, stored, chosen)]
}

/// One reported transform.
///
/// This is where bytes become the reported *text*: `Transform` is a warning to a
/// tenant, not a value that round-trips, and the reference SDK's own JSON decoder
/// repairs `input` the same way — so the two SDKs report identical text. The
/// authoritative bytes of the same value are in `RowResult::values`. See the
/// `Transform` documentation.
fn one(c: &ColDoc, stored: RawText, reason: &str) -> Transform {
    Transform {
        column: c.name.clone(),
        input: c.input.to_lossy().into_owned(),
        stored: stored.to_lossy().into_owned(),
        reason: reason.to_string(),
        row: 0,
    }
}

// ------------------------------------------------------ supplied vs stored

/// Equal after canonicalization, where a number and its decimal string spelling
/// are the same value (`5` vs `"5"`) but a float that has thrown away 60 digits
/// of an `Int256` is not.
fn same_value(a: &Json, b: &Json) -> bool {
    match (a, b) {
        (Json::Object(x), Json::Object(y)) => objects_eq(x, y, same_value),
        (Json::Array(x), Json::Array(y)) => {
            x.len() == y.len() && x.iter().zip(y).all(|(xv, yv)| same_value(xv, yv))
        }
        (Json::Object(_) | Json::Array(_), _) | (_, Json::Object(_) | Json::Array(_)) => false,
        (Json::Null, Json::Null) => true,
        (Json::Null, _) | (_, Json::Null) => false,
        (Json::Bool(x), Json::Bool(y)) => x == y,
        (Json::Bool(_), _) | (_, Json::Bool(_)) => false,
        _ => {
            let (at, a_is_str) = match a.scalar_text() {
                Some(v) => v,
                None => return false,
            };
            let (bt, b_is_str) = match b.scalar_text() {
                Some(v) => v,
                None => return false,
            };
            if at == bt && a_is_str == b_is_str {
                return true;
            }
            // Denormals: ClickHouse renders them as "nan" / "inf" / "-inf".
            if json::denormal(&at).is_some() || json::denormal(&bt).is_some() {
                return json::denormal(&at) == json::denormal(&bt);
            }
            json::num_eq(&at, &bt)
        }
    }
}

fn is_numeric_value(v: &Json) -> bool {
    v.scalar_text().is_some_and(|(t, _)| json::is_numeric(&t))
}

/// Mirrors the arbiter's `_severity`, so this crate's reasons and the bake-off's
/// classes describe the same thing.
fn severity(a: &Json, b: &Json) -> &'static str {
    if is_numeric_value(a) && is_numeric_value(b) {
        return reason::LOSSY_NUMERIC;
    }
    let a_null = matches!(a, Json::Null);
    let b_null = matches!(b, Json::Null);
    if a_null && !b_null {
        return reason::NULL_TO_DEFAULT; // the arbiter's `null_filled`
    }
    if !a_null && b_null {
        return reason::NULL_LOSS;
    }
    let at = a.scalar_text();
    let bt = b.scalar_text();
    if let (Some((at, true)), Some((bt, true))) = (&at, &bt) {
        if dateish(at) && dateish(bt) {
            if at.as_bytes()[..10] != bt.as_bytes()[..10] {
                return reason::DATE_SHIFT;
            }
            return reason::REFORMAT;
        }
        if !at.is_empty() && bt.is_empty() {
            return reason::EMPTIED;
        }
        return reason::REFORMAT;
    }
    if let Some((bt, true)) = &bt {
        if json::denormal(bt).is_some() {
            return reason::LOSSY_NUMERIC;
        }
    }
    reason::REFORMAT
}

/// Compares JSON shape recursively. Two values can be equal by [`same_value`]
/// and still differ in type somewhere inside.
fn same_kind_deep(a: &Json, b: &Json) -> bool {
    if a.kind() != b.kind() {
        return false;
    }
    match (a, b) {
        // Two strings that are "equal" only after denormal folding are still a
        // visible change: the row said "NaN" and the table holds "nan".
        (Json::Str(x), Json::Str(y)) => x == y,
        (Json::Object(x), Json::Object(y)) => objects_eq(x, y, same_kind_deep),
        (Json::Array(x), Json::Array(y)) => {
            x.len() == y.len() && x.iter().zip(y).all(|(xv, yv)| same_kind_deep(xv, yv))
        }
        _ => true,
    }
}

/// Compare two JSON objects **without resolving a key by first match**.
///
/// [`Json`] keeps duplicate keys, deliberately, and unlike the reference SDK's
/// `map[string]any`: ClickHouse's `Map` accepts a duplicate key and stores both
/// entries, so `{"1":1,"1":2}` is a real row and its stored rendering is the same
/// text. Resolving each key by first match compared the *second* member against
/// the *first* and reported a change on a value ClickHouse had stored unchanged —
/// 22 replies of the run of record, every one with `input == stored`.
///
/// Positional first, because ClickHouse renders a `Map` in the order it read it,
/// so the common case is a straight member-for-member comparison. Then a
/// multiset match, so a genuine reordering still compares like with like. Greedy
/// is enough: what was wrong was comparing a member against one it was never
/// paired with, not the absence of a maximum-matching algorithm.
fn objects_eq(x: &[(Vec<u8>, Json)], y: &[(Vec<u8>, Json)], eq: fn(&Json, &Json) -> bool) -> bool {
    if x.len() != y.len() {
        return false;
    }
    let positional = x.iter().zip(y).all(|((kx, _), (ky, _))| kx == ky)
        && x.iter().zip(y).all(|((_, xv), (_, yv))| eq(xv, yv));
    if positional {
        return true;
    }
    let mut used = vec![false; y.len()];
    for (k, xv) in x {
        let paired = y
            .iter()
            .enumerate()
            .position(|(j, (k2, yv))| !used[j] && k2 == k && eq(xv, yv));
        match paired {
            Some(j) => used[j] = true,
            None => return false,
        }
    }
    true
}

fn dateish(s: &str) -> bool {
    let b = s.as_bytes();
    if b.len() < 10 {
        return false;
    }
    (0..10).all(|i| {
        if i == 4 || i == 7 {
            b[i] == b'-'
        } else {
            b[i].is_ascii_digit()
        }
    })
}

// ---------------------------------------------------------- reference type

fn reason_for(base: &str, stored: &str, reference: &str) -> &'static str {
    if base.starts_with("Enum") {
        reason::ENUM_COERCE
    } else if base.starts_with("Array") || base.starts_with("Tuple") || base.starts_with("Map") {
        reason::ELEMENT_CHANGED
    } else if base.starts_with("Decimal") {
        if trim_num(reference).starts_with(trim_num(stored)) {
            reason::DECIMAL_TRUNCATE
        } else {
            reason::OVERFLOW_WRAP
        }
    } else if base.starts_with("DateTime") {
        reason::DATETIME_WRAP
    } else if base.starts_with("Date") {
        reason::DATE_CLAMP
    } else if base.starts_with("Time") {
        // Time / Time64 (25.8+): the same truncation mechanism as DateTime64 —
        // sub-second ticks at the declared scale, whole seconds in Time.
        reason::DATETIME_WRAP
    } else if base == "UUID" {
        reason::UUID_MANGLE
    } else if base == "IPv4" || base == "IPv6" {
        reason::IP_MANGLE
    } else if base == "Float32" || base == "Float64" || base == "BFloat16" {
        reason::FLOAT_PRECISION
    } else if base.starts_with("FixedString") {
        reason::FIXEDSTRING_PAD
    } else if is_int_family(base) {
        reason::OVERFLOW_WRAP
    } else {
        reason::VALUE_CHANGED
    }
}

fn is_int_family(base: &str) -> bool {
    base.starts_with("UInt") || base.starts_with("Int") || base == "Bool"
}

/// Whether two ClickHouse renderings mean the same value. Only formatting
/// differences are collapsed — anything else is a real change.
///
/// The byte comparison comes first and is exact. Everything after it is numeric
/// or textual, so it needs both renderings as text: two different byte sequences
/// where one is not valid UTF-8 are a real difference, and repairing them into
/// U+FFFD could make them compare equal and hide a change.
fn equivalent(base: &str, stored: &RawText, reference: &RawText) -> bool {
    if stored == reference {
        return true;
    }
    match (stored.as_str(), reference.as_str()) {
        (Some(s), Some(r)) => equivalent_text(base, s, r),
        _ => false,
    }
}

fn equivalent_text(base: &str, stored: &str, reference: &str) -> bool {
    // Bool renders as true/false; its Int256 reference renders as 1/0.
    if base == "Bool" {
        return bool_norm(stored) == bool_norm(reference);
    }
    // DateTime64 references widen the scale: ".123" vs ".123000000" — and so
    // do Time64's (Time/Time64 arrived in 25.8; same rendering shape).
    if base.starts_with("DateTime") || base.starts_with("Time") {
        return trim_frac(stored) == trim_frac(reference);
    }
    // Decimal references widen the scale: "2.50" vs "2.5000000".
    if base.starts_with("Decimal")
        || is_int_family(base)
        || base == "Float32"
        || base == "Float64"
        || base == "BFloat16"
    {
        return json::num_eq(trim_num(stored), trim_num(reference));
    }
    if base.starts_with("Array") || base.starts_with("Tuple") || base.starts_with("Map") {
        return compact_json(stored) == compact_json(reference)
            || num_list_equal(stored, reference);
    }
    false
}

fn bool_norm(s: &str) -> String {
    match s.trim_matches('"') {
        "true" | "1" => "1".to_string(),
        "false" | "0" => "0".to_string(),
        _ => s.to_string(),
    }
}

/// Drops trailing zeros (and a bare dot) from a datetime's fractional part so
/// `DateTime64(3)` and its `DateTime64(9)` reference compare equal.
fn trim_frac(s: &str) -> String {
    let Some(i) = s.rfind('.') else {
        return s.to_string();
    };
    let mut end = s.len();
    let quoted = s.ends_with('"');
    if quoted {
        end -= 1;
    }
    if i + 1 > end {
        return s.to_string();
    }
    let frac = s[i + 1..end].trim_end_matches('0');
    let mut out = s[..i].to_string();
    if !frac.is_empty() {
        out.push('.');
        out.push_str(frac);
    }
    if quoted {
        out.push('"');
    }
    out
}

fn trim_num(s: &str) -> &str {
    s.trim_matches('"')
}

/// Compares two rendered containers element-wise as numbers, so `[1,2]` and
/// `[1.000,2.000]` agree while `[1,0,3]` and `[1,256,3]` do not.
fn num_list_equal(a: &str, b: &str) -> bool {
    let ta = tokenize_nums(a);
    let tb = tokenize_nums(b);
    if ta.len() != tb.len() || ta.is_empty() {
        return false;
    }
    ta.iter().zip(&tb).all(|(x, y)| json::num_eq(x, y)) && skeleton(a) == skeleton(b)
}

/// Pulls the numeric literals out of a rendered container.
fn tokenize_nums(s: &str) -> Vec<&str> {
    let b = s.as_bytes();
    let mut out = Vec::new();
    let mut i = 0;
    while i < b.len() {
        let c = b[i];
        if c == b'"' {
            // Skip strings wholesale.
            i += 1;
            while i < b.len() && b[i] != b'"' {
                if b[i] == b'\\' {
                    i += 1;
                }
                i += 1;
            }
            i += 1;
            continue;
        }
        if c == b'-' || c == b'+' || c.is_ascii_digit() {
            let mut j = i + 1;
            while j < b.len()
                && (b[j] == b'.'
                    || b[j] == b'e'
                    || b[j] == b'E'
                    || b[j] == b'-'
                    || b[j] == b'+'
                    || b[j].is_ascii_digit())
            {
                j += 1;
            }
            out.push(&s[i..j]);
            i = j;
            continue;
        }
        i += 1;
    }
    out
}

/// Keeps only structural punctuation, so two renderings that agree numerically
/// but differ in shape are still reported as different.
fn skeleton(s: &str) -> String {
    let b = s.as_bytes();
    let mut out = String::new();
    let mut in_str = false;
    let mut i = 0;
    while i < b.len() {
        let c = b[i];
        if in_str {
            if c == b'\\' {
                i += 1;
            } else if c == b'"' {
                in_str = false;
                out.push('"');
            }
            i += 1;
            continue;
        }
        match c {
            b'"' => {
                in_str = true;
                out.push('"');
            }
            b'[' | b']' | b'{' | b'}' | b'(' | b')' | b',' | b':' => out.push(c as char),
            _ => {}
        }
        i += 1;
    }
    out
}

fn compact_json(s: &str) -> String {
    let b = s.as_bytes();
    let mut out = String::new();
    let mut in_str = false;
    let mut i = 0;
    while i < b.len() {
        let c = b[i];
        if in_str {
            out.push(c as char);
            if c == b'\\' && i + 1 < b.len() {
                i += 1;
                out.push(b[i] as char);
            } else if c == b'"' {
                in_str = false;
            }
            i += 1;
            continue;
        }
        match c {
            b' ' | b'\t' | b'\n' | b'\r' => {}
            b'"' => {
                in_str = true;
                out.push('"');
            }
            _ => {
                // Multi-byte UTF-8 passes through byte by byte via the string's
                // own bytes, so push through a char boundary walk instead.
                let ch_len = utf8_len(c);
                out.push_str(&s[i..i + ch_len]);
                i += ch_len;
                continue;
            }
        }
        i += 1;
    }
    out
}

fn utf8_len(first: u8) -> usize {
    match first {
        0x00..=0x7f => 1,
        0xc0..=0xdf => 2,
        0xe0..=0xef => 3,
        _ => 4,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn col(
        base: &str,
        src: &str,
        input: &str,
        stored: &str,
        reference: &str,
        ref_type: &str,
    ) -> ColDoc {
        ColDoc {
            name: "x".into(),
            declared_type: base.into(),
            base: base.into(),
            src: src.into(),
            input: RawText::from(input),
            ref_type: ref_type.into(),
            stored: (!stored.is_empty()).then(|| RawText::from(stored)),
            reference: (!reference.is_empty()).then(|| RawText::from(reference)),
            ..Default::default()
        }
    }

    #[test]
    fn overflow_wrap_beats_a_bare_numeric_severity() {
        let t = classify(&col("UInt8", "input", "256", "0", "256", "Int256"));
        assert_eq!(t[0].reason, reason::OVERFLOW_WRAP);
        assert_eq!(t[0].input, "256");
        assert_eq!(t[0].stored, "0");
        assert!(t[0].lossy());
    }

    #[test]
    fn a_widened_scale_is_not_a_transformation() {
        assert!(
            classify(&col(
                "Decimal(18, 4)",
                "input",
                "2.5",
                "2.5000",
                "2.500000000",
                "Decimal(76, 24)"
            ))
            .is_empty()
        );
        assert!(
            classify(&col(
                "DateTime64(3)",
                "input",
                "\"2020-01-01 00:00:00.123\"",
                "\"2020-01-01 00:00:00.123\"",
                "\"2020-01-01 00:00:00.123000000\"",
                "DateTime64(9)"
            ))
            .is_empty()
        );
    }

    #[test]
    fn decimal_truncation_is_told_from_a_wrap() {
        let t = classify(&col(
            "Decimal(18, 2)",
            "input",
            "2.555",
            "2.55",
            "2.555",
            "Decimal(76, 24)",
        ));
        assert_eq!(t[0].reason, reason::DECIMAL_TRUNCATE);
    }

    #[test]
    fn a_reformat_is_reported_and_is_not_lossy() {
        // "1024" supplied as a string, stored as a number: same value, different
        // wire type. The tenant's subscriber would see the difference.
        let t = classify(&col(
            "UInt32", "input", "\"1024\"", "1024", "1024", "Int256",
        ));
        assert_eq!(t[0].reason, reason::REFORMAT);
        assert!(!t[0].lossy());
    }

    #[test]
    fn a_container_element_change_recurses() {
        let t = classify(&col(
            "Array(UInt8)",
            "input",
            "[1,256,3]",
            "[1,0,3]",
            "[1,256,3]",
            "Array(Int256)",
        ));
        assert_eq!(t[0].reason, reason::ELEMENT_CHANGED);
        // Numerically equal containers are not a change.
        assert!(
            classify(&col(
                "Array(Decimal(18, 4))",
                "input",
                "[1,2]",
                "[1.0000,2.0000]",
                "[1.000000,2.000000]",
                "Array(Decimal(76, 24))",
            ))
            .is_empty()
        );
    }

    #[test]
    fn a_calendar_rollover_is_caught_by_the_supplied_detector_alone() {
        // Both parses agree (the widened type rolls over identically), so only
        // detector 1 can see this one.
        let t = classify(&col(
            "Date",
            "input",
            "\"2024-02-30\"",
            "\"2024-03-01\"",
            "\"2024-03-01\"",
            "Date32",
        ));
        assert_eq!(t[0].reason, reason::DATE_SHIFT);
        assert!(t[0].lossy());
    }

    #[test]
    fn a_null_replaced_by_a_default_is_lossy() {
        let mut c = col("UInt8", "input", "\\N", "0", "", "Int256");
        c.null_input = true;
        let t = classify(&c);
        assert_eq!(t[0].reason, reason::NULL_TO_DEFAULT);
        assert!(t[0].lossy());
    }

    #[test]
    fn poison_and_duplicate_keys_take_priority() {
        let mut c = col("Enum8('a' = 1)", "input", "null", "0", "", "");
        c.poison = true;
        assert_eq!(classify(&c)[0].reason, reason::POISONED);
        assert_eq!(classify(&c)[0].stored, "<unreadable>");

        let mut c = col("UInt8", "input", "1", "1", "1", "Int256");
        c.dup_dropped = true;
        assert_eq!(classify(&c)[0].reason, reason::DUPLICATE_KEY_DROPPED);
    }

    #[test]
    fn filled_columns_report_where_the_value_came_from() {
        assert_eq!(
            classify(&col("UInt8", "default", "e + 1", "2", "", ""))[0].reason,
            reason::DEFAULT_FILLED
        );
        assert_eq!(
            classify(&col(
                "DateTime",
                "default_substituted",
                "now()",
                "\"2023-11-14 22:13:20\"",
                "",
                ""
            ))[0]
                .reason,
            reason::DEFAULT_MATERIALIZED
        );
        assert_eq!(
            classify(&col("UInt8", "absent", "", "0", "", ""))[0].reason,
            reason::ZERO_FILLED
        );
        for r in [
            reason::DEFAULT_FILLED,
            reason::DEFAULT_MATERIALIZED,
            reason::ZERO_FILLED,
        ] {
            let t = Transform {
                column: "x".into(),
                input: String::new(),
                stored: "0".into(),
                reason: r.into(),
                row: 0,
            };
            assert!(!t.lossy(), "{r} must not be lossy");
        }
    }

    #[test]
    fn declined_and_skipped_columns_emit_nothing() {
        for src in [
            "skipped",
            "default_expr_unsupported",
            "default_volatile_unresolved",
            "default_pending",
        ] {
            assert!(classify(&col("UInt8", src, "", "0", "", "")).is_empty());
        }
    }

    #[test]
    fn a_duplicate_key_that_clickhouse_kept_is_not_a_transformation() {
        // ClickHouse's Map accepts a duplicate key and stores BOTH entries, so
        // input and stored are the same text. Resolving each key by first match
        // compared the second member against the first and reported a change
        // that never happened.
        assert!(
            classify(&col(
                "Map(Int32, UInt256)",
                "input",
                r#"{"1":1,"1":2}"#,
                r#"{"1":1,"1":2}"#,
                "",
                "",
            ))
            .is_empty(),
            "a transform whose input equals its stored is a change that did not happen"
        );
        // Same, with a duplicated string key, and with three entries.
        assert!(
            classify(&col(
                "Map(String, String)",
                "input",
                r#"{"a":"1","a":"2","b":"3"}"#,
                r#"{"a":"1","a":"2","b":"3"}"#,
                "",
                "",
            ))
            .is_empty()
        );
        // A REAL change inside a duplicate-keyed object is still caught: the
        // second entry wrapped mod 2^8.
        let t = classify(&col(
            "Map(Int32, UInt8)",
            "input",
            r#"{"1":1,"1":256}"#,
            r#"{"1":1,"1":0}"#,
            "",
            "",
        ));
        assert_eq!(t.len(), 1, "{t:?}");
        // And a reordering of duplicate members still compares like with like.
        assert!(same_value(
            &json::parse_exact(r#"{"a":1,"a":2}"#).unwrap(),
            &json::parse_exact(r#"{"a":2,"a":1}"#).unwrap(),
        ));
        assert!(!same_value(
            &json::parse_exact(r#"{"a":1,"a":2}"#).unwrap(),
            &json::parse_exact(r#"{"a":1,"a":3}"#).unwrap(),
        ));
        // A duplicate key ClickHouse DROPPED is still reported — that path is
        // the wrapper's `dup_dropped` flag, not a value comparison.
        let mut c = col("UInt8", "input", "1", "1", "1", "Int256");
        c.dup_dropped = true;
        assert_eq!(classify(&c)[0].reason, reason::DUPLICATE_KEY_DROPPED);
    }

    #[test]
    fn a_non_utf8_string_value_is_compared_as_bytes_and_reported_as_text() {
        // `x String` fed the bytes 0xC3 0x28: ClickHouse stores them unchanged,
        // so there is no transformation to report. Repairing one side of the
        // comparison and not the other would invent one.
        let mut c = col("String", "input", "", "", "", "");
        c.input = RawText::from(vec![b'"', 0xc3, b'(', b'"']);
        c.stored = Some(RawText::from(vec![b'"', 0xc3, b'(', b'"']));
        assert!(classify(&c).is_empty(), "{:?}", classify(&c));

        // A real change to those bytes IS reported, and the report is text.
        let mut c2 = col("String", "input", "", "", "", "");
        c2.input = RawText::from(vec![b'"', 0xc3, b'(', b'"']);
        c2.stored = Some(RawText::from(vec![b'"', 0xc3, b'(', b'!', b'"']));
        let t = classify(&c2);
        assert_eq!(t.len(), 1, "{t:?}");
        assert_eq!(t[0].input, "\"\u{fffd}(\"");
        assert_eq!(t[0].stored, "\"\u{fffd}(!\"");
    }

    #[test]
    fn an_int256_is_never_routed_through_a_float() {
        // The exact text differs by one unit in the last place; a float would
        // collapse both to the same value and report no change.
        let t = classify(&col(
            "Int256",
            "input",
            "18446744073709551615",
            "18446744073709551614",
            "",
            "",
        ));
        assert_eq!(t[0].reason, reason::LOSSY_NUMERIC);
    }
}
