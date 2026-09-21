// check-truth-namespace.go — go/chtypes never declares a top-level identifier
// in the name range reserved for the artifact producer's overlay tests.
//
// WHY THIS EXISTS. The artifact producer's server-truth tests are compiled
// into go/chtypes with `go test -overlay`, so the two share one package
// namespace. Three times now a top-level name has been declared on both
// sides — most recently TestUnknownOutcomeDegradesToUnsupported, which broke
// their build. This repository's CI cannot see their files, and theirs only
// meets a new name once the header pin moves to point at their revision — so
// neither side's existing checks could catch it early. The fix is a contract
// where each side checks its own half without needing the other's files: any
// top-level identifier matching ^[Tt]ruth or ^(Test|Benchmark|Fuzz|Example)Truth
// is reserved for that overlay, and this repository never declares one.
//
// WHY GO'S OWN PARSER, NOT A REGEX OVER THE TEXT. A rule anchored on
// ^func|^type|^var|^const finds `var TruthX = 1` and silently misses
//
//	var (
//		TruthX = 1
//	)
//
// because "TruthX" never starts a line. Correctly enumerating every member of
// a grouped const/var/type block — while ignoring a same-named local variable
// declared inside a function body, a struct field, or a string literal that
// happens to contain the word "func" — is exactly the class of thing a
// hand-rolled tokenizer gets subtly wrong, and wrong silently. go/parser
// already solves this: it walks a real syntax tree and hands back exactly
// the top-level declarations, grouped or not, with nothing nested included.
// This is NOT `go build` or `go test` — go/parser parses syntax only, does
// not resolve imports or types, needs no artifact, no CGO, and no network;
// it is closer in cost to `gofmt` than to a build. Chosen deliberately over
// a shell/regex implementation for that reason: a silent miss on a grouped
// block is the exact shape of failure this repository's other static
// checkers exist to refuse.
//
// METHODS ARE EXCLUDED. A function with a receiver (`func (r *Registry) Foo()`)
// is looked up as `r.Foo`, never as a bare package-level identifier — it does
// not occupy the package namespace the overlay shares, so it cannot collide
// with an overlay-declared name and is not checked here.
//
//	scripts/check-truth-namespace.sh              check go/chtypes/*.go
//	scripts/check-truth-namespace.sh --selftest   plant reserved names into a
//	                                              temporary copy and prove
//	                                              each one is caught
package main

import (
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
)

// The contract, exactly, from issue #127: a pure prefix match on "Truth"
// (case-insensitive on the leading letter only — "Truthy" and "Truthless"
// are reserved too, on purpose, see the failure message below), plus the
// Go test-function convention where the marker sits right after the
// required Test/Benchmark/Fuzz/Example prefix.
var reservedPattern = regexp.MustCompile(`^([Tt]ruth|(Test|Benchmark|Fuzz|Example)Truth)`)

const targetDir = "go/chtypes"

// Finding is one reserved-range top-level identifier.
type Finding struct {
	Ident string
	File  string // as passed to checkDir — a real run uses repo-relative paths
	Line  int
}

func (f Finding) String() string {
	return fmt.Sprintf("%s:%d: %s — reserved for the artifact producer's overlay namespace (matches ^[Tt]ruth)", f.File, f.Line, f.Ident)
}

// checkDir parses every *.go file directly inside dir (non-recursive — this
// repository's contract is scoped to go/chtypes/*.go itself, not its
// testdata/ subdirectory) and reports every top-level func, type, var and
// const identifier — including every member of a grouped block — that falls
// in the reserved range.
func checkDir(dir string) ([]Finding, error) {
	matches, err := filepath.Glob(filepath.Join(dir, "*.go"))
	if err != nil {
		return nil, fmt.Errorf("globbing %s/*.go: %w", dir, err)
	}
	if len(matches) == 0 {
		return nil, fmt.Errorf("%s/*.go matched no files — the parser has lost this tree; fix the path rather than trusting an empty run", dir)
	}
	sort.Strings(matches)

	fset := token.NewFileSet()
	var findings []Finding

	check := func(id *ast.Ident, path string) {
		if id == nil || id.Name == "_" {
			return
		}
		if reservedPattern.MatchString(id.Name) {
			pos := fset.Position(id.Pos())
			findings = append(findings, Finding{Ident: id.Name, File: path, Line: pos.Line})
		}
	}

	for _, path := range matches {
		file, err := parser.ParseFile(fset, path, nil, parser.SkipObjectResolution)
		if err != nil {
			return nil, fmt.Errorf("parsing %s: %w", path, err)
		}
		for _, decl := range file.Decls {
			switch d := decl.(type) {
			case *ast.FuncDecl:
				// A receiver means this is a method: it lives in <type>.Name,
				// never in the bare package namespace the overlay shares, so
				// it cannot collide and is deliberately not checked.
				if d.Recv != nil {
					continue
				}
				check(d.Name, path)
			case *ast.GenDecl:
				switch d.Tok {
				case token.TYPE:
					for _, spec := range d.Specs {
						ts, ok := spec.(*ast.TypeSpec)
						if !ok {
							continue
						}
						check(ts.Name, path)
					}
				case token.VAR, token.CONST:
					// Grouped (`var ( … )`) and single (`var Foo = 1`) forms
					// parse identically here — go/parser does not distinguish
					// them beyond where the parens sit, so every member of a
					// grouped block is walked exactly like a standalone
					// declaration. This is the property a line-anchored
					// regex cannot get right.
					for _, spec := range d.Specs {
						vs, ok := spec.(*ast.ValueSpec)
						if !ok {
							continue
						}
						for _, name := range vs.Names {
							check(name, path)
						}
					}
				}
			}
		}
	}

	sort.Slice(findings, func(i, j int) bool {
		if findings[i].File != findings[j].File {
			return findings[i].File < findings[j].File
		}
		return findings[i].Line < findings[j].Line
	})
	return findings, nil
}

