package chtypes

// Silent-transformation detection.
//
// ClickHouse has no notion of "I changed your value": readIntText wraps mod
// 2^N, SerializationDateTime truncates to UInt32, parseUUID maps every non-hex
// byte to 0xff, and all of it returns success. So the fact of a change has to
// be established from outside.
//
// Two independent detectors run, and their union is reported:
//
//  1. **Supplied vs stored.** The direct reading of the contract: the row
//     supplied X, ClickHouse stored Y, and Y is not X. Both are already in
//     hand — the raw field bytes and ClickHouse's own rendering of what it
//     kept — so this models nothing, it only compares exactly. It catches the
//     changes detector 2 is blind to: those a widened type makes identically
//     (a calendar roll-over, 2024-02-30 -> 2024-03-01), and those in types
//     that have no wider type to compare against (Float64, Int256, String).
//
//  2. **Reference type.** The C layer parses each field a second time through
//     a structurally identical type with widened leaves (UInt8 -> Int256,
//     DateTime -> DateTime64(0), Decimal(18,4) -> Decimal(76,24), UUID ->
//     String, Array(T) -> Array(ref T)). Both parses are ClickHouse's. This
//     one names the *reason* precisely — it distinguishes an overflow wrap
//     from a decimal truncation — and it still fires where the supplied text
//     is not comparable (a CSV field, a base64 blob).
//
// Nothing here reimplements a coercion rule: detector 1 compares two values we
// already have, detector 2 compares two ClickHouse answers.
//
// Reasons separate *lossy* changes from `reformat`, a representation change
// with nothing lost (1700000000 -> "2023-11-14 22:13:20"). Both are reported —
// a preview must show the tenant what will actually be stored — but only the
// lossy ones are a warning. `Transform.Lossy()` is the filter.

import (
	"encoding/json"
	"math/big"
	"strings"
)

