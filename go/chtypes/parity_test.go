// parity_test.go — the binding parity contract, checked against this binding.
//
// tests/parity/manifest.json at the repository root is the ONE machine-readable
// source of truth for what every binding must expose; docs/reference/bindings.md is the
// prose that explains why. This file is Go's half of the enforcement: it checks
// every spelling the manifest assigns to the `go` column against this package's
// own syntax tree, and fails by NAME when one is missing — naming the bindings
// that DO have it, because "go is missing Libraries, which python/ts/rust all
// expose" is the sentence that gets the gap fixed and "parity check failed"
// is not.
//
// Three rules this file holds:
//
//   - It cannot pass by doing nothing. A manifest that parsed to zero
//     capabilities, or fewer than its own declared floors, FAILS — as does a run
//     in which nothing resolved, or one whose value table compared nothing.
//   - It needs no artifact. Parity is a claim about the API surface, not about
//     dlopening a library, so all of it runs in the artifact-free CI job.
//     Nothing here opens a Registry.
//   - A deliberate gap is still written down. A binding that should not have a
//     capability declares {"absent": "<why>"}; an absence with no reason fails
//     the manifest's own integrity check. Go has two real ones and the spec
//     argues both: the dlopen'd function-pointer table deliberately resolves
//     neither chs_shutdown nor chs_set_default_settings.
//
// WHY THE MANIFEST IS EMBEDDED. scripts/check-standalone.sh copies go/ to a
// scratch directory "with nothing around it" and runs the suite there — that
// copy is the tree a consumer's `go get` produces, and it has no repository root
// to read tests/parity/manifest.json from. Skipping there would make this check
// pass by doing nothing in exactly the gate that exists to stop that. So Go
// embeds a byte-identical copy, testdata/parity.json, and four suites assert it
// has not drifted: python, ts and rust always, and this one whenever it can see
// the repository root.
//
// PRESENCE IS READ OFF THE SYNTAX TREE, not off reflection: Go cannot reflect on
// package-level functions, and go/types cannot type-check a cgo package from
// source. go/parser can, and the .go files are always beside the test. VALUES
// are read off the real constants through the table below, so a rename is a
// COMPILE error here and a changed value is a test failure.

package chtypes_test

import (
	"bytes"
	_ "embed"
	"encoding/json"
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"testing"

	"github.com/wave-rf/chtypes/go/chtypes"
)

//go:embed testdata/parity.json
var parityManifest []byte

const lang = "go"

var otherLangs = []string{"python", "ts", "rust"}

// ---------------------------------------------------------------- the manifest

type column struct {
	Symbol     string `json:"symbol"`
	Absent     string `json:"absent"`
	ValueCheck *bool  `json:"value_check"`
	Why        string `json:"why"`
}

type capability struct {
	ID    string          `json:"id"`
	Group string          `json:"group"`
	Kind  string          `json:"kind"`
	What  string          `json:"what"`
	Value json.RawMessage `json:"value"`

	Go     json.RawMessage `json:"go"`
	Python json.RawMessage `json:"python"`
	TS     json.RawMessage `json:"ts"`
	Rust   json.RawMessage `json:"rust"`
}

func (c capability) column(l string) column {
	var raw json.RawMessage
	switch l {
	case "go":
		raw = c.Go
	case "python":
		raw = c.Python
	case "ts":
		raw = c.TS
	case "rust":
		raw = c.Rust
	}
	if len(raw) == 0 {
		return column{}
	}
	var s string
	if json.Unmarshal(raw, &s) == nil {
		return column{Symbol: s}
	}
	var col column
	_ = json.Unmarshal(raw, &col)
	return col
}

func (c capability) spelling(l string) string { return c.column(l).Symbol }

// alsoIn names the other bindings that DO carry a capability, for the failure text.
func (c capability) alsoIn() string {
	var have []string
	for _, l := range otherLangs {
		if c.spelling(l) != "" {
			have = append(have, l)
		}
	}
	if len(have) == 0 {
		return "no other binding"
	}
	return strings.Join(have, "/")
}