func printFindings(w io.Writer, findings []Finding) {
	for _, f := range findings {
		fmt.Fprintln(w, f.String())
	}
	fmt.Fprintf(w, `
check-truth-namespace: %d identifier(s) declared in go/chtypes fall in the
  range reserved for the artifact producer's overlay tests
  (^[Tt]ruth or ^(Test|Benchmark|Fuzz|Example)Truth). go/chtypes and that
  overlay share one package namespace when it is compiled in with
  `+"`go test -overlay`"+`, and a name declared on both sides breaks that build.
  ^[Tt]ruth is a PURE PREFIX match, on purpose: it also reserves names like
  `+"`Truthy`"+` or `+"`Truthless`"+` that are not literally "Truth". That is not a bug in
  this lint — false positives here cost a rename, false negatives cost a
  broken build on the other side, and the safe direction is strict.
  Rename the identifier(s) above.
`, len(findings))
}

func runCheck() int {
	findings, err := checkDir(targetDir)
	if err != nil {
		fmt.Fprintf(os.Stderr, "check-truth-namespace: %v\n", err)
		return 2
	}
	if len(findings) > 0 {
		printFindings(os.Stderr, findings)
		return 1
	}
	fmt.Println("check-truth-namespace: ok — go/chtypes declares no identifier in the artifact producer's reserved Truth range")
	return 0
}

func main() {
	for _, arg := range os.Args[1:] {
		if arg == "--selftest" {
			os.Exit(runSelftest())
		}
	}
	os.Exit(runCheck())
}

// -------------------------------------------------------------- selftest

// plant is one thing to add to a temporary copy of go/chtypes and one
// expectation of what checkDir must (or must not) report for it. Every
// plant lives in its OWN new file, so it never has to locate an anchor
// string in existing source the way a find/replace plant would — a fresh,
// self-contained file is added, checked, and removed again before the next
// plant runs.
type plant struct {
	name        string // for the progress line
	file        string // filename written under the temp copy
	content     string
	wantIdent   string
	wantLine    int  // the identifier's line inside content, 1-based
	wantFlagged bool // false for the negative controls
}

var plants = []plant{
	{
		name:        "func TestTruthFoo",
		file:        "zz_selftest_functest.go",
		content:     "package chtypes\n\nfunc TestTruthFoo(t *testing.T) {}\n",
		wantIdent:   "TestTruthFoo",
		wantLine:    3,
		wantFlagged: true,
	},
	{
		name:        "lowercase func truthHelper",
		file:        "zz_selftest_lowerfunc.go",
		content:     "package chtypes\n\nfunc truthHelper() {}\n",
		wantIdent:   "truthHelper",
		wantLine:    3,
		wantFlagged: true,
	},
	{
		name:        "type TruthThing",
		file:        "zz_selftest_type.go",
		content:     "package chtypes\n\ntype TruthThing struct{}\n",
		wantIdent:   "TruthThing",
		wantLine:    3,
		wantFlagged: true,
	},
	{
		name:        "reserved name inside a grouped var ( … ) block",
		file:        "zz_selftest_var.go",
		content:     "package chtypes\n\nvar (\n\tsomethingElse = 1\n\tTruthValue    = 2\n)\n",
		wantIdent:   "TruthValue",
		wantLine:    5,
		wantFlagged: true,
	},
	{
		name:        "reserved name inside a grouped const ( … ) block",
		file:        "zz_selftest_const.go",
		content:     "package chtypes\n\nconst (\n\tsomethingElse = iota\n\tTruthConst\n)\n",
		wantIdent:   "TruthConst",
		wantLine:    5,
		wantFlagged: true,
	},
	{
		name:        "pure-prefix match: Truthy is reserved too, not only literal Truth",
		file:        "zz_selftest_truthy.go",
		content:     "package chtypes\n\ntype Truthy struct{}\n",
		wantIdent:   "Truthy",
		wantLine:    3,
		wantFlagged: true,
	},
	{
		name:        "negative control: func TestUnknownOutcome must NOT be flagged",
		file:        "zz_selftest_negative.go",
		content:     "package chtypes\n\nfunc TestUnknownOutcome(t *testing.T) {}\n",
		wantIdent:   "TestUnknownOutcome",
		wantLine:    3,
		wantFlagged: false,
	},
	{
		name:        "negative control: a method (receiver) named TruthMethod is excluded",
		file:        "zz_selftest_method.go",
		content:     "package chtypes\n\ntype something struct{}\n\nfunc (s *something) TruthMethod() {}\n",
		wantIdent:   "TruthMethod",
		wantLine:    5,
		wantFlagged: false,
	},
}

