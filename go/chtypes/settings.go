package chtypes

// settings.go — the serialization of the call inputs that are not bytes:
// settings and query parameters (a JSON object of strings), the per-call zone
// (one key of the settings), and the INSERT column list (a JSON array of
// names, bindings-v1.md section 5 rule 5). Stock encoding/json throughout.

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"sort"
	"strconv"
	"sync"
	"unicode/utf8"
)

// sessionTimezoneKey is the settings key that carries the per-call zone.
const sessionTimezoneKey = "session_timezone"

// stringMapJSON serializes a map of strings as a JSON object of byte values
// (the content json_object_byte_values), or returns nil for an empty map (the
// ABI reads length 0 as "none"). A value that is valid UTF-8 is a JSON string,
// and any other value is the object {"b64": "<standard base64>"} (spec/abi-v2/docs.md,
// "Input values"), so no value is ever rewritten. A key that is not valid UTF-8
// is a UsageError naming what (the input) and the key: a JSON object cannot
// carry a key's bytes, and stock encoding/json would replace it with U+FFFD, so
// the library would see a different key with no error raised.
func stringMapJSON(what string, m map[string]string) ([]byte, error) {
	if len(m) == 0 {
		return nil, nil
	}
	doc, err := byteValues(what, m)
	if err != nil {
		return nil, err
	}
	return marshalNoEscape(doc)
}

// b64Value is the object form of a value whose bytes are not valid UTF-8.
type b64Value struct {
	B64 string `json:"b64"`
}

// byteValue is one value of json_object_byte_values: the string itself when it is
// valid UTF-8, else the object form.
func byteValue(v string) any {
	if utf8.ValidString(v) {
		return v
	}
	return b64Value{B64: base64.StdEncoding.EncodeToString([]byte(v))}
}

// byteValues is the document of a map of byte values; it refuses a key that is
// not valid UTF-8, the first in sorted order so the message does not depend on
// map iteration.
func byteValues(what string, m map[string]string) (map[string]any, error) {
	if err := checkUTF8Keys(what, m); err != nil {
		return nil, err
	}
	out := make(map[string]any, len(m))
	for k, v := range m {
		out[k] = byteValue(v)
	}
	return out, nil
}

// nonUTF8Hint is the tail of every refusal: a JSON input cannot carry these
// bytes (spec/abi-v2/docs.md, "Input values").
const nonUTF8Hint = "a JSON input cannot carry these bytes here (only a settings or query-parameter value has the {\"b64\": ...} object form)"

// checkUTF8Keys refuses the first key (in sorted order) that is not valid
// UTF-8. The key appears in the message escaped, never as raw bytes.
func checkUTF8Keys(what string, m map[string]string) error {
	var bad []string
	for k := range m {
		if !utf8.ValidString(k) {
			bad = append(bad, k)
		}
	}
	if len(bad) == 0 {
		return nil
	}
	sort.Strings(bad)
	return usageError("%s: the key %s is not valid UTF-8, and %s", what, strconv.QuoteToASCII(bad[0]), nonUTF8Hint)
}

// checkUTF8Map refuses the first key or value (in sorted order) that is not
// valid UTF-8, for an input with no object form for a value (a server's macros).
func checkUTF8Map(what string, m map[string]string) error {
	var bad []string
	for k, v := range m {
		if !utf8.ValidString(k) || !utf8.ValidString(v) {
			bad = append(bad, k)
		}
	}
	if len(bad) == 0 {
		return nil
	}
	sort.Strings(bad)
	k := bad[0]
	if !utf8.ValidString(k) {
		return usageError("%s: the key %s is not valid UTF-8, and %s", what, strconv.QuoteToASCII(k), nonUTF8Hint)
	}
	return usageError("%s: the value for key %s is not valid UTF-8, and %s", what, strconv.QuoteToASCII(k), nonUTF8Hint)
}