// Reasons emitted by classify. Stable strings — the harness groups on them,
// so the spellings must never change (docs/reference/bindings.md §Transformed). All are
// lossy except the four Transform.Lossy names as non-lossy.
const (
	// ReasonOverflowWrap: an integer wrapped mod 2^N (256 into UInt8 → 0).
	ReasonOverflowWrap = "overflow_wrap"
	// ReasonNullToDefault: a supplied null landed in a non-Nullable column
	// and was replaced with the column's default value.
	ReasonNullToDefault = "null_to_default"
	// ReasonNullLoss: a supplied value was stored as null.
	ReasonNullLoss = "null_loss"
	// ReasonDecimalTruncate: a Decimal lost fractional digits past its scale.
	ReasonDecimalTruncate = "decimal_truncate"
	// ReasonDateClamp: a Date outside the type's range clamped to a bound.
	ReasonDateClamp = "date_clamp"
	// ReasonDateTimeWrap: a DateTime/DateTime64/Time value wrapped or
	// truncated (e.g. sub-second ticks past the declared scale).
	ReasonDateTimeWrap = "datetime_wrap"
	// ReasonDateShift: a date-looking string stored as a DIFFERENT calendar
	// day (2024-02-30 → 2024-03-01).
	ReasonDateShift = "date_shift"
	// ReasonUUIDMangle: a malformed UUID parsed with non-hex bytes mapped to
	// 0xff rather than refused.
	ReasonUUIDMangle = "uuid_mangle"
	// ReasonIPMangle: an IPv4/IPv6 value silently altered by the parser.
	ReasonIPMangle = "ip_mangle"
	// ReasonFloatPrecision: a float lost precision on the way to storage.
	ReasonFloatPrecision = "float_precision"
	// ReasonLossyNumeric: a numeric change the classifier cannot name more
	// precisely (also denormal folding: 1e400 stored as "inf").
	ReasonLossyNumeric = "lossy_numeric"
	// ReasonStringPad: a FixedString padded with NUL bytes to its width.
	ReasonStringPad = "fixedstring_pad"
	// ReasonEmptied: a non-empty string stored as the empty string.
	ReasonEmptied = "emptied"
	// ReasonElementChanged: a change inside an Array/Tuple/Map element.
	ReasonElementChanged = "element_changed"
	// ReasonEnumCoerce: an Enum input coerced to a different member spelling.
	ReasonEnumCoerce = "enum_coerce"
	// ReasonValueChanged: a visible change no more precise reason covers —
	// also the conservative upgrade when the reference detector could not run
	// (ref_unclassified) or a text-format wire round-trip differs.
	ReasonValueChanged = "value_changed"
	// ReasonPoisoned: ClickHouse stored a value it cannot read back; every
	// later SELECT fails with code 691. Still an ACCEPTED insert.
	ReasonPoisoned = "poisoned"
	// ReasonDuplicateKeyDropped: the row supplied this column twice and
	// ClickHouse kept the FIRST value, silently discarding the later one.
	// Lossy: the tenant's most recent value for the field is the one thrown
	// away, which is the opposite of what most senders would assume.
	ReasonDuplicateKeyDropped = "duplicate_key_dropped"
	// ReasonReformat is a representation change with no information lost: the
	// same value in a different spelling ("1024" stored as 1024, true as
	// "true", "10:30:00" as "10:30:00.000"). An SSE subscriber reading the
	// preview would otherwise see a different JSON type than the table holds.
	ReasonReformat = "reformat"
	// ReasonDefaultFilled / ReasonZeroFilled: a column the row never supplied
	// that ClickHouse populated — from the DDL DEFAULT, or from the type zero.
	// Not a loss (nothing was sent to lose), but it belongs in a preview.
	ReasonDefaultFilled = "default_filled"
	ReasonZeroFilled    = "zero_filled"
	// ReasonDefaultMaterialized: a VOLATILE DEFAULT (now()/now64(n)/today()/
	// yesterday(), or an expression over one) that this library resolved from
	// its own clock and the caller must send as an explicit column. A separate
	// reason from default_filled because the claim is different: the tenant is
	// being shown a value the *gateway* invented, not one the server chose.
	ReasonDefaultMaterialized = "default_materialized"

	// The storage layer's own verdicts on rows the type layer accepted,
	// produced by the vendored TTL pipeline (TTLDeleteAlgorithm /
	// TTLColumnAlgorithm run at preview time the way OPTIMIZE FINAL runs them
	// at merge time). Both are LOSSY by construction: the row, or the value,
	// is gone the moment the table applies its own rules.
	ReasonTTLExpired       = "ttl_expired"        // the whole row is past its TTL: not stored
	ReasonTTLColumnExpired = "ttl_column_expired" // the value reset to the column DEFAULT
)

// nonLossy are the reasons that change how a value is written, or add one the
// row never carried, without losing information.
func (t Transform) lossyReason() bool {
	switch t.Reason {
	case ReasonReformat, ReasonDefaultFilled, ReasonZeroFilled, ReasonDefaultMaterialized:
		return false
	}
	return true
}

