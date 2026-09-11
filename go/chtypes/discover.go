// discover.go — the discovery kit.
//
// This library NEVER talks to ClickHouse; it ships the queries. At connect
// time a caller (a gateway, WaveHouse) runs these three queries against the
// deployment's own server with whatever client it already has, feeds the
// results to the typed parsers below, and then declares what it learned:
//
//	profile.Version  -> Registry.For / the artifact to load
//	profile.Settings -> CompileDDL's WithCompileSettings (compile-time) and
//	                    per-call settings
//	columns          -> ReconstructDDL -> CompileDDL (+ WithCompileSettings)
//
// The pattern, in full (docs/reference/bindings.md §Discovery):
//
//  1. run QueryServerVersion, QueryChangedSettings once per connection
//  2. cache the ServerProfile per deployment/tenant
//  3. lib, _ := registry.For(chtypes.Version(profile.Version))
//  4. cs, _ := lib.CompileDDL(ddl, chtypes.WithCompileSettings(profile.Settings))
//  5. per-call: pass profile.Settings (plus any per-INSERT overrides) to Rows
//
// Never ask the customer for their settings — ask their server. The queries
// return exactly what the server believes, spelled the way the server spells
// it, which is what the settings gate (the core repository's C ABI specification, Settings rule 2)
// validates against.
package chtypes

import (
	"bytes"
	"encoding/json"
	"fmt"
	"strconv"
	"strings"
)

// The canonical discovery queries. Each carries its FORMAT clause so the
// bytes an HTTP client gets back are exactly what the matching Parse*
// function consumes. A native-protocol client that returns typed rows
// instead can ignore the parsers and fill the structs directly.
const (
	// QueryServerVersion names the deployment's exact release — the string
	// Registry.For resolves (minor line or exact patch both work).
	// One row: {"version":"25.8.28.1"}.
	QueryServerVersion = "SELECT version() AS version FORMAT JSONEachRow"

	// QueryChangedSettings lists every query setting the deployment runs at
	// a NON-default value — the whole declared profile, from the server
	// itself. One row per setting: {"name":"flatten_nested","value":"0"}.
	// system.settings.value is already a String; the map feeds
	// WithCompileSettings and per-call settings verbatim.
	QueryChangedSettings = "SELECT name, value FROM system.settings WHERE changed FORMAT JSONEachRow"

	// QueryTableColumns describes one existing table, in declaration order,
	// with everything a column-declaration list needs — INCLUDING
	// default_kind and default_expression, without which a reconstructed
	// schema silently loses its DEFAULT/MATERIALIZED semantics. Uses
	// ClickHouse's own query parameters: send param_db / param_table (HTTP)
	// or bind {db}/{table} (native).
	QueryTableColumns = "SELECT name, type, default_kind, default_expression, position " +
		"FROM system.columns WHERE database = {db:String} AND table = {table:String} " +
		"ORDER BY position FORMAT JSONEachRow"
)

// ServerProfile is what discovery learns about one deployment: the exact
// release and the settings it runs changed from defaults. Cache one per
// deployment (or per tenant on bring-your-own-ClickHouse) and declare it.
type ServerProfile struct {
	Version  string            // e.g. "25.8.28.1" — feed Registry.For
	Settings map[string]string // changed settings — feed WithCompileSettings / per-call
}

// DiscoveredColumn is one row of QueryTableColumns.
type DiscoveredColumn struct {
	Name              string
	Type              string
	DefaultKind       string // "" | "DEFAULT" | "MATERIALIZED" | "ALIAS" | "EPHEMERAL"
	DefaultExpression string
	Position          uint64
}

// jsonEachRowDocs splits a JSONEachRow body into one raw object per line.
func jsonEachRowDocs(body []byte) ([][]byte, error) {
	var out [][]byte
	for _, line := range bytes.Split(body, []byte("\n")) {
		line = bytes.TrimSpace(line)
		if len(line) == 0 {
			continue
		}
		if line[0] != '{' {
			return nil, fmt.Errorf("chtypes: not a JSONEachRow line: %.60s", line)
		}
		out = append(out, line)
	}
	return out, nil
}

