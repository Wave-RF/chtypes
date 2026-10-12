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

// stringMapJSON serializes a map of strings as a JSON object, or returns nil
// for an empty map (the ABI reads length 0 as "none"). A key or value that is
// not valid UTF-8 is a UsageError naming what (the input) and the key: stock
// encoding/json would replace it with U+FFFD, and the library would then see a
// different value with no error raised.
func stringMapJSON(what string, m map[string]string) ([]byte, error) {
	if len(m) == 0 {
		return nil, nil
	}
	if err := checkUTF8Map(what, m); err != nil {
		return nil, err
	}
	return marshalNoEscape(m)
}

// nonUTF8Hint is the tail of every refusal: a JSON input cannot carry the
// bytes in this ABI generation (spec/abi-v2/docs.md, "Input values").
const nonUTF8Hint = "this ABI generation cannot carry a non-UTF-8 value in a JSON input; write the bytes as unhex('<hex>') in the expression instead"

// checkUTF8Map refuses the first key (in sorted order, so the message does not
// depend on map iteration) or value that is not valid UTF-8. The key appears
// in the message escaped, never as raw bytes.
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
	if err := checkUTF8Map("settings", settings); err != nil {
		return nil, err
	}
	if !utf8.ValidString(*timezone) {
		return nil, usageError("the zone: the value is not valid UTF-8, and %s", nonUTF8Hint)
	}
	merged := make(map[string]string, len(settings)+1)
	for k, v := range settings {
		merged[k] = v
	}
	merged[sessionTimezoneKey] = *timezone
	return stringMapJSON("settings", merged)
}

// columnsJSON serializes the INSERT column list as a JSON array of name
// objects: "name" for a valid UTF-8 name, "name_b64" (standard base64, with
// padding) for any other bytes.
func columnsJSON(names []string, have bool) ([]byte, error) {
	if !have {
		return nil, nil
	}
	list := make([]map[string]string, 0, len(names))
	for _, n := range names {
		if utf8.ValidString(n) {
			list = append(list, map[string]string{"name": n})
		} else {
			list = append(list, map[string]string{"name_b64": base64.StdEncoding.EncodeToString([]byte(n))})
		}
	}
	return marshalNoEscape(list)
}