type manifestDoc struct {
	Schema       int               `json:"schema"`
	Languages    []string          `json:"languages"`
	Groups       map[string]string `json:"groups"`
	Capabilities []capability      `json:"capabilities"`
	Floors       struct {
		Capabilities int `json:"capabilities"`
		Valued       int `json:"valued"`
		PerGroup     int `json:"per_group"`
	} `json:"floors"`
	Unlisted map[string]map[string]string `json:"unlisted"`
}

func loadManifest(t *testing.T) manifestDoc {
	t.Helper()
	if len(parityManifest) == 0 {
		t.Fatal("the embedded parity manifest is EMPTY — the contract this suite enforces is gone")
	}
	var doc manifestDoc
	if err := json.Unmarshal(parityManifest, &doc); err != nil {
		t.Fatalf("the embedded parity manifest is not valid JSON: %v", err)
	}
	return doc
}

// ------------------------------------------------- this package's own surface

type surface struct {
	top map[string]bool // package-level funcs, types, consts and vars
	all map[string]bool // the above plus Type.Method and Type.Field
}

// parseSurface reads every .go file beside this test — the package as a
// consumer's `go get` tree holds it — and returns its exported names. Build
// tags are honored the way the default build sees them: a file behind
// chtypes_linked is NOT part of the dlopen-only surface this contract governs.
func parseSurface(t *testing.T) surface {
	t.Helper()
	_, self, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("cannot locate this test file; the syntax-tree scan cannot run and must not be reported as passing")
	}
	dir := filepath.Dir(self)
	fset := token.NewFileSet()
	// parser.ParseFile per entry rather than parser.ParseDir: the latter is
	// deprecated precisely because it ignores build tags, and this package has
	// one that matters — chtypes_linked adds a whole second API surface that is
	// NOT part of the dlopen-only contract. behindLinkedTag below does that
	// filtering explicitly, so nothing is lost by dropping the deprecated call.
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatalf("cannot read the package directory %s: %v", dir, err)
	}
	var files []*ast.File
	for _, e := range entries {
		name := e.Name()
		if e.IsDir() || !strings.HasSuffix(name, ".go") || strings.HasSuffix(name, "_test.go") {
			continue
		}
		f, err := parser.ParseFile(fset, filepath.Join(dir, name), nil, parser.ParseComments)
		if err != nil {
			t.Fatalf("cannot parse %s: %v", name, err)
		}
		files = append(files, f)
	}
	if len(files) == 0 {
		t.Fatal("the syntax-tree scan found no package files; an empty surface must fail, never pass")
	}
	s := surface{top: map[string]bool{}, all: map[string]bool{}}
	add := func(name string) {
		if name != "" && ast.IsExported(name) {
			s.top[name] = true
			s.all[name] = true
		}
	}
	member := func(recv, name string) {
		if ast.IsExported(name) {
			s.all[recv+"."+name] = true
		}
	}
	for _, file := range files {
		{
			if behindLinkedTag(file) {
				continue
			}
			for _, decl := range file.Decls {
				switch d := decl.(type) {
				case *ast.FuncDecl:
					if d.Recv == nil || len(d.Recv.List) == 0 {
						add(d.Name.Name)
						continue
					}
					member(receiverName(d.Recv.List[0].Type), d.Name.Name)
				case *ast.GenDecl:
					for _, spec := range d.Specs {
						switch sp := spec.(type) {
						case *ast.TypeSpec:
							add(sp.Name.Name)
							if st, isStruct := sp.Type.(*ast.StructType); isStruct && st.Fields != nil {
								for _, f := range st.Fields.List {
									for _, n := range f.Names {
										member(sp.Name.Name, n.Name)
									}
								}
							}
						case *ast.ValueSpec:
							for _, n := range sp.Names {
								add(n.Name)
							}
						}
					}
				}
			}
		}
	}
	return s
}

