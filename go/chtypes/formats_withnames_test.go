// Issue #119 items 1-5, for chs_format 10 (CSVWithNames) and 11
// (TSVWithNames): the round trip against a real artifact, the 26.4 / 26.5
// header-matching boundary from BOTH sides, the INSERT-column-list interplay,
// and the loud export decline. Until revision 5 was published these formats
// were declared and executed by nothing; this file executes them.
//
// Everything here is measured against a loaded artifact. Three rules shape it:
//
//   - The PROBE, never the enum and never the ABI revision, decides whether an
//     artifact knows these two values (docs/reference/bindings.md §Values a
//     binding must accept and reject). They joined enum chs_format inside
//     revision 5 after the number was set, so an artifact built from an earlier
//     revision-5 header reports 5, passes the handshake, and does not know
//     them. Every line here is asked before it is used, with a header spelled
//     exactly as the column is declared — a probe whose answer depended on
//     case-folding would not ask the same question on both sides of the
//     boundary this file measures.
//   - Every block COUNTS what it ran and fails by name on zero. A boundary
//     case that skips forever reads as a pass otherwise, which is the one
//     outcome worse than a red.
//   - Codes and verdicts are asserted; ClickHouse's message text never is.
package chtypes

import (
	"fmt"
	"strconv"
	"strings"
	"testing"
)

// The probe schema and the two probe payloads. The header spells `id` and `p`
// exactly as the DDL declares them, so the question is the same on every line.
const (
	withNamesDDL      = "id UInt8, p String"
	withNamesCSVProbe = "id,p\n1,a\n"
	withNamesTSVProbe = "id\tp\n1\ta\n"
)

// The column-list schema: two DEFAULTs, so "took its DEFAULT" and "took the
// reader's zero" are different observable values rather than both being 0.
const withNamesListDDL = "id UInt8, p UInt8 DEFAULT 7, q UInt8 DEFAULT 9"

// withNamesLine is one line of the registry: loaded, and asked whether it
// knows formats 10 and 11.
type withNamesLine struct {
	minor     string
	lib       *Library
	supported bool // the artifact ANSWERED the probe for both 10 and 11
}

// withNamesLines resolves the registry the other artifact tests use and asks
// every line it can open about formats 10 and 11.
//
// No registry, or nothing on it this build can open, is a loud SKIP by name —
// the same verdict every other artifact test here reaches; a refused ABI
// revision is abi_revision_test.go's subject, not this file's. A line that
// opens but does not know the two formats is reported and kept, because the
// export decline below needs no probe; supportedWithNamesLines is where an
// empty probe result becomes a failure.
func withNamesLines(t *testing.T) []withNamesLine {
	t.Helper()
	dir := testRegistryDir(t)
	r, err := NewRegistry(dir)
	if err != nil {
		skipNoArtifacts(t, dir, "the registry did not load: "+err.Error())
	}
	versions := r.Versions()
	if len(versions) == 0 {
		skipNoArtifacts(t, dir, "the registry discovered no versions")
	}
	var lines []withNamesLine
	opened := 0
	for _, v := range versions {
		lib, err := r.For(Version(v))
		if err != nil {
			// A mixed registry is an ordinary developer situation: an older
			// line sitting at a refused ABI revision beside current ones.
			t.Logf("line %s did not open, so it is not measured here: %v", v, err)
			continue
		}
		opened++
		ok := probeWithNames(t, v, lib)
		if !ok {
			t.Logf("line %s (ClickHouse %s) does not answer the CSVWithNames/TSVWithNames probe: "+
				"a pre-value artifact, not measured for the format cases", v, lib.Version)
		}
		lines = append(lines, withNamesLine{minor: v, lib: lib, supported: ok})
	}
	if opened == 0 {
		skipNoArtifacts(t, dir, "no line on it opened in this build")
	}
	return lines
}