func copyFile(src, dst string) error {
	b, err := os.ReadFile(src)
	if err != nil {
		return err
	}
	return os.WriteFile(dst, b, 0o644)
}

func runSelftest() int {
	// House rule: refuse to run the selftest against a tree that already
	// fails. A plant that "fires" against a tree already red proves nothing.
	base, err := checkDir(targetDir)
	if err != nil {
		fmt.Fprintf(os.Stderr, "SELFTEST REFUSED: %v\n", err)
		return 1
	}
	if len(base) > 0 {
		fmt.Fprintln(os.Stderr, "SELFTEST REFUSED: the real tree does not pass, so a planted violation would prove nothing")
		printFindings(os.Stderr, base)
		return 1
	}

	tmp, err := os.MkdirTemp("", "check-truth-namespace-selftest-")
	if err != nil {
		fmt.Fprintf(os.Stderr, "SELFTEST FAILED: %v\n", err)
		return 1
	}
	defer os.RemoveAll(tmp)

	real, err := filepath.Glob(filepath.Join(targetDir, "*.go"))
	if err != nil || len(real) == 0 {
		fmt.Fprintf(os.Stderr, "SELFTEST FAILED: could not list %s/*.go: %v\n", targetDir, err)
		return 1
	}
	for _, src := range real {
		if err := copyFile(src, filepath.Join(tmp, filepath.Base(src))); err != nil {
			fmt.Fprintf(os.Stderr, "SELFTEST FAILED: copying %s: %v\n", src, err)
			return 1
		}
	}

	// The copy itself, with nothing planted yet, must still pass — never
	// trust a plant against a tree whose starting state you have not
	// verified.
	if findings, err := checkDir(tmp); err != nil || len(findings) > 0 {
		fmt.Fprintln(os.Stderr, "SELFTEST FAILED: the copied tree does not pass before anything is planted")
		if err != nil {
			fmt.Fprintln(os.Stderr, err)
		} else {
			printFindings(os.Stderr, findings)
		}
		return 1
	}

	for _, p := range plants {
		path := filepath.Join(tmp, p.file)
		if err := os.WriteFile(path, []byte(p.content), 0o644); err != nil {
			fmt.Fprintf(os.Stderr, "SELFTEST FAILED: planting %s: %v\n", p.name, err)
			return 1
		}

		findings, err := checkDir(tmp)
		removeErr := os.Remove(path) // revert before evaluating, so a failure below still leaves the copy clean
		if err != nil {
			fmt.Fprintf(os.Stderr, "SELFTEST FAILED: %s: checkDir errored: %v\n", p.name, err)
			return 1
		}
		if removeErr != nil {
			fmt.Fprintf(os.Stderr, "SELFTEST FAILED: %s: could not revert plant: %v\n", p.name, removeErr)
			return 1
		}

		var got *Finding
		for i := range findings {
			if findings[i].Ident == p.wantIdent && filepath.Base(findings[i].File) == p.file {
				got = &findings[i]
				break
			}
		}

		if p.wantFlagged {
			if got == nil {
				fmt.Fprintf(os.Stderr, "SELFTEST FAILED: the %q plant was not caught\n", p.name)
				return 1
			}
			if got.Line != p.wantLine {
				fmt.Fprintf(os.Stderr, "SELFTEST FAILED: the %q plant was caught at line %d, wanted line %d\n", p.name, got.Line, p.wantLine)
				return 1
			}
			// Exactly this one new finding — no interference from a stale
			// plant left behind by an earlier iteration.
			if len(findings) != 1 {
				fmt.Fprintf(os.Stderr, "SELFTEST FAILED: the %q plant produced %d finding(s), wanted exactly 1:\n", p.name, len(findings))
				printFindings(os.Stderr, findings)
				return 1
			}
			fmt.Printf("  plant %-55s caught: %s\n", p.name, got.String())
		} else {
			if got != nil || len(findings) != 0 {
				fmt.Fprintf(os.Stderr, "SELFTEST FAILED: the %q negative control was flagged and must not be:\n", p.name)
				printFindings(os.Stderr, findings)
				return 1
			}
			fmt.Printf("  plant %-55s correctly NOT flagged\n", p.name)
		}
	}

	// The temp copy must end exactly where it started: every plant reverted.
	if findings, err := checkDir(tmp); err != nil || len(findings) > 0 {
		fmt.Fprintln(os.Stderr, "SELFTEST FAILED: the copy did not return to a clean state after all plants were reverted")
		return 1
	}

	fmt.Println(strings.Repeat("-", 78))
	fmt.Printf("check-truth-namespace: selftest ok — %d plant(s) caught, %d negative control(s) stayed clean\n",
		countWant(true), countWant(false))
	return 0
}

func countWant(want bool) int {
	n := 0
	for _, p := range plants {
		if p.wantFlagged == want {
			n++
		}
	}
	return n
}
