// Command ingest-demo is a miniature of a production ingest worker, wired to
// chtypes instead of to hand-written type code.
//
// It walks the whole connect-time-to-publish path against a REAL ClickHouse:
//
//  1. discovery   — the three canonical queries, run over plain HTTP
//  2. reconstruct — system.columns rows -> a column-declaration list
//  3. registry    — pick the artifact matching the server's own version
//  4. compile     — CompileDDL under the deployment's declared profile
//  5. rows        — a batch through Rows(), and the publish decision per row
//
// Nothing here talks to ClickHouse through chtypes. chtypes never opens a
// socket; it ships the queries and parses their results. The HTTP client below
// is standing in for whatever HTTP client your service already has.
//
// Run it:
//
//	docker run -d --name chguide-ch --label com.docker.compose.project=chguide \
//	    -e CLICKHOUSE_PASSWORD=chguide clickhouse/clickhouse-server:25.8
//	CH_ADDR=http://<container-ip>:8123 go run ./ingest-demo
//
// See README.md in this directory.
package main

import (
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strings"

	"github.com/wave-rf/chtypes/go/chtypes"
)

// ---------------------------------------------------------------- the tenant

// The table this demo discovers. A real worker would discover a tenant's
// existing table; we create one first so the demo is self-contained.
const (
	demoDB    = "chguide_demo"
	demoTable = "events"

	createDB = `CREATE DATABASE IF NOT EXISTS ` + demoDB

	createTable = `CREATE TABLE IF NOT EXISTS ` + demoDB + `.` + demoTable + ` (
    ts           DateTime64(3) DEFAULT now64(3),
    device_id    UInt32,
    rssi         Int8,
    seq          UInt8 DEFAULT 0,
    payload      String,
    payload_len  UInt32 MATERIALIZED length(payload)
) ENGINE = MergeTree ORDER BY (device_id, ts)`
)

// The batch. Each row is shaped to exercise one publish decision an ingest
// worker has to make; the comment is what the worker would do with it.
var batch = []struct {
	json string
	want string
}{
	{`{"ts":"2026-08-26 12:00:00.000","device_id":42,"rssi":-70,"seq":7,"payload":"ok"}`,
		"clean accept — publish the coerced values"},
	{`{"ts":"2026-08-26 12:00:01.000","device_id":42,"rssi":-70,"seq":256,"payload":"wrapped"}`,
		"accepted but SILENTLY CHANGED — publish stored, not payload"},
	{`{"ts":"2026-08-26 12:00:02.000","device_id":"not-a-number","rssi":-70,"seq":1,"payload":"bad"}`,
		"rejected — 400 to the producer, publish nothing"},
	{`{"device_id":43,"rssi":-55,"payload":"defaulted"}`,
		"volatile DEFAULT substituted — must be sent explicitly"},
	{`{"ts":"2026-08-26 12:00:04.000","device_id":44,"rssi":-60,"seq":2,"payload":"x","extra":1}`,
		"unknown field — depends on input_format_skip_unknown_fields"},
}

func main() {
	if err := run(); err != nil {
		fmt.Fprintf(os.Stderr, "\nFAILED: %v\n", err)
		os.Exit(1)
	}
}