// probeWithNames asks ONE artifact about format 10 and format 11, separately,
// exactly as docs/reference/bindings.md requires: one payload each through
// chs_rows, and only `accepted` counts. These two need no era refinement —
// both formats exist on every ClickHouse line measured, so unlike Buffers
// there is no 73 era to allow for.
func probeWithNames(t *testing.T, minor string, lib *Library) bool {
	t.Helper()
	s, err := lib.CompileDDL(withNamesDDL)
	if err != nil {
		t.Fatalf("line %s: compiling the probe schema %q: %v", minor, withNamesDDL, err)
	}
	defer s.Close()
	for _, p := range []struct {
		format Format
		body   string
	}{
		{CSVWithNames, withNamesCSVProbe},
		{TSVWithNames, withNamesTSVProbe},
	} {
		res, err := s.Rows(p.format, []byte(p.body), nil)
		if err != nil {
			t.Fatalf("line %s: probing format %d: %v", minor, int(p.format), err)
		}
		if res.Outcome != Accepted {
			return false
		}
	}
	return true
}

// supportedWithNamesLines is the subset the format cases run against — and a
// FAILURE when it is empty. A registry of pre-value artifacts is a real
// finding, and a suite that skipped past it would look exactly like one that
// proved these formats work.
func supportedWithNamesLines(t *testing.T) []withNamesLine {
	t.Helper()
	lines := withNamesLines(t)
	var out []withNamesLine
	for _, ln := range lines {
		if ln.supported {
			out = append(out, ln)
		}
	}
	if len(out) == 0 {
		t.Fatalf("no artifact in %s knows chs_format 10 or 11: %d line(s) opened and every one of them "+
			"refused the CSVWithNames/TSVWithNames probe. These formats joined enum chs_format inside "+
			"ABI revision 5 without bumping it, so an artifact built from an earlier revision-5 header "+
			"reports 5 and still does not know them — fetch a current line (scripts/fetch.sh 26.8) or "+
			"point $CHTYPES_REGISTRY at a registry holding one", testRegistryDir(t), len(lines))
	}
	return out
}

// headerMatchingIsExact reports whether this minor line matches header names
// EXACTLY: true through 26.4, false from 26.5, exactly as those servers do.
func headerMatchingIsExact(t *testing.T, minor string) bool {
	t.Helper()
	majorText, featureText, ok := strings.Cut(minor, ".")
	if !ok {
		t.Fatalf("minor line %q is not <major>.<minor>", minor)
	}
	major, err := strconv.Atoi(majorText)
	if err != nil {
		t.Fatalf("minor line %q: %v", minor, err)
	}
	feature, err := strconv.Atoi(featureText)
	if err != nil {
		t.Fatalf("minor line %q: %v", minor, err)
	}
	return major < 26 || (major == 26 && feature <= 4)
}

// withNamesValueOf finds one column's value in a row, or fails naming what was there.
func withNamesValueOf(t *testing.T, row RowResult, column string) Value {
	t.Helper()
	for _, v := range row.Values {
		if v.Column == column {
			return v
		}
	}
	var had []string
	for _, v := range row.Values {
		had = append(had, v.Column)
	}
	t.Fatalf("row has no value for %q (it has: %s)", column, strings.Join(had, ", "))
	return Value{}
}

// withNamesRowShape renders the parts of a row this file compares — verdict, values and
// unknown fields, never ClickHouse's message text, which the CSV reader change
// warns consumers not to pin.
func withNamesRowShape(row RowResult) string {
	var b strings.Builder
	fmt.Fprintf(&b, "outcome=%s code=%d values=[", row.Outcome, row.ErrCode)
	for i, v := range row.Values {
		if i > 0 {
			b.WriteString(" ")
		}
		fmt.Fprintf(&b, "%s=%s/%s/null=%t", v.Column, v.Text, v.Source, v.Null)
	}
	fmt.Fprintf(&b, "] unknown=[%s] transformed=[", strings.Join(row.UnknownFields, " "))
	for i, tr := range row.Transformed {
		if i > 0 {
			b.WriteString(" ")
		}
		fmt.Fprintf(&b, "%s:%s", tr.Column, tr.Reason)
	}
	b.WriteString("]")
	return b.String()
}