// behindLinkedTag reports whether a file is the statically linked path. The
// linked surface is optional by spec ("A binding MAY additionally expose the
// statically linked, single-version shape … it is the fast path and it is
// optional; the Registry path is the product"), so it is deliberately outside
// this contract.
func behindLinkedTag(file *ast.File) bool {
	for _, group := range file.Comments {
		for _, c := range group.List {
			if strings.HasPrefix(c.Text, "//go:build") && strings.Contains(c.Text, "chtypes_linked") {
				return true
			}
		}
	}
	return false
}

func receiverName(expr ast.Expr) string {
	switch t := expr.(type) {
	case *ast.StarExpr:
		return receiverName(t.X)
	case *ast.Ident:
		return t.Name
	case *ast.IndexExpr:
		return receiverName(t.X)
	}
	return ""
}

// ------------------------------------------------------------- the value table
//
// Every entry is a REAL reference to the constant it names, so a rename does
// not fail this test — it fails the BUILD, which is louder and earlier. The
// keys are the manifest's own `go` spellings, and TestParityValues asserts the
// table covers every valued capability assigned to Go: a contract that grows a
// value Go never compares would otherwise pass in silence.
//
// The spellings whose shared value is a STRING but whose Go representation is a
// typed integer render through the same String() a caller would use.
var goValues = map[string]any{
	// chs_format — the numbers are frozen (the core repository's C ABI specification §Types and schemas).
	"JSONEachRow":                           int(chtypes.JSONEachRow),
	"CSV":                                   int(chtypes.CSV),
	"TSV":                                   int(chtypes.TSV),
	"Values":                                int(chtypes.Values),
	"JSONCompactEachRow":                    int(chtypes.JSONCompactEachRow),
	"RowBinary":                             int(chtypes.RowBinary),
	"RowBinaryWithDefaults":                 int(chtypes.RowBinaryWithDefaults),
	"RowBinaryWithNamesAndTypesAndDefaults": int(chtypes.RowBinaryWithNamesAndTypesAndDefaults),
	"Native":                                int(chtypes.Native),
	"Buffers":                               int(chtypes.Buffers),

	// The row/batch verdict vocabulary, as the result document spells it.
	"Accepted":         chtypes.Accepted.String(),
	"Rejected":         chtypes.Rejected.String(),
	"AcceptedPoisoned": chtypes.AcceptedPoisoned.String(),
	"Unsupported":      chtypes.Unsupported.String(),
	"Skipped":          chtypes.Skipped.String(),

	// The call-level filter verdict.
	"FilterOK":          chtypes.FilterOK.String(),
	"FilterRejected":    chtypes.FilterRejected.String(),
	"FilterUnsupported": chtypes.FilterUnsupported.String(),

	// chs_schema_column_default_kind's five answers.
	"KindNone":         chtypes.KindNone.String(),
	"KindDefault":      chtypes.KindDefault.String(),
	"KindMaterialized": chtypes.KindMaterialized.String(),
	"KindAlias":        chtypes.KindAlias.String(),
	"KindEphemeral":    chtypes.KindEphemeral.String(),

	// The transform reason vocabulary — stable strings; the harness groups on them.
	"ReasonOverflowWrap":        chtypes.ReasonOverflowWrap,
	"ReasonNullToDefault":       chtypes.ReasonNullToDefault,
	"ReasonNullLoss":            chtypes.ReasonNullLoss,
	"ReasonDecimalTruncate":     chtypes.ReasonDecimalTruncate,
	"ReasonDateClamp":           chtypes.ReasonDateClamp,
	"ReasonDateTimeWrap":        chtypes.ReasonDateTimeWrap,
	"ReasonDateShift":           chtypes.ReasonDateShift,
	"ReasonUUIDMangle":          chtypes.ReasonUUIDMangle,
	"ReasonIPMangle":            chtypes.ReasonIPMangle,
	"ReasonFloatPrecision":      chtypes.ReasonFloatPrecision,
	"ReasonLossyNumeric":        chtypes.ReasonLossyNumeric,
	"ReasonStringPad":           chtypes.ReasonStringPad,
	"ReasonEmptied":             chtypes.ReasonEmptied,
	"ReasonElementChanged":      chtypes.ReasonElementChanged,
	"ReasonEnumCoerce":          chtypes.ReasonEnumCoerce,
	"ReasonValueChanged":        chtypes.ReasonValueChanged,
	"ReasonPoisoned":            chtypes.ReasonPoisoned,
	"ReasonDuplicateKeyDropped": chtypes.ReasonDuplicateKeyDropped,
	"ReasonReformat":            chtypes.ReasonReformat,
	"ReasonDefaultFilled":       chtypes.ReasonDefaultFilled,
	"ReasonZeroFilled":          chtypes.ReasonZeroFilled,
	"ReasonDefaultMaterialized": chtypes.ReasonDefaultMaterialized,
	"ReasonTTLExpired":          chtypes.ReasonTTLExpired,
	"ReasonTTLColumnExpired":    chtypes.ReasonTTLColumnExpired,

	// ABI identity and the document/compile/export channels.
	"ABIRevision":     chtypes.ABIRevision,
	"CodeUnsupported": chtypes.CodeUnsupported,
	"CompileDeclared": int(chtypes.CompileDeclared),
	"ExportNone":      int(chtypes.ExportNone),
	"DocValues":       int(chtypes.DocValues),
	"DocTransforms":   int(chtypes.DocTransforms),
	"DocDefaults":     int(chtypes.DocDefaults),
	"DocAll":          int(chtypes.DocAll),

	// docs/fetch.md §6 — the machine-readable codes the four CLIs print.
	"CodeArtifactMissing":     string(chtypes.CodeArtifactMissing),
	"CodeArtifactUntrusted":   string(chtypes.CodeArtifactUntrusted),
	"CodeArtifactCorrupt":     string(chtypes.CodeArtifactCorrupt),
	"CodeArtifactPinned":      string(chtypes.CodeArtifactPinned),
	"CodeArtifactUnpublished": string(chtypes.CodeArtifactUnpublished),
	"CodeSourceUnreachable":   string(chtypes.CodeSourceUnreachable),

	// The canonical discovery queries, carried verbatim (docs/reference/bindings.md §Discovery).
	"QueryServerVersion":   chtypes.QueryServerVersion,
	"QueryChangedSettings": chtypes.QueryChangedSettings,
	"QueryTableColumns":    chtypes.QueryTableColumns,

	// The artifact source and its trust anchor.
	"ReleaseKeyID":        chtypes.ReleaseKeyID,
	"ReleasePublicKeyHex": chtypes.ReleasePublicKeyHex,
	"DefaultArtifactsURL": chtypes.DefaultArtifactsURL,
	"DefaultReleaseTag":   chtypes.DefaultReleaseTag,
	"DefaultLockFile":     chtypes.DefaultLockFile,
	"LockSchema":          chtypes.LockSchema,
}

