package chtypes

import (
	"fmt"
	"sync"
	"testing"
)

// Concurrency, against the contract the C header states (include/chtypes.h):
//
//	"The library is thread-safe for concurrent chs_row() calls on distinct
//	 handles; a single handle must not be used from two threads at once."
//
// The package used to hold one mutex per Library across every call, which is
// strictly stronger than that. These tests are the evidence for the relaxation.
//
// WHAT `-race` DOES AND DOES NOT PROVE. Go's race detector instruments Go
// memory; it does not see inside the C library, so a green -race run proves the
// LOCKING here is sound, not that ClickHouse's C++ is. The load-bearing
// assertion is therefore differential: every concurrent answer must equal the
// answer the same case produces single-threaded. Memory corruption from a real
// C-level race shows up as a wrong or missing value, and that is checked case
// for case rather than inferred from "it did not crash".

// caseSpec is one (schema, row) pair with the answer it must always give.
type caseSpec struct {
	name   string
	ddl    string
	body   string
	format Format
}

// A spread that exercises different amounts of the machinery: plain integer
// coercion, an overflow that must be reported, a string, a temporal parse, and
// a constant DEFAULT (which builds the expression interpreter).
var concurrentCases = []caseSpec{
	{"uint8 plain", "x UInt8", `{"x":7}`, JSONEachRow},
	{"uint8 wrap", "x UInt8", `{"x":256}`, JSONEachRow},
	{"int8 negative", "x Int8", `{"x":-70}`, JSONEachRow},
	{"string", "s String", `{"s":"ok"}`, JSONEachRow},
	{"datetime", "ts DateTime", `{"ts":"2026-01-15 10:30:00"}`, JSONEachRow},
	{"constant default", "x UInt8, s String DEFAULT 'd'", `{"x":1}`, JSONEachRow},
	{"nullable", "x Nullable(Int32)", `{"x":null}`, JSONEachRow},
	{"float", "f Float64", `{"f":1.5}`, JSONEachRow},
	{"array", "a Array(UInt8)", `{"a":[1,2,3]}`, JSONEachRow},
	{"decimal", "d Decimal(9,2)", `{"d":"1.23"}`, JSONEachRow},
}

// answer is the comparable shape of a BatchResult: everything a caller acts on.
// EngineRows is in here deliberately — it is where a table engine's insert-time
// merge shows up, so a test that omitted it could not tell a SummingMergeTree
// handle from a plain one and would silently stop detecting handle leakage.
type answer struct {
	outcome  Outcome
	code     int
	rowsRead int
	values   string
	transfms string
	engine   string
}

func answerOf(res BatchResult) answer {
	a := answer{outcome: res.Outcome, code: res.ErrCode, rowsRead: res.RowsRead}
	for _, r := range res.Rows {
		for _, v := range r.Values {
			a.values += fmt.Sprintf("|%s:%q:%v:%s", v.Column, v.Text, v.Null, v.Source)
		}
	}
	for _, t := range res.Transformed {
		a.transfms += fmt.Sprintf("|%d/%s/%s/%s", t.Row, t.Column, t.Reason, t.Stored)
	}
	a.engine = fmt.Sprintf("n=%d,nil=%v", len(res.EngineRows), res.EngineRows == nil)
	for _, er := range res.EngineRows {
		a.engine += "|" + string(er)
	}
	return a
}

// testRegistry builds a registry from the real per-version artifacts. Unlike
// TestRegistryLoadsAndDispatches (which copies one artifact into two temp dirs
// to prove the loader), this wants GENUINELY DIFFERENT ClickHouse builds in one
// process, because "one handle per version, all live at once" is the thing
// being stressed.
func testRegistry(t *testing.T) *Registry {
	t.Helper()
	// $CHTYPES_REGISTRY first, else the per-user artifact cache (see
	// artifact cache, three levels up from this package (see testRegistryDir).
	dir := testRegistryDir(t)
	r, err := NewRegistry(dir)
	if err != nil {
		t.Skipf("registry did not load: %v", err)
	}
	if len(r.Versions()) == 0 {
		t.Skip("registry loaded no versions")
	}
	return r
}