// withNamesSameVerdicts asserts two batches answered identically, field by field.
func withNamesSameVerdicts(t *testing.T, got, want BatchResult, gotName, wantName string) {
	t.Helper()
	if got.Outcome != want.Outcome || got.ErrCode != want.ErrCode {
		t.Fatalf("%s answered %s/%d, %s answered %s/%d", gotName, got.Outcome, got.ErrCode, wantName, want.Outcome, want.ErrCode)
	}
	if got.RowsRead != want.RowsRead || got.RowsSkipped != want.RowsSkipped {
		t.Fatalf("%s read %d row(s) (%d skipped), %s read %d (%d skipped)",
			gotName, got.RowsRead, got.RowsSkipped, wantName, want.RowsRead, want.RowsSkipped)
	}
	if len(got.Rows) != len(want.Rows) {
		t.Fatalf("%s produced %d row result(s), %s produced %d", gotName, len(got.Rows), wantName, len(want.Rows))
	}
	for i := range got.Rows {
		if withNamesRowShape(got.Rows[i]) != withNamesRowShape(want.Rows[i]) {
			t.Fatalf("row %d differs:\n  %s: %s\n  %s: %s", i, gotName, withNamesRowShape(got.Rows[i]), wantName, withNamesRowShape(want.Rows[i]))
		}
	}
}

// TestWithNamesRoundTripMatchesHeaderless is item 1: a body in format 10 or 11
// is accepted, and its verdicts match the headerless equivalent for the same
// rows. The reordered-header cases are the point of the formats: the data is
// addressed by NAME, so a header that names the columns in the other order
// still produces the declared-order stored row.
func TestWithNamesRoundTripMatchesHeaderless(t *testing.T) {
	lines := supportedWithNamesLines(t)
	cases := 0
	for _, ln := range lines {
		s, err := ln.lib.CompileDDL(withNamesDDL)
		if err != nil {
			t.Fatalf("line %s: compile %q: %v", ln.minor, withNamesDDL, err)
		}
		for _, c := range []struct {
			name       string
			withNames  Format
			headerless Format
			body       string
			plain      string
		}{
			{"CSVWithNames", CSVWithNames, CSV, "id,p\n1,a\n2,b\n", "1,a\n2,b\n"},
			{"CSVWithNames_reordered_header", CSVWithNames, CSV, "p,id\na,1\nb,2\n", "1,a\n2,b\n"},
			{"TSVWithNames", TSVWithNames, TSV, "id\tp\n1\ta\n2\tb\n", "1\ta\n2\tb\n"},
			{"TSVWithNames_reordered_header", TSVWithNames, TSV, "p\tid\na\t1\nb\t2\n", "1\ta\n2\tb\n"},
		} {
			t.Run(ln.minor+"/"+c.name, func(t *testing.T) {
				named, err := s.Rows(c.withNames, []byte(c.body), nil)
				if err != nil {
					t.Fatalf("rows(format %d): %v", int(c.withNames), err)
				}
				plain, err := s.Rows(c.headerless, []byte(c.plain), nil)
				if err != nil {
					t.Fatalf("rows(format %d): %v", int(c.headerless), err)
				}
				// Assert the pair is not vacuously equal: two identical
				// failures would satisfy withNamesSameVerdicts and prove nothing.
				if plain.Outcome != Accepted || len(plain.Rows) != 2 {
					t.Fatalf("the headerless control did not accept two rows: %s/%d, %d row(s)", plain.Outcome, plain.ErrCode, len(plain.Rows))
				}
				if named.Outcome != Accepted || len(named.Rows) != 2 {
					t.Fatalf("format %d did not accept two rows: %s/%d, %d row(s)", int(c.withNames), named.Outcome, named.ErrCode, len(named.Rows))
				}
				withNamesSameVerdicts(t, named, plain, fmt.Sprintf("format %d", int(c.withNames)), fmt.Sprintf("format %d", int(c.headerless)))
			})
			cases++
		}
		s.Close()
	}
	if cases == 0 {
		t.Fatalf("TestWithNamesRoundTripMatchesHeaderless ran ZERO cases: no line in the registry " +
			"answered the format 10/11 probe, so nothing was compared against a headerless body")
	}
	t.Logf("%d round-trip case(s) over %d line(s)", cases, len(lines))
}