func run() error {
	ch := newClient()

	// The table has to exist before discovery can find it.
	for _, q := range []string{createDB, createTable} {
		if _, err := ch.exec(q, nil); err != nil {
			return fmt.Errorf("setup: %w", err)
		}
	}

	// ---------------------------------------------------------- 1. discovery
	//
	// Three queries, run with the caller's own client. chtypes supplies the
	// SQL text and the parsers; it never connects.
	section("1. Connect-time discovery")

	verBody, err := ch.exec(chtypes.QueryServerVersion, nil)
	if err != nil {
		return fmt.Errorf("version query: %w", err)
	}
	version, err := chtypes.ParseVersionResult(verBody)
	if err != nil {
		return err
	}

	setBody, err := ch.exec(chtypes.QueryChangedSettings, nil)
	if err != nil {
		return fmt.Errorf("settings query: %w", err)
	}
	settings, err := chtypes.ParseChangedSettingsResult(setBody)
	if err != nil {
		return err
	}

	colBody, err := ch.exec(chtypes.QueryTableColumns, map[string]string{
		"param_db":    demoDB,
		"param_table": demoTable,
	})
	if err != nil {
		return fmt.Errorf("columns query: %w", err)
	}
	cols, err := chtypes.ParseColumnsResult(colBody)
	if err != nil {
		return err
	}

	profile := chtypes.ServerProfile{Version: version, Settings: settings}
	kv("server version", profile.Version)
	kv("changed settings", fmt.Sprintf("%d %v", len(profile.Settings), profile.Settings))
	kv("columns", fmt.Sprintf("%d", len(cols)))
	for _, c := range cols {
		detail := c.Type
		if c.DefaultKind != "" {
			detail += "  " + c.DefaultKind + " " + c.DefaultExpression
		}
		fmt.Printf("      %-12s %s\n", c.Name, detail)
	}

	// A discovery query that does not SELECT default_expression — an easy
	// column to leave out — loses the table's DEFAULT semantics silently.
	// Note how much of this table is carried by that one column.

	// ------------------------------------------------------- 2. reconstruct
	section("2. ReconstructDDL")

	ddl, err := chtypes.ReconstructDDL(cols)
	if err != nil {
		return err
	}
	for _, part := range strings.Split(ddl, ", ") {
		fmt.Printf("      %s\n", part)
	}

	// ---------------------------------------------------------- 3. registry
	section("3. Registry — artifact matching the server")

	dir := registryDir()
	reg, err := chtypes.NewRegistry(dir)
	if err != nil {
		return fmt.Errorf("registry at %s: %w", dir, err)
	}
	kv("registry dir", dir)
	kv("artifacts", strings.Join(reg.Versions(), " "))

	lib, err := reg.For(chtypes.Version(profile.Version))
	if err != nil {
		return fmt.Errorf("no artifact for server %s: %w", profile.Version, err)
	}
	kv("resolved", fmt.Sprintf("%s (minor %s)", lib.Version, lib.Minor))

	// ----------------------------------------------------------- 4. compile
	section("4. CompileDDL under the declared profile")

	schema, err := lib.CompileDDL(ddl, chtypes.WithCompileSettings(profile.Settings))
	if err != nil {
		return fmt.Errorf("compile: %w", err)
	}
	defer schema.Close()
	kv("compiled", fmt.Sprintf("%d columns", len(schema.Columns)))
	for i, c := range schema.Columns {
		fmt.Printf("      %-12s %-24s %s\n", c.Name, schema.Canonical[i], c.DefaultKind)
	}

	// ----------------------------------------------------- 5a. per-row admission
	//
	// This is the shape a per-record ingest path already has: one verdict per
	// record. Row() gives every record its own answer, so one poisonous record
	// cannot cost the others.
	section("5a. Per-row admission — what the worker would publish")

	for i, r := range batch {
		row, err := schema.Row(chtypes.JSONEachRow, []byte(r.json))
		if err != nil {
			return fmt.Errorf("row %d: %w", i, err)
		}
		reportRow(i, r.want, row, row.Transformed)
	}

	// ------------------------------------------------ 5b. the same as one batch
	//
	// Rows() is the batch twin, and it is NOT the same contract: under stock
	// settings a row ClickHouse cannot parse ENDS the batch. The rows after it
	// never get a verdict. That is the server's real behaviour, and a gateway
	// that batches has to decide what to tell the producers of the rows that
	// were never looked at.
	section("5b. The same rows as one batch — one bad row ends it")

	var body strings.Builder
	for _, r := range batch {
		body.WriteString(r.json)
		body.WriteByte('\n')
	}

	res, err := schema.Rows(chtypes.JSONEachRow, []byte(body.String()), nil)
	if err != nil {
		return fmt.Errorf("rows: %w", err)
	}
	fmt.Printf("   batch outcome=%s code=%d rows_read=%d rows_skipped=%d\n",
		res.Outcome, res.ErrCode, res.RowsRead, res.RowsSkipped)
	fmt.Printf("   verdicts returned: %d of %d rows sent\n", len(res.Rows), len(batch))
	if res.ErrMsg != "" {
		fmt.Printf("   msg: %s\n", firstLine(res.ErrMsg))
	}
	fmt.Printf("\n   -> rows %d..%d were never judged. Per-row admission (5a) is the\n",
		len(res.Rows), len(batch)-1)
	fmt.Printf("      shape that matches a conventional per-record path.\n")

	// ------------------------------------------- 6. an unknown-setting refusal
	//
	// A setting NAME the server would not accept is a refusal with ClickHouse's
	// own code 115, not a chtypes decline. The whole batch fails, which is what
	// a real server does with a bad SETTINGS clause.
	section("6. Unknown setting — refusal, with ClickHouse's own code")

	bad, err := schema.Rows(chtypes.JSONEachRow,
		[]byte(batch[0].json+"\n"),
		map[string]string{"totally_not_a_setting": "1"})
	if err != nil {
		fmt.Printf("   Rows returned a Go error: %v\n", err)
	} else {
		fmt.Printf("   batch outcome=%s code=%d\n", bad.Outcome, bad.ErrCode)
		if bad.ErrMsg != "" {
			fmt.Printf("   msg: %s\n", firstLine(bad.ErrMsg))
		}
		for _, r := range bad.Rows {
			if len(r.UnsupportedSettings) > 0 {
				fmt.Printf("   unsupported_settings: %v  (a DECLINE, not a rejection)\n",
					r.UnsupportedSettings)
			}
		}
	}

	section("done")
	fmt.Println("   The rows above are what the ingest worker publishes to subscribers.")
	fmt.Println("   Note that every published value is the STORED text, not the payload text.")
	return nil
}