func classify(c colDoc) []Transform {
	switch c.Src {
	case "skipped", "default_expr_unsupported", "default_volatile_unresolved", "default_pending":
		return nil
	}

	// The accept-then-poison class: ClickHouse stores a value it cannot read
	// back. Not a changed value — a destroyed one — but silent at insert time,
	// which is what Transformed exists to surface.
	if c.Poison {
		return []Transform{{Column: c.Name, Input: c.Input,
			Stored: "<unreadable>", Reason: ReasonPoisoned}}
	}

	stored, ref := c.Stored(), c.Ref()

	// A duplicate key whose later value ClickHouse discarded. Reported first
	// because the value comparison below sees only the value that survived,
	// so nothing else in this function can notice it.
	if c.DupDropped {
		return []Transform{{Column: c.Name, Input: c.Input, Stored: stored,
			Reason: ReasonDuplicateKeyDropped}}
	}

	// `filled`: a column the row did not supply that ClickHouse populated. The
	// contract's Transform carries an Input, which an absent column has none
	// of, so Input is the empty string and the reason says where the value
	// came from. Skipping the row entirely — which this used to do — leaves a
	// tenant unable to see what will be stored for a field they never sent.
	switch c.Src {
	case "default":
		return []Transform{{Column: c.Name, Input: "", Stored: stored,
			Reason: ReasonDefaultFilled}}
	case "default_substituted":
		return []Transform{{Column: c.Name, Input: "", Stored: stored,
			Reason: ReasonDefaultMaterialized}}
	case "absent":
		return []Transform{{Column: c.Name, Input: "", Stored: stored,
			Reason: ReasonZeroFilled}}
	}

	// ---- detector 2: reference type. Run first because it names the reason.
	refReason := ""
	if c.RefType != "" && ref != "" && !equivalent(c.Base, stored, ref) {
		refReason = reasonFor(c.Base, stored, ref)
	}

	// ---- detector 1: supplied vs stored.
	suppliedReason := ""
	if c.Src == "input" {
		sup, supOK := parseJSONValue(c.Input)
		st, stOK := parseJSONValue(stored)
		switch {
		case supOK && stOK:
			if !sameValue(sup, st) {
				suppliedReason = severity(sup, st)
			} else if !sameKindDeep(sup, st) {
				// Same value, different JSON type: "1024" -> 1024, or a number
				// inside an Array(Map(String,String)) coming back as a string.
				// Nothing is lost, but the preview and the table disagree on
				// the type, and the check has to recurse: the change is often
				// inside a container, not at the top level.
				suppliedReason = ReasonReformat
			}
		case c.NullInput && !c.Nullable:
			// A null the format layer replaced with a default. In CSV/TSV the
			// raw text is `\N`, which is not JSON, so it never reaches the
			// branch above — and this is the single largest lossy class.
			suppliedReason = ReasonNullToDefault
		case c.Wire != nil:
			// A TEXT field. `Input` is not a JSON value, so the comparison
			// above cannot run — and this is where the detector used to fall
			// silent on the very changes it exists to catch: TSV `[NULL]` into
			// Array(String) stores `[""]`, TSV `2026/01/15 10:30:00` into
			// DateTime stores `2026-01-15 10:30:00`, and the reference
			// detector sees neither (Array(String) has no wider leaf; a
			// DateTime spelling survives the widening to DateTime64).
			//
			// `Wire` is the stored value written back by ClickHouse's own
			// serializer for THIS field's vocabulary, so the two strings are
			// comparable exactly. Any difference is one ClickHouse made.
			// Nothing is modeled here: both sides are the vendored code's.
			if *c.Wire != c.Input {
				// Text alone cannot separate a re-spelling from a loss, so it
				// claims the lossy one and lets the reference detector, which
				// CAN tell them apart, override below. Over-reporting is
				// noise; under-reporting hides a loss.
				suppliedReason = ReasonValueChanged
			}
		}
	}

	if refReason == "" && suppliedReason == "" {
		return nil
	}
	// Prefer the reference detector's reason — it can tell an overflow wrap
	// from a decimal truncation, where supplied-vs-stored can only say
	// "numeric" — but never let it downgrade a lossy finding to `reformat`.
	reason := refReason
	if reason == "" || (reason == ReasonReformat && suppliedReason != "") {
		reason = suppliedReason
	}
	// An unclassified numeric/temporal leaf (no reference-ladder entry in the
	// C layer): the precise detector never ran, so a visible change cannot be
	// vouched non-lossy. Claim lossy rather than hide a possible loss.
	if c.RefUnclassified && reason == ReasonReformat {
		reason = ReasonValueChanged
	}
	return []Transform{{Column: c.Name, Input: c.Input, Stored: stored, Reason: reason}}
}

// ------------------------------------------------------ supplied vs stored

// parseJSONValue decodes exactly one JSON value, keeping numbers exact.
//
// docs/reference/bindings.md §detectors: the text is a JSON value only if a strict
// parse consumes ALL of it, with whitespace being exactly JSON's four.
// Two measured defects forced this precision (2026-08-17, 43 differential
// cases): the old trailing-bytes check decoded a SECOND value and only
// refused when that succeeded, so "1.2.3.4" passed as 1.2 (".3.4" is not
// valid JSON, so the second decode "helpfully" failed); and
// strings.TrimSpace admits NBSP and friends, which JSON does not.
func parseJSONValue(s string) (any, bool) {
	s = strings.Trim(s, " \t\n\r")
	if s == "" {
		return nil, false
	}
	dec := json.NewDecoder(strings.NewReader(s))
	dec.UseNumber()
	var v any
	if err := dec.Decode(&v); err != nil {
		return nil, false
	}
	// Everything after the value must be JSON whitespace and nothing else —
	// trailing bytes mean this was not a single JSON value (a TSV field, say).
	if strings.Trim(s[dec.InputOffset():], " \t\n\r") != "" {
		return nil, false
	}
	return v, true
}