// jsonString decodes a JSON value that may arrive quoted or bare (ClickHouse
// quotes 64-bit integers in JSON output by default), preserving digits
// exactly — never through a float.
func jsonString(raw json.RawMessage) string {
	var s string
	if err := json.Unmarshal(raw, &s); err == nil {
		return s
	}
	return string(bytes.TrimSpace(raw))
}

// ParseVersionResult reads QueryServerVersion's JSONEachRow body.
func ParseVersionResult(body []byte) (string, error) {
	docs, err := jsonEachRowDocs(body)
	if err != nil {
		return "", err
	}
	if len(docs) != 1 {
		return "", fmt.Errorf("chtypes: version query returned %d rows, want 1", len(docs))
	}
	var row map[string]json.RawMessage
	if err := json.Unmarshal(docs[0], &row); err != nil {
		return "", err
	}
	v, ok := row["version"]
	if !ok {
		return "", fmt.Errorf("chtypes: version query row has no `version` field")
	}
	out := jsonString(v)
	if out == "" {
		return "", fmt.Errorf("chtypes: version query returned an empty version")
	}
	return out, nil
}

// ParseChangedSettingsResult reads QueryChangedSettings' JSONEachRow body.
// An empty body is a stock server: an empty (non-nil) map. A duplicated name
// resolves LAST-WRITE-WINS — the later row replaces the earlier — which is
// the spec rule (docs/reference/bindings.md §Discovery), asserted by every SDK so one
// server answer can never discover two different profiles.
func ParseChangedSettingsResult(body []byte) (map[string]string, error) {
	docs, err := jsonEachRowDocs(body)
	if err != nil {
		return nil, err
	}
	out := make(map[string]string, len(docs))
	for _, d := range docs {
		var row map[string]json.RawMessage
		if err := json.Unmarshal(d, &row); err != nil {
			return nil, err
		}
		name, ok := row["name"]
		if !ok {
			return nil, fmt.Errorf("chtypes: settings row has no `name` field: %.60s", d)
		}
		value, ok := row["value"]
		if !ok {
			return nil, fmt.Errorf("chtypes: settings row has no `value` field: %.60s", d)
		}
		out[jsonString(name)] = jsonString(value)
	}
	return out, nil
}

// ParseColumnsResult reads QueryTableColumns' JSONEachRow body, in the
// query's ORDER BY position order.
func ParseColumnsResult(body []byte) ([]DiscoveredColumn, error) {
	docs, err := jsonEachRowDocs(body)
	if err != nil {
		return nil, err
	}
	var out []DiscoveredColumn
	for _, d := range docs {
		var row map[string]json.RawMessage
		if err := json.Unmarshal(d, &row); err != nil {
			return nil, err
		}
		col := DiscoveredColumn{
			Name:              jsonString(row["name"]),
			Type:              jsonString(row["type"]),
			DefaultKind:       jsonString(row["default_kind"]),
			DefaultExpression: jsonString(row["default_expression"]),
		}
		if col.Name == "" || col.Type == "" {
			return nil, fmt.Errorf("chtypes: columns row missing name/type: %.80s", d)
		}
		if raw, ok := row["position"]; ok {
			if n, perr := strconv.ParseUint(jsonString(raw), 10, 64); perr == nil {
				col.Position = n
			}
		}
		out = append(out, col)
	}
	if len(out) == 0 {
		return nil, fmt.Errorf("chtypes: columns query returned no rows — wrong database/table, or no access")
	}
	return out, nil
}