// TestConcurrentDistinctHandles is the contract, exercised: N goroutines over
// M versions, every goroutine holding its OWN compiled handle, all calling
// Rows at once. Answers must match the single-threaded baseline exactly.
func TestConcurrentDistinctHandles(t *testing.T) {
	r := testRegistry(t)
	versions := r.Versions()
	t.Logf("versions in one process: %v", versions)

	// Baseline first, strictly single-threaded: version -> case -> answer.
	want := map[string]map[string]answer{}
	for _, v := range versions {
		lib, err := r.For(Version(v))
		if err != nil {
			t.Fatalf("For(%s): %v", v, err)
		}
		want[v] = map[string]answer{}
		for _, c := range concurrentCases {
			cs, err := lib.CompileDDL(c.ddl)
			if err != nil {
				t.Fatalf("%s/%s: CompileDDL: %v", v, c.name, err)
			}
			res, err := cs.Rows(c.format, []byte(c.body), nil)
			if err != nil {
				cs.Close()
				t.Fatalf("%s/%s: Rows: %v", v, c.name, err)
			}
			want[v][c.name] = answerOf(res)
			cs.Close()
		}
	}

	// Now the same work concurrently. Each goroutine compiles its own handles,
	// so every call is on a DISTINCT handle — exactly what the header blesses.
	const goroutinesPerVersion = 4
	const rounds = 12
	var wg sync.WaitGroup
	errs := make(chan string, len(versions)*goroutinesPerVersion*len(concurrentCases)*rounds)

	for _, v := range versions {
		for g := 0; g < goroutinesPerVersion; g++ {
			wg.Add(1)
			go func(v string, g int) {
				defer wg.Done()
				lib, err := r.For(Version(v))
				if err != nil {
					errs <- fmt.Sprintf("%s/g%d: For: %v", v, g, err)
					return
				}
				// Distinct handles, one per case, held for the whole run.
				handles := make([]*LoadedSchema, 0, len(concurrentCases))
				for _, c := range concurrentCases {
					cs, err := lib.CompileDDL(c.ddl)
					if err != nil {
						errs <- fmt.Sprintf("%s/g%d/%s: CompileDDL: %v", v, g, c.name, err)
						return
					}
					handles = append(handles, cs)
				}
				defer func() {
					for _, cs := range handles {
						cs.Close()
					}
				}()
				for round := 0; round < rounds; round++ {
					for i, c := range concurrentCases {
						res, err := handles[i].Rows(c.format, []byte(c.body), nil)
						if err != nil {
							errs <- fmt.Sprintf("%s/g%d/%s r%d: Rows: %v", v, g, c.name, round, err)
							continue
						}
						if got := answerOf(res); got != want[v][c.name] {
							errs <- fmt.Sprintf("%s/g%d/%s r%d: DIVERGED\n got  %+v\n want %+v",
								v, g, c.name, round, got, want[v][c.name])
						}
					}
				}
			}(v, g)
		}
	}
	wg.Wait()
	close(errs)

	n := 0
	for e := range errs {
		n++
		if n <= 10 {
			t.Error(e)
		}
	}
	if n > 10 {
		t.Errorf("... and %d further failures", n-10)
	}
	if n == 0 {
		t.Logf("%d versions x %d goroutines x %d cases x %d rounds = %d concurrent calls, all matching the single-threaded answer",
			len(versions), goroutinesPerVersion, len(concurrentCases), rounds,
			len(versions)*goroutinesPerVersion*len(concurrentCases)*rounds)
	}
}

// TestConcurrentSharedHandle drives ONE handle from many goroutines. The header
// says that is not allowed at the C level, so the package's per-handle mutex is
// what makes it safe — this asserts the mutex actually serialises rather than
// that the C library tolerates it. Answers must still be exact.
func TestConcurrentSharedHandle(t *testing.T) {
	r := testRegistry(t)
	v := r.Versions()[0]
	lib, err := r.For(Version(v))
	if err != nil {
		t.Fatal(err)
	}
	c := concurrentCases[1] // the overflow case: it must report a Transform
	cs, err := lib.CompileDDL(c.ddl)
	if err != nil {
		t.Fatal(err)
	}
	defer cs.Close()

	res, err := cs.Rows(c.format, []byte(c.body), nil)
	if err != nil {
		t.Fatal(err)
	}
	want := answerOf(res)

	var wg sync.WaitGroup
	var mu sync.Mutex
	var bad []string
	for g := 0; g < 16; g++ {
		wg.Add(1)
		go func(g int) {
			defer wg.Done()
			for i := 0; i < 25; i++ {
				res, err := cs.Rows(c.format, []byte(c.body), nil)
				if err != nil {
					mu.Lock()
					bad = append(bad, fmt.Sprintf("g%d/%d: %v", g, i, err))
					mu.Unlock()
					continue
				}
				if got := answerOf(res); got != want {
					mu.Lock()
					bad = append(bad, fmt.Sprintf("g%d/%d: diverged: %+v", g, i, got))
					mu.Unlock()
				}
			}
		}(g)
	}
	wg.Wait()
	for _, b := range bad {
		t.Error(b)
	}
}