// TestWithNamesHeaderCaseBoundary is item 2: header-name matching is EXACT
// through 26.4 and case-insensitive from 26.5, so the same case-differing
// header binds differently on either side. Both sides are required — a test on
// one side alone does not show a boundary, it shows one answer.
//
// The header is `ID,p` (and `ID<TAB>p`) against `id UInt8, p String`, with the
// row `1,a`:
//
//	through 26.4  `ID` names no column: it is an UNKNOWN FIELD and `id` takes
//	              the reader's zero, absent from the input.
//	from 26.5     `ID` names `id` case-insensitively: `id` is 1, from input,
//	              and there is no unknown field.
func TestWithNamesHeaderCaseBoundary(t *testing.T) {
	lines := supportedWithNamesLines(t)
	exactLines, foldingLines := 0, 0
	below, above := 0, 0
	for _, ln := range lines {
		s, err := ln.lib.CompileDDL(withNamesDDL)
		if err != nil {
			t.Fatalf("line %s: compile %q: %v", ln.minor, withNamesDDL, err)
		}
		exact := headerMatchingIsExact(t, ln.minor)
		if exact {
			exactLines++
		} else {
			foldingLines++
		}
		for _, c := range []struct {
			name   string
			format Format
			body   string
		}{
			{"CSVWithNames", CSVWithNames, "ID,p\n1,a\n"},
			{"TSVWithNames", TSVWithNames, "ID\tp\n1\ta\n"},
		} {
			t.Run(ln.minor+"/"+c.name, func(t *testing.T) {
				res, err := s.Rows(c.format, []byte(c.body), nil)
				if err != nil {
					t.Fatalf("rows(format %d): %v", int(c.format), err)
				}
				if res.Outcome != Accepted || len(res.Rows) != 1 {
					t.Fatalf("batch = %s/%d with %d row(s); want one accepted row", res.Outcome, res.ErrCode, len(res.Rows))
				}
				row := res.Rows[0]
				id := withNamesValueOf(t, row, "id")
				unknown := strings.Join(row.UnknownFields, " ")
				if exact {
					if id.Source == "input" {
						t.Fatalf("ClickHouse %s matches header names exactly (through 26.4), so `ID` must not bind to `id`; got %s", ln.lib.Version, withNamesRowShape(row))
					}
					if id.Text != "0" {
						t.Fatalf("ClickHouse %s: `id` was named by no header column, so it takes the reader's zero; got %s", ln.lib.Version, withNamesRowShape(row))
					}
					if unknown != "ID" {
						t.Fatalf("ClickHouse %s: `ID` names no column through 26.4 and must be reported as an unknown field; got %s", ln.lib.Version, withNamesRowShape(row))
					}
				} else {
					if id.Source != "input" || id.Text != "1" {
						t.Fatalf("ClickHouse %s matches header names case-insensitively (from 26.5), so `ID` must bind to `id`; got %s", ln.lib.Version, withNamesRowShape(row))
					}
					if unknown != "" {
						t.Fatalf("ClickHouse %s: `ID` binds to `id` from 26.5, so there is no unknown field; got %s", ln.lib.Version, withNamesRowShape(row))
					}
				}
				t.Logf("ClickHouse %s: %s", ln.lib.Version, withNamesRowShape(row))
			})
			if exact {
				below++
			} else {
				above++
			}
		}
		s.Close()
	}
	// Both sides, or this proved nothing. A registry holding only lines above
	// the boundary answers every case the same way and says nothing about
	// where the behavior changes.
	if below == 0 {
		t.Fatalf("TestWithNamesHeaderCaseBoundary ran ZERO cases BELOW the 26.5 boundary: the registry "+
			"holds no line at or under 26.4 that knows formats 10 and 11 (it offered %d line(s) above it). "+
			"Header matching is exact through 26.4 and case-insensitive from 26.5, and one side alone "+
			"cannot show that — install an older line (scripts/fetch.sh 24.8, or 26.4)", foldingLines)
	}
	if above == 0 {
		t.Fatalf("TestWithNamesHeaderCaseBoundary ran ZERO cases AT OR ABOVE the 26.5 boundary: the "+
			"registry holds no line from 26.5 on that knows formats 10 and 11 (it offered %d line(s) "+
			"below it). Install a current line (scripts/fetch.sh 26.8)", exactLines)
	}
	t.Logf("%d case(s) below the boundary over %d line(s), %d at or above it over %d line(s)", below, exactLines, above, foldingLines)
}