// QuoteIdentifier spells an identifier the way ClickHouse DDL requires:
// plain [A-Za-z_][A-Za-z0-9_]* stays bare, anything else — the flattened
// Nested idiom (`n.a`), spaces, keywords by accident, a leading digit, an
// empty name — is backticked, with backticks DOUBLED (`a“b`). ClickHouse's
// own parser accepts both the doubled and the backslash spelling (measured
// through chs_schema_compile: both round-trip to the same column name);
// doubling is the one every peer SDK's reconstruction uses, so it is the one
// exported spelling.
//
// Exported deliberately: this package used to keep TWO private copies with
// different escapes (ParseSchema's backslash `\x60` vs discovery's doubling),
// and at least one consumer re-implemented the helper because neither was
// public. This is the single identifier-quoting function; ParseSchema and
// ReconstructDDL both use it.
func QuoteIdentifier(name string) string {
	plain := name != "" && !(name[0] >= '0' && name[0] <= '9')
	for i := 0; plain && i < len(name); i++ {
		c := name[i]
		if !(c == '_' || c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9') {
			plain = false
		}
	}
	if plain {
		return name
	}
	return "`" + strings.ReplaceAll(name, "`", "``") + "`"
}

// ReconstructDDL turns QueryTableColumns' rows back into the
// column-declaration list CompileDDL takes. It is a spelling exercise, not a
// semantic one: types and expressions are the server's own text, passed
// through verbatim.
//
// Two facts a caller must know, both properties of the server rather than of
// this function:
//
//   - system.columns reports the table AS STORED. Under the default
//     flatten_nested=1 a Nested(a,b) column appears as its flattened
//     `n.a`/`n.b` Array columns and reconstructs to exactly those — which is
//     the same table. Under flatten_nested=0 it appears as one column of
//     type Nested(...), which also reconstructs directly. Reconstruction is
//     therefore shape-faithful either way, PROVIDED the same flatten_nested
//     value is declared via CompileDDL's WithCompileSettings that the table
//     was created under — which is what the discovered ServerProfile.Settings
//     carries.
//   - a MATERIALIZED/ALIAS column reconstructs with its expression; an
//     EPHEMERAL column may legitimately have an empty default_expression.
func ReconstructDDL(cols []DiscoveredColumn) (string, error) {
	if len(cols) == 0 {
		return "", fmt.Errorf("chtypes: no columns to reconstruct")
	}
	var b strings.Builder
	for i, c := range cols {
		if c.Name == "" || c.Type == "" {
			return "", fmt.Errorf("chtypes: column %d has no name/type", i)
		}
		if i > 0 {
			b.WriteString(", ")
		}
		b.WriteString(QuoteIdentifier(c.Name))
		b.WriteByte(' ')
		b.WriteString(c.Type)
		switch c.DefaultKind {
		case "":
			if c.DefaultExpression != "" {
				return "", fmt.Errorf("chtypes: column %s has a default_expression but no default_kind", c.Name)
			}
		case "DEFAULT", "MATERIALIZED", "ALIAS":
			if c.DefaultExpression == "" {
				return "", fmt.Errorf("chtypes: column %s is %s but has no default_expression", c.Name, c.DefaultKind)
			}
			b.WriteByte(' ')
			b.WriteString(c.DefaultKind)
			b.WriteByte(' ')
			b.WriteString(c.DefaultExpression)
		case "EPHEMERAL":
			b.WriteString(" EPHEMERAL")
			if c.DefaultExpression != "" {
				b.WriteByte(' ')
				b.WriteString(c.DefaultExpression)
			}
		default:
			return "", fmt.Errorf("chtypes: column %s has unknown default_kind %q", c.Name, c.DefaultKind)
		}
	}
	return b.String(), nil
}

// DefaultRegistryDir is the per-user artifact cache for this host:
// ${XDG_CACHE_HOME:-~/.cache}/chtypes/artifacts/<os>-<arch>, with <arch>
// spelled the artifact way (amd64, arm64). It is where scripts/fetch.sh and
// the in-package fetch (Ensure, `chtypes fetch`) install, where a
// core-repository build lands, and what every SDK's tests and playgrounds
// fall back to when CHTYPES_REGISTRY is unset — one directory the four SDKs
// agree on, so a machine set up once serves all of them. It is item 3 of the
// docs/guides/fetch.md §1 search path (RegistrySearchPath is the whole list).
// It is a PATH, not a promise: NewRegistry still errors if nothing is there.
func DefaultRegistryDir() string {
	return DefaultRegistryDirFor(HostPlatform())
}