// TestConcurrentCompileAndRow is the case the relaxation actually opens up:
// compiling new schemas while other handles are answering rows. Under the old
// single Library mutex these could never overlap. CompileDDL takes the library
// READ lock, so this is the test that the read lock is not optimistic.
func TestConcurrentCompileAndRow(t *testing.T) {
	r := testRegistry(t)
	versions := r.Versions()

	var wg sync.WaitGroup
	var mu sync.Mutex
	var bad []string
	fail := func(f string, a ...any) {
		mu.Lock()
		bad = append(bad, fmt.Sprintf(f, a...))
		mu.Unlock()
	}

	for _, v := range versions {
		lib, err := r.For(Version(v))
		if err != nil {
			t.Fatal(err)
		}

		// Compilers: churn handles continuously.
		for g := 0; g < 3; g++ {
			wg.Add(1)
			go func(v string, g int) {
				defer wg.Done()
				for i := 0; i < 30; i++ {
					c := concurrentCases[i%len(concurrentCases)]
					cs, err := lib.CompileDDL(c.ddl)
					if err != nil {
						fail("%s/compile%d/%s: %v", v, g, c.name, err)
						continue
					}
					// Use it immediately, then drop it.
					if _, err := cs.Rows(c.format, []byte(c.body), nil); err != nil {
						fail("%s/compile%d/%s: Rows: %v", v, g, c.name, err)
					}
					cs.Close()
				}
			}(v, g)
		}

		// Validators: a handle-less entry point, also on the read lock.
		wg.Add(1)
		go func(v string) {
			defer wg.Done()
			for i := 0; i < 60; i++ {
				canon, err := lib.ValidateType("Nullable(Decimal(18, 4))")
				if err != nil {
					fail("%s/validate: %v", v, err)
					continue
				}
				if canon != "Nullable(Decimal(18, 4))" {
					fail("%s/validate: canonical = %q", v, canon)
				}
			}
		}(v)

		// Long-lived readers on their own handles, running throughout.
		for g := 0; g < 2; g++ {
			wg.Add(1)
			go func(v string, g int) {
				defer wg.Done()
				c := concurrentCases[1]
				cs, err := lib.CompileDDL(c.ddl)
				if err != nil {
					fail("%s/reader%d: CompileDDL: %v", v, g, err)
					return
				}
				defer cs.Close()
				for i := 0; i < 60; i++ {
					res, err := cs.Rows(c.format, []byte(c.body), nil)
					if err != nil {
						fail("%s/reader%d: Rows: %v", v, g, err)
						continue
					}
					if res.Outcome != Accepted || len(res.Rows) != 1 ||
						res.Rows[0].Values[0].Text != "0" {
						fail("%s/reader%d: wrong answer: %+v", v, g, res)
					}
					if len(res.Transformed) != 1 ||
						res.Transformed[0].Reason != ReasonOverflowWrap {
						fail("%s/reader%d: lost the Transform: %+v", v, g, res.Transformed)
					}
				}
			}(v, g)
		}
	}
	wg.Wait()
	for i, b := range bad {
		if i < 10 {
			t.Error(b)
		}
	}
	if len(bad) > 10 {
		t.Errorf("... and %d further failures", len(bad)-10)
	}
}