// reportRow prints one row the way an ingest worker would have to reason about
// it: publish or not, with what, and what the caller still owes the server.
func reportRow(i int, want string, row chtypes.RowResult, transforms []chtypes.Transform) {
	fmt.Printf("── row %d ── %s\n", i, want)
	fmt.Printf("   outcome: %s", row.Outcome)
	if row.ErrCode != 0 {
		fmt.Printf("   code=%d", row.ErrCode)
	}
	fmt.Println()

	switch row.Outcome {
	case chtypes.Rejected:
		fmt.Printf("   DROP + 400: %s\n", firstLine(row.ErrMsg))
	case chtypes.Unsupported:
		fmt.Printf("   DECLINE — do not publish, do not call it a rejection\n")
	default:
		fmt.Printf("   PUBLISH:\n")
		for _, v := range row.Values {
			text := v.Text
			if v.Null {
				text = "NULL"
			}
			marker := ""
			if v.Source != "input" {
				marker = "   <- " + v.Source
			}
			fmt.Printf("      %-12s %s%s\n", v.Column, text, marker)
		}
	}

	for _, c := range row.Computed {
		fmt.Printf("   computed (%s, durable, not in SELECT *): %s = %s\n", c.Kind, c.Column, c.Text)
	}

	// A transform is the payload-vs-stored asymmetry made visible. Publishing
	// the payload here, rather than the stored value, is the bug to avoid.
	for _, t := range transforms {
		lossy := "reversible"
		if t.Lossy() {
			lossy = "LOSSY"
		}
		fmt.Printf("   SILENT CHANGE (%s, %s): %s  %s -> %s\n",
			t.Reason, lossy, t.Column, t.Input, t.Stored)
	}

	// The caller's obligation: a volatile DEFAULT resolved here is only true if
	// the server is never asked to evaluate it.
	for _, s := range row.Substituted {
		fmt.Printf("   SUBSTITUTED %s = %s (from %s)\n", s.Column, s.Text, s.Expr)
		fmt.Printf("      -> MUST be sent as an explicit column, or stored != preview\n")
	}

	if len(row.UnknownFields) > 0 {
		fmt.Printf("   unknown fields: %v\n", row.UnknownFields)
	}
	if len(row.UnsupportedSettings) > 0 {
		fmt.Printf("   unsupported settings: %v\n", row.UnsupportedSettings)
	}
	fmt.Println()
}

// ------------------------------------------------------------- plumbing only

// client is the caller's own ClickHouse client. chtypes never sees it.
type client struct {
	addr, user, pass string
}

func newClient() *client {
	return &client{
		addr: env("CH_ADDR", "http://127.0.0.1:8123"),
		user: env("CH_USER", "default"),
		pass: env("CH_PASSWORD", "chguide"),
	}
}

func (c *client) exec(query string, params map[string]string) ([]byte, error) {
	q := url.Values{}
	q.Set("query", query)
	for k, v := range params {
		q.Set(k, v)
	}
	req, err := http.NewRequest("POST", c.addr+"/?"+q.Encode(), nil)
	if err != nil {
		return nil, err
	}
	req.SetBasicAuth(c.user, c.pass)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	b, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("HTTP %d: %s", resp.StatusCode, firstLine(string(b)))
	}
	return b, nil
}

// registryDir is $CHTYPES_REGISTRY, else the per-user artifact cache every
// SDK defaults to, the same way the other playground programs do.
func registryDir() string {
	if d := os.Getenv("CHTYPES_REGISTRY"); d != "" {
		return d
	}
	return chtypes.DefaultRegistryDir()
}

func env(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func firstLine(s string) string {
	if i := strings.IndexByte(s, '\n'); i >= 0 {
		return s[:i]
	}
	return s
}

func section(title string) { fmt.Printf("\n=== %s\n\n", title) }
func kv(k, v string)       { fmt.Printf("   %-18s %s\n", k+":", v) }
