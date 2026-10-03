package ocifetch

// json.go — the one duplicate-key-refusing JSON reader every other file in
// this package uses. docs/guides/fetch-v1.md §4: "A duplicate key anywhere in
// the statement's JSON is CHTYPES_ARTIFACT_CORRUPT — every binding's JSON
// parse refuses one rather than silently taking the last value." encoding/
// json's decoder silently keeps the last occurrence of a repeated key, so
// every JSON document this package trusts (statements, manifests, indexes,
// lock files, cases) is walked once to check for repeats before being
// unmarshalled normally.

import (
	"bytes"
	"encoding/json"
	"fmt"
)

// strictUnmarshal decodes data into v, refusing any object in it (at any
// nesting depth) that repeats a key. It does not reject unknown fields: the
// predicate and statement shapes intentionally tolerate fields this package
// does not read (docs/guides/fetch-v1.md §4.1's "core's firm predicate").
func strictUnmarshal(data []byte, v any) error {
	if err := checkNoDuplicateKeys(data); err != nil {
		return err
	}
	return json.Unmarshal(data, v)
}

// checkNoDuplicateKeys walks the single JSON value in data and returns an
// error naming the first repeated object key found, at any depth.
func checkNoDuplicateKeys(data []byte) error {
	dec := json.NewDecoder(bytes.NewReader(data))
	return skipValue(dec)
}

// skipValue consumes exactly one JSON value (scalar, array or object) from
// dec, recursing into objects and arrays to check every nested object for a
// repeated key.
func skipValue(dec *json.Decoder) error {
	tok, err := dec.Token()
	if err != nil {
		return err
	}
	delim, ok := tok.(json.Delim)
	if !ok {
		return nil // a scalar: string, number, bool or null
	}
	switch delim {
	case '{':
		seen := make(map[string]bool)
		for dec.More() {
			keyTok, err := dec.Token()
			if err != nil {
				return err
			}
			key, ok := keyTok.(string)
			if !ok {
				return fmt.Errorf("object key is not a string: %v", keyTok)
			}
			if seen[key] {
				return fmt.Errorf("duplicate key %q", key)
			}
			seen[key] = true
			if err := skipValue(dec); err != nil {
				return err
			}
		}
		_, err := dec.Token() // the closing '}'
		return err
	case '[':
		for dec.More() {
			if err := skipValue(dec); err != nil {
				return err
			}
		}
		_, err := dec.Token() // the closing ']'
		return err
	}
	return nil
}