// jsonEncoder is a stock encoder with HTML escaping off, writing into its own
// buffer, kept between calls so a call builds neither.
type jsonEncoder struct {
	buf bytes.Buffer
	enc *json.Encoder
}

var jsonEncoders sync.Pool

func marshalNoEscape(v any) ([]byte, error) {
	e, _ := jsonEncoders.Get().(*jsonEncoder)
	if e == nil {
		e = &jsonEncoder{}
		e.enc = json.NewEncoder(&e.buf)
		e.enc.SetEscapeHTML(false)
	}
	e.buf.Reset()
	if err := e.enc.Encode(v); err != nil {
		jsonEncoders.Put(e)
		return nil, internalError("encoding a call input: %v", err)
	}
	// The result is the caller's: copy it out of the buffer the encoder keeps.
	out := bytes.Clone(bytes.TrimSuffix(e.buf.Bytes(), []byte("\n")))
	jsonEncoders.Put(e)
	return out, nil
}

// callSettings is a call's settings JSON: the settings map, with the per-call
// zone written into it as the one session_timezone key, verbatim. Passing the
// zone together with a settings key of the same name is a UsageError, raised
// before any call, whether or not the two agree.
func callSettings(settings map[string]string, timezone *string) ([]byte, error) {
	if timezone == nil {
		return stringMapJSON("settings", settings)
	}
	if _, dup := settings[sessionTimezoneKey]; dup {
		return nil, usageError("the zone is given twice: a session timezone option and a %s settings key", sessionTimezoneKey)
	}
	if err := checkUTF8Keys("settings", settings); err != nil {
		return nil, err
	}
	merged := make(map[string]string, len(settings)+1)
	for k, v := range settings {
		merged[k] = v
	}
	merged[sessionTimezoneKey] = *timezone
	return stringMapJSON("settings", merged)
}

// nameObjects is a list of names as name objects: "name" for a valid UTF-8
// name, "name_b64" (standard base64, with padding) for any other bytes.
func nameObjects(names []string) []map[string]string {
	list := make([]map[string]string, 0, len(names))
	for _, n := range names {
		if utf8.ValidString(n) {
			list = append(list, map[string]string{"name": n})
		} else {
			list = append(list, map[string]string{"name_b64": base64.StdEncoding.EncodeToString([]byte(n))})
		}
	}
	return list
}

// columnsJSON serializes the INSERT column list as a JSON array of name
// objects (nameObjects).
func columnsJSON(names []string, have bool) ([]byte, error) {
	if !have {
		return nil, nil
	}
	return marshalNoEscape(nameObjects(names))
}

// optionDoc is an options document under construction: the members a caller
// set, by key. An option the binding has no name for is set with its raw JSON
// value and passed through as given (rule r1: the library refuses a key it does
// not know, naming it; the binding never drops or rewrites one). Setting a key
// twice keeps the last value.
type optionDoc map[string]any

func (d *optionDoc) set(key string, v any) {
	if *d == nil {
		*d = optionDoc{}
	}
	(*d)[key] = v
}

// json is the document, or nil when no member was set (length 0 is "none", the
// document the call took before it had one).
func (d optionDoc) json(what string) ([]byte, error) {
	if len(d) == 0 {
		return nil, nil
	}
	for k, v := range d {
		if raw, ok := v.(rawMember); ok && !json.Valid(raw) {
			return nil, usageError("%s: the value for key %s is not valid JSON", what, strconv.QuoteToASCII(k))
		}
		if !utf8.ValidString(k) {
			return nil, usageError("%s: the key %s is not valid UTF-8, and %s", what, strconv.QuoteToASCII(k), nonUTF8Hint)
		}
	}
	return marshalNoEscape(map[string]any(d))
}

// rawMember is a member's value given as JSON text, passed through verbatim.
type rawMember json.RawMessage

func (r rawMember) MarshalJSON() ([]byte, error) { return []byte(r), nil }