// sameValue: equal after canonicalization, where a number and its decimal
// string spelling are the same value (5 vs "5") but a float that has thrown
// away 60 digits of an Int256 is not. Mirrors the arbiter's `_same_value` so
// this library's reasons and the bake-off's classes describe the same thing.
func sameValue(a, b any) bool {
	switch av := a.(type) {
	case map[string]any:
		bv, ok := b.(map[string]any)
		if !ok || len(av) != len(bv) {
			return false
		}
		for k, x := range av {
			y, ok := bv[k]
			if !ok || !sameValue(x, y) {
				return false
			}
		}
		return true
	case []any:
		bv, ok := b.([]any)
		if !ok || len(av) != len(bv) {
			return false
		}
		for i := range av {
			if !sameValue(av[i], bv[i]) {
				return false
			}
		}
		return true
	}
	if a == nil || b == nil {
		return a == nil && b == nil
	}
	if ab, ok := a.(bool); ok {
		bb, ok2 := b.(bool)
		return ok2 && ab == bb
	}
	if _, ok := b.(bool); ok {
		return false
	}
	as, aIsStr := scalarText(a)
	bs, bIsStr := scalarText(b)
	if as == bs && aIsStr == bIsStr {
		return true
	}
	// Denormals: ClickHouse renders them as the strings "nan" / "inf" / "-inf".
	if denormal(as) != "" || denormal(bs) != "" {
		return denormal(as) == denormal(bs)
	}
	ra, oka := new(big.Rat).SetString(as)
	rb, okb := new(big.Rat).SetString(bs)
	return oka && okb && ra.Cmp(rb) == 0
}

func scalarText(v any) (string, bool) {
	switch t := v.(type) {
	case json.Number:
		return t.String(), false
	case string:
		return t, true
	case float64:
		return new(big.Float).SetFloat64(t).Text('g', -1), false
	}
	return "", false
}

func denormal(s string) string {
	switch strings.ToLower(strings.TrimSpace(s)) {
	case "nan", "__nan__", "-nan":
		return "nan"
	case "inf", "infinity", "__inf__", "+inf":
		return "inf"
	case "-inf", "-infinity", "__-inf__":
		return "-inf"
	}
	return ""
}

func isNumericValue(v any) bool {
	s, _ := scalarText(v)
	if s == "" {
		return false
	}
	if denormal(s) != "" {
		return true
	}
	_, ok := new(big.Rat).SetString(s)
	return ok
}

// severity mirrors the arbiter's `_severity`.
func severity(a, b any) string {
	if isNumericValue(a) && isNumericValue(b) {
		return ReasonLossyNumeric
	}
	if a == nil && b != nil {
		return ReasonNullToDefault // the arbiter's `null_filled`
	}
	if a != nil && b == nil {
		return ReasonNullLoss
	}
	as, aStr := scalarText(a)
	bs, bStr := scalarText(b)
	if aStr && bStr {
		if dateish(as) && dateish(bs) {
			if as[:10] != bs[:10] {
				return ReasonDateShift
			}
			return ReasonReformat
		}
		if as != "" && bs == "" {
			return ReasonEmptied
		}
		return ReasonReformat
	}
	if bStr && denormal(bs) != "" {
		return ReasonLossyNumeric
	}
	return ReasonReformat
}