// ---------------------------------------------------------------------- tests

func TestParityManifestMeetsItsOwnFloors(t *testing.T) {
	doc := loadManifest(t)
	if doc.Schema != 1 {
		t.Fatalf("unknown parity manifest schema %d", doc.Schema)
	}
	if n := len(doc.Capabilities); n < doc.Floors.Capabilities {
		t.Fatalf("the parity manifest declares %d capabilities, below its own floor of %d. "+
			"A shrinking contract is how this check passes by doing nothing.", n, doc.Floors.Capabilities)
	}
	valued := 0
	perGroup := map[string]int{}
	for _, c := range doc.Capabilities {
		if len(c.Value) > 0 {
			valued++
		}
		perGroup[c.Group]++
	}
	if valued < doc.Floors.Valued {
		t.Fatalf("only %d capabilities carry a shared value, below the floor of %d. Values are what "+
			"prove the four bindings answer the same bytes, not merely that they have a symbol.",
			valued, doc.Floors.Valued)
	}
	for group := range doc.Groups {
		if perGroup[group] < doc.Floors.PerGroup {
			t.Errorf("group %q has %d capabilities, below the floor of %d", group, perGroup[group], doc.Floors.PerGroup)
		}
	}
}

func TestParityManifestIsFullyDeclared(t *testing.T) {
	doc := loadManifest(t)
	seen := map[string]bool{}
	var problems []string
	for _, c := range doc.Capabilities {
		if seen[c.ID] {
			problems = append(problems, c.ID+": duplicate id")
		}
		seen[c.ID] = true
		if strings.TrimSpace(c.What) == "" {
			problems = append(problems, c.ID+": no `what` — a capability with no description is a name, not a contract")
		}
		if _, known := doc.Groups[c.Group]; !known {
			problems = append(problems, fmt.Sprintf("%s: unknown group %q", c.ID, c.Group))
		}
		absent := 0
		for _, l := range doc.Languages {
			col := c.column(l)
			switch {
			case col.Symbol != "":
			case col.Absent != "":
				absent++
			default:
				problems = append(problems, fmt.Sprintf("%s: %s column has neither a symbol nor a "+
					"declared absence. A gap that nobody had to justify in writing is how parity rots.", c.ID, l))
			}
		}
		if absent == len(doc.Languages) {
			problems = append(problems, c.ID+": absent in every binding — that is a note, not a contract")
		}
	}
	if len(problems) > 0 {
		t.Fatalf("the parity manifest is not internally consistent:\n  %s", strings.Join(problems, "\n  "))
	}
}