// TestWithNamesColumnListInterplay is item 3. With an INSERT column list as
// well as a header, the LIST decides the block and the HEADER decides the
// layout:
//
//	a listed column the header omits   DEFAULT under
//	                                   input_format_defaults_for_omitted_fields=1,
//	                                   the reader's zero under 0
//	an unlisted column the header names  an unknown field
//	an unlisted column                 its DEFAULT whatever that setting says
//
// The last rule is NOT a WithNames rule — the 0.3.0 CHANGELOG states it
// applies to every format — so it is executed on JSONEachRow and on headerless
// CSV as well, which a test on 10 and 11 alone would not show.
func TestWithNamesColumnListInterplay(t *testing.T) {
	lines := supportedWithNamesLines(t)
	omitted, unknown, unlisted := 0, 0, 0
	for _, ln := range lines {
		s, err := ln.lib.CompileDDL(withNamesListDDL)
		if err != nil {
			t.Fatalf("line %s: compile %q: %v", ln.minor, withNamesListDDL, err)
		}

		// A LISTED column the header omits, under each setting.
		for _, c := range []struct {
			name       string
			format     Format
			body       string
			defaults   string
			wantText   string
			wantSource string
		}{
			{"CSVWithNames_defaults_1", CSVWithNames, "id\n1\n", "1", "7", "default"},
			{"CSVWithNames_defaults_0", CSVWithNames, "id\n1\n", "0", "0", "absent"},
			{"TSVWithNames_defaults_1", TSVWithNames, "id\n1\n", "1", "7", "default"},
			{"TSVWithNames_defaults_0", TSVWithNames, "id\n1\n", "0", "0", "absent"},
		} {
			t.Run(ln.minor+"/listed_column_the_header_omits/"+c.name, func(t *testing.T) {
				res, err := s.Rows(c.format, []byte(c.body),
					map[string]string{"input_format_defaults_for_omitted_fields": c.defaults},
					WithColumns([]string{"id", "p"}))
				if err != nil {
					t.Fatalf("rows(format %d): %v", int(c.format), err)
				}
				if res.Outcome != Accepted || len(res.Rows) != 1 {
					t.Fatalf("batch = %s/%d with %d row(s); want one accepted row", res.Outcome, res.ErrCode, len(res.Rows))
				}
				p := withNamesValueOf(t, res.Rows[0], "p")
				if p.Text != c.wantText || p.Source != c.wantSource {
					t.Fatalf("`p` is listed and the header omits it: under input_format_defaults_for_omitted_fields=%s it must be %s/%s; got %s",
						c.defaults, c.wantText, c.wantSource, withNamesRowShape(res.Rows[0]))
				}
			})
			omitted++
		}

		// An UNLISTED column the header names is an unknown field — and still
		// takes its own DEFAULT.
		for _, c := range []struct {
			name   string
			format Format
			body   string
		}{
			{"CSVWithNames", CSVWithNames, "id,p\n1,3\n"},
			{"TSVWithNames", TSVWithNames, "id\tp\n1\t3\n"},
		} {
			t.Run(ln.minor+"/unlisted_column_the_header_names/"+c.name, func(t *testing.T) {
				res, err := s.Rows(c.format, []byte(c.body), nil, WithColumns([]string{"id"}))
				if err != nil {
					t.Fatalf("rows(format %d): %v", int(c.format), err)
				}
				if res.Outcome != Accepted || len(res.Rows) != 1 {
					t.Fatalf("batch = %s/%d with %d row(s); want one accepted row", res.Outcome, res.ErrCode, len(res.Rows))
				}
				row := res.Rows[0]
				if strings.Join(row.UnknownFields, " ") != "p" {
					t.Fatalf("`p` is not in the column list, so the header naming it is an unknown field; got %s", withNamesRowShape(row))
				}
				p := withNamesValueOf(t, row, "p")
				if p.Text != "7" || p.Source != "default" {
					t.Fatalf("`p` is unlisted, so it takes its DEFAULT 7; got %s", withNamesRowShape(row))
				}
			})
			unknown++
		}

		// The unlisted-column DEFAULT rule, on formats that are NOT WithNames.
		// The 0.3.0 CHANGELOG says it applies to every format; a case on 10 and
		// 11 alone would not show that.
		for _, c := range []struct {
			name     string
			format   Format
			body     string
			defaults string
		}{
			{"JSONEachRow_defaults_0", JSONEachRow, `{"id":1}` + "\n", "0"},
			{"JSONEachRow_defaults_1", JSONEachRow, `{"id":1}` + "\n", "1"},
			{"CSV_defaults_0", CSV, "1\n", "0"},
			{"CSV_defaults_1", CSV, "1\n", "1"},
			{"TSV_defaults_0", TSV, "1\n", "0"},
			{"TSV_defaults_1", TSV, "1\n", "1"},
		} {
			t.Run(ln.minor+"/unlisted_column_takes_its_default/"+c.name, func(t *testing.T) {
				res, err := s.Rows(c.format, []byte(c.body),
					map[string]string{"input_format_defaults_for_omitted_fields": c.defaults},
					WithColumns([]string{"id"}))
				if err != nil {
					t.Fatalf("rows(format %d): %v", int(c.format), err)
				}
				if res.Outcome != Accepted || len(res.Rows) != 1 {
					t.Fatalf("batch = %s/%d with %d row(s); want one accepted row", res.Outcome, res.ErrCode, len(res.Rows))
				}
				row := res.Rows[0]
				for _, want := range []struct{ column, text string }{{"p", "7"}, {"q", "9"}} {
					v := withNamesValueOf(t, row, want.column)
					if v.Text != want.text || v.Source != "default" {
						t.Fatalf("`%s` is not in the column list, so it takes its DEFAULT %s whatever "+
							"input_format_defaults_for_omitted_fields says (here %s); got %s",
							want.column, want.text, c.defaults, withNamesRowShape(row))
					}
				}
			})
			unlisted++
		}
		s.Close()
	}
	if omitted == 0 || unknown == 0 || unlisted == 0 {
		t.Fatalf("TestWithNamesColumnListInterplay ran ZERO cases in at least one block: "+
			"listed-column-the-header-omits=%d, unlisted-column-the-header-names=%d, "+
			"unlisted-column-takes-its-DEFAULT=%d — each must run at least once", omitted, unknown, unlisted)
	}
	t.Logf("%d omitted-listed-column case(s), %d unknown-field case(s), %d unlisted-DEFAULT case(s) over %d line(s)",
		omitted, unknown, unlisted, len(lines))
}