// sameKindDeep compares JSON shape recursively. Two values can be equal by
// sameValue and still differ in type somewhere inside — `[{"a": 1}]` stored as
// `[{"a": "1"}]` is the same data with a different wire type, which an SSE
// subscriber reading the preview would see and the table would not have.
func sameKindDeep(a, b any) bool {
	if jsonKind(a) != jsonKind(b) {
		return false
	}
	// Two strings that are "equal" only after denormal folding are still a
	// visible change: the row said "NaN" and the table holds "nan". Compare
	// string scalars by text, numbers by value.
	if as, ok := a.(string); ok {
		bs, _ := b.(string)
		return as == bs
	}
	switch av := a.(type) {
	case map[string]any:
		bv := b.(map[string]any)
		if len(av) != len(bv) {
			return false
		}
		for k, x := range av {
			y, ok := bv[k]
			if !ok || !sameKindDeep(x, y) {
				return false
			}
		}
	case []any:
		bv := b.([]any)
		if len(av) != len(bv) {
			return false
		}
		for i := range av {
			if !sameKindDeep(av[i], bv[i]) {
				return false
			}
		}
	}
	return true
}

// jsonKind names the JSON type of a decoded value, so a change of spelling
// between two equal values is still visible.
func jsonKind(v any) string {
	switch v.(type) {
	case nil:
		return "null"
	case bool:
		return "bool"
	case json.Number:
		return "number"
	case float64:
		return "number"
	case string:
		return "string"
	case []any:
		return "array"
	case map[string]any:
		return "object"
	}
	return "?"
}

func dateish(s string) bool {
	if len(s) < 10 {
		return false
	}
	for i := 0; i < 10; i++ {
		c := s[i]
		if i == 4 || i == 7 {
			if c != '-' {
				return false
			}
		} else if c < '0' || c > '9' {
			return false
		}
	}
	return true
}

// ---------------------------------------------------------- reference type

func reasonFor(base, stored, ref string) string {
	switch {
	case strings.HasPrefix(base, "Enum"):
		return ReasonEnumCoerce
	case strings.HasPrefix(base, "Array"), strings.HasPrefix(base, "Tuple"),
		strings.HasPrefix(base, "Map"):
		return ReasonElementChanged
	case strings.HasPrefix(base, "Decimal"):
		if strings.HasPrefix(trimNum(ref), trimNum(stored)) {
			return ReasonDecimalTruncate
		}
		return ReasonOverflowWrap
	case strings.HasPrefix(base, "DateTime"):
		return ReasonDateTimeWrap
	case strings.HasPrefix(base, "Date"):
		return ReasonDateClamp
	// Time / Time64 (25.8+): the same truncation mechanism as DateTime64 —
	// sub-second ticks at the declared scale, whole seconds in Time.
	case strings.HasPrefix(base, "Time"):
		return ReasonDateTimeWrap
	case base == "UUID":
		return ReasonUUIDMangle
	case base == "IPv4", base == "IPv6":
		return ReasonIPMangle
	case base == "Float32", base == "Float64", base == "BFloat16":
		return ReasonFloatPrecision
	case strings.HasPrefix(base, "FixedString"):
		return ReasonStringPad
	case isIntFamily(base):
		return ReasonOverflowWrap
	}
	return ReasonValueChanged
}

func isIntFamily(base string) bool {
	return strings.HasPrefix(base, "UInt") || strings.HasPrefix(base, "Int") || base == "Bool"
}

// equivalent decides whether two ClickHouse renderings mean the same value.
// Only formatting differences are collapsed — anything else is a real change.
func equivalent(base, stored, ref string) bool {
	if stored == ref {
		return true
	}
	// Bool renders as true/false; its Int256 reference renders as 1/0.
	if base == "Bool" {
		return boolNorm(stored) == boolNorm(ref)
	}
	// DateTime64 references widen the scale: ".123" vs ".123000000" — and so
	// do Time64's (Time/Time64 arrived in 25.8; same rendering shape).
	if strings.HasPrefix(base, "DateTime") || strings.HasPrefix(base, "Time") {
		return trimFrac(stored) == trimFrac(ref)
	}
	// Decimal references widen the scale: "2.50" vs "2.5000000".
	if strings.HasPrefix(base, "Decimal") || isIntFamily(base) ||
		base == "Float32" || base == "Float64" || base == "BFloat16" {
		return numEqual(stored, ref)
	}
	if strings.HasPrefix(base, "Array") || strings.HasPrefix(base, "Tuple") ||
		strings.HasPrefix(base, "Map") {
		return compactJSON(stored) == compactJSON(ref) || numListEqual(stored, ref)
	}
	return false
}