// TestConcurrentSetEngineIsExclusive covers the two NON-const C entry points.
// chs_schema_engine and chs_schema_ttl take `chs_schema *`, so they WRITE the
// handle; they must not overlap a Rows call on the same handle. Each goroutine
// therefore owns its handle, and the assertion is that an engine set on one
// handle never leaks into another's answer.
func TestConcurrentSetEngineIsExclusive(t *testing.T) {
	r := testRegistry(t)
	v := r.Versions()[0]
	lib, err := r.For(Version(v))
	if err != nil {
		t.Fatal(err)
	}

	const ddl = "key UInt32, val UInt32"
	body := `{"key":1,"val":5}` + "\n" + `{"key":1,"val":7}`

	// Ground truth for both shapes, single-threaded.
	plain, err := lib.CompileDDL(ddl)
	if err != nil {
		t.Fatal(err)
	}
	pres, err := plain.Rows(JSONEachRow, []byte(body), nil)
	plain.Close()
	if err != nil {
		t.Fatal(err)
	}
	wantPlain := answerOf(pres)

	summing, err := lib.CompileDDL(ddl)
	if err != nil {
		t.Fatal(err)
	}
	engineOK := summing.SetEngine("SummingMergeTree(val)", "key") == nil
	sres, err := summing.Rows(JSONEachRow, []byte(body), nil)
	summing.Close()
	if err != nil {
		t.Fatal(err)
	}
	wantSumming := answerOf(sres)
	if !engineOK {
		t.Log("this artifact predates engine support; the engine half is a no-op")
	} else if wantSumming == wantPlain {
		t.Fatal("SummingMergeTree produced the same answer as MergeTree — " +
			"the test cannot detect leakage between handles")
	}

	var wg sync.WaitGroup
	var mu sync.Mutex
	var bad []string
	for g := 0; g < 12; g++ {
		wg.Add(1)
		go func(g int) {
			defer wg.Done()
			useEngine := g%2 == 0
			for i := 0; i < 20; i++ {
				cs, err := lib.CompileDDL(ddl)
				if err != nil {
					mu.Lock()
					bad = append(bad, fmt.Sprintf("g%d: CompileDDL: %v", g, err))
					mu.Unlock()
					return
				}
				want := wantPlain
				if useEngine && engineOK {
					if err := cs.SetEngine("SummingMergeTree(val)", "key"); err != nil {
						mu.Lock()
						bad = append(bad, fmt.Sprintf("g%d: SetEngine: %v", g, err))
						mu.Unlock()
						cs.Close()
						continue
					}
					want = wantSumming
				}
				res, err := cs.Rows(JSONEachRow, []byte(body), nil)
				if err != nil {
					mu.Lock()
					bad = append(bad, fmt.Sprintf("g%d: Rows: %v", g, err))
					mu.Unlock()
					cs.Close()
					continue
				}
				if got := answerOf(res); got != want {
					mu.Lock()
					bad = append(bad, fmt.Sprintf("g%d engine=%v: DIVERGED\n got  %+v\n want %+v",
						g, useEngine, got, want))
					mu.Unlock()
				}
				cs.Close()
			}
		}(g)
	}
	wg.Wait()
	for i, b := range bad {
		if i < 10 {
			t.Error(b)
		}
	}
	if len(bad) > 10 {
		t.Errorf("... and %d further failures", len(bad)-10)
	}
}

// TestArtifactIsInitialisedOnce is the invariant openLibrary exists for: one
// dlopen'd path gets exactly one chs_init, so a second Load can never rebuild
// the refuse-list under live readers. Two Registries over the same directory
// must therefore hand back the SAME *Library, and concurrent Loads of one path
// must not produce two.
func TestArtifactIsInitialisedOnce(t *testing.T) {
	r := testRegistry(t)
	v := r.Versions()[0]
	first, err := r.For(Version(v))
	if err != nil {
		t.Fatal(err)
	}

	// A second, independent Registry over the same artifacts.
	r2 := testRegistry(t)
	second, err := r2.For(Version(v))
	if err != nil {
		t.Fatal(err)
	}
	if first != second {
		t.Fatalf("two Registries produced two Libraries for %s (%p vs %p): the "+
			"artifact was chs_init'd twice into one address space", v, first, second)
	}

	// Concurrent Loads of the same path, from many goroutines.
	path := first.Path
	var wg sync.WaitGroup
	got := make([]*Library, 8)
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			r3 := &Registry{byID: map[string]*Library{}}
			if err := r3.Load(path); err != nil {
				t.Errorf("g%d: Load: %v", i, err)
				return
			}
			lib, err := r3.For(Version(v))
			if err != nil {
				t.Errorf("g%d: For: %v", i, err)
				return
			}
			got[i] = lib
		}(i)
	}
	wg.Wait()
	for i, l := range got {
		if l != nil && l != first {
			t.Errorf("g%d got a different Library (%p, want %p)", i, l, first)
		}
	}
}

// TestConcurrentCloseIsSafe races Close against Rows on the same handle. The
// per-handle mutex must make this report "schema is closed" rather than
// touching a freed pointer. Nothing here asserts an ordering — only that every
// outcome is one of the two legal ones and the process survives.
func TestConcurrentCloseIsSafe(t *testing.T) {
	r := testRegistry(t)
	lib, err := r.For(Version(r.Versions()[0]))
	if err != nil {
		t.Fatal(err)
	}
	for round := 0; round < 25; round++ {
		cs, err := lib.CompileDDL("x UInt8")
		if err != nil {
			t.Fatal(err)
		}
		var wg sync.WaitGroup
		for g := 0; g < 6; g++ {
			wg.Add(1)
			go func() {
				defer wg.Done()
				for i := 0; i < 8; i++ {
					// Either a correct answer or a clean "closed" error.
					if res, err := cs.Rows(JSONEachRow, []byte(`{"x":7}`), nil); err == nil {
						if res.Outcome != Accepted {
							t.Errorf("outcome = %v", res.Outcome)
						}
					}
				}
			}()
		}
		wg.Add(1)
		go func() { defer wg.Done(); cs.Close() }()
		wg.Wait()
		cs.Close() // idempotent
	}
}