func TestGoExposesEveryCapabilityTheContractAssignsIt(t *testing.T) {
	doc := loadManifest(t)
	s := parseSurface(t)
	var missing []string
	resolved := 0
	for _, c := range doc.Capabilities {
		spelled := c.spelling(lang)
		if spelled == "" || c.Kind == "cli" {
			continue
		}
		if s.all[spelled] {
			resolved++
			continue
		}
		missing = append(missing, fmt.Sprintf("%s: go is missing `%s`, which %s expose — %s",
			c.ID, spelled, c.alsoIn(), c.What))
	}
	if resolved == 0 {
		t.Fatal("no go spelling resolved at all — the check asserted nothing")
	}
	if len(missing) > 0 {
		t.Fatalf("go does not carry %d capability/capabilities the parity contract assigns it:\n  %s",
			len(missing), strings.Join(missing, "\n  "))
	}
	t.Logf("%d go spellings resolved against the package's own syntax tree", resolved)
}

func TestGoAnswersTheSameValuesAsTheOtherBindings(t *testing.T) {
	doc := loadManifest(t)
	var wrong, uncovered []string
	checked := 0
	for _, c := range doc.Capabilities {
		if len(c.Value) == 0 || c.Kind == "cli" {
			continue
		}
		col := c.column(lang)
		if col.Symbol == "" || (col.ValueCheck != nil && !*col.ValueCheck) {
			continue
		}
		got, covered := goValues[col.Symbol]
		if !covered {
			uncovered = append(uncovered, fmt.Sprintf("%s: the contract gives `%s` a shared value and the "+
				"go value table does not carry it", c.ID, col.Symbol))
			continue
		}
		checked++
		var want any
		if err := json.Unmarshal(c.Value, &want); err != nil {
			t.Fatalf("%s: unreadable value in the manifest: %v", c.ID, err)
		}
		if !sameValue(want, got) {
			wrong = append(wrong, fmt.Sprintf("%s: go `%s` is %#v, the contract says %#v", c.ID, col.Symbol, got, want))
		}
	}
	// A value table that stopped covering the contract is the quiet failure this
	// guards: the check would still "pass", having compared less and less.
	if len(uncovered) > 0 {
		t.Errorf("the go value table has fallen behind the contract:\n  %s", strings.Join(uncovered, "\n  "))
	}
	if checked < doc.Floors.Valued {
		t.Errorf("only %d shared values were actually compared, below the floor of %d — a value check "+
			"that checks nothing is not a check", checked, doc.Floors.Valued)
	}
	if len(wrong) > 0 {
		t.Fatalf("go answers differently from the contract:\n  %s", strings.Join(wrong, "\n  "))
	}
}