func boolNorm(s string) string {
	switch strings.Trim(s, `"`) {
	case "true", "1":
		return "1"
	case "false", "0":
		return "0"
	}
	return s
}

// trimFrac drops trailing zeros (and a bare dot) from a datetime's fractional
// part so DateTime64(3) and its DateTime64(9) reference compare equal.
func trimFrac(s string) string {
	i := strings.LastIndexByte(s, '.')
	if i < 0 {
		return s
	}
	end := len(s)
	quoted := end > 0 && s[end-1] == '"'
	if quoted {
		end--
	}
	frac := strings.TrimRight(s[i+1:end], "0")
	out := s[:i]
	if frac != "" {
		out += "." + frac
	}
	if quoted {
		out += `"`
	}
	return out
}

func trimNum(s string) string { return strings.Trim(s, `"`) }

// numEqual compares two numeric renderings exactly, using big.Rat so that
// Decimal(76,24)'s extra digits and 64-bit-integer quoting never matter.
func numEqual(a, b string) bool {
	a, b = trimNum(a), trimNum(b)
	if a == b {
		return true
	}
	if denormal(a) != "" || denormal(b) != "" {
		return denormal(a) == denormal(b)
	}
	ra, oka := new(big.Rat).SetString(a)
	rb, okb := new(big.Rat).SetString(b)
	if !oka || !okb {
		return false
	}
	return ra.Cmp(rb) == 0
}

// numListEqual compares two rendered containers element-wise as numbers, so
// [1,2] and [1.000,2.000] agree while [1,0,3] and [1,256,3] do not.
func numListEqual(a, b string) bool {
	ta, tb := tokenizeNums(a), tokenizeNums(b)
	if len(ta) != len(tb) || len(ta) == 0 {
		return false
	}
	for i := range ta {
		if !numEqual(ta[i], tb[i]) {
			return false
		}
	}
	return skeleton(a) == skeleton(b)
}

// tokenizeNums pulls the numeric literals out of a rendered container.
func tokenizeNums(s string) []string {
	var out []string
	i := 0
	for i < len(s) {
		c := s[i]
		if c == '"' { // skip strings wholesale
			i++
			for i < len(s) && s[i] != '"' {
				if s[i] == '\\' {
					i++
				}
				i++
			}
			i++
			continue
		}
		if c == '-' || c == '+' || (c >= '0' && c <= '9') {
			j := i + 1
			for j < len(s) && (s[j] == '.' || s[j] == 'e' || s[j] == 'E' || s[j] == '-' ||
				s[j] == '+' || (s[j] >= '0' && s[j] <= '9')) {
				j++
			}
			out = append(out, s[i:j])
			i = j
			continue
		}
		i++
	}
	return out
}

// skeleton keeps only structural punctuation, so two renderings that agree
// numerically but differ in shape are still reported as different.
func skeleton(s string) string {
	var b strings.Builder
	inStr := false
	for i := 0; i < len(s); i++ {
		c := s[i]
		if inStr {
			if c == '\\' {
				i++
			} else if c == '"' {
				inStr = false
				b.WriteByte('"')
			}
			continue
		}
		switch c {
		case '"':
			inStr = true
			b.WriteByte('"')
		case '[', ']', '{', '}', '(', ')', ',', ':':
			b.WriteByte(c)
		}
	}
	return b.String()
}

func compactJSON(s string) string {
	var b strings.Builder
	inStr := false
	for i := 0; i < len(s); i++ {
		c := s[i]
		if inStr {
			b.WriteByte(c)
			if c == '\\' && i+1 < len(s) {
				i++
				b.WriteByte(s[i])
			} else if c == '"' {
				inStr = false
			}
			continue
		}
		switch c {
		case ' ', '\t', '\n', '\r':
		case '"':
			inStr = true
			b.WriteByte(c)
		default:
			b.WriteByte(c)
		}
	}
	return b.String()
}