// TestWithNamesExportFormatDeclines is item 4: as an export_format, 10 and 11
// are the existing loud decline, like every format other than
// JSONCompactEachRow. This one needs no probe — an artifact that cannot
// SERIALIZE a format declines it whether or not it can READ it — so it runs
// against every line that opened.
func TestWithNamesExportFormatDeclines(t *testing.T) {
	lines := withNamesLines(t)
	cases := 0
	for _, ln := range lines {
		s, err := ln.lib.CompileDDL(withNamesDDL)
		if err != nil {
			t.Fatalf("line %s: compile %q: %v", ln.minor, withNamesDDL, err)
		}
		for _, format := range []Format{CSVWithNames, TSVWithNames} {
			t.Run(fmt.Sprintf("%s/export_format_%d", ln.minor, int(format)), func(t *testing.T) {
				res, err := s.RowsExport(JSONEachRow, []byte(`{"id":1,"p":"a"}`+"\n"), nil, format)
				if err != nil {
					t.Fatalf("rowsExport(export %d): %v", int(format), err)
				}
				if res.Outcome != Unsupported {
					t.Fatalf("export_format %d must be declined, loudly: outcome = %s/%d", int(format), res.Outcome, res.ErrCode)
				}
				if res.ErrCode != CodeUnsupported {
					t.Fatalf("export_format %d declined with code %d; the ABI's sentinel is %d", int(format), res.ErrCode, CodeUnsupported)
				}
				if res.Payload != nil {
					t.Fatalf("export_format %d was declined and still produced %d payload byte(s)", int(format), len(res.Payload))
				}
				if len(res.Rows) != 0 || res.RowsRead != 0 {
					t.Fatalf("a declined export processes nothing: %d row result(s), %d row(s) read", len(res.Rows), res.RowsRead)
				}
			})
			cases++
		}
		s.Close()
	}
	if cases == 0 {
		t.Fatalf("TestWithNamesExportFormatDeclines ran ZERO cases: no line in the registry opened, so " +
			"the export decline for formats 10 and 11 was never asked for")
	}
	t.Logf("%d export-decline case(s) over %d line(s)", cases, len(lines))
}