func sameValue(want, got any) bool {
	switch w := want.(type) {
	case float64: // every JSON number decodes to float64
		switch g := got.(type) {
		case int:
			return float64(g) == w
		case float64:
			return g == w
		}
		return false
	case string:
		g, ok := got.(string)
		return ok && g == w
	}
	return false
}

func TestGoCLIOffersEveryContractSubcommand(t *testing.T) {
	doc := loadManifest(t)
	_, self, _, _ := runtime.Caller(0)
	main := filepath.Join(filepath.Dir(filepath.Dir(self)), "cmd", "chtypes", "main.go")
	src, err := os.ReadFile(main)
	if err != nil {
		t.Fatalf("cannot read the CLI at %s: %v — the subcommand check cannot run and must not be "+
			"reported as passing", main, err)
	}
	commands := 0
	var missing []string
	for _, c := range doc.Capabilities {
		if c.Kind != "cli" || c.spelling(lang) == "" {
			continue
		}
		commands++
		var want string
		_ = json.Unmarshal(c.Value, &want)
		if !bytes.Contains(src, []byte(`"`+want+`"`)) {
			missing = append(missing, fmt.Sprintf("%s: the go CLI has no `%s` subcommand, which %s offer",
				c.ID, want, c.alsoIn()))
		}
	}
	if commands == 0 {
		t.Fatal("the contract names no CLI subcommands")
	}
	if len(missing) > 0 {
		t.Fatalf("%s", strings.Join(missing, "\n  "))
	}
}

func TestNoGoPublicNameEscapesTheContract(t *testing.T) {
	doc := loadManifest(t)
	s := parseSurface(t)
	declared := map[string]bool{}
	for _, c := range doc.Capabilities {
		if sp := c.spelling(lang); sp != "" {
			declared[strings.SplitN(sp, ".", 2)[0]] = true
		}
	}
	for name := range doc.Unlisted[lang] {
		declared[strings.SplitN(name, ".", 2)[0]] = true
	}
	var undeclared []string
	for name := range s.top {
		if !declared[name] {
			undeclared = append(undeclared, name)
		}
	}
	sort.Strings(undeclared)
	if len(undeclared) > 0 {
		t.Fatalf("go exports %d public name(s) the parity contract has never heard of:\n  %s\n\n"+
			"Either give each one a capability in tests/parity/manifest.json (if the other bindings should "+
			"have it too) or list it under `unlisted.go` with a reason.",
			len(undeclared), strings.Join(undeclared, "\n  "))
	}
}

func TestGoUnlistedAllowlistHasNotRotted(t *testing.T) {
	doc := loadManifest(t)
	s := parseSurface(t)
	var gone []string
	for name := range doc.Unlisted[lang] {
		if !s.all[name] {
			gone = append(gone, name)
		}
	}
	sort.Strings(gone)
	if len(gone) > 0 {
		t.Fatalf("`unlisted.go` in the parity manifest excuses names this binding no longer exports:\n  %s",
			strings.Join(gone, "\n  "))
	}
}

// TestEmbeddedParityManifestMatchesTheRepository closes the one seam the embed
// opens. It runs only in a checkout — in the bare copy scripts/check-standalone.sh
// builds there is no repository root by design, and python, ts and rust assert the
// same equality from there, so the seam is covered three times over.
func TestEmbeddedParityManifestMatchesTheRepository(t *testing.T) {
	_, self, _, _ := runtime.Caller(0)
	canonical := filepath.Join(filepath.Dir(filepath.Dir(filepath.Dir(self))), "tests", "parity", "manifest.json")
	want, err := os.ReadFile(canonical)
	if err != nil {
		t.Skipf("SKIPPED: no repository root beside this package (%s) — this is the bare-copy tree, "+
			"where the embedded manifest is the only copy; python, ts and rust assert this equality", canonical)
	}
	if !bytes.Equal(want, parityManifest) {
		t.Fatalf("%s has drifted from the embedded testdata/parity.json. The manifest is one file and "+
			"this is its cache. Re-sync it:\n\n    cp tests/parity/manifest.json go/chtypes/testdata/parity.json\n",
			canonical)
	}
}
